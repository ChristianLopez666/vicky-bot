import os
import logging
import requests
from flask import Flask, request, jsonify

# Configuración básica
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# Variables desde entorno
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")

# Endpoint base actualizado a v23.0
GRAPH_URL = f"https://graph.facebook.com/v23.0/{PHONE_NUMBER_ID}/messages"

# --- Rutas ---

# Verificación inicial del webhook
@app.route("/webhook", methods=["GET"])
def verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        logging.info("✅ Webhook verificado correctamente")
        return challenge, 200
    else:
        logging.warning("❌ Error en verificación del webhook")
        return "Verification failed", 403


# Recepción de mensajes
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    logging.info(f"📩 Mensaje recibido: {data}")

    try:
        if "entry" in data:
            for entry in data["entry"]:
                for change in entry.get("changes", []):
                    value = change.get("value", {})
                    messages = value.get("messages", [])
                    for msg in messages:
                        number = msg["from"]
                        text = msg.get("text", {}).get("body", "").strip().lower()

                        logging.info(f"📥 Mensaje de {number}: {text}")

                        if text in ["menu", "menú", "hola", "buenas", "hi"]:
                            send_menu(number)
                        elif text == "1":
                            send_message(number, "🧓 Asesoría en pensiones Inbursa.")
                        elif text == "2":
                            send_message(number, "🚗 Seguros de auto Inbursa.\n(Planes y requisitos para cotizar).")
                        elif text == "3":
                            send_message(number, "❤️ Seguros de vida y salud Inbursa.")
                        elif text == "4":
                            send_message(number, "🏥 Tarjetas médicas VRIM.")
                        elif text == "5":
                            send_message(number, "💰 Préstamos a pensionados IMSS.")
                        elif text == "6":
                            send_message(number, "💼 Financiamiento empresarial Inbursa.")
                        elif text == "7":
                            send_message(number, "🏦 Nómina empresarial Inbursa.")
                        elif text == "8":
                            send_message(number, "📞 Christian López te contactará pronto.")
                        else:
                            send_message(number, "👋 Escribe *menu* para ver las opciones disponibles.")
    except Exception as e:
        logging.error(f"❌ Error en procesamiento: {e}")

    return "EVENT_RECEIVED", 200


# --- Funciones auxiliares ---

def send_message(to, message):
    """Envía un mensaje simple de texto al número indicado."""
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
    response = requests.post(GRAPH_URL, headers=headers, json=payload)

    if response.status_code != 200:
        logging.error(f"❌ Error enviando mensaje: {response.text}")
    else:
        logging.info(f"📤 Enviado a {to}: {message}")


def send_menu(to):
    """Envía el menú principal."""
    menu_text = (
        "👋 Hola, soy *Vicky*, asistente de Christian López.\n"
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
    send_message(to, menu_text)


# Health check
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", debug=True)
