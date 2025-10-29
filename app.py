import os
import json
import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv
import openai
import gspread
from oauth2client.service_account import ServiceAccountCredentials

load_dotenv()

# ---------------------------------------------------------------
# CONFIGURACIÓN GLOBAL
# ---------------------------------------------------------------
META_TOKEN = os.getenv("META_TOKEN")
WABA_PHONE_ID = os.getenv("WABA_PHONE_ID")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
ADVISOR_NUMBER = os.getenv("ADVISOR_NUMBER")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME")
NOTIFICAR_ASESOR = os.getenv("NOTIFICAR_ASESOR")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai.api_key = OPENAI_API_KEY

app = Flask(__name__)

# ---------------------------------------------------------------
# WEBHOOK DE VERIFICACIÓN
# ---------------------------------------------------------------
@app.route("/webhook", methods=["GET"])
def verificar_webhook():
    if request.args.get("hub.mode") == "subscribe" and request.args.get("hub.verify_token") == VERIFY_TOKEN:
        return request.args.get("hub.challenge"), 200
    return "Error de verificación", 403

# ---------------------------------------------------------------
# WEBHOOK PRINCIPAL: POST
# ---------------------------------------------------------------
@app.route("/webhook", methods=["POST"])
def recibir_mensaje():
    data = request.get_json()
    if data and data.get("entry"):
        for entry in data["entry"]:
            for change in entry.get("changes", []):
                value = change.get("value", {})
                messages = value.get("messages", [])
                for message in messages:
                    phone_number = message["from"]
                    message_text = message["text"]["body"] if "text" in message else ""
                    threading.Thread(target=procesar_mensaje, args=(phone_number, message_text)).start()
    return "EVENT_RECEIVED", 200

# ---------------------------------------------------------------
# PROCESAR MENSAJE
# ---------------------------------------------------------------
def procesar_mensaje(phone: str, message: str):
    try:
        if message:
            respuesta = "¡Hola! Soy Vicky 🤖, tu asistente. ¿En qué puedo ayudarte hoy?"
            send_message(phone, respuesta)
    except Exception as e:
        logging.error(f"Error al procesar el mensaje: {e}")

# ---------------------------------------------------------------
# ENVIAR MENSAJE A WHATSAPP
# ---------------------------------------------------------------
def send_message(to: str, text: str):
    url = f"https://graph.facebook.com/v17.0/{WABA_PHONE_ID}/messages"
    headers = {
        "Authorization": f"Bearer {META_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text}
    }
    response = requests.post(url, headers=headers, json=payload)
    if response.status_code != 200:
        logging.error(f"Error al enviar mensaje: {response.text}")
    return response.json()

# ---------------------------------------------------------------
# ENDPOINT DE SALUD
# ---------------------------------------------------------------
@app.route("/ext/health", methods=["GET"])
def health_check():
    return jsonify({"status": "ok"})

# ---------------------------------------------------------------
# EJECUCIÓN LOCAL
# ---------------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))


