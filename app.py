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

# 🧠 CAMBIO MÍNIMO: sets en memoria para controlar duplicados y saludo único
PROCESSED_MESSAGE_IDS = set()
GREETED_USERS = set()

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
    # Compatibilidad: si quedaron como set, convertir a dict con timestamps
    if isinstance(PROCESSED_MESSAGE_IDS, set):
        PROCESSED_MESSAGE_IDS = {}
    if isinstance(GREETED_USERS, set):
        GREETED_USERS = {}

    MSG_TTL = 600          # 10 minutos
    GREET_TTL = 24 * 3600  # 24 horas

    # Limpieza simple cuando crece mucho
    if len(PROCESSED_MESSAGE_IDS) > 5000:
        PROCESSED_MESSAGE_IDS = {k: v for k, v in PROCESSED_MESSAGE_IDS.items() if now - v < MSG_TTL}
    if len(GREETED_USERS) > 5000:
        GREETED_USERS = {k: v for k, v in GREETED_USERS.items() if now - v < GREET_TTL}

    # ---- Texto del menú (variable local) ----
    MENU_TEXT = (
        "👉 Elige una opción del menú:\n"
        "1) Asesoría en pensiones IMSS (Ley 73 / Modalidad 40 / Modalidad 10)\n"
        "2) Seguros de auto (Amplia PLUS, Amplia, Limitada)\n"
        "3) Seguros de vida y salud\n"
        "4) Tarjetas médicas VRIM\n"
        "5) Préstamos a pensionados IMSS ($40,000 a $650,000)\n"
        "6) Financiamiento empresarial y nómina empresarial\n"
        "7) Contactar con Christian\n"
        "\nEscribe el número de la opción o 'menu' para volver a ver el menú."
    )

    # Respuestas mínimas por opción
    OPTION_RESPONSES = {
        "1": "🧓 Asesoría en pensiones IMSS. Cuéntame tu caso (Ley 73, M40, M10) y te guío paso a paso.",
        "2": "🚗 Seguro de auto. Envíame *foto de tu INE* y *tarjeta de circulación* o tu *número de placa* para cotizar.",
        "3": "🛡️ Seguros de vida y salud. Te preparo una cotización personalizada.",
        "4": "🩺 Tarjetas médicas VRIM. Te comparto información y precios.",
        "5": "💳 Préstamos a pensionados IMSS. Dime tu pensión aproximada y el monto deseado (desde $40,000).",
        "6": "🏢 Financiamiento empresarial y nómina. ¿Qué necesitas: crédito, factoraje o nómina?",
        "7": "📞 ¡Listo! Notifiqué a Christian para que te contacte y te dé seguimiento."
    }

    # ---- Procesar SOLO el primer mensaje válido por payload ----
    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            val = change.get("value", {})

            # Ignorar callbacks de estado (entregas, leídos, etc.)
            if "statuses" in val:
                continue

            messages = val.get("messages", [])
            if not messages:
                continue

            # Tomamos solo el primer mensaje
            message = messages[0]
            msg_id = message.get("id")
            msg_type = message.get("type")
            sender = message.get("from")
            business_phone = val.get("metadata", {}).get("display_phone_number")

            logging.info(f"🧾 id={msg_id} type={msg_type} from={sender} business_phone={business_phone}")

            # 1) Desduplicar por id con TTL
            if msg_id:
                last_seen = PROCESSED_MESSAGE_IDS.get(msg_id)
                if last_seen and (now - last_seen) < MSG_TTL:
                    logging.info(f"🔁 Duplicado ignorado: {msg_id}")
                    continue
                PROCESSED_MESSAGE_IDS[msg_id] = now

            # 2) Ignorar posibles ecos desde el propio número
            if business_phone and sender and sender.endswith(business_phone):
                logging.info("🪞 Echo desde business_phone ignorado")
                continue

            # 3) Solo procesar texto por ahora
            if msg_type != "text":
                logging.info(f"ℹ️ Mensaje no-texto ignorado: {msg_type}")
                continue

            text = message.get("text", {}).get("body", "") or ""
            text_norm = text.strip().lower()
            logging.info(f"✉️ Texto normalizado: {text_norm}")

            # ✅ PRIORIDAD 1: si es opción 1–7, responder y salir (evita que se re-muestre el menú)
            if text_norm in OPTION_RESPONSES:
                send_message(sender, OPTION_RESPONSES[text_norm])
                # Si deseas re-mostrar el menú tras responder, puedes añadir otra llamada aquí.
                continue

            # PRIORIDAD 2: saludos/menú
            first_greet_ts = GREETED_USERS.get(sender)
            if not first_greet_ts or (now - first_greet_ts) >= GREET_TTL:
                # Primera interacción o expiró ventana
                if text_norm in ("hola", "menú", "menu"):
                    send_message(
                        sender,
                        "👋 Hola, soy Vicky, asistente de Christian López. Estoy aquí para ayudarte.\n\n" + MENU_TEXT
                    )
                else:
                    send_message(sender, MENU_TEXT)
                GREETED_USERS[sender] = now
                continue

            # Usuario ya saludado → mostrar menú cuando lo pida
            if text_norm in ("hola", "menú", "menu"):
                send_message(sender, MENU_TEXT)
                continue

            # Nada coincide → guía breve
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
