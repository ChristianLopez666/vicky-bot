import os
import logging
import requests
from flask import Flask, request, jsonify
import gspread
from google.oauth2 import service_account
import openai
from datetime import datetime, timedelta
import time
import json
from threading import Thread

# Configuración
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Variables de entorno
META_ACCESS_TOKEN = os.getenv('META_ACCESS_TOKEN')
VERIFY_TOKEN = os.getenv('VERIFY_TOKEN', 'vicky-verify-2025')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')

# Configurar OpenAI
openai.api_key = OPENAI_API_KEY

# Base de datos de campañas y seguimientos
campañas_activas = {}
seguimiento_clientes = {}

# Configuración de Google Sheets
sheet = None
try:
    GOOGLE_SHEETS_CREDENTIALS = os.getenv('GOOGLE_SHEETS_CREDENTIALS')
    if GOOGLE_SHEETS_CREDENTIALS:
        creds_dict = json.loads(GOOGLE_SHEETS_CREDENTIALS)
        credentials = service_account.Credentials.from_service_account_info(creds_dict)
        client = gspread.authorize(credentials)
        sheet = client.open("SECOM").sheet1
        logger.info("✅ Google Sheets configurado")
except Exception as e:
    logger.error(f"❌ Error Sheets: {e}")

def get_gpt_response(prompt):
    """Obtiene respuesta con tono cálido de GPT"""
    try:
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "Eres Vicky, asistente de SECOM. Usa un tono cálido, empático y cercano. Sé persuasiva pero no insistente."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=150,
            temperature=0.8
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"Error GPT: {e}")
        return None

def enviar_mensaje_whatsapp(numero, mensaje):
    """Envía mensaje por WhatsApp Business API"""
    try:
        url = "https://graph.facebook.com/v17.0/118469193281675/messages"
        headers = {
            "Authorization": f"Bearer {META_ACCESS_TOKEN}",
            "Content-Type": "application/json"
        }
        data = {
            "messaging_product": "whatsapp",
            "to": numero,
            "text": {"body": mensaje}
        }
        
        response = requests.post(url, headers=headers, json=data)
        if response.status_code == 200:
            logger.info(f"✅ Mensaje enviado a {numero}")
            return True
        else:
            logger.error(f"❌ Error enviando mensaje: {response.text}")
            return False
    except Exception as e:
        logger.error(f"❌ Error API WhatsApp: {e}")
        return False

def cargar_base_datos():
    """Carga clientes desde Google Sheets"""
    try:
        if not sheet:
            return []
        
        records = sheet.get_all_records()
        clientes = []
        
        for record in records:
            if record.get('Telefono') and record.get('Nombre'):
                clientes.append({
                    'nombre': record['Nombre'],
                    'telefono': record['Telefono'],
                    'producto_interes': record.get('Producto', ''),
                    'estado': record.get('Estado', 'Activo')
                })
        
        logger.info(f"📊 {len(clientes)} clientes cargados")
        return clientes
        
    except Exception as e:
        logger.error(f"❌ Error cargando BD: {e}")
        return []

def generar_mensaje_personalizado(nombre_cliente, tipo_campana):
    """Genera mensaje personalizado usando GPT"""
    
    plantillas = {
        "seguro_auto": f"""Hola {nombre_cliente}, INBURSA te ofrece hasta un 60% de descuento en tu seguro de auto. 

Este descuento puede ser aprovechado para cualquier familiar que viva en tu domicilio.

¿Te gustaría que te envíe una cotización personalizada?""",

        "tarjeta_credito": f"""Hola {nombre_cliente}, tenemos una promoción exclusiva de tarjetas de crédito para ti.

Sin anualidad el primer año y aprobación inmediata.

¿Puedo ayudarte con el proceso?""",

        "credito_personal": f"""Hola {nombre_cliente}, ¿necesitas liquidez?

Tenemos créditos personales con tasas preferenciales para clientes SECOM.

¡Solicita hasta $100,000 sin aval!"""
    }
    
    # Usar plantilla base y mejorar con GPT
    plantilla_base = plantillas.get(tipo_campana, plantillas["seguro_auto"])
    
    prompt = f"""Mejora este mensaje comercial para que suene más cálido y natural, manteniendo la esencia del mensaje original:

Mensaje original: {plantilla_base}

Requisitos:
- Mantener la información clave del descuento/promoción
- Usar un tono amigable y cercano
- Incluir el nombre del cliente: {nombre_cliente}
- Máximo 2 párrafos"""

    mensaje_mejorado = get_gpt_response(prompt)
    
    return mensaje_mejorado if mensaje_mejorado else plantilla_base

def ejecutar_campana_masiva(tipo_campana):
    """Ejecuta envío masivo para una campaña"""
    logger.info(f"🚀 Iniciando campaña: {tipo_campana}")
    
    clientes = cargar_base_datos()
    if not clientes:
        logger.error("❌ No hay clientes para la campaña")
        return
    
    for cliente in clientes:
        if cliente['estado'] != 'Activo':
            continue
            
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
            
            # Programar recordatorios
            programar_recordatorio(cliente['telefono'], 3)  # 3 días
            programar_recordatorio(cliente['telefono'], 5)  # 5 días
        
        time.sleep(2)  # Rate limiting

def programar_recordatorio(telefono, dias_despues):
    """Programa recordatorio automático"""
    def enviar_recordatorio():
        time.sleep(dias_despues * 24 * 3600)  # Esperar X días
        
        cliente = seguimiento_clientes.get(telefono)
        if cliente and cliente['respuestas'] == 0:  # Solo si no ha respondido
            
            if dias_despues == 3:
                mensaje = f"Hola {cliente['nombre']}, solo pasaba para recordarte nuestra promoción especial. ¿Tienes alguna pregunta sobre el {cliente['campana']}?"
            else:  # día 5 - cierre de embudo
                mensaje = f"Entiendo que por el momento no estás interesado/a {cliente['nombre']}. Estaremos a la orden para cuando quieras una propuesta. ¡Que tengas un excelente día!"
            
            if enviar_mensaje_whatsapp(telefono, mensaje):
                seguimiento_clientes[telefono]['ultimo_envio'] = datetime.now()
                
                if dias_despues == 5:
                    seguimiento_clientes[telefono]['estado'] = 'cerrado'
    
    thread = Thread(target=enviar_recordatorio)
    thread.daemon = True
    thread.start()

# WEBHOOK para respuestas de clientes
@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    """Maneja respuestas de clientes"""
    
    if request.method == "GET":
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        
        if mode and token and mode == "subscribe" and token == VERIFY_TOKEN:
            logger.info("✅ Webhook verificado")
            from flask import Response
            return Response(challenge, mimetype='text/plain')
        return "Verification failed", 403
    
    elif request.method == "POST":
        try:
            data = request.get_json()
            
            if data.get("object") == "whatsapp_business_account":
                for entry in data.get("entry", []):
                    for change in entry.get("changes", []):
                        if change.get("field") == "messages":
                            message_data = change.get("value", {})
                            procesar_respuesta_cliente(message_data)
            
            return jsonify({"status": "success"}), 200
            
        except Exception as e:
            logger.error(f"❌ Error webhook: {e}")
            return jsonify({"status": "error"}), 500

def procesar_respuesta_cliente(message_data):
    """Procesa respuestas de clientes"""
    try:
        messages = message_data.get("messages", [])
        if not messages:
            return
        
        message = messages[0]
        telefono = message.get("from")
        message_type = message.get("type")
        
        if message_type == "text":
            texto = message.get("text", {}).get("body", "").lower()
            
            # Actualizar seguimiento
            if telefono in seguimiento_clientes:
                seguimiento_clientes[telefono]['respuestas'] += 1
                seguimiento_clientes[telefono]['ultimo_envio'] = datetime.now()
                seguimiento_clientes[telefono]['estado'] = 'respondio'
            
            # Responder automáticamente
            if any(palabra in texto for palabra in ['si', 'sí', 'info', 'interesado', 'cuanto', 'cuesta']):
                respuesta = "¡Me alegra tu interés! Te conecto con un especialista para que te dé todos los detalles. ¿Puedes compartirme tu email para enviarte la información?"
                enviar_mensaje_whatsapp(telefono, respuesta)
            elif any(palabra in texto for palabra in ['no', 'gracias', 'ahora no']):
                respuesta = "Entendido, gracias por tu tiempo. Si cambias de opinión, aquí estaré para ayudarte. ¡Que tengas un excelente día!"
                enviar_mensaje_whatsapp(telefono, respuesta)
                if telefono in seguimiento_clientes:
                    seguimiento_clientes[telefono]['estado'] = 'no_interesado'
            
    except Exception as e:
        logger.error(f"❌ Error procesando respuesta: {e}")

# Endpoints de control - CORREGIDOS (sin ñ)
@app.route("/")
def health_check():
    return jsonify({
        "status": "active",
        "service": "Vicky SECOM - Campañas Masivas",
        "clientes_seguimiento": len(seguimiento_clientes),
        "timestamp": datetime.now().isoformat()
    })

@app.route("/iniciar-campana/<tipo_campana>")
def iniciar_campana(tipo_campana):
    """Endpoint para iniciar campaña manualmente"""
    try:
        thread = Thread(target=ejecutar_campana_masiva, args=(tipo_campana,))
        thread.daemon = True
        thread.start()
        
        return jsonify({
            "status": "campana_iniciada",
            "tipo": tipo_campana,
            "mensaje": "Campaña en proceso en segundo plano"
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/estado-campana")
def estado_campana():
    """Muestra estado actual de campañas"""
    return jsonify({
        "seguimiento_clientes": seguimiento_clientes,
        "total_clientes": len(seguimiento_clientes)
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    logger.info(f"🚀 Vicky SECOM - Sistema de Campañas Masivas iniciado en puerto {port}")
    app.run(host='0.0.0.0', port=port, debug=False)

