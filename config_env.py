import os

def get_env_str(name: str, default: str = "") -> str:
    """
    Obtiene el valor de una variable de entorno como string.
    Si no existe, devuelve el valor por defecto.
    """
    return os.getenv(name, default)

# Variables principales para FASE 1
VERIFY_TOKEN = get_env_str("VERIFY_TOKEN")
WHATSAPP_TOKEN = get_env_str("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = get_env_str("PHONE_NUMBER_ID")

# Opcionales (para futuras fases, no afectan FASE 1)
OPENAI_API_KEY = get_env_str("OPENAI_API_KEY")
GOOGLE_SHEET_ID = get_env_str("GOOGLE_SHEET_ID")
GOOGLE_CREDENTIALS_JSON = get_env_str("GOOGLE_CREDENTIALS_JSON")
