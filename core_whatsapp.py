# core_whatsapp.py
import time
import requests
import config_env as cfg
from typing import Dict, Any, Optional
from utils_logger import get_logger

log = get_logger("whatsapp")

# 👇 Importación a prueba de fallos (usa defaults si algo falta)
WA_API_VERSION  = (getattr(cfg, "WA_API_VERSION", "v20.0") or "v20.0").lower().strip()
WHATSAPP_TOKEN  = getattr(cfg, "WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID = (getattr(cfg, "PHONE_NUMBER_ID", "") 
                   or getattr(cfg, "WA_PHONE_ID", "") 
                   or getattr(cfg, "WA_PHONE_NUMBER_ID", ""))

API_BASE = f"https://graph.facebook.com/{WA_API_VERSION}".rstrip("/")
SEND_URL = f"{API_BASE}/{PHONE_NUMBER_ID}/messages"
HEADERS = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}

def _post(url: str, payload: Dict[str, Any], timeout: int = 10, retries: int = 2) -> Dict[str, Any]:
    last_err: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            r = requests.post(url, headers=HEADERS, json=payload, timeout=timeout)
            if r.status_code >= 500:
                raise RuntimeError(f"WA 5xx: {r.status_code} {r.text}")
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = e
            log.warning("POST intento %s falló: %s", attempt + 1, e)
            time.sleep(1.2 * (attempt + 1))
    log.error("POST falló definitivamente: %s | payload=%s", last_err, str(payload)[:500])
    return {"error": str(last_err)}

def send_text(to: str, body: str) -> Dict[str, Any]:
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"preview_url": False, "body": body[:4096]},
    }
    return _post(SEND_URL, payload)
