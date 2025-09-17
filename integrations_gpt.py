from config_env import OPENAI_API_KEY, GPT_MODEL
from utils_logger import get_logger

log = get_logger("gpt")

def ask_gpt(prompt: str, system: str = "Eres Vicky, asistente de Christian López.", max_tokens: int = 300) -> str:
    if not OPENAI_API_KEY:
        return "🤖 (GPT desactivado) Escribe *menu* para ver opciones."
    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
        resp = client.chat.completions.create(
            model=GPT_MODEL,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            temperature=0.6,
            max_tokens=max_tokens,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        log.exception("Error GPT: %s", e)
        return "🤖 Tu consulta es compleja. Escribe *menu* para ver opciones."
