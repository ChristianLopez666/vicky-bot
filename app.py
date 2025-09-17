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

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
