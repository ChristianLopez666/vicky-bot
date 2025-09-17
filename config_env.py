import os

def _get(name: str, default: str = "") -> str:
    return os.getenv(name, default)

# FASE 1 – WhatsApp Cloud API
VERIFY_TOKEN     = _get("VERIFY_TOKEN")
# Acepta ambas convenciones para evitar errores de nombre:
WHATSAPP_TOKEN   = _get("WHATSAPP_TOKEN") or _get("META_TOKEN")
PHONE_NUMBER_ID  = _get("PHONE_NUMBER_ID") or _get("WA_PHONE_ID")
WA_API_VERSION   = (_get("WA_API_VERSION") or "v23.0").lower().strip()

# Notificación a asesor (opción 8)
ADVISOR_NUMBER   = _get("ADVISOR_NUMBER")  # ej. 5216682478005

# Logging
LOG_LEVEL        = _get("LOG_LEVEL", "INFO")

# (FASE 2 – opcional)
OPENAI_API_KEY   = _get("OPENAI_API_KEY")
GOOGLE_SHEET_ID  = _get("GOOGLE_SHEET_ID")
GOOGLE_CREDENTIALS_JSON = _get("GOOGLE_CREDENTIALS_JSON")
