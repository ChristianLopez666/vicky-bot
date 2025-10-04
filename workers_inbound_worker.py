import os
import redis
import json
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("inbound_worker")

# Obtener REDIS_URL del entorno
REDIS_URL = os.getenv("REDIS_URL")
QUEUE_NAME = "inbound_messages"

if not REDIS_URL:
    raise ValueError("❌ No se encontró la variable de entorno REDIS_URL")

# Conexión segura con SSL
redis_client = redis.Redis.from_url(REDIS_URL, ssl=True)

def process_inbound_messages():
    while True:
        _, message_data = redis_client.blpop(QUEUE_NAME)
        message = json.loads(message_data)
        logger.info(f"📥 Recibido: {message}")
        # aquí va la lógica de procesamiento

if __name__ == "__main__":
    logger.info("🚀 Inbound Worker iniciado. Escuchando mensajes...")
    process_inbound_messages()

