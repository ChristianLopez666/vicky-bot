<<<<<<< HEAD
# config_env.py
import os
from dotenv import load_dotenv

load_dotenv()

def _get(name: str, default: str = "") -> str:
    return (os.getenv(name, default) or "").strip()

# WhatsApp Cloud API
VERIFY_TOKEN     = _get("VERIFY_TOKEN")
WHATSAPP_TOKEN   = _get("WHATSAPP_TOKEN") or _get("META_TOKEN")
PHONE_NUMBER_ID  = _get("PHONE_NUMBER_ID") or _get("WA_PHONE_ID") or _get("WA_PHONE_NUMBER_ID")
WA_API_VERSION   = (_get("WA_API_VERSION") or "v20.0").lower().strip()

# Operación
LOG_LEVEL        = _get("LOG_LEVEL", "INFO")
ADVISOR_NUMBER   = _get("ADVISOR_NUMBER") or _get("NOTIFICATION_NUMBER")

# GPT (opcional)
OPENAI_API_KEY   = _get("OPENAI_API_KEY")
GPT_MODEL        = _get("GPT_MODEL", "gpt-4o-mini")

# Google Sheets (opcional)
GOOGLE_SHEET_ID          = _get("GOOGLE_SHEET_ID") or _get("SHEET_ID_PROSPECTOS")
GOOGLE_CREDENTIALS_JSON  = _get("GOOGLE_CREDENTIALS_JSON")
=======
import os

def get_env_str(name: str, default: str = "") -> str:
    """
    Obtiene el valor de una variable de entorno como string.
    Si no existe, devuelve el valor por defecto.
    """
    return os.getenv(name, default)

def get_deploy_sha() -> str:
    """
    Función dummy para compatibilidad con app.py.
    Devuelve un string vacío porque no se usa en FASE 1.
    """
    return ""

def get_graph_base_url() -> str:
    """
    Función dummy para compatibilidad con app.py.
    Devuelve un string vacío porque no se usa en FASE 1.
    """
    return ""

# Variables principales para FASE 1
VERIFY_TOKEN = get_env_str("VERIFY_TOKEN")
WHATSAPP_TOKEN = get_env_str("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = get_env_str("PHONE_NUMBER_ID")

# Opcionales (para futuras fases, no afectan FASE 1)
OPENAI_API_KEY = get_env_str("OPENAI_API_KEY")
GOOGLE_SHEET_ID = get_env_str("GOOGLE_SHEET_ID")
GOOGLE_CREDENTIALS_JSON = get_env_str("GOOGLE_CREDENTIALS_JSON")
>>>>>>> 65514338df9e2ce71ab1d251ea76ee0f79bb2b93
