import os
import redis
import time
import json
import logging
import requests

# Configuración de logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("outbound_worker")

# Conexión a Redis
REDIS_URL = os.getenv("REDIS_URL")
if not REDIS_URL:
    raise ValueError("❌ No se encontró la variable de entorno REDIS_URL")

redis_client = redis.from_url(REDIS_URL)

# Cola de mensajes outbound
QUEUE_NAME = "outbound_queue"

# Variables de entorno para WhatsApp Cloud API
META_TOKEN = os.getenv("META_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")

if not META_TOKEN or not PHONE_NUMBER_ID:
    raise ValueError("❌ Faltan las variables de entorno META_TOKEN o PHONE_NUMBER_ID")

WHATSAPP_API_URL = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"

def send_whatsapp_message(to, message):
    """Envía un mensaje de texto a través de la API de WhatsApp Cloud"""
    headers = {
        "Authorization": f"Bearer {META_TOKEN}",
        "Content-Type": "application/json"
    }
    data = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": message}
    }
    response = requests.post(WHATSAPP_API_URL, headers=headers, json=data)
    if response.status_code == 200:
        logger.info(f"✅ Mensaje enviado a {to}")
    else:
        logger.error(f"❌ Error al enviar mensaje a {to}: {response.text}")

def process_outbound_messages():
    """Procesa mensajes de la cola outbound en Redis"""
    while True:
        _, message_data = redis_client.blpop(QUEUE_NAME)
        try:
            message = json.loads(message_data)
            to = message["to"]
            body = message["body"]
            logger.info(f"📤 Procesando mensaje para {to}: {body}")
            send_whatsapp_message(to, body)
        except Exception as e:
            logger.error(f"⚠️ Error procesando mensaje: {e}")
        time.sleep(1)

if __name__ == "__main__":
    logger.info("🚀 Outbound Worker iniciado. Escuchando mensajes...")
    process_outbound_messages()
