import os
import logging
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# Cargar variables de entorno
load_dotenv()

# Configuración de logging
logging.basicConfig(level=logging.INFO)

# Inicializar Flask
app = Flask(__name__)

# Variables de entorno
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
WHATSAPP_TOKEN = os.getenv("META_TOKEN")  # ✅ Ajustado para Render
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")

# 🧠 CAMBIO MÍNIMO: sets en memoria para controlar duplicados y saludo único
PROCESSED_MESSAGE_IDS = set()
GREETED_USERS = set()

# Endpoint de verificación
@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        logging.info("Webhook verificado correctamente ✅")
        return challenge, 200
    else:
        logging.warning("Fallo en la verificación del webhook ❌")
        return "Verification failed", 403

# Endpoint para recibir mensajes
@app.route("/webhook", methods=["POST"])
def receive_message():
    data = request.get_json()
    logging.info(f"📩 Mensaje recibido: {data}")

    if "entry" in data:
        for entry in data["entry"]:
            if "changes" in entry:
                for change in entry["changes"]:
                    if "value" in change and "messages" in change["value"]:
                        for message in change["value"]["messages"]:
                            # 🧠 CAMBIO MÍNIMO: evitar reprocesar el mismo mensaje
                            msg_id = message.get("id")
                            if msg_id in PROCESSED_MESSAGE_IDS:
                                logging.info(f"🔁 Duplicado ignorado: {msg_id}")
                                continue
                            PROCESSED_MESSAGE_IDS.add(msg_id)
                            if len(PROCESSED_MESSAGE_IDS) > 5000:
                                PROCESSED_MESSAGE_IDS.clear()

                            if message.get("type") == "text":
                                sender = message["from"]
                                text = message["text"]["body"].strip().lower()
                                logging.info(f"Mensaje de {sender}: {text}")

                                # 🧠 CAMBIO MÍNIMO: saludar solo la primera vez
                                if sender not in GREETED_USERS:
                                    send_message(
                                        sender,
                                        "👋 Hola, soy Vicky, asistente de Christian López. Estoy aquí para ayudarte.\n\n👉 Elige una opción del menú:"
                                    )
                                    GREETED_USERS.add(sender)
                                else:
                                    # Si el usuario pide menú nuevamente, no repetir saludo
                                    if text in ["menu", "menú", "hola"]:
                                        send_message(
                                            sender,
                                            "👉 Elige una opción del menú:"
                                        )
                                    else:
                                        logging.info("📌 Mensaje recibido (sin saludo repetido).")
    return jsonify({"status": "ok"}), 200

# Función para enviar mensajes
def send_message(to, text):
    url = f"https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}/messages"
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
    logging.info(f"Respuesta de WhatsApp API: {response.status_code} - {response.text}")

# Endpoint de salud
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
