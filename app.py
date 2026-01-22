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

# ==========================
# GPT — Clasificador de intención (INVISIBLE)
# ==========================
def gpt_classify_intent(text: str) -> Optional[str]:
    """Devuelve una etiqueta de intención o None. GPT NO responde al usuario."""
    if not openai or not OPENAI_API_KEY or not text:
        return None

    try:
        completion = openai.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Clasifica el mensaje del usuario y responde SOLO con UNA etiqueta exacta:\n"
                        "INTENT_PRESTAMO_IMSS\n"
                        "INTENT_SEGURO_AUTO\n"
                        "INTENT_VIDA_SALUD\n"
                        "INTENT_VRIM\n"
                        "INTENT_EMPRESARIAL\n"
                        "INTENT_CONTACTO\n"
                        "INTENT_DESCONOCIDO\n"
                        "No agregues texto adicional."
                    ),
                },
                {"role": "user", "content": text},
            ],
        )

        label = (completion.choices[0].message.content or "").strip()
        if label.startswith("INTENT_"):
            return label
        return "INTENT_DESCONOCIDO"
    except Exception:
        log.exception("❌ Error GPT clasificando intención")
        return None

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
    """Envía mensaje de texto WPP. Reintentos exponenciales en 429/5xx."""
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
                log.info(f"🔄 Reintentando en {2 ** attempt} segundos...")
                _backoff(attempt)
                continue
            return False
        except requests.exceptions.Timeout:
            log.error(f"⏰ Timeout enviando mensaje a {to} (intento {attempt + 1})")
            if attempt < 2:
                _backoff(attempt)
                continue
            return False
        except Exception as e:
            log.exception(f"❌ Error en send_message a {to}")
            if attempt < 2:
                _backoff(attempt)
                continue
            return False
    return False


def forward_media_to_advisor(media_type: str, media_id: str) -> None:
    """Reenvía la multimedia recibida al número del asesor usando el media_id original."""
    if not (META_TOKEN and WPP_API_URL and ADVISOR_NUMBER):
        return
    payload = {
        "messaging_product": "whatsapp",
        "to": ADVISOR_NUMBER,
        "type": media_type,
        media_type: {"id": media_id}
    }
    try:
        requests.post(WPP_API_URL, headers=_wpp_headers(), json=payload, timeout=WPP_TIMEOUT)
        log.info(f"📤 Multimedia reenviada al asesor ({media_type})")
    except Exception:
        log.exception("❌ Error reenviando multimedia al asesor")

def send_template_message(to: str, template_name: str, params: Dict | List) -> bool:
    """Envía plantilla preaprobada.

    - Si `params` es list => parámetros posicionales ({{1}}, {{2}}, ...).
    - Si `params` es dict => parámetros nombrados ({{nombre}}, {{monto}}, ...), usando `parameter_name`.
    """
    if not (META_TOKEN and WPP_API_URL):
        log.error("❌ WhatsApp no configurado para plantillas.")
        return False

    components: List[Dict[str, Any]] = []

    # HEADER image (solo plantillas con imagen fija)
    if template_name == "seguro_auto_70":
        image_url = os.getenv("SEGURO_AUTO_70_IMAGE_URL")
        if not image_url:
            log.error("❌ Falta SEGURO_AUTO_70_IMAGE_URL en entorno.")
            return False
        components.append({
            "type": "header",
            "parameters": [{
                "type": "image",
                "image": {"link": image_url}
            }]
        })

    # BODY parameters
    if isinstance(params, dict):
        body_params = []
        for k, v in params.items():
            body_params.append({
                "type": "text",
                "parameter_name": k,
                "text": str(v)
            })
        if body_params:
            components.append({
                "type": "body",
                "parameters": body_params
            })
    elif isinstance(params, list):
        body_params = [{"type": "text", "text": str(v)} for v in params]
        if body_params:
            components.append({
                "type": "body",
                "parameters": body_params
            })

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
                log.info(f"✅ Plantilla '{template_name}' enviada exitosamente a {to}")
                return True

            log.warning(f"⚠️ WPP send_template fallo {resp.status_code}: {resp.text[:200]}")
            if _should_retry(resp.status_code) and attempt < 2:
                log.info(f"🔄 Reintentando plantilla en {2 ** attempt} segundos...")
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
    """
    Busca el teléfono (últimos 10 dígitos) en el Sheet y devuelve:
      - row: número de fila 1-based en Google Sheets
      - nombre
      - estatus (si existe la columna)
      - last_message_at (si existe la columna)
      - raw: fila completa
    """
    if not (google_ready and sheets_svc and SHEETS_ID_LEADS and SHEETS_TITLE_LEADS):
        log.warning("⚠️ Sheets no disponible; no se puede hacer matching.")
        return None
    try:
        headers, rows = _sheet_get_rows()  # usa el sheet principal (SHEETS_TITLE_LEADS)
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
        for k, row in enumerate(rows, start=2):  # fila 2 = primer registro
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
    """Registra una nota en una hoja 'Seguimiento' (append)."""
    if not (google_ready and sheets_svc and SHEETS_ID_LEADS):
        log.warning("⚠️ Sheets no disponible; no se puede escribir seguimiento.")
        return
    try:
        title = "Seguimiento"
        body = {
            "values": [[str(row), date_iso, note]]
        }
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

def _find_or_create_client_folder(folder_name: str) -> Optional[str]:
    """Ubica/crea subcarpeta dentro de DRIVE_PARENT_FOLDER_ID."""
    if not (google_ready and drive_svc and DRIVE_PARENT_FOLDER_ID):
        log.warning("⚠️ Drive no disponible; no se puede crear carpeta.")
        return None
    try:
        q = f"name = '{folder_name}' and mimeType = 'application/vnd.google-apps.folder' and '{DRIVE_PARENT_FOLDER_ID}' in parents and trashed = false"
        resp = drive_svc.files().list(q=q, fields="files(id, name)").execute()
        items = resp.get("files", [])
        if items:
            log.info(f"✅ Carpeta encontrada: {folder_name}")
            return items[0]["id"]
        meta = {
            "name": folder_name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [DRIVE_PARENT_FOLDER_ID],
        }
        created = drive_svc.files().create(body=meta, fields="id").execute()
        folder_id = created.get("id")
        log.info(f"✅ Carpeta creada: {folder_name} (ID: {folder_id})")
        return folder_id
    except Exception:
        log.exception("❌ Error creando/buscando carpeta en Drive")
        return None

def upload_to_drive(file_name: str, file_bytes: bytes, mime_type: str, folder_name: str) -> Optional[str]:
    """Sube archivo a carpeta del cliente; retorna webViewLink (si posible) o fileId."""
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
    log.info(f\"📋 Enviando menú principal a {phone}\")
    try:
        user = _ensure_user(phone)
        user[\"last_menu_shown\"] = True
    except Exception:
        log.exception(\"❌ No se pudo guardar last_menu_shown\")
    send_message(phone, MAIN_MENU)

# ==========================
# Embudos (conservados del original)
# ==========================
def _notify_advisor(text: str) -> None:
    try:
        log.info(f"👨‍💼 Notificando al asesor: {text}")
        send_message(ADVISOR_NUMBER, text)
    except Exception:
        log.exception("❌ Error notificando al asesor")

# --- TPV / Terminal bancaria (solo cuando viene de plantilla promo_tpv) ---
TPV_TEMPLATE_NAME = "promo_tpv"

def _parse_dt_maybe(value: str) -> Optional[datetime]:
    if not value:
        return None
    v = value.strip()
    try:
        # Soporta "2026-01-08T18:03:24.442624545Z"
        if v.endswith("Z"):
            v = v[:-1] + "+00:00"
        return datetime.fromisoformat(v)
    except Exception:
        return None

def _tpv_is_context(match: Optional[Dict[str, Any]]) -> bool:
    """
    TPV se activa únicamente si:
    - existe match en Sheets
    - ESTATUS == ENVIADO_TPV
    - LAST_MESSAGE_AT dentro de 24h
    """
    if not match:
        return False
    if (match.get("estatus") or "").strip().upper() != "ENVIADO_TPV":
        return False
    dt = _parse_dt_maybe(match.get("last_message_at") or "")
    if not dt:
        return False
    # Si dt viene con tz, normalizamos a UTC; si no, asumimos UTC.
    if dt.tzinfo is not None:
        now = datetime.now(dt.tzinfo)
    else:
        now = datetime.utcnow()
    return (now - dt) <= timedelta(hours=24)

def tpv_start_from_reply(phone: str, text: str, match: Optional[Dict[str, Any]]) -> bool:
    """
    Procesa la respuesta inmediata del prospecto al mensaje TPV (plantilla):
      - 1 / sí => pide solo giro => horario => notifica => cierra
      - 2 / no => pide motivo (opcional) => cierra
    Retorna True si consumió el mensaje (no debe seguir al menú general).
    """
    t = (text or "").strip().lower()
    intent = interpret_response(text)

    # Determina elección
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

    # Nombre para notificación (si existe)
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

        aviso = (
            "🔔 TPV — Prospecto interesado\n"
            f"WhatsApp: {phone}\n"
            f"Nombre: {nombre or '(sin nombre)'}\n"
            f"Giro: {data.get('tpv_giro','')}\n"
            f"Horario: {data.get('tpv_horario','')}"
        )

        _notify_advisor(aviso)

        # Opcional: marcar estatus si existe row
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

# --- AUTO CONTEXT DETECTION (NEW) ---
def _auto_is_context(match: Optional[Dict[str, Any]]) -> bool:
    """
    AUTO (seguro de auto) se activa si:
    - existe match en Sheets
    - ESTATUS en (ENVIADO_INICIAL, ENVIADO_AUTO, ENVIADO_SEGURO_AUTO)
    - LAST_MESSAGE_AT dentro de 24h
    """
    if not match:
        return False
    
    estatus = (match.get("estatus") or "").strip().upper()
    valid_status = {"ENVIADO_INICIAL", "ENVIADO_AUTO", "ENVIADO_SEGURO_AUTO"}
    if estatus not in valid_status:
        return False
    
    dt = _parse_dt_maybe(match.get("last_message_at") or "")
    if not dt:
        return False
    
    if dt.tzinfo is not None:
        now = datetime.now(dt.tzinfo)
    else:
        now = datetime.utcnow()
    
    return (now - dt) <= timedelta(hours=24)

def _handle_auto_context_response(phone: str, text: str, match: Dict[str, Any]) -> bool:
    """
    Maneja respuestas en contexto AUTO post-campaña.
    Retorna True si consumió el mensaje.
    """
    t = (text or "").strip().lower()
    intent = interpret_response(text)
    st_now = user_state.get(phone, "")
    idle = st_now in ("", "__greeted__")
    
    if not idle:
        return False
    
    if not _auto_is_context(match):
        return False
    
    # Respuesta positiva (Sí, 1, etc.)
    if t in ("1", "si", "sí", "ok", "claro") or intent == "positive":
        user_state[phone] = "auto_intro"
        auto_start(phone, match)
        return True
    
    # Respuesta negativa (No, 2, etc.)
    if t in ("2", "no", "nel") or intent == "negative":
        user_state[phone] = "auto_vencimiento_fecha"
        nombre = match.get("nombre", "").strip() or "Cliente"
        send_message(phone, f"Entendido {nombre}. Para poder recordarte a tiempo, ¿cuál es la *fecha de vencimiento* de tu póliza? (formato AAAA-MM-DD)")
        
        # Notificar al asesor
        aviso = (
            "🔔 AUTO — NO INTERESADO / TIENE SEGURO\n"
            f"WhatsApp: {phone}\n"
            f"Nombre: {nombre}\n"
            f"Respuesta: {text}"
        )
        _notify_advisor(aviso)
        return True
    
    # Menú
    if t in ("menu", "menú", "inicio"):
        user_state[phone] = "__greeted__"
        send_main_menu(phone)
        return True
    
    # Cualquier otra cosa: notificar asesor como DUDA
    nombre = match.get("nombre", "").strip() or "Cliente"
    aviso = (
        "📩 AUTO — DUDA / INTERÉS detectada\n"
        f"WhatsApp: {phone}\n"
        f"Nombre: {nombre}\n"
        f"Mensaje: {text}"
    )
    _notify_advisor(aviso)
    
    # Pregunta cerrada de confirmación
    send_message(phone, "¿Deseas cotizar tu seguro de auto ahora? Responde *Sí* o *No*")
    return True

# --- IMSS (opción 1) ---
def imss_start(phone: str, match: Optional[Dict[str, Any]]) -> None:
    user_state[phone] = "imss_beneficios"
    log.info(f"🏥 Iniciando embudo IMSS para {phone}")
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

# --- Crédito Empresarial (opción 5) ---
def emp_start(phone: str, match: Optional[Dict[str, Any]]) -> None:
    user_state[phone] = "emp_confirma"
    log.info(f"🏢 Iniciando embudo empresarial para {phone}")
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

# --- Financiamiento Práctico (opción 6) ---
FP_QUESTIONS = [f"Pregunta {i}" for i in range(1, 12)]
def fp_start(phone: str, match: Optional[Dict[str, Any]]) -> None:
    user_state[phone] = "fp_q1"
    _ensure_user(phone)["fp_answers"] = {}
    log.info(f"💰 Iniciando embudo financiamiento práctico para {phone}")
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
        resumen = "✅ Gracias. Un asesor te contactará.\n" + "\n".join(
            f"{k.upper()}: {v}" for k, v in data.get("fp_answers", {}).items()
        )
        if data.get("fp_comentario"):
            resumen += f"\nCOMENTARIO: {data['fp_comentario']}"
        send_message(phone, resumen)
        _notify_advisor(f"🔔 Financiamiento Práctico — Resumen\nWhatsApp: {phone}\n{resumen}")
        user_state[phone] = "__greeted__"
        send_main_menu(phone)

# --- Seguros de Auto (opción 2) ---
def auto_start(phone: str, match: Optional[Dict[str, Any]]) -> None:
    user_state[phone] = "auto_intro"
    log.info(f"🚗 Iniciando embudo seguro auto para {phone}")
    send_message(phone,
        "🚗 *Seguro de Auto*\nEnvíame por favor:\n• INE (frente)\n• Tarjeta de circulación *o* número de placas\n\nCuando lo envíes, te confirmaré recepción y procesaré la cotización."
    )

def _auto_next(phone: str, text: str) -> None:
    st = user_state.get(phone, "")
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
            threading.Thread(target=_retry_after_days, args=(phone, 7), daemon=True).start()
            send_message(phone, f"✅ Gracias. Te contactaré *un mes antes* ({objetivo.isoformat()}).")
            user_state[phone] = "__greeted__"
            send_main_menu(phone)
        except Exception:
            send_message(phone, "Formato inválido. Usa AAAA-MM-DD. Ejemplo: 2025-12-31")

def _retry_after_days(phone: str, days: int) -> None:
    try:
        time.sleep(days * 24 * 60 * 60)
        send_message(phone, "⏰ Seguimos a tus órdenes. ¿Deseas que coticemos tu seguro de auto cuando se acerque el vencimiento?")
        write_followup_to_sheets("auto_reintento", f"Reintento +{days}d enviado a {phone}", datetime.utcnow().isoformat())
    except Exception:
        log.exception("Error en reintento programado")

# ==========================
# Router helpers
# ==========================

def route_gpt_intent(phone: str, intent: str, match: Optional[Dict[str, Any]]) -> None:
    """Mapea intención -> flujos existentes. GPT NO manda, solo interpreta."""
    if intent == "INTENT_PRESTAMO_IMSS":
        imss_start(phone, match)
        return

    if intent == "INTENT_SEGURO_AUTO":
        auto_start(phone, match)
        return

    if intent == "INTENT_EMPRESARIAL":
        emp_start(phone, match)
        return

    if intent == "INTENT_CONTACTO":
        try:
            _notify_advisor(f"🔔 Contacto solicitado (GPT)\nWhatsApp: {phone}")
        except Exception:
            log.exception("❌ Error notificando asesor (GPT contacto)")
        send_message(phone, "✅ Listo. Avisé a Christian para que te contacte.")
        send_main_menu(phone)
        return

    if intent in ("INTENT_VIDA_SALUD", "INTENT_VRIM"):
        try:
            _notify_advisor(f"🔔 Interés detectado ({intent})\nWhatsApp: {phone}")
        except Exception:
            log.exception("❌ Error notificando asesor (GPT interés)")
        send_message(phone, "Gracias por tu interés. Un asesor te contactará.")
        send_main_menu(phone)
        return

    # Desconocido: menú seguro
    send_main_menu(phone)

def _greet_and_match(phone: str) -> Optional[Dict[str, Any]]:
    last10 = _normalize_phone_last10(phone)
    match = match_client_in_sheets(last10)
    if match and match.get("nombre"):
        send_message(phone, f"Hola {match['nombre']} 👋 Soy *Vicky*. ¿En qué te puedo ayudar hoy?")
    else:
        send_message(phone, "Hola 👋 Soy *Vicky*. Estoy para ayudarte.")
    return match

def _route_command(phone: str, text: str, match: Optional[Dict[str, Any]]) -> None:
    t = text.strip().lower()
    st = user_state.get(phone, "")

    # TPV tiene prioridad si el prospecto viene de la plantilla promo_tpv
    if st.startswith("tpv_"):
        _tpv_next(phone, text, match)
        return
    if _tpv_is_context(match):
        if tpv_start_from_reply(phone, text, match):
            return

    if t in ("1", "imss", "ley 73", "préstamo", "prestamo", "pension", "pensión"):
        imss_start(phone, match)
    elif t in ("2", "auto", "seguros de auto", "seguro auto"):
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
    elif t in ("menu", "menú", "inicio", "hola"):
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
            log.info("✅ Webhook verificado exitosamente")
            return challenge, 200
    except Exception:
        log.exception("❌ Error en verificación webhook")
    log.warning("❌ Webhook verification failed")
    return "Error", 403

# ==========================
# Webhook — recepción (VERSIÓN CORREGIDA)
# ==========================
def _download_media(media_id: str) -> Tuple[Optional[bytes], Optional[str], Optional[str]]:
    """Descarga bytes, mime_type y filename desde WPP Graph para media_id."""
    if not META_TOKEN:
        return None, None, None
    try:
        meta = requests.get(
            f"https://graph.facebook.com/v20.0/{media_id}",
            headers={"Authorization": f"Bearer {META_TOKEN}"},
            timeout=WPP_TIMEOUT
        )
        if meta.status_code != 200:
            log.warning(f"⚠️ Meta media meta fallo {meta.status_code}: {meta.text[:200]}")
            return None, None, None
        meta_j = meta.json()
        url = meta_j.get("url")
        mime = meta_j.get("mime_type")
        fname = meta_j.get("filename") or f"media_{media_id}"
        if not url:
            return None, None, None
        binr = requests.get(url, headers={"Authorization": f"Bearer {META_TOKEN}"}, timeout=WPP_TIMEOUT)
        if binr.status_code != 200:
            log.warning(f"⚠️ Meta media download fallo {binr.status_code}")
            return None, None, None
        log.info(f"✅ Media descargada: {fname} ({len(binr.content)} bytes)")
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

        # 🔁 Reenviar inmediatamente la multimedia al asesor
        forward_media_to_advisor(msg.get("type"), media_id)

        file_bytes, mime, fname = _download_media(media_id)
        if not file_bytes:
            send_message(phone, "Recibí tu archivo, pero hubo un problema procesándolo.")
            return

        last4 = _normalize_phone_last10(phone)[-4:]
        match = match_client_in_sheets(_normalize_phone_last10(phone))
        if match and match.get("nombre"):
            folder_name = f"{match['nombre'].replace(' ', '_')}_{last4}"
        else:
            folder_name = f"Cliente_{last4}"

        link = upload_to_drive(fname, file_bytes, mime or "application/octet-stream", folder_name)
        link_text = link or "(sin link Drive)"

        _notify_advisor(f"🔔 Multimedia recibida\nDesde: {phone}\nArchivo: {fname}\nDrive: {link_text}")
        send_message(phone, "✅ *Recibido y en proceso*. En breve te doy seguimiento.")
    except Exception:
        log.exception("❌ Error manejando multimedia")
        send_message(phone, "Recibí tu archivo, gracias. Si algo falla, lo reviso de inmediato.")

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
            log.info("ℹ️ Webhook sin mensajes (posible status update)")
            return jsonify({"ok": True}), 200

        msg = messages[0]
        phone = msg.get("from")
        
        if not phone:
            log.warning("⚠️ Mensaje sin número de teléfono")
            return jsonify({"ok": True}), 200

        log.info(f"📱 Mensaje de {phone}: {msg.get('type', 'unknown')}")

        # Obtener match SIEMPRE (necesario para contexto de campaña)
        last10 = _normalize_phone_last10(phone)
        match = match_client_in_sheets(last10)
        
        # Estado actual del usuario
        st_now = user_state.get(phone, "")
        idle = st_now in ("", "__greeted__")
        
        # Manejo de mensajes de texto
        mtype = msg.get("type")
        if mtype == "text" and "text" in msg:
            text = msg["text"].get("body", "").strip()
            log.info(f"💬 Texto recibido de {phone}: {text}")
            
            # =========================================================
            # 🔔 INTERCEPTOR POST-CAMPAÑA (AUTO) - PRIORIDAD ALTA
            # =========================================================
            if idle and match:
                # 1. CONTEXTO AUTO (Seguro de Auto)
                if _auto_is_context(match):
                    if _handle_auto_context_response(phone, text, match):
                        return jsonify({"ok": True}), 200
                
                # 2. CONTEXTO TPV
                if _tpv_is_context(match):
                    if tpv_start_from_reply(phone, text, match):
                        return jsonify({"ok": True}), 200

            # =========================================================
            # 🔔 DETECCIÓN DE INTERÉS / DUDA POST-PLANTILLA (GLOBAL)
            # =========================================================
            t_lower = text.lower().strip()
            VALID_COMMANDS = {
                "1","2","3","4","5","6","7",
                "menu","menú","inicio","hola",
                "imss","ley 73","prestamo","préstamo","pension","pensión",
                "auto","seguro auto","seguros de auto",
                "vida","salud","seguro de vida","seguro de salud",
                "vrim","tarjeta medica","tarjeta médica",
                "empresarial","pyme","credito empresarial","crédito empresarial",
                "financiamiento","financiamiento practico","financiamiento práctico",
                "contactar","asesor","contactar con christian"
            }

            if (
                not t_lower.isdigit()
                and t_lower not in VALID_COMMANDS
                and idle
            ):
                aviso = (
                    "📩 Cliente INTERESADO / DUDA detectada\n"
                    f"WhatsApp: {phone}\n"
                    f"Mensaje: {text}"
                )
                _notify_advisor(aviso)
            # =========================================================

            # Inicialización del estado si es nuevo usuario
            if phone not in user_state:
                user_state[phone] = "__greeted__"
                if not match:  # Solo saludar si no tenemos match ya
                    _greet_and_match(phone)


# =========================
# GPT INVISIBLE — FALLBACK (sin comandos)
# =========================
if idle and text and (not t_lower.isdigit()) and (t_lower not in VALID_COMMANDS):
    intent = gpt_classify_intent(text)
    if intent:
        try:
            user = _ensure_user(phone)
            user["last_intent"] = intent
            user["last_menu_shown"] = False
        except Exception:
            log.exception("❌ No se pudo guardar estado mínimo (last_intent)")

        log.info(f"🧠 GPT intent detectado: {intent}")
        route_gpt_intent(phone, intent, match)
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
    return jsonify({
        "status": "ok", 
        "service": "Vicky Bot Inbursa",
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
    """Endpoint para pruebas de envío individual"""
    try:
        data = request.get_json(force=True) or {}
        to = str(data.get("to", "")).strip()
        text = str(data.get("text", "")).strip()
        
        if not to or not text:
            return jsonify({
                "ok": False, 
                "error": "Faltan parámetros 'to' o 'text'"
            }), 400
            
        log.info(f"🧪 Test send a {to}: {text}")
        ok = send_message(to, text)
        return jsonify({"ok": bool(ok)}), 200
    except Exception as e:
        log.exception("❌ Error en /ext/test-send")
        return jsonify({
            "ok": False, 
            "error": str(e)
        }), 500

def _bulk_send_worker(items: List[Dict[str, Any]]) -> None:
    """Worker mejorado para envíos masivos con logging exhaustivo"""
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
                log.warning(f"⏭️ Item {i} sin destinatario, omitiendo")
                failed += 1
                continue
                
            log.info(f"📤 [{i}/{len(items)}] Procesando: {to}")
            
            success = False
            if template:
                success = send_template_message(to, template, params)
                log.info(f"   ↳ Plantilla '{template}' a {to}: {'✅' if success else '❌'}")
            elif text:
                success = send_message(to, text)
                log.info(f"   ↳ Mensaje a {to}: {'✅' if success else '❌'}")
            else:
                log.warning(f"   ↳ Item {i} sin contenido válido")
                failed += 1
                continue
            
            if success:
                successful += 1
            else:
                failed += 1
                
            time.sleep(0.5)
            
        except Exception as e:
            failed += 1
            log.exception(f"❌ Error procesando item {i} para {item.get('to', 'unknown')}")
    
    log.info(f"🎯 Envío masivo completado: {successful} ✅, {failed} ❌")
    
    if ADVISOR_NUMBER:
        summary_msg = f"📊 Resumen envío masivo:\n• Exitosos: {successful}\n• Fallidos: {failed}\n• Total: {len(items)}"
        send_message(ADVISOR_NUMBER, summary_msg)

@app.post("/ext/send-promo")
def ext_send_promo():
    """Endpoint CORREGIDO para envíos masivos tipo WAPI"""
    try:
        if not META_TOKEN or not WABA_PHONE_ID:
            log.error("❌ META_TOKEN o WABA_PHONE_ID no configurados")
            return jsonify({
                "queued": False, 
                "error": "WhatsApp Business API no configurada"
            }), 500

        body = request.get_json(force=True) or {}
        items = body.get("items", [])
        
        log.info(f"📨 Recibida solicitud send-promo con {len(items)} items")
        
        if not isinstance(items, list):
            log.warning("❌ Formato inválido: items no es una lista")
            return jsonify({
                "queued": False, 
                "error": "Formato inválido: 'items' debe ser una lista"
            }), 400
            
        if not items:
            log.warning("❌ Lista de items vacía")
            return jsonify({
                "queued": False, 
                "error": "Lista 'items' vacía"
            }), 400

        valid_items = []
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                log.warning(f"⏭️ Item {i} no es un diccionario, omitiendo")
                continue
                
            to = item.get("to", "").strip()
            text = item.get("text", "").strip()
            template = item.get("template", "").strip()
            
            if not to:
                log.warning(f"⏭️ Item {i} sin destinatario, omitiendo")
                continue
                
            if not text and not template:
                log.warning(f"⏭️ Item {i} sin contenido (text o template), omitiendo")
                continue
                
            valid_items.append(item)

        if not valid_items:
            log.warning("❌ No hay items válidos después de la validación")
            return jsonify({
                "queued": False, 
                "error": "No hay items válidos para enviar"
            }), 400

        log.info(f"✅ Validación exitosa: {len(valid_items)} items válidos de {len(items)} recibidos")
        
        threading.Thread(
            target=_bulk_send_worker, 
            args=(valid_items,), 
            daemon=True,
            name="BulkSendWorker"
        ).start()
        
        response = {
            "queued": True,
            "message": f"Procesando {len(valid_items)} mensajes en background",
            "total_received": len(items),
            "valid_items": len(valid_items),
            "timestamp": datetime.utcnow().isoformat()
        }
        
        log.info(f"✅ Envío masivo encolado: {response}")
        return jsonify(response), 202
        
    except Exception as e:
        log.exception("❌ Error crítico en /ext/send-promo")
        return jsonify({
            "queued": False, 
            "error": f"Error interno: {str(e)}"
        }), 500

# ==========================
# Arranque (para desarrollo local)
# En producción usar Gunicorn: `gunicorn app:app --bind 0.0.0.0:$PORT`
# ==========================
if __name__ == "__main__":
    log.info(f"🚀 Iniciando Vicky Bot SECOM en puerto {PORT}")
    log.info(f"📞 WhatsApp configurado: {bool(META_TOKEN and WABA_PHONE_ID)}")
    log.info(f"📊 Google Sheets/Drive: {google_ready}")
    log.info(f"🧠 OpenAI: {bool(openai and OPENAI_API_KEY)}")
    
    app.run(host="0.0.0.0", port=PORT, debug=False)
# ==========================
# AUTO SEND (1 prospecto por corrida) — Render Cron Job
# ==========================
AUTO_SEND_TOKEN = os.getenv("AUTO_SEND_TOKEN", "").strip()

def _sheet_get_rows() -> Tuple[List[str], List[List[str]]]:
    """Obtiene headers + rows del Sheet principal."""
    if not (google_ready and sheets_svc and SHEETS_ID_LEADS and SHEETS_TITLE_LEADS):
        raise RuntimeError("Sheets no disponible (google_ready/SHEETS_ID_LEADS/SHEETS_TITLE_LEADS).")
    rng = f"{SHEETS_TITLE_LEADS}!A:Z"
    values = sheets_svc.spreadsheets().values().get(spreadsheetId=SHEETS_ID_LEADS, range=rng).execute()
    rows = values.get("values", [])
    if not rows:
        return [], []
    headers = [h.strip() for h in rows[0]]
    data_rows = rows[1:]
    return headers, data_rows

def _idx(headers: List[str], name: str) -> Optional[int]:
    """Encuentra índice por header exacto (case-insensitive)."""
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

    # WhatsApp Cloud API (MX):
    # - móviles normalmente requieren "521" + 10 dígitos
    # - algunos datos vienen como "52" + 10 dígitos; se corrige a "521"
    if len(last10) == 10:
        return f"521{last10}"

    # Si ya viene con 52 + 10 dígitos, insertar el "1"
    if digits.startswith("52") and len(digits) == 12:
        return f"521{digits[2:]}"

    # Si ya viene correcto (521 + 10 dígitos)
    if digits.startswith("521") and len(digits) == 13:
        return digits

    return digits

def _update_row_cells(row_number_1based: int, updates: Dict[str, str], headers: List[str]) -> None:
    """Actualiza celdas por header (ej. ESTATUS, LAST_MESSAGE_AT). row_number_1based incluye header como fila 1."""
    if not (google_ready and sheets_svc and SHEETS_ID_LEADS and SHEETS_TITLE_LEADS):
        raise RuntimeError("Sheets no disponible para update.")
    data = []
    for col_name, value in updates.items():
        j = _idx(headers, col_name)
        if j is None:
            raise RuntimeError(f"No existe columna '{col_name}' en el Sheet.")
        # Columna A=1 => letra:
        col_letter = chr(ord("A") + j)
        a1 = f"{SHEETS_TITLE_LEADS}!{col_letter}{row_number_1based}"
        data.append({"range": a1, "values": [[value]]})
    body = {"valueInputOption": "USER_ENTERED", "data": data}
    sheets_svc.spreadsheets().values().batchUpdate(spreadsheetId=SHEETS_ID_LEADS, body=body).execute()

def _pick_next_pending(headers: List[str], rows: List[List[str]]) -> Optional[Dict[str, Any]]:
    """
    Selecciona 1 prospecto pendiente:
    - WhatsApp no vacío
    - ESTATUS vacío o 'PENDIENTE'
    - Reintenta automáticamente si ESTATUS = 'FALLO_ENVIO'
      (aunque LAST_MESSAGE_AT tenga valor)
    - Para cualquier otro ESTATUS != 'FALLO_ENVIO', requiere LAST_MESSAGE_AT vacío
    """
    i_name = _idx(headers, "Nombre")
    i_wa = _idx(headers, "WhatsApp")
    i_status = _idx(headers, "ESTATUS")
    i_last = _idx(headers, "LAST_MESSAGE_AT")

    if i_name is None or i_wa is None:
        raise RuntimeError("Faltan columnas requeridas: 'Nombre' y/o 'WhatsApp'.")

    for k, row in enumerate(rows, start=2):  # fila 2 = primer registro (fila 1 es header)
        nombre = _cell(row, i_name).strip()
        wa = _cell(row, i_wa).strip()
        estatus = _cell(row, i_status).strip().upper() if i_status is not None else ""
        last_at = _cell(row, i_last).strip() if i_last is not None else ""

        if not wa:
            continue
        # Regla de reintento (Opción 2):
        # - Si ya hay LAST_MESSAGE_AT, normalmente se salta
        # - PERO si ESTATUS=FALLO_ENVIO, se permite reintento aunque haya timestamp
        if last_at and estatus != "FALLO_ENVIO":
            continue
        # Permitimos:
        # - vacío
        # - PENDIENTE
        # - FALLO_ENVIO (reintento)
        if estatus and estatus not in ("PENDIENTE", "FALLO_ENVIO"):
            # si ya trae ENVIADO_INICIAL u otro, lo saltamos
            continue

        return {"row_number": k, "nombre": nombre, "whatsapp": wa}

    return None

@app.post("/ext/auto-send-one")
def ext_auto_send_one():
    """
    Endpoint para cron: envía 1 plantilla al siguiente prospecto pendiente.
    Protegido por header: X-AUTO-TOKEN = AUTO_SEND_TOKEN
    """
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
        updates = {
            "ESTATUS": estatus_val,
            "LAST_MESSAGE_AT": now_iso
        }
        _update_row_cells(nxt["row_number"], updates, headers)

        return jsonify({
            "ok": True,
            "sent": bool(ok),
            "to": to,
            "row": nxt["row_number"],
            "nombre": nombre,
            "timestamp": now_iso
        }), 200

    except Exception as e:
        log.exception("❌ Error en /ext/auto-send-one")
        return jsonify({"ok": False, "error": str(e)}), 500
