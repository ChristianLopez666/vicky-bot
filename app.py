# app.py — Vicky SECOM (parche completo 2025-10-30)
# Python 3.10+
# Run in Render: gunicorn app:app --bind 0.0.0.0:$PORT

import os
import io
import re
import json
import time
import logging
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, List, Tuple

import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# --- PDF ---
from PyPDF2 import PdfReader

# --- Google APIs ---
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials

# --- OpenAI ---
try:
    from openai import OpenAI
except Exception:  # compat con clientes antiguos
    OpenAI = None

load_dotenv()

# =============================================================
# CONFIG
# =============================================================
META_TOKEN = os.getenv("META_TOKEN", "").strip()
WABA_PHONE_ID = os.getenv("WABA_PHONE_ID", os.getenv("PHONE_NUMBER_ID", "").strip())
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "").strip()
ADVISOR_NUMBER = os.getenv("ADVISOR_NUMBER", os.getenv("ADVISOR_WHATSAPP", "5216682478005")).strip()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()

GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", os.getenv("SHEETS_ID_LEADS", "").strip())
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", os.getenv("SHEETS_TITLE_LEADS", "Prospectos SECOM Auto")).strip()
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON", "").strip()

MANUALES_VICKY_FOLDER_ID = os.getenv("MANUALES_VICKY_FOLDER_ID", "").strip()
MANUALES_VICKY_FOLDER_NAME = os.getenv("MANUALES_VICKY_FOLDER_NAME", "Manuales Vicky").strip()

# =============================================================
# LOGGING
# =============================================================
log = logging.getLogger("vicky-secom")
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s [vicky-secom] %(message)s')

# =============================================================
# APP + ESTADOS
# =============================================================
app = Flask(__name__)

user_state: Dict[str, str] = {}         # estado conversacional actual
user_ctx: Dict[str, Dict[str, Any]] = {} # contexto (match de sheet, etc.)
last_sent: Dict[str, str] = {}

# Saludo solo una vez por 24 h
greeted_at: Dict[str, datetime] = {}
GREET_WINDOW_HOURS = 24

# Evitar reprocesar reintentos duplicados de Meta
processed_msg_ids: Dict[str, datetime] = {}

# =============================================================
# GOOGLE CLIENTS (RO)
# =============================================================
google_ready = False
sheets = None
service_drive = None

def _init_google_clients():
    global google_ready, sheets, service_drive
    try:
        if not GOOGLE_CREDENTIALS_JSON:
            log.warning("GOOGLE_CREDENTIALS_JSON vacío — Google deshabilitado")
            return
        info = json.loads(GOOGLE_CREDENTIALS_JSON)
        scopes = [
            'https://www.googleapis.com/auth/spreadsheets.readonly',
            'https://www.googleapis.com/auth/drive.readonly'
        ]
        creds = Credentials.from_service_account_info(info, scopes=scopes)
        sheets = build('sheets', 'v4', credentials=creds)
        service_drive = build('drive', 'v3', credentials=creds)
        google_ready = True
        log.info("Google listo (Sheets RO + Drive RO)")
    except Exception:
        log.exception("No se pudo inicializar Google clients")
        google_ready = False
        sheets = None
        service_drive = None

_init_google_clients()

# =============================================================
# OPENAI CLIENT
# =============================================================
client_oa = None
if OPENAI_API_KEY and OpenAI is not None:
    try:
        client_oa = OpenAI(api_key=OPENAI_API_KEY)
    except Exception:
        log.exception("No se pudo inicializar OpenAI")
        client_oa = None
else:
    if not OPENAI_API_KEY:
        log.warning("OPENAI_API_KEY faltante")
    if OpenAI is None:
        log.warning("Paquete openai (v1) no disponible")

# =============================================================
# HELPERS
# =============================================================
WA_BASE = f"https://graph.facebook.com/v20.0/{WABA_PHONE_ID}/messages"

HEADERS_WA = {
    "Authorization": f"Bearer {META_TOKEN}",
    "Content-Type": "application/json"
}

def send_message(to: str, text: str):
    if not (META_TOKEN and WABA_PHONE_ID):
        log.error("WhatsApp no configurado")
        return
    data = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text}
    }
    try:
        r = requests.post(WA_BASE, headers=HEADERS_WA, json=data, timeout=15)
        if r.status_code >= 300:
            log.error(f"WA send_message error {r.status_code}: {r.text}")
    except Exception:
        log.exception("send_message exception")


def ensure_ctx(phone: str) -> Dict[str, Any]:
    if phone not in user_ctx:
        user_ctx[phone] = {}
    return user_ctx[phone]


def _normalize_last10(phone: str) -> str:
    # toma los últimos 10 dígitos (estándar para matching)
    digits = re.sub(r"\D", "", phone)
    return digits[-10:]


def sheet_match_by_last10(last10: str) -> Optional[Dict[str, Any]]:
    """Busca en la hoja prospectos por los últimos 10 dígitos de WhatsApp.
    Retorna dict con al menos {'nombre': str} si hay match, o None.
    """
    if not (google_ready and sheets and GOOGLE_SHEET_ID and GOOGLE_SHEET_NAME):
        return None
    try:
        rng = f"{GOOGLE_SHEET_NAME}!A:Z"
        resp = sheets.spreadsheets().values().get(spreadsheetId=GOOGLE_SHEET_ID, range=rng).execute()
        rows = resp.get("values", [])
        # heurística: buscar un campo que parezca teléfono
        best = None
        for row in rows:
            line = " ".join(row)
            if last10 and last10 in re.sub(r"\D", "", line):
                best = row
                break
        if best:
            # intenta nombre (primera columna no vacía)
            nombre = next((c for c in best if c.strip()), "")
            return {"nombre": nombre}
        return None
    except Exception:
        log.exception("sheet_match_by_last10 error")
        return None

# =============================================================
# LIMPIEZA DE ESTADOS
# =============================================================

def cleanup_old_states():
    current_time = datetime.utcnow()
    max_age = timedelta(days=2)

    old_phones = [k for k, v in greeted_at.items() if current_time - v >= max_age]
    for phone in old_phones:
        greeted_at.pop(phone, None)
        user_state.pop(phone, None)
        user_ctx.pop(phone, None)

    old_ids = [mid for mid, ts in processed_msg_ids.items() if current_time - ts >= timedelta(minutes=10)]
    for mid in old_ids:
        processed_msg_ids.pop(mid, None)

    if old_phones or old_ids:
        log.info(f"[Cleanup] Estados limpiados: usuarios={len(old_phones)} ids={len(old_ids)}")


def start_cleanup_scheduler():
    def _loop():
        while True:
            time.sleep(3600)
            try:
                cleanup_old_states()
            except Exception:
                log.exception("Error en cleanup_old_states")
    threading.Thread(target=_loop, daemon=True).start()

start_cleanup_scheduler()

# =============================================================
# RAG — Manuales de Auto en Drive
# =============================================================
_manual_auto_cache: Dict[str, Any] = {"text": None, "file_id": None, "loaded_at": None, "file_name": None}
_manual_folder_id_cache: Optional[str] = None


def _resolve_manuals_folder_id() -> Optional[str]:
    global _manual_folder_id_cache
    if _manual_folder_id_cache:
        return _manual_folder_id_cache
    if MANUALES_VICKY_FOLDER_ID:
        _manual_folder_id_cache = MANUALES_VICKY_FOLDER_ID
        return _manual_folder_id_cache
    if not (google_ready and service_drive):
        return None
    try:
        name = MANUALES_VICKY_FOLDER_NAME or "Manuales Vicky"
        q = "mimeType='application/vnd.google-apps.folder' and name='%s' and trashed=false" % name
        resp = service_drive.files().list(q=q, fields="files(id,name)", pageSize=5, orderBy="modifiedTime desc").execute()
        files = resp.get("files", [])
        if files:
            _manual_folder_id_cache = files[0]["id"]
            log.info(f"[RAG] Carpeta de manuales resuelta por nombre '{name}': {_manual_folder_id_cache}")
            return _manual_folder_id_cache
        log.error(f"[RAG] No se encontró carpeta de manuales por nombre: '{name}'")
        return None
    except Exception:
        log.exception("[RAG] Error resolviendo carpeta de manuales")
        return None


def _find_best_auto_manual() -> Optional[Dict[str, str]]:
    folder_id = _resolve_manuals_folder_id()
    if not (google_ready and service_drive and folder_id):
        log.error("Google Drive no configurado para RAG")
        return None
    try:
        q = (
            f"'{folder_id}' in parents and "
            "mimeType='application/pdf' and trashed=false"
        )
        resp = service_drive.files().list(q=q, fields="files(id, name, modifiedTime)", orderBy="modifiedTime desc", pageSize=20).execute()
        files = resp.get("files", [])
        if not files:
            log.warning("No se encontraron PDFs en la carpeta de manuales")
            return None

        auto_files: List[Dict[str, str]] = []
        other_files: List[Dict[str, str]] = []
        for f in files:
            name = (f.get("name") or "").lower()
            if any(k in name for k in ["auto", "vehículo", "vehicular", "cobertura", "automóvil", "automovil"]):
                auto_files.append(f)
            else:
                other_files.append(f)

        if auto_files:
            selected = auto_files[0]
            for f in auto_files:
                nm = (f.get("name") or "").lower()
                if "cobertura" in nm and "auto" in nm:
                    selected = f
                    break
        elif other_files:
            selected = other_files[0]
            log.info(f"[RAG] Usando manual genérico: {selected.get('name')}")
        else:
            return None

        return {"id": selected["id"], "name": selected.get("name", "desconocido"), "modified": selected.get("modifiedTime")}
    except Exception:
        log.exception("[RAG] Error buscando manuales")
        return None


def _download_pdf_text_improved(file_id: str) -> Optional[str]:
    try:
        from googleapiclient.http import MediaIoBaseDownload
        req = service_drive.files().get_media(fileId=file_id)
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, req)
        done = False
        while not done:
            status, done = downloader.next_chunk()
        fh.seek(0)
        reader = PdfReader(fh)
        parts: List[str] = []
        for i, page in enumerate(reader.pages):
            try:
                txt = page.extract_text() or ""
                if txt.strip():
                    cleaned = re.sub(r"\s+", " ", txt).strip()
                    if len(cleaned) > 50:
                        parts.append(f"Página {i+1}: {cleaned}")
            except Exception:
                log.warning(f"[RAG] Error extrayendo página {i+1}")
                continue
        full = "\n\n".join(parts)
        log.info(f"[RAG] Texto extraído: {len(full)} chars, {len(parts)} páginas con contenido")
        return full if full.strip() else None
    except Exception:
        log.exception("[RAG] Error descargando PDF")
        return None


def ensure_auto_manual_text(force_reload: bool = False) -> Optional[str]:
    cache = _manual_auto_cache
    now = datetime.utcnow()
    max_age = timedelta(hours=12)
    if (not force_reload and cache.get("text") and cache.get("loaded_at") and (now - cache["loaded_at"]) < max_age):
        return cache["text"]

    log.info("[RAG] Cargando manual desde Drive...")
    info = _find_best_auto_manual()
    if not info:
        log.error("[RAG] No se pudo encontrar ningún manual")
        return None

    text = _download_pdf_text_improved(info["id"])
    if text:
        cache.update({"text": text, "file_id": info["id"], "file_name": info["name"], "loaded_at": now})
        log.info(f"[RAG] Manual cargado: {info['name']} ({len(text)} chars)")
        return text
    log.error(f"[RAG] Falló la extracción de texto: {info['name']}")
    return None


def answer_auto_from_manual(question: str) -> Optional[str]:
    if not client_oa:
        return None
    manual_text = ensure_auto_manual_text()
    if not manual_text:
        return None

    ql = (question or "").lower()
    base_keys = [
        "amplia plus", "amplia", "cobertura", "asistencia", "cristales",
        "auto de reemplazo", "deducible", "responsabilidad", "robo", "daños",
        "gastos médicos", "muerte", "invalidez", "terceros", "vs", "diferencia", "comparación", "incluye"
    ]
    dyn = [k for k in base_keys if k in ql]
    keys = dyn or base_keys

    sections = manual_text.split("\n\n")
    relevant: List[Tuple[str, int]] = []
    for s in sections:
        if not s.strip() or len(s.strip()) < 20:
            continue
        score = 0
        sl = s.lower()
        for k in keys:
            if k in sl:
                score += 2
        for bonus in ["amplia plus", "comparación", "vs", "diferencia"]:
            if bonus in sl:
                score += 3
        if score > 0:
            relevant.append((s, score))

    if relevant:
        relevant.sort(key=lambda x: x[1], reverse=True)
        selected = "\n\n".join([s for s, _ in relevant[:6]])
    else:
        fb = [s for s in sections if any(t in s.lower() for t in ["auto", "vehículo", "seguro", "cobertura", "póliza"])]
        selected = "\n\n".join(fb[:8]) if fb else manual_text[:6000]

    if len(selected) > 6000:
        selected = selected[:6000] + "\n\n[... texto truncado ...]"

    prompt = (
        "Eres Vicky, una especialista en seguros de auto de Inbursa. "
        "Responde ÚNICAMENTE con base en la información del manual técnico proporcionado. "
        "SÉ PRECISA y no inventes información.\n\n"
        "REGLAS ESTRICTAS:\n"
        "1. Si la información NO está en el manual, di: 'No encontré esta información específica en el manual oficial'\n"
        "2. Usa viñetas (•) para listar coberturas y características\n"
        "3. Sé específica en comparaciones: menciona QUÉ incluye una cobertura vs otra\n"
        "4. Si el manual tiene tablas comparativas, descríbelas claramente\n"
        "5. Mantén la respuesta entre 100-500 palabras\n\n"
        f"PREGUNTA DEL CLIENTE: {question}\n\n"
        "INFORMACIÓN DEL MANUAL TÉCNICO:\n"
        "═══════════════════════════════════════════════════════════════\n"
        f"{selected}\n"
        "═══════════════════════════════════════════════════════════════\n\n"
        "RESPUESTA BASADA EN EL MANUAL:"
    )
    try:
        res = client_oa.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=800,
        )
        ans = (res.choices[0].message.content or "").strip()
        if ans and not ans.startswith("No encontré") and len(ans) > 30:
            return ans
        return None
    except Exception:
        log.exception("[RAG] Error en consulta OpenAI")
        return None

# --- DETECCIÓN DE PREGUNTAS DE COBERTURAS (RAG) ---
_COVERAGE_KEYS = [
    "amplia plus", "amplia", "cobertura", "coberturas", "cristales",
    "asistencia", "auto de reemplazo", "deducible", "qué incluye",
    "que incluye", "qué cubre", "que cubre", "diferencia", "vs", "comparar",
    "comparación", "comparacion"
]

def should_trigger_rag(text: str) -> bool:
    t = (text or "").lower()
    return any(k in t for k in _COVERAGE_KEYS)

# =============================================================
# RUTEO COMANDOS (solo lo necesario para parche)
# =============================================================
MENU_TXT = (
    "\U0001F4D8 Vicky Bot — Inbursa\n"
    "Elige una opción:\n"
    "1) Asesoría en pensiones IMSS\n"
    "2) Cotizador de seguro de auto\n"
    "3) Seguros de vida y salud\n"
    "4) Membresía médica VRIM\n"
    "5) Préstamos a pensionados IMSS ($10,000 a $650,000)\n"
    "6) Financiamiento empresarial\n"
    "7) Contactar con Christian\n\n"
    "Escribe el número u opción (ej. 'imss', 'auto', 'empresarial', 'contactar')."
)

CHECKLIST_AUTO = (
    "\U0001F697 Cotizador Auto\n\n"
    "Envíame:\n"
    "• INE (frente)\n"
    "• Tarjeta de circulación o número de placas.\n\n"
    "Si ya tienes póliza, dime la fecha de vencimiento (AAAAA-MM-DD) para recordarte 30 días antes."
)


def route_command(phone: str, text: str, match: Optional[Dict[str, Any]]):
    t = (text or "").strip().lower()

    # --- Global: preguntas de coberturas → RAG primero ---
    if should_trigger_rag(t):
        rag_ans = answer_auto_from_manual(text)
        if rag_ans:
            send_message(phone, rag_ans)
            return

    # --- Selección de menú ---
    if t in {"menu", "inicio", "hola", "hi", "start", "ayuda"}:
        send_message(phone, MENU_TXT)
        return

    if t in {"1", "imss", "pensión", "pension", "asesoría", "asesoria"}:
        send_message(phone, "Para pensiones IMSS, indícame tu situación actual y te ayudo.")
        user_state[phone] = "imss"
        return

    if t in {"2", "auto", "cotizador", "seguro auto"}:
        # Dentro de auto, si el cliente pregunta por coberturas, ya capturó arriba con RAG.
        send_message(phone, CHECKLIST_AUTO)
        user_state[phone] = "auto"
        return

    if t in {"3", "vida", "salud"}:
        send_message(phone, "Compárteme edad, suma asegurada deseada y te coto.")
        user_state[phone] = "vida"
        return

    if t in {"4", "vrim"}:
        send_message(phone, "VRIM: te explico beneficios y costos. ¿Te interesa individual o familiar?")
        user_state[phone] = "vrim"
        return

    if t in {"5", "préstamo", "prestamo", "pensionados"}:
        send_message(phone, "Perfecto. ¿Eres pensionado IMSS Ley 73? Indícame tu monto aproximado de pensión.")
        user_state[phone] = "prestamo"
        return

    if t in {"6", "empresarial", "financiamiento"}:
        send_message(phone, "¿Qué monto, giro y uso del crédito requieres? Te contacto para la propuesta.")
        user_state[phone] = "empresarial"
        return

    if t in {"7", "contactar", "christian"}:
        send_message(phone, "Gracias. Notificaré al asesor para que te contacte a la brevedad.")
        user_state[phone] = "contacto"
        # aquí notificarías a ADVISOR_NUMBER con datos básicos
        return

    # --- Estados en curso ---
    st = user_state.get(phone, "")
    if st == "auto":
        # Antes de mandar checklist, si insiste con coberturas: RAG
        if should_trigger_rag(t):
            rag_ans = answer_auto_from_manual(text)
            if rag_ans:
                send_message(phone, rag_ans)
                return
        # si no, seguir flujo de documentos
        send_message(phone, CHECKLIST_AUTO)
        return

    # Fallback
    send_message(phone, "Te ayudo con esto. Si quieres ver el menú, escribe 'menu'.")

# =============================================================
# WEBHOOKS
# =============================================================
@app.get("/")
def root():
    return "OK", 200


@app.get("/webhook")
def webhook_verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    return "forbidden", 403


@app.post("/webhook")
def webhook_receive():
    try:
        payload = request.get_json(force=True, silent=True) or {}
        entry = payload.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        value = changes.get("value", {})
        messages = value.get("messages", [])
        if not messages:
            return jsonify({"ok": True}), 200

        msg = messages[0]
        phone = (msg.get("from") or "").strip()
        if not phone:
            return jsonify({"ok": True}), 200

        # Dedupe reintentos
        mid = msg.get("id") or f"{phone}-{msg.get('timestamp','')}"
        now = datetime.utcnow()
        if mid in processed_msg_ids and (now - processed_msg_ids[mid]) < timedelta(seconds=8):
            log.info(f"[Webhook] Duplicado ignorado: {mid}")
            return jsonify({"ok": True}), 200
        processed_msg_ids[mid] = now

        log.info(f"[Webhook] Mensaje de {phone}: {msg.get('type','unknown')}")

        # Contexto + saludo controlado
        ctx = ensure_ctx(phone)
        current_time = datetime.utcnow()

        last_greeting = greeted_at.get(phone)
        should_greet = (last_greeting is None) or ((current_time - last_greeting) >= timedelta(hours=GREET_WINDOW_HOURS))

        if "match" not in ctx or ctx.get("match") is None:
            match = sheet_match_by_last10(_normalize_last10(phone))
            ctx["match"] = match
        else:
            match = ctx["match"]

        if should_greet:
            if match and match.get("nombre"):
                send_message(phone, f"Hola {match['nombre']} 👋 Soy *Vicky*. ¿En qué te puedo ayudar hoy?")
            else:
                send_message(phone, "Hola 👋 Soy *Vicky*. Estoy para ayudarte.")
            greeted_at[phone] = current_time

        mtype = msg.get("type")
        if mtype == "text" and "text" in msg:
            text = (msg["text"].get("body") or "").strip()

            # Comando directo GPT (debug)
            if text.lower().startswith("sgpt:") and client_oa:
                prompt = text.split("sgpt:", 1)[1].strip()
                def _gpt_direct():
                    try:
                        res = client_oa.chat.completions.create(
                            model=OPENAI_MODEL,
                            messages=[{"role": "user", "content": prompt}],
                            temperature=0.3,
                        )
                        ans = (res.choices[0].message.content or "").strip()
                        send_message(phone, ans or "Listo.")
                    except Exception:
                        log.exception("sgpt error")
                        send_message(phone, "Hubo un detalle al procesar tu solicitud.")
                threading.Thread(target=_gpt_direct, daemon=True).start()
                return jsonify({"ok": True}), 200

            route_command(phone, text, match)
            return jsonify({"ok": True}), 200

        if mtype in {"image", "audio", "video", "document"}:
            send_message(phone, "📎 *Recibido*. Gracias, lo reviso y te confirmo en breve.")
            return jsonify({"ok": True}), 200

        return jsonify({"ok": True}), 200
    except Exception:
        log.exception("Error en webhook_receive")
        return jsonify({"ok": True}), 200

# =============================================================
# ENDPOINTS AUXILIARES
# =============================================================
@app.get("/ext/health")
def ext_health():
    manual_status = "loaded" if _manual_auto_cache.get("text") else "empty"
    return jsonify({
        "status": "ok",
        "timestamp": datetime.utcnow().isoformat(),
        "whatsapp_configured": bool(META_TOKEN and WABA_PHONE_ID),
        "google_ready": google_ready,
        "openai_ready": bool(client_oa is not None),
        "rag_status": manual_status,
        "rag_file": _manual_auto_cache.get("file_name"),
        "manuales_folder_id": (_manual_folder_id_cache or MANUALES_VICKY_FOLDER_ID),
        "manuales_folder_name": MANUALES_VICKY_FOLDER_NAME,
        "sheet_name": GOOGLE_SHEET_NAME,
        "manuales_folder": bool((_manual_folder_id_cache or MANUALES_VICKY_FOLDER_ID)),
    }), 200


@app.get("/ext/test-send")
def ext_test_send():
    to = request.args.get("to", ADVISOR_NUMBER)
    txt = request.args.get("text", "Prueba OK — Vicky Bot")
    send_message(to, txt)
    return jsonify({"ok": True}), 200


@app.get("/ext/manuales")
def ext_manuales():
    folder_id = _resolve_manuals_folder_id()
    if not (google_ready and service_drive and folder_id):
        return jsonify({"ok": False, "error": "drive_not_ready"}), 400
    q = f"'{folder_id}' in parents and mimeType='application/pdf' and trashed=false"
    resp = service_drive.files().list(q=q, fields="files(id,name,modifiedTime)", orderBy="modifiedTime desc", pageSize=50).execute()
    return jsonify({"ok": True, "files": resp.get("files", [])}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))







