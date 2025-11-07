# app.py — Vicky SECOM (Versión Corregida - Matching Mejorado + Campañas)
# Mantiene funcionalidades existentes y agrega seguimiento correcto
# a respuestas de campañas WAPI (SECOM Auto / IMSS Ley 73).

from __future__ import annotations

import os
import io
import re
import json
import time
import math
import queue
import logging
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, List, Tuple

import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# Google opcional
try:
    from google.oauth2.service_account import Credentials as service_account
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseUpload
except Exception:
    service_account = None
    build = None
    MediaIoBaseUpload = None

# GPT opcional
try:
    import openai
except Exception:
    openai = None

# ==========================
# Carga entorno + Logging
# ==========================
load_dotenv()

def _get(name: str, default: str = "") -> str:
    return (os.getenv(name, default) or "").strip()

META_TOKEN = _get("META_TOKEN") or _get("WHATSAPP_TOKEN")
WABA_PHONE_ID = _get("WABA_PHONE_ID") or _get("PHONE_NUMBER_ID") or _get("WA_PHONE_ID")
VERIFY_TOKEN = _get("VERIFY_TOKEN")
ADVISOR_NUMBER = _get("ADVISOR_NUMBER") or _get("ADVISOR_WHATSAPP") or ""
PORT = int(_get("PORT", "5000"))

SHEETS_ID_LEADS = _get("SHEETS_ID_LEADS") or _get("SHEET_ID_SECOM")
SHEETS_TITLE_LEADS = _get("SHEETS_TITLE_LEADS") or _get("SHEET_TITLE_SECOM", "Prospectos SECOM Auto")
GOOGLE_CREDENTIALS_JSON = _get("GOOGLE_CREDENTIALS_JSON")
DRIVE_PARENT_FOLDER_ID = _get("DRIVE_FOLDER_ID")  # opcional

OPENAI_API_KEY = _get("OPENAI_API_KEY")
if openai and OPENAI_API_KEY:
    try:
        openai.api_key = OPENAI_API_KEY
    except Exception:
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("vicky-secom")

app = Flask(__name__)

# Estados en memoria (FASE 1)
user_state: Dict[str, str] = {}
user_data: Dict[str, Dict[str, Any]] = {}

# ==========================
# Utilidades generales
# ==========================
WPP_API_URL = (
    f"https://graph.facebook.com/v20.0/{WABA_PHONE_ID}/messages"
    if WABA_PHONE_ID else None
)
WPP_TIMEOUT = 15

def _normalize_phone_last10(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    return digits[-10:] if len(digits) >= 10 else digits

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
    m = re.search(r"(\d{1,12}(\.\d+)?)", clean)
    try:
        return float(m.group(1)) if m else None
    except Exception:
        return None

def _ensure_user(phone: str) -> Dict[str, Any]:
    if phone not in user_data:
        user_data[phone] = {}
    return user_data[phone]

def _headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {META_TOKEN}",
        "Content-Type": "application/json",
    }

def _should_retry(status: int) -> bool:
    return status == 429 or 500 <= status < 600

def _backoff(attempt: int) -> None:
    time.sleep(2 ** attempt)

def send_message(to: str, text: str) -> bool:
    """Envía mensaje de texto al usuario vía WhatsApp Cloud API."""
    if not (META_TOKEN and WPP_API_URL):
        log.error("❌ WhatsApp no configurado (META_TOKEN/WABA_PHONE_ID faltan).")
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
                WPP_API_URL,
                headers=_headers(),
                json=payload,
                timeout=WPP_TIMEOUT,
            )
            if r.status_code == 200:
                log.info(f"📤 Mensaje enviado a {to}: {text[:120]!r}")
                return True
            log.warning(
                f"⚠️ Error send_message {r.status_code} {r.text[:300]!r} "
                f"(intent {attempt+1})"
            )
            if _should_retry(r.status_code) and attempt < 2:
                _backoff(attempt)
                continue
            return False
        except Exception as e:
            log.exception("❌ Error en send_message")
            if attempt < 2:
                _backoff(attempt)
                continue
            return False
    return False

def send_template_message(to: str, template_name: str, params: Dict | List) -> bool:
    """Envía plantilla preaprobada."""
    if not (META_TOKEN and WPP_API_URL):
        log.error("❌ WhatsApp no configurado para plantillas.")
        return False

    if isinstance(params, dict):
        components = params.get("components", [])
    else:
        # params como lista simple → todos body_params
        components = [{
            "type": "body",
            "parameters": [{"type": "text", "text": str(p)} for p in params],
        }]

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
                WPP_API_URL,
                headers=_headers(),
                json=payload,
                timeout=WPP_TIMEOUT,
            )
            if r.status_code == 200:
                log.info(f"📤 Plantilla '{template_name}' enviada a {to}")
                return True
            log.warning(
                f"⚠️ Error plantilla {template_name} {r.status_code} "
                f"{r.text[:300]!r} (intent {attempt+1})"
            )
            if _should_retry(r.status_code) and attempt < 2:
                _backoff(attempt)
                continue
            return False
        except Exception as e:
            log.exception("❌ Error en send_template_message")
            if attempt < 2:
                _backoff(attempt)
                continue
            return False
    return False

# ==========================
# Google Sheets / Drive (opcional, degradable)
# ==========================
creds = None
sheets_svc = None
drive_svc = None
google_ready = False

if GOOGLE_CREDENTIALS_JSON and service_account and build:
    try:
        info = json.loads(GOOGLE_CREDENTIALS_JSON)
        creds = service_account.from_json(json.dumps(info), scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ])
        sheets_svc = build("sheets", "v4", credentials=creds)
        drive_svc = build("drive", "v3", credentials=creds)
        google_ready = True
        log.info("✅ Google configurado correctamente")
    except Exception:
        log.exception("❌ Error configurando Google APIs")

def match_client_in_sheets(phone_last10: str) -> Optional[Dict[str, Any]]:
    """Busca el teléfono en la hoja de SECOM (columna teléfono)."""
    if not (google_ready and sheets_svc and SHEETS_ID_LEADS and SHEETS_TITLE_LEADS):
        return None
    try:
        rng = f"{SHEETS_TITLE_LEADS}!A:Z"
        values = sheets_svc.spreadsheets().values().get(
            spreadsheetId=SHEETS_ID_LEADS,
            range=rng
        ).execute()
        rows = values.get("values", [])
        target = str(phone_last10)
        for row in rows:
            if len(row) > 2:
                tel = re.sub(r"\D", "", row[2])[-10:]
                if tel == target:
                    nombre = row[0] if len(row) > 0 else ""
                    return {"nombre": nombre}
        return None
    except Exception:
        log.exception("❌ Error en match_client_in_sheets")
        return None

# (aquí mantienes tus helpers de Drive para guardar archivos, si ya los tenías)

# ==========================
# Embudos (IMSS, Empresarial, FP, Auto)
# ==========================

def imss_start(phone: str, match: Optional[Dict[str, Any]]) -> None:
    user_state[phone] = "imss_beneficios"
    log.info(f"🏥 Iniciando embudo IMSS para {phone}")
    send_message(
        phone,
        "🟩 *Préstamo IMSS Ley 73*\n"
        "Te ayudo a revisar si calificas para un préstamo con tasa preferencial. "
        "¿Te interesa conocer requisitos? (responde *sí* o *no*)"
    )

def _imss_next(phone: str, text: str) -> None:
    st = user_state.get(phone, "")
    data = _ensure_user(phone)

    if st == "imss_beneficios":
        if interpret_response(text) == "positive":
            user_state[phone] = "imss_pension"
            send_message(phone, "¿Cuál es tu *pensión mensual* aproximada?")
        else:
            send_message(phone, "Sin problema. Si deseas volver al menú, escribe *menú*.")
    elif st == "imss_pension":
        pension = extract_number(text)
        if not pension or pension < 3000:
            send_message(phone, "Con ese monto es poco viable. Si tienes duda, escribe *menú*.")
            return
        data["imss_pension"] = pension
        user_state[phone] = "imss_nombre"
        send_message(phone, "¿Cuál es tu *nombre completo*?")
    elif st == "imss_nombre":
        data["imss_nombre"] = text.strip()
        user_state[phone] = ""
        send_message(phone, "✅ Gracias. Un asesor validará tu información y te contactará.")
        if ADVISOR_NUMBER:
            send_message(
                ADVISOR_NUMBER,
                f"🔔 Lead IMSS\nTel: {phone}\nNombre: {data.get('imss_nombre','')}\nPensión: {data.get('imss_pension','')}"
            )

def emp_start(phone: str, match: Optional[Dict[str, Any]]) -> None:
    user_state[phone] = "emp_confirma"
    send_message(
        phone,
        "🏢 *Crédito Empresarial*\n"
        "Te ayudo a evaluar una línea de crédito para tu empresa. ¿Eres empresario(a)? (responde *sí* o *no*)"
    )

def _emp_next(phone: str, text: str) -> None:
    st = user_state.get(phone, "")
    data = _ensure_user(phone)

    if st == "emp_confirma":
        if interpret_response(text) != "positive":
            send_message(phone, "Entendido. Si deseas volver al menú, escribe *menú*.")
            return
        user_state[phone] = "emp_giro"
        send_message(phone, "¿A qué *se dedica* tu empresa?")
    elif st == "emp_giro":
        data["emp_giro"] = text.strip()
        user_state[phone] = "emp_monto"
        send_message(phone, "¿Qué *monto* necesitas? (mínimo $100,000)")
    elif st == "emp_monto":
        monto = extract_number(text)
        if not monto or monto < 100000:
            send_message(phone, "El monto mínimo es $100,000. Indica un monto igual o mayor.")
            return
        data["emp_monto"] = monto
        user_state[phone] = "emp_nombre"
        send_message(phone, "¿Tu *nombre completo*?")
    elif st == "emp_nombre":
        data["emp_nombre"] = text.strip()
        user_state[phone] = "emp_ciudad"
        send_message(phone, "¿Tu *ciudad*?")
    elif st == "emp_ciudad":
        data["emp_ciudad"] = text.strip()
        resumen = (
            "✅ Gracias. Un asesor te contactará.\n"
            f"- Nombre: {data.get('emp_nombre','')}\n"
            f"- Ciudad: {data.get('emp_ciudad','')}\n"
            f"- Giro: {data.get('emp_giro','')}\n"
            f"- Monto: ${data.get('emp_monto',0):,.0f}\n"
        )
        send_message(phone, resumen)
        if ADVISOR_NUMBER:
            send_message(
                ADVISOR_NUMBER,
                f"🔔 Lead Empresarial\nTel: {phone}\n{resumen}"
            )
        user_state[phone] = ""

def fp_start(phone: str, match: Optional[Dict[str, Any]]) -> None:
    user_state[phone] = "fp_monto"
    send_message(phone, "💳 *Financiamiento Práctico*\n¿Qué monto necesitas?")

def _fp_next(phone: str, text: str) -> None:
    st = user_state.get(phone, "")
    data = _ensure_user(phone)
    if st == "fp_monto":
        monto = extract_number(text)
        if not monto:
            send_message(phone, "Indícame un monto válido, por favor.")
            return
        data["fp_monto"] = monto
        user_state[phone] = ""
        send_message(phone, "✅ Gracias. Un asesor revisará tu solicitud.")
        if ADVISOR_NUMBER:
            send_message(
                ADVISOR_NUMBER,
                f"🔔 Lead Financiamiento Práctico\nTel: {phone}\nMonto: ${monto:,.0f}"
            )

def auto_start(phone: str, match: Optional[Dict[str, Any]]) -> None:
    user_state[phone] = "auto_intro"
    log.info(f"🚗 Iniciando embudo seguro auto para {phone}")
    send_message(
        phone,
        "🚗 *Seguro de Auto*\n"
        "Envíame por favor:\n"
        "• Foto de tu INE\n"
        "• Tarjeta de circulación o placa\n"
        "• Si tienes póliza actual, foto donde se vea la fecha de vencimiento.\n"
        "Cuando lo envíes, te confirmaré recepción y procesaré la cotización."
    )

def _auto_next(phone: str, text: str) -> None:
    st = user_state.get(phone, "")
    intent = interpret_response(text)

    if st == "auto_intro":
        if "vencimiento" in text.lower():
            user_state[phone] = "auto_vencimiento_fecha"
            send_message(phone, "¿Cuál es la *fecha de vencimiento* de tu póliza actual? (AAAA-MM-DD)")
        elif intent == "negative":
            send_message(phone, "Sin problema. Si deseas una propuesta más adelante, escribe *auto* o *2*.")
            user_state[phone] = ""
        else:
            send_message(phone, "Perfecto, también puedes enviarme directamente las fotos y datos para cotizar.")
    elif st == "auto_vencimiento_fecha":
        user_state[phone] = ""
        send_message(phone, "✅ Gracias. Programaremos contacto antes de esa fecha para ofrecerte la mejor opción.")
        if ADVISOR_NUMBER:
            send_message(
                ADVISOR_NUMBER,
                f"🔔 Cliente SECOM {phone} indicó fecha de vencimiento: {text}"
            )

# ==========================
# Helpers de router
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

def _notify_advisor(text: str) -> None:
    if ADVISOR_NUMBER:
        send_message(ADVISOR_NUMBER, text)

def _route_command(phone: str, text: str, match: Optional[Dict[str, Any]]) -> None:
    t = text.strip().lower()
    if t in ("1", "imss", "ley 73", "préstamo", "prestamo", "pension", "pensión"):
        imss_start(phone, match)
    elif t in ("2", "auto", "seguros de auto", "seguro auto"):
        auto_start(phone, match)
    elif t in ("3", "vida", "salud", "seguro de vida", "seguro de salud"):
        send_message(phone, "🧬 *Seguros de Vida/Salud* — Notificaré al asesor para contactarte.")
        _notify_advisor(f"🔔 Vida/Salud — Solicitud de contacto\nWhatsApp: {phone}")
        send_main_menu(phone)
    elif t in ("4", "vrim", "tarjeta médica", "tarjeta medica"):
        send_message(phone, "🩺 *VRIM* — Notificaré al asesor para darte detalles.")
        _notify_advisor(f"🔔 VRIM — Solicitud de contacto\nWhatsApp: {phone}")
        send_main_menu(phone)
    elif t in ("5", "empresarial", "pyme", "crédito empresarial", "credito empresarial"):
        emp_start(phone, match)
    elif t in ("6", "financiamiento práctico", "financiamiento practico", "crédito simple", "credito simple"):
        fp_start(phone, match)
    elif t in ("7", "contactar", "asesor", "contactar con christian"):
        _notify_advisor(f"🔔 Contacto directo — Cliente solicita hablar\nWhatsApp: {phone}")
        send_message(phone, "✅ Listo. Avisé a Christian para que te contacte.")
        send_main_menu(phone)
    elif t in ("menu", "menú", "inicio", "hola"):
        user_state[phone] = ""
        send_main_menu(phone)
    else:
        st = user_state.get(phone, "")
        intent = interpret_response(text)

        # ==========================
        # Seguimiento campañas WAPI / SECOM
        # ==========================
        if st.startswith("campaign_secom_auto"):
            # Campaña: revisión gratuita seguro auto para clientes SECOM
            if intent == "positive":
                log.info(f"✅ Respuesta positiva campaña SECOM AUTO de {phone}")
                send_message(phone, "Perfecto ✅ Iniciemos con la revisión gratuita de tu seguro de auto.")
                user_state[phone] = ""  # se reasigna dentro de auto_start
                auto_start(phone, match)
            elif intent == "negative":
                log.info(f"❌ Respuesta negativa campaña SECOM AUTO de {phone}")
                send_message(phone, "Gracias por responder 🙌. Si más adelante deseas una revisión gratuita de tu seguro, escribe *2* o *auto*.")
                user_state[phone] = ""
                send_main_menu(phone)
            else:
                send_message(phone, "Solo para confirmar, ¿te interesa una revisión gratuita de tu seguro de auto? Responde *sí* o *no*, o escribe *menú*.")
            return

        if st.startswith("campaign_imss_ley73"):
            # Campaña: Préstamo IMSS Ley 73
            if intent == "positive":
                log.info(f"✅ Respuesta positiva campaña IMSS Ley 73 de {phone}")
                send_message(phone, "Perfecto ✅ Empecemos con tu evaluación para *Préstamo IMSS Ley 73*.")
                user_state[phone] = ""  # se reasigna en imss_start
                imss_start(phone, match)
            elif intent == "negative":
                log.info(f"❌ Respuesta negativa campaña IMSS Ley 73 de {phone}")
                send_message(phone, "Entendido 🙌. Si en otro momento te interesa un préstamo IMSS Ley 73, escribe *1* o *imss*.")
                user_state[phone] = ""
                send_main_menu(phone)
            else:
                send_message(phone, "Solo para confirmar, ¿te interesa que revisemos si calificas para un préstamo IMSS Ley 73? Responde *sí* o *no*, o escribe *menú*.")
            return

        # ==========================
        # Flujos existentes
        # ==========================
        if st.startswith("imss_"):
            _imss_next(phone, text)
        elif st.startswith("emp_"):
            _emp_next(phone, text)
        elif st.startswith("fp_"):
            _fp_next(phone, text)
        elif st.startswith("auto_"):
            _auto_next(phone, text)
        else:
            send_message(phone, "No entendí. Escribe *menú* para ver opciones.")

# ==========================
# Webhook — verificación
# ==========================
@app.get("/webhook")
def webhook_verify():
    try:
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge", "")
        if mode == "subscribe" and token == VERIFY_TOKEN:
            log.info("✅ Webhook verificado correctamente")
            return challenge, 200
    except Exception:
        log.exception("❌ Error en verificación webhook")
    log.warning("❌ Webhook verification failed")
    return "forbidden", 403

# ==========================
# Webhook — recepción mensajes
# ==========================
@app.post("/webhook")
def webhook_receive():
    try:
        payload = request.get_json(force=True, silent=True) or {}
        log.info(f"📥 Webhook recibido: {json.dumps(payload, indent=2)[:500]}...")
        entry = payload.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        value = changes.get("value", {})
        messages = value.get("messages", [])
        if not messages:
            return jsonify({"ok": True}), 200

        msg = messages[0]
        phone = msg.get("from")
        if not phone:
            log.warning("⚠️ Mensaje sin número")
            return jsonify({"ok": True}), 200

        log.info(f"📱 Mensaje de {phone}: {msg.get('type', 'unknown')}")

        # Matching SECOM (si existe hoja)
        match = match_client_in_sheets(_normalize_phone_last10(phone))

        mtype = msg.get("type")
        if mtype == "text" and "text" in msg:
            text = msg["text"].get("body", "").strip()
            log.info(f"💬 Texto recibido de {phone}: {text}")

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
                    log.exception("❌ Error llamando a OpenAI")
                    send_message(phone, "Hubo un detalle al procesar tu solicitud. Intentemos de nuevo.")
                    return jsonify({"ok": True}), 200

            _route_command(phone, text, match)
            return jsonify({"ok": True}), 200

        if mtype in {"image", "document", "audio", "video"}:
            log.info(f"📎 Multimedia recibida de {phone}: {mtype}")
            # Aquí puedes reutilizar tu lógica de guardado en Drive si ya la tenías
            return jsonify({"ok": True}), 200

        log.info(f"ℹ️ Tipo de mensaje no manejado: {mtype}")
        return jsonify({"ok": True}), 200

    except Exception:
        log.exception("❌ Error en webhook_receive")
        return jsonify({"ok": True}), 200

# ==========================
# Endpoints auxiliares externos
# ==========================
@app.get("/health")
def health():
    return jsonify({
        "status": "ok",
        "service": "Vicky Bot SECOM",
        "timestamp": datetime.utcnow().isoformat()
    }), 200

@app.get("/ext/health")
def ext_health():
    return jsonify({
        "status": "ok",
        "whatsapp_configured": bool(META_TOKEN and WABA_PHONE_ID),
        "google_ready": google_ready,
        "openai_ready": bool(openai and OPENAI_API_KEY)
    }), 200

@app.post("/ext/test-send")
def ext_test_send():
    try:
        data = request.get_json(force=True) or {}
        to = data.get("to")
        text = data.get("text", "Prueba desde Vicky SECOM")
        if not to:
            return jsonify({"ok": False, "error": "Falta 'to'"}), 400
        ok = send_message(to, text)
        return jsonify({"ok": ok}), (200 if ok else 500)
    except Exception as e:
        log.exception("❌ Error en ext_test_send")
        return jsonify({"ok": False, "error": str(e)}), 500

# ========= Envío masivo (send-promo) =========

def _bulk_send_worker(items: List[Dict[str, Any]]) -> None:
    """Worker para envíos masivos con logging y marcado de campaña."""
    successful = 0
    failed = 0
    log.info(f"🚀 Iniciando envío masivo de {len(items)} mensajes")

    for i, item in enumerate(items, 1):
        try:
            to = (item.get("to") or "").strip()
            text = (item.get("text") or "").strip()
            template = (item.get("template") or "").strip()
            params = item.get("params", [])

            if not to:
                log.warning(f"⏭️ Item {i} sin 'to', se omite")
                failed += 1
                continue

            success = False
            if template:
                success = send_template_message(to, template, params)
            elif text:
                success = send_message(to, text)
            else:
                log.warning(f"⏭️ Item {i} sin contenido, se omite")
                failed += 1
                continue

            if success:
                successful += 1

                # Marcar contexto de campaña para dar seguimiento correcto
                lower_text = (text or "").lower()
                campaign = (item.get("campaign") or item.get("flow") or "").lower()
                try:
                    last10 = _normalize_phone_last10(to)
                    if "cliente secom" in lower_text and "seguro de auto" in lower_text:
                        # Promoción revisión gratuita SECOM Auto
                        user_state[last10] = "campaign_secom_auto"
                    elif "préstamo imss" in lower_text or "prestamo imss" in lower_text:
                        # Promoción préstamo IMSS Ley 73
                        user_state[last10] = "campaign_imss_ley73"
                    elif campaign:
                        # Campañas futuras parametrizadas
                        user_state[last10] = f"campaign_{campaign}"
                except Exception as e:
                    log.warning(f"⚠️ No se pudo registrar estado de campaña para {to}: {e}")

            else:
                failed += 1

            time.sleep(0.5)

        except Exception as e:
            failed += 1
            log.exception(f"❌ Error procesando item {i}: {e}")

    log.info(f"✅ Envío masivo finalizado: OK={successful} / FAIL={failed}")

@app.post("/ext/send-promo")
def ext_send_promo():
    """Endpoint para recibir campañas tipo WAPI y encolarlas en background."""
    try:
        if not META_TOKEN or not WABA_PHONE_ID:
            log.error("❌ WhatsApp Business API no configurada")
            return jsonify({
                "queued": False,
                "error": "WhatsApp Business API no configurada"
            }), 500

        body = request.get_json(force=True) or {}
        items = body.get("items", [])

        if not isinstance(items, list):
            return jsonify({
                "queued": False,
                "error": "Formato inválido: 'items' debe ser lista"
            }), 400

        if not items:
            return jsonify({
                "queued": False,
                "error": "Sin items para procesar"
            }), 400

        threading.Thread(
            target=_bulk_send_worker,
            args=(items,),
            daemon=True,
            name="BulkSendWorker",
        ).start()

        return jsonify({
            "queued": True,
            "message": f"Procesando {len(items)} mensajes en background",
            "total_received": len(items),
            "timestamp": datetime.utcnow().isoformat()
        }), 202

    except Exception as e:
        log.exception("❌ Error en ext_send_promo")
        return jsonify({
            "queued": False,
            "error": str(e)
        }), 500

# ==========================
# Arranque local
# ==========================
if __name__ == "__main__":
    log.info(f"🚀 Iniciando Vicky Bot SECOM en puerto {PORT}")
    log.info(f"📞 WhatsApp configurado: {bool(META_TOKEN and WABA_PHONE_ID)}")
    log.info(f"📊 Google listo: {google_ready}")
    log.info(f"🧠 OpenAI listo: {bool(openai and OPENAI_API_KEY)}")
    app.run(host="0.0.0.0", port=PORT, debug=False)


