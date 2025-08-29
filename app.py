import os
import re
import json
import logging
from flask import Flask, request, jsonify
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ----------------------------------
# App & logging
# ----------------------------------

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# ----------------------------------
# Variables de entorno
# ----------------------------------

GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON", "")
SHEET_NAME = os.getenv("SHEET_NAME", "Prospectos SECOM Auto")
WORKSHEET_NAME = os.getenv("WORKSHEET_NAME", "")  # vacío = primera pestaña
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "vicky-verify-token")
BRAND_NAME = os.getenv("BRAND_NAME", "Christian López")

# ----------------------------------
# Utilidades
# ----------------------------------

def get_sheet():
    try:
        scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
        credentials = ServiceAccountCredentials.from_json_keyfile_dict(json.loads(GOOGLE_CREDENTIALS_JSON), scope)
        client = gspread.authorize(credentials)
        sheet = client.open(SHEET_NAME)
        return sheet.worksheet(WORKSHEET_NAME) if WORKSHEET_NAME else sheet.get_worksheet(0)
    except Exception as e:
        logging.error(f"Error al conectar con Google Sheets: {e}")
        return None

def find_client_by_number(sheet, number):
    try:
        records = sheet.get_all_records()
        for row in records:
            raw_number = re.sub(r'\D', '', str(row.get("WhatsApp", "")))
            if raw_number.endswith(number[-10:]):
                return row
    except Exception as e:
        logging.error(f"Error buscando cliente: {e}")
    return None

# ----------------------------------
# Webhook de verificación (GET)
# ----------------------------------

@app.route('/webhook', methods=['GET'])
def verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        logging.info("✅ Webhook verificado correctamente.")
        return challenge, 200
    else:
        logging.warning("❌ Verificación fallida.")
        return "Verification failed", 403

# ----------------------------------
# Webhook de recepción de mensajes (POST)
# ----------------------------------

@app.route('/webhook', methods=['POST'])
def receive_message():
    data = request.get_json()
    logging.info(f"📩 Mensaje recibido: {json.dumps(data)}")
    # Aquí agregarás la lógica para procesar mensajes
    return "EVENT_RECEIVED", 200

# ----------------------------------
# Ruta de prueba
# ----------------------------------

@app.route('/health', methods=['GET'])
def health_check():
    return "Vicky está activa", 200

# ----------------------------------
# Ejecutar localmente
# ----------------------------------

if __name__ == '__main__':
    app.run(debug=True)



if __name__ == "__main__":
    # Para desarrollo local (Render usará gunicorn con -b 0.0.0.0:$PORT)
    port = int(os.getenv("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
