import logging

from integrations_gpt import ask_gpt

log = logging.getLogger("core_router")

# Mantener el mismo texto de menú y opciones (1–8).
MENU_TEXT = (
    "Menú de opciones:\n"
    "1. Información general 📌\n"
    "2. Horarios ⏰\n"
    "3. Precios 💸\n"
    "4. Soporte técnico 🛠️\n"
    "5. Preguntas frecuentes ❓\n"
    "6. Enlaces útiles 🔗\n"
    "7. Contacto 📞\n"
    "8. Notificar a Christian 🔔\n\n"
    "Escribe el número de la opción que deseas, o escríbeme tu consulta."
)


# Respuestas fijas para las opciones 1..7 y la confirmación 8 (contiene exactamente "Notifiqué a Christian")
FIXED_RESPONSES = {
    "1": "Aquí tienes información general sobre Vicky Bot. ¿Quieres que amplíe algún punto?",
    "2": "Nuestros horarios son de lunes a viernes de 9:00 a 18:00. ¿Necesitas atención fuera de ese horario?",
    "3": "Los precios varían según el servicio. Escríbeme lo que necesitas y te doy una estimación.",
    "4": "Para soporte técnico, por favor describe el problema con detalle y te ayudaré.",
    "5": "Consulta nuestra sección de FAQ o dime tu duda para que la responda.",
    "6": "Te comparto enlaces útiles: https://example.com (ejemplo). ¿Qué buscas exactamente?",
    "7": "Puedes contactarnos por correo a contacto@example.com o aquí mismo. ¿Te ayudo a crear un mensaje?",
    "8": "He tomado nota. Notifiqué a Christian.",
}


# Atajos de saludo que no deben disparar a GPT
_GREETINGS = {
    "hola",
    "buenas",
    "buenos días",
    "buenos dias",
    "buenas tardes",
    "buenas noches",
    "holi",
    "hello",
}


def route_message(wa_id: str, wa_e164_no_plus: str, text_in: str) -> str:
    """
    Procesa el mensaje entrante y devuelve la respuesta como texto en español.
    - Atajos y menú manejados localmente (sin llamar a GPT).
    - Consultas libres enviadas a ask_gpt con fallback sólido.
    """
    text = (text_in or "").strip()
    if not text:
        return MENU_TEXT

    low = text.lower()

    # Atajo de saludos (no usar GPT)
    if low in _GREETINGS:
        return "¡Hola! Soy *Vicky*. Escribe *menu* para ver opciones o dime en qué te ayudo."

    # Menú
    if low in {"menu", "menú"}:
        return MENU_TEXT

    # Opciones fijas 1..8
    if low in FIXED_RESPONSES:
        return FIXED_RESPONSES[low]

    # Consultas libres -> usar GPT con manejo de excepciones
    try:
        reply = ask_gpt(text)
        # ask_gpt ya devuelve mensajes en español o mensajes de error en español
        if reply:
            return reply
        return "⚠️ No pude procesar tu consulta en este momento. Por favor, escribe *menu* para ver las opciones disponibles."
    except Exception:
        log.exception("Fallo en GPT desde route_message")
        return "⚠️ No pude procesar tu consulta en este momento. Por favor, escribe *menu* para ver las opciones disponibles."