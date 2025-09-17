import os
from flask import Flask, request
from config_env import VERIFY_TOKEN, LOG_LEVEL, ADVISOR_NUMBER
from utils_logger import get_logger
from utils_validators import extract_messages, safe_text, safe_from
from core_router import handle_incoming_message
from core_whatsapp import send_text

app = Flask(__name__)
log = get_logger("app", LOG_LEVEL)

@app.route("/", methods=["GET"])
def index():
    return "Vicky FASE 1 – OK ✅", 200

@app.route("/health", methods=["GET"])
def health():
    status = {"verify_token": bool(VERIFY_TOKEN), "advisor_number": bool(ADVISOR_NUMBER), "status": "ok"}
    return status, 200

@app.route("/webhook", methods=["GET"])
def verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        log.info("✅ Webhook verificado correctamente")
        return challenge, 200
    log.warning("❌ Verificación fallida")
    return "Verification failed", 403

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    try:
        msgs = extract_messages(data)
        if not msgs:
            return "EVENT_RECEIVED", 200

        for m in msgs:
            wa_from = safe_from(m)
            text = safe_text(m)
            log.info("📥 %s: %s", wa_from, text)

            reply = handle_incoming_message(wa_from, text)
            send_text(wa_from, reply)

            if reply.endswith("*Notifiqué a Christian*.") and ADVISOR_NUMBER:
                aviso = f"📞 Cliente solicita contacto.\nDe: {wa_from}\nMensaje: {text}"
                send_text(ADVISOR_NUMBER, aviso)

    except Exception as e:
        log.exception("⚠️ Error procesando webhook: %s", e)
        return "EVENT_RECEIVED", 200

    return "EVENT_RECEIVED", 200

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
