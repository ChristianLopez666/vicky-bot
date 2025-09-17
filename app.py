import logging
import requests
from flask import Flask, request
from config_env import VERIFY_TOKEN, WHATSAPP_TOKEN, PHONE_NUMBER_ID

# Configuración de logging
logging.basicConfig(level=logging.INFO)

app = Flask(__name__)

GRAPH_API_URL = f"https://graph.facebook.com/v17.0/{PHONE_NUMBER_ID}/messages"


# Función para enviar mensajes de WhatsApp
def enviar_mensaje(to, text):
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    body = {
        "messaging_product": "whatsapp",
        "to": to,
        "text": {"body": text}
    }

    try:
        response = requests.post(GRAPH_API_URL, headers=headers, json=body)
        logging.info(f"📤 Enviado a {to}: {text} | Respuesta: {response.status_code} {response.text}")
    except Exception as e:
        logging.error(f"❌ Error enviando mensaje: {e}")


# Endpoint de verificación del webhook
@app.route("/webhook", methods=["GET"])
def verificar_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        logging.info("✅ Webhook verificado correctamente.")
        return challenge, 200
    else:
        logging.warning("❌ Error en la verificación del webhook.")
        return "Verification failed", 403


# Endpoint para recibir mensajes
@app.route("/webhook", methods=["POST"])
def recibir_mensaje():
    data = request.get_json()
    logging.info(f"📩 Mensaje recibido: {data}")

    try:
        if "entry" in data:
            for entry in data["entry"]:
                for change in entry.get("changes", []):
                    if "messages" in change["value"]:
                        messages = change["value"]["messages"]
                        for message in messages:
                            numero = message["from"]
                            texto = message["text"]["body"].strip().lower()

                            logging.info(f"📥 Mensaje de {numero}: {texto}")

                            if texto in ["menu", "hola", "buenas", "inicio"]:
                                menu = (
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
                                enviar_mensaje(numero, menu)

                            elif texto == "1":
                                enviar_mensaje(numero, "📘 Asesoría en pensiones.\n\n(Cómo aumentar tu pensión, semanas y más).")

                            elif texto == "2":
                                enviar_mensaje(numero, "🚗 Seguros de auto Inbursa.\n(Planes y requisitos para cotizar).")

                            elif texto == "3":
                                enviar_mensaje(numero, "❤️ Seguros de vida y salud.\n(Protección para ti y tu familia).")

                            elif texto == "4":
                                enviar_mensaje(numero, "🏥 Tarjetas médicas VRIM.\n(Atención médica accesible y sin complicaciones).")

                            elif texto == "5":
                                enviar_mensaje(numero, "💰 Préstamos a pensionados IMSS.\n(Montos desde $10,000 hasta $650,000).")

                            elif texto == "6":
                                enviar_mensaje(numero, "💼 Financiamiento empresarial.\n(Impulsa tu negocio con nuestras soluciones).")

                            elif texto == "7":
                                enviar_mensaje(numero, "🏦 Nómina empresarial.\n(Mejora la dispersión de pagos y beneficios).")

                            elif texto == "8":
                                enviar_mensaje(numero, "📞 Gracias, Christian López será notificado para contactarte.")

    except Exception as e:
        logging.error(f"❌ Error procesando mensaje: {e}")

    return "EVENT_RECEIVED", 200


# Endpoint de salud
@app.route("/health", methods=["GET"])
def health():
    return "Vicky Bot funcionando ✅", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0")
