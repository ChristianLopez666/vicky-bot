# app.py — Vicky SECOM (WAPI + Embudos + AutoSend) — Versión depurada y funcional
# Python 3.11+
# ---------------------------------------------------------------------------------
# OBJETIVO (según bitácora):
# - Mantener menú y embudos de venta (IMSS, Auto, Vida/Salud, VRIM, Empresarial, FP, Contacto).
# - Operar como “sistema WAPI” para envío de plantillas autorizadas por Meta.
# - Mantener endpoints: /webhook (GET verify + POST receive), /ext/health, /ext/test-send,
#   /ext/send-promo, /ext/auto-send-one, /health.
# - Integración Google Sheets + Drive (degradable).
# - Depuración: eliminar redundancias y corregir fallos reales (greeting/matching, orden de carga).
# ---------------------------------------------------------------------------------

from __future__ import annotations

import io
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple, Union

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request

# Google (degradable)
try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseUpload
except Exception:
    service_account = None
    build = None
    MediaIoBaseUpload = None

# OpenAI (opcional, degradable)
try:
    import openai  # type: ignore
except Exception:
    openai = None

# =============================================================================
# Carga entorno + logging
# =============================================================================
load_dotenv()

META_TOKEN = (os.getenv("META_TOKEN") or "").strip()
WABA_PHONE_ID = (os.getenv("WABA_PHONE_ID") or "").strip()
VERIFY_TOKEN = (os.getenv("VERIFY_TOKEN") or "").strip()
ADVISOR_NUMBER = (os.getenv("ADVISOR_NUMBER") or "5216682478005").strip()
OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()

GOOGLE_CREDENTIALS_JSON = (os.getenv("GOOGLE_CREDENTIALS_JSON") or "").strip()
SHEETS_ID_LEADS = (os.getenv("SHEETS_ID_LEADS") or "").strip()
SHEETS_TITLE_LEADS = (os.getenv("SHEETS_TITLE_LEADS") or "Prospectos SECOM Auto").strip()
DRIVE_PARENT_FOLDER_ID = (os.getenv("DRIVE_PARENT_FOLDER_ID") or "").strip()

AUTO_SEND_TOKEN = (os.getenv("AUTO_SEND_TOKEN") or "").strip()

META_API_VERSION = (os.getenv("META_API_VERSION") or "v24.0").strip()
PORT = int(os.getenv("PORT", "5000"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("vicky-secom")

# =============================================================================
# Flask + estado en memoria
# =============================================================================
app = Flask(__name__)

# Estado conversacional (por WhatsApp)
user_state: Dict[str, str] = {}
user_data: Dict[str, Dict[str, Any]] = {}

# Control de saludo (24h)
greeted_at: Dict[str, datetime] = {}
GREET_WINDOW_HOURS = 24

# =============================================================================
# Google Setup (degradable)
# =============================================================================
creds = None
sheets_svc = None
drive_svc = None
google_ready = False

if GOOGLE_CREDENTIALS_JSON and service_account and build:
    try:
        info = json.loads(GOOGLE_CREDENTIALS_JSON)
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
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

# =============================================================================
# WhatsApp (Meta Cloud API)
# =============================================================================
WPP_TIMEOUT = 15
WPP_API_URL = (
    f"https://graph.facebook.com/{META_API_VERSION}/{WABA_PHONE_ID}/messages"
    if WABA_PHONE_ID
    else ""
)

def _wpp_headers() -> Dict[str, str]:
    return {"Authorization": f"Bearer {META_TOKEN}", "Content-Type": "application/json"}

def _should_retry(status: int) -> bool:
    return status == 429 or 500 <= status < 600

def _backoff(attempt: int) -> None:
    time.sleep(2 ** attempt)

def _normalize_phone_last10(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    return digits[-10:] if len(digits) >= 10 else digits

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

def send_message(to: str, text: str) -> bool:
    """Mensaje de texto con reintentos en 429/5xx + timeouts."""
    if not (META_TOKEN and WPP_API_URL):
        log.error("❌ WhatsApp no configurado (META_TOKEN/WABA_PHONE_ID).")
        return False

    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": (text or "")[:4096]},
    }

    for attempt in range(3):
        try:
            resp = requests.post(WPP_API_URL, headers=_wpp_headers(), json=payload, timeout=WPP_TIMEOUT)
            if resp.status_code == 200:
                return True
            log.warning("⚠️ send_message %s: %s", resp.status_code, resp.text[:250])
            if _should_retry(resp.status_code) and attempt < 2:
                _backoff(attempt)
                continue
            return False
        except requests.exceptions.Timeout:
            log.warning("⏰ Timeout send_message a %s (intento %s)", to, attempt + 1)
            if attempt < 2:
                _backoff(attempt)
                continue
            return False
        except Exception:
            log.exception("❌ Error send_message a %s", to)
            if attempt < 2:
                _backoff(attempt)
                continue
            return False
    return False

def send_template_message(to: str, template_name: str, params: Union[Dict[str, Any], List[Any]]) -> bool:
    """Plantillas (Meta approved). Incluye header imagen solo para seguro_auto_70."""
    if not (META_TOKEN and WPP_API_URL):
        log.error("❌ WhatsApp no configurado para templates.")
        return False

    components: List[Dict[str, Any]] = []

    # HEADER con imagen SOLO para seguro_auto_70 (según tu implementación previa)
    if template_name == "seguro_auto_70":
        components.append(
            {
                "type": "header",
                "parameters": [{"type": "image", "image": {"id": "884297197421583"}}],
            }
        )

    # BODY
    if isinstance(params, dict):
        components.append(
            {
                "type": "body",
                "parameters": [{"type": "text", "text": str(v)} for _, v in params.items()],
            }
        )
    elif isinstance(params, list):
        components.append(
            {
                "type": "body",
                "parameters": [{"type": "text", "text": str(x)} for x in params],
            }
        )

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
            resp = requests.post(WPP_API_URL, headers=_wpp_headers(), json=payload, timeout=WPP_TIMEOUT)
            if resp.status_code == 200:
                return True
            log.warning("⚠️ send_template_message %s: %s", resp.status_code, resp.text[:250])
            if _should_retry(resp.status_code) and attempt < 2:
                _backoff(attempt)
                continue
            return False
        except requests.exceptions.Timeout:
            log.warning("⏰ Timeout send_template_message a %s (intento %s)", to, attempt + 1)
            if attempt < 2:
                _backoff(attempt)
                continue
            return False
        except Exception:
            log.exception("❌ Error send_template_message a %s", to)
            if attempt < 2:
                _backoff(attempt)
                continue
            return False
    return False

# =============================================================================
# Utilidades de interpretación
# =============================================================================
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

def _notify_advisor(text: str) -> None:
    if not ADVISOR_NUMBER:
        return
    try:
        send_message(ADVISOR_NUMBER, text)
    except Exception:
        log.exception("❌ Error notificando al asesor")

# =============================================================================
# Google Sheets helpers (usados por matching y autosend)
# =============================================================================
def _sheet_get_rows() -> Tuple[List[str], List[List[str]]]:
    if not (google_ready and sheets_svc and SHEETS_ID_LEADS and SHEETS_TITLE_LEADS):
        raise RuntimeError("Sheets no disponible (google_ready/SHEETS_ID_LEADS/SHEETS_TITLE_LEADS).")
    rng = f"{SHEETS_TITLE_LEADS}!A:Z"
    values = sheets_svc.spreadsheets().values().get(spreadsheetId=SHEETS_ID_LEADS, range=rng).execute()
    rows = values.get("values", [])
    if not rows:
        return [], []
    headers = [str(h).strip() for h in rows[0]]
    data_rows = rows[1:]
    return headers, data_rows

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

def match_client_in_sheets(phone_last10: str) -> Optional[Dict[str, Any]]:
    if not (google_ready and sheets_svc and SHEETS_ID_LEADS and SHEETS_TITLE_LEADS):
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
            return None

        target = str(phone_last10).strip()
        for k, row in enumerate(rows, start=2):
            wa_cell = _cell(row, i_wa)
            wa_last10 = _normalize_phone_last10(wa_cell)
            if target and wa_last10 == target:
                nombre = _cell(row, i_name).strip() if i_name is not None else ""
                estatus = _cell(row, i_status).strip() if i_status is not None else ""
                last_at = _cell(row, i_last).strip() if i_last is not None else ""
                return {"row": k, "nombre": nombre, "estatus": estatus, "last_message_at": last_at, "raw": row}

        return None
    except Exception:
        log.exception("❌ Error buscando en Sheets")
        return None

def write_followup_to_sheets(row: Union[int, str], note: str, date_iso: str) -> None:
    if not (google_ready and sheets_svc and SHEETS_ID_LEADS):
        return
    try:
        title = "Seguimiento"
        body = {"values": [[str(row), date_iso, note]]}
        sheets_svc.spreadsheets().values().append(
            spreadsheetId=SHEETS_ID_LEADS,
            range=f"{title}!A:C",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body=body,
        ).execute()
    except Exception:
        log.exception("❌ Error escribiendo seguimiento en Sheets")

# =============================================================================
# Google Drive helpers (multimedia)
# =============================================================================
def _find_or_create_client_folder(folder_name: str) -> Optional[str]:
    if not (google_ready and drive_svc and DRIVE_PARENT_FOLDER_ID):
        return None
    try:
        q = (
            f"name = '{folder_name}' and mimeType = 'application/vnd.google-apps.folder' "
            f"and '{DRIVE_PARENT_FOLDER_ID}' in parents and trashed = false"
        )
        resp = drive_svc.files().list(q=q, fields="files(id, name)").execute()
        items = resp.get("files", [])
        if items:
            return items[0]["id"]

        meta = {"name": folder_name, "mimeType": "application/vnd.google-apps.folder", "parents": [DRIVE_PARENT_FOLDER_ID]}
        created = drive_svc.files().create(body=meta, fields="id").execute()
        return created.get("id")
    except Exception:
        log.exception("❌ Error creando/buscando carpeta en Drive")
        return None

def upload_to_drive(file_name: str, file_bytes: bytes, mime_type: str, folder_name: str) -> Optional[str]:
    if not (google_ready and drive_svc and MediaIoBaseUpload):
        return None
    try:
        folder_id = _find_or_create_client_folder(folder_name)
        if not folder_id:
            return None
        media = MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype=mime_type, resumable=False)
        meta = {"name": file_name, "parents": [folder_id]}
        created = drive_svc.files().create(body=meta, media_body=media, fields="id,webViewLink").execute()
        return created.get("webViewLink") or created.get("id")
    except Exception:
        log.exception("❌ Error subiendo archivo a Drive")
        return None

# =============================================================================
# Menú principal
# =============================================================================
MAIN_MENU = (
    "🟦 *Vicky Bot — Inbursa*\n"
    "Elige una opción:\n"
    "1) Préstamo IMSS (Ley 73)\n"
    "2) Seguro de Auto (cotización)\n"
    "3) Seguros de Vida / Salud\n"
    "4) Tarjeta médica VRIM\n"
    "5) Crédito Empresarial\n"
    "6) Financiamiento Práctico\n"
    "7) Contactar con Christian\n"
    "\nEscribe el número u opción (ej. 'imss', 'auto', 'empresarial', 'contactar')."
)

def send_main_menu(phone: str) -> None:
    send_message(phone, MAIN_MENU)

# =============================================================================
# Embudos (conservados)
# =============================================================================

# --- TPV / Terminal bancaria ---
TPV_TEMPLATE_NAME = "promo_tpv"

def _parse_dt_maybe(value: str) -> Optional[datetime]:
    if not value:
        return None
    v = value.strip()
    try:
        if v.endswith("Z"):
            v = v[:-1] + "+00:00"
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
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
    now = datetime.now(timezone.utc)
    dt_utc = dt.astimezone(timezone.utc)
    return (now - dt_utc) <= timedelta(hours=24)

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
    nombre = (match.get("nombre") or "").strip() if match else ""

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

        send_message(
            phone,
            "✅ Listo. En breve Christian te contactará para ofrecerte la mejor opción de terminal.\n"
            f"- Giro: {data.get('tpv_giro','')}\n"
            f"- Horario: {data.get('tpv_horario','')}",
        )

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

        user_state[phone] = ""
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

        user_state[phone] = ""
        return

# --- IMSS (opción 1) ---
def imss_start(phone: str, match: Optional[Dict[str, Any]]) -> None:
    user_state[phone] = "imss_beneficios"
    send_message(phone, "🟩 *Préstamo IMSS Ley 73*\nBeneficios clave: trámite rápido, sin aval, pagos fijos y atención personalizada. ¿Te interesa conocer requisitos? (responde *sí* o *no*)")

def _imss_next(phone: str, text: str) -> None:
    st = user_state.get(phone, "")
    data = _ensure_user(phone)

    if st == "imss_beneficios":
        if interpret_response(text) == "positive":
            user_state[phone] = "imss_pension"
            send_message(phone, "¿Cuál es tu *pensión mensual* aproximada? (ej. $8,500)")
        else:
            send_message(phone, "Sin problema. Si deseas volver al menú, escribe *menú*.")
        return

    if st == "imss_pension":
        pension = extract_number(text)
        if not pension:
            send_message(phone, "No pude leer el monto. Indica tu *pensión mensual* (ej. 8500).")
            return
        data["imss_pension"] = pension
        user_state[phone] = "imss_monto"
        send_message(phone, "Gracias. ¿Qué *monto* te gustaría solicitar? (mínimo $40,000)")
        return

    if st == "imss_monto":
        monto = extract_number(text)
        if not monto:
            send_message(phone, "Escribe un *monto* (ej. 100000).")
            return
        data["imss_monto"] = monto
        user_state[phone] = "imss_nombre"
        send_message(phone, "Perfecto. ¿Cuál es tu *nombre completo*?")
        return

    if st == "imss_nombre":
        data["imss_nombre"] = (text or "").strip()
        user_state[phone] = "imss_ciudad"
        send_message(phone, "¿En qué *ciudad* te encuentras?")
        return

    if st == "imss_ciudad":
        data["imss_ciudad"] = (text or "").strip()
        user_state[phone] = "imss_nomina"
        send_message(phone, "¿Tienes *nómina Inbursa* actualmente? (sí/no)\n*Nota:* No es obligatoria; si la tienes, accedes a *beneficios adicionales*.")
        return

    if st == "imss_nomina":
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
        _notify_advisor("🔔 IMSS — Prospecto preautorizado\nWhatsApp: " + phone + "\n" + msg)

        user_state[phone] = ""
        send_main_menu(phone)
        return

# --- Crédito Empresarial (opción 5) ---
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
        return

    if st == "emp_giro":
        data["emp_giro"] = (text or "").strip()
        user_state[phone] = "emp_monto"
        send_message(phone, "¿Qué *monto* deseas? (mínimo $100,000)")
        return

    if st == "emp_monto":
        monto = extract_number(text)
        if not monto or monto < 100000:
            send_message(phone, "El monto mínimo es $100,000. Indica un monto igual o mayor.")
            return
        data["emp_monto"] = monto
        user_state[phone] = "emp_nombre"
        send_message(phone, "¿Tu *nombre completo*?")
        return

    if st == "emp_nombre":
        data["emp_nombre"] = (text or "").strip()
        user_state[phone] = "emp_ciudad"
        send_message(phone, "¿Tu *ciudad*?")
        return

    if st == "emp_ciudad":
        data["emp_ciudad"] = (text or "").strip()
        resumen = (
            "✅ Gracias. Un asesor te contactará.\n"
            f"- Nombre: {data.get('emp_nombre','')}\n"
            f"- Ciudad: {data.get('emp_ciudad','')}\n"
            f"- Giro: {data.get('emp_giro','')}\n"
            f"- Monto: ${data.get('emp_monto',0):,.0f}\n"
        )
        send_message(phone, resumen)
        _notify_advisor("🔔 Empresarial — Nueva solicitud\nWhatsApp: " + phone + "\n" + resumen)

        user_state[phone] = ""
        send_main_menu(phone)
        return

# --- Financiamiento Práctico (opción 6) ---
FP_QUESTIONS = [f"Pregunta {i}" for i in range(1, 12)]

def fp_start(phone: str, match: Optional[Dict[str, Any]]) -> None:
    user_state[phone] = "fp_q1"
    _ensure_user(phone)["fp_answers"] = {}
    send_message(phone, "🟩 *Financiamiento Práctico*\nResponderemos 11 preguntas rápidas.\n1) " + FP_QUESTIONS[0])

def _fp_next(phone: str, text: str) -> None:
    st = user_state.get(phone, "")
    data = _ensure_user(phone)

    if st.startswith("fp_q"):
        try:
            idx = int(st.split("_q")[1]) - 1
        except Exception:
            idx = 0

        data["fp_answers"][f"q{idx+1}"] = (text or "").strip()

        if idx + 1 < len(FP_QUESTIONS):
            user_state[phone] = f"fp_q{idx+2}"
            send_message(phone, f"{idx+2}) {FP_QUESTIONS[idx+1]}")
            return

        user_state[phone] = "fp_comentario"
        send_message(phone, "¿Algún *comentario adicional*?")
        return

    if st == "fp_comentario":
        data["fp_comentario"] = (text or "").strip()
        resumen = "✅ Gracias. Un asesor te contactará.\n" + "\n".join(
            f"{k.upper()}: {v}" for k, v in data.get("fp_answers", {}).items()
        )
        if data.get("fp_comentario"):
            resumen += f"\nCOMENTARIO: {data['fp_comentario']}"
        send_message(phone, resumen)
        _notify_advisor(f"🔔 Financiamiento Práctico — Resumen\nWhatsApp: {phone}\n{resumen}")
        user_state[phone] = ""
        send_main_menu(phone)
        return

# --- Seguros de Auto (opción 2) ---
def auto_start(phone: str, match: Optional[Dict[str, Any]]) -> None:
    user_state[phone] = "auto_intro"
    send_message(
        phone,
        "🚗 *Seguro de Auto*\nEnvíame por favor:\n• INE (frente)\n• Tarjeta de circulación *o* número de placas\n\nCuando lo envíes, te confirmaré recepción y procesaré la cotización.",
    )

def _retry_after_days(phone: str, days: int) -> None:
    try:
        time.sleep(days * 24 * 60 * 60)
        send_message(phone, "⏰ Seguimos a tus órdenes. ¿Deseas que coticemos tu seguro de auto cuando se acerque el vencimiento?")
        write_followup_to_sheets("auto_reintento", f"Reintento +{days}d enviado a {phone}", datetime.utcnow().isoformat())
    except Exception:
        log.exception("Error en reintento programado")

def _auto_next(phone: str, text: str) -> None:
    st = user_state.get(phone, "")

    if st == "auto_intro":
        lower = (text or "").lower()
        intent = interpret_response(text)

        if "vencimiento" in lower or "vence" in lower or "fecha" in lower or intent == "negative":
            user_state[phone] = "auto_vencimiento_fecha"
            send_message(phone, "¿Cuál es la *fecha de vencimiento* de tu póliza actual? (formato AAAA-MM-DD)")
            return

        send_message(phone, "Perfecto. Puedes empezar enviando los *documentos* o una *foto* de la tarjeta/placas.")
        return

    if st == "auto_vencimiento_fecha":
        try:
            fecha = datetime.fromisoformat((text or "").strip()).date()
            objetivo = fecha - timedelta(days=30)
            write_followup_to_sheets("auto_recordatorio", f"Recordatorio póliza -30d para {phone}", objetivo.isoformat())
            threading.Thread(target=_retry_after_days, args=(phone, 7), daemon=True).start()
            send_message(phone, f"✅ Gracias. Te contactaré *un mes antes* ({objetivo.isoformat()}).")
            user_state[phone] = ""
            send_main_menu(phone)
        except Exception:
            send_message(phone, "Formato inválido. Usa AAAA-MM-DD. Ejemplo: 2025-12-31")
        return

# =============================================================================
# Router principal (menú + embudos)
# =============================================================================
def _route_command(phone: str, text: str, match: Optional[Dict[str, Any]]) -> None:
    t = (text or "").strip().lower()
    st = user_state.get(phone, "")

    # TPV prioridad si está en contexto por plantilla promo_tpv
    if st.startswith("tpv_"):
        _tpv_next(phone, text, match)
        return
    if _tpv_is_context(match):
        if tpv_start_from_reply(phone, text, match):
            return

    if t in ("1", "imss", "ley 73", "préstamo", "prestamo", "pension", "pensión"):
        imss_start(phone, match)
        return

    if t in ("2", "auto", "seguros de auto", "seguro auto"):
        auto_start(phone, match)
        return

    if t in ("3", "vida", "salud", "seguro de vida", "seguro de salud"):
        send_message(phone, "🧬 *Seguros de Vida/Salud* — Gracias por tu interés. Notificaré al asesor para contactarte.")
        _notify_advisor(f"🔔 Vida/Salud — Solicitud de contacto\nWhatsApp: {phone}")
        send_main_menu(phone)
        return

    if t in ("4", "vrim", "tarjeta médica", "tarjeta medica"):
        send_message(phone, "🩺 *VRIM* — Membresía médica. Notificaré al asesor para darte detalles.")
        _notify_advisor(f"🔔 VRIM — Solicitud de contacto\nWhatsApp: {phone}")
        send_main_menu(phone)
        return

    if t in ("5", "empresarial", "pyme", "crédito empresarial", "credito empresarial"):
        emp_start(phone, match)
        return

    if t in ("6", "financiamiento práctico", "financiamiento practico", "crédito simple", "credito simple"):
        fp_start(phone, match)
        return

    if t in ("7", "contactar", "asesor", "contactar con christian"):
        _notify_advisor(f"🔔 Contacto directo — Cliente solicita hablar\nWhatsApp: {phone}")
        send_message(phone, "✅ Listo. Avisé a Christian para que te contacte.")
        send_main_menu(phone)
        return

    if t in ("menu", "menú", "inicio", "hola"):
        user_state[phone] = ""
        send_main_menu(phone)
        return

    # Continuación de embudos
    if st.startswith("imss_"):
        _imss_next(phone, text)
        return
    if st.startswith("emp_"):
        _emp_next(phone, text)
        return
    if st.startswith("fp_"):
        _fp_next(phone, text)
        return
    if st.startswith("auto_"):
        _auto_next(phone, text)
        return

    send_message(phone, "No entendí. Escribe *menú* para ver opciones.")

# =============================================================================
# Webhook — verificación (Meta)
# =============================================================================
@app.get("/webhook")
def webhook_verify():
    try:
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge", "")
        if mode == "subscribe" and token == VERIFY_TOKEN:
            log.info("✅ Webhook verificado")
            return challenge, 200
    except Exception:
        log.exception("❌ Error en verificación webhook")
    return "Error", 403

# =============================================================================
# Webhook — recepción (Meta)
# =============================================================================
def _should_greet(phone: str) -> bool:
    last = greeted_at.get(phone)
    if not last:
        return True
    return (datetime.now(timezone.utc) - last) > timedelta(hours=GREET_WINDOW_HOURS)

def _greet(phone: str, match: Optional[Dict[str, Any]]) -> None:
    # Saludo controlado 24h (sin romper matching)
    if not _should_greet(phone):
        return
    greeted_at[phone] = datetime.now(timezone.utc)

    if match and match.get("nombre"):
        send_message(phone, f"Hola {match['nombre']} 👋 Soy *Vicky*. ¿En qué te puedo ayudar hoy?")
    else:
        send_message(phone, "Hola 👋 Soy *Vicky*. Estoy para ayudarte.")

def _download_media(media_id: str) -> Tuple[Optional[bytes], Optional[str], Optional[str]]:
    if not META_TOKEN:
        return None, None, None
    try:
        meta = requests.get(
            f"https://graph.facebook.com/{META_API_VERSION}/{media_id}",
            headers={"Authorization": f"Bearer {META_TOKEN}"},
            timeout=WPP_TIMEOUT,
        )
        if meta.status_code != 200:
            log.warning("⚠️ media meta %s: %s", meta.status_code, meta.text[:250])
            return None, None, None
        meta_j = meta.json()
        url = meta_j.get("url")
        mime = meta_j.get("mime_type")
        fname = meta_j.get("filename") or f"media_{media_id}"
        if not url:
            return None, None, None

        binr = requests.get(url, headers={"Authorization": f"Bearer {META_TOKEN}"}, timeout=WPP_TIMEOUT)
        if binr.status_code != 200:
            log.warning("⚠️ media download %s", binr.status_code)
            return None, None, None
        return binr.content, mime, fname
    except Exception:
        log.exception("❌ Error descargando media")
        return None, None, None

def _handle_media(phone: str, msg: Dict[str, Any], match: Optional[Dict[str, Any]]) -> None:
    try:
        media_id = None
        mtype = msg.get("type")
        if mtype == "image" and "image" in msg:
            media_id = msg["image"].get("id")
        elif mtype == "document" and "document" in msg:
            media_id = msg["document"].get("id")
        elif mtype == "audio" and "audio" in msg:
            media_id = msg["audio"].get("id")
        elif mtype == "video" and "video" in msg:
            media_id = msg["video"].get("id")

        if not media_id:
            send_message(phone, "Recibí tu archivo, gracias. (No se pudo identificar el contenido).")
            return

        file_bytes, mime, fname = _download_media(media_id)
        if not file_bytes:
            send_message(phone, "Recibí tu archivo, pero hubo un problema procesándolo.")
            return

        last4 = _normalize_phone_last10(phone)[-4:]
        if match and match.get("nombre"):
            folder_name = f"{str(match['nombre']).replace(' ', '_')}_{last4}"
        else:
            folder_name = f"Cliente_{last4}"

        link = upload_to_drive(fname, file_bytes, mime or "application/octet-stream", folder_name)

        _notify_advisor(
            "🔔 Multimedia recibida\n"
            f"Desde: {phone}\n"
            f"Archivo: {fname}\n"
            f"Drive: {link or '(sin link Drive)'}"
        )
        send_message(phone, "✅ *Recibido y en proceso*. En breve te doy seguimiento.")
    except Exception:
        log.exception("❌ Error manejando multimedia")
        send_message(phone, "Recibí tu archivo, gracias. Si algo falla, lo reviso de inmediato.")

def _openai_answer(prompt: str) -> Optional[str]:
    """Compatibilidad con distintas versiones del SDK. Si falla, regresa None."""
    if not (OPENAI_API_KEY and openai):
        return None
    try:
        # Nuevo SDK (OpenAI client) vs legado:
        # - Si existe openai.OpenAI, usamos client.
        if hasattr(openai, "OpenAI"):
            client = openai.OpenAI(api_key=OPENAI_API_KEY)
            r = client.chat.completions.create(
                model=os.getenv("OPENAI_MODEL") or "gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.4,
            )
            return (r.choices[0].message.content or "").strip()

        # Legado (si aún está disponible)
        openai.api_key = OPENAI_API_KEY  # type: ignore
        r = openai.chat.completions.create(  # type: ignore
            model=os.getenv("OPENAI_MODEL") or "gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.4,
        )
        return (r.choices[0].message.content or "").strip()
    except Exception:
        log.exception("❌ Error llamando a OpenAI")
        return None

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
        phone = msg.get("from")
        if not phone:
            return jsonify({"ok": True}), 200

        phone = str(phone).strip()

        # Siempre hacer matching (para TPV context, nombre, carpeta Drive, etc.)
        match = match_client_in_sheets(_normalize_phone_last10(phone))
        _greet(phone, match)

        mtype = msg.get("type")

        if mtype == "text" and "text" in msg:
            text = (msg["text"].get("body") or "").strip()

            # Comando opcional: sgpt:<prompt>
            if text.lower().startswith("sgpt:"):
                ans = _openai_answer(text.split("sgpt:", 1)[1].strip())
                if ans:
                    send_message(phone, ans)
                else:
                    send_message(phone, "Hubo un detalle al procesar tu solicitud. Intentemos de nuevo.")
                return jsonify({"ok": True}), 200

            _route_command(phone, text, match)
            return jsonify({"ok": True}), 200

        if mtype in {"image", "document", "audio", "video"}:
            _handle_media(phone, msg, match)
            return jsonify({"ok": True}), 200

        return jsonify({"ok": True}), 200
    except Exception:
        log.exception("❌ Error en webhook_receive")
        return jsonify({"ok": True}), 200

# =============================================================================
# Endpoints auxiliares
# =============================================================================
@app.get("/health")
def health():
    return jsonify({"status": "ok", "service": "Vicky Bot Inbursa", "timestamp": datetime.utcnow().isoformat()}), 200

@app.get("/ext/health")
def ext_health():
    return jsonify(
        {
            "status": "ok",
            "whatsapp_configured": bool(META_TOKEN and WABA_PHONE_ID),
            "google_ready": google_ready,
            "openai_ready": bool(openai and OPENAI_API_KEY),
        }
    ), 200

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

# =============================================================================
# /ext/send-promo (bulk background)
# =============================================================================
def _bulk_send_worker(items: List[Dict[str, Any]]) -> None:
    okc = 0
    failc = 0
    for item in items:
        try:
            to = str(item.get("to", "")).strip()
            text = str(item.get("text", "")).strip()
            template = str(item.get("template", "")).strip()
            params = item.get("params", [])

            if not to:
                failc += 1
                continue

            success = False
            if template:
                success = send_template_message(to, template, params if params is not None else [])
            elif text:
                success = send_message(to, text)
            else:
                failc += 1
                continue

            if success:
                okc += 1
            else:
                failc += 1

            time.sleep(0.35)
        except Exception:
            failc += 1
            log.exception("❌ Error en worker bulk")

    if ADVISOR_NUMBER:
        send_message(ADVISOR_NUMBER, f"📊 Resumen envío masivo:\n• Exitosos: {okc}\n• Fallidos: {failc}\n• Total: {len(items)}")

@app.post("/ext/send-promo")
def ext_send_promo():
    try:
        if not (META_TOKEN and WABA_PHONE_ID):
            return jsonify({"queued": False, "error": "WhatsApp Business API no configurada"}), 500

        body = request.get_json(force=True) or {}
        items = body.get("items", [])
        if not isinstance(items, list) or not items:
            return jsonify({"queued": False, "error": "Formato inválido: 'items' debe ser una lista no vacía"}), 400

        valid: List[Dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            to = str(item.get("to", "")).strip()
            text = str(item.get("text", "")).strip()
            template = str(item.get("template", "")).strip()
            if not to:
                continue
            if not text and not template:
                continue
            valid.append(item)

        if not valid:
            return jsonify({"queued": False, "error": "No hay items válidos para enviar"}), 400

        threading.Thread(target=_bulk_send_worker, args=(valid,), daemon=True, name="BulkSendWorker").start()
        return jsonify({"queued": True, "valid_items": len(valid), "total_received": len(items), "timestamp": datetime.utcnow().isoformat()}), 202
    except Exception as e:
        log.exception("❌ Error crítico en /ext/send-promo")
        return jsonify({"queued": False, "error": str(e)}), 500

# =============================================================================
# AUTO SEND (1 prospecto por corrida) — Render Cron Job
# =============================================================================
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

        # regla: si LAST_MESSAGE_AT existe, solo reintentar si ESTATUS=FALLO_ENVIO
        if last_at and estatus != "FALLO_ENVIO":
            continue

        # permitido: vacío, PENDIENTE, FALLO_ENVIO
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

        ok = send_template_message(to, template_name, {"nombre": nombre})

        now_iso = datetime.utcnow().isoformat()
        estatus_val = "FALLO_ENVIO" if not ok else ("ENVIADO_TPV" if template_name == TPV_TEMPLATE_NAME else "ENVIADO_INICIAL")
        _update_row_cells(nxt["row_number"], {"ESTATUS": estatus_val, "LAST_MESSAGE_AT": now_iso}, headers)

        return jsonify({"ok": True, "sent": bool(ok), "to": to, "row": nxt["row_number"], "nombre": nombre, "timestamp": now_iso}), 200
    except Exception as e:
        log.exception("❌ Error en /ext/auto-send-one")
        return jsonify({"ok": False, "error": str(e)}), 500

# =============================================================================
# Arranque local (en Render usar Gunicorn)
# =============================================================================
if __name__ == "__main__":
    log.info("🚀 Iniciando Vicky Bot SECOM en puerto %s", PORT)
    log.info("📞 WhatsApp configurado: %s", bool(META_TOKEN and WABA_PHONE_ID))
    log.info("📊 Google Sheets/Drive: %s", google_ready)
    log.info("🧠 OpenAI: %s", bool(openai and OPENAI_API_KEY))
    app.run(host="0.0.0.0", port=PORT, debug=False)
