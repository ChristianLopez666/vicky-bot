# app.py - VERSIÓN SIMPLIFICADA Y FUNCIONAL
from flask import Flask, request, jsonify
import os
import requests
import logging
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# Configuración básica
META_TOKEN = os.getenv("META_TOKEN")
WABA_PHONE_ID = os.getenv("WABA_PHONE_ID")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("vicky-simple")

# Respuestas predefinidas para seguros - DATOS REALES
SEGURO_RESPONSES = {
    "diferencia entre poliza amplia y limitada": """🚗 *DIFERENCIA ENTRE PÓLIZAS*

📋 *PÓLIZA AMPLIA* (Cobertura Completa):
• ✅ Daños a tu auto por accidente
• ✅ Robo total del vehículo  
• ✅ Responsabilidad civil a terceros
• ✅ Gastos médicos a ocupantes
• ✅ Asistencia vial 24/7
• ✅ Cristales y espejos

📋 *PÓLIZA LIMITADA* (Cobertura Básica):
• ✅ Responsabilidad civil a terceros
• ✅ Gastos médicos a ocupantes
• ❌ NO cubre daños a tu auto
• ❌ NO cubre robo

💡 *La diferencia principal:* La amplia protege tu auto, la limitada solo protege a terceros.

¿Quieres cotizar o más información?""",

    "que incluye la cobertura amplia plus": """🌟 *PÓLIZA AMPLIA PLUS* - Cobertura Premium

Incluye TODO de la póliza amplia MÁS:

✨ *Beneficios exclusivos:*
• 🚙 Auto sustituto por 15 días
• 🌎 Cobertura en USA y Canadá  
• 💰 Deducible $0 en primer incidente
• 🏨 Asistencia VIP en viajes
• 🔧 Mantenimiento preventivo
• 📱 App exclusiva

💎 *Ideal para:* Máxima protección con beneficios premium

¿Te interesa conocer costos?""",

    "que documentos necesito": """📄 *DOCUMENTOS PARA COTIZACIÓN*

Necesito:
• 📷 INE (frente)
• 🚗 Tarjeta de circulación 
• 🔢 Número de placas

¿Los tienes a la mano? Puedes enviarlos ahora.""",
}

MENU_PRINCIPAL = """🟦 *Vicky Bot — Inbursa*

Elige una opción:
1) Seguro de Auto 
2) Préstamo IMSS
3) Seguros Vida/Salud
4) Contactar asesor

Ejemplo: escribe '1' o 'seguro auto'"""

def send_whatsapp(to, text):
    """Envía mensaje por WhatsApp"""
    if not META_TOKEN or not WABA_PHONE_ID:
        return False
        
    url = f"https://graph.facebook.com/v20.0/{WABA_PHONE_ID}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text", 
        "text": {"body": text}
    }
    headers = {
        "Authorization": f"Bearer {META_TOKEN}",
        "Content-Type": "application/json"
    }
    
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        return response.status_code == 200
    except:
        return False

def procesar_mensaje(phone, text):
    """Procesa el mensaje del usuario - LÓGICA SIMPLE"""
    text_lower = text.lower().strip()
    
    # Detectar consultas sobre seguros
    if "diferencia" in text_lower and any(palabra in text_lower for palabra in ["amplia", "limitada", "poliza", "póliza"]):
        return SEGURO_RESPONSES["diferencia entre poliza amplia y limitada"]
        
    elif "amplia plus" in text_lower or "plus" in text_lower:
        return SEGURO_RESPONSES["que incluye la cobertura amplia plus"]
        
    elif "documento" in text_lower or "necesito" in text_lower:
        return SEGURO_RESPONSES["que documentos necesito"]
    
    # Comandos del menú
    elif text_lower in ["1", "seguro", "auto", "seguro auto"]:
        return """🚗 *Seguro de Auto*

Puedo ayudarte con:
• Información de coberturas
• Diferencias entre pólizas  
• Cotización

Pregunta cosas como:
• "¿Qué diferencia hay entre amplia y limitada?"
• "¿Qué cubre la amplia plus?" 
• "¿Qué documentos necesito?"

¿En qué te ayudo?"""
    
    elif text_lower in ["2", "imss", "préstamo"]:
        return "🏥 *Préstamo IMSS* - Un asesor te contactará para explicarte beneficios y requisitos."
    
    elif text_lower in ["3", "vida", "salud"]:
        return "🧬 *Seguros Vida/Salud* - Te conecto con nuestro especialista."
    
    elif text_lower in ["4", "asesor", "christian", "contactar"]:
        return "👨‍💼 *Contactando a Christian* - Te atenderá personalmente."
    
    elif text_lower in ["menu", "menú", "hola", "inicio"]:
        return MENU_PRINCIPAL
    
    else:
        return "❓ No entendí. Escribe 'menu' para ver opciones o pregunta sobre seguros de auto."

# WEBHOOKS SIMPLES
@app.get("/webhook")
def verify_webhook():
    if request.args.get("hub.verify_token") == VERIFY_TOKEN:
        return request.args.get("hub.challenge", ""), 200
    return "Error", 403

@app.post("/webhook")
def handle_webhook():
    try:
        data = request.get_json()
        
        # Extraer mensaje
        entry = data.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0] 
        value = changes.get("value", {})
        messages = value.get("messages", [])
        
        if not messages:
            return jsonify({"status": "ok"}), 200
            
        message = messages[0]
        phone = message.get("from")
        
        if message.get("type") == "text":
            text = message.get("text", {}).get("body", "")
            log.info(f"Mensaje de {phone}: {text}")
            
            # Procesar y responder
            respuesta = procesar_mensaje(phone, text)
            send_whatsapp(phone, respuesta)
            
        return jsonify({"status": "ok"}), 200
        
    except Exception as e:
        log.error(f"Error: {e}")
        return jsonify({"status": "error"}), 500

@app.get("/health")
def health():
    return jsonify({"status": "ok", "message": "Bot funcionando"})

if __name__ == "__main__":
    log.info("🚀 Bot Vicky iniciado - VERSIÓN SIMPLE")
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
