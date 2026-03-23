from config_env import OPENAI_API_KEY, GPT_MODEL
from utils_logger import get_logger

log = get_logger("gpt")

_system_prompt = (
    "Eres Vicky, asistente virtual de COHIFIS, empresa de asesoria financiera "
    "en Ahome, Sinaloa, respaldada por Inbursa. "
    "Tu asesor es Christian Lopez. "
    "Eres cercana, clara y honesta. Nunca condescendiente. "
    "No uses jerga financiera tecnica sin explicarla. "
    "No inventes informacion. Si no sabes algo, di: Lo verifico con Christian y te confirmo. "
    "No presiones ni apures. Presenta opciones y deja que el prospecto elija. "
    "Responde en espanol. Maximo 3 oraciones por mensaje. "
    "Productos COHIFIS: asesoria pensiones, seguros auto, seguros vida/salud, "
    "tarjetas medicas VRIM, prestamos IMSS ($10k-$650k), financiamiento empresarial, nomina. "
    "Diferenciador: Inbursa es #1 en servicio por CONDUSEF. "
    "Si objetan precio: el costo va en relacion al servicio que recibes. "
    "Flujo: califica con una pregunta, ofrece 2-3 opciones, cierra con cual te interesa mas. "
    "Limites: no des precios sin calificar, no prometas aprobaciones, "
    "no compares negativamente con otras aseguradoras."
)

def ask_gpt(prompt: str, max_tokens: int = 300) -> str:
    if not OPENAI_API_KEY:
        return "Escribe *menu* para ver las opciones disponibles."
    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
        resp = client.chat.completions.create(
            model=GPT_MODEL,
            messages=[
                {"role": "system", "content": _system_prompt},
                {"role": "user", "content": prompt}
            ],
            temperature=0.6,
            max_tokens=max_tokens,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        log.exception("Error GPT: %s", e)
        return "No pude procesar tu consulta. Escribe *menu* para ver opciones."
