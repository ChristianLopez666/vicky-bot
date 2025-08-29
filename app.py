import os
import logging
from flask import Flask, request, jsonify

# Configuración del logging para Render
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Crear instancia de Flask
app = Flask(__name__)

@app.route('/webhook', methods=['GET'])
def verify_webhook():
    """
    Validación del webhook según el proceso oficial de Meta.
    Verifica hub.mode, hub.verify_token y retorna hub.challenge si es válido.
    """
    try:
        # Obtener parámetros de la query string
        hub_mode = request.args.get('hub.mode')
        hub_verify_token = request.args.get('hub.verify_token')
        hub_challenge = request.args.get('hub.challenge')
        
        # Obtener el token de verificación desde variables de entorno
        verify_token = os.getenv('VERIFY_TOKEN')
        
        logger.info(f"Validación webhook - Mode: {hub_mode}, Token recibido: {hub_verify_token}")
        
        # Verificar que el modo sea 'subscribe' y el token coincida
        if hub_mode == 'subscribe' and hub_verify_token == verify_token:
            logger.info("Validación exitosa del webhook")
            # Retornar el challenge como texto plano con código 200
            return hub_challenge, 200
        else:
            logger.warning("Validación fallida del webhook")
            return "Verification failed", 403
            
    except Exception as e:
        logger.error(f"Error durante la validación del webhook: {str(e)}")
        return "Verification failed", 403

@app.route('/webhook', methods=['POST'])
def receive_webhook():
    """
    Recibe y procesa los eventos entrantes de WhatsApp Business.
    Registra el contenido y responde con EVENT_RECEIVED.
    """
    try:
        # Obtener el JSON entrante
        webhook_data = request.get_json()
        
        # Loggear el contenido completo
        logger.info(f"Evento WhatsApp recibido: {webhook_data}")
        
        # Responder siempre con EVENT_RECEIVED y código 200
        return "EVENT_RECEIVED", 200
        
    except Exception as e:
        logger.error(f"Error procesando webhook POST: {str(e)}")
        # Incluso en caso de error, responder 200 según las mejores prácticas
        return "EVENT_RECEIVED", 200

@app.route('/health', methods=['GET'])
def health_check():
    """
    Endpoint de verificación de estado del servidor.
    Retorna un mensaje indicando que Vicky está activa.
    """
    logger.info("Health check solicitado")
    return "Vicky está viva 🟢", 200

@app.errorhandler(404)
def not_found(error):
    """Manejador para rutas no encontradas."""
    logger.warning(f"Ruta no encontrada: {request.path}")
    return "Ruta no encontrada", 404

@app.errorhandler(500)
def internal_error(error):
    """Manejador para errores internos del servidor."""
    logger.error(f"Error interno del servidor: {str(error)}")
    return "Error interno del servidor", 500

if __name__ == '__main__':
    # Configuración para desarrollo local
    port = int(os.environ.get('PORT', 5000))
    debug_mode = os.environ.get('FLASK_DEBUG', 'False').lower() == 'true'
    
    logger.info(f"Iniciando Vicky Bot en puerto {port}")
    logger.info(f"Modo debug: {debug_mode}")
    
    # Para Render.com, la app se ejecuta con gunicorn
    # Esta configuración es para desarrollo local
    app.run(host='0.0.0.0', port=port, debug=debug_mode)
