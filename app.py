# app.py — Vicky SECOM CORE ESTABLE v1
# Python 3.11+
# Alcance: WhatsApp Cloud API v20+, envíos confiables (texto + plantillas)
# Nota: Sin workers, sin colas, sin threads. Secuencial y legal.

from __future__ import annotations
import os, re, json, time, logging
from typing import Dict, Any, List, Optional

import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# ==========================
# Carga entorno + logging
# ==========================
load_dotenv()

META_TOKEN = os.getenv("META_TOKEN")
WABA_PHONE_ID = os.getenv("WABA_PHONE_ID")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
PORT = int(os.getenv("PORT", "5000"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("vicky-core")

app = Flask(__name__)

WPP_API_URL = f"https://graph.facebook.com/v20.0/{WABA_PHONE_ID}/messages" if WABA_PHONE_ID else None

# ==========================
# Utilidades
# ==========================
def _wpp_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {META_TOKEN}",
        "Content-Type": "application/json"
    }

def _normalize_e164_mx(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 10:
        return "521" + digits
    if digits.startswith("52") and len(digits) == 12:
        return digits
    if digits.startswith("1") and len(digits) >= 11:
        return "52" + digits[-10:]
    return phone  # Meta rechazará con error claro

def _should_retry(status: int) -> bool:
    return status == 429 or 500 <= status < 600

# ==========================
# Envíos WhatsApp
# ==========================
def send_message(to: str, text: str) -> bool:
    if not (META_TOKEN and WPP_API_URL):
        log.error("❌ WhatsApp no configurado")
        return False

    to_e164 = _normalize_e164_mx(to)
    payload = {
        "messaging_product": "whatsapp",
        "to": to_e164,
        "type": "text",
        "text": {"body": text[:4096]}
    }

    for attempt in range(3):
        try:
            log.info(f"📤 Mensaje a {to_e164} (intento {attempt+1})")
            resp = requests.post(WPP_API_URL, headers=_wpp_headers(), json=payload, timeout=30)
            if resp.status_code == 200:
                mid = (resp.json().get("messages") or [{}])[0].get("id", "unknown")
                log.info(f"✅ Mensaje enviado | ID: {mid}")
                return True

            err = resp.json() if resp.text else {}
            log.warning(f"⚠️ Fallo {resp.status_code}: {err}")
            if resp.status_code == 429:
                wait = min(int(resp.headers.get("Retry-After", "300")), 600)
                time.sleep(wait)
                continue
            if _should_retry(resp.status_code) and attempt < 2:
                time.sleep(2 ** (attempt + 1))
                continue
            return False
        except requests.exceptions.Timeout:
            if attempt < 2:
                time.sleep(2 ** (attempt + 1))
                continue
            return False
        except Exception:
            if attempt < 2:
                time.sleep(2 ** (attempt + 1))
                continue
            return False
    return False

def send_template_message(to: str, template_name: str, params: List[str]) -> bool:
    if not (META_TOKEN and WPP_API_URL):
        log.error("❌ WhatsApp no configurado")
        return False

    to_e164 = _normalize_e164_mx(to)

    parameters = []
    for p in (params or []):
        if p is not None and str(p).strip():
            parameters.append({"type": "text", "text": str(p).strip()[:32768]})

    components = []
    if parameters:
        components.append({"type": "body", "parameters": parameters})

    payload = {
        "messaging_product": "whatsapp",
        "to": to_e164,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": "es_MX"},
            "components": components
        }
    }

    for attempt in range(3):
        try:
            log.info(f"📤 Plantilla '{template_name}' a {to_e164} (intento {attempt+1})")
            resp = requests.post(WPP_API_URL, headers=_wpp_headers(), json=payload, timeout=30)
            if resp.status_code == 200:
                mid = (resp.json().get("messages") or [{}])[0].get("id", "unknown")
                log.info(f"✅ Plantilla enviada | ID: {mid}")
                return True

            err = resp.json() if resp.text else {}
            log.warning(f"⚠️ Fallo {resp.status_code}: {err}")
            if resp.status_code == 429:
                wait = min(int(resp.headers.get("Retry-After", "300")), 600)
                time.sleep(wait)
                continue
            if resp.status_code == 400 and "parameter" in str(err).lower():
                return False
            if _should_retry(resp.status_code) and attempt < 2:
                time.sleep(2 ** (attempt + 1))
                continue
            return False
        except requests.exceptions.Timeout:
            if attempt < 2:
                time.sleep(2 ** (attempt + 1))
                continue
            return False
        except Exception:
            if attempt < 2:
                time.sleep(2 ** (attempt + 1))
                continue
            return False
    return False

# ==========================
# Webhook verify
# ==========================
@app.get("/webhook")
def webhook_verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge", "")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        log.info("✅ Webhook verificado")
        return challenge, 200
    return "Error", 403

# ==========================
# Webhook receive (mínimo)
# ==========================
@app.post("/webhook")
def webhook_receive():
    payload = request.get_json(force=True, silent=True) or {}
    entry = payload.get("entry", [{}])[0]
    changes = entry.get("changes", [{}])[0]
    value = changes.get("value", {})
    messages = value.get("messages", [])
    if not messages:
        return jsonify({"ok": True}), 200

    msg = messages[0]
    phone = msg.get("from")
    if not phone:
        return jsonify({"ok": True}), 200

    if msg.get("type") == "text":
        text = msg.get("text", {}).get("body", "")
        send_message(phone, f"Recibido: {text}")
    return jsonify({"ok": True}), 200

# ==========================
# Endpoints de prueba
# ==========================
@app.get("/ext/health")
def ext_health():
    return jsonify({
        "status": "ok",
        "whatsapp_configured": bool(META_TOKEN and WABA_PHONE_ID)
    }), 200

@app.post("/ext/test-send")
def ext_test_send():
    data = request.get_json(force=True) or {}
    to = data.get("to", "")
    text = data.get("text", "")
    if not to or not text:
        return jsonify({"ok": False, "error": "Faltan 'to' o 'text'"}), 400
    ok = send_message(to, text)
    return jsonify({"ok": bool(ok)}), 200

@app.post("/ext/send-promo")
def ext_send_promo():
    """
    Envío SECUENCIAL y CONFIABLE.
    Body:
    {
      "items": [
        {"to": "6681234567", "text": "Hola"},
        {"to": "6681234567", "template": "mi_plantilla", "params": ["Juan"]}
      ]
    }
    """
    data = request.get_json(force=True) or {}
    items = data.get("items", [])
    if not isinstance(items, list) or not items:
        return jsonify({"ok": False, "error": "items inválidos"}), 400

    results = []
    for i, item in enumerate(items, 1):
        to = item.get("to", "")
        text = item.get("text", "")
        tpl = item.get("template", "")
        params = item.get("params", [])
        if not to or (not text and not tpl):
            results.append({"index": i, "ok": False})
            continue

        ok = send_template_message(to, tpl, params) if tpl else send_message(to, text)
        results.append({"index": i, "ok": ok})
        # pausa corta y segura
        time.sleep(1)

    return jsonify({"ok": True, "results": results}), 200

# ==========================
# Run
# ==========================
if __name__ == "__main__":
    log.info(f"🚀 Vicky CORE en puerto {PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False)
