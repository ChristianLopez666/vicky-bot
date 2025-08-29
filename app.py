import os
from flask import Flask, request, make_response
import json

app = Flask(__name__)

# Token de verificación hardcodeado según especificaciones
VERIFY_TOKEN = "vicky-verify-token"

@app.route('/webhook', methods=['GET'])
def verify_webhook():
    """
    Endpoint de verificación para Meta (Facebook Developer Platform)
    Verifica el webhook según el protocolo oficial de WhatsApp Business API
    """
    print("🔍 Webhook GET - Solicitud de verificación recibida")
    
    # Extraer parámetros de la query string
    mode = request.args.get('hub.mode')
    token = request.args.get('hub.verify_token')
    challenge = request.args.get('hub.challenge')
    
    print(f"📋 Parámetros recibidos:")
    print(f"   - hub.mode: {mode}")
    print(f"   - hub.verify_token: {token}")
    print(f"   - hub.challenge: {challenge}")
    
    # Verificar que el mode sea 'subscribe' y el token coincida
    if mode == 'subscribe' and token == VERIFY_TOKEN:
        print("✅ Verificación exitosa - Token válido")
        
        # Crear respuesta con Content-Type text/plain y código 200
        response = make_response(challenge, 200)
        response.headers['Content-Type'] = 'text/plain'
        
        print(f"🔄 Respondiendo con challenge: {challenge}")
        return response
    else:
        print("❌ Verificación fallida - Token inválido o mode incorrecto")
        
        # Respuesta de error con código 403
        response = make_response("Verification failed", 403)
        response.headers['Content-Type'] = 'text/plain'
        
        return response

@app.route('/webhook', methods=['POST'])
def webhook_post():
    """
    Endpoint POST para recibir eventos de WhatsApp Business
    Registra el JSON recibido para futuro procesamiento
    """
    print("📨 Webhook POST - Evento de WhatsApp recibido")
    
    try:
        # Obtener datos JSON del request
        webhook_data = request.get_json()
        
        if webhook_data:
            print("📝 Datos del webhook recibidos:")
            print(json.dumps(webhook_data, indent=2, ensure_ascii=False))
        else:
            print("⚠️  No se recibieron datos JSON en el POST")
        
        # Respuesta exitosa requerida por WhatsApp API
        return make_response("EVENT_RECEIVED", 200)
        
    except Exception as e:
        print(f"🚨 Error procesando webhook POST: {str(e)}")
        # Incluso con error, responder 200 para evitar reintentos de Meta
        return make_response("EVENT_RECEIVED", 200)

@app.route('/health', methods=['GET'])
def health_check():
    """
    Endpoint de salud para verificar que el servicio está activo
    """
    print("💚 Health check solicitado")
    return make_response("Bot Vicky está activo y funcionando", 200)

@app.route('/')
def home():
    """
    Endpoint raíz básico
    """
    print("🏠 Acceso a endpoint raíz")
    return make_response("Bot Vicky Webhook - Servicio activo", 200)

if __name__ == '__main__':
    # Configuración para desarrollo local
    port = int(os.environ.get('PORT', 5000))
    print(f"🚀 Iniciando Bot Vicky en puerto {port}")
    print(f"🔑 Token de verificación configurado: {VERIFY_TOKEN}")
    print(f"🌐 URL del webhook: /webhook")
    
    app.run(host='0.0.0.0', port=port, debug=False)

