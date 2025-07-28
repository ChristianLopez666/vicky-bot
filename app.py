from flask import Flask, request
import os
from dotenv import load_dotenv
from twilio.twiml.messaging_response import MessagingResponse

# Cargar variables de entorno desde .env
load_dotenv()

app = Flask(__name__)

@app.route("/webhook", methods=["POST"])
def webhook():
    incoming_msg = request.values.get("Body", "").strip().lower()
    resp = MessagingResponse()
    msg = resp.message()

    # Lógica simple para responder
    if "hola" in incoming_msg:
        msg.body("¡Hola! Soy Vicky, tu asistente virtual 🤖 ¿En qué puedo ayudarte hoy?")
    elif "seguro" in incoming_msg:
        msg.body("Ofrezco seguros de auto, vida y salud. ¿Cuál te interesa?")
    elif "préstamo" in incoming_msg or "prestamo" in incoming_msg:
        msg.body("Tenemos préstamos para pensionados IMSS desde $10,000 hasta $650,000. ¿Quieres una cotización?")
    else:
        msg.body("No entendí tu mensaje 🤔. Prueba con palabras como 'seguro', 'préstamo', 'hola', etc.")

    return str(resp)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
