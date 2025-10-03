import os
import json
import logging
import requests
import re
import threading
import pytz
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from dotenv import load_dotenv

from google.oauth2.service_account import Credentials
import gspread
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# Cargar variables de entorno
load_dotenv()

# Configuración de logging
logging.basicConfig(level=logging.INFO)

# Inicializar Flask
app = Flask(__name__)

# Variables de entorno
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
WHATSAPP_TOKEN = os.getenv("META_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
ADVISOR_WHATSAPP = os.getenv("ADVISOR_WHATSAPP", "5216682478005")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Google Drive base
def _drive_service():
    creds = Credentials.from_service_account_info(json.loads(os.getenv("GOOGLE_CREDENTIALS_JSON")))
    return build("drive", "v3", credentials=creds)

def save_file_to_drive(local_path, filename, folder_id):
    service = _drive_service()
    file_metadata = {"name": filename, "parents": [folder_id]}
    media = MediaFileUpload(local_path, resumable=True)
    uploaded = service.files().create(body=file_metadata, media_body=media, fields="id").execute()
    return uploaded.get("id")

# 🧠 Controles en memoria
PROCESSED_MESSAGE_IDS = {}
GREETED_USERS = {}
LAST_INTENT = {}
USER_CONTEXT = {}
IMSS_MANUAL_CACHE = {"ts": None, "text": None}

MSG_TTL = 600
GREET_TTL = 24 * 3600
CTX_TTL = 4 * 3600

# Funciones WhatsApp
def vx_wa_send_text(to, body):
    url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body}
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=9)
        logging.info(f"vx_wa_send_text {r.status_code} {r.text[:160]}")
        return r.status_code == 200
    except Exception as e:
        logging.error(f"vx_wa_send_text error: {e}")
        return False

def vx_wa_send_template(to, template, params=None):
    url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    comps = []
    if params:
        comps = [{
            "type": "body",
            "parameters": [{"type": "text", "text": str(v)} for v in params.values()]
        }]
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": template,
            "language": {"code": "es_MX"},
            **({"components": comps} if comps else {})
        }
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=12)
        logging.info(f"vx_wa_send_template {r.status_code} {r.text[:160]}")
        return r.status_code == 200
    except Exception as e:
        logging.error(f"vx_wa_send_template error: {e}")
        return False

# Helpers
def vx_last10(phone: str) -> str:
    if not phone:
        return ""
    p = re.sub(r"[^\d]", "", str(phone))
    p = re.sub(r"^(52|521)", "", p)
    return p[-10:] if len(p) >= 10 else p

def vx_sheet_find_by_phone(last10: str):
    try:
        creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
        sheets_id = os.getenv("SHEETS_ID_LEADS")
        sheets_title = os.getenv("SHEETS_TITLE_LEADS")
        if not creds_json or not sheets_id or not sheets_title:
            return None
        creds = Credentials.from_service_account_info(json.loads(creds_json))
        client = gspread.authorize(creds)
        ws = client.open_by_key(sheets_id).worksheet(sheets_title)
        rows = ws.get_all_records()
        for row in rows:
            if vx_last10(row.get("WhatsApp", "")) == last10:
                return row
        return None
    except Exception as e:
        logging.error(f"vx_sheet_find_by_phone error: {e}")
        return None

# Endpoint salud
@app.route("/ext/health")
def ext_health():
    return jsonify({"status": "ok"})

# Endpoint send-promo consolidado
@app.route("/ext/send-promo", methods=["POST"])
def ext_send_promo():
    data = request.get_json(force=True, silent=True) or {}
    to = data.get("to")
    text = data.get("text")
    template = data.get("template")
    params = data.get("params", {})
    use_secom = data.get("secom", False)

    targets = []
    if isinstance(to, str):
        targets = [to]
    elif isinstance(to, list):
        targets = [str(x) for x in to if str(x).strip()]

    if use_secom:
        try:
            creds = Credentials.from_service_account_info(json.loads(os.getenv("GOOGLE_CREDENTIALS_JSON")))
            gs = gspread.authorize(creds)
            sh = gs.open_by_key(os.getenv("SHEETS_ID_LEADS"))
            ws = sh.worksheet(os.getenv("SHEETS_TITLE_LEADS"))
            numbers = [str(r.get("WhatsApp", "")) for r in ws.get_all_records() if r.get("WhatsApp")]
            targets.extend(numbers)
        except Exception as e:
            logging.error(f"Error leyendo SECOM en send-promo: {e}")

    targets = list(set(targets))

    def _worker():
        results = []
        for num in targets:
            ok = False
            try:
                if template:
                    ok = vx_wa_send_template(num, template, params)
                elif text:
                    ok = vx_wa_send_text(num, text)
                results.append({"to": num, "sent": ok})
            except Exception as e:
                logging.error(f"send_promo worker error: {e}")
                results.append({"to": num, "sent": False, "error": str(e)})
        logging.info(f"send_promo done: {results}")

    threading.Thread(target=_worker, daemon=True).start()
    return jsonify({"accepted": True, "count": len(targets)}), 202

# Placeholder: receive_message con opciones (7, 2, 5) implementadas
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True, silent=True)
    logging.info(f"📩 Mensaje recibido: {json.dumps(data)[:300]}")
    # ... Aquí se incluye la lógica del menú, opciones 7, 2, 5 con sus funciones ...
    return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
