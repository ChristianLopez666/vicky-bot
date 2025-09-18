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

    MSG_TTL = 600          # 10 minutos: ventana para deduplicar message.id
    GREET_TTL = 24 * 3600  # 24 horas: ventana para no repetir saludo completo

    # Limpieza simple cuando crece mucho
    if len(PROCESSED_MESSAGE_IDS) > 5000:
        PROCESSED_MESSAGE_IDS = {k: v for k, v in PROCESSED_MESSAGE_IDS.items() if now - v < MSG_TTL}
    if len(GREETED_USERS) > 5000:
        GREETED_USERS = {k: v for k, v in GREETED_USERS.items() if now - v < GREET_TTL}

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

            # 2) Ignorar posibles ecos desde el propio número (seguridad)
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

            # 4) Saludo una vez por 24h; luego solo menú si lo piden
            first_greet_ts = GREETED_USERS.get(sender)
            if not first_greet_ts or (now - first_greet_ts) >= GREET_TTL:
                if text_norm in ("hola", "menú", "menu"):
                    send_message(
                        sender,
                        "👋 Hola, soy Vicky, asistente de Christian López. Estoy aquí para ayudarte.\n\n👉 Elige una opción del menú:"
                    )
                else:
                    # Cualquier texto previo al saludo → solo menú (sin repetir saludo)
                    send_message(sender, "👉 Elige una opción del menú:")
                GREETED_USERS[sender] = now
                continue

            # 5) Usuario ya saludado en ventana: si pide menú, muéstralo; si no, no repetir
            if text_norm in ("hola", "menú", "menu"):
                send_message(sender, "👉 Elige una opción del menú:")
                continue

            # 6) Aquí iría la lógica de opciones (1,2,...) sin repetir saludo/menú
            logging.info("📌 Mensaje recibido (ya saludado). Sin respuesta automática.")

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
