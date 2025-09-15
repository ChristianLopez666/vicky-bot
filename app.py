# -*- coding: utf-8 -*-
#!/usr/bin/env python3
import os
import hmac
import hashlib
import logging
from typing import Any, Dict
from dotenv import load_dotenv
from flask import Flask, request, jsonify, Response

load_dotenv(override=True)

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL)
logger = logging.getLogger("vicky")

DEPLOY_SHA = os.getenv("RENDER_GIT_COMMIT", os.getenv("COMMIT_SHA", "unknown"))
logger.info("BOOT OK | DEPLOY_SHA=%s", DEPLOY_SHA)

from core_router import route_message
from integrations_gpt import send_whatsapp_message, ask_gpt

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False

META_APP_SECRET = os.getenv("META_APP_SECRET", "")
VERIFY_TOKEN     = os.getenv("VERIFY_TOKEN", "")
ADVISOR_NUMBER   = os.getenv("ADVISOR_NUMBER", "")
PORT             = int(os.getenv("PORT", 5000))

def _valid_signature(req: request) -> bool:
    if not META_APP_SECRET:
        logger.warning("META_APP_SECRET not set -> skipping signature check.")
        return True
    signature = req.headers.get("X-Hub-Signature-256", "")
    if not signature:
        logger.warning("Missing X-Hub-Signature-256 header.")
        return False
    try:
        body = req.get_data() or b""
        mac = hmac.new(META_APP_SECRET.encode("utf-8"), msg=body, digestmod=hashlib.sha256)
        expected = "sha256=" + mac.hexdigest()
        ok = hmac.compare_digest(expected, signature)
        if not ok:
            logger.warning("Invalid signature.")
        return ok
    except Exception:
        logger.exception("Exception validating signature")
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
        logger.exception("Error extracting text from message")
        return ""

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "message": "Vicky Bot funcionando", "deploy_sha": DEPLOY_SHA}), 200

@app.route("/webhook", methods=["GET"])
def webhook_verify():
    mode      = request.args.get("hub.mode", "")
    token     = request.args.get("hub.verify_token", "")
    challenge = request.args.get("hub.challenge", "")
    if mode == "subscribe" and token and token == VERIFY_TOKEN:
        logger.info("WEBHOOK VERIFY OK")
        return Response(challenge, status=200, content_type="text/plain")
    logger.warning("WEBHOOK VERIFY FAIL")
    return Response("Forbidden", status=403, content_type="text/plain")

@app.route("/webhook", methods=["POST"])
def webhook_receive():
    if not _valid_signature(request):
        return jsonify({"ok": False, "error": "Invalid signature"}), 403

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        logger.warning("EMPTY OR NON-DICT PAYLOAD")
        return jsonify({"ok": False, "error": "empty payload"}), 200

    try:
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value") or {}
                if value.get("statuses"):
                    continue
                for msg in value.get("messages", []) or []:
                    wa_from = msg.get("from", "")
                    wa_id   = msg.get("id", "")
                    if not wa_from or not wa_id:
                        logger.warning("MSG without from/id")
                        continue

                    text_in = _extract_text_from_message(msg).strip()
                    logger.info("BRANCH_DECISION | from=%s | text='%s'", wa_from, (text_in[:120] if text_in else ""))

                    reply = None
                    try:
                        if text_in.lower() == "menu" or text_in.isdigit():
                            logger.info("BRANCH=ROUTER")
                            reply = route_message(wa_id=wa_id, wa_e164_no_plus=wa_from, text_in=text_in)
                        else:
                            logger.info("BRANCH=GPT")
                            reply = ask_gpt(text_in)
                    except Exception:
                        logger.exception("Error building reply for from=%s", wa_from)
                        reply = "Lo siento, tuve un problema procesando tu mensaje."

                    if reply:
                        try:
                            send_whatsapp_message(wa_from, reply)
                        except Exception:
                            logger.exception("Error sending reply to %s", wa_from)

                        try:
                            if ADVISOR_NUMBER and ADVISOR_NUMBER != wa_from and "Notifiqué a Christian" in reply:
                                notify_text = f"Notificacion de {wa_from}\nUltimo mensaje:\n{(text_in or '[sin texto]')}"
                                send_whatsapp_message(ADVISOR_NUMBER, notify_text)
                        except Exception:
                            logger.exception("Error notifying advisor")
    except Exception:
        logger.exception("Unhandled error in webhook handler")

    return jsonify({"ok": True}), 200

@app.route("/send_test", methods=["GET"])
def send_test():
    if not ADVISOR_NUMBER:
        return jsonify({"ok": False, "error": "ADVISOR_NUMBER no configurado"}), 200
    try:
        send_whatsapp_message(ADVISOR_NUMBER, "Mensaje de prueba desde Vicky Bot")
        return jsonify({"ok": True}), 200
    except Exception:
        logger.exception("Error sending test message")
        return jsonify({"ok": False, "error": "Fallo al enviar mensaje de prueba"}), 200

@app.route("/gpt_test", methods=["GET"])
def gpt_test():
    try:
        respuesta = ask_gpt("Dame un consejo financiero para un trabajador en Mexico")
        return jsonify({"ok": True, "deploy_sha": DEPLOY_SHA, "respuesta": respuesta}), 200
    except Exception as e:
        logger.exception("GPT_TEST failed")
        return jsonify({"ok": False, "deploy_sha": DEPLOY_SHA, "error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)

