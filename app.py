import os
import logging
import requests
from flask import Flask, request, jsonify
from config_env import (
    VERIFY_TOKEN,
    WHATSAPP_TOKEN,
    PHONE_NUMBER_ID,
    OPENAI_API_KEY,
    GOOGLE_SHEETS_KEY
)

app = Flask(__name__)

# Configurar logging
logging.basicConfig(level=logging.INFO)

# Endpoint de verificación del webhook
@app.route("/webhook", methods=["GET"])
def verify():
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    mode = request.args.get("hub.mode")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        logging.info("✅ Webhook verificado correctamente")
        return challenge, 200
    else:
        logging.warning("❌ Error en la verificación del webhook")
        return "Verification failed", 403

# Endpoint para recibir mensajes
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    logging.info(f"📩 Mensaje recibido: {data}")

    try:
        if "entry" in data:
            for entry in data["entry"]:
                if "changes" in entry:
                    for change in entry["changes"]:
                        if "value" in change and "messages" in change["value"]:
                            for message in change["value"]["messages"]:
                                sender = message["from"]
                                if "text" in message:
                                    incoming_text = message["text"]["body"]
                                    logging.info(f"👤 {sender}: {incoming_text}")
                                    send_whatsapp_message(sender, "Hola 👋 soy Vicky, asistente de Christian López.")
        return "EVENT_RECEIVED", 200
    except Exception as e:
        logging.error(f"⚠️ Error procesando mensaje: {e}")
        return "ERROR", 500

# Función para enviar mensajes a WhatsApp
def send_whatsapp_message(to, text):
    url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text}
    }
    response = requests.post(url, headers=headers, json=payload)
    logging.info(f"📤 Respuesta de WhatsApp API: {response.status_code} {response.text}")

# Endpoint de salud
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

