import os

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

def ask_gpt(user_text: str) -> str:
    """
    FASE 2 – Placeholder seguro.
    Si no hay OPENAI_API_KEY, responde una guía amable sin romper el flujo.
    """
    if not OPENAI_API_KEY:
        return ("🤖 (GPT desactivado) Por ahora puedo ayudarte con el menú y opciones fijas. "
                "Escribe *menu* para ver opciones o activa OPENAI_API_KEY para respuestas inteligentes.")
    try:
        # Implementación real (client v1) cuando activemos Fase 2:
        # from openai import OpenAI
        # client = OpenAI(api_key=OPENAI_API_KEY)
        # resp = client.chat.completions.create(
        #     model=os.getenv("GPT_MODEL", "gpt-4o-mini"),
        #     messages=[
        #         {"role": "system", "content": "Eres Vicky, asistente de Christian López."},
        #         {"role": "user", "content": user_text}
        #     ],
        #     temperature=0.7,
        #     max_tokens=300
        # )
        # return resp.choices[0].message.content.strip()
        return "🤖 (GPT activo) Respuesta generada — (placeholder)."
    except Exception as e:
        return f"⚠️ Error al generar respuesta: {e}"
