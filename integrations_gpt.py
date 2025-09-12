import os
import logging
import requests

log = logging.getLogger("vicky.gpt")

# =======================
# Configuración GPT
# =======================
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
GPT_MODEL = os.getenv("GPT_MODEL", "gpt-4o-mini")


def ask_gpt(prompt: str) -> str:
    """
    Envía el texto del usuario a GPT y devuelve la respuesta.
    """
    if not OPENAI_API_KEY:
        log.error("OPENAI_API_KEY faltante.")
        return "⚠️ No tengo conexión con GPT en este momento."

    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": GPT_MODEL,
        "messages": [
            {"role": "system", "content": "Eres Vicky, asistente de Christian López. Responde en español de forma clara y útil."},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 400,
        "temperature": 0.7,
    }

    try:
        r = requests.post(url, headers=headers, json=payload, timeout=30)
        r.raise_for_status()
        data = r.json()
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        log.exception("Error al consultar GPT")
        return f"⚠️ Hubo un error al conectarme con GPT: {e}"


# =======================
# Configuración WhatsApp
# =======================
META_TOKEN = os.getenv("META_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
WHATSAPP_API_URL = (
    f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    if PHONE_NUMBER_ID
    else None
)


def send_whatsapp_message(to: str, message: str) -> None:
    """
    Envía un mensaje de texto a WhatsApp usando la API de Meta.
    :param to: número en formato E164 (ejemplo: 5216681234567)
    :param message: texto a enviar
    """
    if not META_TOKEN or not PHONE_NUMBER_ID:
        log.error("❌ META_TOKEN o PHONE_NUMBER_ID no configurados en variables de entorno.")
        return

    headers = {
        "Authorization": f"Bearer {META_TOKEN}",
        "Content-Type": "application/json"
    }

    data = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": message}
    }

    try:
        resp = requests.post(WHATSAPP_API_URL, headers=headers, json=data)
        if resp.status_code != 200:
            log.error(f"❌ Error al enviar mensaje WA: {resp.status_code} {resp.text}")
        else:
            log.info(f"✅ Mensaje enviado a {to}: {message}")
    except Exception as e:
        log.exception(f"❌ Excepción enviando mensaje a {to}: {e}")
