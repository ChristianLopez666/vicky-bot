import logging
import os
import requests
from flask import Flask, request
from config_env import (
    VERIFY_TOKEN, WHATSAPP_TOKEN, PHONE_NUMBER_ID,
    WA_API_VERSION, ADVISOR_NUMBER, LOG_LEVEL
)

app = Flask(__name__)
logging.basicConfig(level=getattr(logging, (LOG_LEVEL or "INFO").upper(), logging.INFO))
log = app.logger

API_BASE = f"https://graph.facebook.com/{WA_API_VERSION}".rstrip("/")

# ====== Helpers ======
def send_whatsapp_text(to: str, message: str):
    """Envía un mensaje de texto vía WhatsApp Cloud API."""
    url = f"{API_BASE}/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": message}
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=15)
        log.info("📤 Enviado a %s: %s | %s %s", to, (message[:120] + "…") if len(message) > 120 else message, resp.status_code, resp.text)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.exception("Error enviando mensaje a %s", to)
        return {"error": str(e)}

def get_menu_text():
    return (
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

def process_message(body: str) -> str:
    text = (body or "").strip().lower()
    if text in {"menu", "menú", "hola", "hi", "hello"}:
        return get_menu_text()
    if text == "1":
        return "📊 *Asesoría en pensiones.*\n(Modalidad 40, Ley 73, cálculo de pensión, etc.)"
    if text == "2":
        return "🚗 *Seguros de auto Inbursa.*\n(Planes y requisitos para cotizar)."
    if text == "3":
        return "❤️ *Seguros de vida y salud.*\n(Protección para ti y tu familia)."
    if text == "4":
        return "🏥 *Tarjetas médicas VRIM.*\n(Acceso a servicios médicos privados)."
    if text == "5":
        return "💰 *Préstamos a pensionados IMSS.*\n(Montos desde $10,000 hasta $650,000)."
    if text == "6":
        return "💼 *Financiamiento empresarial.*\n(Crédito, factoraje, arrendamiento)."
    if text == "7":
        return "🏦 *Nómina empresarial.*\n(Dispersión de nómina y beneficios)."
    if text == "8":
        return "📞 He notificado a Christian para que te contacte. ⏱️ *Notifiqué a Christian*."
    return "❓ No entendí tu mensaje. Escribe *menu* para ver las opciones disponibles."

# ====== Rutas ======
@app.route("/", methods=["GET"])
def index():
    return "Vicky Bot – FASE 1 OK ✅", 200

@app.route("/health", methods=["GET"])
def health():
    return "Bot Vicky corriendo OK ✅", 200

@app.route("/webhook", methods=["GET"])
def verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        log.info("✅ Webhook verificado correctamente")
        return challenge, 200
    log.warning("❌ Verificación de webhook fallida")
    return "Verification failed", 403

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    log.info("📩 Evento recibido")

    try:
        # Estructura oficial de Meta
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                messages = value.get("messages", [])
                for message in messages:
                    wa_from = message.get("from")  # E.164 sin '+'
                    text = (message.get("text", {}) or {}).get("body", "")

                    log.info("📥 Mensaje de %s: %s", wa_from, text)
                    reply = process_message(text)

                    # Enviar respuesta al usuario
                    send_whatsapp_text(wa_from, reply)

                    # Si es la opción 8, notificar al asesor
                    if reply.endswith("*Notifiqué a Christian*.") and ADVISOR_NUMBER:
                        aviso = f"📞 Cliente solicita contacto.\nDe: {wa_from}\nMensaje: {text}"
                        send_whatsapp_text(ADVISOR_NUMBER, aviso)
    except Exception:
        log.exception("⚠️ Error procesando webhook")

    return "EVENT_RECEIVED", 200

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
