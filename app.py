# -*- coding: utf-8 -*-
import os
import json
import logging
import requests
import re
import threading
import tempfile
from time import time
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from dotenv import load_dotenv

import pytz
from google.oauth2.service_account import Credentials
import gspread
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# ==============================
# Configuración base
# ==============================
load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("vicky")

app = Flask(__name__)

# Entorno
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
META_TOKEN = os.getenv("META_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")  # usado por funciones directas
WABA_PHONE_ID = os.getenv("WABA_PHONE_ID") or PHONE_NUMBER_ID  # usado por helpers VX
ADVISOR_WHATSAPP = os.getenv("ADVISOR_WHATSAPP", "5216682478005")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

SHEETS_ID_LEADS = os.getenv("SHEETS_ID_LEADS")
SHEETS_TITLE_LEADS = os.getenv("SHEETS_TITLE_LEADS")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")

DRIVE_FOLDER_CLIENTS_ID = os.getenv("DRIVE_FOLDER_CLIENTS_ID")  # raíz clientes
DRIVE_FOLDER_MANUALES_ID = os.getenv("DRIVE_FOLDER_MANUALES_ID")  # carpeta manuales (contiene "Procedimiento IMSS")

TZ = pytz.timezone("America/Mazatlan")

# ==============================
# Estado en memoria con TTL
# ==============================
MSG_TTL = 600            # 10 minutos
GREET_TTL = 24 * 3600    # 24h
CTX_TTL = 4 * 3600       # 4h
IMSS_CACHE_TTL = 30 * 60 # 30 min
NOTIFY_TTL = 10 * 60     # 10 min para evitar spam al asesor

PROCESSED_MESSAGE_IDS = {}   # {msg_id: ts}
GREETED_USERS = {}           # {wa_id: ts}
LAST_INTENT = {}             # {wa_id: {"opt":..., "title":..., "ts":...}}
USER_CONTEXT = {}            # {wa_id: {"ctx":str, "step":str, "ts":float, "data":dict}}
CONTACT_NOTIFIES = {}        # {wa_id: ts} para deduplicar opción 7
IMSS_MANUAL_CACHE = {"ts": 0, "text": None}

# ==============================
# Utilidades generales
# ==============================
def now_ts():
    return time()

def vx_last10(phone: str) -> str:
    if not phone:
        return ""
    p = re.sub(r"[^\d]", "", str(phone))
    p = re.sub(r"^(52|521)", "", p)
    return p[-10:] if len(p) >= 10 else p

def _cleanup_dict(d: dict, ttl: int, now=None):
    n = now or now_ts()
    for k in list(d.keys()):
        v = d[k]
        last = v if isinstance(v, (int, float)) else v.get("ts", n)
        if n - last > ttl:
            d.pop(k, None)

# ==============================
# Google APIs
# ==============================
def _drive_service():
    creds = Credentials.from_service_account_info(json.loads(GOOGLE_CREDENTIALS_JSON))
    return build("drive", "v3", credentials=creds, cache_discovery=False)

def _gspread_client():
    creds = Credentials.from_service_account_info(json.loads(GOOGLE_CREDENTIALS_JSON))
    return gspread.authorize(creds)

def vx_sheet_find_by_phone(last10: str):
    try:
        if not (SHEETS_ID_LEADS and SHEETS_TITLE_LEADS and last10):
            return None
        client = _gspread_client()
        ws = client.open_by_key(SHEETS_ID_LEADS).worksheet(SHEETS_TITLE_LEADS)
        rows = ws.get_all_records()
        for row in rows:
            if vx_last10(row.get("WhatsApp", "")) == last10:
                return row
        return None
    except Exception as e:
        log.error(f"vx_sheet_find_by_phone error: {e}")
        return None

def drive_find_or_create_folder(parent_id: str, name: str) -> str:
    """Busca una carpeta por nombre dentro de parent; si no existe, la crea."""
    svc = _drive_service()
    # Buscar
    q = "mimeType='application/vnd.google-apps.folder' and trashed=false and name=%s and '%s' in parents" % (
        json.dumps(name), parent_id
    )
    res = svc.files().list(q=q, fields="files(id,name)", pageSize=1).execute()
    files = res.get("files", [])
    if files:
        return files[0]["id"]
    # Crear
    meta = {"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]}
    created = svc.files().create(body=meta, fields="id").execute()
    return created["id"]

def drive_upload_bytes(parent_id: str, filename: str, content: bytes, mimetype: str = None) -> str:
    """Sube bytes como archivo a Drive, usando archivo temporal."""
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        tmp.write(content)
        tmp_path = tmp.name
    try:
        svc = _drive_service()
        meta = {"name": filename, "parents": [parent_id]}
        media = MediaFileUpload(tmp_path, mimetype=mimetype, resumable=True)
        up = svc.files().create(body=meta, media_body=media, fields="id").execute()
        return up.get("id")
    finally:
        try:
            os.remove(tmp_path)
        except Exception:
            pass

# ==============================
# WhatsApp helpers
# ==============================
def vx_wa_send_text(to_e164: str, body: str):
    if not (META_TOKEN and WABA_PHONE_ID and to_e164 and body):
        log.warning("vx_wa_send_text: falta configuración o parámetros")
        return False
    url = f"https://graph.facebook.com/v20.0/{WABA_PHONE_ID}/messages"
    headers = {"Authorization": f"Bearer {META_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to_e164, "type": "text", "text": {"body": body}}
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=12)
        log.info(f"vx_wa_send_text: {r.status_code} {r.text[:160]}")
        return r.status_code == 200
    except Exception as e:
        log.error(f"vx_wa_send_text error: {e}")
        return False

def vx_wa_send_template(to_e164: str, template_name: str, params: dict | None = None, lang_code="es_MX"):
    if not (META_TOKEN and WABA_PHONE_ID and to_e164 and template_name):
        log.warning("vx_wa_send_template: falta configuración o parámetros")
        return False
    components = []
    if params:
        components = [{
            "type": "body",
            "parameters": [{"type": "text", "text": str(v)} for v in params.values()]
        }]
    url = f"https://graph.facebook.com/v20.0/{WABA_PHONE_ID}/messages"
    headers = {"Authorization": f"Bearer {META_TOKEN}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "to": to_e164,
        "type": "template",
        "template": {"name": template_name, "language": {"code": lang_code}}
    }
    if components:
        payload["template"]["components"] = components
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=15)
        log.info(f"vx_wa_send_template: {r.status_code} {r.text[:160]}")
        return r.status_code == 200
    except Exception as e:
        log.error(f"vx_wa_send_template error: {e}")
        return False

def vx_wa_mark_read(message_id: str):
    if not (META_TOKEN and WABA_PHONE_ID and message_id):
        return False
    url = f"https://graph.facebook.com/v20.0/{WABA_PHONE_ID}/messages"
    headers = {"Authorization": f"Bearer {META_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "status": "read", "message_id": message_id}
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=9)
        log.info(f"vx_wa_mark_read: {r.status_code}")
        return r.status_code == 200
    except Exception as e:
        log.error(f"vx_wa_mark_read error: {e}")
        return False

# Medios (descargar/reenviar)
def _get_media_url(media_id: str) -> str | None:
    try:
        r = requests.get(f"https://graph.facebook.com/v21.0/{media_id}",
                         headers={"Authorization": f"Bearer {META_TOKEN}"}, timeout=8)
        if r.status_code == 200:
            return r.json().get("url")
        log.warning(f"_get_media_url {r.status_code}: {r.text[:160]}")
    except Exception as e:
        log.error(f"_get_media_url error: {e}")
    return None

def _download_media_bytes(url: str) -> bytes | None:
    try:
        r = requests.get(url, headers={"Authorization": f"Bearer {META_TOKEN}"}, timeout=15)
        if r.status_code == 200:
            return r.content
        log.warning(f"_download_media_bytes {r.status_code}: {r.text[:160]}")
    except Exception as e:
        log.error(f"_download_media_bytes error: {e}")
    return None

def send_media_image(to: str, media_id: str, caption: str = ""):
    url = f"https://graph.facebook.com/v21.0/{WABA_PHONE_ID}/messages"
    headers = {"Authorization": f"Bearer {META_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, "type": "image", "image": {"id": media_id}}
    if caption:
        payload["image"]["caption"] = caption
    r = requests.post(url, headers=headers, json=payload, timeout=15)
    log.info(f"send_media_image: {r.status_code} {r.text[:160]}")

def send_media_document(to: str, media_id: str, caption: str = ""):
    url = f"https://graph.facebook.com/v21.0/{WABA_PHONE_ID}/messages"
    headers = {"Authorization": f"Bearer {META_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, "type": "document", "document": {"id": media_id}}
    if caption:
        payload["document"]["caption"] = caption
    r = requests.post(url, headers=headers, json=payload, timeout=15)
    log.info(f"send_media_document: {r.status_code} {r.text[:160]}")

# ==============================
# Menú base
# ==============================
MENU_TEXT = (
    "👉 Elige una opción del menú:\n"
    "1) Asesoría en pensiones IMSS (Ley 73 / Modalidad 40 / Modalidad 10)\n"
    "2) Seguros de auto (Amplia PLUS, Amplia, Limitada)\n"
    "3) Seguros de vida y salud\n"
    "4) Tarjetas médicas VRIM\n"
    "5) Préstamos a pensionados IMSS (desde $40,000 hasta $650,000)\n"
    "6) Financiamiento empresarial y nómina empresarial\n"
    "7) Contactar con Christian\n"
    "\nEscribe el número de la opción o 'menu' para volver a ver el menú."
)

OPTION_RESPONSES = {
    "1": "🧓 Asesoría en pensiones IMSS. Cuéntame tu caso (Ley 73, M40, M10) y te guío paso a paso.",
    "2": "🚗 Para cotizar: envíame *foto de tu INE* y *tarjeta de circulación* o tu *número de placa*.",
    "3": "🛡️ Seguros de vida y salud. Te preparo una cotización personalizada.",
    "4": "🩺 Tarjetas médicas VRIM. Te comparto información y precios.",
    "5": "💳 Préstamos a pensionados IMSS. Dime tu pensión mensual aproximada y el monto deseado.",
    "6": "🏢 Financiamiento empresarial y nómina. ¿Qué necesitas: *crédito*, *factoraje* o *nómina*?",
    "7": "📞 ¡Listo! He notificado a Christian para que te contacte y te dé seguimiento."
}

KEYWORD_INTENTS = [
    (("pension", "pensión", "imss", "modalidad 40", "modalidad 10", "ley 73"), "1"),
    (("auto", "seguro de auto", "placa", "tarjeta de circulación", "tarjeta de circulacion", "coche", "carro"), "2"),
    (("vida", "seguro de vida", "salud", "gastos médicos", "gastos medicos"), "3"),
    (("vrim", "tarjeta médica", "tarjeta medica", "membresía médica", "membresia medica"), "4"),
    (("préstamo", "prestamo", "pensionado", "préstamo imss", "prestamo imss"), "5"),
    (("financiamiento", "factoraje", "nómina", "nomina", "empresarial", "crédito empresarial", "credito empresarial"), "6"),
    (("contacto", "contactar", "asesor", "christian", "llámame", "llamame", "quiero hablar"), "7"),
]

def infer_option_from_text(t: str):
    tn = t.lower()
    for keywords, opt in KEYWORD_INTENTS:
        if any(k in tn for k in keywords):
            return opt
    return None

# ==============================
# IMSS – lectura de manual y utilidades
# ==============================
def imss_manual_text() -> str | None:
    """Lee el manual 'Procedimiento IMSS' desde DRIVE_FOLDER_MANUALES_ID. Cachea 30 min."""
    now = now_ts()
    if IMSS_MANUAL_CACHE["text"] and (now - IMSS_MANUAL_CACHE["ts"] < IMSS_CACHE_TTL):
        return IMSS_MANUAL_CACHE["text"]

    try:
        svc = _drive_service()
        q = "trashed=false and '%s' in parents" % DRIVE_FOLDER_MANUALES_ID
        res = svc.files().list(q=q, fields="files(id,name,mimeType)", pageSize=50).execute()
        files = res.get("files", [])
        target = None
        for f in files:
            if "procedimiento imss" in f["name"].lower():
                target = f
                break
        if not target:
            log.warning("Manual IMSS no encontrado en Drive.")
            return None

        file_id = target["id"]
        mime = target["mimeType"]
        if mime == "application/vnd.google-apps.document":
            # exportar como texto
            content = svc.files().export(fileId=file_id, mimeType="text/plain").execute()
            text = content.decode("utf-8", errors="ignore") if isinstance(content, bytes) else str(content)
        elif mime == "application/pdf":
            # Descargar PDF pero no parsearemos (sin dependencias nuevas)
            log.warning("Manual IMSS es PDF, sin parser. Se usará flujo guiado con fallback.")
            text = None
        else:
            log.warning(f"Tipo de manual no soportado: {mime}")
            text = None

        IMSS_MANUAL_CACHE["text"] = text
        IMSS_MANUAL_CACHE["ts"] = now
        return text
    except Exception as e:
        log.error(f"imss_manual_text error: {e}")
        return None

def imss_extract_benefits(text: str | None) -> str:
    """Intenta extraer beneficios; si no hay texto, usa bullets por defecto."""
    if not text:
        return (
            "• Montos desde $40,000 hasta $650,000\n"
            "• Descuento vía pensión (sin salir de casa)\n"
            "• Sin aval; trámite ágil y acompañado\n"
            "• Pagos fijos y transparentes\n"
            "• Orientación completa durante el proceso"
        )
    # Heurística simple: líneas con palabras clave
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    keys = ("beneficio", "ventaja", "incluye", "característica", "caracteristica", "monto", "pago", "pensión", "pension")
    found = [l for l in lines if any(k in l.lower() for k in keys)]
    if not found:
        return (
            "• Montos desde $40,000 hasta $650,000\n"
            "• Descuento vía pensión (sin salir de casa)\n"
            "• Sin aval; trámite ágil y acompañado\n"
            "• Pagos fijos y transparentes\n"
            "• Orientación completa durante el proceso"
        )
    # Limitar a 6 bullets
    return "• " + "\n• ".join(found[:6])

def imss_sales_funnel(text: str | None) -> list[str]:
    return [
        "1) Dime tu *pensión mensual aproximada*.",
        "2) Dime el *monto deseado* del préstamo.",
        "3) Verifico *pre‑elegibilidad* y te digo los siguientes pasos.",
        "4) Te indico *documentos* y *validaciones* para avanzar.",
    ]

def _parse_money_num(s: str) -> int | None:
    nums = re.findall(r"\d[\d,.\s]*", s.replace("$", ""))
    if not nums:
        return None
    raw = nums[0].replace(",", "").replace(" ", "")
    try:
        if "." in raw:
            raw = raw.split(".")[0]
        return int(raw)
    except Exception:
        return None

def imss_check_eligibility(text: str | None, user_inputs: dict) -> dict:
    pension = int(user_inputs.get("pension") or 0)
    monto = int(user_inputs.get("monto") or 0)
    reasons = []
    missing = []

    if pension <= 0:
        missing.append("pensión mensual")
    if monto <= 0:
        missing.append("monto deseado")

    # Reglas mínimas (genéricas, sin inventar tasas): monto rango oficial, pensión positiva
    eligible = True
    if monto and (monto < 40000 or monto > 650000):
        eligible = False
        reasons.append("El monto debe estar entre $40,000 y $650,000.")
    if pension and pension < 5000:
        # No rechazamos; solo marcamos evaluación (según guía usada por Christian)
        reasons.append("Con pensión menor a $5,000, la oferta puede requerir validación adicional.")
    if missing:
        eligible = False

    return {"eligible": eligible and not missing, "reasons": reasons, "missing": missing}

# ==============================
# Endpoints externos (ext/*)
# ==============================
@app.get("/ext/health")
def vx_ext_health():
    return jsonify({"status": "ok"})

@app.get("/ext/webhook")
def vx_ext_webhook_get():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        log.info("vx_ext_webhook_get: verificado OK")
        return challenge or "OK", 200
    log.warning("vx_ext_webhook_get: verificación fallida")
    return "Verification failed", 403

@app.post("/ext/webhook")
def vx_ext_webhook_post():
    try:
        payload = request.get_json(force=True, silent=True) or {}
        entry = payload.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])
        if not changes or "value" not in changes[0]:
            return jsonify({"status": "ignored"}), 200
        value = changes[0]["value"]
        msgs = value.get("messages", [])
        if not msgs:
            return jsonify({"status": "ignored"}), 200
        msg = msgs[0]
        from_number = msg.get("from")
        message_id = msg.get("id")
        # Enviar menú simple (arranque)
        vx_wa_send_text(from_number, MENU_TEXT)
        if message_id:
            vx_wa_mark_read(message_id)
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        log.error(f"vx_ext_webhook_post error: {e}")
        return jsonify({"status": "ok"}), 200

@app.route("/ext/test-send", methods=["GET", "POST"])
def vx_ext_test_send():
    try:
        if request.method == "GET":
            return jsonify({"status": "ready", "note": "Usa POST con {to, text} para enviar"}), 200
        data = request.get_json(force=True, silent=True) or {}
        to = data.get("to"); text = data.get("text")
        ok = vx_wa_send_text(to, text)
        return jsonify({"ok": ok}), 200
    except Exception as e:
        log.error(f"vx_ext_test_send error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 200

# /ext/test-secom – busca en hoja por últimos 10
@app.get("/ext/test-secom")
def vx_test_secom():
    phone = request.args.get("phone", "").strip()
    if not phone:
        return jsonify({"ok": False, "error": "Debes enviar ?phone=NUMERO"}), 400
    row = vx_sheet_find_by_phone(vx_last10(phone))
    if row:
        return jsonify({
            "ok": True,
            "match": {
                "nombre": row.get("Nombre", ""),
                "whatsapp": row.get("WhatsApp", ""),
                "rfc": row.get("RFC", ""),
                "beneficio": "Hasta 60% de descuento en seguro de auto 🚗"
            }
        }), 200
    return jsonify({"ok": False, "message": "No se encontró coincidencia"}), 200

# /ext/send-promo – CONSOLIDADO
@app.post("/ext/send-promo")
def vx_ext_send_promo():
    """Body JSON:
    {
      "to": "521..." | ["521...","521..."],
      "text": "mensaje opcional",
      "template": "nombre_template_opcional",
      "params": {k:v},
      "secom": true/false,
      "producto": "string opcional"
    }
    """
    data = request.get_json(force=True, silent=True) or {}
    to = data.get("to")
    text = data.get("text")
    template = data.get("template")
    params = data.get("params", {}) or {}
    use_secom = bool(data.get("secom"))
    producto = data.get("producto")

    # targets iniciales
    targets = []
    if isinstance(to, str) and to.strip():
        targets.append(to.strip())
    elif isinstance(to, list):
        targets.extend([str(x).strip() for x in to if str(x).strip()])

    # agregar SECOM
    if use_secom and SHEETS_ID_LEADS and SHEETS_TITLE_LEADS:
        try:
            client = _gspread_client()
            ws = client.open_by_key(SHEETS_ID_LEADS).worksheet(SHEETS_TITLE_LEADS)
            numbers = [str(r.get("WhatsApp", "")).strip() for r in ws.get_all_records() if r.get("WhatsApp")]
            targets.extend(numbers)
        except Exception as e:
            log.error(f"/ext/send-promo: error leyendo SECOM: {e}")

    # únicos y limpios
    uniq = []
    seen = set()
    for n in targets:
        if n and n not in seen:
            seen.add(n); uniq.append(n)

    def _worker(nums: list[str], text: str | None, template: str | None, params: dict):
        res = []
        for num in nums:
            try:
                ok = False
                if template:
                    ok = vx_wa_send_template(num, template, params)
                elif text:
                    ok = vx_wa_send_text(num, text)
                res.append({"to": num, "sent": ok})
            except Exception as e:
                log.error(f"send-promo worker error: {e}")
                res.append({"to": num, "sent": False, "error": str(e)})
        log.info(f"/ext/send-promo done: {res}")

    threading.Thread(target=_worker, args=(uniq, text, template, params), daemon=True).start()
    return jsonify({"accepted": True, "count": len(uniq)}), 202

# ==============================
# Webhook principal (GET/POST)
# ==============================
@app.get("/webhook")
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        log.info("Webhook verificado correctamente ✅")
        return challenge or "OK", 200
    log.warning("Fallo en la verificación del webhook ❌")
    return "Verification failed", 403

@app.post("/webhook")
def receive_message():
    data = request.get_json(silent=True, force=True) or {}
    log.info(f"📩 Mensaje recibido: {str(data)[:600]}")

    # Limpieza TTL
    n = now_ts()
    _cleanup_dict(PROCESSED_MESSAGE_IDS, MSG_TTL, n)
    _cleanup_dict(GREETED_USERS, GREET_TTL, n)
    _cleanup_dict(LAST_INTENT, GREET_TTL, n)
    _cleanup_dict(USER_CONTEXT, CTX_TTL, n)
    _cleanup_dict(CONTACT_NOTIFIES, NOTIFY_TTL, n)

    if "entry" not in data:
        return jsonify({"status": "ignored"}), 200

    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            val = change.get("value", {})
            if "statuses" in val:
                continue  # ignorar statuses

            msgs = val.get("messages", [])
            if not msgs:
                continue

            message = msgs[0]
            msg_id = message.get("id")
            msg_type = message.get("type")
            sender = message.get("from")

            # Dedupe mensaje
            if msg_id:
                seen = PROCESSED_MESSAGE_IDS.get(msg_id)
                if seen and (n - seen) < MSG_TTL:
                    log.info(f"🔁 Duplicado ignorado: {msg_id}")
                    continue
                PROCESSED_MESSAGE_IDS[msg_id] = n

            # Perfil
            profile_name = None
            try:
                profile_name = (val.get("contacts", [{}])[0].get("profile", {}) or {}).get("name")
            except Exception:
                pass
            last10 = vx_last10(sender)
            secom_row = vx_sheet_find_by_phone(last10)
            display_name = (secom_row or {}).get("Nombre") or profile_name or "No disponible"

            # ======== BLOQUE: medios antes del texto ========
            ctx = USER_CONTEXT.get(sender) or {}
            ctx_name = ctx.get("ctx")
            if msg_type in ("image", "document", "audio", "voice"):
                # Reenviar a asesor + si ctx=auto subir a Drive
                if msg_type == "image":
                    media_id = (message.get("image") or {}).get("id")
                    caption = (message.get("image") or {}).get("caption") or ""
                    if ADVISOR_WHATSAPP and media_id:
                        send_media_image(ADVISOR_WHATSAPP, media_id,
                                         f"📎 Imagen de {display_name} ({sender}). {('Nota: '+caption) if caption else ''}")
                    if ctx_name == "auto" and media_id and DRIVE_FOLDER_CLIENTS_ID:
                        try:
                            url = _get_media_url(media_id)
                            blob = _download_media_bytes(url) if url else None
                            if blob:
                                folder_name = build_client_folder_name(display_name, last10)
                                folder_id = drive_find_or_create_folder(DRIVE_FOLDER_CLIENTS_ID, folder_name)
                                ts = datetime.now(TZ).strftime("%Y%m%d_%H%M")
                                fname = f"IMG_{ts}.jpg"
                                drive_upload_bytes(folder_id, fname, blob, "image/jpeg")
                                vx_wa_send_text(sender, "✅ Recibido. Estoy guardando tus documentos y avanzando con la cotización.")
                        except Exception as e:
                            log.error(f"Error subiendo imagen Drive: {e}")
                    continue

                if msg_type == "document":
                    media_id = (message.get("document") or {}).get("id")
                    filename = (message.get("document") or {}).get("filename") or "documento.pdf"
                    if ADVISOR_WHATSAPP and media_id:
                        send_media_document(ADVISOR_WHATSAPP, media_id,
                                            f"📄 Doc de {display_name} ({sender}) — {filename}")
                    if ctx_name == "auto" and media_id and DRIVE_FOLDER_CLIENTS_ID:
                        try:
                            url = _get_media_url(media_id)
                            blob = _download_media_bytes(url) if url else None
                            if blob:
                                folder_name = build_client_folder_name(display_name, last10)
                                folder_id = drive_find_or_create_folder(DRIVE_FOLDER_CLIENTS_ID, folder_name)
                                ts = datetime.now(TZ).strftime("%Y%m%d_%H%M")
                                drive_upload_bytes(folder_id, f"DOC_{ts}_{filename}", blob, None)
                                vx_wa_send_text(sender, "✅ Recibido. Estoy guardando tus documentos y avanzando con la cotización.")
                        except Exception as e:
                            log.error(f"Error subiendo doc Drive: {e}")
                    continue

                if msg_type in ("audio", "voice"):
                    # No transcribimos para evitar dependencias; reenviar aviso
                    vx_wa_send_text(sender, "🎙️ Recibí tu nota de voz. Si puedes, escribe el mensaje para avanzar más rápido.")
                    continue

            # ======== Texto ========
            if msg_type != "text":
                vx_wa_send_text(sender, "No te entendí. Escribe 'menu' para ver opciones o elige un número del 1 al 7.")
                continue

            text = (message.get("text") or {}).get("body", "") or ""
            text_norm = text.strip().lower()

            # Contextos activos (IMSS/auto)
            if ctx_name == "auto":
                # Si envía placa en texto
                if any(k in text_norm for k in ("placa", "placas")) or re.search(r"[A-Z]{3}\d{3,4}", text_norm.upper()):
                    if ADVISOR_WHATSAPP:
                        vx_wa_send_text(ADVISOR_WHATSAPP, f"🔎 Placa de {display_name} ({sender}): {text.strip()}")
                    vx_wa_send_text(sender, "✅ Gracias. Con tu INE y tarjeta de circulación (o con la placa) preparo la cotización.")
                    USER_CONTEXT[sender] = {"ctx": "auto", "ts": n}
                    continue

            if ctx_name == "imss":
                step = ctx.get("step")
                data_ctx = ctx.get("data", {})
                if step == "pension":
                    pension = _parse_money_num(text_norm) or 0
                    data_ctx["pension"] = pension
                    USER_CONTEXT[sender] = {"ctx": "imss", "step": "monto", "data": data_ctx, "ts": n}
                    vx_wa_send_text(sender, "👍 Gracias. ¿Qué *monto* deseas solicitar? (ej. 120000)")
                    continue
                if step == "monto":
                    monto = _parse_money_num(text_norm) or 0
                    data_ctx["monto"] = monto
                    # Evaluar
                    manual = imss_manual_text()
                    evalr = imss_check_eligibility(manual, data_ctx)
                    if evalr["eligible"]:
                        vx_wa_send_text(sender, "🟢 Con la información proporcionada, estás *pre‑elegible*. Te indico los siguientes pasos y documentos.")
                        notify_imss_to_advisor(display_name, sender, data_ctx, elegible=True, reasons=evalr["reasons"])
                        next_steps_imss(sender)
                    else:
                        msg = "🟡 Tu caso está *en evaluación*. "
                        if evalr["missing"]:
                            msg += "Falta: " + ", ".join(evalr["missing"]) + ". "
                        if evalr["reasons"]:
                            msg += "Notas: " + " ".join(evalr["reasons"])
                        vx_wa_send_text(sender, msg.strip())
                        notify_imss_to_advisor(display_name, sender, data_ctx, elegible=False, reasons=evalr["reasons"], missing=evalr["missing"])
                    # Reiniciar contexto a paso final
                    USER_CONTEXT[sender] = {"ctx": "imss", "step": "end", "data": data_ctx, "ts": n}
                    continue

            # Opción directa o inferida
            is_menu = text_norm in ("hola", "menú", "menu")
            is_option = text_norm in OPTION_RESPONSES
            option = text_norm if is_option else infer_option_from_text(text_norm)

            if is_menu:
                greet_or_menu(sender, display_name)
                continue

            if option:
                vx_wa_send_text(sender, OPTION_RESPONSES[option])
                LAST_INTENT[sender] = {"opt": option, "title": option, "ts": n}

                if option == "2":
                    USER_CONTEXT[sender] = {"ctx": "auto", "ts": n}
                    continue

                if option == "7":
                    # Notificar asesor con dedupe
                    last_sent = CONTACT_NOTIFIES.get(sender)
                    if not last_sent or (n - last_sent) > NOTIFY_TTL:
                        motive = "Contacto con Christian"
                        notify_text = (
                            "🔔 *Vicky Bot – Solicitud de contacto*\n"
                            f"- Nombre: {display_name}\n"
                            f"- WhatsApp: {sender}\n"
                            f"- Motivo: {motive}\n"
                            f"- Mensaje original: \"{text.strip()}\""
                        )
                        if ADVISOR_WHATSAPP and ADVISOR_WHATSAPP != sender:
                            vx_wa_send_text(ADVISOR_WHATSAPP, notify_text)
                            CONTACT_NOTIFIES[sender] = n
                            log.info(f"📨 Notificación privada enviada al asesor {ADVISOR_WHATSAPP}")
                        vx_wa_send_text(sender, "Christian ha sido notificado, pronto se pondrá en contacto contigo. 🙌")
                    else:
                        vx_wa_send_text(sender, "Ya notifiqué a Christian recientemente. Te contactará pronto. 🙌")
                    continue

                if option == "5":
                    # Mostrar beneficios + embudo
                    manual = imss_manual_text()
                    benefits = imss_extract_benefits(manual)
                    funnel = "\n".join(imss_sales_funnel(manual))
                    vx_wa_send_text(sender, f"🟩 *Beneficios*: \n{benefits}\n\n{funnel}")
                    USER_CONTEXT[sender] = {"ctx": "imss", "step": "pension", "data": {}, "ts": n}
                    continue

                continue

            # Si no eligió opción y no está en contexto: saludo inicial
            greet_or_menu(sender, display_name)

    return jsonify({"status": "ok"}), 200

def greet_or_menu(sender: str, display_name: str | None):
    first_greet_ts = GREETED_USERS.get(sender)
    if not first_greet_ts or (now_ts() - first_greet_ts) >= GREET_TTL:
        vx_wa_send_text(sender, f"👋 Hola {display_name if display_name!='No disponible' else ''}.\n\n{MENU_TEXT}")
        GREETED_USERS[sender] = now_ts()
    else:
        vx_wa_send_text(sender, MENU_TEXT)

def build_client_folder_name(display_name: str, last10: str) -> str:
    # "Apellido_Nombre_####" si hay nombre, si no "Cliente_{last10}"
    tail = last10[-4:] if last10 else "0000"
    if not display_name or display_name == "No disponible":
        return f"Cliente_{last10 or 'desconocido'}"
    parts = display_name.split()
    if len(parts) >= 2:
        last = parts[-1]
        first = parts[0]
        return f"{last}_{first}_{tail}"
    return f"{display_name}_{tail}"

def notify_imss_to_advisor(name: str, wa: str, data_ctx: dict, elegible: bool, reasons=None, missing=None):
    status = "🟢 *Elegible*" if elegible else "🟡 *En evaluación*"
    lines = [
        "📣 *Vicky Bot – Préstamo IMSS*",
        f"Estado: {status}",
        f"Nombre: {name}",
        f"WhatsApp: {wa}",
        f"Pensión mensual: ${int(data_ctx.get('pension') or 0):,}".replace(",", ","),
        f"Monto deseado: ${int(data_ctx.get('monto') or 0):,}".replace(",", ","),
    ]
    if reasons:
        lines.append("Notas: " + " ".join(reasons))
    if (missing or []) and not elegible:
        lines.append("Faltantes: " + ", ".join(missing))
    txt = "\n".join(lines)
    if ADVISOR_WHATSAPP and ADVISOR_WHATSAPP != wa:
        vx_wa_send_text(ADVISOR_WHATSAPP, txt)

def next_steps_imss(to: str):
    steps = [
        "📄 Documentos típicos: identificación oficial, comprobante de pensión y cuenta bancaria.",
        "🗓️ Verificación de datos: revisamos montos y capacidad de descuento.",
        "✍️ Firma y autorización: avanzamos el trámite con acompañamiento.",
    ]
    vx_wa_send_text(to, "➡️ *Siguientes pasos*\n" + "\n".join(f"• {s}" for s in steps))

# ==============================
# Top-level health (no romper)
# ==============================
@app.get("/health")
def health():
    return jsonify({"status": "ok"}), 200

# ==============================
# Main
# ==============================
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
