import os
import logging
from flask import Flask, jsonify

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ==================== DIAGNÓSTICO ====================
@app.route("/debug")
def debug():
    """Endpoint para debug de variables"""
    variables = {
        "WHATSAPP_ACCESS_TOKEN": os.getenv('WHATSAPP_ACCESS_TOKEN'),
        "WHATSAPP_PHONE_NUMBER_ID": os.getenv('WHATSAPP_PHONE_NUMBER_ID'),
        "WHATSAPP_VERIFY_TOKEN": os.getenv('WHATSAPP_VERIFY_TOKEN'),
        "TODAS_LAS_VARIABLES": dict(os.environ)
    }
    
    logger.info(f"🔧 Variables: {variables}")
    
    return jsonify({
        "whatsapp_token_length": len(variables["WHATSAPP_ACCESS_TOKEN"] or ""),
        "phone_number_id": variables["WHATSAPP_PHONE_NUMBER_ID"],
        "verify_token": variables["WHATSAPP_VERIFY_TOKEN"],
        "whatsapp_configured": bool(variables["WHATSAPP_ACCESS_TOKEN"] and variables["WHATSAPP_PHONE_NUMBER_ID"])
    })

@app.route("/")
def health():
    return jsonify({"status": "active"})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)

