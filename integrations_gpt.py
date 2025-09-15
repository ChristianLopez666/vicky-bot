import os
import time
from typing import Optional

from openai import OpenAI
from openai.error import OpenAIError, APIError, Timeout as OpenAITimeout

# Configuration
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GPT_MODEL = os.getenv("GPT_MODEL", "gpt-4o-mini")
RETRIES = 3
BACKOFF = 0.5  # seconds
TIMEOUT = 10  # seconds

_system_prompt = (
    "Eres Vicky, asistente amable. Responde en espanol, claro y sin emojis. "
    "Responde brevemente y directamente."
)

def _client() -> OpenAI:
    if not OPENAI_API_KEY:
        raise Exception("OPENAI_API_KEY no configurada")
    return OpenAI(api_key=OPENAI_API_KEY)

def ask_gpt(user_text: str) -> str:
    """
    Ask GPT for a reply to user_text.

    Returns the assistant reply as a string. Raises Exception on error.
    """
    if user_text is None:
        user_text = ""

    client = _client()
    last_err = None
    for attempt in range(1, RETRIES + 1):
        try:
            # Use chat completion like interface
            resp = client.chat.completions.create(
                model=GPT_MODEL,
                messages=[
                    {"role": "system", "content": _system_prompt},
                    {"role": "user", "content": user_text},
                ],
                max_tokens=512,
                temperature=0.7,
                timeout=TIMEOUT,
            )
            # Extract content
            if hasattr(resp, "choices") and len(resp.choices) > 0:
                choice = resp.choices[0]
                if hasattr(choice, "message") and isinstance(choice.message, dict):
                    content = choice.message.get("content", "")
                    if content is None:
                        content = ""
                    return str(content).strip()
                # fallback for different shape
                txt = getattr(choice, "text", None)
                if txt:
                    return str(txt).strip()
            # Fallback if structure not as expected
            raise Exception("Respuesta de GPT en formato inesperado")
        except (OpenAITimeout, TimeoutError) as e:
            last_err = f"timeout: {e}"
            time.sleep(BACKOFF * attempt)
            continue
        except APIError as e:
            last_err = f"api_error: {e}"
            time.sleep(BACKOFF * attempt)
            continue
        except OpenAIError as e:
            last_err = f"openai_error: {e}"
            # For some errors, do not retry
            break
        except Exception as e:
            last_err = str(e)
            time.sleep(BACKOFF * attempt)
            continue
    raise Exception(f"GPT request failed: {last_err}")
