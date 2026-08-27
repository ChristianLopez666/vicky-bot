# app.py — Vicky SECOM (HOTFIX 2: embudo activo antes de comandos globales)
# Python 3.11+
# ------------------------------------------------------------
# Correcciones incluidas:
# 1. _route_command() protege estados activos antes de comandos globales.
# 2. vida_objetivo + "1" cierra Vida Temporal; no inicia IMSS.
# 3. webhook_receive() no llama Boardroom cuando hay embudo activo local.
# 4. /ext/send-promo y _bulk_send_worker no envían texto libre proactivo sin template.
# 5. /ext/auto-send-one exige template para mensajes business-initiated.
# ------------------------------------------------------------

from __future__ import annotations

import io
import json
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request

import cierre_cortesia as cc

# Google
try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseUpload
except Exception:  # pragma: no cover - dependencias opcionales de entorno
    service_account = None
    build = None
    MediaIoBaseUpload = None

# GPT opcional
try:
    import openai
except Exception:  # pragma: no cover - dependencia opcional
    openai = None


# ==========================
# Carga entorno + logging
# ==========================
load_dotenv()

META_TOKEN = os.getenv("META_TOKEN", "").strip()
WABA_PHONE_ID = os.getenv("WABA_PHONE_ID", "").strip()
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "").strip()
ADVISOR_NUMBER = os.getenv("ADVISOR_NUMBER", "5216682478005").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

BOARDROOM_DECISION_URL = os.getenv("BOARDROOM_DECISION_URL", "").strip()
BOARDROOM_AUTH_TOKEN = os.getenv("BOARDROOM_AUTH_TOKEN", "").strip()
BOARDROOM_ENABLED = os.getenv("BOARDROOM_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
BOARDROOM_ORCHESTRATE_PATH = "/api/boardroom/orchestrate"
BUS_URL = os.getenv("BUS_URL", "").strip()
BUS_INTERNAL_TOKEN = os.getenv("BUS_INTERNAL_TOKEN", "").strip()
_BUS_ACTIVE = os.getenv("BUS_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
BOARDROOM_IS_AUTHORITY = True
NEUTRAL_FALLBACK_MESSAGE = "Recibí tu mensaje. En un momento te atiendo."
# SECOM-AUTH-FIX-1 (DOC-0043, hydra-source-of-truth): default false = modo
# legacy sin cambios de comportamiento. true = Boardroom deja de bloquear
# funnels deterministas ya iniciados y el resultado de Boardroom se
# clasifica por `status` en vez de tratarse siempre como manejado.
SECOM_LOCAL_FALLBACK_ENABLED = os.getenv("SECOM_LOCAL_FALLBACK_ENABLED", "false").strip().lower() in (
    "1", "true", "yes", "on",
)
_BOARDROOM_ALLOWED_INSTRUCTIONS = {
    "send_message",
    "ask_question",
    "send_options",
    "request_document",
    "notify_advisor",
    "handoff",
    "no_action",
}

GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON", "").strip()
SHEETS_ID_LEADS = os.getenv("SHEETS_ID_LEADS", "").strip()
SHEETS_TITLE_LEADS = os.getenv("SHEETS_TITLE_LEADS", "Prospectos SECOM Auto").strip()
DRIVE_PARENT_FOLDER_ID = os.getenv("DRIVE_PARENT_FOLDER_ID", "").strip()
AUTO_SEND_TOKEN = os.getenv("AUTO_SEND_TOKEN", "").strip()

# CF-4: kill switch de la campana outbound (auto-send-one), persistente en
# Sheets para sobrevivir un reinicio del servicio mientras esta pausada.
#
# Vive en su PROPIA pestana, no en una celda lejana de la hoja de leads. La
# version anterior usaba "<hoja de leads>!AA1" para quedar fuera del rango
# A:Z que lee _sheet_get_rows(); el defecto es que esa celda solo existe si
# la cuadricula llega hasta la columna 27. En produccion la hoja tiene 16
# columnas, asi que CADA lectura respondia HTTP 400 "exceeds grid limits":
# _is_campaign_paused() caia al fail-open (campana siempre activa) y
# _set_campaign_paused() no podia escribir, dejando muertos a la vez el
# freno manual y la auto-pausa por racha de fallos. Una pestana propia no
# depende del ancho de la hoja de datos ni se rompe al editar columnas.
CAMPAIGN_CONTROL_TAB = os.getenv("SHEETS_TITLE_CONTROL", "CONTROL").strip()
CAMPAIGN_PAUSE_CELL = "A2"
CAMPAIGN_PAUSED_VALUE = "PAUSED"
_CAMPAIGN_CONTROL_HEADER = [
    "ESTADO_CAMPANA",
    "Escribe PAUSED en A2 para detener la campana saliente; "
    "borra la celda para reanudarla.",
]
# La pestana se verifica una sola vez por proceso: crearla es idempotente,
# pero no hace falta pagar una lectura de metadatos cada 5 minutos.
_control_tab_ready = False

# Bitacora de avisos al asesor. El monitor de correos (Apps Script) no puede
# ver WhatsApp: solo sabe leer hojas de calculo. Sin esta pestana un aviso
# llega al telefono del asesor y a ningun otro lado -- ese es el motivo real
# de que Redes mande correos de prospecto y SECOM no. El encabezado replica
# el de la pestana CONVERSACIONES de Redes para que el monitor pueda leer
# ambas fuentes con la misma logica.
CONVERSACIONES_TAB = os.getenv("SHEETS_TITLE_CONVERSACIONES", "CONVERSACIONES").strip()
_CONVERSACIONES_HEADER = ["Phone", "Nombre", "Mensaje", "Fecha", "Tipo", "Origen"]
_conversaciones_tab_ready = False

# Disparo automatico del kill switch: N envios fallidos seguidos pausan la
# campana solos, sin esperar a que alguien llame /ext/boardroom/instruct a
# mano. Contador en memoria (no en Sheets): si el servicio reinicia entre
# fallos, el peor caso es tardar unos ciclos mas en detectar la racha, no
# perder la pausa ya activa (esa si vive en Sheets, ver arriba).
CAMPAIGN_FAILURE_THRESHOLD = int(os.getenv("CAMPAIGN_FAILURE_THRESHOLD", "3"))
_consecutive_send_failures = 0

PORT = int(os.getenv("PORT", "5000"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("vicky-secom")

if OPENAI_API_KEY and openai:
    try:
        openai.api_key = OPENAI_API_KEY
        log.info("OpenAI configurado correctamente")
    except Exception:
        log.warning("OpenAI configurado pero no disponible")


# ==========================
# Google setup degradable
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


# =================================
# Estado por usuario en memoria
# =================================
app = Flask(__name__)
user_state: Dict[str, str] = {}
user_data: Dict[str, Dict[str, Any]] = {}


# ==========================
# Constantes de router
# ==========================
ACTIVE_FUNNEL_PREFIXES = ("vida_", "imss_", "auto_", "tpv_", "emp_", "fp_")
ESCAPE_COMMANDS = {"menu", "menú", "inicio", "cancelar", "salir"}

# Campos que Boardroom/Vida puede actualizar sin abrir superficie de escritura arbitraria.
VIDA_SHEET_FIELDS = {
    "ESTATUS",
    "PRODUCTO",
    "ULTIMO_CONTACTO",
    "NOTAS",
    "BENEFICIO_OFRECIDO",
    "LAST_MESSAGE",
    "LAST_MESSAGE_AT",
}

TPV_TEMPLATE_NAME = "promo_tpv"
ALLIANCE_TEMPLATES = {"despachis_contables"}

SECOM_VIDA_TEMPLATES = {"vida_inbursa_proveedor_v1", "vida_temporal"}

TEMPLATE_IMAGE_ENV = {
    "seguro_auto_70": "SEGURO_AUTO_70_IMAGE_URL",
    "vida_temporal": "VIDA_TEMPORAL_IMAGE_URL",
}

# Idioma por defecto de las plantillas aprobadas en Meta. El cron puede
# sobrescribirlo por campana con el campo "language"; no se mantiene un
# idioma por plantilla dentro del codigo.
DEFAULT_TEMPLATE_LANGUAGE = "es_MX"

# Formato permitido para el codigo de idioma (es_MX, en, pt_BR, ...).
_LANGUAGE_CODE_RE = re.compile(r"^[a-zA-Z]{2,3}(_[a-zA-Z]{2,4})?$")

# Formato permitido para success_status: solo mayusculas, digitos y "_".
_SUCCESS_STATUS_RE = re.compile(r"^[A-Z0-9_]{1,64}$")


TEMPLATE_INTEREST_WORDS = {
    "si",
    "sí",
    "s",
    "ok",
    "claro",
    "me interesa",
    "info",
    "informacion",
    "información",
    "mas info",
    "más info",
    "contrata hoy",
    "contratar",
    "quiero contratar",
    "me interesa contratar",
    "lo quiero",
    "vrim10",
}

AWAITING_TEMPLATE_RECOVERABLE_STATUSES = {
    "ENVIADO_INICIAL",
    "ENVIADO_TEMPLATE",
    "ENVIADO_VRIM",
    "ENVIADO_VIDA_TEMPORAL",
}


# ==========================
# Utilidades generales
# ==========================
WPP_API_URL = f"https://graph.facebook.com/v20.0/{WABA_PHONE_ID}/messages" if WABA_PHONE_ID else None
WPP_TIMEOUT = 15

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


def _utc_now_iso() -> str:
    return datetime.utcnow().isoformat()


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


def interpret_response(text: str) -> str:
    if not text:
        return "neutral"
    t = text.lower().strip()
    pos = ("sí", "si", "claro", "ok", "de acuerdo", "vale", "afirmativo", "correcto", "me interesa")
    neg = ("no", "nel", "nop", "negativo", "no quiero", "no gracias", "no interesa")
    if any(p in t for p in pos):
        return "positive"
    if any(n in t for n in neg):
        return "negative"
    return "neutral"


def extract_number(text: str) -> Optional[float]:
    if not text:
        return None
    clean = (
        text.lower()
        .replace(",", "")
        .replace("$", "")
        .replace("millón", "000000")
        .replace("millon", "000000")
        .replace("millones", "000000")
    )
    match = re.search(r"(\d{1,12}(?:\.\d+)?)", clean)
    try:
        return float(match.group(1)) if match else None
    except Exception:
        return None


def _ensure_user(phone: str) -> Dict[str, Any]:
    if phone not in user_data:
        user_data[phone] = {}
    return user_data[phone]


def _is_active_funnel_state(state: str | None) -> bool:
    return isinstance(state, str) and state.strip().startswith(ACTIVE_FUNNEL_PREFIXES)


def _is_funnel_exit_command(text: str | None) -> bool:
    return (text or "").strip().lower() in ESCAPE_COMMANDS


def _should_continue_active_funnel(state: str | None, text: str | None) -> bool:
    return _is_active_funnel_state(state) and not _is_funnel_exit_command(text)


# ==========================
# WhatsApp helpers
# ==========================
def _wpp_headers() -> Dict[str, str]:
    return {"Authorization": f"Bearer {META_TOKEN}", "Content-Type": "application/json"}


def _should_retry(status: int) -> bool:
    return status == 429 or 500 <= status < 600


def _backoff(attempt: int) -> None:
    time.sleep(2**attempt)


def send_message(to: str, text: str) -> bool:
    """Envía mensaje de texto WPP dentro de conversación activa."""
    if not (META_TOKEN and WPP_API_URL):
        log.error("❌ WhatsApp no configurado (META_TOKEN/WABA_PHONE_ID faltan).")
        return False

    payload = {
        "messaging_product": "whatsapp",
        "to": str(to),
        "type": "text",
        "text": {"body": str(text or "")[:4096]},
    }

    for attempt in range(3):
        try:
            log.info("📤 Enviando mensaje a %s (intento %s)", to, attempt + 1)
            resp = requests.post(WPP_API_URL, headers=_wpp_headers(), json=payload, timeout=WPP_TIMEOUT)
            if resp.status_code in (200, 201):
                log.info("✅ Mensaje enviado exitosamente a %s", to)
                return True
            log.warning("⚠️ WPP send_message falló %s: %s", resp.status_code, resp.text[:200])
            if _should_retry(resp.status_code) and attempt < 2:
                _backoff(attempt)
                continue
            return False
        except requests.exceptions.Timeout:
            log.error("⏰ Timeout enviando mensaje a %s (intento %s)", to, attempt + 1)
            if attempt < 2:
                _backoff(attempt)
                continue
            return False
        except Exception:
            log.exception("❌ Error en send_message a %s", to)
            if attempt < 2:
                _backoff(attempt)
                continue
            return False
    return False


def send_template_message(
    to: str,
    template_name: str,
    params: Dict[str, Any] | List[Any] | None = None,
    image_url: Optional[str] = None,
    components: Optional[List[Dict[str, Any]]] = None,
    language: str = DEFAULT_TEMPLATE_LANGUAGE,
) -> bool:
    """Envía plantilla Meta aprobada.

    Reglas:
    - template_name siempre es obligatorio.
    - params es opcional; si no viene, NO se mandan parámetros de body.
      dict => parámetros nombrados (parameter_name), para plantillas con
      variables tipo {{nombre}}. list => parámetros posicionales.
    - image_url es opcional; si viene, se manda como header image.
    - components permite enviar componentes Meta completos cuando la plantilla lo requiera.
    - language es el código de idioma de la plantilla aprobada en Meta.

    Un rechazo de Meta NO se "auto-repara" reintentando con un payload
    distinto: la estructura de cada plantilla la declara quien llama
    (el cron), no se deduce de los mensajes de error.
    """
    if not (META_TOKEN and WPP_API_URL):
        log.error("❌ WhatsApp no configurado para plantillas.")
        return False

    template_name = str(template_name or "").strip()
    if not template_name:
        log.error("❌ template_name vacío")
        return False

    if components is not None and not isinstance(components, list):
        log.error("❌ components inválido para plantilla %s; debe ser lista.", template_name)
        return False

    built_components: List[Dict[str, Any]] = []

    if components is not None:
        built_components = components
    else:
        final_image_url = str(image_url or "").strip()

        if not final_image_url:
            img_env = TEMPLATE_IMAGE_ENV.get(template_name)
            if img_env:
                final_image_url = os.getenv(img_env, "").strip()
                if not final_image_url:
                    log.error("❌ Falta %s en entorno para plantilla %s.", img_env, template_name)
                    return False

        if final_image_url:
            if not final_image_url.startswith(("https://", "http://")):
                log.error("❌ image_url inválida para plantilla %s.", template_name)
                return False
            built_components.append({
                "type": "header",
                "parameters": [{"type": "image", "image": {"link": final_image_url}}],
            })

        if params is not None:
            if isinstance(params, dict):
                body_params = [
                    {"type": "text", "parameter_name": str(k), "text": str(v)}
                    for k, v in params.items()
                ]
            elif isinstance(params, list):
                body_params = [{"type": "text", "text": str(v)} for v in params]
            else:
                log.error("❌ params inválido para plantilla %s; debe ser dict, list o null.", template_name)
                return False

            if body_params:
                built_components.append({"type": "body", "parameters": body_params})

    payload = {
        "messaging_product": "whatsapp",
        "to": str(to),
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language},
            **({"components": built_components} if built_components else {}),
        },
    }

    for attempt in range(3):
        try:
            log.info("📤 Enviando plantilla '%s' a %s (intento %s)", template_name, to, attempt + 1)
            resp = requests.post(WPP_API_URL, headers=_wpp_headers(), json=payload, timeout=WPP_TIMEOUT)
            if resp.status_code in (200, 201):
                message_id = ""
                try:
                    data = resp.json() if resp.text else {}
                    messages = data.get("messages") or []
                    if messages:
                        message_id = (messages[0] or {}).get("id", "")
                except Exception:
                    message_id = ""
                try:
                    append_envio_status(str(to), message_id, "sent", template_name, _utc_now_iso())
                except Exception:
                    pass
                log.info("✅ Plantilla '%s' enviada exitosamente a %s", template_name, to)
                return True

            log.warning("⚠️ WPP send_template falló %s: %s", resp.status_code, resp.text[:500])
            if _should_retry(resp.status_code) and attempt < 2:
                _backoff(attempt)
                continue
            return False
        except requests.exceptions.Timeout:
            log.error("⏰ Timeout enviando plantilla a %s (intento %s)", to, attempt + 1)
            if attempt < 2:
                _backoff(attempt)
                continue
            return False
        except Exception:
            log.exception("❌ Error en send_template_message a %s", to)
            if attempt < 2:
                _backoff(attempt)
                continue
            return False
    return False

def forward_media_to_advisor(media_type: str, media_id: str) -> None:
    if not (META_TOKEN and WPP_API_URL and ADVISOR_NUMBER and media_id):
        return
    payload = {
        "messaging_product": "whatsapp",
        "to": ADVISOR_NUMBER,
        "type": media_type,
        media_type: {"id": media_id},
    }
    try:
        requests.post(WPP_API_URL, headers=_wpp_headers(), json=payload, timeout=WPP_TIMEOUT)
        log.info("📤 Multimedia reenviada al asesor (%s)", media_type)
    except Exception:
        log.exception("❌ Error reenviando multimedia al asesor")


# ==========================
# Google helpers
# ==========================
def _sheet_get_rows() -> Tuple[List[str], List[List[str]]]:
    if not (google_ready and sheets_svc and SHEETS_ID_LEADS and SHEETS_TITLE_LEADS):
        raise RuntimeError("Sheets no disponible (google_ready/SHEETS_ID_LEADS/SHEETS_TITLE_LEADS).")
    rng = f"{SHEETS_TITLE_LEADS}!A:Z"
    values = sheets_svc.spreadsheets().values().get(spreadsheetId=SHEETS_ID_LEADS, range=rng).execute()
    rows = values.get("values", [])
    if not rows:
        return [], []
    headers = [str(h).strip() for h in rows[0]]
    return headers, rows[1:]


def _idx(headers: List[str], name: str) -> Optional[int]:
    target = name.strip().lower()
    for i, header in enumerate(headers):
        if (header or "").strip().lower() == target:
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
    for col_name, value in (updates or {}).items():
        j = _idx(headers, col_name)
        if j is None:
            raise RuntimeError(f"No existe columna '{col_name}' en el Sheet.")
        col_letter = chr(ord("A") + j)
        a1 = f"{SHEETS_TITLE_LEADS}!{col_letter}{row_number_1based}"
        data.append({"range": a1, "values": [[value]]})
    if not data:
        return
    body = {"valueInputOption": "USER_ENTERED", "data": data}
    sheets_svc.spreadsheets().values().batchUpdate(spreadsheetId=SHEETS_ID_LEADS, body=body).execute()


def _ensure_tab(title: str, header: list, filas: int = 500) -> None:
    """Crea la pestana `title` si falta y escribe su encabezado.

    Mismo criterio que _sheets_ensure_tab() del bot de Redes: la hoja se
    prepara sola en vez de depender de que alguien la haya armado a mano.

    La cuadricula se pide EXPLICITA y pequena. Por defecto Sheets crea cada
    pestana con 1000x26 = 26.000 celdas, y este libro ya esta rozando el
    tope de 10.000.000 de celdas por libro que impone Google: con el tamano
    por defecto, addSheet respondia HTTP 400 ("would increase the number of
    cells in the workbook above the limit") y la pestana no se creaba nunca.
    """
    meta = sheets_svc.spreadsheets().get(
        spreadsheetId=SHEETS_ID_LEADS, fields="sheets.properties.title"
    ).execute()
    existentes = {
        (hoja.get("properties") or {}).get("title")
        for hoja in (meta.get("sheets") or [])
    }
    if title in existentes:
        return

    sheets_svc.spreadsheets().batchUpdate(
        spreadsheetId=SHEETS_ID_LEADS,
        body={"requests": [{"addSheet": {"properties": {
            "title": title,
            "gridProperties": {
                "rowCount": max(int(filas), 2),
                "columnCount": max(len(header), 1),
            },
        }}}]},
    ).execute()
    if header:
        ultima = chr(ord("A") + len(header) - 1)
        sheets_svc.spreadsheets().values().update(
            spreadsheetId=SHEETS_ID_LEADS,
            range=f"{title}!A1:{ultima}1",
            valueInputOption="RAW",
            body={"values": [header]},
        ).execute()
    log.info("✅ Pestana '%s' creada en la hoja de leads", title)


def _ensure_control_tab() -> None:
    """Crea la pestana de control del kill switch si no existe.

    Sin esto el freno depende de que alguien haya preparado la hoja a mano,
    que es exactamente como se rompio antes. Se salta el trabajo despues de
    la primera verificacion exitosa del proceso.
    """
    global _control_tab_ready
    if _control_tab_ready:
        return
    # 10x2 = 20 celdas: el interruptor no crece nunca.
    _ensure_tab(CAMPAIGN_CONTROL_TAB, _CAMPAIGN_CONTROL_HEADER, filas=10)
    _control_tab_ready = True


def _ensure_conversaciones_tab() -> None:
    """Crea la bitacora de avisos al asesor si no existe."""
    global _conversaciones_tab_ready
    if _conversaciones_tab_ready:
        return
    # Bitacora que si crece; Sheets la extiende sola al hacer append.
    _ensure_tab(CONVERSACIONES_TAB, _CONVERSACIONES_HEADER, filas=500)
    _conversaciones_tab_ready = True


def _log_conversacion(mensaje: str) -> None:
    """Registra en Sheets cada aviso enviado al asesor.

    Es el eslabon que le faltaba a SECOM: el monitor de correos solo lee
    hojas, asi que un aviso que unicamente viaja por WhatsApp le resulta
    invisible.

    Sincrono a proposito. sheets_svc (httplib2) no es seguro entre hilos y
    todo el resto del archivo lo usa desde el hilo de la peticion; se sigue
    el patron ya probado en produccion (append_respuesta_cliente) en vez de
    estrenar el primer acceso concurrente al cliente de Sheets. Nunca
    propaga: un fallo de bitacora no puede tumbar un aviso.
    """
    if not (google_ready and sheets_svc and SHEETS_ID_LEADS):
        return
    try:
        _ensure_conversaciones_tab()
        sheets_svc.spreadsheets().values().append(
            spreadsheetId=SHEETS_ID_LEADS,
            range=f"{CONVERSACIONES_TAB}!A:F",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [[
                re.sub("[^0-9]", "", str(ADVISOR_NUMBER or "")),
                "Asesor",
                str(mensaje or "")[:500],
                _utc_now_iso(),
                "saliente",
                "asesor",
            ]]},
        ).execute()
    except Exception:
        log.exception("⚠️ No se pudo registrar el aviso al asesor en Sheets")


def _is_campaign_paused() -> bool:
    """CF-4: lee el kill switch de la campana outbound desde Sheets."""
    if not (google_ready and sheets_svc and SHEETS_ID_LEADS):
        return False
    try:
        _ensure_control_tab()
        rng = f"{CAMPAIGN_CONTROL_TAB}!{CAMPAIGN_PAUSE_CELL}"
        result = sheets_svc.spreadsheets().values().get(
            spreadsheetId=SHEETS_ID_LEADS, range=rng
        ).execute()
        values = result.get("values", [])
        cell = (values[0][0] if values and values[0] else "").strip().upper()
        pausada = cell == CAMPAIGN_PAUSED_VALUE
        # Sin esta linea el kill switch es una caja negra: devuelve False sin
        # decir que leyo, asi que un "no se pausa" no se puede distinguir de
        # un "leyo otra celda". Con campana viva hay que poder auditarlo en
        # caliente, sin desplegar de nuevo.
        log.info(
            "kill switch CF-4: rango=%s crudo=%r normalizado=%r pausada=%s",
            rng, values, cell, pausada,
        )
        return pausada
    except Exception:
        log.exception("❌ Error leyendo kill switch de campana (CF-4); fail-open")
        return False


def _set_campaign_paused(paused: bool) -> None:
    """CF-4: escribe el kill switch de la campana outbound en Sheets."""
    if not (google_ready and sheets_svc and SHEETS_ID_LEADS):
        raise RuntimeError("Sheets no disponible para actualizar kill switch.")
    _ensure_control_tab()
    rng = f"{CAMPAIGN_CONTROL_TAB}!{CAMPAIGN_PAUSE_CELL}"
    value = CAMPAIGN_PAUSED_VALUE if paused else ""
    body = {"values": [[value]]}
    sheets_svc.spreadsheets().values().update(
        spreadsheetId=SHEETS_ID_LEADS,
        range=rng,
        valueInputOption="USER_ENTERED",
        body=body,
    ).execute()


def _register_send_result(ok: bool) -> bool:
    """
    Lleva la cuenta de envios fallidos consecutivos de la campana outbound.

    Un exito resetea el contador. Al llegar a CAMPAIGN_FAILURE_THRESHOLD
    fallos seguidos, pausa la campana sola (mismo kill switch que
    /ext/boardroom/instruct) y notifica a Boardroom por el bus para que
    quede en el Ledger -- Boardroom gobierna, debe enterarse aunque la
    pausa la ejecute vicky-bot localmente por velocidad/confiabilidad.

    Devuelve True solo en el ciclo donde se dispara la pausa (para loguear
    y para que el caller pueda reflejarlo en la respuesta del endpoint).
    """
    global _consecutive_send_failures

    if ok:
        _consecutive_send_failures = 0
        return False

    _consecutive_send_failures += 1
    if _consecutive_send_failures < CAMPAIGN_FAILURE_THRESHOLD:
        return False

    _consecutive_send_failures = 0
    try:
        _set_campaign_paused(True)
    except Exception:
        log.exception("❌ No se pudo aplicar auto-pausa tras racha de fallos")
        return False

    log.warning(
        "⏸️ Campana outbound auto-pausada: %s envios fallidos seguidos",
        CAMPAIGN_FAILURE_THRESHOLD,
    )
    _emit_bus_event(
        "system",
        f"Campana outbound auto-pausada tras {CAMPAIGN_FAILURE_THRESHOLD} envios fallidos seguidos",
        event_type="campaign_auto_paused",
        metadata={"threshold": CAMPAIGN_FAILURE_THRESHOLD, "source": "vicky_secom"},
    )
    return True


def _safe_update_row_cells(
    row_number_1based: int,
    updates: Dict[str, str],
    allowed_fields: Optional[set[str]] = None,
) -> None:
    try:
        headers, _ = _sheet_get_rows()
        if not headers:
            log.warning("⚠️ Sheets sin headers; no se actualizaron campos")
            return

        filtered: Dict[str, str] = {}
        for key, value in (updates or {}).items():
            if allowed_fields and key not in allowed_fields:
                log.warning("⚠️ Campo no permitido para update Sheets: %s", key)
                continue
            if _idx(headers, key) is None:
                log.warning("⚠️ Columna '%s' no existe en el Sheet; se omite", key)
                continue
            filtered[key] = str(value)

        if filtered:
            _update_row_cells(int(row_number_1based), filtered, headers)
    except Exception:
        log.exception("⚠️ No fue posible actualizar Sheets; continúa flujo")


def match_client_in_sheets(phone_last10: str) -> Optional[Dict[str, Any]]:
    if not (google_ready and sheets_svc and SHEETS_ID_LEADS and SHEETS_TITLE_LEADS):
        log.warning("⚠️ Sheets no disponible; no se puede hacer matching.")
        return None
    try:
        headers, rows = _sheet_get_rows()
        i_name = _idx(headers, "Nombre")
        i_wa = _idx(headers, "WhatsApp")
        i_status = _idx(headers, "ESTATUS")
        i_last = _idx(headers, "LAST_MESSAGE_AT")

        if i_wa is None:
            log.warning("⚠️ No existe columna 'WhatsApp' en el Sheet.")
            return None

        target = str(phone_last10).strip()
        for row_number, row in enumerate(rows, start=2):
            if target and _normalize_phone_last10(_cell(row, i_wa)) == target:
                nombre = _cell(row, i_name).strip() if i_name is not None else ""
                estatus = _cell(row, i_status).strip() if i_status is not None else ""
                last_at = _cell(row, i_last).strip() if i_last is not None else ""
                log.info("✅ Cliente encontrado en Sheets: %s (%s)", nombre, target)
                return {"row": row_number, "nombre": nombre, "estatus": estatus, "last_message_at": last_at, "raw": row}

        log.info("ℹ️ Cliente no encontrado en Sheets: %s", target)
        return None
    except Exception:
        log.exception("❌ Error buscando en Sheets")
        return None


def append_envio_status(phone: str, message_id: str, status: str, template_name: str, timestamp_iso: str) -> None:
    if not (google_ready and sheets_svc and SHEETS_ID_LEADS):
        return
    try:
        body = {"values": [[_normalize_phone_last10(phone), message_id or "", status or "", timestamp_iso or "", template_name or ""]]}
        sheets_svc.spreadsheets().values().append(
            spreadsheetId=SHEETS_ID_LEADS,
            range="ENVIO_STATUS!A:E",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body=body,
        ).execute()
    except Exception:
        log.exception("❌ Error escribiendo ENVIO_STATUS")


def append_respuesta_cliente(phone: str, nombre: str, mensaje: str, fecha_iso: str) -> None:
    if not (google_ready and sheets_svc and SHEETS_ID_LEADS):
        return
    try:
        body = {"values": [[_normalize_phone_last10(phone), nombre or "", mensaje or "", fecha_iso or ""]]}
        sheets_svc.spreadsheets().values().append(
            spreadsheetId=SHEETS_ID_LEADS,
            range="RESPUESTAS_CLIENTE!A:D",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body=body,
        ).execute()
    except Exception:
        log.exception("❌ Error escribiendo RESPUESTAS_CLIENTE")


def write_followup_to_sheets(row: int | str, note: str, date_iso: str) -> None:
    if not (google_ready and sheets_svc and SHEETS_ID_LEADS):
        log.warning("⚠️ Sheets no disponible; no se puede escribir seguimiento.")
        return
    try:
        body = {"values": [[str(row), date_iso, note]]}
        sheets_svc.spreadsheets().values().append(
            spreadsheetId=SHEETS_ID_LEADS,
            range="Seguimiento!A:C",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body=body,
        ).execute()
        log.info("✅ Seguimiento registrado en Sheets: %s", note)
    except Exception:
        log.exception("❌ Error escribiendo seguimiento en Sheets")


def get_last_envio_template(phone_last10: str) -> str:
    if not (google_ready and sheets_svc and SHEETS_ID_LEADS):
        return ""
    try:
        resp = sheets_svc.spreadsheets().values().get(
            spreadsheetId=SHEETS_ID_LEADS,
            range="ENVIO_STATUS!A:E",
        ).execute()
        values = resp.get("values") or []
        target = (phone_last10 or "").strip()
        for row in reversed(values[1:]):
            if len(row) >= 1 and _normalize_phone_last10(row[0]) == target:
                return (row[4] if len(row) >= 5 else "").strip()
    except Exception:
        log.exception("❌ Error leyendo ENVIO_STATUS")
    return ""


def _find_or_create_client_folder(folder_name: str) -> Optional[str]:
    if not (google_ready and drive_svc and DRIVE_PARENT_FOLDER_ID):
        log.warning("⚠️ Drive no disponible; no se puede crear carpeta.")
        return None
    try:
        safe_name = folder_name.replace("'", "\\'")
        q = (
            f"name = '{safe_name}' and mimeType = 'application/vnd.google-apps.folder' "
            f"and '{DRIVE_PARENT_FOLDER_ID}' in parents and trashed = false"
        )
        resp = drive_svc.files().list(q=q, fields="files(id, name)").execute()
        items = resp.get("files", [])
        if items:
            return items[0]["id"]
        created = drive_svc.files().create(
            body={
                "name": folder_name,
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [DRIVE_PARENT_FOLDER_ID],
            },
            fields="id",
        ).execute()
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
        created = drive_svc.files().create(
            body={"name": file_name, "parents": [folder_id]},
            media_body=media,
            fields="id, webViewLink",
        ).execute()
        return created.get("webViewLink") or created.get("id")
    except Exception:
        log.exception("❌ Error subiendo archivo a Drive")
        return None


# ==========================
# Menú y asesor
# ==========================
def send_main_menu(phone: str) -> None:
    log.info("📋 Enviando menú principal a %s", phone)
    send_message(phone, MAIN_MENU)


def _notify_advisor(text: str) -> None:
    try:
        log.info("👨‍💼 Notificando al asesor: %s", text)
        if ADVISOR_NUMBER:
            send_message(ADVISOR_NUMBER, text)
    except Exception:
        log.exception("❌ Error notificando al asesor")
    finally:
        # En `finally` a proposito: si el envio por WhatsApp fallo, dejar
        # rastro del aviso importa mas, no menos.
        _log_conversacion(text)


def _match_name(match: Optional[Dict[str, Any]]) -> str:
    return ((match or {}).get("nombre") or "").strip()


# ==========================
# Contextos post-campaña
# ==========================
def _parse_dt_maybe(value: str) -> Optional[datetime]:
    if not value:
        return None
    value = value.strip()
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return datetime.fromisoformat(value)
    except Exception:
        return None


def _within_24h(value: str) -> bool:
    dt = _parse_dt_maybe(value or "")
    if not dt:
        return False
    now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.utcnow()
    return (now - dt) <= timedelta(hours=24)


def _tpv_is_context(match: Optional[Dict[str, Any]]) -> bool:
    if not match:
        return False
    if (match.get("estatus") or "").strip().upper() != "ENVIADO_TPV":
        return False
    return _within_24h(match.get("last_message_at") or "")


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


def _alianza_is_context(match: Optional[Dict[str, Any]]) -> bool:
    if not match:
        return False
    if (match.get("estatus") or "").strip().upper() != "ENVIADO_ALIANZA":
        return False
    return _within_24h(match.get("last_message_at") or "")


def _explicit_non_alianza_intent(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return False
    alianza_signals = ("alianza", "despacho", "contable", "contables", "contador", "contadores", "comision", "comisión")
    if any(k in t for k in alianza_signals):
        return False
    other_signals = (
        "auto", "seguro", "póliza", "poliza", "tpv", "terminal", "imss", "ley 73",
        "prestamo", "préstamo", "credito", "crédito", "vida", "salud", "vrim",
    )
    return any(k in t for k in other_signals)


def _handle_alianza_context_response(phone: str, text: str, match: Dict[str, Any]) -> bool:
    if user_state.get(phone, "") not in ("", "__greeted__"):
        return False
    if not _alianza_is_context(match):
        return False
    if _explicit_non_alianza_intent(text):
        log.info("🔀 Escape ALIANZA→Router por intención explícita: %s", text)
        return False
    nombre = (match.get("nombre") or "").strip() or "Cliente"
    _notify_advisor(
        "🤝 ALIANZA — Interés/Respuesta detectada\n"
        f"WhatsApp: {phone}\n"
        f"Nombre: {nombre}\n"
        f"Mensaje: {(text or '').strip()}"
    )
    send_message(
        phone,
        "✅ Gracias. Ya tengo tu interés en la *alianza para despachos contables*.\n"
        "En breve te comparto la información y un asesor te contactará.\n"
        "Para avanzar: ¿cómo se llama tu despacho y en qué ciudad estás?",
    )
    user_state[phone] = "__greeted__"
    return True


def _auto_is_context(match: Optional[Dict[str, Any]]) -> bool:
    if not match:
        return False
    estatus = (match.get("estatus") or "").strip().upper()
    if estatus not in {"ENVIADO_AUTO", "ENVIADO_SEGURO_AUTO"}:
        return False
    return _within_24h(match.get("last_message_at") or "")


def _explicit_non_auto_intent(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return False
    auto_signals = ("auto", "seguro auto", "seguro de auto", "póliza", "poliza", "placa", "placas")
    if any(k in t for k in auto_signals):
        return False
    other_signals = (
        "credito", "crédito", "prestamo", "préstamo", "imss", "ley 73", "empresarial",
        "pyme", "tpv", "terminal", "vida", "salud", "vrim", "financiamiento",
    )
    return any(k in t for k in other_signals)


def _handle_auto_context_response(phone: str, text: str, match: Dict[str, Any]) -> bool:
    t = (text or "").strip().lower()
    intent = interpret_response(text)
    if user_state.get(phone, "") not in ("", "__greeted__"):
        return False
    if not _auto_is_context(match):
        return False
    if _explicit_non_auto_intent(text):
        log.info("🔀 Escape AUTO→Router por intención explícita: %s", text)
        return False

    nombre = (match.get("nombre") or "").strip() or "Cliente"
    if t in ("1", "si", "sí", "ok", "claro") or intent == "positive":
        auto_start(phone, match)
        return True

    if t in ("2", "no", "nel") or intent == "negative":
        user_state[phone] = "auto_vencimiento_fecha"
        send_message(phone, f"Entendido {nombre}. Para poder recordarte a tiempo, ¿cuál es la *fecha de vencimiento* de tu póliza? (formato AAAA-MM-DD)")
        _notify_advisor(
            "🔔 AUTO — NO INTERESADO / TIENE SEGURO\n"
            f"WhatsApp: {phone}\n"
            f"Nombre: {nombre}\n"
            f"Respuesta: {text}"
        )
        return True

    if t in ("menu", "menú", "inicio"):
        user_state[phone] = "__greeted__"
        send_main_menu(phone)
        return True

    _notify_advisor(
        "📩 AUTO — DUDA / INTERÉS detectada\n"
        f"WhatsApp: {phone}\n"
        f"Nombre: {nombre}\n"
        f"Mensaje: {text}"
    )
    send_message(phone, "¿Deseas cotizar tu seguro de auto ahora? Responde *Sí* o *No*")
    return True


# ==========================
# Boardroom
# ==========================
def _normalize_boardroom_url(url: str) -> str:
    candidate = (url or "").strip()
    if not candidate:
        return ""
    for dead_path in ("/boardroom/decision/process", "/api/decision/process"):
        if dead_path in candidate:
            candidate = candidate.replace(dead_path, BOARDROOM_ORCHESTRATE_PATH)
    if candidate.endswith("/"):
        candidate = candidate[:-1]
    if candidate.startswith("http://") or candidate.startswith("https://"):
        if BOARDROOM_ORCHESTRATE_PATH in candidate:
            return candidate
        if re.match(r"^https?://[^/]+$", candidate):
            return f"{candidate}{BOARDROOM_ORCHESTRATE_PATH}"
        return candidate
    return ""


def _infer_product_hint(text: str) -> str:
    t = (text or "").strip().lower()
    if t in ("3",) or any(k in t for k in ("vida", "vida temporal", "seguro de vida", "protección familiar", "proteccion familiar")):
        return "vida_temporal"
    if any(k in t for k in ("auto", "seguro auto", "placas", "póliza", "poliza")):
        return "auto"
    if any(k in t for k in ("tpv", "terminal", "punto de venta")):
        return "tpv"
    if any(k in t for k in ("imss", "ley 73", "pensión", "pension")):
        return "imss"
    if any(k in t for k in ("empresarial", "pyme")):
        return "empresarial"
    return "unknown"


def send_to_boardroom(phone: str, text: str, match: Optional[Dict[str, Any]] = None, message_id: Optional[str] = None, state: Optional[str] = None) -> dict:
    url = _normalize_boardroom_url(BOARDROOM_DECISION_URL)
    if not (BOARDROOM_ENABLED and url and BOARDROOM_AUTH_TOKEN):
        log.info("⚠️ Boardroom unavailable; fallback local")
        return {"ok": False, "handled": False, "reason": "not_configured"}

    payload = {
        "source": "vicky_secom",
        "channel": "whatsapp",
        "phone": phone or "",
        "message": text or "",
        "message_id": message_id or "",
        "state": state or "",
        "priority": "commercial",
        "product_hint": _infer_product_hint(text),
        "metadata": {
            "match_found": bool(match),
            "lead_name": _match_name(match),
            "sheet_row": str((match or {}).get("row") or ""),
            "service": "vicky-bot-secom",
        },
    }
    headers = {
        "X-Boardroom-Token": BOARDROOM_AUTH_TOKEN,
        "Authorization": f"Bearer {BOARDROOM_AUTH_TOKEN}",
        "Content-Type": "application/json",
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=3)
        log.info("🧠 Boardroom request enviado")
        if resp.status_code >= 400:
            return {"ok": False, "handled": False, "reason": f"http_{resp.status_code}"}
        data = resp.json() if resp.text else {}
        return data if isinstance(data, dict) else {"ok": False, "handled": False, "reason": "invalid_response"}
    except requests.exceptions.Timeout:
        return {"ok": False, "handled": False, "reason": "timeout"}
    except Exception:
        log.exception("⚠️ Boardroom unavailable; fallback local")
        return {"ok": False, "handled": False, "reason": "exception"}


def _emit_bus_event(
    phone: str,
    text: str,
    event_type: str = "inbound_message",
    template_name: Optional[str] = None,
    intent: Optional[str] = None,
    metadata: Optional[dict] = None
) -> None:
    if not _BUS_ACTIVE:
        return
    if not BUS_URL or not BUS_INTERNAL_TOKEN:
        log.warning(
            "BUS_URL o BUS_INTERNAL_TOKEN no configurados — emit omitido"
        )
        return

    payload: Dict[str, Any] = {
        "source": "vicky_secom",
        "event_type": event_type,
        "telefono": phone,
        "mensaje": text or "",
        "timestamp": _utc_now_iso(),
    }
    if template_name:
        payload["template_name"] = template_name
    if intent:
        payload["intent"] = intent
    if metadata:
        payload["metadata"] = metadata

    def _post() -> None:
        try:
            requests.post(
                BUS_URL,
                json=payload,
                headers={
                    "Authorization": f"Bearer {BUS_INTERNAL_TOKEN}",
                    "Content-Type": "application/json",
                },
                timeout=3,
            )
        except Exception as exc:
            log.warning(
                "Bus emit fallido phone_last4=%s error=%s: %s",
                str(phone)[-4:],
                type(exc).__name__,
                str(exc),
            )

    threading.Thread(target=_post, daemon=True).start()


def _bus_event_url() -> str:
    url = (BUS_URL or "").strip().rstrip("/")
    if not url:
        return ""
    if url.endswith("/bus/event"):
        return url
    return f"{url}/bus/event"


def _bus_confirm_url() -> str:
    url = _bus_event_url()
    if not url:
        return ""
    return f"{url}/confirm"


def _message_text(msg: Dict[str, Any], mtype: str) -> str:
    if mtype == "text":
        return ((msg.get("text") or {}).get("body") or "").strip()[:500]
    if mtype == "button":
        btn = msg.get("button") or {}
        return (btn.get("text") or btn.get("payload") or "").strip()[:500]
    return ""


def _canonical_message_type(mtype: str) -> str:
    return mtype if mtype in {"text", "audio", "image", "document", "button"} else "unknown"


def _attachments_for_message(msg: Dict[str, Any], mtype: str) -> List[Dict[str, Any]]:
    media = msg.get(mtype) or {}
    media_id = media.get("id")
    if mtype in {"image", "document", "audio"} and media_id:
        return [{"type": mtype, "media_id": media_id}]
    return []


def _build_boardroom_event(
    phone: str,
    text: str,
    msg: Dict[str, Any],
    mtype: str,
    match: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    state = user_state.get(phone, "")
    data = user_data.get(phone, {})
    return {
        "event_id": str(uuid.uuid4()),
        "message_id": msg.get("id", ""),
        "timestamp": f"{_utc_now_iso()}Z",
        "source": "whatsapp",
        "channel": "vicky_secom",
        "phone": phone,
        "contact_name": _match_name(match) or None,
        "text": text or "",
        "message_type": _canonical_message_type(mtype),
        "campaign": {
            "source": "whatsapp",
            "campaign_id": None,
            "ad_id": None,
            "product_hint": "unknown",
        },
        "conversation": {
            "conversation_id": f"vicky_secom:{phone}",
            "last_known_stage": state or None,
            "last_bot_message": data.get("last_bot_message") or None,
        },
        "attachments": _attachments_for_message(msg, mtype),
        "metadata": {
            "raw_payload_available": True,
            "vicky_version": "vicky-bot-1342-phase1",
            "environment": "production",
        },
    }


def _parse_boardroom_instruction(body: object, event_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not isinstance(body, dict):
        return None, "invalid_json"
    status = body.get("status")
    if status not in {"ok", "fallback", "error"}:
        return None, "invalid_status"
    if body.get("event_id") and body.get("event_id") != event_id:
        log.warning("Boardroom event_id mismatch sent=%s got=%s", event_id, body.get("event_id"))
    instruction = body.get("instruction")
    if not isinstance(instruction, dict):
        return None, "missing_instruction"
    instruction_type = str(instruction.get("type") or "").strip()
    if instruction_type not in _BOARDROOM_ALLOWED_INSTRUCTIONS:
        log.error("Boardroom instruction type not allowed: %s", instruction_type)
        return None, "invalid_instruction_type"
    return body, None


def _request_boardroom_instruction(payload: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not _BUS_ACTIVE or not BUS_URL:
        return None, "bus_disabled_or_empty"
    if not BUS_INTERNAL_TOKEN:
        return None, "missing_bus_token"
    try:
        resp = requests.post(
            _bus_event_url(),
            json=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {BUS_INTERNAL_TOKEN}",
                "X-Source-System": "vicky",
                "X-Event-Type": "inbound_message",
            },
            timeout=3,
        )
        if resp.status_code >= 400:
            return None, f"http_{resp.status_code}"
        body = resp.json() if resp.text else {}
        return _parse_boardroom_instruction(body, payload["event_id"])
    except requests.exceptions.Timeout:
        return None, "timeout"
    except Exception as exc:
        log.warning("Boardroom bus request failed: %s: %s", type(exc).__name__, exc)
        return None, "exception"


def _instruction_message(instruction: Dict[str, Any]) -> str:
    message = str(instruction.get("message") or "").strip()
    options = instruction.get("options")
    if instruction.get("type") == "send_options" and isinstance(options, list) and options:
        labels = []
        for idx, option in enumerate(options, start=1):
            if isinstance(option, dict) and option.get("label"):
                labels.append(f"{idx}. {option['label']}")
        if labels:
            return "\n".join([message, *labels]).strip()
    return message


def _execute_boardroom_instruction(phone: str, body: Dict[str, Any]) -> Tuple[bool, str, Optional[str]]:
    instruction = body.get("instruction") or {}
    instruction_type = instruction.get("type")
    advisor = body.get("advisor_notification") or {}
    delivery_status = "unknown"
    try:
        if advisor.get("required") and advisor.get("message"):
            _notify_advisor(str(advisor.get("message")))

        if instruction_type == "no_action":
            return True, delivery_status, None

        if instruction_type == "notify_advisor":
            message = _instruction_message(instruction)
            if message:
                _notify_advisor(message)
            return True, delivery_status, None

        message = _instruction_message(instruction) or NEUTRAL_FALLBACK_MESSAGE
        ok = send_message(phone, message)
        delivery_status = "sent" if ok else "failed"
        return ok, delivery_status, None if ok else "send_failed"
    except Exception as exc:
        log.exception("Boardroom instruction execution failed")
        return False, "failed", f"{type(exc).__name__}: {exc}"


def _confirm_boardroom_execution(
    body: Dict[str, Any],
    executed: bool,
    delivery_status: str,
    error: Optional[str],
) -> None:
    instruction_id = body.get("instruction_id")
    if not instruction_id or not _BUS_ACTIVE or not BUS_URL or not BUS_INTERNAL_TOKEN:
        return
    try:
        requests.post(
            _bus_confirm_url(),
            json={
                "instruction_id": instruction_id,
                "executed": bool(executed),
                "executed_at": f"{_utc_now_iso()}Z",
                "delivery_status": delivery_status,
                "error": error,
            },
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {BUS_INTERNAL_TOKEN}",
                "X-Source-System": "vicky",
            },
            timeout=3,
        )
    except Exception as exc:
        log.warning("Boardroom confirm failed instruction_id=%s error=%s", instruction_id, exc)


def _send_neutral_fallback(phone: str) -> None:
    send_message(phone, NEUTRAL_FALLBACK_MESSAGE)


def _handle_boardroom_authority(
    phone: str,
    msg: Dict[str, Any],
    match: Optional[Dict[str, Any]],
    mtype: str,
    text: str,
) -> bool:
    if not BOARDROOM_IS_AUTHORITY:
        return False

    payload = _build_boardroom_event(phone, text, msg, mtype, match)
    body, error = _request_boardroom_instruction(payload)
    if body is None:
        log.warning("Boardroom authority fallback reason=%s phone_last4=%s", error, phone[-4:])
        _send_neutral_fallback(phone)
        return True

    executed, delivery_status, exec_error = _execute_boardroom_instruction(phone, body)
    _confirm_boardroom_execution(body, executed, delivery_status, exec_error)
    if not executed:
        _send_neutral_fallback(phone)
    return True


def _is_boardroom_instruction_executable(body: Dict[str, Any]) -> bool:
    """Valida que la instruccion de un body `status: ok` sea semanticamente
    ejecutable, no solo formalmente valida. _parse_boardroom_instruction ya
    garantiza que `instruction` es un dict y que `instruction.type` esta en
    _BOARDROOM_ALLOWED_INSTRUCTIONS -- eso no basta: un `send_message` sin
    `message` es una forma valida pero una decision vacia (hallazgo de
    auditoria independiente sobre PR #4). Evalua unicamente el campo
    `instruction`; `advisor_notification` es un efecto secundario que
    acompana a una decision ya valida, nunca justifica por si solo tratar
    una instruction invalida como ejecutable (DOC-0043 no le da ese rol).
    """
    instruction = body.get("instruction")
    if not isinstance(instruction, dict):
        return False
    instruction_type = instruction.get("type")

    if instruction_type == "no_action":
        # Ejecutable sin payload por diseno: Boardroom decide deliberadamente
        # no actuar, eso es en si mismo una decision util (DOC-0043 regla 4).
        return True

    if instruction_type in (
        "send_message", "ask_question", "send_options", "request_document",
        "notify_advisor", "handoff",
    ):
        # _instruction_message ya combina message + labels de send_options --
        # cubre ambos casos (mensaje vacio con opciones validas, o mensaje
        # presente) con una sola verificacion de contenido no vacio.
        return bool(_instruction_message(instruction).strip())

    return False


def _classify_boardroom_outcome(body: Optional[Dict[str, Any]]) -> str:
    """HANDLED / NOT_HANDLED / FAILED segun DOC-0043 (hydra-source-of-truth,
    01_GOVERNANCE/DOC-0043-secom-boardroom-authority-boundary.md, regla 4):
    `status: ok` NO es por si solo sinonimo de decision util -- ademas debe
    traer una instruccion semanticamente ejecutable
    (_is_boardroom_instruction_executable). `status: fallback` y
    `status: error` nunca autorizan tratar el turno como manejado, sin
    importar que instruction traigan -- un HTTP 200 no es sinonimo de
    decision util. Un `status: ok` con instruccion no ejecutable se
    clasifica FAILED, no NOT_HANDLED: Boardroom si respondio con
    autoridad, pero la respuesta fue defectuosa, no una ausencia de
    decision. body=None cubre fallo de transporte o de parseo (timeout,
    4xx/5xx, JSON invalido, instruction invalida en forma).
    """
    if not isinstance(body, dict):
        return "FAILED"
    status = body.get("status")
    if status == "fallback":
        return "NOT_HANDLED"
    if status == "ok":
        return "HANDLED" if _is_boardroom_instruction_executable(body) else "FAILED"
    return "FAILED"


def _consult_boardroom(
    phone: str,
    msg: Dict[str, Any],
    match: Optional[Dict[str, Any]],
    mtype: str,
    text: str,
) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Consulta sincrona a Boardroom (REQUEST_FOR_DECISION) y clasifica el
    resultado. Usada solo para turnos sin funnel activo -- DOC-0043 regla 3
    reserva la ejecucion local sin consulta previa exclusivamente a un
    funnel ya iniciado."""
    payload = _build_boardroom_event(phone, text, msg, mtype, match)
    body, error = _request_boardroom_instruction(payload)
    outcome = _classify_boardroom_outcome(body)
    log.info(
        "🧭 boardroom_outcome phone_last4=%s outcome=%s error=%s",
        phone[-4:], outcome, error,
    )
    return outcome, body


def _execute_handled_boardroom_instruction(phone: str, body: Dict[str, Any]) -> None:
    """Ejecuta una instruccion de Boardroom ya clasificada como HANDLED. Si
    el envio falla, usa el mensaje neutral (no una decision comercial local
    nueva) -- Boardroom ya decidio, solo fallo la entrega."""
    executed, delivery_status, exec_error = _execute_boardroom_instruction(phone, body)
    _confirm_boardroom_execution(body, executed, delivery_status, exec_error)
    if not executed:
        _send_neutral_fallback(phone)


def _stateless_text_fallback(
    phone: str,
    text: str,
    match: Optional[Dict[str, Any]],
    idle: bool,
    last10: str,
) -> None:
    """TECHNICAL_FALLBACK (DOC-0043 regla 4) para un turno de texto sin
    funnel activo, cuando Boardroom no produjo una decision util o fallo.
    Restaura, detras de SECOM_LOCAL_FALLBACK_ENABLED, la logica
    deterministica preexistente que vivia en este mismo lugar de
    webhook_receive antes de CP-08a: respuesta a plantilla ("info"),
    contexto de alianza/auto/tpv, saludo, keywords de TPV, interpretacion
    de "no", heuristica de interes/duda, y _route_command como catch-all
    final. Deliberadamente NO incluye: (a) el mecanismo legacy
    send_to_boardroom/execute_boardroom_decision -- es una integracion
    Boardroom distinta a /bus/event, consultarla aqui violaria "un solo
    response owner por turno"; (b) el fallback GPT "sgpt:" -- genera
    respuesta persuasiva libre no guionada, exactamente lo que DOC-0043
    clasifica como COMMERCIAL_DECISION, no TECHNICAL_FALLBACK.
    """
    t_norm_info = text.strip().lower()
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
                    f"Nombre: {_match_name(match) or '(sin nombre)'}\n"
                    f"Mensaje: {text}"
                )
            except Exception:
                pass
            send_message(phone, "✅ Perfecto. Para recomendarte la mejor terminal Inbursa, dime: ¿*a qué giro* pertenece tu negocio?")
            return

    if idle and match:
        intent_handled = False
        if _auto_is_context(match) and _explicit_non_auto_intent(text):
            log.info("🔀 Escape de flujo AUTO por intención explícita: %s", text)
        else:
            if _alianza_is_context(match):
                if _handle_alianza_context_response(phone, text, match):
                    intent_handled = True
            if intent_handled:
                return

            if _auto_is_context(match):
                if _handle_auto_context_response(phone, text, match):
                    intent_handled = True
            if intent_handled:
                return

        if _tpv_is_context(match):
            if tpv_start_from_reply(phone, text, match):
                intent_handled = True
        if intent_handled:
            return

    if idle:
        t_norm = text.strip().lower()
        greet_words = {
            "hola", "buenas", "buenos dias", "buenos días", "buen dia", "buen día",
            "buenas tardes", "buenas noches", "hey", "que tal", "qué tal", "holi",
        }
        if t_norm in greet_words:
            base = "Dime qué necesitas y con gusto te guío para ayudarte a encontrar el servicio que necesitas."
            nombre = _match_name(match)
            send_message(phone, f"Hola {nombre} 👋 {base}" if nombre else f"Hola 👋 {base}")
            user_state[phone] = "__greeted__"
            return

        tpv_keywords = (
            "tpv", "terminal", "terminales", "punto de venta", "punto-de-venta",
            "cobrar con tarjeta", "cobro con tarjeta", "pagar con tarjeta",
            "ligas de pago", "link de pago", "link pago", "cobro a distancia",
        )
        if any(k in t_norm for k in tpv_keywords):
            user_state[phone] = "tpv_giro"
            _notify_advisor(
                "🧠 Interés detectado (TPV)\n"
                f"WhatsApp: {phone}\n"
                f"Nombre: {_match_name(match) or '(sin nombre)'}\n"
                f"Mensaje: {text}"
            )
            send_message(phone, "✅ Perfecto. Para recomendarte la mejor terminal Inbursa, dime: ¿*a qué giro* pertenece tu negocio?")
            return

    if idle and interpret_response(text) == "negative":
        send_message(phone, "Gracias por tu respuesta. Quedo a tus órdenes para cualquier duda o si más adelante deseas revisarlo.")
        user_state[phone] = "__greeted__"
        send_main_menu(phone)
        return

    t_lower = text.lower().strip()
    valid_commands = {
        "1", "2", "3", "4", "5", "6", "7",
        "menu", "menú", "inicio", "hola",
        "imss", "ley 73", "prestamo", "préstamo", "pension", "pensión",
        "auto", "seguro auto", "seguros de auto",
        "vida", "salud", "seguro de vida", "seguro de salud",
        "vrim", "tarjeta medica", "tarjeta médica",
        "empresarial", "pyme", "credito", "crédito", "credito empresarial", "crédito empresarial",
        "financiamiento", "financiamiento practico", "financiamiento práctico",
        "contactar", "asesor", "contactar con christian",
    }
    if not t_lower.isdigit() and t_lower not in valid_commands and idle:
        _notify_advisor(
            "📩 Cliente INTERESADO / DUDA detectada\n"
            f"WhatsApp: {phone}\n"
            f"Mensaje: {text}"
        )

    if phone not in user_state:
        user_state[phone] = "__greeted__"
        if not match:
            _greet_and_match(phone)

    _route_command(phone, text, match)


def _emit_boardroom_observation(
    phone: str,
    msg: Dict[str, Any],
    match: Optional[Dict[str, Any]],
    mtype: str,
    text: str,
) -> None:
    """OBSERVATION_EVENT fire-and-forget hacia /bus/event para turnos que
    Vicky ya resolvio localmente (DETERMINISTIC_EXECUTION dentro de un
    funnel activo, DOC-0043 regla 3) -- Boardroom/Rodys siguen recibiendo
    el evento, pero Vicky no espera ni actua sobre la respuesta. Reutiliza
    el mismo payload canonico (_build_boardroom_event) y el mismo endpoint
    (_bus_event_url) que _request_boardroom_instruction: mismo contrato de
    /bus/event, sin endpoint nuevo. No reutiliza _emit_bus_event porque ese
    helper postea a BUS_URL sin el sufijo /bus/event y con un payload de
    forma distinta (legacy) -- evita el riesgo de apuntar a la ruta
    equivocada dependiendo de como este configurado BUS_URL.
    """
    if not _BUS_ACTIVE or not BUS_URL or not BUS_INTERNAL_TOKEN:
        return
    payload = _build_boardroom_event(phone, text, msg, mtype, match)

    def _post() -> None:
        try:
            requests.post(
                _bus_event_url(),
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {BUS_INTERNAL_TOKEN}",
                    "X-Source-System": "vicky",
                    "X-Event-Type": "inbound_message",
                },
                timeout=3,
            )
        except Exception as exc:
            log.warning(
                "Boardroom observation emit fallido phone_last4=%s error=%s: %s",
                phone[-4:], type(exc).__name__, str(exc),
            )

    threading.Thread(target=_post, daemon=True).start()


def _extract_boardroom_decision(decision: Any) -> Dict[str, Any]:
    if not isinstance(decision, dict):
        return {}
    nested = decision.get("decision")
    if isinstance(nested, dict):
        merged = dict(decision)
        merged.update(nested)
        return merged
    return dict(decision)


def execute_boardroom_decision(phone: str, decision: Any, match: Optional[Dict[str, Any]] = None) -> bool:
    try:
        data = _extract_boardroom_decision(decision)
        if not data:
            return False

        reply_raw = data.get("reply") or data.get("response") or data.get("message")
        reply = reply_raw.strip() if isinstance(reply_raw, str) else ""
        action = (data.get("action") or "").strip()
        product = (data.get("product") or "").strip()
        advisor_message = data.get("advisor_message")
        notify_advisor = data.get("notify_advisor")
        sheet_update = data.get("sheet_update")
        valid_sheet_update = isinstance(sheet_update, dict) and bool(sheet_update)

        if product == "vida_temporal" and match and match.get("row"):
            _safe_update_row_cells(int(match["row"]), {"PRODUCTO": "vida_temporal"}, VIDA_SHEET_FIELDS)

        if reply:
            if valid_sheet_update and match and match.get("row"):
                _safe_update_row_cells(int(match["row"]), sheet_update, VIDA_SHEET_FIELDS)
            send_message(phone, reply)
            log.info("✅ Boardroom decision handled")
            return True

        if action == "start_vida_temporal_flow":
            vida_start(phone, match)
            log.info("✅ Boardroom decision handled")
            return True

        if advisor_message:
            _notify_advisor(str(advisor_message))
            log.info("✅ Boardroom decision handled")
            return True

        if notify_advisor:
            _notify_advisor(
                "🔔 Boardroom — seguimiento requerido\n"
                f"WhatsApp: {phone}\n"
                f"Producto: {product or '(sin producto)'}\n"
                f"Acción: {action or '(sin acción)'}"
            )
            log.info("✅ Boardroom decision handled")
            return True

        if valid_sheet_update and match and match.get("row"):
            _safe_update_row_cells(int(match["row"]), sheet_update, VIDA_SHEET_FIELDS)
            log.info("✅ Boardroom decision handled")
            return True

        return False
    except Exception:
        log.exception("⚠️ Error ejecutando decisión Boardroom; fallback local")
        return False


# ==========================
# Embudos
# ==========================
def vida_start(phone: str, match: Optional[Dict[str, Any]] = None) -> None:
    user_state[phone] = "vida_edad"
    data = _ensure_user(phone)
    data["producto"] = "vida_temporal"
    log.info("🧬 Vida Temporal flow started")
    try:
        if match and match.get("row"):
            _safe_update_row_cells(
                int(match["row"]),
                {
                    "ESTATUS": "interesado",
                    "PRODUCTO": "vida_temporal",
                    "ULTIMO_CONTACTO": _utc_now_iso(),
                    "NOTAS": "interesado en vida temporal desde WhatsApp",
                    "BENEFICIO_OFRECIDO": "posible descuento hasta 40% sujeto a edad, perfil y condiciones",
                    "LAST_MESSAGE": data.get("last_message", ""),
                },
                VIDA_SHEET_FIELDS,
            )
    except Exception:
        log.exception("⚠️ No fue posible actualizar Sheets al iniciar Vida Temporal")

    send_message(
        phone,
        "Perfecto, te ayudo con Seguro de Vida Temporal.\n\n"
        "Para revisar una opción necesito algunos datos rápidos.\n\n"
        "¿Cuál es tu edad?",
    )


def _vida_next(phone: str, text: str, match: Optional[Dict[str, Any]] = None) -> None:
    st = user_state.get(phone, "")
    data = _ensure_user(phone)
    data["last_message"] = text or ""

    if st == "vida_edad":
        edad = extract_number(text)
        if edad is None or edad < 18 or edad > 75:
            send_message(phone, "Para revisar Seguro de Vida Temporal necesito una edad entre 18 y 75. ¿Cuál es tu edad?")
            return
        data["edad"] = int(edad)
        user_state[phone] = "vida_fuma"
        send_message(phone, "¿Fumas actualmente? Responde *sí* o *no*.")
        return

    if st == "vida_fuma":
        intent = interpret_response(text)
        if intent == "positive":
            data["fuma"] = "sí"
        elif intent == "negative":
            data["fuma"] = "no"
        else:
            send_message(phone, "¿Fumas actualmente? Responde *sí* o *no*.")
            return
        user_state[phone] = "vida_estado"
        send_message(phone, "¿En qué estado de la República vives?")
        return

    if st == "vida_estado":
        estado = (text or "").strip()
        if not estado:
            send_message(phone, "¿En qué estado de la República vives?")
            return
        data["estado"] = estado
        user_state[phone] = "vida_suma"
        send_message(phone, "¿Qué suma asegurada te gustaría revisar? Ejemplo: 500 mil, 1 millón o 2 millones.")
        return

    if st == "vida_suma":
        suma = (text or "").strip()
        if not suma:
            send_message(phone, "¿Qué suma asegurada te gustaría revisar? Ejemplo: 500 mil, 1 millón o 2 millones.")
            return
        data["suma"] = suma
        user_state[phone] = "vida_objetivo"
        send_message(phone, "¿Qué buscas proteger principalmente?\n1) Familia\n2) Deuda\n3) Negocio\n4) Otro")
        return

    if st == "vida_objetivo":
        raw = (text or "").strip()
        objetivo = {"1": "Familia", "2": "Deuda", "3": "Negocio", "4": "Otro"}.get(raw, raw.capitalize() if raw else "Otro")
        data["objetivo"] = objetivo
        send_message(
            phone,
            "Gracias. Ya tengo los datos iniciales para revisar una opción de Seguro de Vida Temporal.\n\n"
            "Christian te dará seguimiento para revisar una propuesta según tu edad, perfil, suma asegurada y condiciones de contratación.",
        )
        _notify_advisor(
            "🔔 VIDA TEMPORAL — Prospecto interesado\n"
            f"WhatsApp: {phone}\n"
            f"Nombre: {_match_name(match) or '(sin nombre)'}\n"
            f"Edad: {data.get('edad', '')}\n"
            f"Fuma: {data.get('fuma', '')}\n"
            f"Estado: {data.get('estado', '')}\n"
            f"Suma asegurada: {data.get('suma', '')}\n"
            f"Objetivo: {data.get('objetivo', '')}"
        )
        try:
            if match and match.get("row"):
                _safe_update_row_cells(
                    int(match["row"]),
                    {
                        "ESTATUS": "perfil_inicial_capturado",
                        "PRODUCTO": "vida_temporal",
                        "ULTIMO_CONTACTO": _utc_now_iso(),
                        "NOTAS": "datos iniciales capturados para vida temporal",
                        "LAST_MESSAGE": data.get("last_message", ""),
                    },
                    VIDA_SHEET_FIELDS,
                )
        except Exception:
            log.exception("⚠️ No fue posible actualizar Sheets al cerrar Vida Temporal")
        _cierre_registrar(phone, "vida", acuse=False)
        user_state[phone] = "__greeted__"
        log.info("✅ Vida Temporal perfil inicial capturado")
        return

    vida_start(phone, match)


def imss_start(phone: str, match: Optional[Dict[str, Any]]) -> None:
    user_state[phone] = "imss_beneficios"
    log.info("🏥 Iniciando embudo IMSS para %s", phone)
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
        data["imss_nombre"] = text.strip()
        user_state[phone] = "imss_ciudad"
        send_message(phone, "¿En qué *ciudad* te encuentras?")
        return

    if st == "imss_ciudad":
        data["imss_ciudad"] = text.strip()
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
        _notify_advisor(f"🔔 IMSS — Prospecto preautorizado\nWhatsApp: {phone}\n{msg}")
        _cierre_registrar(phone, "imss")
        user_state[phone] = "__greeted__"
        send_main_menu(phone)
        return

    imss_start(phone, None)


def emp_start(phone: str, match: Optional[Dict[str, Any]]) -> None:
    user_state[phone] = "emp_confirma"
    log.info("🏢 Iniciando embudo empresarial para %s", phone)
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
        data["emp_giro"] = text.strip()
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
        data["emp_nombre"] = text.strip()
        user_state[phone] = "emp_ciudad"
        send_message(phone, "¿Tu *ciudad*?")
        return
    if st == "emp_ciudad":
        data["emp_ciudad"] = text.strip()
        resumen = (
            "✅ Gracias. Un asesor te contactará.\n"
            f"- Nombre: {data.get('emp_nombre','')}\n"
            f"- Ciudad: {data.get('emp_ciudad','')}\n"
            f"- Giro: {data.get('emp_giro','')}\n"
            f"- Monto: ${data.get('emp_monto',0):,.0f}\n"
        )
        send_message(phone, resumen)
        _notify_advisor(f"🔔 Empresarial — Nueva solicitud\nWhatsApp: {phone}\n{resumen}")
        _cierre_registrar(phone, "empresarial")
        user_state[phone] = "__greeted__"
        send_main_menu(phone)
        return
    emp_start(phone, None)


FP_QUESTIONS = [f"Pregunta {i}" for i in range(1, 12)]


def fp_start(phone: str, match: Optional[Dict[str, Any]]) -> None:
    user_state[phone] = "fp_q1"
    _ensure_user(phone)["fp_answers"] = {}
    log.info("💰 Iniciando embudo financiamiento práctico para %s", phone)
    send_message(phone, "🟩 *Financiamiento Práctico*\nResponderemos 11 preguntas rápidas.\n1) " + FP_QUESTIONS[0])


def _fp_next(phone: str, text: str) -> None:
    st = user_state.get(phone, "")
    data = _ensure_user(phone)
    if st.startswith("fp_q"):
        idx = int(st.split("_q", 1)[1]) - 1
        data.setdefault("fp_answers", {})[f"q{idx + 1}"] = text.strip()
        if idx + 1 < len(FP_QUESTIONS):
            user_state[phone] = f"fp_q{idx + 2}"
            send_message(phone, f"{idx + 2}) {FP_QUESTIONS[idx + 1]}")
            return
        user_state[phone] = "fp_comentario"
        send_message(phone, "¿Algún *comentario adicional*?")
        return

    if st == "fp_comentario":
        data["fp_comentario"] = text.strip()
        resumen = "✅ Gracias. Un asesor te contactará.\n" + "\n".join(
            f"{k.upper()}: {v}" for k, v in data.get("fp_answers", {}).items()
        )
        if data.get("fp_comentario"):
            resumen += f"\nCOMENTARIO: {data['fp_comentario']}"
        send_message(phone, resumen)
        _notify_advisor(f"🔔 Financiamiento Práctico — Resumen\nWhatsApp: {phone}\n{resumen}")
        _cierre_registrar(phone, "financiamiento")
        user_state[phone] = "__greeted__"
        send_main_menu(phone)
        return

    fp_start(phone, None)


def auto_start(phone: str, match: Optional[Dict[str, Any]]) -> None:
    user_state[phone] = "auto_intro"
    log.info("🚗 Iniciando embudo seguro auto para %s", phone)
    send_message(
        phone,
        "🚗 *Seguro de Auto*\nEnvíame por favor:\n• INE (frente)\n• Tarjeta de circulación *o* número de placas\n\nCuando lo envíes, te confirmaré recepción y procesaré la cotización.",
    )


def _auto_next(phone: str, text: str) -> None:
    st = user_state.get(phone, "")
    if st == "auto_intro":
        intent = interpret_response(text)
        lower = text.lower()
        if "vencimiento" in lower or "vence" in lower or "fecha" in lower:
            user_state[phone] = "auto_vencimiento_fecha"
            send_message(phone, "¿Cuál es la *fecha de vencimiento* de tu póliza actual? (formato AAAA-MM-DD)")
            return
        if intent == "negative":
            user_state[phone] = "auto_vencimiento_fecha"
            send_message(phone, "Entendido. Para poder recordarte a tiempo, ¿cuál es la *fecha de vencimiento* de tu póliza? (AAAA-MM-DD)")
            return
        send_message(phone, "Perfecto. Puedes empezar enviando los *documentos* o una *foto* de la tarjeta/placas.")
        return

    if st == "auto_vencimiento_fecha":
        try:
            fecha = datetime.fromisoformat(text.strip()).date()
            objetivo = fecha - timedelta(days=30)
            write_followup_to_sheets("auto_recordatorio", f"Recordatorio póliza -30d para {phone}", objetivo.isoformat())
            threading.Thread(target=_retry_after_days, args=(phone, 7), daemon=True).start()
            send_message(phone, f"✅ Gracias. Te contactaré *un mes antes* ({objetivo.isoformat()}).")
            _cierre_registrar(phone, "auto", acuse=False)
            user_state[phone] = "__greeted__"
            send_main_menu(phone)
        except Exception:
            send_message(phone, "Formato inválido. Usa AAAA-MM-DD. Ejemplo: 2025-12-31")
        return

    auto_start(phone, None)


def _tpv_next(phone: str, text: str, match: Optional[Dict[str, Any]]) -> None:
    st = user_state.get(phone, "")
    data = _ensure_user(phone)
    nombre = _match_name(match)

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
        _cierre_registrar(phone, "tpv")
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

    user_state[phone] = "tpv_giro"
    send_message(phone, "✅ Perfecto. Para recomendarte la mejor terminal Inbursa, dime: ¿*a qué giro* pertenece tu negocio?")


# ==========================
# Cierre de cortesia post-cuestionario
# ==========================
# Cuando el cliente termina un embudo, Vicky agradece y avisa que Christian lo
# contactara sin esperar otro mensaje suyo. A partir de ahi absorbe el tramo
# final de la conversacion: un "gracias" recibe cortesia (sin marca de genero)
# con la invitacion al menu, un "no gracias" cierra agradeciendo el tiempo, y
# si el cliente no responde en una hora se le manda una ultima linea.
#
# INVARIANTE: el recordatorio se arma en UN solo lugar por conversacion --
# _cierre_registrar(), el cierre automatico del embudo -- y cualquier mensaje
# entrante lo cancela (ver webhook_receive). Ninguna respuesta de Vicky a un
# mensaje del cliente lo vuelve a armar: si el cliente contesto, ya respondio,
# y "si no responde en una hora" dejo de aplicar.
#
# El recordatorio vive en memoria del proceso, igual que user_state: SECOM no
# tiene Valkey, asi que un reinicio de Render lo pierde. Se pierde en silencio
# a proposito -- perder un mensaje de cortesia es aceptable; duplicarlo o
# mandarlo fuera de tiempo, no.
CIERRE_NUDGE_SECONDS = int(os.getenv("CIERRE_NUDGE_SECONDS", "3600") or 3600)
# Si el barrido se atrasa (proceso ocupado, hilo detenido), un recordatorio muy
# vencido ya no se entrega: llegar horas tarde es peor que no llegar.
CIERRE_NUDGE_MAX_ATRASO = int(os.getenv("CIERRE_NUDGE_MAX_ATRASO", str(6 * 3600)) or 6 * 3600)
CIERRE_NUDGE_TICK = int(os.getenv("CIERRE_NUDGE_TICK", "60") or 60)
CIERRE_NUDGE_SWEEPER = (os.getenv("CIERRE_NUDGE_SWEEPER", "true").strip().lower()
                        in ("1", "true", "yes", "on"))
# Cuanto dura el contexto de cierre. Pasado ese plazo, un "gracias" suelto ya
# no es la cola de esta conversacion y se rutea normalmente.
CIERRE_VENTANA_SECONDS = int(os.getenv("CIERRE_VENTANA_SECONDS", str(6 * 3600)) or 6 * 3600)

_cierre_ctx: Dict[str, Dict[str, Any]] = {}
_cierre_lock = threading.Lock()
# Clase capturada al importar: el barrido es un bucle infinito y las pruebas
# sustituyen threading.Thread por un doble que ejecuta el target en linea.
_NUDGE_THREAD_CLS = threading.Thread
_nudge_sweeper_started = False


def _cierre_registrar(phone: str, producto: str = "", acuse: bool = True) -> None:
    """Cierra el embudo: manda el acuse automatico (salvo que el mensaje de
    cierre ya lo diga) y deja armado el recordatorio de una hora."""
    if acuse:
        send_message(phone, cc.ACUSE)
    ahora = time.time()
    with _cierre_lock:
        _cierre_ctx[phone] = {
            "producto": producto,
            "ts": ahora,
            "cortesia_enviada": False,
            "despedido": False,
            "nudge_due": ahora + CIERRE_NUDGE_SECONDS,
        }
    _nudge_ensure_sweeper()


def _cierre_cancelar_nudge(phone: str) -> None:
    """El cliente escribio: el recordatorio de "no respondio" ya no aplica."""
    with _cierre_lock:
        ctx = _cierre_ctx.get(phone)
        if ctx:
            ctx["nudge_due"] = None


def _cierre_activo(phone: str) -> Optional[Dict[str, Any]]:
    with _cierre_lock:
        ctx = _cierre_ctx.get(phone)
        if not ctx:
            return None
        if time.time() - ctx["ts"] > CIERRE_VENTANA_SECONDS:
            _cierre_ctx.pop(phone, None)
            return None
        return ctx


def _cierre_manejar_cortesia(phone: str, text: str) -> bool:
    """Atiende el tramo final tras un cierre reciente. Devuelve True si ya
    resolvio el mensaje (respondiendo o callando a proposito) y no hay que
    rutearlo; False si el cliente retomo la conversacion con algo sustantivo."""
    ctx = _cierre_activo(phone)
    if ctx is None:
        return False

    if cc.es_respuesta_negativa(text):
        if not ctx["despedido"]:
            send_message(phone, cc.DESPEDIDA_NEGATIVA)
            ctx["despedido"] = True
        _cierre_cancelar_nudge(phone)
        return True

    if cc.es_cortesia_pura(text):
        if ctx["despedido"]:
            # Ya hubo despedida: no se vuelve a ofrecer nada.
            return True
        if not ctx["cortesia_enviada"]:
            send_message(phone, cc.cortesia_final(ctx.get("producto")))
            ctx["cortesia_enviada"] = True
        return True

    # Mensaje con contenido: el cliente retomo la conversacion y el contexto de
    # cierre deja de aplicar.
    with _cierre_lock:
        _cierre_ctx.pop(phone, None)
    return False


def _nudge_reclamar_vencidos(ahora: float) -> List[Tuple[str, float]]:
    """Devuelve [(phone, vencimiento)] y los marca como reclamados en el mismo
    paso, para que dos barridos concurrentes no manden el mensaje dos veces.
    Aprovecha para tirar los contextos ya expirados."""
    listos: List[Tuple[str, float]] = []
    with _cierre_lock:
        for phone, ctx in list(_cierre_ctx.items()):
            if ahora - ctx["ts"] > CIERRE_VENTANA_SECONDS:
                _cierre_ctx.pop(phone, None)
                continue
            due = ctx.get("nudge_due")
            if due is None or due > ahora:
                continue
            ctx["nudge_due"] = None
            listos.append((phone, due))
    return listos


def nudge_sweep_once() -> int:
    ahora = time.time()
    entregados = 0
    for phone, due in _nudge_reclamar_vencidos(ahora):
        try:
            if ahora - due > CIERRE_NUDGE_MAX_ATRASO:
                log.warning("cierre_nudge_descartado_por_atraso phone=%s horas=%.1f",
                            phone, (ahora - due) / 3600)
                continue
            if send_message(phone, cc.NUDGE):
                entregados += 1
        except Exception:
            log.exception("cierre_nudge_error phone=%s", phone)
    return entregados


def _nudge_loop() -> None:
    while True:
        time.sleep(max(CIERRE_NUDGE_TICK, 5))
        try:
            nudge_sweep_once()
        except Exception:
            log.exception("cierre_nudge_barrido_fallido")


def _nudge_ensure_sweeper() -> None:
    """Arranca el barrido una sola vez por proceso, de forma perezosa: mientras
    nadie cierre un embudo no hace falta ningun hilo."""
    global _nudge_sweeper_started
    if not CIERRE_NUDGE_SWEEPER or _nudge_sweeper_started:
        return
    with _cierre_lock:
        if _nudge_sweeper_started:
            return
        _NUDGE_THREAD_CLS(target=_nudge_loop, daemon=True,
                          name="CierreNudgeSweeper").start()
        _nudge_sweeper_started = True


def _retry_after_days(phone: str, days: int) -> None:
    try:
        time.sleep(days * 24 * 60 * 60)
        send_message(phone, "⏰ Seguimos a tus órdenes. ¿Deseas que coticemos tu seguro de auto cuando se acerque el vencimiento?")
        write_followup_to_sheets("auto_reintento", f"Reintento +{days}d enviado a {phone}", _utc_now_iso())
    except Exception:
        log.exception("Error en reintento programado")


# ==========================
# Router helpers
# ==========================
def _greet_and_match(phone: str) -> Optional[Dict[str, Any]]:
    match = match_client_in_sheets(_normalize_phone_last10(phone))
    base = "Dime qué necesitas y con gusto te guío para ayudarte a encontrar el servicio que necesitas."
    nombre = _match_name(match)
    send_message(phone, f"Hola {nombre} 👋 {base}" if nombre else f"Hola 👋 {base}")
    return match


def _route_command(phone: str, text: str, match: Optional[Dict[str, Any]]) -> None:
    t = (text or "").strip().lower()
    st = user_state.get(phone, "")

    # HOTFIX 2 SECOM:
    # Estado activo local tiene prioridad absoluta sobre comandos globales.
    # Esto evita que "1" dentro de vida_objetivo active IMSS.
    log.info("🧭 _route_command phone=%s state=%s text=%s", phone, st, text)

    if st.startswith(ACTIVE_FUNNEL_PREFIXES) and t in ESCAPE_COMMANDS:
        user_state[phone] = "__greeted__"
        send_main_menu(phone)
        return

    if st.startswith("vida_"):
        log.info("🧭 vida dispatch active phone=%s state=%s text=%s", phone, st, text)
        _vida_next(phone, text, match)
        return

    if st.startswith("imss_"):
        _imss_next(phone, text)
        return

    if st.startswith("auto_"):
        _auto_next(phone, text)
        return

    if st.startswith("tpv_"):
        _tpv_next(phone, text, match)
        return

    if st.startswith("emp_"):
        _emp_next(phone, text)
        return

    if st.startswith("fp_"):
        _fp_next(phone, text)
        return

    # TPV por contexto de plantilla queda después del dispatch temprano.
    if _tpv_is_context(match):
        if tpv_start_from_reply(phone, text, match):
            return

    tlow = t.lower()
    if (
        ("credito" in tlow or "crédito" in tlow or "prestamo" in tlow or "préstamo" in tlow)
        and not any(k in tlow for k in ("auto", "seguro auto", "seguro de auto", "póliza", "poliza", "placa"))
    ):
        send_message(
            phone,
            "¿Qué tipo de crédito buscas?\n"
            "1) Préstamo IMSS (Ley 73)\n"
            "5) Crédito Empresarial\n"
            "6) Financiamiento Práctico\n\n"
            "Responde *1*, *5* o *6*.",
        )
        return

    if t in ("1", "imss", "ley 73", "préstamo", "prestamo", "pension", "pensión"):
        log.info("🧭 imss_start candidate phone=%s state=%s text=%s", phone, user_state.get(phone, ""), text)
        imss_start(phone, match)
    elif t in ("2", "auto", "seguros de auto", "seguro auto"):
        auto_start(phone, match)
    elif t in (
        "3", "vida", "salud", "seguro de vida", "seguro de salud", "vida temporal",
        "seguro vida", "seguros de vida", "protección familiar", "proteccion familiar",
        "seguro de vida y salud",
    ):
        vida_start(phone, match)
    elif t in ("4", "vrim", "tarjeta médica", "tarjeta medica"):
        send_message(phone, "🩺 *VRIM* — Membresía médica. Notificaré al asesor para darte detalles.")
        _notify_advisor(f"🔔 VRIM — Solicitud de contacto\nWhatsApp: {phone}")
        send_main_menu(phone)
    elif t in ("5", "empresarial", "pyme", "crédito empresarial", "credito empresarial"):
        emp_start(phone, match)
    elif t in ("6", "financiamiento práctico", "financiamiento practico", "crédito simple", "credito simple", "financiamiento"):
        fp_start(phone, match)
    elif t in ("7", "contactar", "asesor", "contactar con christian"):
        _notify_advisor(f"🔔 Contacto directo — Cliente solicita hablar\nWhatsApp: {phone}")
        send_message(phone, "✅ Listo. Avisé a Christian para que te contacte.")
        send_main_menu(phone)
    elif t in ("menu", "menú", "inicio"):
        user_state[phone] = "__greeted__"
        send_main_menu(phone)
    else:
        send_message(
            phone,
            "En breve, su asesor Christian López se pondrá en contacto con usted para brindarle asesoría personalizada y resolver todas sus dudas de manera directa y segura. Escribe *menú* para ver opciones.",
        )


# ==========================
# Webhook
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


def _download_media(media_id: str) -> Tuple[Optional[bytes], Optional[str], Optional[str]]:
    if not META_TOKEN:
        return None, None, None
    try:
        meta = requests.get(
            f"https://graph.facebook.com/v20.0/{media_id}",
            headers={"Authorization": f"Bearer {META_TOKEN}"},
            timeout=WPP_TIMEOUT,
        )
        if meta.status_code != 200:
            log.warning("⚠️ Meta media meta falló %s: %s", meta.status_code, meta.text[:200])
            return None, None, None

        meta_json = meta.json()
        url = meta_json.get("url")
        mime = meta_json.get("mime_type")
        filename = meta_json.get("filename") or f"media_{media_id}"
        if not url:
            return None, None, None

        binary = requests.get(url, headers={"Authorization": f"Bearer {META_TOKEN}"}, timeout=WPP_TIMEOUT)
        if binary.status_code != 200:
            log.warning("⚠️ Meta media download falló %s", binary.status_code)
            return None, None, None

        log.info("✅ Media descargada: %s (%s bytes)", filename, len(binary.content))
        return binary.content, mime, filename
    except Exception:
        log.exception("❌ Error descargando media")
        return None, None, None


def _handle_media(phone: str, msg: Dict[str, Any]) -> None:
    try:
        media_id = None
        media_type = msg.get("type")
        if media_type in {"image", "document", "audio", "video"}:
            media_id = (msg.get(media_type) or {}).get("id")

        if not media_id:
            send_message(phone, "Recibí tu archivo, gracias. (No se pudo identificar el contenido).")
            return

        forward_media_to_advisor(media_type, media_id)

        file_bytes, mime, filename = _download_media(media_id)
        if not file_bytes:
            send_message(phone, "Recibí tu archivo, pero hubo un problema procesándolo.")
            return

        match = match_client_in_sheets(_normalize_phone_last10(phone))
        last4 = _normalize_phone_last10(phone)[-4:]
        folder_name = f"{_match_name(match).replace(' ', '_')}_{last4}" if _match_name(match) else f"Cliente_{last4}"
        link = upload_to_drive(filename, file_bytes, mime or "application/octet-stream", folder_name)
        _notify_advisor(f"🔔 Multimedia recibida\nDesde: {phone}\nArchivo: {filename}\nDrive: {link or '(sin link Drive)'}")
        send_message(phone, "✅ *Recibido y en proceso*. En breve te doy seguimiento.")
    except Exception:
        log.exception("❌ Error manejando multimedia")
        send_message(phone, "Recibí tu archivo, gracias. Si algo falla, lo reviso de inmediato.")


def _is_recent_awaiting_template_context(phone: str, match: Optional[Dict[str, Any]]) -> bool:
    """Valida ventana 24h usando memoria si existe, o Sheets como fallback."""
    try:
        started_at = (_ensure_user(phone).get("awaiting_info_started_at") or "").strip()
        if started_at and _within_24h(started_at):
            return True
    except Exception:
        pass

    try:
        last_message_at = ((match or {}).get("last_message_at") or "").strip()
        return bool(last_message_at and _within_24h(last_message_at))
    except Exception:
        return False


def _resolve_awaiting_template_context(phone: str, match: Optional[Dict[str, Any]]) -> str:
    """
    Resuelve template pendiente desde:
    1) user_state en memoria.
    2) Sheets + ENVIO_STATUS cuando Render reinició y perdió memoria.

    Gobernanza:
    - Solo recupera contexto si está dentro de 24h.
    - Solo recupera estatus outbound permitido.
    - Solo aplica a templates explícitamente registrados.
    """
    st = user_state.get(phone, "")

    if st.startswith("awaiting_info:"):
        template_name = st.split(":", 1)[1].strip()

        if not _is_recent_awaiting_template_context(phone, match):
            log.info("⏳ awaiting_info expirado para %s template=%s", phone, template_name)
            user_state[phone] = "__greeted__"
            return ""

        return template_name

    if not match:
        return ""

    if not _is_recent_awaiting_template_context(phone, match):
        return ""

    estatus = ((match or {}).get("estatus") or "").strip().upper()
    if estatus not in AWAITING_TEMPLATE_RECOVERABLE_STATUSES:
        return ""

    last_tpl = get_last_envio_template(_normalize_phone_last10(phone))
    if last_tpl:
        log.info("♻️ Recuperando contexto awaiting_info desde Sheets/ENVIO_STATUS phone=%s template=%s", phone, last_tpl)
        return last_tpl

    return ""


def _handle_awaiting_template_response(phone: str, text: str, match: Optional[Dict[str, Any]]) -> bool:
    template_name = _resolve_awaiting_template_context(phone, match)
    if not template_name:
        return False

    t = (text or "").strip().lower()
    nombre = _match_name(match) or "(sin nombre)"

    if template_name in SECOM_VIDA_TEMPLATES:
        if t in TEMPLATE_INTEREST_WORDS or interpret_response(text) == "positive":
            _notify_advisor(
                "🚨 SECOM / VIDA INBURSA — Prospecto interesado\n"
                f"Template: {template_name}\n"
                f"WhatsApp: {phone}\n"
                f"Nombre: {nombre}\n"
                f"Respuesta: {text}"
            )

            try:
                if match and match.get("row"):
                    _safe_update_row_cells(
                        int(match["row"]),
                        {
                            "ESTATUS": "INTERESADO_SECOM_VIDA",
                            "PRODUCTO": "vida_inbursa",
                            "ULTIMO_CONTACTO": _utc_now_iso(),
                            "LAST_MESSAGE_AT": _utc_now_iso(),
                            "NOTAS": f"Respondió interés a plantilla {template_name}: {text}",
                            "LAST_MESSAGE": text,
                        },
                        VIDA_SHEET_FIELDS,
                    )
            except Exception:
                log.exception("⚠️ No fue posible actualizar Sheets para interés SECOM VIDA")

            send_message(
                phone,
                "En breve, su asesor Christian López se pondrá en contacto con usted para "
                "brindarle asesoría personalizada y resolver todas sus dudas de manera directa y segura. "
                "Escribe *menú* para ver opciones."
            )
            _cierre_registrar(phone, "vida", acuse=False)
            user_state[phone] = "__greeted__"
            return True

        if interpret_response(text) == "negative":
            try:
                if match and match.get("row"):
                    _safe_update_row_cells(
                        int(match["row"]),
                        {
                            "ESTATUS": "NO_INTERESADO_SECOM_VIDA",
                            "ULTIMO_CONTACTO": _utc_now_iso(),
                            "LAST_MESSAGE_AT": _utc_now_iso(),
                            "NOTAS": f"No interesado a plantilla {template_name}: {text}",
                            "LAST_MESSAGE": text,
                        },
                        VIDA_SHEET_FIELDS,
                    )
            except Exception:
                log.exception("⚠️ No fue posible actualizar Sheets para rechazo SECOM VIDA")

            send_message(phone, "Gracias por tu respuesta. Quedo a tus órdenes si más adelante deseas revisarlo.")
            user_state[phone] = "__greeted__"
            return True

        _notify_advisor(
            "📩 SECOM / VIDA INBURSA — Respuesta o duda detectada\n"
            f"Template: {template_name}\n"
            f"WhatsApp: {phone}\n"
            f"Nombre: {nombre}\n"
            f"Mensaje: {text}"
        )

        try:
            if match and match.get("row"):
                _safe_update_row_cells(
                    int(match["row"]),
                    {
                        "ESTATUS": "DUDA_SECOM_VIDA",
                        "ULTIMO_CONTACTO": _utc_now_iso(),
                        "LAST_MESSAGE_AT": _utc_now_iso(),
                        "NOTAS": f"Respuesta neutral a plantilla {template_name}: {text}",
                        "LAST_MESSAGE": text,
                    },
                    VIDA_SHEET_FIELDS,
                )
        except Exception:
            log.exception("⚠️ No fue posible actualizar Sheets para duda SECOM VIDA")

        send_message(
            phone,
            "Gracias. En breve, su asesor Christian López se pondrá en contacto con usted para darle seguimiento."
        )
        user_state[phone] = "__greeted__"
        return True

    if t in TEMPLATE_INTEREST_WORDS or interpret_response(text) == "positive":
        _notify_advisor(
            "🚨 SECOM / PLANTILLA — Prospecto interesado\n"
            f"Template: {template_name}\n"
            f"WhatsApp: {phone}\n"
            f"Nombre: {nombre}\n"
            f"Respuesta: {text}"
        )

        try:
            if match and match.get("row"):
                _safe_update_row_cells(
                    int(match["row"]),
                    {
                        "ESTATUS": "INTERESADO_TEMPLATE",
                        "PRODUCTO": template_name,
                        "ULTIMO_CONTACTO": _utc_now_iso(),
                        "LAST_MESSAGE_AT": _utc_now_iso(),
                        "NOTAS": f"Respondió interés a plantilla {template_name}: {text}",
                        "LAST_MESSAGE": text,
                    },
                    VIDA_SHEET_FIELDS,
                )
        except Exception:
            log.exception("⚠️ No fue posible actualizar Sheets para interés de plantilla genérica")

        send_message(
            phone,
            "✅ Gracias. Ya registré tu interés. En breve, Christian López te contactará para darte seguimiento."
        )
        _cierre_registrar(phone, "", acuse=False)
        user_state[phone] = "__greeted__"
        return True

    if interpret_response(text) == "negative":
        try:
            if match and match.get("row"):
                _safe_update_row_cells(
                    int(match["row"]),
                    {
                        "ESTATUS": "NO_INTERESADO_TEMPLATE",
                        "ULTIMO_CONTACTO": _utc_now_iso(),
                        "LAST_MESSAGE_AT": _utc_now_iso(),
                        "NOTAS": f"No interesado a plantilla {template_name}: {text}",
                        "LAST_MESSAGE": text,
                    },
                    VIDA_SHEET_FIELDS,
                )
        except Exception:
            log.exception("⚠️ No fue posible actualizar Sheets para rechazo de plantilla genérica")

        send_message(phone, "Gracias por tu respuesta. Quedo a tus órdenes si más adelante deseas revisarlo.")
        user_state[phone] = "__greeted__"
        return True

    _notify_advisor(
        "📩 SECOM / PLANTILLA — Respuesta o duda detectada\n"
        f"Template: {template_name}\n"
        f"WhatsApp: {phone}\n"
        f"Nombre: {nombre}\n"
        f"Mensaje: {text}"
    )

    try:
        if match and match.get("row"):
            _safe_update_row_cells(
                int(match["row"]),
                {
                    "ESTATUS": "DUDA_TEMPLATE",
                    "ULTIMO_CONTACTO": _utc_now_iso(),
                    "LAST_MESSAGE_AT": _utc_now_iso(),
                    "NOTAS": f"Respuesta neutral a plantilla {template_name}: {text}",
                    "LAST_MESSAGE": text,
                },
                VIDA_SHEET_FIELDS,
            )
    except Exception:
        log.exception("⚠️ No fue posible actualizar Sheets para duda de plantilla genérica")

    send_message(
        phone,
        "Gracias. En breve, Christian López se pondrá en contacto contigo para darte seguimiento."
    )
    user_state[phone] = "__greeted__"
    return True


@app.post("/webhook")
def webhook_receive():
    try:
        intent_handled = False
        payload = request.get_json(force=True, silent=True) or {}
        log.info("📥 Webhook recibido: %s...", json.dumps(payload, ensure_ascii=False)[:500])

        entry = (payload.get("entry") or [{}])[0]
        changes = (entry.get("changes") or [{}])[0]
        value = changes.get("value", {})
        messages = value.get("messages", [])
        statuses = value.get("statuses", [])

        if not messages:
            if statuses:
                for st in statuses:
                    try:
                        if (st.get("status") or "").lower() == "failed":
                            log.warning("❌ STATUS failed (detalle): %s", json.dumps(st, ensure_ascii=False))
                    except Exception:
                        pass
            log.info("ℹ️ Webhook sin mensajes (posible status update)")
            return jsonify({"ok": True}), 200

        msg = messages[0]
        phone = msg.get("from")
        if not phone:
            log.warning("⚠️ Mensaje sin número de teléfono")
            return jsonify({"ok": True}), 200

        # El cliente respondio: el recordatorio de "no respondio en una hora"
        # ya no aplica. Va antes de cualquier ruteo, para que valga con
        # cualquier tipo de mensaje (texto, boton, archivo).
        _cierre_cancelar_nudge(phone)

        last10 = _normalize_phone_last10(phone)
        match = match_client_in_sheets(last10)
        st_now = user_state.get(phone, "")
        idle = st_now in ("", "__greeted__")

        mtype = msg.get("type")
        if mtype == "text" and "text" in msg:
            text = (msg.get("text") or {}).get("body", "").strip()
            log.info("💬 Texto recibido de %s: %s", phone, text)

            try:
                append_respuesta_cliente(phone, _match_name(match), text, _utc_now_iso())
            except Exception:
                pass

            _ensure_user(phone)["last_message"] = text

            # Tramo final tras un cierre reciente ("gracias" / "no gracias").
            # Va antes del ruteo y es independiente de user_state y de
            # Boardroom: sin esto, un simple "gracias" caia en el fallback
            # generico ("En breve, su asesor Christian Lopez...") como si
            # fuera una consulta nueva.
            if not _is_active_funnel_state(st_now) and _cierre_manejar_cortesia(phone, text):
                return jsonify({"ok": True}), 200

            if SECOM_LOCAL_FALLBACK_ENABLED and _is_active_funnel_state(st_now):
                # ACTIVE_DETERMINISTIC_FUNNEL_TURN (DOC-0043 regla 3): continua
                # localmente sin bloquear en Boardroom por cada paso.
                _emit_boardroom_observation(phone, msg, match, mtype, text)
                _route_command(phone, text, match)
                return jsonify({"ok": True}), 200

            if SECOM_LOCAL_FALLBACK_ENABLED and st_now.startswith("awaiting_info:"):
                _emit_boardroom_observation(phone, msg, match, mtype, text)
                if _handle_awaiting_template_response(phone, text, match):
                    return jsonify({"ok": True}), 200
                _stateless_text_fallback(phone, text, match, idle, last10)
                return jsonify({"ok": True}), 200

            if BOARDROOM_IS_AUTHORITY:
                if SECOM_LOCAL_FALLBACK_ENABLED:
                    outcome, body = _consult_boardroom(phone, msg, match, mtype, text)
                    if outcome == "HANDLED":
                        _execute_handled_boardroom_instruction(phone, body)
                    else:
                        _stateless_text_fallback(phone, text, match, idle, last10)
                else:
                    _handle_boardroom_authority(phone, msg, match, mtype, text)
                return jsonify({"ok": True}), 200

            # HOTFIX 2: si hay estado activo local, NO entra Boardroom ni interceptores globales.
            active_local_state = user_state.get(phone, "").startswith(ACTIVE_FUNNEL_PREFIXES)
            log.info("🧭 Router input phone=%s state=%s text=%s", phone, user_state.get(phone, ""), text)

            if active_local_state:
                _route_command(phone, text, match)
                return jsonify({"ok": True}), 200

            if _handle_awaiting_template_response(phone, text, match):
                return jsonify({"ok": True}), 200

            _emit_bus_event(phone=phone, text=text)

            if BOARDROOM_ENABLED:
                boardroom_result = send_to_boardroom(
                    phone,
                    text,
                    match=match,
                    message_id=msg.get("id"),
                    state=user_state.get(phone, ""),
                )
                if execute_boardroom_decision(phone, boardroom_result, match=match):
                    return jsonify({"ok": True}), 200

            t_norm_info = text.strip().lower()
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
                            f"Nombre: {_match_name(match) or '(sin nombre)'}\n"
                            f"Mensaje: {text}"
                        )
                    except Exception:
                        pass
                    send_message(phone, "✅ Perfecto. Para recomendarte la mejor terminal Inbursa, dime: ¿*a qué giro* pertenece tu negocio?")
                    return jsonify({"ok": True}), 200

            if idle and match:
                if _auto_is_context(match) and _explicit_non_auto_intent(text):
                    log.info("🔀 Escape de flujo AUTO por intención explícita: %s", text)
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
                t_norm = text.strip().lower()
                greet_words = {
                    "hola", "buenas", "buenos dias", "buenos días", "buen dia", "buen día",
                    "buenas tardes", "buenas noches", "hey", "que tal", "qué tal", "holi",
                }
                if t_norm in greet_words:
                    base = "Dime qué necesitas y con gusto te guío para ayudarte a encontrar el servicio que necesitas."
                    nombre = _match_name(match)
                    send_message(phone, f"Hola {nombre} 👋 {base}" if nombre else f"Hola 👋 {base}")
                    user_state[phone] = "__greeted__"
                    return jsonify({"ok": True}), 200

                tpv_keywords = (
                    "tpv", "terminal", "terminales", "punto de venta", "punto-de-venta",
                    "cobrar con tarjeta", "cobro con tarjeta", "pagar con tarjeta",
                    "ligas de pago", "link de pago", "link pago", "cobro a distancia",
                )
                if any(k in t_norm for k in tpv_keywords):
                    user_state[phone] = "tpv_giro"
                    _notify_advisor(
                        "🧠 Interés detectado (TPV)\n"
                        f"WhatsApp: {phone}\n"
                        f"Nombre: {_match_name(match) or '(sin nombre)'}\n"
                        f"Mensaje: {text}"
                    )
                    send_message(phone, "✅ Perfecto. Para recomendarte la mejor terminal Inbursa, dime: ¿*a qué giro* pertenece tu negocio?")
                    return jsonify({"ok": True}), 200

            if idle and interpret_response(text) == "negative":
                send_message(phone, "Gracias por tu respuesta. Quedo a tus órdenes para cualquier duda o si más adelante deseas revisarlo.")
                user_state[phone] = "__greeted__"
                send_main_menu(phone)
                return jsonify({"ok": True}), 200

            t_lower = text.lower().strip()
            valid_commands = {
                "1", "2", "3", "4", "5", "6", "7",
                "menu", "menú", "inicio", "hola",
                "imss", "ley 73", "prestamo", "préstamo", "pension", "pensión",
                "auto", "seguro auto", "seguros de auto",
                "vida", "salud", "seguro de vida", "seguro de salud",
                "vrim", "tarjeta medica", "tarjeta médica",
                "empresarial", "pyme", "credito", "crédito", "credito empresarial", "crédito empresarial",
                "financiamiento", "financiamiento practico", "financiamiento práctico",
                "contactar", "asesor", "contactar con christian",
            }
            if not t_lower.isdigit() and t_lower not in valid_commands and idle:
                _notify_advisor(
                    "📩 Cliente INTERESADO / DUDA detectada\n"
                    f"WhatsApp: {phone}\n"
                    f"Mensaje: {text}"
                )

            if phone not in user_state:
                user_state[phone] = "__greeted__"
                if not match:
                    _greet_and_match(phone)

            if text.lower().startswith("sgpt:") and openai and OPENAI_API_KEY:
                prompt = text.split("sgpt:", 1)[1].strip()
                try:
                    log.info("🧠 Procesando solicitud GPT para %s", phone)
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
            log.info("📎 Multimedia recibida de %s: %s", phone, mtype)

            if SECOM_LOCAL_FALLBACK_ENABLED and _is_active_funnel_state(st_now):
                _emit_boardroom_observation(phone, msg, match, mtype, _message_text(msg, mtype))
                _handle_media(phone, msg)
                return jsonify({"ok": True}), 200

            if BOARDROOM_IS_AUTHORITY:
                if SECOM_LOCAL_FALLBACK_ENABLED:
                    outcome, body = _consult_boardroom(phone, msg, match, mtype, _message_text(msg, mtype))
                    if outcome == "HANDLED":
                        _execute_handled_boardroom_instruction(phone, body)
                    else:
                        _handle_media(phone, msg)
                else:
                    _handle_boardroom_authority(phone, msg, match, mtype, _message_text(msg, mtype))
                return jsonify({"ok": True}), 200
            _handle_media(phone, msg)
            return jsonify({"ok": True}), 200

        if mtype == "button":
            _btn = msg.get("button") or {}
            button_text = (_btn.get("text") or _btn.get("payload") or "").strip()
            if button_text:
                log.info("🔘 Botón Quick Reply de %s: %s", phone, button_text)
                try:
                    append_respuesta_cliente(phone, _match_name(match), button_text, _utc_now_iso())
                except Exception:
                    pass

                if SECOM_LOCAL_FALLBACK_ENABLED and _is_active_funnel_state(st_now):
                    _emit_boardroom_observation(phone, msg, match, mtype, button_text)
                    _route_command(phone, button_text, match)
                    return jsonify({"ok": True}), 200

                if BOARDROOM_IS_AUTHORITY:
                    if SECOM_LOCAL_FALLBACK_ENABLED:
                        outcome, body = _consult_boardroom(phone, msg, match, mtype, button_text)
                        if outcome == "HANDLED":
                            _execute_handled_boardroom_instruction(phone, body)
                        elif not _handle_awaiting_template_response(phone, button_text, match):
                            _route_command(phone, button_text, match)
                    else:
                        _handle_boardroom_authority(phone, msg, match, mtype, button_text)
                    return jsonify({"ok": True}), 200
                if _handle_awaiting_template_response(phone, button_text, match):
                    return jsonify({"ok": True}), 200
                _route_command(phone, button_text, match)
            return jsonify({"ok": True}), 200

        if BOARDROOM_IS_AUTHORITY:
            if SECOM_LOCAL_FALLBACK_ENABLED:
                outcome, body = _consult_boardroom(phone, msg, match, mtype or "unknown", "")
                if outcome == "HANDLED":
                    _execute_handled_boardroom_instruction(phone, body)
                # NOT_HANDLED/FAILED: sin accion local segura definida para
                # tipos de mensaje desconocidos -- se responde 200 sin enviar
                # nada, igual que el log "tipo no manejado" de mas abajo.
            else:
                _handle_boardroom_authority(phone, msg, match, mtype or "unknown", "")
            return jsonify({"ok": True}), 200

        log.info("ℹ️ Tipo de mensaje no manejado: %s", mtype)
        return jsonify({"ok": True}), 200

    except Exception:
        log.exception("❌ Error en webhook_receive")
        return jsonify({"ok": True}), 200


# ==========================
# Endpoints auxiliares
# ==========================
@app.get("/")
def index():
    return jsonify({"ok": True, "service": "Vicky Bot SECOM"}), 200


@app.get("/health")
def health():
    return jsonify({"status": "ok", "service": "Vicky Bot Inbursa", "timestamp": _utc_now_iso()}), 200


@app.get("/ext/health")
def ext_health():
    return jsonify({
        "status": "ok",
        "whatsapp_configured": bool(META_TOKEN and WABA_PHONE_ID),
        "google_ready": google_ready,
        "openai_ready": bool(openai and OPENAI_API_KEY),
        "boardroom_enabled": BOARDROOM_ENABLED,
    }), 200


@app.post("/ext/test-send")
def ext_test_send():
    try:
        token = (request.headers.get("X-AUTO-TOKEN") or "").strip()
        if not AUTO_SEND_TOKEN or token != AUTO_SEND_TOKEN:
            return jsonify({"ok": False, "error": "unauthorized"}), 401

        data = request.get_json(force=True) or {}
        to = str(data.get("to", "")).strip()
        text = str(data.get("text", "")).strip()
        if not to or not text:
            return jsonify({"ok": False, "error": "Faltan parámetros 'to' o 'text'"}), 400
        ok = send_message(to, text)
        return jsonify({"ok": bool(ok)}), 200
    except Exception as exc:
        log.exception("❌ Error en /ext/test-send")
        return jsonify({"ok": False, "error": str(exc)}), 500


def _bulk_send_worker(items: List[Dict[str, Any]]) -> None:
    """Worker de outbound proactivo. Requiere template; no envía texto libre proactivo."""
    successful = 0
    failed = 0
    log.info("🚀 Iniciando envío masivo de %s mensajes", len(items))

    for i, item in enumerate(items, 1):
        try:
            to = str(item.get("to", "")).strip()
            text = str(item.get("text", "")).strip()
            template = str(item.get("template", "")).strip()
            params = item.get("params") if "params" in item else None
            image_url = str(item.get("image_url") or item.get("header_image_url") or "").strip() or None
            components = item.get("components") if isinstance(item.get("components"), list) else None

            if not to:
                log.warning("⏭️ Item %s sin destinatario, omitiendo", i)
                failed += 1
                continue

            log.info("📤 [%s/%s] Procesando: %s", i, len(items), to)

            if not template and text:
                log.warning("⚠️ Outbound proactivo sin template rechazado para %s", to)
                failed += 1
                continue

            if template:
                success = send_template_message(
                    to,
                    template,
                    params=params,
                    image_url=image_url,
                    components=components,
                )
                log.info("   ↳ Plantilla '%s' a %s: %s", template, to, "✅" if success else "❌")
            else:
                log.warning("   ↳ Item %s sin contenido válido", i)
                failed += 1
                continue

            successful += 1 if success else 0
            failed += 0 if success else 1
            time.sleep(0.5)

        except Exception:
            failed += 1
            log.exception("❌ Error procesando item %s para %s", i, item.get("to", "unknown"))

    log.info("🎯 Envío masivo completado: %s ✅, %s ❌", successful, failed)

    if ADVISOR_NUMBER:
        send_message(ADVISOR_NUMBER, f"📊 Resumen envío masivo:\n• Exitosos: {successful}\n• Fallidos: {failed}\n• Total: {len(items)}")


@app.post("/ext/send-promo")
def ext_send_promo():
    """Endpoint de outbound proactivo. Solo encola templates; rechaza text sin template."""
    try:
        token = (request.headers.get("X-AUTO-TOKEN") or "").strip()
        if not AUTO_SEND_TOKEN or token != AUTO_SEND_TOKEN:
            return jsonify({"queued": False, "error": "unauthorized"}), 401

        if not META_TOKEN or not WABA_PHONE_ID:
            log.error("❌ META_TOKEN o WABA_PHONE_ID no configurados")
            return jsonify({"queued": False, "error": "WhatsApp Business API no configurada"}), 500

        body = request.get_json(force=True) or {}
        items = body.get("items", [])
        log.info("📨 Recibida solicitud send-promo con %s items", len(items) if isinstance(items, list) else "invalid")

        if not isinstance(items, list):
            return jsonify({"queued": False, "error": "Formato inválido: 'items' debe ser una lista"}), 400
        if not items:
            return jsonify({"queued": False, "error": "Lista 'items' vacía"}), 400

        valid_items: List[Dict[str, Any]] = []
        rejected_items: List[Dict[str, Any]] = []

        for i, item in enumerate(items):
            if not isinstance(item, dict):
                rejected_items.append({"index": i, "reason": "invalid_item"})
                continue

            to = str(item.get("to", "")).strip()
            text = str(item.get("text", "")).strip()
            template = str(item.get("template", "")).strip()

            if not to:
                rejected_items.append({"index": i, "reason": "missing_to"})
                continue

            if text and not template:
                log.warning("⚠️ Outbound proactivo sin template rechazado para %s", to)
                rejected_items.append({"index": i, "to": to, "reason": "outbound_requires_template"})
                continue

            if not template:
                rejected_items.append({"index": i, "to": to, "reason": "missing_template"})
                continue

            valid_items.append(item)

        if not valid_items:
            return jsonify({
                "queued": False,
                "error": "No hay items válidos para enviar",
                "failed": len(rejected_items),
                "rejected": rejected_items,
            }), 400

        threading.Thread(target=_bulk_send_worker, args=(valid_items,), daemon=True, name="BulkSendWorker").start()

        return jsonify({
            "queued": True,
            "message": f"Procesando {len(valid_items)} mensajes en background",
            "total_received": len(items),
            "valid_items": len(valid_items),
            "failed": len(rejected_items),
            "rejected": rejected_items,
            "timestamp": _utc_now_iso(),
        }), 202

    except Exception as exc:
        log.exception("❌ Error crítico en /ext/send-promo")
        return jsonify({"queued": False, "error": f"Error interno: {str(exc)}"}), 500



def _status_for_template(template_name: str) -> str:
    name = (template_name or "").strip().lower()
    if name == TPV_TEMPLATE_NAME:
        return "ENVIADO_TPV"
    if name in ALLIANCE_TEMPLATES:
        return "ENVIADO_ALIANZA"
    if name in SECOM_VIDA_TEMPLATES:
        return "ENVIADO_VIDA_TEMPORAL"
    if "vrim" in name:
        return "ENVIADO_VRIM"
    return "ENVIADO_TEMPLATE"

class ParamsFromRowError(ValueError):
    """params_from_row apunta a una columna que no existe o esta vacia."""


def _resolve_params_from_row(
    params_from_row: Any,
    headers: List[str],
    row: List[str],
) -> Dict[str, str] | List[str]:
    """Traduce la declaracion del cron en los parametros de body reales,
    leyendolos de la fila del lead ya seleccionada.

    - dict  {"nombre": "Nombre"}  -> {"nombre": "<valor de columna Nombre>"}
      (parametro nombrado: la clave es el nombre aprobado en Meta)
    - list  ["Nombre", "Monto"]   -> ["<Nombre>", "<Monto>"]
      (parametros posicionales, en el orden de la lista)

    No conoce ninguna plantilla: que parametros necesita cada campana lo
    declara el cron. Si una columna no existe o su valor esta vacio,
    levanta ParamsFromRowError para que el caller aborte ANTES de llamar
    a Meta y sin tocar la fila.
    """
    def _value_for(column_name: str, param_label: str) -> str:
        column_name = str(column_name).strip()
        if not column_name:
            raise ParamsFromRowError(
                f"params_from_row: columna vacia para el parametro '{param_label}'"
            )
        j = _idx(headers, column_name)
        if j is None:
            raise ParamsFromRowError(
                f"params_from_row: no existe la columna '{column_name}' "
                f"(parametro '{param_label}')"
            )
        value = _cell(row, j).strip()
        if not value:
            raise ParamsFromRowError(
                f"params_from_row: la columna '{column_name}' esta vacia "
                f"(parametro '{param_label}')"
            )
        return value

    if isinstance(params_from_row, dict):
        if not params_from_row:
            raise ParamsFromRowError("params_from_row: objeto vacio")
        return {
            str(param_name): _value_for(column_name, str(param_name))
            for param_name, column_name in params_from_row.items()
        }

    if isinstance(params_from_row, list):
        if not params_from_row:
            raise ParamsFromRowError("params_from_row: lista vacia")
        return [
            _value_for(column_name, f"posicion {i}")
            for i, column_name in enumerate(params_from_row, start=1)
        ]

    raise ParamsFromRowError("params_from_row debe ser objeto o lista")


def _pick_next_pending(headers: List[str], rows: List[List[str]]) -> Optional[Dict[str, Any]]:
    i_name = _idx(headers, "Nombre")
    i_wa = _idx(headers, "WhatsApp")
    i_status = _idx(headers, "ESTATUS")
    i_last = _idx(headers, "LAST_MESSAGE_AT")

    if i_name is None or i_wa is None:
        raise RuntimeError("Faltan columnas requeridas: 'Nombre' y/o 'WhatsApp'.")

    for row_number, row in enumerate(rows, start=2):
        wa = _cell(row, i_wa).strip()
        if not wa:
            continue

        last_at = _cell(row, i_last).strip() if i_last is not None else ""
        if last_at:
            continue

        estatus = _cell(row, i_status).strip().upper() if i_status is not None else ""
        if estatus not in ("", "PENDIENTE"):
            continue

        nombre = _cell(row, i_name).strip()
        # "row" se expone para poder resolver params_from_row contra las
        # columnas de esta misma fila. Las reglas de elegibilidad de arriba
        # no cambian.
        return {"row_number": row_number, "nombre": nombre, "whatsapp": wa, "row": row}

    return None


@app.post("/ext/auto-send-one")
def ext_auto_send_one():
    """Cron: envía 1 plantilla al siguiente prospecto pendiente."""
    try:
        token = (request.headers.get("X-AUTO-TOKEN") or "").strip()
        if not AUTO_SEND_TOKEN or token != AUTO_SEND_TOKEN:
            return jsonify({"ok": False, "error": "unauthorized"}), 401

        if _is_campaign_paused():
            return jsonify({"ok": True, "sent": False, "reason": "paused_by_boardroom"}), 200

        body = request.get_json(force=True, silent=True) or {}
        template_name = str(body.get("template", "")).strip()
        if not template_name:
            return jsonify({
                "ok": False,
                "reason": "template_required_for_business_initiated_message",
            }), 400

        # Configuracion de campana: la declara el cron, no el codigo. Aqui no
        # hay ninguna lista de plantillas conocidas -- una campana nueva solo
        # cambia el comando del cron.
        has_params = "params" in body
        has_params_from_row = "params_from_row" in body
        components = body.get("components") if isinstance(body.get("components"), list) else None

        if body.get("components") is not None and components is None:
            return jsonify({"ok": False, "error": "components debe ser una lista"}), 400

        if has_params and has_params_from_row:
            return jsonify({
                "ok": False,
                "error": "params y params_from_row son mutuamente excluyentes",
            }), 400

        if components is not None and (has_params or has_params_from_row):
            return jsonify({
                "ok": False,
                "error": "components no puede combinarse con params ni params_from_row",
            }), 400

        language = str(body.get("language") or DEFAULT_TEMPLATE_LANGUAGE).strip()
        if not _LANGUAGE_CODE_RE.match(language):
            return jsonify({"ok": False, "error": f"language inválido: {language}"}), 400

        success_status = body.get("success_status")
        if success_status is not None:
            success_status = str(success_status).strip()
            if not _SUCCESS_STATUS_RE.match(success_status):
                return jsonify({
                    "ok": False,
                    "error": "success_status inválido: solo A-Z, 0-9 y guion bajo",
                }), 400

        headers, rows = _sheet_get_rows()
        if not headers:
            return jsonify({"ok": False, "error": "Sheet vacío"}), 400

        nxt = _pick_next_pending(headers, rows)
        if not nxt:
            return jsonify({"ok": True, "sent": False, "reason": "no_pending"}), 200

        to = _normalize_to_e164_mx(nxt["whatsapp"])
        nombre = (nxt["nombre"] or "").strip() or "Cliente"
        image_url = str(body.get("image_url") or body.get("header_image_url") or "").strip() or None

        params = None
        if has_params:
            params = body.get("params")
        elif has_params_from_row:
            # Un dato faltante aborta ANTES de llamar a Meta y sin tocar la fila.
            try:
                params = _resolve_params_from_row(
                    body.get("params_from_row"), headers, nxt.get("row") or []
                )
            except ParamsFromRowError as exc:
                log.warning("⚠️ params_from_row inválido para '%s': %s", template_name, exc)
                return jsonify({
                    "ok": False,
                    "sent": False,
                    "error": str(exc),
                    "row": nxt["row_number"],
                    "template": template_name,
                }), 400

        ok = send_template_message(
            to,
            template_name,
            params=params,
            image_url=image_url,
            components=components,
            language=language,
        )

        if ok:
            user_state[to] = f"awaiting_info:{template_name}"
            data = _ensure_user(to)
            data["awaiting_info_started_at"] = _utc_now_iso()
        else:
            try:
                append_envio_status(to, "", "failed", template_name, _utc_now_iso())
            except Exception:
                pass

        auto_paused = _register_send_result(ok)

        now_iso = _utc_now_iso()
        if not ok:
            estatus_val = "FALLO_ENVIO"
        else:
            estatus_val = success_status or _status_for_template(template_name)
        _update_row_cells(nxt["row_number"], {"ESTATUS": estatus_val, "LAST_MESSAGE_AT": now_iso}, headers)

        response = {
            "ok": True,
            "sent": bool(ok),
            "to": to,
            "row": nxt["row_number"],
            "nombre": nombre,
            "template": template_name,
            "timestamp": now_iso,
        }
        if auto_paused:
            response["auto_paused"] = True
        return jsonify(response), 200

    except Exception as exc:
        log.exception("❌ Error en /ext/auto-send-one")
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/ext/boardroom/instruct", methods=["POST"])
def boardroom_instruct():
    """CF-4: recibe instrucciones de Boardroom. Alcance minimo: kill switch
    de la campana outbound (pause_outbound/resume_outbound). Reusa
    BUS_INTERNAL_TOKEN -- ya es el secreto compartido entre Boardroom y este
    servicio, evita provisionar uno nuevo."""
    token = (request.headers.get("X-Internal-Token") or "").strip()
    if not BUS_INTERNAL_TOKEN or token != BUS_INTERNAL_TOKEN:
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    body = request.get_json(silent=True) or {}
    instruction = str(body.get("instruction", "") or "").strip()

    if instruction == "pause_outbound":
        try:
            _set_campaign_paused(True)
        except Exception as exc:
            log.exception("❌ Error aplicando pause_outbound")
            return jsonify({"ok": False, "error": str(exc)}), 500
        log.warning("⏸️ Campana outbound pausada por Boardroom")
        return jsonify({"ok": True, "instruction": instruction, "paused": True}), 200

    if instruction == "resume_outbound":
        try:
            _set_campaign_paused(False)
        except Exception as exc:
            log.exception("❌ Error aplicando resume_outbound")
            return jsonify({"ok": False, "error": str(exc)}), 500
        log.warning("▶️ Campana outbound reanudada por Boardroom")
        return jsonify({"ok": True, "instruction": instruction, "paused": False}), 200

    return jsonify({"ok": False, "error": f"instruction desconocida: {instruction}"}), 400


if __name__ == "__main__":
    log.info("🚀 Iniciando Vicky Bot SECOM en puerto %s", PORT)
    log.info("📞 WhatsApp configurado: %s", bool(META_TOKEN and WABA_PHONE_ID))
    log.info("📊 Google Sheets/Drive: %s", google_ready)
    log.info("🧠 OpenAI: %s", bool(openai and OPENAI_API_KEY))
    app.run(host="0.0.0.0", port=PORT, debug=False)
