# ========================================================
# Vicky Bot – Fase 1 (Render / Flask / WhatsApp API)
# Archivo: app.py
# Descripción: Backend principal. Listo para producción en Render.
# ========================================================

import os
import json
import logging
import re
import threading
import pytz
from datetime import datetime
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# Google Sheets & Drive
import gspread
from google.oauth2.service_account import Credentials

# ------------------- Configuración -------------------

load_dotenv()  # Cargar variables de entorno

# Variables de entorno principales
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
WABA_PHONE_ID = os.getenv("WABA_PHONE_ID")
ADVISOR_WHATSAPP = os.getenv("ADVISOR_WHATSAPP", "5216682478005")
SHEETS_ID_LEADS = os.getenv("SHEETS_ID_LEADS")
SHEETS_TITLE_LEADS = os.getenv("SHEETS_TITLE_LEADS", "Prospectos SECOM Auto")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")

VERIFY_TOKEN = "vicky_token"  # Token fijo para verificación webhook

# Configuración de logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# Inicializar Flask
app = Flask(__name__)

# ------------------- Utilidades -------------------

def send_whatsapp_message(to, message):
    """
    Envía un mensaje de texto por WhatsApp Cloud API.
    Registra cada envío en logs.
    """
    url = f"https://graph.facebook.com/v17.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": message}
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=10)
        logging.info(f"[WA SEND] to={to} text='{message[:80]}' status={resp.status_code}")
        return resp.status_code == 200
    except Exception as e:
        logging.error(f"[WA SEND ERROR] to={to} error={e}")
        return False

def last10_digits(phone):
    """
    Extrae los últimos 10 dígitos de un número (sin prefijo país).
    """
    phone = re.sub(r"[^\d]", "", str(phone or ""))
    phone = re.sub(r"^(52|521)", "", phone)
    return phone[-10:] if len(phone) >= 10 else phone

def match_sheet_phone(phone):
    """
    Busca coincidencia de teléfono en Google Sheets.
    Retorna dict con la fila si encuentra, None si no.
    Maneja errores robustamente.
    """
    try:
        creds_info = json.loads(GOOGLE_CREDENTIALS_JSON)
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets.readonly",
            "https://www.googleapis.com/auth/drive.readonly"
        ]
        creds = Credentials.from_service_account_info(creds_info, scopes=scopes)
        client = gspread.authorize(creds)
        sheet = client.open_by_key(SHEETS_ID_LEADS)
        ws = sheet.worksheet(SHEETS_TITLE_LEADS)
        rows = ws.get_all_records()
        for row in rows:
            wa = str(row.get("WhatsApp", ""))
            if last10_digits(wa) == phone:
                return row
        return None
    except Exception as e:
        logging.error(f"[GSHEET ERROR] {e}")
        return None

# ------------------- Endpoints -------------------

@app.route("/webhook", methods=["GET"])
def webhook_verify():
    """
    Verificación de Webhook para WhatsApp Cloud API (GET).
    Token fijo: vicky_token.
    """
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        logging.info("[WEBHOOK] Verificado correctamente")
        return challenge or "OK", 200
    logging.warning("[WEBHOOK] Verificación fallida")
    return "Verification failed", 403

@app.route("/webhook", methods=["POST"])
def webhook_receive():
    """
    Recepción de mensajes entrantes desde WhatsApp Cloud API (POST).
    Flujo principal de interacción.
    """
    data = request.get_json(force=True, silent=True)
    logging.info(f"[INCOMING] {json.dumps(data)[:400]}")
    if not data or "entry" not in data:
        return jsonify({"status": "ignored"}), 200

    try:
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                messages = value.get("messages", [])
                if not messages:
                    continue
                msg = messages[0]
                msg_type = msg.get("type")
                from_number = msg.get("from")
                profile_name = None
                try:
                    contacts = value.get("contacts", [{}])
                    profile_name = (contacts[0].get("profile") or {}).get("name")
                except Exception:
                    profile_name = None

                if msg_type != "text":
                    continue  # Solo procesar texto

                text = msg.get("text", {}).get("body", "").strip()
                text_norm = text.lower()
                logging.info(f"[TEXT] from={from_number} text='{text_norm}'")

                # Opción 7: Notificación al asesor
                if text_norm == "7":
                    notify_msg = f"📢 Cliente {from_number} seleccionó 'Contactar con Christian'."
                    send_whatsapp_message(ADVISOR_WHATSAPP, notify_msg)
                    reply_msg = "Christian ha sido notificado, pronto se pondrá en contacto contigo. 🙌"
                    send_whatsapp_message(from_number, reply_msg)
                    continue

                # Respuesta default
                menu = (
                    "👋 Hola, soy Vicky Bot. Elige una opción del menú principal.\n"
                    "1) Pensiones IMSS (Ley 73 / Modalidad 40 / Modalidad 10)\n"
                    "2) Seguro de auto\n"
                    "3) Seguros de vida y salud\n"
                    "4) Tarjetas médicas VRIM\n"
                    "5) Préstamos a pensionados IMSS\n"
                    "6) Financiamiento empresarial\n"
                    "7) Contactar con Christian\n"
                    "Escribe el número de la opción."
                )
                send_whatsapp_message(from_number, menu)
    except Exception as e:
        logging.error(f"[WEBHOOK ERROR] {e}")

    return jsonify({"status": "ok"}), 200

@app.route("/ext/health", methods=["GET"])
def ext_health():
    """
    Endpoint de salud para Render.
    """
    return jsonify({"status": "ok"}), 200

@app.route("/ext/test-send", methods=["GET"])
def ext_test_send():
    """
    Envía mensaje de prueba al número del asesor.
    Útil para verificar conectividad y envío desde Render.
    """
    test_msg = "🧪 Prueba de envío desde Vicky Bot (Render OK)"
    ok = send_whatsapp_message(ADVISOR_WHATSAPP, test_msg)
    status = "ok" if ok else "fail"
    return jsonify({"status": status}), 200

# ------------------- Main -------------------

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
