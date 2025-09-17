from utils_logger import get_logger
from integrations_gpt import ask_gpt
from integrations_sheets import find_prospect_by_phone_last10

log = get_logger("router")

MENU = (
    "👋 Hola, soy *Vicky*, asistente de Christian López.\n\n"
    "Selecciona una opción escribiendo el número correspondiente:\n\n"
    "1️⃣ Asesoría en pensiones\n"
    "2️⃣ Seguros de auto 🚗\n"
    "3️⃣ Seguros de vida y salud ❤️\n"
    "4️⃣ Tarjetas médicas VRIM 🏥\n"
    "5️⃣ Préstamos a pensionados IMSS 💰\n"
    "6️⃣ Financiamiento empresarial 💼\n"
    "7️⃣ Nómina empresarial 🏦\n"
    "8️⃣ Contactar con Christian 📞\n\n"
    "👉 También puedes escribir *menu* en cualquier momento para ver estas opciones."
)

def handle_incoming_message(wa_from: str, text: str) -> str:
    t = (text or "").strip().lower()
    if t in {"menu", "menú", "hola", "hi", "hello", "ayuda"}:
        return MENU
    if t == "1":
        return "📊 *Asesoría en pensiones.* Modalidad 40, Ley 73, cálculo de pensión."
    if t == "2":
        return "🚗 *Seguros de auto Inbursa.* Planes, coberturas y requisitos."
    if t == "3":
        return "❤️ *Seguros de vida y salud.* Protección integral."
    if t == "4":
        return "🏥 *Tarjetas médicas VRIM.* Servicios médicos privados."
    if t == "5":
        return "💰 *Préstamos a pensionados IMSS.* Montos desde $10,000 a $650,000."
    if t == "6":
        return "💼 *Financiamiento empresarial.* Crédito, factoraje, arrendamiento."
    if t == "7":
        return "🏦 *Nómina empresarial.* Dispersión y beneficios."
    if t == "8":
        return "📞 He notificado a Christian para que te contacte. ⏱️ *Notifiqué a Christian*."

    # Búsqueda simple en Sheets por últimos 10 dígitos
    last10 = "".join(filter(str.isdigit, wa_from))[-10:]
    p = find_prospect_by_phone_last10(last10)
    if p:
        nombre = p.get("nombre") or "cliente"
        prod = p.get("producto") or "servicio"
        return f"✅ {nombre}, tengo tu registro asociado a *{prod}*. ¿Deseas que te contacte Christian? Escribe *8*."

    # GPT opcional
    gpt = ask_gpt(text)
    return gpt or "❓ No entendí tu mensaje. Escribe *menu* para ver opciones."
