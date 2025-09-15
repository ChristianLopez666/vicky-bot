#!/usr/bin/env python3
import os
import hmac
import hashlib
import logging
from dotenv import load_dotenv
from typing import Any, Dict

from flask import Flask, request, jsonify, Response

# Load .env
load_dotenv(override=True)

# Logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL)
logger = logging.getLogger("vicky")

from core_router import route_message
from integrations_gpt import send_whatsapp_message, ask_gpt

app = Flask(__name__)

# Env vars
META_APP_SECRET = os.getenv("META_APP_SECRET", "")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "")
ADVISOR_NUMBER = os.getenv("ADVISOR_NUMBER", "")
PORT = int(os.getenv("PORT", 5000))


def _valid_signature(req: request) -> bool:
    if not META_APP_SECRET:
        return True
    header = req.headers.get("X-Hub-Signature-256", "")
    if not header:
        return False
    try:
        body = req.get_data() or b""
        mac = hmac.new(META_APP_SECRET.encode("utf-8"), msg=body, digestmod=hashlib.sha256)
        expected = "sha256=" + mac.hexdigest()
        return hmac.compare_digest(expected, header)
    except Exception:
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
    return Response("Forbidden", status=403, content_type="text/plain")


@app.route("/webhook", methods=["POST"])
def webhook_receive():
    if not _valid_signature(request):
        return jsonify({"ok": False, "error": "Invalid signature"}), 403

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "Payload vacío"}), 200

    try:
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value") or {}
                if value.get("statuses"):
                    continue
                for msg in value.get("messages", []) or []:
                    wa_from = msg.get("from", "")
                    wa_id = msg.get("id", "")
                    if not wa_from or not wa_id:
                        continue

                    text_in = _extract_text_from_message(msg).strip()
                    reply = None

                    try:
                        # 👉 Menú solo si es "menu" o un número
                        if text_in.lower() == "menu" or text_in.isdigit():
                            reply = route_message(wa_id=wa_id, wa_e164_no_plus=wa_from, text_in=text_in)
                        else:
                            # 👉 Todo lo demás → GPT directo
                            reply = ask_gpt(text_in)
                    except Exception:
                        reply = "⚠️ Lo siento, tuve un problema procesando tu mensaje."

                    if reply:
                        send_whatsapp_message(wa_from, reply)

                        # Notificación al asesor si aplica
                        if ADVISOR_NUMBER and ADVISOR_NUMBER != wa_from and "Notifiqué a Christian" in reply:
                            notify_text = f"Notificación de {wa_from}\nÚltimo mensaje:\n{(text_in or '[sin texto]')}"
                            send_whatsapp_message(ADVISOR_NUMBER, notify_text)
    except Exception as e:
        logger.exception("Error procesando webhook: %s", e)

    return jsonify({"ok": True}), 200


@app.route("/send_test", methods=["GET"])
def send_test():
    if not ADVISOR_NUMBER:
        return jsonify({"ok": False, "error": "ADVISOR_NUMBER no configurado"}), 200
    try:
        send_whatsapp_message(ADVISOR_NUMBER, "Mensaje de prueba desde Vicky Bot ✅")
        return jsonify({"ok": True}), 200
    except Exception:
        return jsonify({"ok": False, "error": "Fallo al enviar mensaje de prueba"}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
