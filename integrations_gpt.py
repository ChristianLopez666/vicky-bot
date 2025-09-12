import os
import time
import logging
from typing import Optional

import requests
from dotenv import load_dotenv

# Load envs (allow override)
load_dotenv(override=True)

logger = logging.getLogger("vicky.gpt")

# Configuración GPT
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
GPT_MODEL = os.getenv("GPT_MODEL", "gpt-4o-mini")

# Configuración WhatsApp / Meta
META_TOKEN = os.getenv("META_TOKEN", "")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "")
WA_API_VERSION = os.getenv("WA_API_VERSION", "v20.0")

WHATSAPP_API_URL = (
    f"https://graph.facebook.com/{WA_API_VERSION}/{PHONE_NUMBER_ID}/messages"
    if PHONE_NUMBER_ID
    else None
)


def ask_gpt(prompt: str) -> str:
    """
    Consulta al endpoint chat/completions de OpenAI y devuelve texto.
    Manejo de 429 con reintentos exponenciales (2,4,8s) hasta 3 intentos.
    """
    if not OPENAI_API_KEY:
        logger.error("OPENAI_API_KEY no está configurada. ask_gpt no puede ejecutarse.")
        return ⚠️ No tengo conexión con GPT en este momento."

    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": GPT_MODEL,
        "messages": [
            {"role": "system", "content": "Eres Vicky, asistente de Christian López. Responde siempre en español, de forma clara y útil."},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 400,
        "temperature": 0.7,
    }

    max_retries = 3
    for attempt in range(max_retries):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=30)
            if resp.status_code == 429:
                backoff = 2 ** (attempt + 1)
                logger.warning("GPT rate limit (429). Reintentando en %s segundos (intento %s/%s).", backoff, attempt + 1, max_retries)
                time.sleep(backoff)
                continue
            # For other non-2xx codes, raise for handling below
            resp.raise_for_status()
            data = resp.json()
            # Validate structure
            choices = data.get("choices") or []
            if not choices or not isinstance(choices, list):
                logger.error("Respuesta de GPT sin 'choices' válido. Resp preview: %s", str(data)[:500])
                break
            message = choices[0].get("message", {})
            content = message.get("content", "") if isinstance(message, dict) else ""
            if not content:
                logger.error("Respuesta de GPT sin contenido. Resp preview: %s", str(data)[:500])
                break
            return content.strip()
        except requests.RequestException:
            logger.exception("Error en petición a OpenAI (intento %s/%s).", attempt + 1, max_retries)
            # If last attempt, fallthrough to fallback
            if attempt + 1 < max_retries:
                backoff = 2 ** (attempt + 1)
                time.sleep(backoff)
                continue
            else:
                break
        except Exception:
            logger.exception("Error procesando respuesta de OpenAI.")
            break

    return "⚠️ Estoy teniendo problemas para conectarme a GPT en este momento. Intenta de nuevo más tarde."


def send_whatsapp_message(to: str, message: str) -> None:
    """
    Envía un mensaje de texto a través de la API de WhatsApp (Meta Cloud).
    No devuelve nada; loggea errores en caso de fallo.
    """
    if not META_TOKEN or not PHONE_NUMBER_ID or not WHATSAPP_API_URL:
        logger.error("META_TOKEN o PHONE_NUMBER_ID no están configurados; no se puede enviar mensaje WhatsApp.")
        return

    headers = {
        "Authorization": f"Bearer {META_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": message},
    }

    try:
        resp = requests.post(WHATSAPP_API_URL, headers=headers, json=payload, timeout=30)
        if resp.status_code != 200:
            text_preview = (resp.text[:1000] + "...") if resp.text and len(resp.text) > 1000 else resp.text
            logger.error("WhatsApp API returned status %s. Response: %s", resp.status_code, text_preview)
    except requests.RequestException:
        logger.exception("Error realizando petición a la API de WhatsApp.")
    except Exception:
        logger.exception("Error inesperado enviando mensaje WhatsApp.")