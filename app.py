import os 
import logging
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# Cargar variables de entorno
load_dotenv()

# Configuración de logging
logging.basicConfig(level=logging.INFO)

# Inicializar Flask
app = Flask(__name__)

# Variables de entorno
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
WHATSAPP_TOKEN = os.getenv("META_TOKEN")  # ✅ Ajustado para Render
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
ADVISOR_NUMBER = os.getenv("ADVISOR_NUMBER", "5216682478005")  # Notificación al asesor
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")  # ✅ GPT (opcional)

# 🧠 Control simple en memoria
PROCESSED_MESSAGE_IDS = set()
GREETED_USERS = set()

# --------- GPT fallback (opcional, sin dependencias nuevas) ----------
def gpt_reply(user_text: str) -> str | None:
    """
    Devuelve una respuesta breve usando GPT si OPENAI_API_KEY existe.
    Si no hay API key o falla la llamada, devuelve None (para no romper el flujo).
    """
    if not OPENAI_API_KEY:
        return None
    try:
        url = "https://api.openai.com/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": "gpt-4o-mini",  # liviano y rápido; puedes cambiarlo por otro
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Eres Vicky, asistente de Christian López (asesor financiero de Inbursa). "
                        "Responde en español, de forma breve, clara y orientada a conversión. "
                        "Si la pregunta es sobre opciones del menú, sugiere escribir 'menu'. "
                        "No inventes datos de pólizas ni montos; si faltan datos, pídelos de forma amable."
                    )
                },
                {"role": "user", "content": user_text}
            ],
            "temperature": 0.3,
            "max_tokens": 220
        }
        resp = requests.post(url, headers=headers, json=payload, timeout=12)
        if resp.status_code == 200:
            data = resp.json()
            msg = data.get("choices", [{}])[0].get("message", {}).get("content")
            return msg.strip() if msg else None
        else:
            logging.warning(f"GPT fallback no disponible: {resp.status_code} - {resp.text}")
            return None
    except Exception as e:
        logging.error(f"Error en gpt_reply: {e}")
        return None
# ---------------------------------------------------------------------

# Endpoint de verificación
@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        logging.info("Webhook verificado correctamente ✅")
        return challenge, 200
    else:
        logging.warning("Fallo en la verificación del webhook ❌")
        return "Verification failed", 403

# Endpoint para recibir mensajes
@app.route("/webhook", methods=["POST"])
def receive_message():
    data = request.get_json()
    logging.info(f"📩 Mensaje recibido: {data}")

    # Ignorar payloads inesperados
    if not data or "entry" not in data:
        return jsonify({"status": "ignored"}), 200

    # ---- Idempotencia y estado mínimo con TTL ----
    from time import time
    now = time()

    global PROCESSED_MESSAGE_IDS, GREETED_USERS
    if isinstance(PROCESSED_MESSAGE_IDS, set):
        PROCESSED_MESSAGE_IDS = {}
    if isinstance(GREETED_USERS, set):
        GREETED_USERS = {}

    MSG_TTL = 600          # 10 minutos
    GREET_TTL = 24 * 3600  # 24 horas

    # Limpieza simple
    if len(PROCESSED_MESSAGE_IDS) > 5000:
        PROCESSED_MESSAGE_IDS = {k: v for k, v in PROCESSED_MESSAGE_IDS.items() if now - v < MSG_TTL}
    if len(GREETED_USERS) > 5000:
        GREETED_USERS = {k: v for k, v in GREETED_USERS.items() if now - v < GREET_TTL}

    # ---- Menú y respuestas ----
    MENU_TEXT = (
        "👉 Elige una opción del menú:\n"
        "1) Asesoría en pensiones IMSS (Ley 73 / Modalidad 40 / Modalidad 10)\n"
        "2) Seguros de auto (Amplia PLUS, Amplia, Limitada)\n"
        "3) Seguros de vida y salud\n"
        "4) Tarjetas médicas VRIM\n"
        "5) Préstamos a pensionados IMSS (a partir de $40,000 pesos hasta $650,000)\n"
        "6) Financiamiento empresarial y nómina empresarial\n"
        "7) Contactar con Christian\n"
        "\nEscribe el número de la opción o 'menu' para volver a ver el menú."
    )

    OPTION_RESPONSES = {
        "1": "🧓 Asesoría en pensiones IMSS. Cuéntame tu caso (Ley 73, M40, M10) y te guío paso a paso.",
        "2": "🚗 Seguro de auto. Envíame *foto de tu INE* y *tarjeta de circulación* o tu *número de placa* para cotizar.",
        "3": "🛡️ Seguros de vida y salud. Te preparo una cotización personalizada.",
        "4": "🩺 Tarjetas médicas VRIM. Te comparto información y precios.",
        "5": "💳 Préstamos a pensionados IMSS. Monto *a partir de $40,000* y hasta $650,000. Dime tu pensión aproximada y el monto deseado.",
        "6": "🏢 Financiamiento empresarial y nómina. ¿Qué necesitas: crédito, factoraje o nómina?",
        "7": "📞 ¡Listo! Notifiqué a Christian para que te contacte y te dé seguimiento."
    }

    KEYWORD_INTENTS = [
        (("pension", "pensión", "imss", "modalidad 40", "modalidad 10", "ley 73"), "1"),
        (("auto", "seguro de auto", "placa", "tarjeta de circulación", "coche", "carro"), "2"),
        (("vida", "seguro de vida", "salud", "gastos médicos", "asegurar vida", "planes de seguro"), "3"),
        (("vrim", "tarjeta médica", "membresía médica"), "4"),
        (("préstamo", "prestamo", "pensionado", "crédito", "credito"), "5"),
        (("financiamiento", "factoraje", "nómina", "nomina", "empresarial"), "6"),
        (("contacto", "contactar", "asesor", "christian", "llámame", "quiero hablar"), "7"),
    ]

    def infer_option_from_text(t: str):
        for keywords, opt in KEYWORD_INTENTS:
            if any(k in t for k in keywords):
                return opt
        return None

    # ---- Procesar SOLO el primer mensaje válido por payload ----
    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            val = change.get("value", {})

            # Ignorar callbacks de estado
            if "statuses" in val:
                continue

            messages = val.get("messages", [])
            if not messages:
                continue

            # Primer mensaje del payload
            message = messages[0]
            msg_id = message.get("id")
            msg_type = message.get("type")
            sender = message.get("from")
            business_phone = val.get("metadata", {}).get("display_phone_number")

            # Nombre del perfil si viene
            profile_name = None
            try:
                profile_name = (val.get("contacts", [{}])[0].get("profile", {}) or {}).get("name")
            except Exception:
                profile_name = None

            logging.info(f"🧾 id={msg_id} type={msg_type} from={sender} business_phone={business_phone} profile={profile_name}")

            # Deduplicar por id con TTL
            if msg_id:
                last_seen = PROCESSED_MESSAGE_IDS.get(msg_id)
                if last_seen and (now - last_seen) < MSG_TTL:
                    logging.info(f"🔁 Duplicado ignorado: {msg_id}")
                    continue
                PROCESSED_MESSAGE_IDS[msg_id] = now

            # Ignorar posibles ecos
            if business_phone and sender and sender.endswith(business_phone):
                logging.info("🪞 Echo desde business_phone ignorado")
                continue

            if msg_type != "text":
                logging.info(f"ℹ️ Mensaje no-texto ignorado: {msg_type}")
                continue

            text = message.get("text", {}).get("body", "") or ""
            text_norm = text.strip().lower()
            logging.info(f"✉️ Texto normalizado: {text_norm}")

            # ✅ PRIORIDAD 1: opción 1–7 o intención por palabras clave
            option = text_norm if text_norm in OPTION_RESPONSES else infer_option_from_text(text_norm)
            if option:
                send_message(sender, OPTION_RESPONSES[option])
                if option == "7":
                    # Notificar al asesor
                    notify_text = (
                        "🔔 *Vicky Bot – Solicitud de contacto*\n"
                        f"- Nombre: {profile_name or 'No disponible'}\n"
                        f"- WhatsApp: {sender}\n"
                        f"- Mensaje original: \"{text.strip()}\""
                    )
                    try:
                        send_message(ADVISOR_NUMBER, notify_text)
                        logging.info(f"📨 Notificación enviada al asesor {ADVISOR_NUMBER}")
                    except Exception as e:
                        logging.error(f"❌ Error notificando al asesor: {e}")
                continue

            # PRIORIDAD 2: saludo/menú
            first_greet_ts = GREETED_USERS.get(sender)
            if not first_greet_ts or (now - first_greet_ts) >= GREET_TTL:
                if text_norm in ("hola", "menú", "menu"):
                    send_message(
                        sender,
                        "👋 Hola, soy Vicky, asistente de Christian López. Estoy aquí para ayudarte.\n\n" + MENU_TEXT
                    )
                else:
                    send_message(sender, MENU_TEXT)
                GREETED_USERS[sender] = now
                continue

            if text_norm in ("hola", "menú", "menu"):
                send_message(sender, MENU_TEXT)
                continue

            # PRIORIDAD 3: GPT fallback (si hay API Key)
            ai = gpt_reply(text)
            if ai:
                send_message(sender, ai)
                # opcional: sugerir el menú
                # send_message(sender, "Escribe 'menu' para ver opciones.")
                continue

            # Fallback final sin GPT
            logging.info("📌 Mensaje recibido (ya saludado). Respuesta guía.")
            send_message(sender, "No te entendí. Escribe un número del 1 al 7 o 'menu' para ver opciones.")

    return jsonify({"status": "ok"}), 200

# Función para enviar mensajes
def send_message(to, text):
    url = f"https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text}
    }

    response = requests.post(url, headers=headers, json=payload)
    logging.info(f"Respuesta de WhatsApp API: {response.status_code} - {response.text}")

# Endpoint de salud
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
