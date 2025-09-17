#!/usr/bin/env python3
import os
import hmac
import hashlib
import logging
from dotenv import load_dotenv
from typing import Any, Dict

from flask import Flask, request, jsonify, Response

# Load .env and override environment (required)
load_dotenv(override=True)

# Logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL)
logger = logging.getLogger("vicky")

from core_router import route_message  # keep exact import/signature
from integrations_gpt import send_whatsapp_message, ask_gpt  # ahora también ask_gpt

app = Flask(__name__)

# Required envs
META_APP_SECRET = os.getenv("META_APP_SECRET", "")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "")
ADVISOR_NUMBER = os.getenv("ADVISOR_NUMBER", "")
PORT = int(os.getenv("PORT", 5000))


def _valid_signature(req: request) -> bool:
    if not META_APP_SECRET:
        logger.warning("META_APP_SECRET is not set; skipping signature validation.")
        return True

    header = req.headers.get("X-Hub-Signature-256", "")
    if not header:
        logger.warning("Missing X-Hub-Signature-256 header.")
        return False

    try:
        body = req.get_data() or b""
        mac = hmac.new(META_APP_SECRET.encode("utf-8"), msg=body, digestmod=hashlib.sha256)
        expected = "sha256=" + mac.hexdigest()
        valid = hmac.compare_digest(expected, header)
        if not valid:
            logger.warning("Invalid X-Hub-Signature-256 for incoming request.")
        return valid
    except Exception:
        logger.exception("Exception while validating signature.")
        return False


def _extract_text_from_message(msg: Dict[str, Any]) -> str:
    try:
        mtype = msg.get("type", "")
        if mtype == "text":
            return msg.get("text", {}).get("body", "") or ""
        if mtype == "interactive":
            interactive = msg.get("interactive", {}) or {}
            if "button_reply" in interactive:
                return interactive.get("button_reply", {}).get("title", "") or ""
            if "list_reply" in interactive:
                return interactive.get("list_reply", {}).get("title", "") or ""
            return interactive.get("text", "") or ""
        return f"[{mtype}]"
    except Exception:
        logger.exception("Error extracting text from message.")
        return ""


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "message": "Vicky Bot funcionando"}), 200


@app.route("/webhook", methods=["GET"])
def webhook_verify():
    mode = request.args.get("hub.mode", "")
    token = request.args.get("hub.verify_token", "")
    challenge = request.args.get("hub.challenge", "")

    if mode == "subscribe" and token and token == VERIFY_TOKEN:
        return Response(challenge, status=200, content_type="text/plain")
    logger.warning("Webhook verification failed: mode=%s token_provided=%s", mode, bool(token))
    return Response("Forbidden", status=403, content_type="text/plain")


@app.route("/webhook", methods=["POST"])
def webhook_receive():
    if not _valid_signature(request):
        return jsonify({"ok": False, "error": "Invalid signature"}), 403

    data = request.get_json(silent=True)
    data_preview = (str(data)[:1000] + "...") if data is not None and len(str(data)) > 1000 else str(data)
    if not isinstance(data, dict):
        logger.warning("Payload vacío o no es JSON dict: %s", data_preview)
        return jsonify({"ok": False, "error": "Payload vacío"}), 200

    try:
        entries = data.get("entry", [])
        for entry in entries:
            for change in entry.get("changes", []):
                value = change.get("value") or {}
                if value.get("statuses"):
                    logger.debug("Ignored statuses in value.")
                    continue
                messages = value.get("messages", []) or []
                if not messages:
                    logger.debug("No messages found in change.value (preview %s)", data_preview[:200])
                    continue
                for msg in messages:
                    wa_from = msg.get("from", "")
                    wa_id = msg.get("id", "")
                    if not wa_from or not wa_id:
                        logger.warning("Mensaje sin 'from' o 'id' - ignorado. preview: %s", str(msg)[:400])
                        continue
                    text_in = _extract_text_from_message(msg)
                    reply = None
                    try:
                        reply = route_message(wa_id=wa_id, wa_e164_no_plus=wa_from, text_in=text_in)
                    except Exception:
                        logger.exception("Error en route_message para wa_id=%s", wa_id)
                        reply = None

                    # 👇 Nuevo bloque: fallback a GPT
                    if not reply and text_in:
                        try:
                            reply = ask_gpt(text_in)
                        except Exception:
                            logger.exception("Error consultando GPT para wa_id=%s", wa_id)
                            reply = "⚠️ Lo siento, tuve un problema procesando tu mensaje."

                    if reply:
                        try:
                            send_whatsapp_message(wa_from, reply)
                        except Exception:
                            logger.exception("Error enviando respuesta a %s", wa_from)

                        try:
                            if ADVISOR_NUMBER and ADVISOR_NUMBER != wa_from and "Notifiqué a Christian" in reply:
                                notify_text = (
                                    f"Notificación de {wa_from}\n"
                                    f"Último mensaje:\n{(text_in or '[sin texto]')}"
                                )
                                try:
                                    send_whatsapp_message(ADVISOR_NUMBER, notify_text)
                                except Exception:
                                    logger.exception("Error notificando al asesor %s", ADVISOR_NUMBER)
                        except Exception:
                            logger.exception("Error comprobando necesidad de notificar al asesor.")
    except Exception:
        logger.exception("Error general procesando webhook; payload preview: %s", data_preview)

    return jsonify({"ok": True}), 200


@app.route("/send_test", methods=["GET"])
def send_test():
    if not ADVISOR_NUMBER:
        logger.warning("/send_test llamado pero ADVISOR_NUMBER no está configurado.")
        return jsonify({"ok": False, "error": "ADVISOR_NUMBER no configurado"}), 200
    try:
        send_whatsapp_message(ADVISOR_NUMBER, "Mensaje de prueba desde Vicky Bot ✅")
        return jsonify({"ok": True}), 200
    except Exception:
        logger.exception("Error enviando mensaje de prueba a ADVISOR_NUMBER %s", ADVISOR_NUMBER)
        return jsonify({"ok": False, "error": "Fallo al enviar mensaje de prueba"}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
