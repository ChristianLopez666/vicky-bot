# Vicky Bot – Fase 1 (app.py)
# --------------------------------------------
# Flask app for WhatsApp Cloud API integration
# + Google Sheets (SECOM Auto)
# Compatible with Render & gunicorn "app:app"
# --------------------------------------------

import os
import json
import logging
import requests
import re
import threading
from datetime import datetime
from flask import Flask, request, jsonify
from dotenv import load_dotenv
import pytz

# --- Google Sheets Integration ---
import gspread
from google.oauth2.service_account import Credentials

# --- Configuración inicial ---
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

app = Flask(__name__)

# --- Variables de entorno ---
WHATSAPP_TOKEN      = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID     = os.getenv("PHONE_NUMBER_ID")
WABA_PHONE_ID       = os.getenv("WABA_PHONE_ID")
ADVISOR_WHATSAPP    = os.getenv("ADVISOR_WHATSAPP", "")
SHEETS_ID_LEADS     = os.getenv("SHEETS_ID_LEADS")
SHEETS_TITLE_LEADS  = os.getenv("SHEETS_TITLE_LEADS", "Prospectos SECOM Auto")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")
VERIFY_TOKEN        = "vicky_token"

# --- Utilidad: normalizar número a últimos 10 dígitos ---
def last10(phone: str) -> str:
    if not phone:
        return ""
    phone = re.sub(r"[^\d]", "", str(phone))
    phone = re.sub(r"^(52|521)", "", phone)
    return phone[-10:] if len(phone) >= 10 else phone

# --- Integración con Google Sheets (SECOM Auto) ---
def find_lead_by_phone(last_10: str):
    """
    Busca coincidencia por los últimos 10 dígitos en la hoja de leads.
    Retorna el dict de la fila si encuentra, None si no, o error si falla la conexión.
    """
    if not (GOOGLE_CREDENTIALS_JSON and SHEETS_ID_LEADS and SHEETS_TITLE_LEADS and last_10):
        return None, "Faltan variables de entorno para Google Sheets"
    try:
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets.readonly",
            "https://www.googleapis.com/auth/drive.readonly",
        ]
        creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        client = gspread.authorize(creds)
        ws = client.open_by_key(SHEETS_ID_LEADS).worksheet(SHEETS_TITLE_LEADS)
        rows = ws.get_all_records()
        for row in rows:
            wa = str(row.get("WhatsApp", ""))
            if last10(wa) == last_10:
                return row, None
        return None, None
    except Exception as e:
        logging.error(f"[Sheets] Error de conexión: {e}")
        return None, str(e)

# --- Envío de mensajes por WhatsApp Cloud API ---
def send_whatsapp_message(to: str, message: str) -> bool:
    """
    Envía mensaje de texto por WhatsApp Cloud API.
    """
    if not (WHATSAPP_TOKEN and PHONE_NUMBER_ID and to and message):
        logging.warning(f"[WA SEND] Faltan datos para enviar: to={to}, msg={message}")
        return False
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
        resp = requests.post(url, headers=headers, json=payload, timeout=9)
        logging.info(f"[WA SEND] To: {to}, Status: {resp.status_code}, Response: {resp.text[:160]}")
        return resp.status_code == 200
    except Exception as e:
        logging.error(f"[WA SEND] Error enviando mensaje: {e}")
        return False

# --- Endpoint: Verificación de Webhook (GET) ---
@app.route("/webhook", methods=["GET"])
def webhook_verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        logging.info("[Webhook] Verificado correctamente.")
        return challenge or "OK", 200
    logging.warning("[Webhook] Fallo verificación.")
    return "Verification failed", 403

# --- Endpoint: Recepción de mensajes WhatsApp Cloud API (POST) ---
@app.route("/webhook", methods=["POST"])
def webhook_receive():
    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"status": "ignored", "error": "No JSON"}), 200

    logging.info(f"[Webhook] Payload recibido: {json.dumps(data)[:240]}")

    if not data or "entry" not in data:
        return jsonify({"status": "ignored"}), 200

    # Procesar solo el primer mensaje válido
    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            messages = value.get("messages", [])
            if not messages:
                continue
            msg = messages[0]
            msg_type = msg.get("type")
            from_number = msg.get("from")
            text_body = ""
            if msg_type == "text":
                text_body = msg.get("text", {}).get("body", "").strip()
            else:
                continue  # Solo procesar texto

            # --- Opción "7": notificar al asesor y responder al cliente ---
            if text_body == "7":
                notify_text = f"📢 Cliente {from_number} seleccionó 'Contactar con Christian'."
                # Notifica al asesor en background
                def notify_worker():
                    send_whatsapp_message(ADVISOR_WHATSAPP, notify_text)
                threading.Thread(target=notify_worker, daemon=True).start()
                # Responde al cliente
                send_whatsapp_message(
                    from_number,
                    "Christian ha sido notificado, pronto se pondrá en contacto contigo. 🙌"
                )
                return jsonify({"status": "ok"}), 200

            # --- Respuesta default para cualquier otro texto ---
            send_whatsapp_message(
                from_number,
                "👋 Hola, soy Vicky Bot. Elige una opción del menú principal."
            )
            return jsonify({"status": "ok"}), 200

    return jsonify({"status": "ignored"}), 200

# --- Endpoint: Health para Render ---
@app.route("/ext/health", methods=["GET"])
def ext_health():
    return jsonify({"status": "ok"}), 200

# --- Endpoint: Test de envío al asesor ---
@app.route("/ext/test-send", methods=["GET"])
def ext_test_send():
    """
    Envía mensaje de prueba al asesor configurado en ADVISOR_WHATSAPP.
    """
    if not ADVISOR_WHATSAPP:
        return jsonify({"sent": False, "error": "ADVISOR_WHATSAPP no configurado"}), 400

    test_text = (
        f"🔔 Prueba de envío automática.\n"
        f"Hora: {datetime.now(pytz.timezone('America/Mexico_City')).strftime('%Y-%m-%d %H:%M:%S')}"
    )
    ok = send_whatsapp_message(ADVISOR_WHATSAPP, test_text)
    return jsonify({"sent": ok, "to": ADVISOR_WHATSAPP}), 200

# --- Bloque ejecución Render/gunicorn ---
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
