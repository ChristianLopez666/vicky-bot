
# 🚀 Vicky Bot - app.py optimizado con endpoint /ext/send-promo
# Incluye: webhook, GPT, manejo de medios, SECOM y promo sender.

import os
import logging
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# Cargar variables de entorno
load_dotenv()

# Configuración de logging
logging.basicConfig(level=logging.INFO)
app = Flask(__name__)

# Variables de entorno
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
WHATSAPP_TOKEN = os.getenv("META_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
ADVISOR_NUMBER = os.getenv("ADVISOR_NUMBER", "5216682478005")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# ================= FUNCIONES UTILITARIAS =================
def vx_get_env(name, default=None):
    return os.getenv(name, default)

def vx_normalize_phone(raw):
    import re
    if not raw:
        return ""
    phone = re.sub(r"[^\d]", "", str(raw))
    phone = re.sub(r"^(52|521)", "", phone)
    return phone[-10:] if len(phone) >= 10 else phone

def vx_last10(phone):
    return vx_normalize_phone(phone)

# ================= CLIENTE WHATSAPP =================
def vx_wa_send_text(to_e164: str, body: str):
    token = vx_get_env("META_TOKEN")
    phone_id = vx_get_env("WABA_PHONE_ID") or vx_get_env("PHONE_NUMBER_ID")
    if not token or not phone_id or not to_e164:
        logging.warning("vx_wa_send_text: falta config")
        return False
    url = f"https://graph.facebook.com/v20.0/{phone_id}/messages"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "to": to_e164,
        "type": "text",
        "text": {"body": body}
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=9)
        logging.info(f"vx_wa_send_text: {resp.status_code} {resp.text[:160]}")
        return resp.status_code == 200
    except Exception as e:
        logging.error(f"vx_wa_send_text error: {e}")
        return False

# ================= RUTAS =================

@app.get("/ext/health")
def vx_ext_health():
    return jsonify({"status": "ok"})

@app.post("/ext/test-send")
def vx_ext_test_send():
    try:
        data = request.get_json(force=True, silent=True)
        to = data.get("to")
        text = data.get("text")
        ok = vx_wa_send_text(to, text)
        return jsonify({"ok": ok}), 200
    except Exception as e:
        logging.error(f"vx_ext_test_send error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 200

# 🚀 NUEVO ENDPOINT: enviar promo en segundo plano
@app.post("/ext/send-promo")
def vx_ext_send_promo():
    try:
        data = request.get_json(force=True, silent=True)
        to = data.get("to")
        text = data.get("text")
        if not to or not text:
            return jsonify({"ok": False, "error": "Faltan parámetros"}), 400

        import threading
        def worker():
            vx_wa_send_text(to, text)

        threading.Thread(target=worker, daemon=True).start()
        return jsonify({"ok": True, "msg": "Envío en proceso"}), 200
    except Exception as e:
        logging.error(f"vx_ext_send_promo error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 200

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
