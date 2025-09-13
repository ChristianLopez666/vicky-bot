# -*- coding: utf-8 -*-
import os
import logging
import requests
import time
import random

log = logging.getLogger("vicky.gpt")

_session = requests.Session()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
GPT_MODEL = os.getenv("GPT_MODEL", "gpt-4o-mini")

# Opcionales para reintentos/backoff
MAX_TRIES = int(os.getenv("GPT_MAX_RETRIES", "5"))
BASE_DELAY = float(os.getenv("GPT_BACKOFF_BASE", "2.0"))

# WhatsApp (mantener como estaba)
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
    Envía una consulta a la API de OpenAI (chat/completions) y devuelve la respuesta en texto.
    Maneja 429 y errores de red con reintentos exponenciales con jitter.
    """
    if not OPENAI_API_KEY:
        return "⚠️ No tengo conexión con GPT en este momento."

    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": GPT_MODEL,
        "messages": [
            {
                "role": "system",
                "content": "Eres Vicky, asistente de Christian López. Responde siempre en español de forma clara y útil.",
            },
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 400,
        "temperature": 0.7,
    }

    for attempt in range(1, MAX_TRIES + 1):
        try:
            resp = _session.post(url, headers=headers, json=payload, timeout=30)
        except requests.RequestException:
            # Error de red: reintentar con backoff si quedan intentos
            if attempt >= MAX_TRIES:
                log.exception("Error de red al contactar GPT en intento %d/%d", attempt, MAX_TRIES)
                break
            delay = BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
            log.warning(
                "Error de red contactando a GPT. Reintentando en %.1fs (intento %d/%d).",
                delay,
                attempt,
                MAX_TRIES,
            )
            time.sleep(delay)
            continue

        # Manejo explícito de 429
        if resp.status_code == 429:
            if attempt >= MAX_TRIES:
                log.warning("GPT rate limit persistente (429) tras %d intentos.", MAX_TRIES)
                break
            delay = BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
            log.warning(
                "GPT rate limit (429). Reintentando en %.1fs (intento %d/%d).",
                delay,
                attempt,
                MAX_TRIES,
            )
            time.sleep(delay)
            continue

        try:
            resp.raise_for_status()
        except requests.HTTPError:
            # No incluir tokens/keys en los logs
            body_preview = resp.text[:500] if resp is not None else ""
            log.exception("Respuesta inesperada de GPT: %s %s", resp.status_code, body_preview)
            break

        try:
            data = resp.json()
        except ValueError:
            log.exception("Respuesta JSON inválida desde GPT")
            break

        # Validar estructura esperada
        try:
            choices = data.get("choices") or []
            if not choices or not isinstance(choices, list):
                log.error("Respuesta de GPT sin 'choices' válido")
                break
            first_choice = choices[0]
            message = None
            if isinstance(first_choice, dict):
                message = first_choice.get("message")
            if not message or not isinstance(message, dict):
                log.error("Respuesta de GPT sin 'message' en choices[0]")
                break
            content = message.get("content", "")
            if not content or not content.strip():
                log.error("Contenido de respuesta de GPT vacío")
                break
            return content.strip()
        except Exception:
            log.exception("Error procesando la respuesta de GPT")
            break

    # Fallback final tras agotar reintentos o excepciones
    return "⚠️ Estoy teniendo problemas para conectarme a GPT en este momento. Intenta de nuevo más tarde."


def send_whatsapp_message(to: str, message: str) -> None:
    """
    Envía un mensaje de texto vía la API de WhatsApp (Graph API).
    No retorna nada. Loggea errores sin exponer tokens.
    """
    if not META_TOKEN or not PHONE_NUMBER_ID or not WHATSAPP_API_URL:
        log.error("Falta configuración de WhatsApp: META_TOKEN o PHONE_NUMBER_ID. No se enviará el mensaje.")
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
        resp = _session.post(WHATSAPP_API_URL, headers=headers, json=payload, timeout=30)
        if resp.status_code != 200:
            # Loggear el error pero no exponer tokens
            log.error("Error al enviar mensaje WA: %s %s", resp.status_code, resp.text)
    except requests.RequestException:
        log.exception("Excepción al intentar enviar mensaje WhatsApp")
    # No return necesario