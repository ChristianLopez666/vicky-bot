import os
from flask import Flask, request, jsonify

app = Flask(__name__)

# Verificación del webhook (GET)
@app.route('/webhook', methods=['GET'])
def verify():
    VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "vicky-verify-token")
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    else:
        return "Verification failed", 403

# Recepción de mensajes (POST)
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json()
    print("Mensaje recibido:", data)
    return "EVENT_RECEIVED", 200

# Endpoint de salud (opcional)
@app.route('/', methods=['GET'])
def health():
    return "Webhook activo y corriendo", 200

if __name__ == '__main__':
    app.run(debug=True)
