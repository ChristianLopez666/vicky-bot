import os
import logging
from flask import Flask, request, jsonify
from dotenv import load_dotenv

from config_env import VERIFY_TOKEN, ADVISOR_NUMBER, LOG_LEVEL
from core_whatsapp import send_whatsapp_message
from core_router import route_message

# Cargar variables de entorno
load_dotenv(override=True)

app = Flask(__name__)
logging.basicConfig(level=getattr(logging, LOG_LEVEL.upper(), logging.INFO))


# ------------------------
# Endpoint de salud
# ------------------------
@app.route("/health", methods=["GET"])
def health():
    """Endpoint de prueba de salud."""
    return jsonify({"status": "ok", "message": "Vicky Bot funcionando"}), 200


# ------------------------
# Verificación del webhook
# ------------------------
@app.route("/webhook", methods=["GET"])
def verify():
    """Verifica el webhook con Meta (GET)."""
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        logging.info("Webhook verificado correctamente.")
        return challenge, 200

    logging.warning("Fallo en verificación del webhook")
    return "Verification failed", 403


# ------------------------
# Recepción de mensajes
# ------------------------
@app.route("/webhook", methods=["POST"])
def receive():
    """Recibe y procesa mensajes de WhatsApp (POST)."""
    data = request.get_json(silent=True) or {}
    logging.info(f"Incoming: {str(data)[:1000]}")

    try:
        entry = (data.get("entry") or [{}])[0]
        changes = (entry.get("changes") or [{}])[0]
        value = changes.get("value", {})
        messages = value.get("messages", [])

        if not messages:
            return jsonify({"ignored": True}), 200

        msg = messages[0]
        wa_from = msg.get("from") or ""
        wa_id = msg.get("id", "")
        wa_type = msg.get("type", "text")

        # Extraer texto según tipo de mensaje
        text = ""
        if wa_type == "text":
            text = (msg.get("text") or {}).get("body", "").strip()
        elif wa_type == "interactive":
            inter = msg.get("interactive", {})
            text = (
                inter.get("list_reply", {}).get("title")
                or inter.get("button_reply", {}).get("title")
                or ""
            )
        else:
            text = f"[{wa_type}]"

        if not text:
            send_whatsapp_message(
                wa_from,
                "📩 Recibí tu mensaje, ¿puedes escribirlo en texto por favor?"
            )
            return jsonify({"ok": True}), 200

        # Ruta principal
        reply = route_message(wa_id=wa_id, wa_e164_no_plus=wa_from, text_in=text)
        send_whatsapp_message(wa_from, reply)

        # Notificación al asesor
        if "Notifiqué a Christian" in reply and ADVISOR_NUMBER and ADVISOR_NUMBER != wa_from:
            send_whatsapp_message(
                ADVISOR_NUMBER,
                f"📣 Nuevo contacto desde Vicky\nNúmero: {wa_from}\nMensaje: {text}"
            )

        return jsonify({"ok": True}), 200

    except Exception as e:
        logging.exception("Error procesando webhook")
        # Fallback: notificar al usuario
        if "wa_from" in locals() and wa_from:
            send_whatsapp_message(
                wa_from,
                "⚠️ Hubo un problema procesando tu mensaje. Intenta de nuevo más tarde."
            )
        return jsonify({"ok": False, "error": str(e)}), 200


# ------------------------
# Main
# ------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))

