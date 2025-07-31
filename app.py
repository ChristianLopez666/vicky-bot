from flask import Flask, request, jsonify
import os
import openai
import gspread
import json
from oauth2client.service_account import ServiceAccountCredentials

app = Flask(__name__)

# Configuración de OpenAI (opcional)
openai.api_key = os.getenv("OPENAI_API_KEY")

# Cargar credenciales desde variable de entorno
google_creds_raw = os.getenv("GOOGLE_CREDENTIALS_JSON")
if isinstance(google_creds_raw, str):
    creds_dict = json.loads(google_creds_raw)
else:
    creds_dict = google_creds_raw  # En caso Render ya la haya interpretado

scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]
credentials = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
client = gspread.authorize(credentials)
sheet = client.open("Prospectos SECOM Auto").sheet1

def get_client_name_from_whatsapp(phone):
    registros = sheet.get_all_records()
    for fila in registros:
        numero = str(fila.get("Whatsapp", "")).strip().replace("+52", "").replace(" ", "")
        if numero.endswith(phone[-10:]):
            return fila.get("Nombre", "")
    return None

@app.route("/", methods=["GET"])
def index():
    return "Bot Vicky activo 🚀"

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        incoming_data = request.get_json(force=True)
        message = incoming_data['messages'][0]
        from_number = message['from']
        body = message.get('text', {}).get('body', '')

        nombre = get_client_name_from_whatsapp(from_number)

        if nombre:
            respuesta = f"Hola {nombre}, soy Vicky 🤖. ¡Tienes un beneficio especial en tu seguro de auto! 🚗"
        else:
            respuesta = "Hola, soy Vicky 🤖, asistente de Christian López. Aquí tienes nuestro menú de servicios disponibles: [Menú]"

        return jsonify({
            "messages": [
                {
                    "to": from_number,
                    "type": "text",
                    "text": {
                        "body": respuesta
                    }
                }
            ]
        })

    except Exception as e:
        print(f"Error en webhook: {e}")
        return "error", 500

if __name__ == "__main__":
    app.run(debug=True)
