import os
import logging
import requests
from flask import Flask, request, jsonify

# Configuración de logging
logging.basicConfig(level=logging.INFO)

app = Flask(__name__)

# Variables de entorno
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")

# Función para enviar mensajes a WhatsApp
def send_whatsapp_message(to, message):
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
    response = requests.post(url, headers=headers, json=payload)
    logging.info(f"Enviado a {to}: {message}")
    return response.json()

# Webhook de verificación
@app.route("/webhook", methods=["GET"])
def verify():
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if token == VERIFY_TOKEN:
        return challenge
    return "Error de verificación", 403

# Webhook de mensajes entrantes
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    logging.info(f"Mensaje recibido: {data}")

    if data and "entry" in data:
        for entry in data["entry"]:
            if "changes" in entry:
                for change in entry["changes"]:
                    value = change.get("value", {})
                    messages = value.get("messages", [])
                    for message in messages:
                        from_number = message["from"]
                        text = message.get("text", {}).get("body", "").lower()

                        if text in ["menu", "menú", "hola", "hi", "hello"]:
                            menu = (
                                "Vicky:\n👋 Hola, soy *Vicky*, asistente de Christian López.\n"
                                "Selecciona una opción escribiendo el número correspondiente:\n\n"
                                "1️⃣ Asesoría en pensiones\n"
                                "2️⃣ Seguros de auto 🚗\n"
                                "3️⃣ Seguros de vida y salud ❤️\n"
                                "4️⃣ Tarjetas médicas VRIM 🏥\n"
                                "5️⃣ Préstamos a pensionados IMSS 💰\n"
                                "6️⃣ Financiamiento empresarial 💼\n"
                                "7️⃣ Nómina empresarial 🏦\n"
                                "8️⃣ Contactar con Christian 📞\n\n"
                                "👉 También puedes escribir *menu* en cualquier momento para ver estas opciones."
                            )
                            send_whatsapp_message(from_number, menu)

                        elif text == "2":
                            send_whatsapp_message(from_number, "🚗 *Seguros de auto Inbursa.*\n(Planes y requisitos para cotizar).")

                        else:
                            send_whatsapp_message(from_number, "❓ No entendí tu mensaje. Escribe *menu* para ver las opciones disponibles.")

    return jsonify({"status": "ok"}), 200

# Health check
@app.route("/health", methods=["GET"])
def health():
    return "OK", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
