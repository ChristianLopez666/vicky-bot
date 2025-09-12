from typing import Literal
from integrations_gpt import ask_gpt

MenuText = (
    "👋 Hola, soy Vicky, asistente de Christian López. Estoy aquí para ayudarte.\n\n"
    "Por favor selecciona una opción:\n\n"
    "1️⃣ ✔️ Asesoría en pensiones\n"
    "2️⃣ 🚗 Seguros de auto (amplia PLUS / amplia / limitada)\n"
    "3️⃣ ❤️ Seguros de vida y salud\n"
    "4️⃣ 🩺 Tarjetas médicas VRIM\n"
    "5️⃣ 💰 Préstamos a pensionados IMSS\n"
    "6️⃣ 🏢 Financiamiento empresarial\n"
    "7️⃣ 💳 Nómina empresarial\n"
    "8️⃣ 📞 Contactar con Christian"
)

def _respuesta_opcion(opcion: Literal["1","2","3","4","5","6","7","8"]) -> str:
    if opcion == "1":
        return "✔️ *Asesoría en pensiones IMSS*...\n(Explicación fija + pide datos clave)."
    if opcion == "2":
        return "🚗 *Seguro de auto Inbursa*...\n(Planes y requisitos para cotizar)."
    if opcion == "3":
        return "❤️ *Seguros de vida y salud*...\n(Opciones de protección, pide edad y ocupación)."
    if opcion == "4":
        return "🩺 *Tarjetas médicas VRIM*...\n(Explicación sobre membresía médica)."
    if opcion == "5":
        return "💰 *Préstamos a pensionados IMSS*...\n(Montos disponibles y requisitos)."
    if opcion == "6":
        return "🏢 *Financiamiento empresarial*...\n(Indica RFC, antigüedad y monto)."
    if opcion == "7":
        return "💳 *Nómina empresarial*...\n(Mejora dispersión y beneficios)."
    if opcion == "8":
        return "📞 He notificado a Christian para que te contacte. ⏱️ *Notifiqué a Christian*."
    return MenuText

def route_message(wa_id: str, wa_e164_no_plus: str, text_in: str) -> str:
    """
    Router híbrido:
    - MENÚ → muestra menú
    - Números 1-8 → respuesta fija
    - Otro texto → consulta a GPT
    """
    if not (text_in or "").strip():
        return MenuText

    txt = text_in.strip().lower()

    if txt in {"menu", "menú", "inicio", "hola", "buenas", "hi", "hola vicky"}:
        return MenuText

    if txt in {"1","2","3","4","5","6","7","8"}:
        return _respuesta_opcion(txt)

    # Cualquier otro texto → GPT
    return ask_gpt(text_in)
