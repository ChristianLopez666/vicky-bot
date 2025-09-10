import os
import hmac
import hashlib
import logging
import requests
from flask import Flask, request, jsonify, Response
from dotenv import load_dotenv

# ========= Cargar variables desde .env =========
load_dotenv()

# ========= Entorno =========
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "")
META_TOKEN = os.getenv("META_TOKEN", "")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "")
WA_API_VERSION = os.getenv("WA_API_VERSION", "v20.0")
APP_SECRET = os.getenv("META_APP_SECRET", "")
ADVISOR_NOTIFY_NUMBER = os.getenv("ADVISOR_NOTIFY_NUMBER", "")
PORT = int(os.getenv("PORT", "5000"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("vicky")

app = Flask(__name__)

# ========= WhatsApp =========
def send_wa_text(to_e164: str, body: str):
    if not (META_TOKEN and PHONE_NUMBER_ID):
        log.error("WhatsApp no configurado: faltan META_TOKEN o PHONE_NUMBER_ID.")
        return {"error": "Faltan credenciales"}

    url = f"https://graph.facebook.com/{WA_API_VERSION}/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {META_TOKEN}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "to": to_e164,
        "type": "text",
        "text": {"preview_url": False, "body": body},
    }

    r = requests.post(url, headers=headers, json=payload, timeout=20)
    if r.status_code >= 400:
        log.error("WA send error %s: %s", r.status_code, r.text)
    return r.json()

# ========= Handlers =========
@app.get("/")
def index():
    return "Vicky Bot Ready", 200

@app.get("/health")
def health():
    flags = {
        "whatsapp": bool(META_TOKEN and PHONE_NUMBER_ID),
        "advisor_notify": bool(ADVISOR_NOTIFY_NUMBER),
    }
    return jsonify(flags), 200

@app.get("/webhook")
def webhook_get():
    mode = request.args.get("hub.mode", "")
    token = request.args.get("hub.verify_token", "")
    challenge = request.args.get("hub.challenge", "")
    if mode == "subscribe" and token == VERIFY_TOKEN and challenge != "":
        return Response(challenge, status=200)
    return Response("Forbidden", status=403)

@app.post("/webhook")
def webhook_post():
    # Firma opcional
    if APP_SECRET:
        signature = request.headers.get("X-Hub-Signature-256", "")
        expected = "sha256=" + hmac.new(APP_SECRET.encode(), request.data, hashlib.sha256).hexdigest()
        if not signature or not hmac.compare_digest(signature, expected):
            log.warning("Invalid signature")
            return Response(status=403)

    data = request.get_json(silent=True) or {}
    if data.get("object") != "whatsapp_business_account":
        return Response(status=200)

    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for message in value.get("messages", []) or []:
                wa_id = message.get("from", "")
                text = (message.get("text") or {}).get("body", "")
                log.info(f"Mensaje de {wa_id}: {text}")
                # Respuesta automática (menú básico)
                send_wa_text(
                    wa_id,
                    "Hola 👋 Soy Vicky Bot. Responde con:\n"
                    "1) Seguro de auto\n"
                    "2) Salud / Vida\n"
                    "3) Pensión IMSS\n"
                    "4) Crédito\n"
                    "5) Hablar con Christian"
                )

    return Response(status=200)

# ========= Endpoint de prueba =========
@app.get("/send_test")
def send_test():
    return send_wa_text("5216682478005", "🚀 Prueba directa con token desde .env"), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False)
