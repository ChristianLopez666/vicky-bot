from flask import Flask, request
import os
import logging

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# Token usado por Meta para la verificación
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "vicky-verify-token")

@app.route("/", methods=["GET"])
def index():
    return "Bot Vicky activo 🚀"

# Webhook: debe aceptar GET (verificación) y POST (eventos)
@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")

        if mode == "subscribe" and token == VERIFY_TOKEN and challenge:
            logging.info("Webhook verificado por Meta.")
            # Meta espera el challenge en texto plano y 200
            return challenge, 200
        logging.warning("Fallo verificación: mode=%s token_ok=%s", mode, token == VERIFY_TOKEN)
        return "Verification failed", 403

    # POST: eventos entrantes
    data = request.get_json(silent=True) or {}
    logging.info("Evento recibido: %s", str(data)[:1200])
    return "EVENT_RECEIVED", 200

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
