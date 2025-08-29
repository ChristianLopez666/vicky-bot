import os
from flask import Flask, request, jsonify

# 1. Instancia la aplicación Flask
app = Flask(__name__)

# 2. Configura las variables de entorno
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "vicky-verify-token")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")

# 3. Implementa la ruta /webhook
@app.route('/webhook', methods=['GET', 'POST'])
def webhook():
    # Lógica para la verificación del webhook (GET)
    if request.method == 'GET':
        mode = request.args.get('hub.mode')
        token = request.args.get('hub.verify_token')
        challenge = request.args.get('hub.challenge')
        
        # Valida que el modo y el token sean correctos
        if mode == 'subscribe' and token == VERIFY_TOKEN:
            print("Webhook verificado exitosamente.")
            return challenge, 200
        else:
            return jsonify({"error": "Token de verificación o modo incorrecto"}), 403

    # Lógica para recibir mensajes (POST)
    elif request.method == 'POST':
        data = request.json
        print("Mensaje recibido:", data)
        # Aquí iría la lógica para procesar los mensajes
        return jsonify({"status": "success"}), 200

# 4. Inicia el servidor
if __name__ == '__main__':
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
