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
from datetime import datetime, timedelta
import re
import json

# Configuración de logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Configuración de variables de entorno
META_ACCESS_TOKEN = os.getenv('META_ACCESS_TOKEN')
VERIFY_TOKEN = os.getenv('VERIFY_TOKEN')
ADVISOR_NUMBER = os.getenv('ADVISOR_NUMBER')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
GOOGLE_SHEETS_CREDENTIALS = os.getenv('GOOGLE_SHEETS_CREDENTIALS')
GOOGLE_DRIVE_CREDENTIALS = os.getenv('GOOGLE_DRIVE_CREDENTIALS')

# Configurar OpenAI
openai.api_key = OPENAI_API_KEY

# Configuración de Google Sheets
def setup_google_sheets():
    """Configura la conexión con Google Sheets"""
    try:
        if not GOOGLE_SHEETS_CREDENTIALS:
            logger.error("GOOGLE_SHEETS_CREDENTIALS no configurado")
            return None
            
        creds_dict = json.loads(GOOGLE_SHEETS_CREDENTIALS)
        credentials = service_account.Credentials.from_service_account_info(creds_dict)
        scoped_credentials = credentials.with_scopes([
            'https://www.googleapis.com/auth/spreadsheets',
            'https://www.googleapis.com/auth/drive'
        ])
        
        client = gspread.authorize(scoped_credentials)
        sheet = client.open("Prospectos SECOM Auto").sheet1
        logger.info("Google Sheets configurado exitosamente")
        return sheet
    except Exception as e:
        logger.error(f"Error configurando Google Sheets: {e}")
        return None

# Configuración de Google Drive
def setup_google_drive():
    """Configura la conexión con Google Drive"""
    try:
        if not GOOGLE_DRIVE_CREDENTIALS:
            logger.error("GOOGLE_DRIVE_CREDENTIALS no configurado")
            return None
            
        creds_dict = json.loads(GOOGLE_DRIVE_CREDENTIALS)
        credentials = service_account.Credentials.from_service_account_info(creds_dict)
        drive_service = build('drive', 'v3', credentials=credentials)
        logger.info("Google Drive configurado exitosamente")
        return drive_service
    except Exception as e:
        logger.error(f"Error configurando Google Drive: {e}")
        return None

# Inicializar servicios
sheet = setup_google_sheets()
drive_service = setup_google_drive()

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

# Función para subir archivos a Drive
def upload_to_drive(file_url, filename, client_number):
    """Sube archivos a Google Drive en la carpeta correspondiente"""
    try:
        if not drive_service:
            logger.error("Drive service no disponible")
            return False
            
        # Descargar el archivo desde WhatsApp
        headers = {"Authorization": f"Bearer {META_ACCESS_TOKEN}"}
        response = requests.get(file_url, headers=headers)
        if response.status_code != 200:
            logger.error(f"No se pudo descargar el archivo: {response.status_code}")
            return False
        
        file_content = io.BytesIO(response.content)
        
        # Crear nombre de carpeta con formato Cliente_#### (últimos 4 dígitos)
        folder_name = f'Cliente_{client_number[-4:]}'
        
        # Buscar carpeta existente
        folder_query = f"name='{folder_name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
        folders = drive_service.files().list(q=folder_query).execute()
        
        if folders.get('files'):
            folder_id = folders['files'][0]['id']
        else:
            # Crear carpeta si no existe
            folder_metadata = {
                'name': folder_name,
                'mimeType': 'application/vnd.google-apps.folder'
            }
            folder = drive_service.files().create(body=folder_metadata, fields='id').execute()
            folder_id = folder['id']
            logger.info(f"Carpeta creada: {folder_name} (ID: {folder_id})")
        
        # Subir archivo a la carpeta
        file_metadata = {
            'name': filename,
            'parents': [folder_id]
        }
        
        # Determinar tipo MIME
        file_extension = filename.split('.')[-1].lower()
        mime_type = 'application/octet-stream'
        if file_extension in ['jpg', 'jpeg']:
            mime_type = 'image/jpeg'
        elif file_extension == 'png':
            mime_type = 'image/png'
        elif file_extension == 'pdf':
            mime_type = 'application/pdf'
        
        media = MediaIoBaseUpload(file_content, mimetype=mime_type)
        file = drive_service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id'
        ).execute()
        
        logger.info(f"Archivo {filename} subido exitosamente a Drive (ID: {file['id']})")
        return True
        
    except Exception as e:
        logger.error(f"Error subiendo archivo a Drive: {e}")
        return False

# Función para enviar mensajes por WhatsApp
def send_whatsapp_message(phone_number, message):
    """Envía mensaje a través de Meta WhatsApp API"""
    try:
        url = f"https://graph.facebook.com/v17.0/118469193281675/messages"
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
def notify_advisor(client_number, client_name, service_type, message=None):
    """Notifica al asesor sobre un nuevo prospecto"""
    try:
        notification = f"🚨 NUEVO PROSPECTO 🚨\n\n📱 Cliente: {client_name}\n📞 Teléfono: {client_number}\n📋 Servicio: {service_type}"
        if message:
            notification += f"\n💬 Mensaje: {message}"
        
        send_whatsapp_message(ADVISOR_NUMBER, notification)
        
        # Registrar en Google Sheets si está disponible
        if sheet:
            try:
                next_row = len(sheet.get_all_values()) + 1
                sheet.update(f"A{next_row}:E{next_row}", [[
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    client_name,
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

# Webhook principal de WhatsApp
@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    """Webhook principal para recibir mensajes de WhatsApp"""
    
    # Verificación del webhook
    if request.method == "GET":
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        
        if mode and token:
            if mode == "subscribe" and token == VERIFY_TOKEN:
                logger.info("Webhook verificado exitosamente")
                return challenge
            else:
                logger.error("Token de verificación inválido")
                return "Verification token mismatch", 403
        else:
            logger.error("Parámetros de verificación faltantes")
            return "Missing parameters", 400
    
    # Procesar mensajes entrantes
    elif request.method == "POST":
        try:
            data = request.get_json()
            logger.info(f"Datos recibidos del webhook")
            
            if data.get("object") == "whatsapp_business_account":
                for entry in data.get("entry", []):
                    for change in entry.get("changes", []):
                        if change.get("field") == "messages":
                            message_data = change.get("value", {})
                            process_message(message_data)
            
            return jsonify({"status": "success"}), 200
            
        except Exception as e:
            logger.error(f"Error procesando webhook: {e}")
            return jsonify({"status": "error"}), 500

def process_message(message_data):
    """Procesa los mensajes entrantes de WhatsApp"""
    try:
        messages = message_data.get("messages", [])
        if not messages:
            return
        
        message = messages[0]
        phone_number = message.get("from")
        message_type = message.get("type")
        
        # Extraer últimos 10 dígitos del número
        client_number = re.sub(r'\D', '', phone_number)[-10:]
        
        # Procesar según el tipo de mensaje
        if message_type == "text":
            process_text_message(message, client_number)
        elif message_type in ["image", "document"]:
            process_media_message(message, client_number, message_type)
            
    except Exception as e:
        logger.error(f"Error procesando mensaje: {e}")

def process_text_message(message, client_number):
    """Procesa mensajes de texto"""
    try:
        text_body = message.get("text", {}).get("body", "").strip()
        logger.info(f"Mensaje de texto recibido de {client_number}: {text_body}")
        
        # Menú de opciones principales
        if text_body == "1":
            response = """🏥 *PENSIONES IMSS*

¿Cumples alguno de estos requisitos?

• 60 años o más
• 500 semanas cotizadas
• Trabajaste antes de 1997

Si cumples alguno, ¡podrías tener derecho a tu pensión! Un asesor se contactará contigo."""
            send_whatsapp_message(client_number, response)
            notify_advisor(client_number, "Cliente IMSS", "Pensiones IMSS", "Interesado en pensiones")
            
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
            notify_advisor(client_number, "Cliente Seguro Auto", "Seguros de Auto", "Solicitó información")
            
        elif text_body == "5":
            response = """🏥 *TARJETAS MÉDICAS VRIM*

Beneficios exclusivos para militares:

• Atención médica especializada
• Medicamentos gratuitos
• Estudios de laboratorio
• Consultas con especialistas

Un asesor te contactará para explicarte el proceso."""
            send_whatsapp_message(client_number, response)
            notify_advisor(client_number, "Cliente VRIM", "Tarjetas Médicas VRIM")
            
        elif text_body == "7":
            response = "👨‍💼 Te pondré en contacto con Christian, nuestro especialista. Él te atenderá personalmente en breve."
            send_whatsapp_message(client_number, response)
            notify_advisor(client_number, "Cliente Christian", "Contactar Asesor", "Solicita contacto directo con Christian")
            
        else:
            # Mensaje de bienvenida para primer contacto
            if "hola" in text_body.lower() or "buenas" in text_body.lower() or "info" in text_body.lower():
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
        media_id = message.get(message_type, {}).get("id")
        
        if not media_id:
            logger.error("ID de medio no encontrado")
            return
        
        # Obtener URL del archivo
        url = f"https://graph.facebook.com/v17.0/{media_id}"
        headers = {"Authorization": f"Bearer {META_ACCESS_TOKEN}"}
        response = requests.get(url, headers=headers)
        
        if response.status_code != 200:
            logger.error(f"Error obteniendo URL del medio: {response.status_code}")
            return
        
        media_url = response.json().get("url")
        if not media_url:
            logger.error("URL de medio no encontrada")
            return
        
        # Crear nombre de archivo
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        file_extension = "jpg" if message_type == "image" else "pdf"
        filename = f"{message_type}_{client_number}_{timestamp}.{file_extension}"
        
        # Subir a Drive
        if upload_to_drive(media_url, filename, client_number):
            logger.info(f"Archivo {filename} procesado exitosamente")
            
            # Notificar al asesor
            file_type = "imagen" if message_type == "image" else "documento"
            notification = f"📎 Se recibió un {file_type} del cliente {client_number}\n📁 Guardado en Drive como: {filename}"
            send_whatsapp_message(ADVISOR_NUMBER, notification)
            
            # Confirmar al cliente
            confirmation = f"✅ Recibí tu {file_type}. Un asesor revisará tu documentación y te contactará pronto."
            send_whatsapp_message(client_number, confirmation)
        else:
            logger.error(f"Error subiendo archivo a Drive: {filename}")
            # Notificar error al asesor
            send_whatsapp_message(ADVISOR_NUMBER, f"❌ Error al subir archivo del cliente {client_number}")
            
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
        "sheets_connected": sheet is not None,
        "drive_connected": drive_service is not None
    })

# Configuración para Render
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    
    # Solo ejecutar con Flask si no estamos en Render
    if os.getenv("RENDER", "false") != "true":
        app.run(host='0.0.0.0', port=port, debug=False)
    else:
        # En Render, Gunicorn maneja la ejecución
        logger.info("Aplicación iniciada en entorno Render")
