# app.py — Vicky SECOM (Vicky WAPI + Campañas + Recordatorios + Forward Docs)
from __future__ import annotations
import os, re, json, time, logging, threading
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, List

import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# Google Sheets API
try:
    from google.oauth2.service_account import Credentials as ServiceAccountCredentials
    from googleapiclient.discovery import build as gbuild
except Exception:
    ServiceAccountCredentials = None
    gbuild = None

# OpenAI opcional
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
# WhatsApp helpers
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
    """Envía mensaje de texto simple."""
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
    """Envía mensaje de plantilla."""
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


def _send_media(to: str, mtype: str, media_id: str, filename: Optional[str] = None, caption: str = "") -> bool:
    """
    Reenvía un media existente (id) al número indicado.
    Soporta image, document, audio, video.
    """
    if not (META_TOKEN and WPP_API_URL):
        log.error("❌ WhatsApp API no configurada para media")
        return False

    if mtype not in ("image", "document", "audio", "video"):
        log.error(f"❌ Tipo de media no soportado para enviar: {mtype}")
        return False

    media_obj: Dict[str, Any] = {"id": media_id}
    if filename and mtype == "document":
        media_obj["filename"] = filename
    if caption:
        media_obj["caption"] = caption[:1024]

    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": mtype,
        mtype: media_obj,
    }

    try:
        r = requests.post(WPP_API_URL, headers=_headers(), json=payload, timeout=20)
        if r.status_code == 200:
            log.info(f"📤 Media reenviado a {to} ({mtype}, id={media_id})")
            return True
        log.warning(f"⚠️ Error al reenviar media {r.status_code} {r.text[:300]!r}")
        return False
    except Exception:
        log.exception("❌ Error en _send_media")
        return False


def forward_media_to_advisor(origin_phone: str, mtype: str, msg: Dict[str, Any]) -> None:
    """
    Reenvía el archivo recibido al asesor (ADVISOR_NUMBER) usando el mismo media_id.
    Esto asegura que tengas los documentos para cotizar.
    """
    if not ADVISOR_NUMBER:
        return
    try:
        media = msg.get(mtype) or {}
        media_id = media.get("id")
        if not media_id:
            log.warning("⚠️ No se encontró media_id para reenviar")
            return
        filename = media.get("filename")
        caption = f"Documento reenviado de {origin_phone}"
        _send_media(ADVISOR_NUMBER, mtype, media_id, filename=filename, caption=caption)
    except Exception:
        log.exception("❌ Error reenviando media al asesor")


# ==========================
# Google Sheets (SECOM) - lectura/escritura
# ==========================
sheets = None
google_ready = False

if GOOGLE_CREDENTIALS_JSON and ServiceAccountCredentials and gbuild and SHEETS_ID_LEADS:
    try:
        info = json.loads(GOOGLE_CREDENTIALS_JSON)
        creds = ServiceAccountCredentials.from_service_account_info(
            info,
            scopes=[
                "https://www.googleapis.com/auth/spreadsheets",
            ],
        )
        sheets = gbuild("sheets", "v4", credentials=creds)
        google_ready = True
        log.info("✅ Google Sheets configurado (RW)")
    except Exception:
        log.exception("❌ Error configurando Google Sheets")


def _col_letter(col: int) -> str:
    res = ""
    while col > 0:
        col, rem = divmod(col - 1, 26)
        res = chr(65 + rem) + res
    return res


def _find_col(headers: List[str], names: List[str]) -> Optional[int]:
    if not headers:
        return None
    low = [h.strip().lower() for h in headers]
    for name in names:
        n = name.strip().lower()
        if n in low:
            return low.index(n)
    return None


def _get_sheet_headers_and_rows() -> tuple[List[str], List[List[str]]]:
    if not (google_ready and sheets and SHEETS_ID_LEADS and SHEETS_TITLE_LEADS):
        return [], []
    rng = f"{SHEETS_TITLE_LEADS}!A:Z"
    res = sheets.spreadsheets().values().get(
        spreadsheetId=SHEETS_ID_LEADS, range=rng
    ).execute()
    rows = res.get("values", [])
    if not rows:
        return [], []
    headers = rows[0]
    data_rows = rows[1:]
    return headers, data_rows


def _batch_update_cells(row_index: int, updates: Dict[str, str], headers: List[str]) -> None:
    """
    Actualiza celdas por nombre de columna en la fila indicada (2 = primera fila de datos).
    Ignora columnas que no existan.
    """
    if not (google_ready and sheets and SHEETS_ID_LEADS and SHEETS_TITLE_LEADS):
        return
    if row_index < 2:
        return

    header_low = [h.strip().lower() for h in headers]
    data_ranges = []

    for key, value in updates.items():
        key_low = key.strip().lower()
        if key_low in header_low:
            idx = header_low.index(key_low) + 1  # 1-based
        else:
            continue
        col_letter = _col_letter(idx)
        cell_range = f"{SHEETS_TITLE_LEADS}!{col_letter}{row_index}"
        data_ranges.append({"range": cell_range, "values": [[str(value)]]})

    if not data_ranges:
        return

    body = {
        "valueInputOption": "RAW",
        "data": data_ranges,
    }
    try:
        sheets.spreadsheets().values().batchUpdate(
            spreadsheetId=SHEETS_ID_LEADS, body=body
        ).execute()
    except Exception:
        log.exception("❌ Error en _batch_update_cells")


def match_client_in_sheets(phone: str) -> Optional[Dict[str, Any]]:
    """Devuelve nombre del cliente si el WhatsApp coincide en la hoja de leads."""
    if not (google_ready and sheets and SHEETS_ID_LEADS and SHEETS_TITLE_LEADS):
        return None
    try:
        headers, rows = _get_sheet_headers_and_rows()
        if not headers or not rows:
            return None

        idx_name = _find_col(headers, ["Nombre", "CLIENTE", "Cliente"])
        idx_wa = _find_col(headers, ["WhatsApp", "Whatsapp", "WHATSAPP", "Telefono", "Teléfono", "CELULAR"])
        if idx_wa is None:
            return None

        target = _normalize_phone(phone)
        for row in rows:
            if len(row) <= idx_wa:
                continue
            tel = _normalize_phone(row[idx_wa])
            if tel == target:
                nombre = row[idx_name] if idx_name is not None and len(row) > idx_name else ""
                return {"nombre": nombre}
        return None
    except Exception:
        log.exception("❌ Error en match_client_in_sheets")
        return None


def _touch_last_inbound(phone: str) -> None:
    """
    Marca la última actividad entrante (cliente → bot) en LAST_MESSAGE_AT / LastInboundAt si existen.
    """
    if not (google_ready and sheets and SHEETS_ID_LEADS and SHEETS_TITLE_LEADS):
        return
    try:
        headers, rows = _get_sheet_headers_and_rows()
        if not headers or not rows:
            return

        idx_wa = _find_col(headers, ["WhatsApp", "Whatsapp", "WHATSAPP", "Telefono", "Teléfono", "CELULAR"])
        idx_last = _find_col(headers, ["LastInboundAt", "LAST_INBOUND_AT", "LAST_MESSAGE_AT"])
        if idx_wa is None or idx_last is None:
            return

        target = _normalize_phone(phone)

        for offset, row in enumerate(rows, start=2):
            if len(row) <= idx_wa:
                continue
            if _normalize_phone(row[idx_wa]) == target:
                col_letter = _col_letter(idx_last + 1)
                cell_range = f"{SHEETS_TITLE_LEADS}!{col_letter}{offset}"
                body = {
                    "range": cell_range,
                    "majorDimension": "ROWS",
                    "values": [[datetime.utcnow().isoformat()]],
                }
                sheets.spreadsheets().values().update(
                    spreadsheetId=SHEETS_ID_LEADS,
                    range=cell_range,
                    valueInputOption="RAW",
                    body=body,
                ).execute()
                break
    except Exception:
        log.exception("❌ Error registrando LAST_MESSAGE_AT")


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
        if not st and intent == "positive" and match:
            send_message(
                phone,
                "Perfecto ✅ Iniciemos con la revisión gratuita de tu seguro de auto."
            )
            auto_start(phone, match)
            return
        send_message(
            phone,
            "Perfecto ✅ Iniciemos con la revisión gratuita de tu seguro de auto."
        )
        auto_start(phone, match)
        return
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

        # Registrar última actividad
        _touch_last_inbound(phone)

        match = match_client_in_sheets(phone)
        mtype = msg.get("type")

            if mtype == "text":
        text = msg.get("text", {}).get("body", "")
        log.info(f"💬 Texto de {phone}: {text!r}")

        # --- 🔔 Notificación automática al asesor ---
        try:
            if ADVISOR_NUMBER and phone:
                preview = text[:120] if text else "(sin texto)"
                notify_msg = (
                    f"📩 *Nuevo mensaje recibido por Vicky*\n"
                    f"De: +{phone}\n"
                    f"Mensaje: {preview}"
                )
                send_message(ADVISOR_NUMBER, notify_msg)
                log.info(f"📨 Notificación enviada al asesor: {phone}")
        except Exception:
            log.exception("❌ Error al enviar notificación automática al asesor")


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
            # Reenvía el mismo media al asesor para que tenga la documentación
            forward_media_to_advisor(phone, mtype, msg)
            return jsonify({"ok": True}), 200

        log.info(f"ℹ️ Tipo de mensaje no manejado: {mtype}")
        return jsonify({"ok":True}), 200

    except Exception:
        log.exception("❌ Error en webhook_receive")
        return jsonify({"ok": True}), 200


# ==========================
# Endpoints externos básicos
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


# ==========================
# Worker envíos masivos manual (lista explícita)
# ==========================
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
    """Endpoint histórico para campañas donde envías la lista completa de items."""
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
# Envío masivo SECOM desde Sheets (WAPI)
# ==========================
def _bulk_send_from_sheets_worker(
    message_template: str,
    use_sheet_message: bool,
    limit: Optional[int] = None,
) -> None:
    """
    Lee la hoja de leads SECOM y envía mensajes uno a uno (60s).
    Reglas:
      - Usa columna WhatsApp / Teléfono.
      - Usa Mensaje_Base si existe y use_sheet_message=True.
      - Solo envía si:
          * FirstSentAt vacío
          * Status/ESTATUS != NO_INTERESADO, CERRADO
      - Marca:
          * FirstSentAt = ahora
          * Status/ESTATUS = ENVIADO_INICIAL
    """
    if not (google_ready and sheets and SHEETS_ID_LEADS and SHEETS_TITLE_LEADS):
        log.error("[SECOM-PROMO] Google Sheets no configurado")
        return

    try:
        headers, rows = _get_sheet_headers_and_rows()
        if not headers or not rows:
            log.warning("[SECOM-PROMO] Hoja vacía")
            return

        idx_name = _find_col(headers, ["Nombre", "CLIENTE", "Cliente"])
        idx_wa = _find_col(headers, ["WhatsApp", "Whatsapp", "WHATSAPP", "Telefono", "Teléfono", "CELULAR"])
        idx_status = _find_col(headers, ["Status", "ESTATUS"])
        idx_first = _find_col(headers, ["FirstSentAt", "FIRST_SENT_AT"])
        idx_msg_base = _find_col(headers, ["Mensaje_Base", "MENSAJE_BASE"])

        if idx_wa is None:
            log.error("[SECOM-PROMO] No se encontró columna de WhatsApp")
            return

        now_iso = datetime.utcnow().isoformat()
        enviados = 0
        fallidos = 0

        for offset, row in enumerate(rows, start=2):
            if limit is not None and enviados >= limit:
                break

            if len(row) <= idx_wa:
                continue

            phone_raw = row[idx_wa]
            norm = _normalize_phone(str(phone_raw))
            if not norm:
                continue

            status_val = row[idx_status] if idx_status is not None and len(row) > idx_status else ""
            status_up = str(status_val).strip().upper()

            first_val = row[idx_first] if idx_first is not None and len(row) > idx_first else ""
            first_exists = bool(str(first_val).strip())

            if first_exists:
                continue
            if status_up in ("NO_INTERESADO", "NO INTERESADO", "CERRADO"):
                continue

            name = row[idx_name].strip() if idx_name is not None and len(row) > idx_name else ""

            msg = ""
            if use_sheet_message and idx_msg_base is not None and len(row) > idx_msg_base:
                msg = str(row[idx_msg_base] or "").strip()
            if not msg:
                msg = str(message_template or "").strip()
            if not msg:
                continue

            msg = msg.replace("{{nombre}}", name if name else "Hola")

            to = str(phone_raw).strip()
            if not to.startswith("52"):
                to = f"52{norm}"

            if send_message(to, msg):
                updates = {"FirstSentAt": now_iso}
                if idx_status is not None:
                    updates[headers[idx_status]] = "ENVIADO_INICIAL"
                _batch_update_cells(offset, updates, headers)
                enviados += 1
                log.info(f"[SECOM-PROMO] Enviado a {to} fila {offset}")
            else:
                fallidos += 1
                log.warning(f"[SECOM-PROMO] Falló envío a {to} fila {offset}")

            time.sleep(60)

        log.info(
            f"[SECOM-PROMO] Finalizado. Enviados={enviados} Fallidos={fallidos}"
        )
        if ADVISOR_NUMBER:
            send_message(
                ADVISOR_NUMBER,
                f"📊 Envío masivo SECOM finalizado.\nExitosos: {enviados}\nFallidos: {fallidos}",
            )
    except Exception:
        log.exception("❌ Error en _bulk_send_from_sheets_worker")


@app.post("/ext/send-promo-secom")
def ext_send_promo_secom():
    """
    Envío masivo SECOM desde Google Sheets.

    Body JSON:
    {
      "message": "Texto base con {{nombre}}",   # opcional si se usa Mensaje_Base
      "use_sheet_message": true,                # por defecto true
      "limit": 100                              # opcional
    }
    """
    try:
        if not (META_TOKEN and WABA_PHONE_ID):
            return jsonify({"ok": False, "error": "WhatsApp API no configurada"}), 500
        if not (google_ready and sheets and SHEETS_ID_LEADS and SHEETS_TITLE_LEADS):
            return jsonify({"ok": False, "error": "Google Sheets no configurado"}), 500

        data = request.get_json(force=True) or {}
        message_template = (data.get("message") or "").strip()
        use_sheet_message = bool(data.get("use_sheet_message", True))
        limit = data.get("limit")

        if not message_template and not use_sheet_message:
            return jsonify(
                {
                    "ok": False,
                    "error": "Debes enviar 'message' o activar 'use_sheet_message'.",
                }
            ), 400

        t = threading.Thread(
            target=_bulk_send_from_sheets_worker,
            args=(message_template, use_sheet_message, limit),
            daemon=True,
        )
        t.start()

        return jsonify(
            {
                "ok": True,
                "status": "queued",
                "timestamp": datetime.utcnow().isoformat(),
            }
        ), 202

    except Exception as e:
        log.exception("❌ Error en /ext/send-promo-secom")
        return jsonify({"ok": False, "error": str(e)}), 500


# ==========================
# Recordatorios 3 y 5 días
# ==========================
def _parse_iso(dt_str: str) -> Optional[datetime]:
    if not dt_str:
        return None
    try:
        return datetime.fromisoformat(str(dt_str).replace("Z", ""))
    except Exception:
        return None


def _start_reminders_worker() -> None:
    """
    Worker que cada hora revisa la hoja y envía recordatorios:
      - 3 días después de FirstSentAt (si no respondió).
      - 5 días después de FirstSentAt (si no respondió).
    """
    if not (google_ready and sheets and SHEETS_ID_LEADS and SHEETS_TITLE_LEADS):
        log.info("[REMINDERS] Sheets no configurado; worker no iniciado")
        return

    def worker():
        while True:
            try:
                headers, rows = _get_sheet_headers_and_rows()
                if not headers or not rows:
                    time.sleep(3600)
                    continue

                idx_wa = _find_col(headers, ["WhatsApp", "Whatsapp", "WHATSAPP", "Telefono", "Teléfono", "CELULAR"])
                idx_status = _find_col(headers, ["Status", "ESTATUS"])
                idx_first = _find_col(headers, ["FirstSentAt", "FIRST_SENT_AT"])
                idx_rem3 = _find_col(headers, ["Reminder3Sent", "REMINDER3", "REM3"])
                idx_rem5 = _find_col(headers, ["Reminder5Sent", "REMINDER5", "REM5"])
                idx_last = _find_col(headers, ["LastInboundAt", "LAST_INBOUND_AT", "LAST_MESSAGE_AT"])
                idx_name = _find_col(headers, ["Nombre", "CLIENTE", "Cliente"])

                if idx_wa is None or idx_first is None:
                    time.sleep(3600)
                    continue

                now = datetime.utcnow()

                for offset, row in enumerate(rows, start=2):
                    if len(row) <= idx_wa:
                        continue

                    phone_raw = row[idx_wa]
                    norm = _normalize_phone(str(phone_raw))
                    if not norm:
                        continue

                    first_val = row[idx_first] if len(row) > idx_first else ""
                    first_dt = _parse_iso(str(first_val).strip())
                    if not first_dt:
                        continue

                    status_val = row[idx_status] if idx_status is not None and len(row) > idx_status else ""
                    status_up = str(status_val).strip().upper()
                    if status_up in ("NO_INTERESADO", "NO INTERESADO", "CERRADO"):
                        continue

                    days = (now - first_dt).days

                    rem3_val = row[idx_rem3] if idx_rem3 is not None and len(row) > idx_rem3 else ""
                    rem5_val = row[idx_rem5] if idx_rem5 is not None and len(row) > idx_rem5 else ""

                    last_in_val = row[idx_last] if idx_last is not None and len(row) > idx_last else ""
                    last_in_dt = _parse_iso(str(last_in_val).strip())

                    inactive = (last_in_dt is None) or (last_in_dt <= first_dt)

                    name = row[idx_name] if idx_name is not None and len(row) > idx_name else "Hola"
                    name = str(name).strip() or "Hola"

                    to = str(phone_raw).strip()
                    if not to.startswith("52"):
                        to = f"52{norm}"

                    # Recordatorio 3 días
                    if days >= 3 and str(rem3_val).strip().upper() != "YES" and inactive:
                        msg3 = (
                            f"{name}, solo para recordarte que tenemos lista tu propuesta de seguro de auto "
                            "con beneficio especial para ti. Si gustas te ayudo a revisarla por aquí mismo. 🚗"
                        )
                        if send_message(to, msg3):
                            updates = {"Reminder3Sent": "YES"}
                            if idx_status is not None:
                                updates[headers[idx_status]] = "RECORDATORIO_3D"
                            _batch_update_cells(offset, updates, headers)

                    # Recordatorio 5 días
                    if days >= 5 and str(rem5_val).strip().upper() != "YES" and inactive:
                        msg5 = (
                            f"{name}, confirmo si aún te interesa aprovechar tu beneficio preferencial "
                            "en tu seguro de auto. Si quieres retomamos tu trámite por este medio. ✅"
                        )
                        if send_message(to, msg5):
                            updates = {"Reminder5Sent": "YES"}
                            if idx_status is not None:
                                updates[headers[idx_status]] = "RECORDATORIO_5D"
                            _batch_update_cells(offset, updates, headers)

                time.sleep(3600)

            except Exception:
                log.exception("❌ Error en ciclo de recordatorios")
                time.sleep(3600)

    threading.Thread(target=worker, daemon=True).start()


# Iniciar worker de recordatorios al cargar la app
_start_reminders_worker()


# ==========================
# Arranque local
# ==========================
if __name__ == "__main__":
    log.info(f"🚀 Iniciando Vicky Bot SECOM en puerto {PORT}")
    log.info(f"📞 WhatsApp configurado: {bool(META_TOKEN and WABA_PHONE_ID)}")
    log.info(f"📊 Google listo: {google_ready}")
    log.info(f"🧠 OpenAI listo: {bool(openai and OPENAI_API_KEY)}")
    app.run(host="0.0.0.0", port=PORT, debug=False)
