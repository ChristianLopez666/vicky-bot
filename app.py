from flask import Flask, request
import os
import json
from oauth2client.service_account import ServiceAccountCredentials
import gspread
from twilio.twiml.messaging_response import MessagingResponse
from dotenv import load_dotenv

# Solo se usa en local
load_dotenv()

# Configura Flask
app = Flask(__name__)

# Configura Google Sheets desde variable de entorno
scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]

google_credentials_json = os.environ["GOOGLE_CREDENTIALS_JSON"]
credentials_dict = json.loads(google_credentials_json)

credentials = ServiceAccountCredentials.from_json_keyfile_dict(credentials_dict, scope)
client = gspread.authorize(credentials)

# Accede a la hoja de cálculo
sheet = client.open("Prospectos SECOM Auto").sheet1  # Asegúrate que este nombre coincide con el real

@app.route("/webhook", methods=["POST"])
def webhook():
    incoming_msg = request.values.get("Body", "").strip().lower()
    sender_number = request.values.get("From", "").replace("whatsapp:", "")

    resp = MessagingResponse()
    msg = resp.message()

    # Respuesta genérica temporal
    if "hola" in incoming_msg or "menu" in incoming_msg:
        msg.body("👋 ¡Hola! Soy Vicky, asistente de Christian López.\n\n📋 Opciones:\n1️⃣ Seguro de Auto\n2️⃣ Seguro de Vida\n3️⃣ Préstamo a Pensionados\n\nResponde con una opción para continuar.")
    else:
        msg.body("✅ Recibido. Estamos procesando tu mensaje...")

    return str(resp)

# Ruta raíz para confirmar despliegue
@app.route("/", methods=["GET"])
def home():
    return "Vicky está activa 🚀"

if __name__ == "__main__":
    app.run(debug=True)
