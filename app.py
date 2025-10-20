import os
import logging
import requests
from flask import Flask, request, jsonify, Response
import gspread
from google.oauth2 import service_account
from datetime import datetime
import time
import json
from threading import Thread

# Configuración de logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ==================== CONFIGURACIÓN CORREGIDA ====================
# VARIABLES DE ENTORNO - USAR ESTOS NOMBRES EXACTOS EN RENDER:
WHATSAPP_ACCESS_TOKEN = os.getenv('WHATSAPP_ACCESS_TOKEN')
WHATSAPP_VERIFY_TOKEN = os.getenv('WHATSAPP_VERIFY_TOKEN', 'vicky-verify-2025')
WHATSAPP_PHONE_NUMBER_ID = os.getenv('WHATSAPP_PHONE_NUMBER_ID')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
GOOGLE_SHEETS_CREDENTIALS = os.getenv('GOOGLE_SHEETS_CREDENTIALS')

# Verificar variables críticas (solo log, no detener ejecución)
if not WHATSAPP_ACCESS_TOKEN:
    logger.warning("⚠️ WHATSAPP_ACCESS_TOKEN no configurado - El bot no podrá enviar mensajes")
if not WHATSAPP_PHONE_NUMBER_ID:
    logger.warning("⚠️ WHATSAPP_PHONE_NUMBER_ID no configurado - El bot no podrá enviar mensajes")
if not OPENAI_API_KEY:
    logger.warning("⚠️ OPENAI_API_KEY no configurado - Se usarán plantillas básicas")

# ==================== BASE DE DATOS EN MEMORIA ====================
campañas_activas = {}
seguimiento_clientes = {}

# ==================== CONFIGURACIÓN GOOGLE SHEETS ====================
def inicializar_google_sheets():
    """Inicializa conexión con Google Sheets"""
    try:
        if GOOGLE_SHEETS_CREDENTIALS:
            creds_dict = json.loads(GOOGLE_SHEETS_CREDENTIALS)
            credentials = service_account.Credentials.from_service_account_info(creds_dict)
            client = gspread.authorize(credentials)
            sheet = client.open("SECOM").sheet1
            logger.info("✅ Google Sheets configurado correctamente")
            return sheet
        else:
            logger.warning("⚠️ GOOGLE_SHEETS_CREDENTIALS no configurado")
            return None
    except Exception as e:
        logger.error(f"❌ Error inicializando Google Sheets: {e}")
        return None

sheet = inicializar_google_sheets()

# ==================== FUNCIONES DE WHATSAPP ====================
def enviar_mensaje_whatsapp(numero, mensaje):
    """Envía mensaje por WhatsApp Business API"""
    try:
        if not WHATSAPP_ACCESS_TOKEN or not WHATSAPP_PHONE_NUMBER_ID:
            logger.error("❌ Faltan credenciales de WhatsApp - No se puede enviar mensaje")
            return False

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
        
        logger.info(f"📤 Enviando mensaje a {numero}: {mensaje[:50]}...")
        response = requests.post(url, headers=headers, json=payload, timeout=30)
        
        if response.status_code == 200:
            logger.info(f"✅ Mensaje enviado exitosamente a {numero}")
            return True
        else:
            logger.error(f"❌ Error enviando mensaje: {response.status_code} - {response.text}")
            return False
            
    except requests.exceptions.Timeout:
        logger.error("❌ Timeout enviando mensaje")
        return False
    except Exception as e:
        logger.error(f"❌ Error enviando mensaje WhatsApp: {e}")
        return False

# ==================== FUNCIONES GPT (VERSIÓN SEGURA) ====================
def get_gpt_response(prompt):
    """Obtiene respuesta usando OpenAI API (versión compatible)"""
    try:
        if not OPENAI_API_KEY:
            return None

        # Usar requests directamente para evitar problemas de compatibilidad
        url = "https://api.openai.com/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json"
        }
        data = {
            "model": "gpt-3.5-turbo",
            "messages": [
                {
                    "role": "system", 
                    "content": "Eres Vicky, asistente de SECOM. Usa un tono cálido, empático y cercano. Sé persuasiva pero no insistente. Responde en español."
                },
                {"role": "user", "content": prompt}
            ],
            "max_tokens": 150,
            "temperature": 0.8
        }
        
        response = requests.post(url, headers=headers, json=data, timeout=30)
        
        if response.status_code == 200:
            result = response.json()
            return result['choices'][0]['message']['content'].strip()
        else:
            logger.error(f"❌ Error API OpenAI: {response.status_code} - {response.text}")
            return None
            
    except Exception as e:
        logger.error(f"❌ Error GPT: {e}")
        return None

# ==================== FUNCIONES BASE DE DATOS ====================
def cargar_base_datos():
    """Carga clientes desde Google Sheets"""
    try:
        if not sheet:
            logger.warning("⚠️ Google Sheets no disponible, usando datos de prueba")
            return [
                {'nombre': 'Cliente Prueba', 'telefono': '5215551234567', 'producto_interes': 'seguro_auto', 'estado': 'Activo'}
            ]
        
        records = sheet.get_all_records()
        clientes = []
        
        for record in records:
            if record.get('Telefono') and record.get('Nombre'):
                # Formatear número (agregar prefijo si es necesario)
                telefono = str(record['Telefono']).strip()
                if not telefono.startswith('521'):
                    telefono = '521' + telefono.lstrip('1')
                
                clientes.append({
                    'nombre': record['Nombre'],
                    'telefono': telefono,
                    'producto_interes': record.get('Producto', ''),
                    'estado': record.get('Estado', 'Activo')
                })
        
        logger.info(f"📊 {len(clientes)} clientes cargados desde Google Sheets")
        return clientes
        
    except Exception as e:
        logger.error(f"❌ Error cargando BD: {e}")
        return []

def guardar_seguimiento(telefono, datos):
    """Guarda seguimiento en Google Sheets"""
    try:
        if sheet:
            # Buscar si ya existe el teléfono
            records = sheet.get_all_records()
            for i, record in enumerate(records, start=2):  # start=2 porque la fila 1 son headers
                if str(record.get('Telefono', '')).strip() == telefono:
                    # Actualizar existente
                    sheet.update(f'E{i}', [[datos['estado']]])
                    sheet.update(f'F{i}', [[datos['ultimo_envio'].strftime('%Y-%m-%d %H:%M:%S')]])
                    sheet.update(f'G{i}', [[datos['respuestas']]])
                    return
            
            # Agregar nuevo
            nueva_fila = [
                datos['nombre'],
                telefono,
                datos['campana'],
                datos['primer_envio'].strftime('%Y-%m-%d %H:%M:%S'),
                datos['estado'],
                datos['respuestas']
            ]
            sheet.append_row(nueva_fila)
            
    except Exception as e:
        logger.error(f"❌ Error guardando seguimiento: {e}")

# ==================== FUNCIONES CAMPAÑAS ====================
def generar_mensaje_personalizado(nombre_cliente, tipo_campana):
    """Genera mensaje personalizado usando GPT o plantilla"""
    
    plantillas = {
        "seguro_auto": f"""Hola {nombre_cliente} 😊, INBURSA te ofrece hasta un 60% de descuento en tu seguro de auto. 

Este descuento puede ser aprovechado para cualquier familiar que viva en tu domicilio.

¿Te gustaría que te envíe una cotización personalizada?""",

        "tarjeta_credito": f"""Hola {nombre_cliente} 💳, tenemos una promoción exclusiva de tarjetas de crédito para ti.

Sin anualidad el primer año y aprobación inmediata.

¿Puedo ayudarte con el proceso?""",

        "credito_personal": f"""Hola {nombre_cliente} 💰, ¿necesitas liquidez?

Tenemos créditos personales con tasas preferenciales para clientes SECOM.

¡Solicita hasta $100,000 sin aval!"""
    }
    
    plantilla_base = plantillas.get(tipo_campana, plantillas["seguro_auto"])
    
    # Intentar mejorar con GPT si está disponible
    mensaje_mejorado = get_gpt_response(f"""Mejora este mensaje comercial para que suene más cálido y natural:

Mensaje original: {plantilla_base}

Requisitos:
- Mantener la información clave
- Tono amigable y cercano
- Incluir el nombre: {nombre_cliente}
- Máximo 2 párrafos en español""")
    
    return mensaje_mejorado if mensaje_mejorado else plantilla_base

def ejecutar_campana_masiva(tipo_campana):
    """Ejecuta envío masivo para una campaña"""
    logger.info(f"🚀 Iniciando campaña: {tipo_campana}")
    
    clientes = cargar_base_datos()
    if not clientes:
        logger.error("❌ No hay clientes para la campaña")
        return
    
    campañas_activas[tipo_campana] = {
        'inicio': datetime.now(),
        'total_clientes': len(clientes),
        'enviados': 0,
        'errores': 0
    }
    
    for i, cliente in enumerate(clientes):
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
                
                guardar_seguimiento(cliente['telefono'], seguimiento_clientes[cliente['telefono']])
                campañas_activas[tipo_campana]['enviados'] += 1
                
            else:
                campañas_activas[tipo_campana]['errores'] += 1
            
            # Rate limiting (1 mensaje por segundo)
            if i < len(clientes) - 1:  # No esperar después del último
                time.sleep(1)
                
        except Exception as e:
            logger.error(f"❌ Error con cliente {cliente['telefono']}: {e}")
            campañas_activas[tipo_campana]['errores'] += 1
    
    logger.info(f"✅ Campaña {tipo_campana} completada: {campañas_activas[tipo_campana]['enviados']} enviados")

# ==================== WEBHOOK WHATSAPP ====================
@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    """Maneja webhook de WhatsApp"""
    
    if request.method == "GET":
        # Verificación del webhook
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        
        logger.info(f"🔐 Verificación webhook - Mode: {mode}, Token: {token}")
        
        if mode and token and mode == "subscribe" and token == WHATSAPP_VERIFY_TOKEN:
            logger.info("✅ Webhook verificado exitosamente")
            return Response(challenge, status=200, mimetype='text/plain')
        else:
            logger.error("❌ Verificación de webhook fallida")
            return Response("Verification failed", status=403)
    
    elif request.method == "POST":
        try:
            logger.info("📨 Webhook POST recibido")
            data = request.get_json()
            
            # Log simplificado para no saturar
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
            logger.error(f"❌ Error procesando webhook: {e}")
            return jsonify({"status": "error", "message": str(e)}), 500

def procesar_mensaje_entrante(message_data):
    """Procesa mensajes entrantes de WhatsApp"""
    try:
        messages = message_data.get("messages", [])
        if not messages:
            return
        
        message = messages[0]
        telefono = message.get("from")
        message_type = message.get("type")
        
        logger.info(f"📩 Mensaje recibido de {telefono}, tipo: {message_type}")
        
        if message_type == "text":
            texto = message.get("text", {}).get("body", "").lower()
            logger.info(f"💬 Texto recibido: {texto}")
            
            # Actualizar seguimiento
            if telefono in seguimiento_clientes:
                seguimiento_clientes[telefono]['respuestas'] += 1
                seguimiento_clientes[telefono]['ultimo_envio'] = datetime.now()
                seguimiento_clientes[telefono]['estado'] = 'respondio'
                guardar_seguimiento(telefono, seguimiento_clientes[telefono])
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
            
            # Respuesta automática inteligente
            responder_mensaje_cliente(telefono, texto)
            
    except Exception as e:
        logger.error(f"❌ Error procesando mensaje entrante: {e}")

def responder_mensaje_cliente(telefono, texto):
    """Genera respuesta automática para el cliente"""
    try:
        if any(palabra in texto for palabra in ['hola', 'hi', 'hello', 'buenas']):
            respuesta = "¡Hola! Soy Vicky de SECOM. 😊 ¿En qué puedo ayudarte hoy?"
        
        elif any(palabra in texto for palabra in ['si', 'sí', 'info', 'interesado', 'cuanto', 'cuesta', 'cotiz']):
            respuesta = "¡Me alegra tu interés! 💫 Te estoy conectando con un especialista que te dará todos los detalles. ¿Podrías compartirme tu email para enviarte información personalizada?"
        
        elif any(palabra in texto for palabra in ['no', 'gracias', 'ahora no', 'despues']):
            respuesta = "Entendido, ¡gracias por tu tiempo! 😊 Si cambias de opinión, aquí estaré para ayudarte. ¡Que tengas un excelente día! 🌟"
        
        elif any(palabra in texto for palabra in ['seguro', 'auto', 'carro']):
            respuesta = "¡Excelente! 🚗 Para el seguro de auto podemos ofrecerte hasta 60% de descuento. ¿Me compartes tu email para enviarte una cotización?"
        
        elif any(palabra in texto for palabra in ['tarjeta', 'credito', 'visa', 'mastercard']):
            respuesta = "¡Perfecto! 💳 Tenemos tarjetas sin anualidad el primer año. ¿Podrías decirme tu email para enviarte los requisitos?"
        
        elif any(palabra in texto for palabra in ['credito', 'prestamo', 'dinero']):
            respuesta = "¡Entendido! 💰 Para créditos personales manejamos tasas preferenciales. ¿Me compartes tu email para cotizar?"
        
        else:
            respuesta = "¡Hola! Soy Vicky de SECOM. 😊 ¿Te interesa conocer sobre: seguros de auto 🚗, tarjetas de crédito 💳 o créditos personales 💰?"
        
        # Enviar respuesta
        if enviar_mensaje_whatsapp(telefono, respuesta):
            logger.info(f"✅ Respuesta enviada a {telefono}")
        else:
            logger.error(f"❌ Error enviando respuesta a {telefono}")
            
    except Exception as e:
        logger.error(f"❌ Error en responder_mensaje_cliente: {e}")

# ==================== ENDPOINTS DE CONTROL ====================
@app.route("/")
def health_check():
    return jsonify({
        "status": "active",
        "service": "Vicky SECOM - Campañas Masivas",
        "clientes_seguimiento": len(seguimiento_clientes),
        "campañas_activas": len(campañas_activas),
        "whatsapp_configured": bool(WHATSAPP_ACCESS_TOKEN and WHATSAPP_PHONE_NUMBER_ID),
        "timestamp": datetime.now().isoformat()
    })

@app.route("/health")
def health():
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.now().isoformat()
    })

@app.route("/iniciar-campana/<tipo_campana>")
def iniciar_campana(tipo_campana):
    """Endpoint para iniciar campaña manualmente"""
    try:
        if tipo_campana not in ['seguro_auto', 'tarjeta_credito', 'credito_personal']:
            return jsonify({"error": "Tipo de campaña no válido"}), 400
        
        # Ejecutar en hilo separado
        thread = Thread(target=ejecutar_campana_masiva, args=(tipo_campana,))
        thread.daemon = True
        thread.start()
        
        return jsonify({
            "status": "campana_iniciada",
            "tipo": tipo_campana,
            "mensaje": "Campaña iniciada en segundo plano",
            "timestamp": datetime.now().isoformat()
        })
        
    except Exception as e:
        logger.error(f"❌ Error iniciando campaña: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/estado")
def estado_campana():
    """Muestra estado actual de campañas"""
    return jsonify({
        "campañas_activas": campañas_activas,
        "total_clientes_seguimiento": len(seguimiento_clientes),
        "timestamp": datetime.now().isoformat()
    })

@app.route("/test-mensaje", methods=["POST"])
def test_mensaje():
    """Endpoint para probar envío de mensajes"""
    try:
        data = request.json
        telefono = data.get('telefono')
        mensaje = data.get('mensaje', 'Mensaje de prueba de Vicky-SECOM')
        
        if not telefono:
            return jsonify({"error": "Número de teléfono requerido"}), 400
        
        if enviar_mensaje_whatsapp(telefono, mensaje):
            return jsonify({"status": "mensaje_enviado", "telefono": telefono})
        else:
            return jsonify({"error": "Error enviando mensaje"}), 500
            
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ==================== FINAL CORRECTO DEL ARCHIVO ====================
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    logger.info(f"🚀 Vicky SECOM - Sistema de Campañas Masivas")
    logger.info(f"🔧 Configuración:")
    logger.info(f"   WhatsApp Token: {'✅' if WHATSAPP_ACCESS_TOKEN else '❌'}")
    logger.info(f"   WhatsApp Phone ID: {'✅' if WHATSAPP_PHONE_NUMBER_ID else '❌'}")
    logger.info(f"   OpenAI: {'✅' if OPENAI_API_KEY else '❌'}")
    logger.info(f"   Google Sheets: {'✅' if sheet else '❌'}")
    logger.info(f"🌐 Servidor iniciado en puerto {port}")
    
    app.run(host='0.0.0.0', port=port, debug=False)
