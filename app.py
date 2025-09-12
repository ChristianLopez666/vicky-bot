import os
import hmac
import hashlib
import logging
from typing import Any, Dict

from flask import Flask, request, jsonify, Response, Request
from dotenv import load_dotenv

# =======================
# Carga de entorno
# =======================
load_dotenv(override=True)

# Módulos del proyecto
from config_env import VERIFY_TOKEN, ADVISOR_NUMBER, LOG_LEVEL
from integrations_gpt import send_whatsapp_message
from core_router import route_message

# =======================
# Logging y config
# =======================
logging.basicConfig(
    level=getattr(logging, (LOG_LEVEL or "INFO").upper(), logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("vicky")

META_APP_SECRET = os.getenv("META_APP_SECRET", "")
if not META_APP_SECRET:
    log.warning("META_APP_SECRET vacío. Webhook sin validación de firma (menos seguro).")

app = Flask(__name__)


# =======================
# Utilidades
# =======================
def _valid_signature(req: Request) -> bool:
    if not META_APP_SECRET:
        return True
    signature = req.headers.get("X-Hub-Signature-256", "")
    expected = "sha256=" + hmac.new(
        META_APP_SECRET.encode("utf-8"), req.data, hashlib.sha256
    ).hexdigest()
    if not signature or not hmac.compare_digest(signature, expected):
        log.warning("Firma inválida en webhook.")
        return False
    return True


def _extract_text_from_message(msg: Dict[str, Any]) -> str:
    mtype = msg.get("type", "text")
    if mtype == "text":
        return (msg.get("text") or {}).get("body", "").strip()
    if mtype == "interactive":
        inter = msg.get("interactive", {}) or {}
        return (
            (inter.get("button_reply") or {}).get("title")
            or (inter.get("list_reply") or {}).get("title")
            or ""
        ).strip()
    return f"[{mtype}]"


# =======================
# Endpoints
# =======================
@app.get("/health")
def health() -> Response:
    return jsonify({"status": "ok", "message": "Vicky Bot funcionando"}), 200


@app.get("/webhook")
def webhook_verify() -> Response:
    mode = request.args.get("hub.mode", "")
    token = request.args.get("hub.verify_token", "")
    challenge = request.args.get("hub.challenge", "")
    if mode == "subscribe" and token == (VERIFY_TOKEN or "") and challenge:
        return Response(challenge, status=200, mimetype="text/plain")
    return Response("Verification failed", status=403)


@app.post("/webhook")
def webhook_receive() -> Response:
    if not _valid_signature(request):
        return Response(status=403)

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        log.warning("Payload vacío o inválido")
        return jsonify({"ok": False, "error": "Payload vacío"}), 200

    log.info("Incoming WA payload: %s", str(data)[:1000])

    if not data.get("entry"):
        return jsonify({"ignored": True}), 200

    try:
        for entry in data.get("entry", []) or []:
            if not isinstance(entry, dict):
                continue

            for change in entry.get("changes", []) or []:
                value = (change.get("value") or {})
                if not isinstance(value, dict):
                    continue

                if value.get("statuses"):
                    continue

                messages = value.get("messages") or []
                if not isinstance(messages, list) or not messages:
                    log.debug("Webhook sin 'messages' procesables")
                    continue

                for msg in messages:
                    if not isinstance(msg, dict):
                        continue

                    wa_from = msg.get("from", "")
                    wa_msg_id = msg.get("id", "")
                    if not wa_from or not wa_msg_id:
                        log.warning("Mensaje inválido sin remitente o id")
                        continue

                    text_in = _extract_text_from_message(msg)

                    try:
                        reply = route_message(
                            wa_id=wa_msg_id,
                            wa_e164_no_plus=wa_from,
                            text_in=text_in,
                        )
                    except Exception:
                        log.exception("Error en route_message para %s", wa_from)
                        reply = None

                    if reply:
                        try:
                            send_whatsapp_message(wa_from, reply)

                            if (
                                ADVISOR_NUMBER
                                and ADVISOR_NUMBER != wa_from
                                and isinstance(reply, str)
                                and "Notifiqué a Christian" in reply
                            ):
                                notify = (
                                    "📣 Nuevo contacto desde Vicky\n"
                                    f"📱 Número: {wa_from}\n"
                                    f"💬 Mensaje: {text_in}"
                                )
                                send_whatsapp_message(ADVISOR_NUMBER, notify)
                        except Exception:
                            log.exception("Error enviando respuesta a %s", wa_from)

        return jsonify({"ok": True}), 200

    except Exception as e:
        log.exception("Error procesando webhook")
        return jsonify({"ok": False, "error": str(e)}), 200


@app.get("/send_test")
def send_test() -> Response:
    to = ADVISOR_NUMBER or "5216682478005"
    try:
        send_whatsapp_message(to, "🚀 Prueba directa desde Vicky Bot (Render/ENV).")
        return jsonify({"ok": True, "to": to}), 200
    except Exception as e:
        log.exception("Fallo en send_test")
        return jsonify({"ok": False, "error": str(e)}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)
 str(e)}), 200