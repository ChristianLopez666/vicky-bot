# app.py — Vicky SECOM (Versión 100% Funcional Corregida - Webhook FIXED)
# Python 3.11+
# ------------------------------------------------------------
# CORRECCIONES APLICADAS:
# 1. ✅ Endpoint /ext/send-promo completamente funcional
# 2. ✅ Eliminación de función duplicada
# 3. ✅ Validación robusta de configuración
# 4. ✅ Logging exhaustivo para diagnóstico
# 5. ✅ Manejo mejorado de errores
# 6. ✅ Worker para envíos masivos
# 7. ✅ WEBHOOK FIXED - Detección temprana de respuestas a plantillas
# 8. ✅ Decision Layer integrado (Boardroom Engine)
# ------------------------------------------------------------

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

# Google
try:
    from google.oauth2 import service_account
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

META_TOKEN = os.getenv("META_TOKEN")
WABA_PHONE_ID = os.getenv("WABA_PHONE_ID")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
ADVISOR_NUMBER = os.getenv("ADVISOR_NUMBER", "5216682478005")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")
SHEETS_ID_LEADS = os.getenv("SHEETS_ID_LEADS")
SHEETS_TITLE_LEADS = os.getenv("SHEETS_TITLE_LEADS", "Prospectos SECOM Auto")
DRIVE_PARENT_FOLDER_ID = os.getenv("DRIVE_PARENT_FOLDER_ID")

PORT = int(os.getenv("PORT", "5000"))

# ==========================
# Decision Layer (Boardroom)
# ==========================
BOARDROOM_DECISION_URL = os.getenv(
    "BOARDROOM_DECISION_URL",
    "https://boardroom-engine.onrender.com/api/decision/process",
)
BOARDROOM_AUTH_TOKEN = os.getenv("BOARDROOM_AUTH_TOKEN", "")
DECISION_TIMEOUT_SECONDS = int(os.getenv("DECISION_TIMEOUT_SECONDS", "5"))

# Configuración de logging robusta
logging.basicConfig(
    level=logging.INFO, 
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-d %H:%M:%S"
)
log = logging.getLogger("vicky-secom")

if OPENAI_API_KEY and openai:
    try:
        openai.api_key = OPENAI_API_KEY
        log.info("OpenAI configurado correctamente")
    except Exception:
        log.warning("OpenAI configurado pero no disponible")

# ==========================
# Google Setup (degradable)
# ==========================
creds = None
sheets_svc = None
drive_svc = None
google_ready = False

if GOOGLE_CREDENTIALS_JSON and service_account and build:
    try:
        info = json.loads(GOOGLE_CREDENTIALS_JSON)
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]
        creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
        sheets_svc = build("sheets", "v4", credentials=creds)
        drive_svc = build("drive", "v3", credentials=creds)
        google_ready = True
        log.info("✅ Google services listos (Sheets + Drive)")
    except Exception:
        log.exception("❌ No fue posible inicializar Google. Modo mínimo activo.")
else:
    log.warning("⚠️ Credenciales de Google no disponibles. Modo mínimo activo.")

# =================================
# Estado por usuario en memoria
# =================================
app = Flask(__name__)
user_state: Dict[str, str] = {}
user_data: Dict[str, Dict[str, Any]] = {}

# ==========================
# Utilidades generales
# ==========================
WPP_API_URL = f"https://graph.facebook.com/v20.0/{WABA_PHONE_ID}/messages" if WABA_PHONE_ID else None
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

# ==========================
# Decision Layer Helper
# ==========================
def call_decision_layer(telefono: str, mensaje: str, nombre: str = "") -> Optional[Dict[str, Any]]:
    if not BOARDROOM_DECISION_URL or not BOARDROOM_AUTH_TOKEN:
        logging.warning("[decision-layer] configuración incompleta; se omite llamada")
        return None
    payload = {
        "source": "whatsapp_inbound",
        "telefono": telefono,
        "mensaje": mensaje or "",
        "nombre": nombre or "",
    }
    headers = {
        "Authorization": f"Bearer {BOARDROOM_AUTH_TOKEN}",
        "Content-Type": "application/json",
    }
    try:
        resp = requests.post(
            BOARDROOM_DECISION_URL,
            headers=headers,
            json=payload,
            timeout=DECISION_TIMEOUT_SECONDS,
        )
        if resp.status_code != 200:
            logging.warning(
                "[decision-layer] HTTP %s: %s",
                resp.status_code,
                resp.text[:500],
            )
            return None
        data = resp.json()
        if not isinstance(data, dict) or not data.get("ok"):
            logging.warning("[decision-layer] respuesta inválida: %s", data)
            return None
        decision = data.get("decision")
        if not isinstance(decision, dict):
            logging.warning("[decision-layer] 'decision' ausente o inválido")
            return None
        return decision
    except Exception as e:
        logging.exception("[decision-layer] fallo en POST /api/decision/process: %s", e)
        return None

# ==========================
# WhatsApp Helpers (retries)
# ==========================
def _wpp_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {META_TOKEN}",
        "Content-Type": "application/json"
    }

def _should_retry(status: int) -> bool:
    return status == 429 or 500 <= status < 600

def _backoff(attempt: int) -> None:
    time.sleep(2 ** attempt)

def send_message(to: str, text: str) -> bool:
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
            log.info(f"📤 Enviando mensaje a {to} (intento {attempt + 1})")
            resp = requests.post(WPP_API_URL, headers=_wpp_headers(), json=payload, timeout=WPP_TIMEOUT)
            if resp.status_code == 200:
                log.info(f"✅ Mensaje enviado exitosamente a {to}")
                return True
            log.warning(f"⚠️ WPP send_message fallo {resp.status_code}: {resp.text[:200]}")
            if _should_retry(resp.status_code) and attempt < 2:
                _backoff(attempt)
                continue
            return False
        except requests.exceptions.Timeout:
            log.error(f"⏰ Timeout enviando mensaje a {to} (intento {attempt + 1})")
            if attempt < 2:
                _backoff(attempt)
                continue
            return False
        except Exception:
            log.exception(f"❌ Error en send_message a {to}")
            if attempt < 2:
                _backoff(attempt)
                continue
            return False
    return False


def forward_media_to_advisor(media_type: str, media_id: str) -> bool:
    if not (META_TOKEN and WPP_API_URL and ADVISOR_NUMBER):
        return False
    payload = {
        "messaging_product": "whatsapp",
        "to": ADVISOR_NUMBER,
        "type": media_type,
        media_type: {"id": media_id}
    }
    try:
        resp = requests.post(WPP_API_URL, headers=_wpp_headers(), json=payload, timeout=WPP_TIMEOUT)
        if resp.status_code == 200:
            log.info(f"📤 Multimedia reenviada al asesor ({media_type})")
            return True
        log.warning(f"⚠️ Reenvío multimedia falló {resp.status_code}: {resp.text[:200]}")
        return False
    except Exception:
        log.exception("❌ Error reenviando multimedia al asesor")
        return False

def send_template_message(to: str, template_name: str, params: Dict | List) -> bool:
    if not (META_TOKEN and WPP_API_URL):
        log.error("❌ WhatsApp no configurado para plantillas.")
        return False
    components: List[Dict[str, Any]] = []
    if template_name == "seguro_auto_70":
        image_url = os.getenv("SEGURO_AUTO_70_IMAGE_URL")
        if not image_url:
            log.error("❌ Falta SEGURO_AUTO_70_IMAGE_URL en entorno.")
            return False
        components.append({
            "type": "header",
            "parameters": [{"type": "image", "image": {"link": image_url}}]
        })
    if isinstance(params, dict):
        body_params = [{"type": "text", "parameter_name": k, "text": str(v)} for k, v in params.items()]
        if body_params:
            components.append({"type": "body", "parameters": body_params})
    elif isinstance(params, list):
        body_params = [{"type": "text", "text": str(v)} for v in params]
        if body_params:
            components.append({"type": "body", "parameters": body_params})
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": "es_MX"},
            **({"components": components} if components else {})
        }
    }
    for attempt in range(3):
        try:
            log.info(f"📤 Enviando plantilla '{template_name}' a {to} (intento {attempt + 1})")
            resp = requests.post(WPP_API_URL, headers=_wpp_headers(), json=payload, timeout=WPP_TIMEOUT)
            if resp.status_code == 200:
                msg_id = ""
                try:
                    j = resp.json() if resp.text else {}
                    msgs = (j or {}).get("messages") or []
                    if msgs and isinstance(msgs, list):
                        msg_id = (msgs[0] or {}).get("id", "")
                except Exception:
                    msg_id = ""
                try:
                    append_envio_status(to, msg_id, "sent", template_name, datetime.utcnow().isoformat())
                except Exception:
                    pass
                log.info(f"✅ Plantilla '{template_name}' enviada exitosamente a {to}")
                return True
            log.warning(f"⚠️ WPP send_template fallo {resp.status_code}: {resp.text[:200]}")
            if _should_retry(resp.status_code) and attempt < 2:
                _backoff(attempt)
                continue
            return False
        except requests.exceptions.Timeout:
            log.error(f"⏰ Timeout enviando plantilla a {to} (intento {attempt + 1})")
            if attempt < 2:
                _backoff(attempt)
                continue
            return False
        except Exception:
            log.exception(f"❌ Error en send_template_message a {to}")
            if attempt < 2:
                _backoff(attempt)
                continue
            return False
    return False

# ==========================
# Google Helpers
# ==========================
def match_client_in_sheets(phone_last10: str) -> Optional[Dict[str, Any]]:
    if not (google_ready and sheets_svc and SHEETS_ID_LEADS and SHEETS_TITLE_LEADS):
        log.warning("⚠️ Sheets no disponible; no se puede hacer matching.")
        return None
    try:
        headers, rows = _sheet_get_rows()
        if not headers:
            return None
        i_name = _idx(headers, "Nombre")
        i_wa = _idx(headers, "WhatsApp")
        i_status = _idx(headers, "ESTATUS")
        i_last = _idx(headers, "LAST_MESSAGE_AT")
        if i_wa is None:
            log.warning("⚠️ No existe columna 'WhatsApp' en el Sheet.")
            return None
        target = str(phone_last10).strip()
        for k, row in enumerate(rows, start=2):
            wa_cell = _cell(row, i_wa)
            wa_last10 = _normalize_phone_last10(wa_cell)
            if target and wa_last10 == target:
                nombre = _cell(row, i_name).strip() if i_name is not None else ""
                estatus = _cell(row, i_status).strip() if i_status is not None else ""
                last_at = _cell(row, i_last).strip() if i_last is not None else ""
                log.info(f"✅ Cliente encontrado en Sheets: {nombre} ({target})")
                return {"row": k, "nombre": nombre, "estatus": estatus, "last_message_at": last_at, "raw": row}
        log.info(f"ℹ️ Cliente no encontrado en Sheets: {target}")
        return None
    except Exception:
        log.exception("❌ Error buscando en Sheets")
        return None

def write_followup_to_sheets(row: int | str, note: str, date_iso: str) -> None:
    if not (google_ready and sheets_svc and SHEETS_ID_LEADS):
        log.warning("⚠️ Sheets no disponible; no se puede escribir seguimiento.")
        return
    try:
        title = "Seguimiento"
        body = {"values": [[str(row), date_iso, note]]}
        sheets_svc.spreadsheets().values().append(
            spreadsheetId=SHEETS_ID_LEADS,
            range=f"{title}!A:C",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body=body
        ).execute()
        log.info(f"✅ Seguimiento registrado en Sheets: {note}")
    except Exception:
        log.exception("❌ Error escribiendo seguimiento en Sheets")

def append_envio_status(phone: str, message_id: str, status: str, template_name: str, timestamp_iso: str) -> None:
    if not (google_ready and sheets_svc and SHEETS_ID_LEADS):
        return
    try:
        p10 = _normalize_phone_last10(phone)
        body = {"values": [[p10, message_id or "", status or "", timestamp_iso or "", template_name or ""]]}
        sheets_svc.spreadsheets().values().append(
            spreadsheetId=SHEETS_ID_LEADS,
            range="ENVIO_STATUS!A:E",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body=body
        ).execute()
    except Exception:
        log.exception("❌ Error escribiendo ENVIO_STATUS")

def append_respuesta_cliente(phone: str, nombre: str, mensaje: str, fecha_iso: str) -> None:
    if not (google_ready and sheets_svc and SHEETS_ID_LEADS):
        return
    try:
        p10 = _normalize_phone_last10(phone)
        body = {"values": [[p10, nombre or "", mensaje or "", fecha_iso or ""]]}
        sheets_svc.spreadsheets().values().append(
            spreadsheetId=SHEETS_ID_LEADS,
            range="RESPUESTAS_CLIENTE!A:D",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body=body
        ).execute()
    except Exception:
        log.exception("❌ Error escribiendo RESPUESTAS_CLIENTE")

def get_last_envio_template(phone_last10: str) -> str:
    if not (google_ready and sheets_svc and SHEETS_ID_LEADS):
        return ""
    try:
        resp = sheets_svc.spreadsheets().values().get(
            spreadsheetId=SHEETS_ID_LEADS, range="ENVIO_STATUS!A:E").execute()
        vals = resp.get("values") or []
        target = (phone_last10 or "").strip()
        for row in reversed(vals[1:]):
            if len(row) >= 1 and _normalize_phone_last10(row[0]) == target:
                return (row[4] if len(row) >= 5 else "").strip()
        return ""
    except Exception:
        log.exception("❌ Error leyendo ENVIO_STATUS")
        return ""

def _find_or_create_client_folder(folder_name: str) -> Optional[str]:
    if not (google_ready and drive_svc and DRIVE_PARENT_FOLDER_ID):
        log.warning("⚠️ Drive no disponible; no se puede crear carpeta.")
        return None
    try:
        q = f"name = '{folder_name}' and mimeType = 'application/vnd.google-apps.folder' and '{DRIVE_PARENT_FOLDER_ID}' in parents and trashed = false"
        resp = drive_svc.files().list(q=q, fields="files(id, name)").execute()
        items = resp.get("files", [])
        if items:
            return items[0]["id"]
        meta = {
            "name": folder_name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [DRIVE_PARENT_FOLDER_ID],
        }
        created = drive_svc.files().create(body=meta, fields="id").execute()
        return created.get("id")
    except Exception:
        log.exception("❌ Error creando/buscando carpeta en Drive")
        return None

def upload_to_drive(file_name: str, file_bytes: bytes, mime_type: str, folder_name: str) -> Optional[str]:
    if not (google_ready and drive_svc and MediaIoBaseUpload):
        log.warning("⚠️ Drive no disponible; no se puede subir archivo.")
        return None
    try:
        folder_id = _find_or_create_client_folder(folder_name)
        if not folder_id:
            return None
        media = MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype=mime_type, resumable=False)
        meta = {"name": file_name, "parents": [folder_id]}
        created = drive_svc.files().create(body=meta, media_body=media, fields="id, webViewLink").execute()
        link = created.get("webViewLink") or created.get("id")
        log.info(f"✅ Archivo subido a Drive: {file_name} -> {link}")
        return link
    except Exception:
        log.exception("❌ Error subiendo archivo a Drive")
        return None

# ==========================
# Menú principal
# ==========================
MAIN_MENU = (
     "Hola 👋 Soy Vicky, asistente de tu Asesor Financiero Christian López — INBURSA.\n\n"
    "😊 ¿En qué te puedo orientar hoy?\n\n"
    "1️⃣ Préstamo IMSS (Ley 73)\n"
    "2️⃣ Seguro de Auto 🚗\n"
    "3️⃣ Seguro de Vida / Salud ❤️\n"
    "4️⃣ Tarjeta médica VRIM 🩺\n"
    "5️⃣ Crédito Empresarial 🏢\n"
    "6️⃣ Financiamiento Práctico 💳\n"
    "7️⃣ Hablar con Christian 📞\n\n"
    "✍️ Responde con el número o el nombre del servicio."
)

def send_main_menu(phone: str) -> None:
    log.info(f"📋 Enviando menú principal a {phone}")
    send_message(phone, MAIN_MENU)

# ==========================
# Embudos
# ==========================
def _notify_advisor(text: str) -> None:
    try:
        log.info(f"👨‍💼 Notificando al asesor: {text}")
        send_message(ADVISOR_NUMBER, text)
    except Exception:
        log.exception("❌ Error notificando al asesor")

TPV_TEMPLATE_NAME = "promo_tpv"
ALLIANCE_TEMPLATES = {"despachis_contables"}

def _parse_dt_maybe(value: str) -> Optional[datetime]:
    if not value:
        return None
    v = value.strip()
    try:
        if v.endswith("Z"):
            v = v[:-1] + "+00:00"
        return datetime.fromisoformat(v)
    except Exception:
        return None

def _tpv_is_context(match: Optional[Dict[str, Any]]) -> bool:
    if not match:
        return False
    if (match.get("estatus") or "").strip().upper() != "ENVIADO_TPV":
        return False
    dt = _parse_dt_maybe(match.get("last_message_at") or "")
    if not dt:
        return False
    now = datetime.now(dt.tzinfo) if dt.tzinfo is not None else datetime.utcnow()
    return (now - dt) <= timedelta(hours=24)

def tpv_start_from_reply(phone: str, text: str, match: Optional[Dict[str, Any]]) -> bool:
    t = (text or "").strip().lower()
    intent = interpret_response(text)
    if t == "1" or intent == "positive":
        user_state[phone] = "tpv_giro"
        send_message(phone, "✅ Perfecto. Para recomendarte la mejor terminal Inbursa, dime: ¿*a qué giro* pertenece tu negocio?")
        return True
    if t == "2" or intent == "negative":
        user_state[phone] = "tpv_motivo"
        send_message(phone, "Entendido. ¿Cuál fue el *motivo*? (opcional). Si no deseas responder, escribe *omitir*.")
        return True
    return False

def _tpv_next(phone: str, text: str, match: Optional[Dict[str, Any]]) -> None:
    st = user_state.get(phone, "")
    data = _ensure_user(phone)
    nombre = ""
    if match and match.get("nombre"):
        nombre = match["nombre"].strip()
    if st == "tpv_giro":
        giro = (text or "").strip()
        if not giro:
            send_message(phone, "Solo indícame tu *giro* (ej. restaurante, abarrotes, consultorio).")
            return
        data["tpv_giro"] = giro
        user_state[phone] = "tpv_horario"
        send_message(phone, "¿Qué *horario* te conviene para que Christian te contacte? (ej. hoy 4pm, mañana 10am)")
        return
    if st == "tpv_horario":
        horario = (text or "").strip()
        if not horario:
            send_message(phone, "Indícame un *horario* (ej. hoy 4pm, mañana 10am).")
            return
        data["tpv_horario"] = horario
        resumen = (
            "✅ Listo. En breve Christian te contactará para ofrecerte la mejor opción de terminal.\n"
            f"- Giro: {data.get('tpv_giro','')}\n"
            f"- Horario: {data.get('tpv_horario','')}"
        )
        send_message(phone, resumen)
        _notify_advisor(
            "🔔 TPV — Prospecto interesado\n"
            f"WhatsApp: {phone}\n"
            f"Nombre: {nombre or '(sin nombre)'}\n"
            f"Giro: {data.get('tpv_giro','')}\n"
            f"Horario: {data.get('tpv_horario','')}"
        )
        try:
            if match and match.get("row"):
                headers, _ = _sheet_get_rows()
                if headers and _idx(headers, "ESTATUS") is not None:
                    _update_row_cells(int(match["row"]), {"ESTATUS": "TPV_INTERESADO"}, headers)
        except Exception:
            log.exception("⚠️ No fue posible actualizar ESTATUS TPV_INTERESADO")
        user_state[phone] = "__greeted__"
        return
    if st == "tpv_motivo":
        motivo = (text or "").strip()
        if motivo.lower() == "omitir":
            motivo = ""
        data["tpv_motivo"] = motivo
        send_message(phone, "Gracias por tu respuesta. Si más adelante deseas una terminal, aquí estaré para ayudarte.")
        try:
            if match and match.get("row"):
                headers, _ = _sheet_get_rows()
                if headers and _idx(headers, "ESTATUS") is not None:
                    _update_row_cells(int(match["row"]), {"ESTATUS": "TPV_NO_INTERESADO"}, headers)
        except Exception:
            log.exception("⚠️ No fue posible actualizar ESTATUS TPV_NO_INTERESADO")
        user_state[phone] = "__greeted__"
        return

def _alianza_is_context(match: Optional[Dict[str, Any]]) -> bool:
    if not match:
        return False
    estatus = (match.get("estatus") or "").strip().upper()
    if estatus != "ENVIADO_ALIANZA":
        return False
    dt = _parse_dt_maybe(match.get("last_message_at") or "")
    if not dt:
        return False
    now = datetime.now(dt.tzinfo) if dt.tzinfo is not None else datetime.utcnow()
    return (now - dt) <= timedelta(hours=24)

def _explicit_non_alianza_intent(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return False
    alianza_signals = ("alianza", "despacho", "contable", "contables", "contador", "contadores", "comision", "comisión", "referir", "referencia")
    if any(k in t for k in alianza_signals):
        return False
    other_signals = ("auto", "seguro", "póliza", "poliza", "placa", "tarjeta de circulación", "circulacion", "tpv", "terminal", "punto de venta", "liga de pago", "link de pago", "imss", "ley 73", "modalidad", "pension", "pensión", "prestamo", "préstamo", "credito", "crédito", "empresarial", "pyme", "vida", "salud", "gmm", "gastos medicos", "gastos médicos", "vrim", "tarjeta medica", "tarjeta médica")
    return any(k in t for k in other_signals)

def _handle_alianza_context_response(phone: str, text: str, match: Dict[str, Any]) -> bool:
    st_now = user_state.get(phone, "")
    idle = st_now in ("", "__greeted__")
    if not idle:
        return False
    if not _alianza_is_context(match):
        return False
    if _explicit_non_alianza_intent(text):
        log.info(f"🔀 Escape ALIANZA→Router por intención explícita: {text}")
        return False
    nombre = (match.get("nombre") or "").strip() or "Cliente"
    _notify_advisor(f"🤝 ALIANZA — Interés/Respuesta detectada\nWhatsApp: {phone}\nNombre: {nombre}\nMensaje: {(text or '').strip()}")
    send_message(phone, "✅ Gracias. Ya tengo tu interés en la *alianza para despachos contables*.\nEn breve te comparto la información y un asesor te contactará.\nPara avanzar: ¿cómo se llama tu despacho y en qué ciudad estás?")
    user_state[phone] = "__greeted__"
    return True

def _auto_is_context(match: Optional[Dict[str, Any]]) -> bool:
    if not match:
        return False
    estatus = (match.get("estatus") or "").strip().upper()
    if estatus not in {"ENVIADO_INICIAL", "ENVIADO_AUTO", "ENVIADO_SEGURO_AUTO"}:
        return False
    dt = _parse_dt_maybe(match.get("last_message_at") or "")
    if not dt:
        return False
    now = datetime.now(dt.tzinfo) if dt.tzinfo is not None else datetime.utcnow()
    return (now - dt) <= timedelta(hours=24)

def _explicit_non_auto_intent(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return False
    auto_signals = ("auto", "seguro auto", "seguro de auto", "póliza", "poliza", "placa", "placas", "tarjeta de circulación", "tarjeta de circulacion", "circulación", "circulacion")
    if any(k in t for k in auto_signals):
        return False
    other_signals = ("credito", "crédito", "prestamo", "préstamo", "imss", "ley 73", "empresarial", "pyme", "tpv", "terminal", "punto de venta", "vida", "salud", "vrim", "tarjeta medica", "tarjeta médica", "financiamiento", "financiamiento práctico", "financiamiento practico")
    return any(k in t for k in other_signals)

def _handle_auto_context_response(phone: str, text: str, match: Dict[str, Any]) -> bool:
    t = (text or "").strip().lower()
    intent = interpret_response(text)
    st_now = user_state.get(phone, "")
    idle = st_now in ("", "__greeted__")
    if not idle:
        return False
    if not _auto_is_context(match):
        return False
    if _explicit_non_auto_intent(text):
        log.info(f"🔀 Escape AUTO→Router por intención explícita: {text}")
        return False
    if t in ("1", "si", "sí", "ok", "claro") or intent == "positive":
        user_state[phone] = "auto_intro"
        auto_start(phone, match)
        return True
    if t in ("2", "no", "nel") or intent == "negative":
        user_state[phone] = "auto_vencimiento_fecha"
        nombre = match.get("nombre", "").strip() or "Cliente"
        send_message(phone, f"Entendido {nombre}. Para poder recordarte a tiempo, ¿cuál es la *fecha de vencimiento* de tu póliza? (formato AAAA-MM-DD)")
        _notify_advisor(f"🔔 AUTO — NO INTERESADO / TIENE SEGURO\nWhatsApp: {phone}\nNombre: {nombre}\nRespuesta: {text}")
        return True
    if t in ("menu", "menú", "inicio"):
        user_state[phone] = "__greeted__"
        send_main_menu(phone)
        return True
    nombre = match.get("nombre", "").strip() or "Cliente"
    _notify_advisor(f"📩 AUTO — DUDA / INTERÉS detectada\nWhatsApp: {phone}\nNombre: {nombre}\nMensaje: {text}")
    send_message(phone, "¿Deseas cotizar tu seguro de auto ahora? Responde *Sí* o *No*")
    return True

def imss_start(phone: str, match: Optional[Dict[str, Any]]) -> None:
    user_state[phone] = "imss_beneficios"
    log.info(f"🏥 Iniciando embudo IMSS para {phone}")
    data = _ensure_user(phone)
    bonus_eligible = data.get("campaign_bonus_eligible") is True
    msg = "🟩 *Préstamo IMSS Ley 73*\n"
    if bonus_eligible:
        msg += "🎯 Este trimestre hay condiciones especiales en el préstamo IMSS.\n"
    msg += "Beneficios clave: trámite rápido, sin aval, pagos fijos y atención personalizada. ¿Te interesa conocer requisitos? (responde *sí* o *no*)"
    send_message(phone, msg)

def _imss_next(phone: str, text: str) -> None:
    st = user_state.get(phone, "")
    data = _ensure_user(phone)
    if st == "imss_beneficios":
        if interpret_response(text) == "positive":
            user_state[phone] = "imss_pension"
            send_message(phone, "¿Cuál es tu *pensión mensual* aproximada? (ej. $8,500)")
        else:
            send_message(phone, "Sin problema. Si deseas volver al menú, escribe *menú*.")
    elif st == "imss_pension":
        pension = extract_number(text)
        if not pension:
            send_message(phone, "No pude leer el monto. Indica tu *pensión mensual* (ej. 8500).")
            return
        data["imss_pension"] = pension
        user_state[phone] = "imss_monto"
        send_message(phone, "Gracias. ¿Qué *monto* te gustaría solicitar? (mínimo $40,000)")
    elif st == "imss_monto":
        monto = extract_number(text)
        if not monto:
            send_message(phone, "Escribe un *monto* (ej. 100000).")
            return
        data["imss_monto"] = monto
        user_state[phone] = "imss_nombre"
        send_message(phone, "Perfecto. ¿Cuál es tu *nombre completo*?")
    elif st == "imss_nombre":
        data["imss_nombre"] = text.strip()
        user_state[phone] = "imss_ciudad"
        send_message(phone, "¿En qué *ciudad* te encuentras?")
    elif st == "imss_ciudad":
        data["imss_ciudad"] = text.strip()
        user_state[phone] = "imss_nomina"
        send_message(phone, "¿Tienes *nómina Inbursa* actualmente? (sí/no)\n*Nota:* No es obligatoria; si la tienes, accedes a *beneficios adicionales*.")
    elif st == "imss_nomina":
        tiene_nomina = interpret_response(text) == "positive"
        data["imss_nomina_inbursa"] = "sí" if tiene_nomina else "no"
        msg = (
            "✅ *Preautorizado*. Un asesor te contactará.\n"
            f"- Nombre: {data.get('imss_nombre','')}\n"
            f"- Ciudad: {data.get('imss_ciudad','')}\n"
            f"- Pensión: ${data.get('imss_pension',0):,.0f}\n"
            f"- Monto deseado: ${data.get('imss_monto',0):,.0f}\n"
            f"- Nómina Inbursa: {data.get('imss_nomina_inbursa','no')}\n"
        )
        send_message(phone, msg)
        _notify_advisor(f"🔔 IMSS — Prospecto preautorizado\nWhatsApp: {phone}\n" + msg)
        user_state[phone] = "__greeted__"
        send_main_menu(phone)

def emp_start(phone: str, match: Optional[Dict[str, Any]]) -> None:
    user_state[phone] = "emp_confirma"
    send_message(phone, "🟦 *Crédito Empresarial*\n¿Eres empresario(a) o representas una empresa? (sí/no)")

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
        send_message(phone, "¿Qué *monto* deseas? (mínimo $100,000)")
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
        _notify_advisor(f"🔔 Empresarial — Nueva solicitud\nWhatsApp: {phone}\n" + resumen)
        user_state[phone] = "__greeted__"
        send_main_menu(phone)

FP_QUESTIONS = [f"Pregunta {i}" for i in range(1, 12)]

def fp_start(phone: str, match: Optional[Dict[str, Any]]) -> None:
    user_state[phone] = "fp_q1"
    _ensure_user(phone)["fp_answers"] = {}
    send_message(phone, "🟩 *Financiamiento Práctico*\nResponderemos 11 preguntas rápidas.\n1) " + FP_QUESTIONS[0])

def _fp_next(phone: str, text: str) -> None:
    st = user_state.get(phone, "")
    data = _ensure_user(phone)
    if st.startswith("fp_q"):
        idx = int(st.split("_q")[1]) - 1
        data["fp_answers"][f"q{idx+1}"] = text.strip()
        if idx + 1 < len(FP_QUESTIONS):
            user_state[phone] = f"fp_q{idx+2}"
            send_message(phone, f"{idx+2}) {FP_QUESTIONS[idx+1]}")
        else:
            user_state[phone] = "fp_comentario"
            send_message(phone, "¿Algún *comentario adicional*?")
    elif st == "fp_comentario":
        data["fp_comentario"] = text.strip()
        resumen = "✅ Gracias. Un asesor te contactará.\n" + "\n".join(f"{k.upper()}: {v}" for k, v in data.get("fp_answers", {}).items())
        if data.get("fp_comentario"):
            resumen += f"\nCOMENTARIO: {data['fp_comentario']}"
        send_message(phone, resumen)
        _notify_advisor(f"🔔 Financiamiento Práctico — Resumen\nWhatsApp: {phone}\n{resumen}")
        user_state[phone] = "__greeted__"
        send_main_menu(phone)

def auto_start(phone: str, match: Optional[Dict[str, Any]]) -> None:
    user_state[phone] = "auto_califica"
    send_message(phone, "🚗 *Seguro de Auto — Inbursa*\n\nPara orientarte mejor, ¿cuál es tu situación actual?\n\n1) Tengo seguro y quiero comparar precios\n2) Es mi primera vez contratando\n3) Mi póliza está por vencer")

def _auto_next(phone: str, text: str) -> None:
    st = user_state.get(phone, "")
    if st == "auto_califica":
        t = text.strip().lower()
        if t in ("1", "comparar", "tengo seguro", "ya tengo") or any(k in t for k in ("ya tengo", "ya cuento", "ya tengo seguro", "quiero comparar")):
            user_state[phone] = "auto_intro"
            send_message(phone, "Perfecto. Para prepararte una comparativa necesito:\n\n• *Año y modelo* de tu vehículo\n• *Número de placas*\n• *Aseguradora actual* (si recuerdas)\n\nMándame esos datos 👇")
        elif t in ("2", "primera vez", "primera contratación") or any(k in t for k in ("primera vez", "nunca he tenido", "no tengo seguro", "no tengo")):
            user_state[phone] = "auto_intro"
            send_message(phone, "Con gusto te orientamos. Para cotizarte el mejor plan necesito:\n\n• *Año y modelo* de tu vehículo\n• *Número de placas* (si ya los tienes)\n\nMándame esa información 👇")
        elif t in ("3", "vencer", "vencimiento", "por vencer") or any(k in t for k in ("por vencer", "se vence", "vence pronto", "vencimiento")):
            user_state[phone] = "auto_vencimiento_fecha"
            send_message(phone, "Bien hecho que lo piensas con tiempo. ¿Cuál es la *fecha de vencimiento* de tu póliza? (formato AAAA-MM-DD)\n\nChristian te contactará antes del vencimiento para que no quedes sin cobertura.")
        elif t in ("menu", "menú", "inicio", "salir", "cancelar"):
            user_state[phone] = "__greeted__"
            send_main_menu(phone)
        elif any(k in t for k in ("caro", "precio", "cuanto", "cuánto", "cuesta", "cobran", "vale")):
            _notify_advisor(f"💬 Objeción de precio en Auto\nWhatsApp: {phone}\nMensaje: {text}")
            user_state[phone] = "__greeted__"
            send_message(phone, "Entiendo tu preocupación. El precio depende del vehículo y cobertura — hay opciones desde cobertura básica hasta amplia.\n\nChristian te contactará para darte una cotización exacta sin compromiso. Escribe *menú* si necesitas otra cosa.")
        else:
            _notify_advisor(f"📩 Respuesta libre en flujo Auto\nWhatsApp: {phone}\nMensaje: {text}")
            send_message(phone, "Recibido. Para continuar con el seguro de auto elige:\n\n1) Tengo seguro y quiero comparar\n2) Primera vez contratando\n3) Mi póliza está por vencer\n\nO escribe *menú* para ver otras opciones.")
        return
    if st == "auto_intro":
        intent = interpret_response(text)
        if "vencimiento" in text.lower() or "vence" in text.lower() or "fecha" in text.lower():
            user_state[phone] = "auto_vencimiento_fecha"
            send_message(phone, "¿Cuál es la *fecha de vencimiento* de tu póliza actual? (formato AAAA-MM-DD)")
            return
        if intent == "negative":
            user_state[phone] = "auto_vencimiento_fecha"
            send_message(phone, "Entendido. Para poder recordarte a tiempo, ¿cuál es la *fecha de vencimiento* de tu póliza? (AAAA-MM-DD)")
            return
        send_message(phone, "Perfecto. Puedes empezar enviando los *documentos* o una *foto* de la tarjeta/placas.")
    elif st == "auto_vencimiento_fecha":
        try:
            fecha = datetime.fromisoformat(text.strip()).date()
            objetivo = fecha - timedelta(days=30)
            write_followup_to_sheets("auto_recordatorio", f"Recordatorio póliza -30d para {phone}", objetivo.isoformat())
            send_message(phone,
                f"✅ Gracias. Registré la fecha de vencimiento ({fecha.isoformat()}). "
                f"Christian te contactará antes del {objetivo.isoformat()} para que no quedes sin cobertura."
            )
            _notify_advisor(
                f"📅 Recordatorio de póliza registrado\nWhatsApp: {phone}\n"
                f"Vencimiento: {fecha.isoformat()}\nContactar antes de: {objetivo.isoformat()}"
            )
            user_state[phone] = "__greeted__"
            send_main_menu(phone)
        except Exception:
            send_message(phone, "Formato inválido. Usa AAAA-MM-DD. Ejemplo: 2025-12-31")



# ==========================
# Router helpers
# ==========================
def _greet_and_match(phone: str) -> Optional[Dict[str, Any]]:
    last10 = _normalize_phone_last10(phone)
    match = match_client_in_sheets(last10)
    base = "¿En qué te puedo orientar hoy? Escribe *menú* para ver las opciones disponibles."
    data = _ensure_user(phone)
    hint = (data.get("vicky_hint") or "").strip()
    nombre = (match.get("nombre") or "").strip() if match else ""
    if hint:
        saludo = f"Hola {nombre} 👋 Vi que te interesa {hint}. ¿En qué te puedo orientar?" if nombre else f"Hola 👋 Vi que te interesa {hint}. ¿En qué te puedo orientar?"
    else:
        saludo = f"Hola {nombre} 👋 {base}" if nombre else f"Hola 👋 {base}"
    send_message(phone, saludo)
    return match

def _route_command(phone: str, text: str, match: Optional[Dict[str, Any]]) -> None:
    t = text.strip().lower()
    st = user_state.get(phone, "")
    if st.startswith("tpv_"):
        _tpv_next(phone, text, match)
        return
    if _tpv_is_context(match):
        if tpv_start_from_reply(phone, text, match):
            return
    tlow = t.lower()
    imss_signals = ("imss", "ley 73", "pensionado", "jubilado", "pension", "pensión", "modalidad 40")
    if ("credito" in tlow or "crédito" in tlow or "prestamo" in tlow or "préstamo" in tlow) and not any(k in tlow for k in ("auto", "seguro auto", "seguro de auto", "póliza", "poliza", "placa")) and not any(k in tlow for k in imss_signals):
        send_message(phone, "¿Qué tipo de crédito buscas?\n1) Préstamo IMSS (Ley 73)\n5) Crédito Empresarial\n6) Financiamiento Práctico\n\nResponde *1*, *5* o *6*.")
        return
    imss_keywords = ("imss", "ley 73", "pensionado", "jubilado", "préstamo imss", "prestamo imss", "credito imss", "crédito imss")
    auto_keywords = ("seguro de auto", "seguro auto", "cotizar auto", "póliza auto", "poliza auto")
    if t in ("1", "imss", "ley 73", "préstamo", "prestamo", "pension", "pensión") or any(k in t for k in imss_keywords):
        imss_start(phone, match)
    elif t in ("2", "auto", "seguros de auto", "seguro auto") or any(k in t for k in auto_keywords):
        auto_start(phone, match)
    elif t in ("3", "vida", "salud", "seguro de vida", "seguro de salud"):
        send_message(phone, "🧬 *Seguros de Vida/Salud* — Gracias por tu interés. Notificaré al asesor para contactarte.")
        _notify_advisor(f"🔔 Vida/Salud — Solicitud de contacto\nWhatsApp: {phone}")
        send_main_menu(phone)
    elif t in ("4", "vrim", "tarjeta médica", "tarjeta medica"):
        send_message(phone, "🩺 *VRIM* — Membresía médica. Notificaré al asesor para darte detalles.")
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
    elif t in ("menu", "menú", "inicio"):
        user_state[phone] = "__greeted__"
        send_main_menu(phone)
    else:
        st = user_state.get(phone, "")
        if st.startswith("imss_"):
            _imss_next(phone, text)
        elif st.startswith("emp_"):
            _emp_next(phone, text)
        elif st.startswith("fp_"):
            _fp_next(phone, text)
        elif st.startswith("auto_"):
            _auto_next(phone, text)
        else:
            send_message(phone, "En breve, su asesor Christian López se pondrá en contacto con usted para brindarle asesoría personalizada y resolver todas sus dudas de manera directa y segura. Escribe *menú* para ver opciones.")

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
            log.info("✅ Webhook verificado exitosamente")
            return challenge, 200
    except Exception:
        log.exception("❌ Error en verificación webhook")
    log.warning("❌ Webhook verification failed")
    return "Error", 403

# ==========================
# Webhook — recepción
# ==========================
def _download_media(media_id: str) -> Tuple[Optional[bytes], Optional[str], Optional[str]]:
    if not META_TOKEN:
        return None, None, None
    try:
        meta = requests.get(f"https://graph.facebook.com/v20.0/{media_id}", headers={"Authorization": f"Bearer {META_TOKEN}"}, timeout=WPP_TIMEOUT)
        if meta.status_code != 200:
            return None, None, None
        meta_j = meta.json()
        url = meta_j.get("url")
        mime = meta_j.get("mime_type")
        fname = meta_j.get("filename") or f"media_{media_id}"
        if not url:
            return None, None, None
        binr = requests.get(url, headers={"Authorization": f"Bearer {META_TOKEN}"}, timeout=WPP_TIMEOUT)
        if binr.status_code != 200:
            return None, None, None
        return binr.content, mime, fname
    except Exception:
        log.exception("❌ Error descargando media")
        return None, None, None

def _handle_media(phone: str, msg: Dict[str, Any]) -> None:
    try:
        media_id = None
        if msg.get("type") == "image" and "image" in msg:
            media_id = msg["image"].get("id")
        elif msg.get("type") == "document" and "document" in msg:
            media_id = msg["document"].get("id")
        elif msg.get("type") == "audio" and "audio" in msg:
            media_id = msg["audio"].get("id")
        elif msg.get("type") == "video" and "video" in msg:
            media_id = msg["video"].get("id")
        if not media_id:
            send_message(phone, "Recibí tu archivo, gracias. (No se pudo identificar el contenido).")
            return
        forward_media_to_advisor(msg.get("type"), media_id)
        file_bytes, mime, fname = _download_media(media_id)
        if not file_bytes:
            send_message(phone, "Recibí tu archivo, pero hubo un problema procesándolo.")
            return
        last4 = _normalize_phone_last10(phone)[-4:]
        match = match_client_in_sheets(_normalize_phone_last10(phone))
        folder_name = f"{match['nombre'].replace(' ', '_')}_{last4}" if match and match.get("nombre") else f"Cliente_{last4}"
        link = upload_to_drive(fname, file_bytes, mime or "application/octet-stream", folder_name)
        _notify_advisor(f"🔔 Multimedia recibida\nDesde: {phone}\nArchivo: {fname}\nDrive: {link or '(sin link Drive)'}")
        send_message(phone, "✅ *Recibido y en proceso*. En breve te doy seguimiento.")
    except Exception:
        log.exception("❌ Error manejando multimedia")
        send_message(phone, "Recibí tu archivo, gracias. Si algo falla, lo reviso de inmediato.")

@app.post("/webhook")
def webhook_receive():
    try:
        intent_handled = False
        payload = request.get_json(force=True, silent=True) or {}
        log.info(f"📥 Webhook recibido: {json.dumps(payload, indent=2)[:500]}...")
        
        # Iterar todos los entries y changes del payload
        all_messages = []
        all_statuses = []
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                all_messages.extend(value.get("messages", []))
                all_statuses.extend(value.get("statuses", []))

        if not all_messages:
            for st in all_statuses:
                try:
                    if (st.get("status") or "").lower() == "failed":
                        log.warning("❌ STATUS failed (detalle): %s", json.dumps(st, ensure_ascii=False))
                except Exception:
                    pass
            log.info("ℹ️ Webhook sin mensajes (posible status update)")
            return jsonify({"ok": True}), 200

        if len(all_messages) > 1:
            log.info(f"ℹ️ Payload con {len(all_messages)} mensajes — procesando el primero (comportamiento estándar Meta)")

        msg = all_messages[0]
        phone = msg.get("from")
        if not phone:
            log.warning("⚠️ Mensaje sin número de teléfono")
            return jsonify({"ok": True}), 200

        log.info(f"📱 Mensaje de {phone}: {msg.get('type', 'unknown')}")
        last10 = _normalize_phone_last10(phone)
        match = match_client_in_sheets(last10)
        st_now = user_state.get(phone, "")
        idle = st_now in ("", "__greeted__")
        
        mtype = msg.get("type")
        if mtype == "text" and "text" in msg:
            text = msg["text"].get("body", "").strip()
            log.info(f"💬 Texto recibido de {phone}: {text}")

            # Registrar respuesta entrante en Sheets
            try:
                now_iso = datetime.utcnow().isoformat()
                nombre_sheet = (match.get("nombre", "").strip() if match else "")
                append_respuesta_cliente(phone, nombre_sheet, text, now_iso)
            except Exception:
                pass

            # --- Decision Layer (Boardroom Engine) ---
            try:
                decision = call_decision_layer(
                    telefono=phone,
                    mensaje=text,
                    nombre=nombre_sheet,
                )
                if isinstance(decision, dict):
                    route_to = decision.get("route_to", "")
                    existing_client = str(decision.get("existing_client", ""))
                    interest = decision.get("interest", "")
                    active_campaign = decision.get("active_campaign", "")
                    hint = decision.get("vicky_context_hint", "")
                    udata = user_data.setdefault(phone, {})
                    # Actualizar hint y bonus — limpiar si no vienen activos
                    udata["vicky_hint"] = hint if hint else ""
                    udata["campaign_bonus_eligible"] = bool(decision.get("campaign_bonus_eligible"))
                    if route_to == "VICKY_CAMPANAS" and existing_client != "true":
                        _notify_advisor(
                            "🧠 BOARDROOM — Lead enrutable a VICKY_CAMPANAS\n"
                            f"Teléfono: {phone}\n"
                            f"Interés: {interest or '-'}\n"
                            f"Campaña activa: {active_campaign or '-'}\n"
                            f"Hint: {hint or '-'}"
                        )
                        udata["boardroom_notified"] = True
            except Exception as e:
                logging.exception("[decision-layer] integración no bloqueante: %s", e)
            # --- Fin Decision Layer ---

            # Interceptor ultra-prioritario: respuesta "info" a plantilla
            t_norm_info = (text or "").strip().lower()
            if t_norm_info in ("info", "informacion", "información", "mas info", "más info"):
                last_tpl = ""
                st = user_state.get(phone, "")
                if st.startswith("awaiting_info:"):
                    last_tpl = st.split(":", 1)[1].strip()
                if not last_tpl:
                    last_tpl = get_last_envio_template(last10)
                if last_tpl in ("tpv_3", "promo_tpv", TPV_TEMPLATE_NAME):
                    user_state[phone] = "tpv_giro"
                    try:
                        _notify_advisor(
                            "🧾 Respuesta a plantilla (TPV)\n"
                            f"Template: {last_tpl}\n"
                            f"WhatsApp: {phone}\n"
                            f"Nombre: {(match.get('nombre', '') if match else '') or '(sin nombre)'}\n"
                            f"Mensaje: {text}"
                        )
                    except Exception:
                        pass
                    send_message(phone, "✅ Perfecto. Para recomendarte la mejor terminal Inbursa, dime: ¿*a qué giro* pertenece tu negocio?")
                    return jsonify({"ok": True}), 200

            # Interceptores post-campaña
            if idle and match:
                if _auto_is_context(match) and _explicit_non_auto_intent(text):
                    log.info(f"🔀 Escape de flujo AUTO por intención explícita: {text}")
                else:
                    if _alianza_is_context(match):
                        if _handle_alianza_context_response(phone, text, match):
                            intent_handled = True
                    if intent_handled:
                        return jsonify({"ok": True}), 200
                    if _auto_is_context(match):
                        if _handle_auto_context_response(phone, text, match):
                            intent_handled = True
                    if intent_handled:
                        return jsonify({"ok": True}), 200
                if _tpv_is_context(match):
                    if tpv_start_from_reply(phone, text, match):
                        intent_handled = True
                if intent_handled:
                    return jsonify({"ok": True}), 200

            if idle:
                t_norm = (text or "").strip().lower()
                GREET_WORDS = {"hola", "buenas", "buenos dias", "buenos días", "buen dia", "buen día", "buenas tardes", "buenas noches", "hey", "que tal", "qué tal", "holi"}
                if t_norm in GREET_WORDS:
                    user_state[phone] = "__greeted__"
                    _greet_and_match(phone)
                    return jsonify({"ok": True}), 200
                TPV_KEYWORDS = ("tpv", "terminal", "terminales", "punto de venta", "punto-de-venta", "cobrar con tarjeta", "cobro con tarjeta", "pagar con tarjeta", "ligas de pago", "link de pago", "link pago", "cobro a distancia")
                if any(k in t_norm for k in TPV_KEYWORDS):
                    user_state[phone] = "tpv_giro"
                    nombre = (match.get("nombre", "").strip() if match else "")
                    _notify_advisor(f"🧠 Interés detectado (TPV)\nWhatsApp: {phone}\nNombre: {nombre or '(sin nombre)'}\nMensaje: {text}")
                    send_message(phone, "✅ Perfecto. Para recomendarte la mejor terminal Inbursa, dime: ¿*a qué giro* pertenece tu negocio?")
                    return jsonify({"ok": True}), 200

            if idle and interpret_response(text) == "negative":
                send_message(phone, "Gracias por tu respuesta. Quedo a tus órdenes para cualquier duda o si más adelante deseas revisarlo.")
                user_state[phone] = "__greeted__"
                send_main_menu(phone)
                return jsonify({"ok": True}), 200

            t_lower = text.lower().strip()
            VALID_COMMANDS = {"1","2","3","4","5","6","7","menu","menú","inicio","hola","imss","ley 73","prestamo","préstamo","pension","pensión","auto","seguro auto","seguros de auto","vida","salud","seguro de vida","seguro de salud","vrim","tarjeta medica","tarjeta médica","empresarial","pyme","credito","crédito","credito empresarial","crédito empresarial","financiamiento","financiamiento practico","financiamiento práctico","contactar","asesor","contactar con christian"}
            if not t_lower.isdigit() and t_lower not in VALID_COMMANDS and idle:
                if not user_data.get(phone, {}).get("boardroom_notified", False):
                    _notify_advisor(f"📩 Cliente INTERESADO / DUDA detectada\nWhatsApp: {phone}\nMensaje: {text}")
                # No resetear boardroom_notified aquí — se limpia en el siguiente ciclo de Boardroom

            if phone not in user_state:
                user_state[phone] = "__greeted__"
                # Solo saludar si el mensaje no contiene intención procesable
                _t = text.strip().lower()
                _tiene_intencion = any(k in _t for k in (
                    "imss", "préstamo", "prestamo", "ley 73", "pensión", "pension",
                    "auto", "seguro", "póliza", "poliza", "placa",
                    "vida", "salud", "vrim", "tarjeta medica",
                    "empresarial", "pyme", "crédito", "credito",
                    "financiamiento", "tpv", "terminal", "nómina", "nomina",
                ))
                if not _tiene_intencion:
                    _greet_and_match(phone)
                    return jsonify({"ok": True}), 200
                # Si hay intención: saludar brevemente y continuar al router
                _greet_and_match(phone)

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
            _handle_media(phone, msg)
            return jsonify({"ok": True}), 200

        log.info(f"ℹ️ Tipo de mensaje no manejado: {mtype}")
        return jsonify({"ok": True}), 200
    except Exception:
        log.exception("❌ Error en webhook_receive")
        return jsonify({"ok": True}), 200

# ==========================
# Endpoints auxiliares
# ==========================
@app.get("/health")
def health():
    return jsonify({"status": "ok", "service": "Vicky Bot Inbursa", "timestamp": datetime.utcnow().isoformat()}), 200

@app.get("/ext/health")
def ext_health():
    return jsonify({"status": "ok", "whatsapp_configured": bool(META_TOKEN and WABA_PHONE_ID), "google_ready": google_ready, "openai_ready": bool(openai and OPENAI_API_KEY)}), 200

@app.post("/ext/test-send")
def ext_test_send():
    try:
        data = request.get_json(force=True) or {}
        to = str(data.get("to", "")).strip()
        text = str(data.get("text", "")).strip()
        if not to or not text:
            return jsonify({"ok": False, "error": "Faltan parámetros 'to' o 'text'"}), 400
        ok = send_message(to, text)
        return jsonify({"ok": bool(ok)}), 200
    except Exception as e:
        log.exception("❌ Error en /ext/test-send")
        return jsonify({"ok": False, "error": str(e)}), 500

def _bulk_send_worker(items: List[Dict[str, Any]]) -> None:
    successful = 0
    failed = 0
    log.info(f"🚀 Iniciando envío masivo de {len(items)} mensajes")
    for i, item in enumerate(items, 1):
        try:
            to = item.get("to", "").strip()
            text = item.get("text", "").strip()
            template = item.get("template", "").strip()
            params = item.get("params", [])
            if not to:
                failed += 1
                continue
            success = False
            if template:
                success = send_template_message(to, template, params)
            elif text:
                success = send_message(to, text)
            else:
                failed += 1
                continue
            if success:
                successful += 1
            else:
                failed += 1
            time.sleep(0.5)
        except Exception:
            failed += 1
            log.exception(f"❌ Error procesando item {i}")
    log.info(f"🎯 Envío masivo completado: {successful} ✅, {failed} ❌")
    if ADVISOR_NUMBER:
        send_message(ADVISOR_NUMBER, f"📊 Resumen envío masivo:\n• Exitosos: {successful}\n• Fallidos: {failed}\n• Total: {len(items)}")

@app.post("/ext/send-promo")
def ext_send_promo():
    try:
        if not META_TOKEN or not WABA_PHONE_ID:
            return jsonify({"queued": False, "error": "WhatsApp Business API no configurada"}), 500
        body = request.get_json(force=True) or {}
        items = body.get("items", [])
        if not isinstance(items, list) or not items:
            return jsonify({"queued": False, "error": "Lista 'items' inválida o vacía"}), 400
        valid_items = [i for i in items if isinstance(i, dict) and i.get("to", "").strip() and (i.get("text", "").strip() or i.get("template", "").strip())]
        if not valid_items:
            return jsonify({"queued": False, "error": "No hay items válidos para enviar"}), 400
        threading.Thread(target=_bulk_send_worker, args=(valid_items,), daemon=True, name="BulkSendWorker").start()
        return jsonify({"queued": True, "message": f"Procesando {len(valid_items)} mensajes en background", "total_received": len(items), "valid_items": len(valid_items), "timestamp": datetime.utcnow().isoformat()}), 202
    except Exception as e:
        log.exception("❌ Error crítico en /ext/send-promo")
        return jsonify({"queued": False, "error": f"Error interno: {str(e)}"}), 500

@app.get("/ext/ping-advisor")
def ext_ping_advisor():
    try:
        token = (request.args.get("token") or "").strip()
        if not AUTO_SEND_TOKEN or token != AUTO_SEND_TOKEN:
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        if not ADVISOR_NUMBER:
            return jsonify({"ok": False, "error": "ADVISOR_NUMBER no configurado"}), 500
        ok = send_message(ADVISOR_NUMBER, "🤖 Vicky SECOM activa. Este mensaje mantiene la ventana de notificaciones abierta.")
        return jsonify({"ok": bool(ok), "to": ADVISOR_NUMBER}), 200
    except Exception as e:
        log.exception("❌ Error en /ext/ping-advisor")
        return jsonify({"ok": False, "error": str(e)}), 500

# ==========================
# AUTO SEND
# ==========================
AUTO_SEND_TOKEN = os.getenv("AUTO_SEND_TOKEN", "").strip()

def _sheet_get_rows() -> Tuple[List[str], List[List[str]]]:
    if not (google_ready and sheets_svc and SHEETS_ID_LEADS and SHEETS_TITLE_LEADS):
        raise RuntimeError("Sheets no disponible.")
    rng = f"{SHEETS_TITLE_LEADS}!A:Z"
    values = sheets_svc.spreadsheets().values().get(spreadsheetId=SHEETS_ID_LEADS, range=rng).execute()
    rows = values.get("values", [])
    if not rows:
        return [], []
    headers = [h.strip() for h in rows[0]]
    return headers, rows[1:]

def _idx(headers: List[str], name: str) -> Optional[int]:
    n = name.strip().lower()
    for i, h in enumerate(headers):
        if (h or "").strip().lower() == n:
            return i
    return None

def _cell(row: List[str], i: Optional[int]) -> str:
    if i is None:
        return ""
    return (row[i] if i < len(row) else "") or ""

def _normalize_to_e164_mx(phone_raw: str) -> str:
    digits = re.sub(r"\D", "", phone_raw or "")
    last10 = _normalize_phone_last10(digits)
    if len(last10) == 10:
        return f"521{last10}"
    if digits.startswith("52") and len(digits) == 12:
        return f"521{digits[2:]}"
    if digits.startswith("521") and len(digits) == 13:
        return digits
    return digits

def _update_row_cells(row_number_1based: int, updates: Dict[str, str], headers: List[str]) -> None:
    if not (google_ready and sheets_svc and SHEETS_ID_LEADS and SHEETS_TITLE_LEADS):
        raise RuntimeError("Sheets no disponible para update.")
    data = []
    for col_name, value in updates.items():
        j = _idx(headers, col_name)
        if j is None:
            raise RuntimeError(f"No existe columna '{col_name}' en el Sheet.")
        col_letter = chr(ord("A") + j)
        a1 = f"{SHEETS_TITLE_LEADS}!{col_letter}{row_number_1based}"
        data.append({"range": a1, "values": [[value]]})
    body = {"valueInputOption": "USER_ENTERED", "data": data}
    sheets_svc.spreadsheets().values().batchUpdate(spreadsheetId=SHEETS_ID_LEADS, body=body).execute()

def _pick_next_pending(headers: List[str], rows: List[List[str]]) -> Optional[Dict[str, Any]]:
    i_name = _idx(headers, "Nombre")
    i_wa = _idx(headers, "WhatsApp")
    i_status = _idx(headers, "ESTATUS")
    i_last = _idx(headers, "LAST_MESSAGE_AT")
    if i_name is None or i_wa is None:
        raise RuntimeError("Faltan columnas requeridas: 'Nombre' y/o 'WhatsApp'.")
    for k, row in enumerate(rows, start=2):
        nombre = _cell(row, i_name).strip()
        wa = _cell(row, i_wa).strip()
        estatus = _cell(row, i_status).strip().upper() if i_status is not None else ""
        last_at = _cell(row, i_last).strip() if i_last is not None else ""
        if not wa:
            continue
        if last_at and estatus != "FALLO_ENVIO":
            continue
        if estatus and estatus not in ("PENDIENTE", "FALLO_ENVIO"):
            continue
        return {"row_number": k, "nombre": nombre, "whatsapp": wa}
    return None

@app.post("/ext/auto-send-one")
def ext_auto_send_one():
    try:
        token = (request.headers.get("X-AUTO-TOKEN") or "").strip()
        if not AUTO_SEND_TOKEN or token != AUTO_SEND_TOKEN:
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        body = request.get_json(force=True, silent=True) or {}
        template_name = str(body.get("template", "")).strip()
        if not template_name:
            return jsonify({"ok": False, "error": "Falta 'template'"}), 400
        headers, rows = _sheet_get_rows()
        if not headers:
            return jsonify({"ok": False, "error": "Sheet vacío"}), 400
        nxt = _pick_next_pending(headers, rows)
        if not nxt:
            return jsonify({"ok": True, "sent": False, "reason": "no_pending"}), 200
        to = _normalize_to_e164_mx(nxt["whatsapp"])
        nombre = (nxt["nombre"] or "").strip() or "Cliente"
        ok = send_template_message(to, template_name, ({} if template_name == "vrim_ideal" else {"nombre": nombre}))
        if ok:
            user_state[to] = f"awaiting_info:{template_name}"
        else:
            try:
                append_envio_status(to, "", "failed", template_name, datetime.utcnow().isoformat())
            except Exception:
                pass
        now_iso = datetime.utcnow().isoformat()
        estatus_val = "FALLO_ENVIO" if not ok else ("ENVIADO_TPV" if template_name == TPV_TEMPLATE_NAME else ("ENVIADO_ALIANZA" if template_name in ALLIANCE_TEMPLATES else "ENVIADO_INICIAL"))
        _update_row_cells(nxt["row_number"], {"ESTATUS": estatus_val, "LAST_MESSAGE_AT": now_iso}, headers)
        return jsonify({"ok": True, "sent": bool(ok), "to": to, "row": nxt["row_number"], "nombre": nombre, "timestamp": now_iso}), 200
    except Exception as e:
        log.exception("❌ Error en /ext/auto-send-one")
        return jsonify({"ok": False, "error": str(e)}), 500

if __name__ == "__main__":
    log.info(f"🚀 Iniciando Vicky Bot SECOM en puerto {PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False)
    
