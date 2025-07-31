from flask import Flask, reqfrom flask import Flask, request, jsonify
import os
import json

app = Flask(__name__)

@app.route("/", methods=["GET"])
def home():
    return "Vicky está en línea"

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        incoming_data = request.get_json(force=True)
        message = incoming_data['messages'][0]
        from_number = message['from']
        body = message.get('text', {}).get('body', '')

        nombre = get_client_name_from_whatsapp(from_number)

        if nombre:
            respuesta = f"Hola {nombre}, soy Vicky 🤖. ¡Tienes un beneficio especial en tu seguro de auto!"
        else:
            respuesta = "Hola, soy Vicky 🤖, asistente de Christian López. Aquí para ayudarte 😊"

        return {
            "messages": [
                {
                    "to": from_number,
                    "text": {
                        "body": respuesta
                    }
                }
            ]
        }
    except Exception as e:
        print("Error:", e)
        return jsonify({"error": "Ocurrió un error"}), 500

def get_client_name_from_whatsapp(whatsapp_number):
    # Lógica provisional o conexión con Google Sheets
    return None

if __name__ == "__main__":
    app.run(debug=True)


