# app.py - Bot WhatsApp Simple para Promociones
from flask import Flask, request, jsonify
import requests
import os
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# Configuración básica
META_TOKEN = os.getenv("META_TOKEN")
WABA_PHONE_ID = os.getenv("WABA_PHONE_ID") 
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
ADVISOR_NUMBER = os.getenv("ADVISOR_NUMBER", "5216682478005")  # Tu número

# URL de WhatsApp
WPP_API_URL = f"https://graph.facebook.com/v20.0/{WABA_PHONE_ID}/messages"

def send_message(to, text):
    """Envía mensaje de WhatsApp"""
    headers = {
        "Authorization": f"Bearer {META_TOKEN}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text},
    }
    
    try:
        response = requests.post(WPP_API_URL, headers=headers, json=payload)
        return response.status_code == 200
    except:
        return False

def notify_advisor(message):
    """Te notifica a ti cuando un cliente acepta"""
    send_message(ADVISOR_NUMBER, message)

# Endpoint para enviar promoción masiva
@app.post("/send-promo")
def send_promo():
    """Envía promoción de seguros de auto a tu cartera"""
    try:
        # Tu lista de clientes (puedes cambiarla)
        clients = [
            {"name": "Juan Pérez", "phone": "5211234567890"},
            {"name": "María García", "phone": "5210987654321"},
            # Agrega más clientes aquí...
        ]
        
        mensaje_promo = "🚗 *OFERTA EXCLUSIVA: 30% DESCUENTO EN SEGURO DE AUTO* 🚗\n\nHola {nombre}, tenemos una promoción especial solo para clientes preferentes:\n\n• 30% de descuento en seguro de auto\n• Cobertura completa\n• Pagos mensuales\n• Asistencia vial 24/7\n\n¿Te interesa conocer los detalles? Responde *SÍ* para más información."
        
        enviados = 0
        for client in clients:
            mensaje_personalizado = mensaje_promo.replace("{nombre}", client["name"])
            if send_message(client["phone"], mensaje_personalizado):
                enviados += 1
        
        return jsonify({
            "success": True,
            "message": f"Promoción enviada a {enviados} clientes",
            "total": len(clients)
        })
        
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

# Webhook para recibir respuestas
@app.get("/webhook")
def webhook_verify():
    """Verificación del webhook"""
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    
    if token == VERIFY_TOKEN:
        return challenge
    return "Error de verificación", 403

@app.post("/webhook")
def webhook_receive():
    """Recibe respuestas de los clientes"""
    try:
        data = request.get_json()
        
        # Extraer información del mensaje
        entry = data.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        value = changes.get("value", {})
        messages = value.get("messages", [])
        
        if not messages:
            return jsonify({"ok": True})
        
        message = messages[0]
        phone = message.get("from")  # Número del cliente
        text = message.get("text", {}).get("body", "").lower()  # Texto del mensaje
        
        print(f"📱 Mensaje de {phone}: {text}")
        
        # Detectar si el cliente acepta
        if any(palabra in text for palabra in ["sí", "si", "interesado", "me interesa", "quiero"]):
            # ¡Cliente interesado! Te notificamos
            mensaje_notificacion = f"🎯 *CLIENTE INTERESADO EN SEGURO DE AUTO* 🎯\n\nNúmero: {phone}\nMensaje: '{text}'\n\n¡Contacta de inmediato!"
            
            notify_advisor(mensaje_notificacion)
            
            # Responder al cliente
            send_message(phone, "¡Excelente! 🎉 Un asesor se pondrá en contacto contigo en los próximos minutos para darte todos los detalles de la promoción. Gracias por tu interés.")
        
        return jsonify({"ok": True})
        
    except Exception as e:
        print(f"Error en webhook: {e}")
        return jsonify({"ok": True})

# Endpoint de salud
@app.get("/health")
def health():
    return jsonify({"status": "ok", "service": "Bot Promocional Simple"})

if __name__ == "__main__":
    print("🚀 Bot WhatsApp Simple iniciado")
    print("📞 WhatsApp configurado:", bool(META_TOKEN and WABA_PHONE_ID))
    app.run(host="0.0.0.0", port=5000)
    
