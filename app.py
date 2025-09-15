"""
Main Flask application for Vicky WhatsApp bot.

Requirements met:
- Flask app with endpoints /health, /gpt_test, /webhook (GET and POST)
- JSON structured logs per line (UTC timestamps)
- Idempotency via in-memory LRU cache
- Integration with WhatsApp Cloud API via httpx with retries and timeout
- Uses integrations_gpt.ask_gpt for GPT responses
- Uses core_router for menu routing
- Reads configuration from config_env
"""

import os
import time
import json
import logging
from typing import Optional, Tuple, Dict, Any
from collections import OrderedDict, defaultdict

from flask import Flask, request, Response, jsonify

import httpx

from config_env import (
    get_env_str,
    get_deploy_sha,
    get_graph_base_url,
)
from core_router import is_menu_message, route_message, menu_text
from integrations_gpt import ask_gpt

# Deploy sha
DEPLOY_SHA = get_deploy_sha()

# Configure logger to output JSON lines
logger = logging.getLogger("vicky")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setLevel(logging.INFO)


class JSONLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # Basic JSON fields
        log_record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname,
            "message": record.getMessage(),
            "deploy_sha": DEPLOY_SHA,
        }
        # Allow extra data passed via record.args if dict
        try:
            if isinstance(record.args, dict):
                # Avoid overwriting core fields
                for k, v in record.args.items():
                    if k not in log_record:
                        log_record[k] = v
        except Exception:
            pass
        return json.dumps(log_record, ensure_ascii=True)


handler.setFormatter(JSONLineFormatter())
# Avoid duplicate handlers during re-imports
if not logger.handlers:
    logger.addHandler(handler)
else:
    logger.handlers = [handler]

# Flask app
app = Flask(__name__)

# Load environment variables used at runtime
WHATSAPP_TOKEN = get_env_str("WHATSAPP_TOKEN", default=None, required=False)
PHONE_NUMBER_ID = get_env_str("PHONE_NUMBER_ID", default=None, required=False)
VERIFY_TOKEN = get_env_str("VERIFY_TOKEN", default=None, required=False)
GRAPH_API_VERSION = get_env_str("GRAPH_API_VERSION", default="v20.0", required=False)
ADVISOR_NUMBER = get_env_str("ADVISOR_NUMBER", default=None, required=False)
FORCE_BRANCH = get_env_str("FORCE_BRANCH", default=None, required=False)

GRAPH_BASE = get_graph_base_url()

# HTTP client default settings
HTTP_TIMEOUT = 10.0  # seconds
HTTP_RETRIES = 3
HTTP_BACKOFF = 0.5  # seconds

# Idempotency cache
class IdempotencyCache:
    """Simple LRU cache for wamid idempotency with optional TTL."""

    def __init__(self, capacity: int = 1000, ttl_seconds: int = 24 * 3600):
        self.capacity = capacity
        self.ttl = ttl_seconds
        self.store: "OrderedDict[str, float]" = OrderedDict()

    def _evict_if_needed(self) -> None:
        while len(self.store) > self.capacity:
            self.store.popitem(last=False)

    def add(self, key: str) -> None:
        now = time.time()
        if key in self.store:
            # move to end
            self.store.move_to_end(key)
            self.store[key] = now
            return
        self.store[key] = now
        self._evict_if_needed()

    def exists(self, key: str) -> bool:
        now = time.time()
        if key in self.store:
            ts = self.store.get(key, 0)
            if self.ttl and (now - ts) > self.ttl:
                # expired
                try:
                    del self.store[key]
                except KeyError:
                    pass
                return False
            # move to end as recently used
            self.store.move_to_end(key)
            return True
        return False


idempotency = IdempotencyCache(capacity=1000, ttl_seconds=24 * 3600)

# Simple counters for metrics in memory
counters = defaultdict(int)

def log_event(level: str, event: str, extra: Optional[Dict[str, Any]] = None) -> None:
    payload = {"event": event, "branch": FORCE_BRANCH or "auto"}
    if extra:
        payload.update(extra)
    if level.upper() == "INFO":
        logger.info(event, payload)
    else:
        logger.error(event, payload)

def _extract_text_from_whatsapp_event(data: Dict[str, Any]) -> Optional[Tuple[str, str, str]]:
    """
    Extract wamid, wa_id and text from WhatsApp Cloud webhook payload.

    Returns tuple (wamid, wa_id, text) or None if no message text found.
    """
    try:
        entry = data.get("entry", [])
        if not entry:
            return None
        for e in entry:
            changes = e.get("changes", [])
            for ch in changes:
                value = ch.get("value", {})
                messages = value.get("messages", [])
                if not messages:
                    continue
                for m in messages:
                    # Only handle text messages
                    if "text" in m and isinstance(m.get("text"), dict):
                        wamid = m.get("id")
                        wa_id = m.get("from")
                        text = m.get("text", {}).get("body", "")
                        if text is None:
                            text = ""
                        # normalize to plain ASCII by ensuring JSON serialization will escape non-ascii
                        return (str(wamid), str(wa_id), str(text))
        return None
    except Exception as e:
        # log and return None to ignore non-standard payloads
        logger.error("extract_text_error", {"error": str(e)})
        return None

def send_whatsapp_message(to: str, text: str) -> Tuple[bool, Optional[str]]:
    """
    Send a text message via WhatsApp Cloud API.

    Returns (success, error_message).
    """
    if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
        err = "WHATSAPP_TOKEN or PHONE_NUMBER_ID not configured"
        logger.error("whatsapp_send_config_error", {"error": err})
        return False, err

    url = f"{GRAPH_BASE}/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text},
    }

    last_err = None
    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            with httpx.Client(timeout=HTTP_TIMEOUT) as client:
                resp = client.post(url, headers=headers, json=payload)
                if resp.status_code in (200, 201):
                    logger.info("whatsapp_message_sent", {"to": to, "status_code": resp.status_code})
                    return True, None
                else:
                    last_err = f"status={{resp.status_code}}, body={{resp.text}}"
                    logger.error("whatsapp_send_failed", {"to": to, "attempt": attempt, "error": last_err})
        except Exception as exc:
            last_err = str(exc)
            logger.error("whatsapp_send_exception", {"to": to, "attempt": attempt, "error": last_err})
        time.sleep(HTTP_BACKOFF * attempt)
    return False, last_err

@app.route("/health", methods=["GET"])
def health():
    """
    Health endpoint.
    """
    resp = {"status": "ok", "message": "Vicky Bot funcionando", "deploy_sha": DEPLOY_SHA}
    return jsonify(resp), 200

@app.route("/gpt_test", methods=["GET"])
def gpt_test():
    """
    Call GPT with a small prompt and return the result.
    """
    try:
        reply = ask_gpt("Responde solo: OK")
        return jsonify({"ok": True, "gpt_reply": reply}), 200
    except Exception as e:
        err = str(e)
        logger.error("gpt_test_error", {"error": err})
        return jsonify({"ok": False, "error": err}), 200

@app.route("/webhook", methods=["GET"])
def webhook_verify():
    """
    Verify webhook from Meta/WhatsApp.
    """
    hub_mode = request.args.get("hub.mode")
    hub_challenge = request.args.get("hub.challenge")
    hub_verify_token = request.args.get("hub.verify_token")
    if hub_mode == "subscribe" and hub_verify_token and VERIFY_TOKEN and hub_verify_token == VERIFY_TOKEN:
        logger.info("webhook_verified", {"mode": hub_mode})
        return Response(hub_challenge or "", status=200)
    logger.error("webhook_verify_failed", {"provided_token": bool(hub_verify_token)})
    return Response("Forbidden", status=403)

@app.route("/webhook", methods=["POST"])
def webhook_message():
    """
    Handle incoming WhatsApp webhook events.
    """
    try:
        data = request.get_json(silent=True)
        if not data:
            logger.error("webhook_no_json")
            return "", 200

        extracted = _extract_text_from_whatsapp_event(data)
        if not extracted:
            # nothing to do
            logger.info("webhook_no_message")
            return "", 200

        wamid, wa_id, text = extracted

        # idempotency
        if idempotency.exists(wamid):
            logger.info("duplicate_event_ignored", {"wamid": wamid})
            return "", 200
        idempotency.add(wamid)

        # decide branch
        branch = "GPT"
        if FORCE_BRANCH == "GPT":
            branch = "GPT"
        elif FORCE_BRANCH == "ROUTER":
            branch = "ROUTER"
        else:
            if is_menu_message(text):
                branch = "ROUTER"
            else:
                branch = "GPT"

        counters[f"processed_{{branch}}"] += 1

        logger.info(
            "route_message",
            {
                "branch": branch,
                "wa_id": wa_id,
                "wamid": wamid,
                "text": text,
                "counters": dict(counters),
            },
        )

        reply_text = ""
        if branch == "ROUTER":
            # Determine numeric option: if text is "menu", return menu
            normalized = text.strip().lower()
            if normalized == "menu":
                reply_text = menu_text()
            elif normalized.isdigit() and normalized in [str(i) for i in range(1, 9)]:
                reply_text = route_message(normalized)
                # If option 8, notify advisor
                if normalized == "8" and ADVISOR_NUMBER:
                    advisor_msg = f"El usuario {{wa_id}} solicita atencion de un asesor. Mensaje original: {{text}}"
                    # send notification in background-like manner (synchronous here)
                    ok, err = send_whatsapp_message(ADVISOR_NUMBER, advisor_msg)
                    if not ok:
                        logger.error("advisor_notify_failed", {"error": err, "advisor": ADVISOR_NUMBER})
                    else:
                        logger.info("advisor_notified", {"advisor": ADVISOR_NUMBER})
                elif normalized == "8" and not ADVISOR_NUMBER:
                    logger.warning("advisor_number_not_configured", {"option": "8"})
            else:
                # fallback to menu
                reply_text = menu_text()
        else:
            # GPT branch
            try:
                gpt_reply = ask_gpt(text)
                reply_text = gpt_reply
            except Exception as e:
                logger.error("gpt_call_failed", {"error": str(e)})
                reply_text = "Lo siento, tuve un problema procesando tu mensaje."

        # Ensure reply is not empty
        if not reply_text:
            reply_text = "Lo siento, no tengo una respuesta en este momento."

        # Send message back to user
        ok, err = send_whatsapp_message(wa_id, reply_text)
        if not ok:
            logger.error("send_reply_failed", {"wa_id": wa_id, "error": err})
            # Attempt fallback message text to user via logs only
            # Do not crash, respond 200 to webhook
        logger.info("message_processed", {"wamid": wamid, "wa_id": wa_id, "branch": branch})

        return "", 200
    except Exception as exc:
        # Global fallback
        logger.error("webhook_processing_exception", {"error": str(exc)})
        return "", 200


if __name__ == "__main__":
    # Only run when invoked directly for local debug
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))