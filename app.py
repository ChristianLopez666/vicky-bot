<<<<<<< HEAD
import os
import logging
import requests
from flask import Flask, request, jsonify
from config_env import (
    VERIFY_TOKEN,
    WHATSAPP_TOKEN,
    PHONE_NUMBER_ID,
    OPENAI_API_KEY,
    GOOGLE_SHEETS_KEY
)

app = Flask(__name__)

# Configurar logging
logging.basicConfig(level=logging.INFO)

# Endpoint de verificación del webhook
@app.route("/webhook", methods=["GET"])
def verify():
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    mode = request.args.get("hub.mode")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        logging.info("✅ Webhook verificado correctamente")
        return challenge, 200
    else:
        logging.warning("❌ Error en la verificación del webhook")
        return "Verification failed", 403

# Endpoint para recibir mensajes
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    logging.info(f"📩 Mensaje recibido: {data}")

    try:
        if "entry" in data:
            for entry in data["entry"]:
                if "changes" in entry:
                    for change in entry["changes"]:
                        if "value" in change and "messages" in change["value"]:
                            for message in change["value"]["messages"]:
                                sender = message["from"]
                                if "text" in message:
                                    incoming_text = message["text"]["body"]
                                    logging.info(f"👤 {sender}: {incoming_text}")
                                    send_whatsapp_message(sender, "Hola 👋 soy Vicky, asistente de Christian López.")
        return "EVENT_RECEIVED", 200
    except Exception as e:
        logging.error(f"⚠️ Error procesando mensaje: {e}")
        return "ERROR", 500

# Función para enviar mensajes a WhatsApp
def send_whatsapp_message(to, text):
    url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text}
    }
    response = requests.post(url, headers=headers, json=payload)
    logging.info(f"📤 Respuesta de WhatsApp API: {response.status_code} {response.text}")

# Endpoint de salud
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200
=======
import logging
import os
import requests
from flask import Flask, request
from config_env import (
    VERIFY_TOKEN, WHATSAPP_TOKEN, PHONE_NUMBER_ID,
    WA_API_VERSION, ADVISOR_NUMBER, LOG_LEVEL
)

# 🔧 Import robusto para evitar error en Render
try:
    from core_router import route_message
except ModuleNotFoundError:
    import core_router
    route_message = core_router.route_message

app = Flask(__name__)
logging.basicConfig(level=getattr(logging, (LOG_LEVEL or "INFO").upper(), logging.INFO))
log = app.logger

API_BASE = f"https://graph.facebook.com/{WA_API_VERSION}".rstrip("/")

# ====== Helpers ======
def send_whatsapp_text(to: str, message: str):
    """Envía un mensaje de texto vía WhatsApp Cloud API."""
    url = f"{API_BASE}/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": message}
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=15)
        log.info("📤 Enviado a %s: %s | %s %s", to, (message[:120] + "…") if len(message) > 120 else message, resp.status_code, resp.text)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.exception("Error enviando mensaje a %s", to)
        return {"error": str(e)}

# ====== Rutas ======
@app.route("/", methods=["GET"])
def index():
    return "Vicky Bot – FASE 1 OK ✅", 200

@app.route("/health", methods=["GET"])
def health():
    return "Bot Vicky corriendo OK ✅", 200

@app.route("/webhook", methods=["GET"])
def verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        log.info("✅ Webhook verificado correctamente")
        return challenge, 200
    log.warning("❌ Verificación de webhook fallida")
    return "Verification failed", 403

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    log.info("📩 Evento recibido")

    try:
        # Estructura oficial de Meta
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                messages = value.get("messages", [])
                for message in messages:
                    wa_from = message.get("from")  # E.164 sin '+'
                    text = (message.get("text", {}) or {}).get("body", "")

                    log.info("📥 Mensaje de %s: %s", wa_from, text)
                    reply = route_message(wa_from, wa_from, text)

                    # Enviar respuesta al usuario
                    send_whatsapp_text(wa_from, reply)

                    # Si es la opción 8, notificar al asesor
                    if reply.endswith("*Notifiqué a Christian*.") and ADVISOR_NUMBER:
                        aviso = f"📞 Cliente solicita contacto.\nDe: {wa_from}\nMensaje: {text}"
                        send_whatsapp_text(ADVISOR_NUMBER, aviso)
    except Exception:
        log.exception("⚠️ Error procesando webhook")

    return "EVENT_RECEIVED", 200
>>>>>>> 65514338df9e2ce71ab1d251ea76ee0f79bb2b93

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
<<<<<<< HEAD

=======
>>>>>>> 65514338df9e2ce71ab1d251ea76ee0f79bb2b93
