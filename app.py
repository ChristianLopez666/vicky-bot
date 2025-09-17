import logging
import requests
from flask import Flask, request
from config_env import VERIFY_TOKEN, WHATSAPP_TOKEN, PHONE_NUMBER_ID

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# ====== FUNCIONES AUXILIARES ======
def send_whatsapp_message(to: str, message: str):
    url = f"https://graph.facebook.com/v23.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    data = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": message}
    }
    response = requests.post(url, headers=headers, json=data)
    logging.info(f"📤 Enviado a {to}: {message} | Respuesta: {response.status_code} {response.text}")
    return response.json()

def get_menu_text():
    return (
        "👋 Hola, soy *Vicky*, asistente de Christian López.\n\n"
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

def process_message(body: str):
    body_lower = body.strip().lower()
    if body_lower in ["menu", "menú", "hola", "hi", "hello"]:
        return get_menu_text()
    elif body_lower == "1":
        return "📊 *Asesoría en pensiones.*\n(Modalidad 40, Ley 73, cálculo de pensión, etc.)"
    elif body_lower == "2":
        return "🚗 *Seguros de auto Inbursa.*\n(Planes y requisitos para cotizar)."
    elif body_lower == "3":
        return "❤️ *Seguros de vida y salud.*\n(Protección para ti y tu familia)."
    elif body_lower == "4":
        return "🏥 *Tarjetas médicas VRIM.*\n(Acceso a servicios médicos privados)."
    elif body_lower == "5":
        return "💰 *Préstamos a pensionados IMSS.*\n(Montos desde $10,000 hasta $650,000)."
    elif body_lower == "6":
        return "💼 *Financiamiento empresarial.*\n(Crédito, factoraje, arrendamiento)."
    elif body_lower == "7":
        return "🏦 *Nómina empresarial.*\n(Dispersión de nómina y beneficios adicionales)."
    elif body_lower == "8":
        return "📞 Un asesor de Christian se pondrá en contacto contigo."
    else:
        return "❓ No entendí tu mensaje. Escribe *menu* para ver las opciones disponibles."

# ====== RUTAS WEBHOOK ======
@app.route("/webhook", methods=["GET"])
def verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        logging.info("✅ Webhook verificado correctamente")
        return challenge, 200
    logging.warning("❌ Verificación de webhook fallida")
    return "Verification failed", 403

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    logging.info(f"📩 Mensaje recibido: {data}")

    try:
        if "messages" in data["entry"][0]["changes"][0]["value"]:
            message = data["entry"][0]["changes"][0]["value"]["messages"][0]
            number = message["from"]
            text = message.get("text", {}).get("body", "")

            logging.info(f"📥 Mensaje de {number}: {text}")
            response_text = process_message(text)
            send_whatsapp_message(number, response_text)
    except Exception as e:
        logging.error(f"⚠️ Error procesando mensaje: {e}")

    return "EVENT_RECEIVED", 200

@app.route("/health", methods=["GET"])
def health():
    return "Bot Vicky corriendo OK ✅", 200

if __name__ == "__main__":
    import os
    port = int(os.getenv("PORT", 5000))  # Render usa $PORT, local usa 5000
    app.run(host="0.0.0.0", port=port)
