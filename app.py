import os
from flask import Flask, request, jsonify

# Inicializa la aplicación Flask
app = Flask(__name__)

# Carga el token de verificación desde las variables de entorno de Render
# Usa un valor por defecto si no está definido
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "vicky-verify-token")

@app.route('/webhook', methods=['GET', 'POST'])
def webhook():
    """
    Ruta para la verificación del webhook (GET) y para procesar
    los mensajes entrantes de WhatsApp (POST).
    """
    # Lógica para la verificación del webhook (solicitud GET de Meta)
    if request.method == 'GET':
        mode = request.args.get('hub.mode')
        token = request.args.get('hub.verify_token')
        challenge = request.args.get('hub.challenge')
        
        # Comprueba si el modo y el token son correctos
        if mode == 'subscribe' and token == VERIFY_TOKEN:
            # Responde con el challenge para verificar el webhook
            print("Webhook verificado exitosamente.")
            return challenge, 200
        else:
            # Si el token o el modo no coinciden, devuelve un error 403
            print("Error: Token de verificación o modo incorrecto.")
            return jsonify({"error": "Forbidden"}), 403

    # Lógica para procesar mensajes entrantes (solicitud POST)
    elif request.method == 'POST':
        data = request.json
        print("Mensaje recibido:", data)
        # Aquí iría tu lógica para procesar los mensajes.
        # Por ahora, simplemente devuelve una respuesta exitosa.
        return jsonify({"status": "success"}), 200

# Inicia la aplicación en el puerto y host correctos para Render
if __name__ == '__main__':
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
