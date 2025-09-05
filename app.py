# app.py — Vicky Fase 1 con extras Fase 2 (compatibles y sin romper Fase 1)
# Ejecuta: python app.py  (usar .env con META_TOKEN, PHONE_NUMBER_ID, VERIFY_TOKEN, etc.)
# - Incluye funnel de ventas (SPIN ligero + scoring + handoff)
# - Soporte interactive (button_reply/list_reply)
# - Idempotencia básica (anti-duplicados por wamid, TTL 5 min)
# - Reenvío de medios al asesor + acuse único a los ~10s
# - Validación opcional de firma X-Hub-Signature-256 (si META_APP_SECRET está presente)
# - Endpoints admin mínimos: /admin/status (requiere X-Admin-Token)
# - Stubs NO intrusivos para RAG/broadcasts (se activarán en Fase 2 con envs y deps extra)

import os, json, logging, time, uuid, hmac, hashlib
from threading import Timer
from typing import Dict, Any, Optional, List

from flask import Flask, request, Response, jsonify
import requests
from dotenv import load_dotenv

# =========================
# Configuración y Entorno
# =========================
load_dotenv()

META_TOKEN           = os.getenv("META_TOKEN", "")
PHONE_NUMBER_ID     = os.getenv("PHONE_NUMBER_ID", "")
WA_API_VERSION      = os.getenv("WA_API_VERSION", "v23.0")
VERIFY_TOKEN        = os.getenv("VERIFY_TOKEN", "vicky-verify-2025")
ADVISOR_NOTIFY      = os.getenv("ADVISOR_NOTIFY_NUMBER", "5216682478005")
PORT                = int(os.getenv("PORT", "5000"))
TZ                  = os.getenv("TZ", "America/Mazatlan")
META_APP_SECRET     = os.getenv("META_APP_SECRET", "")  # opcional: si está, se valida firma
ADMIN_TOKEN         = os.getenv("ADMIN_TOKEN", "")       # para endpoints admin

# Flags futuras (no influyen si faltan envs)
RAG_ENABLED         = os.getenv("RAG_ENABLED", "false").lower() == "true"
BROADCAST_ENABLED   = os.getenv("BROADCAST_ENABLED", "false").lower() == "true"

# Logs
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("vicky")

app = Flask(__name__)

# =========================
# Estado en memoria (Fase 1)
# =========================
SESSION: Dict[str, Dict[str, Any]] = {}
DEDUP: Dict[str, Dict[str, float]] = {}
DEDUP_TTL = 300.0       # 5 minutos
ACK_SECONDS = 10.0      # acuse único de documentos

MENU = (
    "Hola 👋 soy Vicky, Asistente de Christian López.\n\n"
    "Puedo ayudarte con:\n"
    "1) Seguro de auto\n"
    "2) Salud / Vida (VRIM, gastos médicos, seguro de vida, seguro para ahorro, seguro de vida mixto)\n"
    "3) Pensión IMSS (Ley 73)\n"
    "4) Crédito empresarial / Nómina\n"
    "5) Contactar a Christian (pídeme el tema y le aviso en privado)\n\n"
    "Responde con el número de la opción que necesitas 👇"
)

# =========================
# Utilidades de sesión (memoria)
# =========================

def _get(u: str) -> Dict[str, Any]:
    return SESSION.setdefault(u, {"stage": "none", "data": {}, "media_batch": [], "timer": None, "funnel": {}, "last_seen": time.time()})

def set_stage(u: str, stage: str, data: Optional[Dict[str, Any]] = None) -> None:
    s = _get(u)
    s["stage"] = stage
    if data:
        s["data"].update(data)
    s["last_seen"] = time.time()

def get_stage(u: str) -> str:
    return _get(u).get("stage", "none")

def set_funnel(u: str, product: str, q_list: List[str]) -> None:
    s = _get(u)
    s["funnel"] = {"product": product, "q": q_list, "answers": [], "idx": 0, "score": 0}
    s["stage"] = "qualifying"

def clear(u: str) -> None:
    s = _get(u)
    t = s.get("timer")
    if t:
        try:
            t.cancel()
        except Exception:
            pass
    SESSION[u] = {"stage": "none", "data": {}, "media_batch": [], "timer": None, "funnel": {}, "last_seen": time.time()}

def remember_media(u: str, media_id: str) -> None:
    s = _get(u)
    if media_id:
        s["media_batch"].append(media_id)

def schedule_ack(u: str) -> None:
    s = _get(u)
    if s.get("timer"):
        return
    def _ack():
        try:
            count = len(_get(u).get("media_batch", []))
            if count:
                send_text(u, "Recibí tus documentos ✅ Estoy validando la información para tu cotización.")
                _get(u)["media_batch"].clear()
        finally:
            _get(u)["timer"] = None
    t = Timer(ACK_SECONDS, _ack)
    t.daemon = True
    t.start()
    s["timer"] = t

def dedup(u: str, wamid: Optional[str]) -> bool:
    if not wamid:
        return False
    bucket = DEDUP.setdefault(u, {})
    now = time.time()
    for mid, ts in list(bucket.items()):
        if now - ts > DEDUP_TTL:
            bucket.pop(mid, None)
    if wamid in bucket:
        return True
    bucket[wamid] = now
    return False

# =========================
# Seguridad: firma webhook (opcional)
# =========================

def verify_signature(body: bytes, header_sig: Optional[str]) -> bool:
    if not META_APP_SECRET:
        return True  # validación opcional desactivada
    if not header_sig:
        return False
    try:
        algo, their_sig = header_sig.split("=", 1)
        if algo != "sha256":
            return False
    except Exception:
        return False
    mac = hmac.new(META_APP_SECRET.encode("utf-8"), msg=body, digestmod=hashlib.sha256)
    expected = mac.hexdigest()
    # comparación en tiempo constante
    return hmac.compare_digest(their_sig, expected)

# =========================
# WhatsApp Helpers
# =========================

def _wa_url() -> str:
    return f"https://graph.facebook.com/{WA_API_VERSION}/{PHONE_NUMBER_ID}/messages"

def _wa_headers() -> Dict[str, str]:
    return {"Authorization": f"Bearer {META_TOKEN}", "Content-Type": "application/json"}

def _can_send() -> bool:
    ok = bool(META_TOKEN and PHONE_NUMBER_ID)
    if not ok:
        log.error("Faltan credenciales de WhatsApp: META_TOKEN/PHONE_NUMBER_ID.")
    return ok

def _log_send(to: str, resp: requests.Response, payload: dict) -> None:
    try:
        j = resp.json()
    except Exception:
        j = {"raw": resp.text}
    log.info("→ %s | %s | %s", to, resp.status_code, json.dumps(j, ensure_ascii=False))

def send_text(to: str, body: str) -> int:
    if not _can_send():
        return 0
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": body, "preview_url": False}}
    try:
        r = requests.post(_wa_url(), headers=_wa_headers(), json=payload, timeout=30)
        _log_send(to, r, payload)
        return r.status_code
    except Exception as e:
        log.exception(f"send_text error: {e}")
        return 0

def send_media(to: str, media_type: str, media_id: str, caption: Optional[str] = None) -> int:
    if not _can_send():
        return 0
    item = {"id": media_id}
    if caption:
        item["caption"] = caption
    payload = {"messaging_product": "whatsapp", "to": to, "type": media_type, media_type: item}
    try:
        r = requests.post(_wa_url(), headers=_wa_headers(), json=payload, timeout=30)
        _log_send(to, r, {"type": media_type, "id": media_id})
        return r.status_code
    except Exception as e:
        log.exception(f"send_media error: {e}")
        return 0

# Plantillas (stub para Fase 2; no rompe si no se usa)
def send_template(to: str, template_name: str, lang_code: str, components: list) -> int:
    if not _can_send():
        return 0
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {"name": template_name, "language": {"code": lang_code}, "components": components},
    }
    try:
        r = requests.post(_wa_url(), headers=_wa_headers(), json=payload, timeout=30)
        _log_send(to, r, payload)
        return r.status_code
    except Exception as e:
        log.exception(f"send_template error: {e}")
        return 0

# =========================
# Funnel: preguntas y scoring
# =========================
PRODUCT_QUESTIONS = {
    "auto": [
        "¿El uso del vehículo es *particular* o *plataforma* (Uber/DiDi)?",
        "¿Cuál es *modelo y año* del vehículo?",
    ],
    "saludvida": [
        "¿Qué buscas exactamente? (VRIM, gastos médicos, vida, ahorro, mixto)",
        "¿Para cuántas personas sería?",
    ],
    "pension": [
        "¿Tu primera cotización al IMSS fue antes de 1997? (sí/no)",
        "¿Cuál es tu edad actual?",
    ],
    "credito_nomina": [
        "¿Es para *empresa* o *persona física*?",
        "¿Qué monto aproximado necesitas?",
    ],
}

URGENCY_WORDS = {"hoy", "ahora", "de inmediato", "esta semana", "urgente"}

def score_lead(from_number: str) -> int:
    s = _get(from_number)
    f = s.get("funnel", {})
    product = f.get("product")
    answers = f.get("answers", [])
    score = 0
    if product:
        score += 30
    if answers and len(answers) >= len(PRODUCT_QUESTIONS.get(product, [])):
        score += 20
    if product == "auto" and s.get("media_batch"):
        score += 20
    if s["data"].get("source") == "ctwa":
        score += 10
    last = s["data"].get("last_text_norm", "")
    if any(w in last for w in URGENCY_WORDS):
        score += 20
    f["score"] = score
    s["funnel"] = f
    return score

def next_qual_question(from_number: str) -> Optional[str]:
    f = _get(from_number).get("funnel", {})
    q = f.get("q", [])
    idx = f.get("idx", 0)
    if idx < len(q):
        return q[idx]
    return None

def register_answer(from_number: str, text: str) -> None:
    f = _get(from_number)["funnel"]
    f["answers"].append((text or "").strip())
    f["idx"] = f.get("idx", 0) + 1
    _get(from_number)["funnel"] = f

def finalize_or_continue_funnel(from_number: str) -> None:
    qn = next_qual_question(from_number)
    if qn:
        send_text(from_number, qn)
        return
    sc = score_lead(from_number)
    if sc >= 70:
        _notify_advisor(from_number, reason="lead_calificado")
        send_text(from_number, "Tengo suficiente información. Ya notifiqué a Christian; te contactará en breve.")
        clear(from_number)
    elif 40 <= sc < 70:
        send_text(from_number, "Gracias. Puedo preparar opciones y enviártelas por aquí. ¿Te parece si te mando 3 propuestas?")
    else:
        send_text(from_number, "Te ayudo con gusto. Si quieres ver opciones, escribe *menu*.")

def _notify_advisor(from_number: str, reason: str, extra: Optional[Dict[str, Any]] = None) -> None:
    s = _get(from_number)
    f = s.get("funnel", {})
    resumen = {
        "from": from_number,
        "reason": reason,
        "product": f.get("product"),
        "answers": f.get("answers"),
        "score": f.get("score", 0),
        "docs_in_auto_session": len(s.get("media_batch", [])),
    }
    if extra:
        resumen.update(extra)
    body = "🔔 Lead nuevo\n" + json.dumps(resumen, ensure_ascii=False)
    send_text(ADVISOR_NOTIFY, body)

# =========================
# Endpoints
# =========================
@app.get("/health")
def health():
    return "OK", 200

@app.get("/webhook")
def verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return Response(challenge or "", status=200)
    return Response("Forbidden", status=403)

@app.post("/webhook")
def webhook():
    # Verificación opcional de firma (no rompe Fase 1 si no hay secret)
    body = request.get_data() or b""
    sig = request.headers.get("X-Hub-Signature-256")
    if not verify_signature(body, sig):
        return Response("Invalid signature", status=403)

    req_id = str(uuid.uuid4())
    t0 = time.time()
    data = request.get_json(silent=True) or {}
    log.info(f"[{req_id}] Entrada: {json.dumps(data, ensure_ascii=False)}")

    try:
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                if "statuses" in value:
                    continue
                for message in value.get("messages", []):
                    _handle_message(value, message)
    except Exception as e:
        log.exception(f"[{req_id}] error manejando mensaje: {e}")

    ms = int((time.time() - t0) * 1000)
    log.info(f"[{req_id}] OK 200 ({ms}ms)")
    return Response("EVENT_RECEIVED", status=200)

# --- Admin mínimos (seguros y no intrusivos) ---
@app.get("/admin/status")
def admin_status():
    if not ADMIN_TOKEN or request.headers.get("X-Admin-Token") != ADMIN_TOKEN:
        return Response("Forbidden", status=403)
    status = {
        "signature_enabled": bool(META_APP_SECRET),
        "rag_enabled": RAG_ENABLED,
        "broadcast_enabled": BROADCAST_ENABLED,
        "funnel": True,
        "version": "fase1+extras",
        "tz": TZ,
    }
    return jsonify(status), 200

# =========================
# Handler principal
# =========================

def _handle_message(value: Dict[str, Any], message: Dict[str, Any]) -> None:
    from_number = message.get("from", "")
    wamid = message.get("id")

    if dedup(from_number, wamid):
        log.info("Duplicado ignorado: %s %s", from_number, wamid)
        return

    mtype = message.get("type", "text")
    text_norm = ""

    if mtype == "text":
        text_norm = (message.get("text", {}) or {}).get("body", "")
    elif mtype == "interactive":
        interactive = message.get("interactive", {}) or {}
        br = (interactive.get("button_reply") or {}).get("title")
        lr = (interactive.get("list_reply") or {}).get("title")
        # Si en botones usamos ids numéricos ("1".."5"), también intentamos mapear por id
        bid = (interactive.get("button_reply") or {}).get("id")
        lid = (interactive.get("list_reply") or {}).get("id")
        text_norm = (bid or lid or br or lr or "").strip()
    else:
        text_norm = ""

    text_norm_low = (text_norm or "").strip().lower()
    _get(from_number)["data"]["last_text_norm"] = text_norm_low

    # Opt-out
    if text_norm_low in {"alto", "baja", "stop"}:
        send_text(from_number, "He tomado nota. No volverás a recibir mensajes de promoción. Si necesitas algo, escribe *hola*.")
        clear(from_number)
        return

    # CTWA (campañas)
    if message.get("referral"):
        _get(from_number)["data"]["source"] = "ctwa"

    # Medios: reenviar a asesor + acuse único en sesión de auto
    if mtype in {"image", "document"}:
        media_obj = message.get(mtype) or {}
        media_id = media_obj.get("id")
        if media_id and ADVISOR_NOTIFY:
            caption = f"Docs de {from_number}"
            send_media(ADVISOR_NOTIFY, "image" if mtype == "image" else "document", media_id, caption)
        if get_stage(from_number) == "auto_docs":
            remember_media(from_number, media_id or "")
            schedule_ack(from_number)
            return
        send_text(from_number, "Gracias, ya registré tu archivo. Si quieres ver opciones, escribe *menu*.")
        return

    # Comandos globales
    if text_norm_low in {"hola", "menu", "menú"}:
        clear(from_number)
        send_text(from_number, MENU)
        return

    # Contactar (esperando tema)
    if get_stage(from_number) == "awaiting_topic" and mtype in {"text", "interactive"}:
        topic = (text_norm or "").strip()
        send_text(from_number, "¡Gracias! Ya notifiqué a Christian. Te contactará en breve.")
        _notify_advisor(from_number, reason="contacto_directo", extra={"topic": topic})
        clear(from_number)
        return

    # Calificación en curso
    if get_stage(from_number) == "qualifying" and mtype in {"text", "interactive"}:
        register_answer(from_number, text_norm or "")
        finalize_or_continue_funnel(from_number)
        return

    # Elección de menú (1..5)
    if mtype in {"text", "interactive"} and text_norm_low in {"1", "2", "3", "4", "5"}:
        opt = text_norm_low
        if opt == "1":
            set_stage(from_number, "auto_docs")
            set_funnel(from_number, "auto", PRODUCT_QUESTIONS["auto"])
            send_text(from_number, "🚗 Perfecto. Envíame *foto de tu INE* y *foto de la tarjeta de circulación*. Yo me encargo del resto.")
            finalize_or_continue_funnel(from_number)
            return
        if opt == "2":
            set_funnel(from_number, "saludvida", PRODUCT_QUESTIONS["saludvida"])
            send_text(from_number, "🏥 Con gusto. Te hago un par de preguntas rápidas para afinar tu propuesta.")
            finalize_or_continue_funnel(from_number)
            return
        if opt == "3":
            set_funnel(from_number, "pension", PRODUCT_QUESTIONS["pension"])
            send_text(from_number, "🧓 Claro. Te hago un par de preguntas para orientarte mejor.")
            finalize_or_continue_funnel(from_number)
            return
        if opt == "4":
            set_funnel(from_number, "credito_nomina", PRODUCT_QUESTIONS["credito_nomina"])
            send_text(from_number, "💼 Perfecto. Te hago un par de preguntas rápidas.")
            finalize_or_continue_funnel(from_number)
            return
        if opt == "5":
            set_stage(from_number, "awaiting_topic")
            send_text(from_number, "Con gusto coordino el contacto con Christian. ¿Sobre qué *tema* te gustaría hablar?")
            return

    # Fallback humano
    send_text(from_number, "Gracias, te ayudo con eso. Si quieres ver opciones, escribe *menu*.")

# =========================
# Main
# =========================
if __name__ == "__main__":
    log.info("Vicky iniciando en http://127.0.0.1:%s (TZ=%s) — firma=%s, rag=%s, broadcast=%s", PORT, TZ, bool(META_APP_SECRET), RAG_ENABLED, BROADCAST_ENABLED)
    app.run(host="127.0.0.1", port=PORT, debug=True)
