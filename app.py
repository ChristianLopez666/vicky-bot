import os
import hmac
import hashlib
import logging
from typing import Any, Dict

from flask import Flask, request, jsonify, Response, Request
from dotenv import load_dotenv

# =======================
# Carga de entorno PRIMERO
# =======================
load_dotenv(override=True)

# Módulos del proyecto (usan variables ya cargadas)
from config_env import VERIFY_TOKEN, ADVISOR_NUMBER, LOG_LEVEL
from integrations_gpt import send_whatsapp_message   # ✅ corregido: ya no usamos core_whatsapp
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
    """Valida X-Hub-Signature-256 si META_APP_SECRET está configurado."""
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
    """Obtiene texto de un mensaje WA (text/interactive) o devuelve [tipo]."""
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
    """Healthcheck simple."""
    return jsonify({"status": "ok", "message": "Vicky Bot funcionando"}), 200


@app.get("/webhook")
def webhook_verify() -> Response:
    """Verificación de webhook (GET). Devuelve hub.challenge en texto plano."""
    mode = request.args.get("hub.mode", "")
    token = request.args.get("hub.verify_token", "")
    challenge = request.args.get("hub.challenge", "")
    if mode == "subscribe" and token == (VERIFY_TOKEN or "") and challenge:
        return Response(challenge, status=200, mimetype="text/plain")
    return Response("Verification failed", status=403)


@app.post("/webhook")
def webhook_receive() -> Response:
    """Recepción de eventos de WhatsApp (POST)."""
    if not _valid_signature(request):
        return Response(status=403)

    data = request.get_json(silent=True) or {}
    log.info("Incoming WA payload: %s", str(data)[:1000])

    if data.get("object") != "whatsapp_business_account":
        return jsonify({"ignored": True}), 200

    try:
        for entry in data.get("entry", []):
            for change in (entry.get("changes") or []):
                value = change.get("value", {}) or {}

                # Ignorar notificaciones de estatus (delivered/read/etc.)
                if value.get("statuses"):
                    continue

                for msg in value.get("messages", []) or []:
                    wa_from = msg.get("from", "")            # E164 sin '+'
                    wa_msg_id = msg.get("id", "")
                    text_in = _extract_text_from_message(msg)

                    # 3) Router principal (GPT / lógica de negocio)
                    reply = route_message(
                        wa_id=wa_msg_id,                 # id del mensaje
                        wa_e164_no_plus=wa_from,         # número del usuario
                        text_in=text_in,
                    )

                    # 4) Envío a usuario
                    send_whatsapp_message(wa_from, reply)

                    # 5) Notificación al asesor (si aplica)
                    if (
                        ADVISOR_NUMBER
                        and ADVISOR_NUMBER != wa_from
                        and "Notifiqué a Christian" in (reply or "")
                    ):
                        notify = (
                            "📣 Nuevo contacto desde Vicky\n"
                            f"📱 Número: {wa_from}\n"
                            f"💬 Mensaje: {text_in}"
                        )
                        send_whatsapp_message(ADVISOR_NUMBER, notify)

        return jsonify({"ok": True}), 200

    except Exception as e:
        log.exception("Error procesando webhook")
        return jsonify({"ok": False, "error": str(e)}), 200


@app.get("/send_test")
def send_test() -> Response:
    """Envía un mensaje de prueba al asesor."""
    to = ADVISOR_NUMBER or "5216682478005"
    try:
        send_whatsapp_message(to, "🚀 Prueba directa desde Vicky Bot (Render/ENV).")
        return jsonify({"ok": True, "to": to}), 200
    except Exception as e:
        log.exception("Fallo en send_test")
        return jsonify({"ok": False, "error": str(e)}), 200


# =======================
# Main
# =======================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)

