import os
import logging
import requests
from flask import Flask, request, jsonify, Response
from datetime import datetime
import time
import json
from threading import Thread

# ==================== CONFIGURACIÓN INICIAL ====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ==================== VARIABLES DE ENTORNO ====================
WHATSAPP_ACCESS_TOKEN = os.getenv('WHATSAPP_ACCESS_TOKEN')
WHATSAPP_VERIFY_TOKEN = os.getenv('WHATSAPP_VERIFY_TOKEN', 'vicky-verify-2025')
WHATSAPP_PHONE_NUMBER_ID = os.getenv('WHATSAPP_PHONE_NUMBER_ID')

# ==================== BASE DE DATOS EN MEMORIA ====================
campañas_activas = {}
seguimiento_clientes = {}
clientes_base = [
    {
        'nombre': 'Cliente Demo 1',
        'telefono': '5216681922865',
        'producto_interes': 'seguro_auto',
        'estado': 'Activo'
    },
    {
        'nombre': 'Cliente Demo 2', 
        'telefono': '5215551234567',
        'producto_interes': 'tarjeta_credito',
        'estado': 'Activo'
    }
]

# ==================== FUNCIONES PRINCIPALES ====================

def enviar_mensaje_whatsapp(numero, mensaje):
    """Envía mensaje por WhatsApp Business API"""
    try:
        # Verificar credenciales
        if not WHATSAPP_ACCESS_TOKEN:
            logger.error("❌ WHATSAPP_ACCESS_TOKEN no configurado")
            return False
        if not WHATSAPP_PHONE_NUMBER_ID:
            logger.error("❌ WHATSAPP_PHONE_NUMBER_ID no configurado")
            return False

        # Configurar solicitud
        url = f"https://graph.facebook.com/v18.0/{WHATSAPP_PHONE_NUMBER_ID}/messages"
        
        headers = {
            "Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "messaging_product": "whatsapp",
            "to": numero,
            "text": {"body": mensaje}
        }
        
        logger.info(f"📤 Enviando mensaje a {numero}")
        
        # Enviar mensaje
        response = requests.post(url, headers=headers, json=payload, timeout=30)
        
        if response.status_code == 200:
            logger.info(f"✅ Mensaje enviado exitosamente a {numero}")
            return True
        else:
            logger.error(f"❌ Error API WhatsApp: {response.status_code} - {response.text}")
            return False
            
    except requests.exceptions.Timeout:
        logger.error("❌ Timeout enviando mensaje a WhatsApp")
        return False
    except Exception as e:
        logger.error(f"❌ Error enviando mensaje: {e}")
        return False

def generar_mensaje_personalizado(nombre_cliente, tipo_campana):
    """Genera mensaje personalizado para campañas"""
    
    plantillas = {
        "seguro_auto": f"""¡Hola {nombre_cliente}! 😊 

INBURSA te ofrece hasta un 60% de descuento en tu seguro de auto. 

Este descuento puede ser aprovechado para cualquier familiar que viva en tu domicilio.

¿Te gustaría que te envíe una cotización personalizada?""",

        "tarjeta_credito": f"""¡Hola {nombre_cliente}! 💳 

Tenemos una promoción exclusiva de tarjetas de crédito para ti:

• Sin anualidad el primer año
• Aprobación inmediata  
• Límites preferenciales

¿Puedo ayudarte con el proceso?""",

        "credito_personal": f"""¡Hola {nombre_cliente}! 💰 

¿Necesitas liquidez? Tenemos créditos personales con:

• Tasas preferenciales para clientes SECOM
• Hasta $100,000 sin aval
• Desembolso en 24 horas

¡Solicita tu cotización sin compromiso!"""
    }
    
    return plantillas.get(tipo_campana, plantillas["seguro_auto"])

def ejecutar_campana_masiva(tipo_campana):
    """Ejecuta campaña masiva de mensajes"""
    logger.info(f"🚀 Iniciando campaña masiva: {tipo_campana}")
    
    campañas_activas[tipo_campana] = {
        'inicio': datetime.now(),
        'total_clientes': len(clientes_base),
        'enviados': 0,
        'errores': 0,
        'estado': 'en_progreso'
    }
    
    for i, cliente in enumerate(clientes_base):
        if cliente['estado'] != 'Activo':
            continue
            
        try:
            # Generar mensaje personalizado
            mensaje = generar_mensaje_personalizado(cliente['nombre'], tipo_campana)
            
            # Enviar mensaje
            if enviar_mensaje_whatsapp(cliente['telefono'], mensaje):
                # Registrar en seguimiento
                seguimiento_clientes[cliente['telefono']] = {
                    'nombre': cliente['nombre'],
                    'campana': tipo_campana,
                    'primer_envio': datetime.now(),
                    'ultimo_envio': datetime.now(),
                    'respuestas': 0,
                    'estado': 'enviado'
                }
                campañas_activas[tipo_campana]['enviados'] += 1
                logger.info(f"✅ Mensaje {i+1}/{len(clientes_base)} enviado a {cliente['nombre']}")
            else:
                campañas_activas[tipo_campana]['errores'] += 1
                logger.error(f"❌ Error enviando a {cliente['nombre']}")
            
            # Rate limiting (1 mensaje por segundo)
            if i < len(clientes_base) - 1:
                time.sleep(1)
                
        except Exception as e:
            logger.error(f"❌ Error con cliente {cliente['telefono']}: {e}")
            campañas_activas[tipo_campana]['errores'] += 1
    
    campañas_activas[tipo_campana]['estado'] = 'completada'
    campañas_activas[tipo_campana]['fin'] = datetime.now()
    logger.info(f"✅ Campaña {tipo_campana} completada: {campañas_activas[tipo_campana]['enviados']} enviados, {campañas_activas[tipo_campana]['errores']} errores")

# ==================== WEBHOOK WHATSAPP ====================

@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    """Maneja webhook de WhatsApp para verificación y mensajes"""
    
    if request.method == "GET":
        # Verificación del webhook
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        
        logger.info(f"🔐 Verificación webhook - Mode: {mode}, Token: {token}")
        
        if mode and token:
            if mode == "subscribe" and token == WHATSAPP_VERIFY_TOKEN:
                logger.info("✅ Webhook verificado exitosamente")
                return Response(challenge, status=200, mimetype='text/plain')
            else:
                logger.error("❌ Token de verificación incorrecto")
                return Response("Verification failed", status=403)
        else:
            logger.error("❌ Parámetros de verificación faltantes")
            return Response("Missing parameters", status=400)
    
    elif request.method == "POST":
        try:
            logger.info("📨 Webhook POST recibido")
            data = request.get_json()
            
            # Log simplificado del webhook
            if data and data.get("object") == "whatsapp_business_account":
                entries = data.get("entry", [])
                logger.info(f"📋 Procesando {len(entries)} entradas del webhook")
                
                for entry in entries:
                    for change in entry.get("changes", []):
                        if change.get("field") == "messages":
                            message_data = change.get("value", {})
                            procesar_mensaje_entrante(message_data)
            
            return jsonify({"status": "success"}), 200
            
        except Exception as e:
            logger.error(f"❌ Error procesando webhook POST: {e}")
            return jsonify({"status": "error", "message": str(e)}), 500

def procesar_mensaje_entrante(message_data):
    """Procesa mensajes entrantes de WhatsApp"""
    try:
        messages = message_data.get("messages", [])
        if not messages:
            logger.info("📭 No hay mensajes en la entrada")
            return
        
        message = messages[0]
        telefono = message.get("from")
        message_type = message.get("type")
        message_id = message.get("id")
        
        logger.info(f"📩 Mensaje recibido - ID: {message_id}, De: {telefono}, Tipo: {message_type}")
        
        if message_type == "text":
            texto = message.get("text", {}).get("body", "").lower()
            logger.info(f"💬 Texto recibido de {telefono}: {texto}")
            
            # Actualizar seguimiento si existe
            if telefono in seguimiento_clientes:
                seguimiento_clientes[telefono]['respuestas'] += 1
                seguimiento_clientes[telefono]['ultimo_envio'] = datetime.now()
                seguimiento_clientes[telefono]['estado'] = 'respondio'
            else:
                # Cliente nuevo que escribe espontáneamente
                seguimiento_clientes[telefono] = {
                    'nombre': 'Cliente Nuevo',
                    'campana': 'espontaneo',
                    'primer_envio': datetime.now(),
                    'ultimo_envio': datetime.now(),
                    'respuestas': 1,
                    'estado': 'respondio'
                }
            
            # Generar y enviar respuesta automática
            responder_mensaje_cliente(telefono, texto)
            
        else:
            logger.info(f"📎 Mensaje no textual recibido - Tipo: {message_type}")
            # Responder a mensajes no textuales
            enviar_mensaje_whatsapp(telefono, "¡Hola! Soy Vicky de SECOM. 😊 Por el momento solo puedo procesar mensajes de texto. ¿En qué puedo ayudarte?")
            
    except Exception as e:
        logger.error(f"❌ Error procesando mensaje entrante: {e}")

def responder_mensaje_cliente(telefono, texto):
    """Genera respuesta automática inteligente basada en el texto recibido"""
    try:
        texto_limpio = texto.lower().strip()
        
        # Lógica de respuestas inteligentes
        if any(palabra in texto_limpio for palabra in ['hola', 'hi', 'hello', 'buenas', 'saludos']):
            respuesta = """¡Hola! 👋 Soy Vicky, tu asistente virtual de SECOM. 

😊 ¿En qué puedo ayudarte hoy?

• 🚗 Seguros de auto
• 💳 Tarjetas de crédito  
• 💰 Créditos personales

Escribe la opción que te interesa."""

        elif any(palabra in texto_limpio for palabra in ['si', 'sí', 'info', 'información', 'interesado', 'cuanto', 'cuesta', 'cotiz', 'precio']):
            respuesta = """¡Me alegra tu interés! 💫 

Te estoy conectando con uno de nuestros especialistas que te dará todos los detalles y precios personalizados.

¿Podrías compartirme tu email para enviarte información completa?"""

        elif any(palabra in texto_limpio for palabra in ['seguro', 'auto', 'carro', 'vehículo', 'coche']):
            respuesta = """¡Excelente elección! 🚗 

Para el seguro de auto podemos ofrecerte:
• Hasta 60% de descuento
• Cobertura para familiares en tu domicilio
• Asistencia vial 24/7

¿Me compartes tu email para enviarte una cotización personalizada?"""

        elif any(palabra in texto_limpio for palabra in ['tarjeta', 'credito', 'visa', 'mastercard', 'débito']):
            respuesta = """¡Perfecto! 💳 

Nuestras tarjetas incluyen:
• Sin anualidad el primer año
• Aprobación inmediata
• Programa de recompensas

¿Podrías decirme tu email para enviarte los requisitos y beneficios completos?"""

        elif any(palabra in texto_limpio for palabra in ['credito', 'préstamo', 'dinero', 'financiamiento', 'liquidez']):
            respuesta = """¡Entendido! 💰 

Para créditos personales manejamos:
• Tasas preferenciales
• Hasta $100,000 sin aval
• Desembolso en 24 horas

¿Me compartes tu email para cotizar sin compromiso?"""

        elif any(palabra in texto_limpio for palabra in ['no', 'gracias', 'ahora no', 'despues', 'luego']):
            respuesta = """Entendido, ¡gracias por tu tiempo! 😊 

Si cambias de opinión, aquí estaré para ayudarte con:
• Seguros de auto 🚗
• Tarjetas de crédito 💳
• Créditos personales 💰

¡Que tengas un excelente día! 🌟"""

        elif any(palabra in texto_limpio for palabra in ['email', 'correo', 'mail']):
            respuesta = """📧 Perfecto, he registrado tu solicitud. 

Un especialista de SECOM se pondrá en contacto contigo en las próximas horas con la información completa.

Mientras tanto, ¿hay algo más en lo que pueda ayudarte?"""

        else:
            respuesta = """¡Hola! Soy Vicky de SECOM. 😊 

¿Te interesa conocer sobre alguna de estas opciones?

🚗 **Seguros de auto** - Hasta 60% descuento
💳 **Tarjetas de crédito** - Sin anualidad 1er año  
💰 **Créditos personales** - Hasta $100,000 sin aval

Solo dime qué te interesa para ayudarte mejor."""

        # Enviar respuesta
        if enviar_mensaje_whatsapp(telefono, respuesta):
            logger.info(f"✅ Respuesta enviada exitosamente a {telefono}")
        else:
            logger.error(f"❌ Error enviando respuesta a {telefono}")
            
    except Exception as e:
        logger.error(f"❌ Error en responder_mensaje_cliente: {e}")

# ==================== ENDPOINTS DE CONTROL ====================

@app.route("/")
def home():
    """Endpoint principal de salud"""
    return jsonify({
        "status": "active",
        "service": "Vicky SECOM - WhatsApp Business Bot",
        "timestamp": datetime.now().isoformat(),
        "webhook_url": "https://vicky-bot-x6wt.onrender.com/webhook",
        "whatsapp_configured": bool(WHATSAPP_ACCESS_TOKEN and WHATSAPP_PHONE_NUMBER_ID),
        "clientes_seguimiento": len(seguimiento_clientes),
        "campañas_activas": len([c for c in campañas_activas.values() if c.get('estado') == 'en_progreso'])
    })

@app.route("/health")
def health():
    """Endpoint de salud simplificado"""
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "whatsapp_configured": bool(WHATSAPP_ACCESS_TOKEN and WHATSAPP_PHONE_NUMBER_ID)
    })

@app.route("/debug")
def debug():
    """Endpoint de diagnóstico"""
    return jsonify({
        "whatsapp_access_token_length": len(WHATSAPP_ACCESS_TOKEN or ""),
        "whatsapp_phone_number_id": WHATSAPP_PHONE_NUMBER_ID,
        "verify_token": WHATSAPP_VERIFY_TOKEN,
        "whatsapp_fully_configured": bool(WHATSAPP_ACCESS_TOKEN and WHATSAPP_PHONE_NUMBER_ID),
        "total_clientes_base": len(clientes_base),
        "total_seguimiento": len(seguimiento_clientes),
        "campañas_activas": campañas_activas
    })

@app.route("/iniciar-campana/<tipo_campana>", methods=["POST"])
def iniciar_campana(tipo_campana):
    """Endpoint para iniciar campaña manualmente"""
    try:
        tipos_validos = ['seguro_auto', 'tarjeta_credito', 'credito_personal']
        
        if tipo_campana not in tipos_validos:
            return jsonify({
                "error": "Tipo de campaña no válido",
                "tipos_permitidos": tipos_validos
            }), 400
        
        # Verificar que no hay campaña en progreso
        campaña_en_progreso = any(
            campana.get('estado') == 'en_progreso' 
            for campana in campañas_activas.values()
        )
        
        if campaña_en_progreso:
            return jsonify({
                "error": "Ya hay una campaña en progreso",
                "campaña_activa": True
            }), 409
        
        # Ejecutar campaña en hilo separado
        thread = Thread(target=ejecutar_campana_masiva, args=(tipo_campana,))
        thread.daemon = True
        thread.start()
        
        return jsonify({
            "status": "campana_iniciada",
            "tipo": tipo_campana,
            "mensaje": "Campaña iniciada en segundo plano",
            "total_clientes": len(clientes_base),
            "timestamp": datetime.now().isoformat()
        })
        
    except Exception as e:
        logger.error(f"❌ Error iniciando campaña: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/estado-campanas")
def estado_campanas():
    """Endpoint para ver estado de campañas"""
    return jsonify({
        "campañas_activas": campañas_activas,
        "total_campañas": len(campañas_activas),
        "campañas_en_progreso": len([c for c in campañas_activas.values() if c.get('estado') == 'en_progreso']),
        "seguimiento_clientes": seguimiento_clientes,
        "timestamp": datetime.now().isoformat()
    })

@app.route("/test-mensaje", methods=["POST"])
def test_mensaje():
    """Endpoint para probar envío de mensajes"""
    try:
        data = request.get_json()
        
        if not data:
            return jsonify({"error": "JSON body requerido"}), 400
        
        telefono = data.get('telefono')
        mensaje = data.get('mensaje', 'Este es un mensaje de prueba de Vicky-SECOM Bot')
        
        if not telefono:
            return jsonify({"error": "Número de teléfono requerido"}), 400
        
        # Validar formato de teléfono
        if not telefono.startswith('521'):
            telefono = '521' + telefono.lstrip('1')
        
        logger.info(f"🧪 Enviando mensaje de prueba a {telefono}")
        
        if enviar_mensaje_whatsapp(telefono, mensaje):
            return jsonify({
                "status": "mensaje_enviado",
                "telefono": telefono,
                "mensaje": mensaje,
                "timestamp": datetime.now().isoformat()
            })
        else:
            return jsonify({
                "error": "No se pudo enviar el mensaje",
                "telefono": telefono
            }), 500
            
    except Exception as e:
        logger.error(f"❌ Error en test-mensaje: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/webhook/test", methods=["GET"])
def test_webhook():
    """Endpoint para probar verificación de webhook"""
    return jsonify({
        "webhook_url": "https://vicky-bot-x6wt.onrender.com/webhook",
        "verify_token": WHATSAPP_VERIFY_TOKEN,
        "verification_url": f"https://vicky-bot-x6wt.onrender.com/webhook?hub.mode=subscribe&hub.verify_token={WHATSAPP_VERIFY_TOKEN}&hub.challenge=TEST123"
    })

# ==================== MANEJO DE ERRORES ====================

@app.errorhandler(404)
def not_found(error):
    return jsonify({"error": "Endpoint no encontrado"}), 404

@app.errorhandler(500)
def internal_error(error):
    return jsonify({"error": "Error interno del servidor"}), 500

# ==================== INICIALIZACIÓN ====================

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    
    logger.info("🚀" * 50)
    logger.info("🚀 VICKY SECOM - BOT INICIADO")
    logger.info("🚀" * 50)
    logger.info(f"🔧 Configuración:")
    logger.info(f"   ✅ Webhook: https://vicky-bot-x6wt.onrender.com/webhook")
    logger.info(f"   ✅ WhatsApp Token: {'CONFIGURADO' if WHATSAPP_ACCESS_TOKEN else 'NO CONFIGURADO'}")
    logger.info(f"   ✅ WhatsApp Phone ID: {'CONFIGURADO' if WHATSAPP_PHONE_NUMBER_ID else 'NO CONFIGURADO'}")
    logger.info(f"   ✅ Verify Token: {WHATSAPP_VERIFY_TOKEN}")
    logger.info(f"🌐 Servidor iniciado en puerto {port}")
    logger.info("📱 Listo para recibir mensajes de WhatsApp...")
    
    app.run(host='0.0.0.0', port=port, debug=False)

