# config_env.py
import os
from dotenv import load_dotenv

load_dotenv()


def _get(name: str, default: str = "") -> str:
    return (os.getenv(name, default) or "").strip()


def get_env_str(name: str, default: str = "") -> str:
    """Obtiene una variable de entorno como string, con default seguro."""
    return _get(name, default)


def get_deploy_sha() -> str:
    """Compatibilidad con módulos que esperan este helper."""
    return _get("RENDER_GIT_COMMIT") or _get("DEPLOY_SHA")


def get_graph_base_url() -> str:
    """Compatibilidad con módulos que esperan este helper."""
    version = (_get("WA_API_VERSION") or "v20.0").lower().strip()
    return f"https://graph.facebook.com/{version}"


# WhatsApp Cloud API
VERIFY_TOKEN = _get("VERIFY_TOKEN")
WHATSAPP_TOKEN = _get("WHATSAPP_TOKEN") or _get("META_TOKEN")
PHONE_NUMBER_ID = _get("PHONE_NUMBER_ID") or _get("WA_PHONE_ID") or _get("WA_PHONE_NUMBER_ID")
WA_API_VERSION = (_get("WA_API_VERSION") or "v20.0").lower().strip()

# Operación
LOG_LEVEL = _get("LOG_LEVEL", "INFO")
ADVISOR_NUMBER = _get("ADVISOR_NUMBER") or _get("NOTIFICATION_NUMBER")

# GPT (opcional)
OPENAI_API_KEY = _get("OPENAI_API_KEY")
GPT_MODEL = _get("GPT_MODEL", "gpt-4o-mini")

# Google Sheets (opcional)
GOOGLE_SHEET_ID = _get("GOOGLE_SHEET_ID") or _get("SHEET_ID_PROSPECTOS")
GOOGLE_CREDENTIALS_JSON = _get("GOOGLE_CREDENTIALS_JSON")
