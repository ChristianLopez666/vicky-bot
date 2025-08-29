from flask import Flask, request, jsonify
import os
import logging

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# Token que Meta usará para verificar el webhook
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "vicky-verify-token")

@app.route("/", methods=["GET"])
def index():
    # Respuesta simple para health/landing
    return "Bot Vicky activo 🚀"

@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        # Verificación de Meta (debe devolver el challenge en TEXTO plano)
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")

        ok = (mode == "subscribe") and (token == VERIFY_TOKEN) and bool(challenge)
        if ok:
            logging.info("✅ Webhook verificado por Meta.")
            return challenge, 200  # ← Importante: texto plano
        logging.warning("❌ Verificación fallida: mode=%s token_ok=%s", mode, token == VERIFY_TOKEN)
        return jsonify({"error": "Forbidden"}), 403

    # POST: eventos entrantes
    data = request.get_json(silent=True) or {}
    logging.info("📩 Evento recibido: %s", str(data)[:1200])

    # Devolvemos JSON consistente
    return jsonify({"status": "EVENT_RECEIVED"}), 200

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
