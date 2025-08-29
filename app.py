# app.py
# -*- coding: utf-8 -*-
"""
Webhook de WhatsApp Business (Meta) con Flask
Listo para desplegar en Render.com (Python 3.11+)

Rutas:
- GET  /webhook  -> Validación del webhook (Meta)
- POST /webhook  -> Recepción de eventos entrantes
- GET  /health   -> Verificación de estado

Variables de entorno requeridas:
- VERIFY_TOKEN
"""

import os
import json
import logging
from flask import Flask, request, Response

app = Flask(__name__)

# Configurar logging para entornos como Render (stdout)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
# Opcional: ajustar el logger de werkzeug (servidor de desarrollo)
logging.getLogger("werkzeug").setLevel(logging.INFO)


@app.route("/webhook", methods=["GET"])
def verify_webhook():
    """
    Validación oficial de Meta:
    - Lee hub.mode, hub.verify_token y hub.challenge
    - Si son correctos, devuelve el challenge (texto plano) con HTTP 200
    - Si no, devuelve "Verification failed" con HTTP 403
    """
    verify_token_env = os.environ.get("VERIFY_TOKEN", "")

    mode = request.args.get("hub.mode", "")
    token = request.args.get("hub.verify_token", "")
    challenge = request.args.get("hub.challenge", "")

    token_ok = (token == verify_token_env)
    if mode == "subscribe" and token_ok:
        logging.info("✅ Webhook verificado correctamente.")
        # Responder el challenge en texto plano
        return Response(challenge, status=200, content_type="text/plain; charset=utf-8")

    logging.warning(
        "❌ Fallo en la verificación del webhook | mode=%s | token_ok=%s",
        mode, token_ok
    )
    return Response("Verification failed", status=403, content_type="text/plain; charset=utf-8")


@app.route("/webhook", methods=["POST"])
def receive_event():
    """
    Recepción de eventos (mensajes) desde Meta.
    - Acepta JSON entrante
    - Registra el contenido con nivel INFO
    - Responde siempre 'EVENT_RECEIVED' con HTTP 200
    """
    data = request.get_json(silent=True) or {}
    try:
        pretty = json.dumps(data, ensure_ascii=False)
    except Exception:
        # Fallback si hubiera datos no serializables
        pretty = str(data)

    logging.info("📩 Evento entrante: %s", pretty)
    return Response("EVENT_RECEIVED", status=200, content_type="text/plain; charset=utf-8")


@app.route("/health", methods=["GET"])
def health():
    """Ruta de salud para monitoreo básico."""
    return Response("Vicky está viva 🟢", status=200, content_type="text/plain; charset=utf-8")


if __name__ == "__main__":
    # Render expone el puerto vía la variable de entorno PORT
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

# Solo para pruebas locales
if __name__ == '__main__':
    app.run(debug=True)

