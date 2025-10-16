# app.py — Vicky SECOM
# Versión: 2025-10-15
# Objetivo: Bot SECOM basado en la estructura de Vicky Bot, con:
#  - Integración GPT para tono cálido
#  - WhatsApp Cloud API (Meta)
#  - Google Sheets (Prospectos SECOM Auto)
#  - Google Drive (respaldo de archivos por cliente)
#  - Flujos SECOM: Renovación, Documentos Auto, Promos, Seguimiento, IMSS, VRIM, Contacto
#  - Envíos asíncronos con threads (evita 502 en /ext/send-promo)
#  - Recordatorios (-30 días) y Reintentos (+7 días)

import os
import logging
import requests
from flask import Flask, request, jsonify
import gspread
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
import openai
import io
from datetime import datetime
import re
import json

# Configuración de logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Configuración de variables de entorno
META_ACCESS_TOKEN = os.getenv('META_ACCESS_TOKEN')
VERIFY_TOKEN = os.getenv('VERIFY_TOKEN', 'vicky-verify-2025')  # Valor por defecto
ADVISOR_NUMBER = os.getenv('ADVISOR_NUMBER')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')

# Configurar OpenAI
openai.api_key = OPENAI_API_KEY

# Inicializar servicios de Google (opcionales para que funcione sin ellos)
sheet = None
drive_service = None

try:
    # Configuración de Google Sheets
    GOOGLE_SHEETS_CREDENTIALS = os.getenv('GOOGLE_SHEETS_CREDENTIALS')
    if GOOGLE_SHEETS_CREDENTIALS:
        creds_dict = json.loads(GOOGLE_SHEETS_CREDENTIALS)
        credentials = service_account.Credentials.from_service_account_info(creds_dict)
        scoped_credentials = credentials.with_scopes([
            'https://www.googleapis.com/auth/spreadsheets',
            'https://www.googleapis.com/auth/drive'
        ])
        client = gspread.authorize(scoped_credentials)
        sheet = client.open("Prospectos SECOM Auto").sheet1
        logger.info("Google Sheets configurado exitosamente")
except Exception as e:
    logger.warning(f"Google Sheets no configurado: {e}")

try:
    # Configuración de Google Drive
    GOOGLE_DRIVE_CREDENTIALS = os.getenv('GOOGLE_DRIVE_CREDENTIALS')
    if GOOGLE_DRIVE_CREDENTIALS:
        creds_dict = json.loads(GOOGLE_DRIVE_CREDENTIALS)
        credentials = service_account.Credentials.from_service_account_info(creds_dict)
        drive_service = build('drive', 'v3', credentials=credentials)
        logger.info("Google Drive configurado exitosamente")
except Exception as e:
    logger.warning(f"Google Drive no configurado: {e}")

# Función para obtener respuesta de GPT
def get_gpt_response(prompt):
    """Obtiene respuesta de OpenAI GPT"""
    try:
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "Eres Vicky, una asistente virtual educada y servicial de SECOM. Ofreces información sobre pensiones IMSS, seguros de auto, tarjetas médicas VRIM y contactas con asesores."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=150,
            temperature=0.7
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"Error en GPT: {e}")
        return "Lo siento, estoy teniendo dificultades técnicas. Por favor, selecciona una opción del menú:\n\n1️⃣ Pensiones IMSS\n2️⃣ Seguros de Auto\n5️⃣ Tarjetas médicas VRIM\n7️⃣ Contactar a Christian"

# Función para enviar mensajes por WhatsApp
def send_whatsapp_message(phone_number, message):
    """Envía mensaje a través de Meta WhatsApp API"""
    try:
        url = "https://graph.facebook.com/v17.0/118469193281675/messages"
        headers = {
            "Authorization": f"Bearer {META_ACCESS_TOKEN}",
            "Content-Type": "application/json"
        }
        data = {
            "messaging_product": "whatsapp",
            "to": phone_number,
            "text": {"body": message}
        }
        
        response = requests.post(url, headers=headers, json=data)
        if response.status_code == 200:
            logger.info(f"Mensaje enviado a {phone_number}")
            return True
        else:
            logger.error(f"Error enviando mensaje: {response.status_code} - {response.text}")
            return False
    except Exception as e:
        logger.error(f"Excepción enviando mensaje: {e}")
        return False

# Función para notificar al asesor
def notify_advisor(client_number, service_type, message=None):
    """Notifica al asesor sobre un nuevo prospecto"""
    try:
        notification = f"🚨 NUEVO PROSPECTO 🚨\n\n📞 Teléfono: {client_number}\n📋 Servicio: {service_type}"
        if message:
            notification += f"\n💬 Mensaje: {message}"
        
        send_whatsapp_message(ADVISOR_NUMBER, notification)
        
        # Registrar en Google Sheets si está disponible
        if sheet:
            try:
                next_row = len(sheet.get_all_values()) + 1
                sheet.update(f"A{next_row}:D{next_row}", [[
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    client_number,
                    service_type,
                    "Activo"
                ]])
                logger.info(f"Prospecto registrado en Google Sheets: {client_number}")
            except Exception as e:
                logger.error(f"Error registrando en Google Sheets: {e}")
        
        logger.info(f"Asesor notificado sobre prospecto: {client_number}")
        return True
    except Exception as e:
        logger.error(f"Error notificando al asesor: {e}")
        return False

# WEBHOOK PRINCIPAL - CORREGIDO
@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    """Webhook principal para recibir mensajes de WhatsApp"""
    
    # VERIFICACIÓN DEL WEBHOOK (GET)
    if request.method == "GET":
        logger.info("Solicitud GET recibida para verificación del webhook")
        
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        
        logger.info(f"Parámetros recibidos - mode: {mode}, token: {token}, challenge: {challenge}")
        
        if mode and token:
            if mode == "subscribe" and token == VERIFY_TOKEN:
                logger.info("✅ Webhook verificado exitosamente")
                return challenge, 200
            else:
                logger.error(f"❌ Token de verificación inválido. Esperado: {VERIFY_TOKEN}, Recibido: {token}")
                return "Verification token mismatch", 403
        else:
            logger.error("❌ Parámetros de verificación faltantes")
            return "Missing verification parameters", 400
    
    # PROCESAR MENSAJES ENTRANTES (POST)
    elif request.method == "POST":
        logger.info("📨 Mensaje POST recibido en webhook")
        
        try:
            data = request.get_json()
            logger.info(f"Datos recibidos: {json.dumps(data, indent=2)}")
            
            # Verificar estructura básica del webhook
            if not data or data.get("object") != "whatsapp_business_account":
                logger.error("Estructura de webhook inválida")
                return jsonify({"status": "error", "message": "Invalid webhook structure"}), 400
            
            entries = data.get("entry", [])
            if not entries:
                logger.info("No entries found in webhook")
                return jsonify({"status": "success"}), 200
            
            # Procesar cada entrada
            for entry in entries:
                changes = entry.get("changes", [])
                for change in changes:
                    if change.get("field") == "messages":
                        message_data = change.get("value", {})
                        process_message(message_data)
            
            logger.info("✅ Webhook procesado exitosamente")
            return jsonify({"status": "success"}), 200
            
        except Exception as e:
            logger.error(f"❌ Error procesando webhook: {e}")
            return jsonify({"status": "error", "message": str(e)}), 500

def process_message(message_data):
    """Procesa los mensajes entrantes de WhatsApp"""
    try:
        messages = message_data.get("messages", [])
        if not messages:
            logger.info("No messages found in message_data")
            return
        
        message = messages[0]
        phone_number = message.get("from")
        message_type = message.get("type")
        
        if not phone_number:
            logger.error("No phone number found in message")
            return
        
        # Extraer últimos 10 dígitos del número
        client_number = re.sub(r'\D', '', phone_number)[-10:]
        logger.info(f"Procesando mensaje de {client_number}, tipo: {message_type}")
        
        # Procesar según el tipo de mensaje
        if message_type == "text":
            process_text_message(message, client_number)
        elif message_type in ["image", "document"]:
            process_media_message(message, client_number, message_type)
        else:
            logger.info(f"Tipo de mensaje no manejado: {message_type}")
            
    except Exception as e:
        logger.error(f"Error procesando mensaje: {e}")

def process_text_message(message, client_number):
    """Procesa mensajes de texto"""
    try:
        text_body = message.get("text", {}).get("body", "").strip()
        logger.info(f"📝 Mensaje de texto recibido de {client_number}: {text_body}")
        
        # Menú de opciones principales
        if text_body == "1":
            response = """🏥 *PENSIONES IMSS*

¿Cumples alguno de estos requisitos?

• 60 años o más
• 500 semanas cotizadas
• Trabajaste antes de 1997

Si cumples alguno, ¡podrías tener derecho a tu pensión! Un asesor se contactará contigo."""
            send_whatsapp_message(client_number, response)
            notify_advisor(client_number, "Pensiones IMSS", "Interesado en pensiones")
            
        elif text_body == "2":
            response = """🚗 *SEGUROS DE AUTO*

Protege tu auto con las mejores coberturas:

• Responsabilidad Civil
• Daños Materiales
• Robo Total
• Asistencia Vial

Por favor, envía fotos de:
1. INE (ambos lados)
2. Tarjeta de circulación"""
            send_whatsapp_message(client_number, response)
            notify_advisor(client_number, "Seguros de Auto", "Solicitó información")
            
        elif text_body == "5":
            response = """🏥 *TARJETAS MÉDICAS VRIM*

Beneficios exclusivos para militares:

• Atención médica especializada
• Medicamentos gratuitos
• Estudios de laboratorio
• Consultas con especialistas

Un asesor te contactará para explicarte el proceso."""
            send_whatsapp_message(client_number, response)
            notify_advisor(client_number, "Tarjetas Médicas VRIM")
            
        elif text_body == "7":
            response = "👨‍💼 Te pondré en contacto con Christian, nuestro especialista. Él te atenderá personalmente en breve."
            send_whatsapp_message(client_number, response)
            notify_advisor(client_number, "Contactar Asesor", "Solicita contacto directo con Christian")
            
        else:
            # Mensaje de bienvenida para primer contacto
            lower_text = text_body.lower()
            if any(word in lower_text for word in ['hola', 'buenas', 'info', 'opciones', 'menu']):
                welcome_message = """¡Hola! 👋 Soy Vicky, tu asistente virtual de SECOM.

¿En qué te puedo ayudar? Selecciona una opción:

1️⃣ Pensiones IMSS
2️⃣ Seguros de Auto  
5️⃣ Tarjetas médicas VRIM
7️⃣ Contactar a Christian"""
                send_whatsapp_message(client_number, welcome_message)
            else:
                # Usar GPT para respuestas no reconocidas
                gpt_prompt = f"El cliente dijo: '{text_body}'. Responde educadamente como Vicky de SECOM y sugiere las opciones del menú: 1) Pensiones IMSS, 2) Seguros de Auto, 5) Tarjetas médicas VRIM, 7) Contactar a Christian. Mantén la respuesta breve y amable."
                gpt_response = get_gpt_response(gpt_prompt)
                send_whatsapp_message(client_number, gpt_response)
            
    except Exception as e:
        logger.error(f"Error procesando mensaje de texto: {e}")
        # Respuesta de fallback
        send_whatsapp_message(client_number, "Lo siento, hubo un error. Por favor, selecciona: 1, 2, 5 o 7.")

def process_media_message(message, client_number, message_type):
    """Procesa mensajes con archivos (imágenes o documentos)"""
    try:
        logger.info(f"Procesando archivo {message_type} de {client_number}")
        
        # Notificar al asesor sobre el archivo recibido
        file_type = "imagen" if message_type == "image" else "documento"
        notification = f"📎 Se recibió un {file_type} del cliente {client_number}"
        send_whatsapp_message(ADVISOR_NUMBER, notification)
        
        # Confirmar al cliente
        confirmation = f"✅ Recibí tu {file_type}. Un asesor revisará tu documentación y te contactará pronto."
        send_whatsapp_message(client_number, confirmation)
            
    except Exception as e:
        logger.error(f"Error procesando mensaje multimedia: {e}")

# Ruta de salud para Render
@app.route("/")
def health_check():
    """Endpoint de salud para verificar que la app está funcionando"""
    return jsonify({
        "status": "active",
        "service": "Vicky Bot SECOM",
        "timestamp": datetime.now().isoformat(),
        "webhook_url": "https://vicky-bot-x6wt.onrender.com/webhook",
        "verify_token": VERIFY_TOKEN
    })

# Ruta adicional para testing
@app.route("/test", methods=["GET"])
def test():
    """Ruta para testing básico"""
    return jsonify({
        "message": "Vicky Bot está funcionando",
        "status": "OK",
        "time": datetime.now().isoformat()
    })

# Configuración para producción en Render
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    logger.info(f"Iniciando aplicación en puerto {port}")
    app.run(host='0.0.0.0', port=port, debug=False)
