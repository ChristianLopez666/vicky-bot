import os
from flask import Flask, request

app = Flask(__name__)

# Cargar el token de verificación desde las variables de entorno
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "vicky-verify-token")

@app.route('/webhook', methods=['GET', 'POST'])
def webhook():
    if request.method == 'GET':
        mode = request.args.get('hub.mode')
        token = request.args.get('hub.verify_token')
        challenge = request.args.get('hub.challenge')

        if mode == 'subscribe' and token == VERIFY_TOKEN:
            print("✅ Webhook verificado correctamente.")
            return challenge, 200
        else:
            print("❌ Verificación fallida. Token inválido.")
            return "Token de verificación inválido", 403

    if request.method == 'POST':
        data = request.get_json()
        print("📩 Mensaje recibido:", data)
        return "EVENT_RECEIVED", 200

    return "Método no permitido", 405

@app.route('/health', methods=['GET'])
def health_check():
    return "Vicky está viva", 200

if __name__ == '__main__':
    app.run(debug=True)



if __name__ == "__main__":
    # Para desarrollo local (Render usará gunicorn con -b 0.0.0.0:$PORT)
    port = int(os.getenv("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
