import os
from flask import Flask, request
import logging

app = Flask(__name__)

# Configurar logging para Render
logging.basicConfig(level=logging.INFO)

# Ruta de verificación del webhook (GET)
@app.route('/webhook', methods=['GET'])
def verify():
    VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        logging.info("✅ Webhook verificado correctamente.")
        return challenge, 200
    else:
        logging.warning("❌ Fallo en la verificación del webhook.")
        return "Verification failed", 403

# Ruta para recibir mensajes (POST)
@app.route('/webhook', methods=['POST'])
def receive_message():
    data = request.get_json()
    logging.info(f"📨 Mensaje recibido: {data}")
    return "EVENT_RECEIVED", 200

# Ruta de prueba de vida
@app.route('/health', methods=['GET'])
def health_check():
    return "Vicky está viva 🟢", 200
# Solo para pruebas locales
if __name__ == '__main__':
    app.run(debug=True)

