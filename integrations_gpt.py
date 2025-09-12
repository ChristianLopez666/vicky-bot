# integrations_gpt.py
# Módulo para integrar GPT (OpenAI) y la API de WhatsApp (Meta Cloud) usando requests y logging.
# Diseñado para importarse en app.py y core_router.py de Vicky Bot (Flask).
from typing import Optional
import os
import logging
import requests
import re

logger = logging.getLogger(__name__)
# Evita "No handler found" si la app no configura logging explícitamente.
logger.addHandler(logging.NullHandler())

# Cargar variables de entorno de forma segura (no lanzar si faltan; validar en tiempo de uso).
OPENAI_API_KEY: Optional[str] = os.getenv("OPENAI_API_KEY")
GPT_MODEL: str = os.getenv("GPT_MODEL", "gpt-4o-mini")
META_TOKEN: Optional[str] = os.getenv("META_TOKEN")
PHONE_NUMBER_ID: Optional[str] = os.getenv("PHONE_NUMBER_ID")

# URL base de WhatsApp (Meta Cloud)
_WHATSAPP_API_URL_TEMPLATE = "https://graph.facebook.com/v19.0/{phone_number_id}/messages"

# Configuración de la solicitud
_OPENAI_CHAT_COMPLETION_URL = "https://api.openai.com/v1/chat/completions"
_REQUEST_TIMEOUT_SECONDS = 10  # tiempo de espera para llamadas HTTP


def _clean_text(text: str) -> str:
    """
    Limpia y normaliza texto devuelto por GPT:
    - elimina espacios múltiples y saltos de línea innecesarios
    - recorta espacios al inicio/fin
    - asegura que sea una cadena
    """
    if not isinstance(text, str):
        text = str(text or "")
    # Reemplaza múltiples espacios y tabs por uno
    text = re.sub(r"[ \t]+", " ", text)
    # Normaliza saltos de línea (más de uno -> uno)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    text = text.strip()
    return text


def ask_gpt(prompt: str) -> str:
    """
    Envía el prompt a OpenAI Chat Completions y devuelve la respuesta (texto en español).
    Manejo robusto de errores según requisitos.

    Retorna:
      - Texto respuesta en español (limpio).
      - En caso de problemas, una cadena con el mensaje de error en español.
    """
    system_context = (
        "Eres Vicky, asistente de Christian López. Responde siempre en español de forma clara, útil y conversacional."
    )

    if not OPENAI_API_KEY:
        logger.error("OPENAI_API_KEY no está configurada. No es posible conectar con GPT.")
        return "⚠️ No tengo conexión con GPT en este momento."

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": GPT_MODEL,
        "messages": [
            {"role": "system", "content": system_context},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 400,
        "temperature": 0.7,
    }

    try:
        resp = requests.post(
            _OPENAI_CHAT_COMPLETION_URL, json=payload, headers=headers, timeout=_REQUEST_TIMEOUT_SECONDS
        )
    except requests.exceptions.RequestException as exc:
        # Error de red / conexión
        logger.exception("Error de red al conectar con OpenAI: %s", exc)
        return "⚠️ Hubo un error al conectarme con GPT."

    # Manejo por código de estado
    if resp.status_code == 429:
        logger.warning("OpenAI rate limit (429) al solicitar completions.")
        return "⚠️ Estoy recibiendo demasiadas solicitudes. Intenta más tarde."

    if not (200 <= resp.status_code < 300):
        # Intenta leer mensaje de error para detalles
        err_text = ""
        try:
            err_json = resp.json()
            err_text = err_json.get("error", {}).get("message", "") or str(err_json)
        except Exception:
            err_text = resp.text or f"HTTP {resp.status_code}"
        logger.error(
            "Respuesta inesperada de OpenAI: status=%s, body=%s", resp.status_code, err_text[:1000]
        )
        return "⚠️ Hubo un error al conectarme con GPT."

    # Parsear respuesta exitosa
    try:
        data = resp.json()
        # Estructura estándar: choices[0].message.content
        choices = data.get("choices", [])
        if not choices:
            logger.error("OpenAI devolvió estructura inesperada (sin choices). Payload: %s", data)
            return "⚠️ Hubo un error al conectarme con GPT."

        message_content = choices[0].get("message", {}).get("content") or choices[0].get("text")
        if not message_content:
            # algunos modelos antiguos usan 'text' directo
            logger.error("OpenAI devolvió choice sin contenido textual. choice: %s", choices[0])
            return "⚠️ Hubo un error al conectarme con GPT."

        cleaned = _clean_text(message_content)
        # Aseguramos que haya contenido
        if not cleaned:
            logger.warning("OpenAI devolvió respuesta vacía.")
            return "⚠️ Hubo un error al conectarme con GPT."

        logger.info("Respuesta de GPT obtenida correctamente (long=%d).", len(cleaned))
        return cleaned

    except ValueError as ve:
        logger.exception("No se pudo decodificar JSON de OpenAI: %s", ve)
        return "⚠️ Hubo un error al conectarme con GPT."
    except Exception as exc:
        logger.exception("Error procesando la respuesta de OpenAI: %s", exc)
        return "⚠️ Hubo un error al conectarme con GPT."


def send_whatsapp_message(to: str, message: str) -> None:
    """
    Envía un mensaje de texto a través de la API de WhatsApp (Meta Cloud).
    - to: número en formato E.164 (ej. 5216681234567)
    - message: cuerpo del texto a enviar

    No lanza excepciones hacia fuera por errores previsibles; los registra en logs.
    """
    if not META_TOKEN or not PHONE_NUMBER_ID:
        logger.error(
            "No se envió mensaje: falta configuración de META_TOKEN o PHONE_NUMBER_ID. META_TOKEN_set=%s, PHONE_NUMBER_ID_set=%s",
            bool(META_TOKEN),
            bool(PHONE_NUMBER_ID),
        )
        return

    url = _WHATSAPP_API_URL_TEMPLATE.format(phone_number_id=PHONE_NUMBER_ID)
    headers = {
        "Authorization": f"Bearer {META_TOKEN}",
        "Content-Type": "application/json",
    }

    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"preview_url": False, "body": message},
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=_REQUEST_TIMEOUT_SECONDS)
    except requests.exceptions.RequestException as exc:
        logger.exception("Error de red al enviar mensaje por WhatsApp a %s: %s", to, exc)
        return

    # Graph API típicamente devuelve 200 OK (o 201 en algunos casos); tratamos cualquier 2xx como éxito.
    if 200 <= resp.status_code < 300:
        logger.info("Mensaje enviado a %s vía WhatsApp (status=%s).", to, resp.status_code)
        return

    # Si no es 2xx, registrar fallo con detalle
    resp_text_snippet = (resp.text or "")[:2000]
    logger.error(
        "Fallo al enviar mensaje por WhatsApp a %s: status=%s, body=%s",
        to,
        resp.status_code,
        resp_text_snippet,
    )
    return


__all__ = ["ask_gpt", "send_whatsapp_message"]