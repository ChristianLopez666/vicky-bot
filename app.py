import os
import logging
import requests
from flask import Flask, request, jsonify
import openai

# Configuración básica
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# Variables de entorno
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

openai.api_key = OPENAI_API_KEY

# 🟢 Endpoint de salud
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "message": "Vicky Bot corriendo correctamente"}), 200

# 🟢 Verificación de webhook
@app.route("/webhook", methods=["GET"])
def verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        logging.info("✅ Webhook verificado correctamente.")
        return challenge, 200
    else:
        logging.warning("❌ Fallo en la verificación del webhook.")
        return "Verification failed", 403

# 🟢 Recepción de mensajes
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    logging.info(f"📩 Mensaje recibido: {data}")

    if data and "entry" in data:
        for entry in data["entry"]:
            for change in entry.get("changes", []):
                value = change.get("value", {})
                messages = value.get("messages", [])
                for message in messages:
                    user_id = message["from"]
                    texto = message.get("text", {}).get("body", "").lower()

                    if texto == "menu":
                        menu = (
                            "👋 Hola, soy Vicky, asistente de Christian López.\n\n"
                            "Selecciona una opción:\n\n"
                            "1️⃣ Asesoría en pensiones\n"
                            "2️⃣ Seguros de auto 🚗\n"
                            "3️⃣ Seguros de vida y salud ❤️\n"
                            "4️⃣ Tarjetas médicas VRIM 🏥\n"
                            "5️⃣ Préstamos a pensionados IMSS 💰\n"
                            "6️⃣ Financiamiento empresarial 💼\n"
                            "7️⃣ Nómina empresarial 🏦\n"
                            "8️⃣ Contactar con Christian 📞"
                        )
                        enviar_mensaje_whatsapp(user_id, menu)
                    else:
                        respuesta = generar_respuesta_gpt(texto)
                        enviar_mensaje_whatsapp(user_id, respuesta)

    return "EVENT_RECEIVED", 200

# 🟢 Enviar mensaje por WhatsApp
def enviar_mensaje_whatsapp(to, body):
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body}
    }

    try:
        response = requests.post(url, headers=headers, json=payload)
        logging.info(f"✅ Mensaje enviado a {to}: {response.status_code}")
    except Exception as e:
        logging.error(f"❌ Error enviando mensaje a {to}: {str(e)}")

# 🟢 Generar respuesta con GPT
def generar_respuesta_gpt(mensaje_usuario):
    try:
        response = openai.ChatCompletion.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Eres Vicky, asistente de Christian López. Responde de forma clara, profesional y cercana."},
                {"role": "user", "content": mensaje_usuario}
            ],
            max_tokens=200,
            temperature=0.7
        )
        return response["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"⚠️ Error con GPT: {str(e)}"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
