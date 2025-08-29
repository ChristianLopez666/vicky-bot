import os
from flask import Flask, request

app = Flask(__name__)

# Token de verificación que debe coincidir con el configurado en Meta
VERIFY_TOKEN = "vicky-verify-token"

# Ruta para verificación (GET) y recepción de mensajes (POST)
@app.route('/webhook', methods=['GET', 'POST'])
def webhook():
    if request.method == 'GET':
        # Parámetros enviados por Meta para verificar
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")

        # Verificamos que el modo y token coincidan
        if mode == "subscribe" and token == VERIFY_TOKEN:
            print("✅ Webhook verificado correctamente.")
            return challenge, 200
        else:
            print("❌ Verificación fallida.")
            return "Verification failed", 403

    elif request.method == 'POST':
        # Mensaje recibido desde WhatsApp
        data = request.get_json()
        print("📩 Mensaje recibido:")
        print(data)
        return "EVENT_RECEIVED", 200

    # Método no permitido
    return "Method not allowed", 405

# Ruta de prueba para ver si está vivo el servidor
@app.route('/health', methods=['GET'])
def health_check():
    return "OK", 200

