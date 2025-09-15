import json
import os
import sys
import time
import typing as t
from collections import OrderedDict

from flask import Flask, jsonify, request
import httpx

from config_env import (
    get_env_str,
    get_deploy_sha,
    get_graph_base_url,
)
from core_router import is_menu_message, menu_text, route_message
from integrations_gpt import ask_gpt

# -----------------------------------------------------------------------------
# Configuracion
# -----------------------------------------------------------------------------
APP_NAME = "vicky-bot"
DEPLOY_SHA = get_deploy_sha()

GRAPH_API_VERSION = get_env_str("GRAPH_API_VERSION", default="v20.0")
BASE_URL = get_graph_base_url(GRAPH_API_VERSION)
PHONE_NUMBER_ID = get_env_str("PHONE_NUMBER_ID", required=True)
WHATSAPP_TOKEN = get_env_str("WHATSAPP_TOKEN", required=True)
VERIFY_TOKEN = get_env_str("VERIFY_TOKEN", required=True)
ADVISOR_NUMBER = get_env_str("ADVISOR_NUMBER", required=False)

FORCE_BRANCH = get_env_str("FORCE_BRANCH", default="").upper().strip()

# -----------------------------------------------------------------------------
# Utilidades
# -----------------------------------------------------------------------------
def log_json(level: str, msg: str, **fields: t.Any) -> None:
    row = {"ts": int(time.time()), "level": level, "app": APP_NAME, "msg": msg, "deploy_sha": DEPLOY_SHA}
    row.update(fields)
    sys.stdout.write(json.dumps(row, ensure_ascii=True) + "\n")
    sys.stdout.flush()


class LruSeen:
    """Conjunto LRU simple para idempotencia (wamid)."""
    def __init__(self, capacity: int = 1000) -> None:
        self.capacity = capacity
        self.data: "OrderedDict[str, None]" = OrderedDict()

    def add(self, key: str) -> bool:
        # True si se agrego nuevo; False si ya existia
        if key in self.data:
            self.data.move_to_end(key, last=True)
            return False
        self.data[key] = None
        self.data.move_to_end(key, last=True)
        if len(self.data) > self.capacity:
            self.data.popitem(last=False)
        return True


seen_wamids = LruSeen(capacity=1000)

# -----------------------------------------------------------------------------
# Cliente HTTP
# -----------------------------------------------------------------------------
def _headers() -> dict:
    return {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}

async def _post_json(client: httpx.AsyncClient, url: str, payload: dict) -> httpx.Response:
    # reintentos simples
    backoff = [0.5, 1.0, 2.0]
    for i, delay in enumerate(backoff):
        try:
            return await client.post(url, json=payload, headers=_headers(), timeout=10.0)
        except Exception as e:
            if i == len(backoff) - 1:
                raise
            time.sleep(delay)

def send_whatsapp_message(to_number: str, text: str) -> bool:
    url = f"{BASE_URL}/{PHONE_NUMBER_ID}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_number,
        "type": "text",
        "text": {"preview_url": False, "body": text},
    }
    try:
        with httpx.Client(timeout=10.0) as client:
            res = client.post(url, headers=_headers(), json=payload)
            ok = res.status_code in (200, 201)
            if not ok:
                log_json("warn", "whatsapp_send_failed", status=res.status_code, body=res.text[:500])
            return ok
    except Exception as e:
        log_json("error", "whatsapp_send_exception", err=str(e))
        return False

def notify_advisor(wa_id: str, user_text: str) -> None:
    if not ADVISOR_NUMBER:
        log_json("info", "advisor_notify_skipped", reason="no_advisor_number")
        return
    text = f"Nuevo contacto de WhatsApp: {wa_id}. Mensaje: {user_text}"
    ok = send_whatsapp_message(ADVISOR_NUMBER, text)
    log_json("info", "advisor_notified", ok=ok)

# -----------------------------------------------------------------------------
# Parseo Webhook
# -----------------------------------------------------------------------------
def extract_message(payload: dict) -> t.Tuple[str, str, str]:
    """
    Devuelve (wamid, wa_id, text). Lanza ValueError si no se pudo extraer.
    """
    try:
        entry = payload.get("entry", [])[0]
        change = entry.get("changes", [])[0]
        value = change.get("value", {})
        messages = value.get("messages", [])
        if not messages:
            raise ValueError("no_messages")
        msg = messages[0]
        wamid = msg.get("id") or msg.get("wamid") or ""
        wa_id = msg.get("from") or ""
        # Tipos posibles: text, button, interactive, etc.
        text = ""
        if msg.get("type") == "text":
            text = (msg.get("text") or {}).get("body", "")
        elif msg.get("type") == "button":
            text = (msg.get("button") or {}).get("text", "")
        elif msg.get("type") == "interactive":
            interactive = msg.get("interactive") or {}
            if "button_reply" in interactive:
                text = (interactive["button_reply"] or {}).get("title", "")
            elif "list_reply" in interactive:
                text = (interactive["list_reply"] or {}).get("title", "")
        if not text:
            # Fallback generico
            text = ""
        if not wamid or not wa_id:
            raise ValueError("missing_ids")
        return wamid, wa_id, text.strip()
    except Exception:
        raise ValueError("bad_payload")

# -----------------------------------------------------------------------------
# Flask app
# -----------------------------------------------------------------------------
app = Flask(__name__)

@app.get("/health")
def health() -> t.Any:
    return jsonify({"status": "ok", "message": "Vicky Bot funcionando", "deploy_sha": DEPLOY_SHA})

@app.get("/gpt_test")
def gpt_test() -> t.Any:
    try:
        out = ask_gpt("Responde solo: OK")
        return jsonify({"ok": True, "gpt": out})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.get("/webhook")
def webhook_verify() -> t.Any:
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN and challenge:
        log_json("info", "webhook_verified")
        return challenge, 200
    log_json("warn", "webhook_verify_failed", mode=mode, token_ok=(token == VERIFY_TOKEN))
    return "forbidden", 403

@app.post("/webhook")
def webhook_receive() -> t.Any:
    try:
        payload = request.get_json(force=True, silent=True) or {}
        log_json("info", "webhook_received", size=len(json.dumps(payload)))
        try:
            wamid, wa_id, text = extract_message(payload)
        except ValueError as e:
            log_json("warn", "no_message_extracted", reason=str(e))
            return jsonify({"ok": True}), 200

        # Idempotencia
        if not seen_wamids.add(wamid):
            log_json("info", "duplicate_wamid", wamid=wamid)
            return jsonify({"ok": True}), 200

        # Branching
        decision = FORCE_BRANCH
        if not decision:
            decision = "ROUTER" if is_menu_message(text) else "GPT"

        reply = ""
        try:
            if decision == "ROUTER":
                # Si el usuario pide menu
                if text.lower().strip() == "menu":
                    reply = menu_text()
                else:
                    reply = route_message(text)
                    # Opcion 8: notificar a asesor
                    if text.strip() == "8":
                        notify_advisor(wa_id, text)
            else:
                # GPT por defecto
                reply = ask_gpt(text or "Hola")

        except Exception as e:
            log_json("error", "branch_exception", err=str(e))
            reply = "Lo siento, tuve un problema procesando tu mensaje."

        # Enviar respuesta
        ok = send_whatsapp_message(wa_id, reply)
        log_json("info", "reply_sent", ok=ok, wa_id=wa_id, wamid=wamid, branch=decision)
        return jsonify({"ok": True}), 200

    except Exception as e:
        log_json("error", "webhook_exception", err=str(e))
        # Fallback generico
        return jsonify({"ok": True}), 200


if __name__ == "__main__":
    # Desarrollo local
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
