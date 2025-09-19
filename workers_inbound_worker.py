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
    raise ValueError("❌ Faltan variables de entorno: META_TOKEN o PHONE_NUMBER_ID")

def send_whatsapp_message(to: str, text: str):
    """
    Envía un mensaje de texto a través de la API de WhatsApp Cloud.
    """
    url = f"https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {META_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text}
    }

    try:
        response = requests.post(url, headers=headers, json=payload)
        if response.status_code == 200:
            logger.info(f"✅ Mensaje enviado a {to}: {text}")
        else:
            logger.warning(f"⚠️ Error al enviar mensaje a {to}: {response.text}")
    except Exception as e:
        logger.error(f"❌ Excepción al enviar mensaje a {to}: {e}", exc_info=True)

def process_outbound(message: dict):
    """
    Procesa un mensaje saliente de la cola.
    """
    try:
        wa_id = message.get("wa_id")
        text = message.get("text")

        if not wa_id or not text:
            logger.warning("⚠️ Mensaje outbound inválido, falta wa_id o text")
            return

        logger.info(f"📤 Enviando mensaje a {wa_id}: {text}")
        send_whatsapp_message(wa_id, text)

    except Exception as e:
        logger.error(f"⚠️ Error procesando outbound: {e}", exc_info=True)

def main():
    logger.info("✅ Outbound Worker iniciado, escuchando cola...")

    while True:
        try:
            # Espera un nuevo mensaje en la cola outbound (blocking)
            _, raw_msg = redis_client.blpop(QUEUE_NAME)
            message = json.loads(raw_msg)

            process_outbound(message)

        except Exception as e:
            logger.error(f"❌ Error en loop principal outbound: {e}", exc_info=True)
            time.sleep(5)

if __name__ == "__main__":
    main()

