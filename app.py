# app.py — Vicky SECOM (Versión Corregida - Campañas + Estado normalizado)
from __future__ import annotations
import os, re, json, time, logging, threading
from datetime import datetime
from typing import Any, Dict, Optional, List

import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv

try:
    from google.oauth2.service_account import Credentials as ServiceAccountCredentials
    from googleapiclient.discovery import build as gbuild
except Exception:
    ServiceAccountCredentials = None
    gbuild = None

try:
    import openai
except Exception:
    openai = None

# ==========================
# Carga entorno
# ==========================
load_dotenv()

def _get(name: str, default: str = "") -> str:
    return (os.getenv(name, default) or "").strip()

META_TOKEN = _get("META_TOKEN") or _get("WHATSAPP_TOKEN")
WABA_PHONE_ID = _get("WABA_PHONE_ID") or _get("PHONE_NUMBER_ID")
VERIFY_TOKEN = _get("VERIFY_TOKEN")
ADVISOR_NUMBER = _get("ADVISOR_NUMBER") or _get("ADVISOR_WHATSAPP")
PORT = int(_get("PORT", "5000"))

SHEETS_ID_LEADS = _get("SHEETS_ID_LEADS") or _get("SHEET_ID_SECOM")
SHEETS_TITLE_LEADS = _get("SHEETS_TITLE_LEADS") or _get("SHEET_TITLE_SECOM", "Prospectos SECOM Auto")
GOOGLE_CREDENTIALS_JSON = _get("GOOGLE_CREDENTIALS_JSON")

OPENAI_API_KEY = _get("OPENAI_API_KEY")
if openai and OPENAI_API_KEY:
    openai.api_key = OPENAI_API_KEY

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("vicky-secom")

app = Flask(__name__)

# ==========================
# Estado (normalizado por últimos 10 dígitos)
# ==========================
_user_state: Dict[str, str] = {}
_user_data: Dict[str, Dict[str, Any]] = {}

def _normalize_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    return digits[-10:] if len(digits) >= 10 else digits or phone

def get_state(phone: str) -> str:
    return _user_state.get(_normalize_phone(phone), "")

def set_state(phone: str, value: str) -> None:
    key = _normalize_phone(phone)
    if value:
        _user_state[key] = value
    elif key in _user_state:
        del _user_state[key]

def get_data(phone: str) -> Dict[str, Any]:
    key = _normalize_phone(phone)
    if key not in _user_data:
        _user_data[key] = {}
    return _user_data[key]

# ==========================
# Utilidades
# ==========================
WPP_API_URL = (
    f"https://graph.facebook.com/v20.0/{WABA_PHONE_ID}/messages"
    if WABA_PHONE_ID
    else None
)

def interpret_response(text: str) -> str:
    if not text:
        return "neutral"
    t = text.lower()
    pos = ["sí", "si", "claro", "ok", "de acuerdo", "vale", "afirmativo", "correcto"]
    neg = ["no", "nel", "nop", "negativo", "no quiero", "no gracias", "no interesa"]
    if any(p in t for p in pos):
        return "positive"
    if any(n in t for n in neg):
        return "negative"
    return "neutral"

def extract_number(text: str) -> Optional[float]:
    if not text:
        return None
    clean = text.replace(",", "").replace("$", "")
    m = re.search(r"(\d+(\.\d+)?)", clean)
    try:
        return float(m.group(1)) if m else None
    except Exception:
        return None

def _headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {META_TOKEN}",
        "Content-Type": "application/json",
    }

def _should_retry(status: int) -> bool:
    return status == 429 or 500 <= status < 600

def _backoff(attempt: int) -> None:
    time.sleep(2**attempt)

def send_message(to: str, text: str) -> bool:
    if not (META_TOKEN and WPP_API_URL):
        log.error("❌ WhatsApp API no configurada")
        return False

    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text[:4096]},
    }

    for attempt in range(3):
        try:
            r = requests.post(
                WPP_API_URL, headers=_headers(), json=payload, timeout=15
            )
            if r.status_code == 200:
                log.info(f"📤 Mensaje enviado a {to}: {text[:120]!r}")
                return True
            log.warning(
                f"⚠️ Error send_message {r.status_code} {r.text[:300]!r}"
            )
            if _should_retry(r.status_code) and attempt < 2:
                _backoff(attempt)
                continue
            return False
        except Exception:
            log.exception("❌ Error en send_message")
            if attempt < 2:
                _backoff(attempt)
                continue
            return False
    return False

def send_template_message(
    to: str, template_name: str, components: List[Dict[str, Any]]
) -> bool:
    if not (META_TOKEN and WPP_API_URL):
        log.error("❌ WhatsApp API no configurada para plantillas")
        return False

    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": "es_MX"},
            "components": components,
        },
    }

    for attempt in range(3):
        try:
            r = requests.post(
                WPP_API_URL, headers=_headers(), json=payload, timeout=15
            )
            if r.status_code == 200:
                log.info(f"📤 Plantilla '{template_name}' enviada a {to}")
                return True
            log.warning(
                f"⚠️ Error plantilla {template_name} {r.status_code} {r.text[:300]!r}"
            )
            if _should_retry(r.status_code) and attempt < 2:
                _backoff(attempt)
                continue
            return False
        except Exception:
            log.exception("❌ Error en send_template_message")
            if attempt < 2:
                _backoff(attempt)
                continue
            return False
    return False

# ==========================
# Google Sheets (matching SECOM)
# ==========================
sheets = None
google_ready = False

if GOOGLE_CREDENTIALS_JSON and ServiceAccountCredentials and gbuild and SHEETS_ID_LEADS:
    try:
        info = json.loads(GOOGLE_CREDENTIALS_JSON)
        creds = ServiceAccountCredentials.from_service_account_info(
            info,
            scopes=[
                "https://www.googleapis.com/auth/spreadsheets.readonly",
            ],
        )
        sheets = gbuild("sheets", "v4", credentials=creds)
        google_ready = True
        log.info("✅ Google Sheets configurado")
    except Exception:
        log.exception("❌ Error configurando Google Sheets")

def match_client_in_sheets(phone: str) -> Optional[Dict[str, Any]]:
    if not (google_ready and sheets):
        return None
    try:
        rng = f"{SHEETS_TITLE_LEADS}!A:Z"
        res = sheets.spreadsheets().values().get(
            spreadsheetId=SHEETS_ID_LEADS, range=rng
        ).execute()
        rows = res.get("values", [])
        target = _normalize_phone(phone)
        for row in rows:
            if len(row) < 3:
                continue
            tel = _normalize_phone(row[2])
            if tel == target:
                nombre = row[0] if row else ""
                return {"nombre": nombre}
        return None
    except Exception:
        log.exception("❌ Error en match_client_in_sheets")
        return None

# ==========================
# Menú y helpers
# ==========================
def send_main_menu(phone: str) -> None:
    menu = (
        "Vicky Bot — Inbursa\n"
        "Elige una opción:\n"
        "1) Préstamo IMSS (Ley 73)\n"
        "2) Seguro de Auto (cotización)\n"
        "3) Seguros de Vida / Salud\n"
        "4) Tarjeta médica VRIM\n"
        "5) Crédito Empresarial\n"
        "6) Financiamiento Práctico\n"
        "7) Contactar con Christian\n\n"
        "Escribe el número u opción (ej. 'imss', 'auto', 'empresarial', 'contactar')."
    )
    send_message(phone, menu)

def notify_advisor(msg: str) -> None:
    if ADVISOR_NUMBER:
        send_message(ADVISOR_NUMBER, msg)

# ==========================
# Embudos
# ==========================
# IMSS
def imss_start(phone: str, match: Optional[Dict[str, Any]]) -> None:
    set_state(phone, "imss_beneficios")
    send_message(
        phone,
        "🟩 *Préstamo IMSS Ley 73*\n"
        "Te ayudo a revisar si calificas para un préstamo con tasa preferencial. "
        "¿Te interesa conocer requisitos? (responde *sí* o *no*).",
    )

def imss_next(phone: str, text: str) -> None:
    st = get_state(phone)
    data = get_data(phone)

    if st == "imss_beneficios":
        if interpret_response(text) == "positive":
            set_state(phone, "imss_pension")
            send_message(phone, "¿Cuál es tu *pensión mensual* aproximada?")
        else:
            send_message(
                phone,
                "Sin problema. Si deseas continuar después, escribe *1* o *imss*.",
            )
            set_state(phone, "")
    elif st == "imss_pension":
        monto = extract_number(text)
        if not monto:
            send_message(phone, "Indícame un monto aproximado válido, por favor.")
            return
        data["imss_pension"] = monto
        set_state(phone, "imss_nombre")
        send_message(phone, "¿Cuál es tu *nombre completo*?")
    elif st == "imss_nombre":
        data["imss_nombre"] = text.strip()
        set_state(phone, "")
        send_message(
            phone,
            "✅ Gracias. Un asesor validará tu información y te contactará.",
        )
        notify_advisor(
            f"🔔 Lead IMSS\nWhatsApp: {phone}\nNombre: {data.get('imss_nombre','')}\nPensión: {data.get('imss_pension','')}"
        )

# Empresarial
def emp_start(phone: str, match: Optional[Dict[str, Any]]) -> None:
    set_state(phone, "emp_confirma")
    send_message(
        phone,
        "🏢 *Crédito Empresarial*\n"
        "¿Eres empresario(a) o representante de una empresa? (responde *sí* o *no*).",
    )

def emp_next(phone: str, text: str) -> None:
    st = get_state(phone)
    data = get_data(phone)

    if st == "emp_confirma":
        if interpret_response(text) != "positive":
            send_message(
                phone,
                "Entendido. Si cambias de opinión, escribe *5* o *empresarial*.",
            )
            set_state(phone, "")
            return
        set_state(phone, "emp_giro")
        send_message(phone, "¿A qué *se dedica* tu empresa?")
    elif st == "emp_giro":
        data["emp_giro"] = text.strip()
        set_state(phone, "emp_monto")
        send_message(phone, "¿Qué *monto* necesitas? (mínimo $100,000)")
    elif st == "emp_monto":
        monto = extract_number(text)
        if not monto or monto < 100000:
            send_message(
                phone,
                "El monto mínimo es $100,000. Indícame un monto igual o mayor.",
            )
            return
        data["emp_monto"] = monto
        set_state(phone, "emp_nombre")
        send_message(phone, "¿Tu *nombre completo*?")
    elif st == "emp_nombre":
        data["emp_nombre"] = text.strip()
        set_state(phone, "emp_ciudad")
        send_message(phone, "¿En qué *ciudad* está tu empresa?")
    elif st == "emp_ciudad":
        data["emp_ciudad"] = text.strip()
        set_state(phone, "")
        resumen = (
            "✅ Gracias. Un asesor te contactará.\n"
            f"- Nombre: {data.get('emp_nombre','')}\n"
            f"- Ciudad: {data.get('emp_ciudad','')}\n"
            f"- Giro: {data.get('emp_giro','')}\n"
            f"- Monto: ${data.get('emp_monto',0):,.0f}"
        )
        send_message(phone, resumen)
        notify_advisor(
            f"🔔 Lead Empresarial\nWhatsApp: {phone}\n{resumen}"
        )

# Financiamiento Práctico
def fp_start(phone: str, match: Optional[Dict[str, Any]]) -> None:
    set_state(phone, "fp_monto")
    send_message(
        phone, "💳 *Financiamiento Práctico*\n¿Qué monto necesitas?"
    )

def fp_next(phone: str, text: str) -> None:
    st = get_state(phone)
    data = get_data(phone)

    if st == "fp_monto":
        monto = extract_number(text)
        if not monto:
            send_message(phone, "Indícame un monto válido, por favor.")
            return
        data["fp_monto"] = monto
        set_state(phone, "")
        send_message(
            phone,
            "✅ Gracias. Un asesor revisará tu solicitud.",
        )
        notify_advisor(
            f"🔔 Lead Financiamiento Práctico\nWhatsApp: {phone}\nMonto: ${monto:,.0f}"
        )

# Auto
def auto_start(phone: str, match: Optional[Dict[str, Any]]) -> None:
    set_state(phone, "auto_intro")
    send_message(
        phone,
        "🚗 *Seguro de Auto*\n"
        "Envíame por favor:\n"
        "• Foto de tu INE\n"
        "• Tarjeta de circulación o placa\n"
        "• Si tienes póliza actual, foto donde se vea la fecha de vencimiento.\n"
        "Cuando lo envíes, te confirmaré recepción y procesaré la cotización.",
    )

def auto_next(phone: str, text: str) -> None:
    st = get_state(phone)
    intent = interpret_response(text)

    if st == "auto_intro":
        if (
            "vencimiento" in text.lower()
            or "vence" in text.lower()
            or "fecha" in text.lower()
        ):
            set_state(phone, "auto_vencimiento_fecha")
            send_message(
                phone,
                "¿Cuál es la *fecha de vencimiento* de tu póliza actual? (AAAA-MM-DD)",
            )
        elif intent == "negative":
            set_state(phone, "auto_vencimiento_fecha")
            send_message(
                phone,
                "Entendido 👍 Para apoyarte cuando se acerque la fecha, dime "
                "¿cuándo vence tu póliza actual? (AAAA-MM-DD)",
            )
        else:
            send_message(
                phone,
                "Perfecto ✅ Puedes enviarme desde ahora las fotos de tus documentos para cotizar.",
            )
    elif st == "auto_vencimiento_fecha":
        set_state(phone, "")
        send_message(
            phone,
            "✅ Gracias. Tomo nota de la fecha para recordarte antes del vencimiento.",
        )
        notify_advisor(
            f"🔔 Cliente SECOM {phone} indicó fecha de vencimiento: {text}"
        )

# ==========================
# Router principal
# ==========================
def route_command(phone: str, text: str, match: Optional[Dict[str, Any]]) -> None:
    t = (text or "").strip().lower()

    # Comandos directos
    if t in ("1", "imss", "ley 73", "prestamo imss", "préstamo imss", "pension", "pensión"):
        imss_start(phone, match)
        return
    if t in ("2", "auto", "seguro auto", "seguro de auto"):
        auto_start(phone, match)
        return
    if t in ("3", "vida", "salud", "seguro de vida", "seguro de salud"):
        send_message(
            phone,
            "🧬 En breve un asesor te comparte opciones de Vida / Salud.",
        )
        notify_advisor(
            f"🔔 Vida/Salud — Solicitud de contacto\nWhatsApp: {phone}"
        )
        send_main_menu(phone)
        return
    if t in ("4", "vrim", "tarjeta medica", "tarjeta médica"):
        send_message(
            phone,
            "🩺 En breve un asesor te comparte información de la tarjeta médica VRIM.",
        )
        notify_advisor(
            f"🔔 VRIM — Solicitud de contacto\nWhatsApp: {phone}"
        )
        send_main_menu(phone)
        return
    if t in (
        "5",
        "empresarial",
        "credito empresarial",
        "crédito empresarial",
        "pyme",
    ):
        emp_start(phone, match)
        return
    if t in (
        "6",
        "financiamiento practico",
        "financiamiento práctico",
        "credito simple",
        "crédito simple",
    ):
        fp_start(phone, match)
        return
    if t in ("7", "contactar", "asesor", "contactar con christian"):
        notify_advisor(
            f"🔔 Contacto directo solicitado\nWhatsApp: {phone}"
        )
        send_message(
            phone,
            "✅ Listo. Avisé a Christian para que te contacte personalmente.",
        )
        send_main_menu(phone)
        return
    if t in ("menu", "menú", "inicio", "hola"):
        set_state(phone, "")
        send_main_menu(phone)
        return

    # No es comando directo → revisar estado
    st = get_state(phone)
    intent = interpret_response(text)

    # Campaña SECOM Auto
    if st == "campaign_secom_auto":
        if intent == "positive":
            send_message(
                phone,
                "Perfecto ✅ Iniciemos con la revisión gratuita de tu seguro de auto.",
            )
            set_state(phone, "")
            auto_start(phone, match)
        elif intent == "negative":
            send_message(
                phone,
                "Gracias por responder 🙌. Si más adelante deseas una revisión, escribe *2* o *auto*.",
            )
            set_state(phone, "")
            send_main_menu(phone)
        else:
            send_message(
                phone,
                "Solo para confirmar, ¿te interesa la revisión gratuita de tu seguro de auto? "
                "Responde *sí* o *no*, o escribe *menú*.",
            )
        return

    # Campaña IMSS Ley 73
    if st == "campaign_imss_ley73":
        if intent == "positive":
            send_message(
                phone,
                "Perfecto ✅ Revisemos tu opción de *Préstamo IMSS Ley 73*.",
            )
            set_state(phone, "")
            imss_start(phone, match)
        elif intent == "negative":
            send_message(
                phone,
                "Entendido 🙌. Si luego te interesa, escribe *1* o *imss*.",
            )
            set_state(phone, "")
            send_main_menu(phone)
        else:
            send_message(
                phone,
                "¿Te interesa que revisemos si calificas para un préstamo IMSS Ley 73? "
                "Responde *sí* o *no*, o escribe *menú*.",
            )
        return

    # Flujos activos
    if st.startswith("imss_"):
        imss_next(phone, text)
    elif st.startswith("emp_"):
        emp_next(phone, text)
    elif st.startswith("fp_"):
        fp_next(phone, text)
    elif st.startswith("auto_"):
        auto_next(phone, text)
    else:
        # Sin estado y sin comando válido
        send_message(phone, "No entendí. Escribe *menú* para ver opciones.")

# ==========================
# Webhook
# ==========================
@app.get("/webhook")
def webhook_verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge", "")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        log.info("✅ Webhook verificado correctamente")
        return challenge, 200
    log.warning("❌ Webhook verification failed")
    return "forbidden", 403

@app.post("/webhook")
def webhook_receive():
    try:
        payload = request.get_json(force=True, silent=True) or {}
        log.info(f"📥 Webhook recibido: {json.dumps(payload)[:500]}...")
        entry = (payload.get("entry") or [{}])[0]
        changes = (entry.get("changes") or [{}])[0]
        value = changes.get("value", {})
        messages = value.get("messages", [])

        # Si no hay messages (solo statuses), salimos
        if not messages:
            return jsonify({"ok": True}), 200

        msg = messages[0]
        phone = msg.get("from")
        if not phone:
            return jsonify({"ok": True}), 200

        match = match_client_in_sheets(phone)
        mtype = msg.get("type")

        if mtype == "text":
            text = msg.get("text", {}).get("body", "")
            log.info(f"💬 Texto de {phone}: {text!r}")

            # GPT directo opcional
            if text.lower().startswith("sgpt:") and openai and OPENAI_API_KEY:
                prompt = text.split("sgpt:", 1)[1].strip()
                try:
                    completion = openai.chat.completions.create(
                        model="gpt-4o-mini",
                        messages=[{"role": "user", "content": prompt}],
                        temperature=0.4,
                    )
                    answer = completion.choices[0].message.content.strip()
                    send_message(phone, answer)
                    return jsonify({"ok": True}), 200
                except Exception:
                    log.exception("❌ Error OpenAI")
                    send_message(
                        phone,
                        "Hubo un detalle al procesar tu mensaje, intenta de nuevo.",
                    )
                    return jsonify({"ok": True}), 200

            route_command(phone, text, match)
            return jsonify({"ok": True}), 200

        if mtype in ("image", "document", "audio", "video"):
            log.info(f"📎 Multimedia recibida de {phone}: {mtype}")
            send_message(
                phone,
                "✅ Archivo recibido. Lo revisaré junto con tu solicitud.",
            )
            return jsonify({"ok": True}), 200

        log.info(f"ℹ️ Tipo de mensaje no manejado: {mtype}")
        return jsonify({"ok": True}), 200

    except Exception:
        log.exception("❌ Error en webhook_receive")
        return jsonify({"ok": True}), 200

# ==========================
# Endpoints externos
# ==========================
@app.get("/health")
def health():
    return jsonify(
        {
            "status": "ok",
            "service": "Vicky Bot SECOM",
            "timestamp": datetime.utcnow().isoformat(),
        }
    )

@app.get("/ext/health")
def ext_health():
    return jsonify(
        {
            "status": "ok",
            "whatsapp_configured": bool(META_TOKEN and WABA_PHONE_ID),
            "google_ready": google_ready,
            "openai_ready": bool(openai and OPENAI_API_KEY),
        }
    )

@app.post("/ext/test-send")
def ext_test_send():
    try:
        data = request.get_json(force=True) or {}
        to = str(data.get("to", "")).strip()
        text = str(data.get("text", "Prueba desde Vicky SECOM")).strip()
        if not to:
            return jsonify({"ok": False, "error": "Falta 'to'"}), 400
        ok = send_message(to, text)
        return jsonify({"ok": bool(ok)}), 200
    except Exception as e:
        log.exception("❌ Error en /ext/test-send")
        return jsonify({"ok": False, "error": str(e)}), 500

# --- Worker envíos masivos ---
def _bulk_send_worker(items: List[Dict[str, Any]]) -> None:
    ok = 0
    fail = 0
    for i, item in enumerate(items, 1):
        try:
            to = str(item.get("to", "")).strip()
            text = str(item.get("text", "")).strip()
            template = str(item.get("template", "")).strip()
            components = item.get("components") or []

            if not to or (not text and not template):
                log.warning(f"⏭️ Item {i} inválido: {item}")
                fail += 1
                continue

            sent = False
            if template:
                sent = send_template_message(to, template, components)
            else:
                sent = send_message(to, text)

            if sent:
                ok += 1
                # Marcar campaña (estado por últimos 10)
                key = _normalize_phone(to)
                low = (text or "").lower()
                campaign = (item.get("campaign") or "").lower()
                if "cliente secom" in low and "seguro de auto" in low:
                    _user_state[key] = "campaign_secom_auto"
                elif "préstamo imss" in low or "prestamo imss" in low:
                    _user_state[key] = "campaign_imss_ley73"
                elif campaign:
                    _user_state[key] = f"campaign_{campaign}"
            else:
                fail += 1

            time.sleep(0.4)

        except Exception:
            fail += 1
            log.exception(f"❌ Error item {i} en _bulk_send_worker")

    log.info(f"🎯 Envío masivo terminado OK={ok} FAIL={fail}")
    if ADVISOR_NUMBER:
        send_message(
            ADVISOR_NUMBER,
            f"📊 Envío masivo finalizado.\nExitosos: {ok}\nFallidos: {fail}\nTotal: {len(items)}",
        )

@app.post("/ext/send-promo")
def ext_send_promo():
    try:
        if not (META_TOKEN and WABA_PHONE_ID):
            return jsonify(
                {"queued": False, "error": "WhatsApp API no configurada"}
            ), 500

        data = request.get_json(force=True) or {}
        items = data.get("items")

        if not isinstance(items, list) or not items:
            return jsonify(
                {
                    "queued": False,
                    "error": "Se requiere lista 'items' con mensajes",
                }
            ), 400

        t = threading.Thread(
            target=_bulk_send_worker, args=(items,), daemon=True
        )
        t.start()

        return jsonify(
            {
                "queued": True,
                "count": len(items),
                "timestamp": datetime.utcnow().isoformat(),
            }
        ), 202

    except Exception as e:
        log.exception("❌ Error en /ext/send-promo")
        return jsonify({"queued": False, "error": str(e)}), 500

# ==========================
# Arranque local
# ==========================
if __name__ == "__main__":
    log.info(f"🚀 Iniciando Vicky Bot SECOM en puerto {PORT}")
    log.info(
        f"📞 WhatsApp configurado: {bool(META_TOKEN and WABA_PHONE_ID)}"
    )
    log.info(f"📊 Google listo: {google_ready}")
    log.info(
        f"🧠 OpenAI listo: {bool(openai and OPENAI_API_KEY)}"
    )
    app.run(host="0.0.0.0", port=PORT, debug=False)
