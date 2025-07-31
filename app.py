from flask import Flask, request
import os
import json
from dotenv import load_dotenv
from twilio.twiml.messaging_response import MessagingResponse
from oauth2client.service_account import ServiceAccountCredentials
import gspread

load_dotenv()

app = Flask(__name__)

openai_api_key = os.getenv("OPENAI_API_KEY")


def get_client_name_from_whatsapp(number):
    try:
        google_credentials_json = os.environ["GOOGLE_CREDENTIALS_JSON"]
        credentials_dict = json.loads(google_credentials_json)

        scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
        credentials = ServiceAccountCredentials.from_json_keyfile_dict(credentials_dict, scope)

        gc = gspread.authorize(credentials)
        sheet = gc.open("Prospectos SECOM Auto").sheet1
        values = sheet.get_all_records()

        for row in values:
            if str(row['WhatsApp']).strip() == str(number).strip():
                return row['Nombre']

        return None

    except Exception as e:
        print("Error al buscar en Sheets:", e)
        return None


@app.route("/webhook", methods=["POST"])
def webhook():
    incoming_data = request.get_json()
    try:
        message = incoming_data['messages'][0]
        from_number = message['from']
        body = message.get('text', {}).get('body', '')

        nombre = get_client_name_from_whatsapp(from_number)

        if nombre:
            respuesta = f"Hola {nombre}, soy Vicky 🤖. ¡Tienes un beneficio especial en tu seguro de auto! 🚗💸 ¿Te interesa conocer tu descuento?"
        else:
            respuesta = "Hola, soy Vicky 🤖, asistente de Christian López. Aquí tienes el menú de servicios disponibles para ti. ¿En qué te gustaría que te apoye hoy?"

        return {
            "messages": [
                {
                    "to": from_number,
                    "type": "text",
                    "text": {"body": respuesta}
                }
            ]
        }

    except Exception as e:
        print("Error en webhook:", e)
        return "Error", 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
