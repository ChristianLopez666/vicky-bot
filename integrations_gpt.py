import os
import logging
import requests

log = logging.getLogger("vicky.gpt")

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
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
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
