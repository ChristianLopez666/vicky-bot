# -*- coding: utf-8 -*-
import logging
import re
import time
import unicodedata

from integrations_gpt import ask_gpt

log = logging.getLogger("core_router")

# Menú oficial de Vicky
MENU_TEXT = (
    "Vicky:\n"
    "👋 Hola, soy *Vicky*, asistente de Christian López.\n"
    "Selecciona una opción escribiendo el número correspondiente:\n\n"
    "1️⃣ Asesoría en pensiones\n"
    "2️⃣ Seguros de auto 🚗\n"
    "3️⃣ Seguros de vida y salud ❤️\n"
    "4️⃣ Tarjetas médicas VRIM 🏥\n"
    "5️⃣ Préstamos a pensionados IMSS 💰\n"
    "6️⃣ Financiamiento empresarial 💼\n"
    "7️⃣ Nómina empresarial 💳\n"
    "8️⃣ Contactar con Christian 📞\n\n"
    "👉 También puedes escribir *menu* en cualquier momento para ver estas opciones."
)

GREETINGS = {
    "hola", "holi", "hello", "buenas", "buenos dias", "buenas tardes", "buenas noches"
}

def _strip_accents_and_punct(s: str) -> str:
    """Normaliza: minúsculas, sin tildes, sin puntuación y sin espacios extra."""
    s = (s or "").lower().strip()
    s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

# Throttle simple por usuario para evitar 429
_LAST_GPT_AT = {}  # wa_e164_no_plus -> timestamp

def route_message(wa_id: str, wa_e164_no_plus: str, text_in: str) -> str:
    text = (text_in or "").strip()
    if not text:
        return MENU_TEXT

    norm = _strip_accents_and_punct(text)
    log.info("core_router: route_message called - wa_id=%s wa_e164=%s text_in='%s'", wa_id, wa_e164_no_plus, text)

    # Menú
    if norm in ("menu", "menu "):
        return MENU_TEXT

    # Saludos comunes (sin GPT)
    if norm in GREETINGS or norm.startswith("hola") or norm.startswith("hello"):
        return "¡Hola! Soy *Vicky*. Escribe *menu* para ver opciones o dime en qué te ayudo."

    # Opciones fijas 1–8
    if norm == "1":
        return "✅ *Asesoría en pensiones IMSS*.\n(Explicación fija + pide datos clave)."
    if norm == "2":
        return "🚗 *Seguros de auto Inbursa*.\n(Planes y requisitos para cotizar)."
    if norm == "3":
        return "❤️ *Seguros de vida y salud*.\n(Opciones de protección, pide edad y ocupación)."
    if norm == "4":
        return "🏥 *Tarjetas médicas VRIM*.\n(Cobertura y cómo solicitarlas)."
    if norm == "5":
        return "💰 *Préstamos a pensionados IMSS*.\nMontos desde $10,000 hasta $650,000. Responde con '8' para iniciar tu trámite."
    if norm == "6":
        return "💼 *Financiamiento empresarial*.\n(Planes y requisitos)."
    if norm == "7":
        return "💳 *Nómina empresarial*.\n(Información y beneficios)."
    if norm == "8":
        # Literal requerido para notificación automática en app.py
        return "📞 He notificado a Christian para que te contacte. ⏱️ *Notifiqué a Christian*."

    # Consultas libres → GPT con throttle de 10s por usuario
    now = time.time()
    last = _LAST_GPT_AT.get(wa_e164_no_plus, 0)
    if now - last < 10:
        log.info("core_router: throttle GPT for %s (%.1fs)", wa_e164_no_plus, now - last)
        return "⌛ Dame unos segundos y vuelve a intentarlo, por favor. Mientras tanto, escribe *menu* para ver opciones."

    try:
        _LAST_GPT_AT[wa_e164_no_plus] = now
        log.info("core_router: Consulta libre detectada - delegando a GPT - wa_id=%s wa_e164=%s", wa_id, wa_e164_no_plus)
        reply = ask_gpt(text)
        return reply or "⚠️ No pude procesar tu consulta en este momento. Por favor, escribe *menu* para ver las opciones disponibles."
    except Exception:
        log.exception("core_router: error consultando GPT")
        return "⚠️ No pude procesar tu consulta en este momento. Por favor, escribe *menu* para ver las opciones disponibles."
