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
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Configuración de variables de entorno
META_ACCESS_TOKEN = os.getenv('META_ACCESS_TOKEN')
VERIFY_TOKEN = os.getenv('VERIFY_TOKEN', 'vicky-verify-2025')
ADVISOR_NUMBER = os.getenv('ADVISOR_NUMBER')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')

# Configurar OpenAI
openai.api_key = OPENAI_API_KEY

# Inicializar servicios de Google
sheet = None
drive_service = None

try:
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
        logger.info("✅ Google Sheets configurado")
except Exception as e:
    logger.warning(f"❌ Google Sheets no configurado: {e}")

try:
    GOOGLE_DRIVE_CREDENTIALS = os.getenv('GOOGLE_DRIVE_CREDENTIALS')
    if GOOGLE_DRIVE_CREDENTIALS:
        creds_dict = json.loads(GOOGLE_DRIVE_CREDENTIALS)
        credentials = service_account.Credentials.from_service_account_info(creds_dict)
        drive_service = build('drive', 'v3', credentials=credentials)
        logger.info("✅ Google Drive configurado")
except Exception as e:
    logger.warning(f"❌ Google Drive no configurado: {e}")

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
            logger.info(f"✅ Mensaje enviado a {phone_number}")
            return True
        else:
            logger.error(f"❌ Error enviando mensaje: {response.status_code} - {response.text}")
            return False
    except Exception as e:
        logger.error(f"❌ Excepción enviando mensaje: {e}")
        return False

def notify_advisor(client_number, service_type, message=None):
    """Notifica al asesor sobre un nuevo prospecto"""
    try:
        notification = f"🚨 NUEVO PROSPECTO 🚨\n\n📞 Teléfono: {client_number}\n📋 Servicio: {service_type}"
        if message:
            notification += f"\n💬 Mensaje: {message}"
        
        send_whatsapp_message(ADVISOR_NUMBER, notification)
        
        if sheet:
            try:
                next_row = len(sheet.get_all_values()) + 1
                sheet.update(f"A{next_row}:D{next_row}", [[
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    client_number,
                    service_type,
                    "Activo"
                ]])
                logger.info(f"✅ Prospecto registrado en Sheets: {client_number}")
            except Exception as e:
                logger.error(f"❌ Error registrando en Sheets: {e}")
        
        logger.info(f"✅ Asesor notificado: {client_number}")
        return True
    except Exception as e:
        logger.error(f"❌ Error notificando al asesor: {e}")
        return False

# WEBHOOK PRINCIPAL - VERSIÓN SIMPLIFICADA Y ROBUSTA
@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    """Webhook principal para WhatsApp - Versión corregida"""
    
    # VERIFICACIÓN DEL WEBHOOK (GET)
    if request.method == "GET":
        logger.info("=== SOLICITUD DE VERIFICACIÓN META RECIBIDA ===")
        
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        
        logger.info(f"Mode: {mode}")
        logger.info(f"Token recibido: {token}")
        logger.info(f"Token esperado: {VERIFY_TOKEN}")
        logger.info(f"Challenge: {challenge}")
        
        # Verificar los parámetros
        if not all([mode, token, challenge]):
            logger.error("❌ Faltan parámetros en la solicitud")
            return "Missing parameters", 400
        
        if mode == "subscribe" and token == VERIFY_TOKEN:
            logger.info("✅✅✅ VERIFICACIÓN EXITOSA - Webhook configurado correctamente")
            # Devolver el challenge como texto plano
            from flask import Response
            response = Response(challenge, status=200, mimetype='text/plain')
            return response
        else:
            logger.error(f"❌ VERIFICACIÓN FALLIDA - Token mismatch: {token} vs {VERIFY_TOKEN}")
            return "Verification failed", 403
    
    # PROCESAR MENSAJES (POST)
    elif request.method == "POST":
        logger.info("📨 Mensaje POST recibido de Meta")
        
        try:
            data = request.get_json()
            logger.info(f"Datos recibidos: {json.dumps(data, indent=2)[:500]}...")
            
            # Verificar estructura básica
            if not data or data.get("object") != "whatsapp_business_account":
                logger.error("Estructura de datos inválida")
                return jsonify({"status": "error"}), 400
            
            # Procesar entradas
            entries = data.get("entry", [])
            logger.info(f"Número de entradas: {len(entries)}")
            
            for entry in entries:
                changes = entry.get("changes", [])
                for change in changes:
                    if change.get("field") == "messages":
                        message_data = change.get("value", {})
                        process_message(message_data)
            
            logger.info("✅ Webhook procesado correctamente")
            return jsonify({"status": "success"}), 200
            
        except Exception as e:
            logger.error(f"❌ Error procesando webhook POST: {e}")
            return jsonify({"status": "error", "message": str(e)}), 500

def process_message(message_data):
    """Procesa los mensajes entrantes"""
    try:
        messages = message_data.get("messages", [])
        if not messages:
            logger.info("No hay mensajes en los datos")
            return
        
        message = messages[0]
        phone_number = message.get("from")
        message_type = message.get("type")
        
        if not phone_number:
            logger.error("No se encontró número de teléfono")
            return
        
        client_number = re.sub(r'\D', '', phone_number)[-10:]
        logger.info(f"📱 Procesando mensaje de {client_number}, tipo: {message_type}")
        
        if message_type == "text":
            process_text_message(message, client_number)
        elif message_type in ["image", "document"]:
            process_media_message(message, client_number, message_type)
        else:
            logger.info(f"Tipo de mensaje no manejado: {message_type}")
            
    except Exception as e:
        logger.error(f"❌ Error en process_message: {e}")

def process_text_message(message, client_number):
    """Procesa mensajes de texto"""
    try:
        text_body = message.get("text", {}).get("body", "").strip()
        logger.info(f"💬 Texto recibido: {text_body}")
        
        # Menú de opciones
        if text_body == "1":
            response = """🏥 *PENSIONES IMSS*

¿Cumples alguno de estos requisitos?

• 60 años o más
• 500 semanas cotizadas
• Trabajaste antes de 1997

Si cumples alguno, ¡podrías tener derecho a tu pensión! Un asesor se contactará contigo."""
            send_whatsapp_message(client_number, response)
            notify_advisor(client_number, "Pensiones IMSS")
            
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
            notify_advisor(client_number, "Seguros de Auto")
            
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
            notify_advisor(client_number, "Contactar Asesor")
            
        else:
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
                gpt_response = get_gpt_response(f"El cliente dijo: '{text_body}'. Responde educadamente como Vicky de SECOM y sugiere las opciones del menú.")
                send_whatsapp_message(client_number, gpt_response)
            
    except Exception as e:
        logger.error(f"❌ Error en process_text_message: {e}")
        send_whatsapp_message(client_number, "Lo siento, hubo un error. Por favor, selecciona: 1, 2, 5 o 7.")

def process_media_message(message, client_number, message_type):
    """Procesa archivos multimedia"""
    try:
        file_type = "imagen" if message_type == "image" else "documento"
        logger.info(f"📎 {file_type} recibido de {client_number}")
        
        notification = f"📎 Se recibió un {file_type} del cliente {client_number}"
        send_whatsapp_message(ADVISOR_NUMBER, notification)
        
        confirmation = f"✅ Recibí tu {file_type}. Un asesor revisará tu documentación y te contactará pronto."
        send_whatsapp_message(client_number, confirmation)
            
    except Exception as e:
        logger.error(f"❌ Error en process_media_message: {e}")

@app.route("/")
def health_check():
    return jsonify({
        "status": "active",
        "service": "Vicky Bot SECOM",
        "webhook_url": "https://vicky-bot-x6wt.onrender.com/webhook",
        "verify_token": VERIFY_TOKEN,
        "timestamp": datetime.now().isoformat()
    })

@app.route("/debug-webhook")
def debug_webhook():
    """Página de debug para el webhook"""
    return f"""
    <html>
        <head><title>Debug Webhook Meta</title></head>
        <body>
            <h1>🔧 Debug Webhook Meta</h1>
            <p><strong>URL del Webhook:</strong> https://vicky-bot-x6wt.onrender.com/webhook</p>
            <p><strong>Token de Verificación:</strong> {VERIFY_TOKEN}</p>
            <p><strong>Estado:</strong> ✅ Activo</p>
            <p><strong>Instrucciones:</strong></p>
            <ol>
                <li>Ve a Meta Developer → WhatsApp → Configuration</li>
                <li>En Callback URL pon: <code>https://vicky-bot-x6wt.onrender.com/webhook</code></li>
                <li>En Verify Token pon: <code>{VERIFY_TOKEN}</code></li>
                <li>Haz clic en "Verify and Save"</li>
            </ol>
            <p><a href="https://vicky-bot-x6wt.onrender.com/">Volver al inicio</a></p>
        </body>
    </html>
    """

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    logger.info(f"🚀 Iniciando Vicky Bot en puerto {port}")
    logger.info(f"🔗 Webhook URL: https://vicky-bot-x6wt.onrender.com/webhook")
    logger.info(f"🔑 Verify Token: {VERIFY_TOKEN}")
    app.run(host='0.0.0.0', port=port, debug=False)

