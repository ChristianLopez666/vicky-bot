from utils_logger import get_logger
from integrations_gpt import ask_gpt
from integrations_sheets import find_prospect_by_phone_last10

log = get_logger("router")

MENU = (
    "Hola, soy *Vicky*, asistente de Christian López en COHIFIS.\n\n"
    "¿En qué puedo ayudarte hoy?\n\n"
    "1️⃣ Asesoría en pensiones\n"
    "2️⃣ Seguro de auto\n"
    "3️⃣ Seguro de vida y salud\n"
    "4️⃣ Tarjeta médica VRIM\n"
    "5️⃣ Préstamo a pensionado IMSS\n"
    "6️⃣ Financiamiento empresarial\n"
    "7️⃣ Nómina empresarial\n"
    "8️⃣ Hablar con Christian\n\n"
    "O descríbeme directamente lo que necesitas."
)

def handle_incoming_message(wa_from: str, text: str) -> str:
    t = (text or "").strip().lower()

    if t in {"menu", "menú", "hola", "hi", "hello", "ayuda", "inicio", "start"}:
        return MENU

    if t == "1":
        return (
            "Asesoría en pensiones. Antes de orientarte necesito saber:\n\n"
            "¿Actualmente sigues cotizando al IMSS o ya dejaste de cotizar?\n\n"
            "Según tu situación, las opciones cambian bastante."
        )

    if t == "2":
        return (
            "Seguro de auto Inbursa. Para armarte las opciones que mejor te convienen, "
            "¿qué tipo de cobertura te interesa?\n\n"
            "1. *Amplia* — cubre todo, incluyendo daños a tu auto\n"
            "2. *Limitada* — robo y daños a terceros\n"
            "3. *Básica* — responsabilidad civil\n\n"
            "Escríbeme 1, 2 o 3."
        )

    if t == "3":
        return (
            "Seguros de vida y salud. Tenemos opciones individuales y familiares.\n\n"
            "¿Es para ti solo o quieres incluir a tu familia también?"
        )

    if t == "4":
        return (
            "Tarjeta médica VRIM: acceso a servicios médicos privados a costos accesibles.\n\n"
            "¿Buscas cobertura solo para ti o para toda tu familia?"
        )

    if t == "5":
        return (
            "Préstamos para pensionados IMSS desde $10,000 hasta $650,000.\n\n"
            "Para orientarte mejor: ¿ya eres pensionado del IMSS o todavía estás en proceso?"
        )

    if t == "6":
        return (
            "Financiamiento empresarial: crédito directo, factoraje y arrendamiento.\n\n"
            "¿Tu empresa ya está constituida formalmente o eres persona física con actividad empresarial?"
        )

    if t == "7":
        return (
            "Nómina empresarial Inbursa: dispersión de pagos y beneficios para tu equipo.\n\n"
            "¿Cuántos empleados tienes aproximadamente?"
        )

    if t == "8":
        return (
            "Listo, le aviso a Christian ahora mismo. "
            "Normalmente responde en menos de una hora.\n\n"
            "¿Hay algo específico que quieras que le comunique para que llegue preparado?"
        )

    # Búsqueda en Sheets por últimos 10 dígitos
    last10 = "".join(filter(str.isdigit, wa_from))[-10:]
    p = find_prospect_by_phone_last10(last10)
    if p:
        nombre = p.get("nombre") or "cliente"
        prod = p.get("producto") or "servicio"
        return (
            f"Hola {nombre}, tengo tu registro asociado a *{prod}*.\n\n"
            "¿En qué te puedo ayudar hoy? Escribe *menu* para ver todas las opciones "
            "o cuéntame directamente lo que necesitas."
        )

    # GPT como fallback
    gpt = ask_gpt(text)
    return gpt or (
        "No capté bien tu mensaje.\n\n"
        "¿Me cuentas con más detalle qué necesitas? "
        "O escribe *menu* para ver todas las opciones."
    )
