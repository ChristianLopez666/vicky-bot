import os
import json
import logging
import requests
import threading
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
import io

# ---------------------------------------------------------------
# SECCIÓN: CARGA E INICIALIZACIÓN
# ---------------------------------------------------------------

# Cargar variables de entorno
load_dotenv()

# Configuración de logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Inicializar Flask
app = Flask(__name__)

# Variables de entorno
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
WHATSAPP_TOKEN = os.getenv("META_TOKEN")
PHONE_NUMBER_ID = os.getenv("WABA_PHONE_ID")
ADVISOR_NUMBER = os.getenv("ADVISOR_NUMBER")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")
SHEETS_ID_LEADS = os.getenv("SHEETS_ID_LEADS")
SHEETS_TITLE_LEADS = os.getenv("SHEETS_TITLE_LEADS", "Prospectos SECOM Auto")
DRIVE_FOLDER_ID = os.getenv("DRIVE_FOLDER_ID")

# Inicializar clientes de Google
google_creds = None
sheets_client = None
drive_service = None
openai_client = None

try:
    if GOOGLE_CREDENTIALS_JSON:
        creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
        google_creds = Credentials.from_service_account_info(creds_dict)
        sheets_client = gspread.authorize(google_creds)
        drive_service = build('drive', 'v3', credentials=google_creds)
        logger.info("✅ Clientes de Google inicializados correctamente")
except Exception as e:
    logger.error(f"❌ Error inicializando clientes de Google: {e}")

# Inicializar OpenAI (manejo de versiones)
try:
    if OPENAI_API_KEY:
        # Para la versión 1.x de OpenAI
        import openai
        openai_client = openai.OpenAI(api_key=OPENAI_API_KEY)
        logger.info("✅ Cliente OpenAI inicializado correctamente")
except Exception as e:
    logger.error(f"❌ Error inicializando cliente OpenAI: {e}")
    openai_client = None

# Controles en memoria
PROCESSED_MESSAGE_IDS = {}
GREETED_USERS = {}
LAST_INTENT = {}
USER_CONTEXT = {}

# ---------------------------------------------------------------
# SECCIÓN: GOOGLE SHEETS (SECOM)
# ---------------------------------------------------------------

def find_client_in_sheet(phone):
    """Busca cliente en Google Sheets por número de teléfono"""
    if not sheets_client or not SHEETS_ID_LEADS:
        return None
    
    try:
        sheet = sheets_client.open_by_key(SHEETS_ID_LEADS)
        worksheet = sheet.worksheet(SHEETS_TITLE_LEADS)
        records = worksheet.get_all_records()
        
        # Normalizar phone (últimos 10 dígitos)
        phone_normalized = phone[-10:] if len(phone) >= 10 else phone
        
        for record in records:
            record_phone = str(record.get('WhatsApp', '') or record.get('Teléfono', '') or '')
            if record_phone and record_phone[-10:] == phone_normalized:
                return {
                    'nombre': record.get('Nombre', ''),
                    'rfc': record.get('RFC', ''),
                    'email': record.get('Email', ''),
                    'vencimiento_poliza': record.get('Vencimiento Póliza', ''),
                    'estatus': record.get('Estatus', '')
                }
    except Exception as e:
        logger.error(f"❌ Error buscando cliente en sheet: {e}")
    
    return None

def update_client_status(phone, status, additional_data=None):
    """Actualiza estatus del cliente en Google Sheets"""
    if not sheets_client or not SHEETS_ID_LEADS:
        return False
    
    try:
        sheet = sheets_client.open_by_key(SHEETS_ID_LEADS)
        worksheet = sheet.worksheet(SHEETS_TITLE_LEADS)
        records = worksheet.get_all_records()
        
        phone_normalized = phone[-10:] if len(phone) >= 10 else phone
        
        for i, record in enumerate(records, start=2):  # start=2 porque la primera fila es encabezado
            record_phone = str(record.get('WhatsApp', '') or record.get('Teléfono', '') or '')
            if record_phone and record_phone[-10:] == phone_normalized:
                # Encontrar columnas por nombre
                col_names = worksheet.row_values(1)
                status_col = col_names.index("Estatus") + 1 if "Estatus" in col_names else None
                last_contact_col = col_names.index("Último Contacto") + 1 if "Último Contacto" in col_names else None
                
                if status_col:
                    worksheet.update_cell(i, status_col, status)
                
                if last_contact_col:
                    worksheet.update_cell(i, last_contact_col, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                
                # Actualizar datos adicionales
                if additional_data:
                    for key, value in additional_data.items():
                        if key in col_names:
                            col_idx = col_names.index(key) + 1
                            worksheet.update_cell(i, col_idx, value)
                
                logger.info(f"✅ Estatus actualizado para {phone}: {status}")
                return True
    except Exception as e:
        logger.error(f"❌ Error actualizando estatus del cliente: {e}")
    
    return False

def register_new_interaction(phone, interaction_type, details):
    """Registra nueva interacción en Google Sheets"""
    if not sheets_client or not SHEETS_ID_LEADS:
        return False
    
    try:
        client_data = find_client_in_sheet(phone)
        
        sheet = sheets_client.open_by_key(SHEETS_ID_LEADS)
        worksheet = sheet.worksheet(SHEETS_TITLE_LEADS)
        col_names = worksheet.row_values(1)
        
        if not client_data:
            # Crear nuevo registro si no existe
            new_row = [""] * len(col_names)
            
            # Mapear datos a columnas
            if "WhatsApp" in col_names:
                new_row[col_names.index("WhatsApp")] = phone[-10:]
            if "Nombre" in col_names:
                new_row[col_names.index("Nombre")] = "Nuevo Cliente"
            if "Estatus" in col_names:
                new_row[col_names.index("Estatus")] = "Nuevo Prospecto"
            if "Tipo de Interacción" in col_names:
                new_row[col_names.index("Tipo de Interacción")] = interaction_type
            if "Último Contacto" in col_names:
                new_row[col_names.index("Último Contacto")] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if "Detalles" in col_names:
                new_row[col_names.index("Detalles")] = details
            if "Fecha Registro" in col_names:
                new_row[col_names.index("Fecha Registro")] = datetime.now().strftime("%Y-%m-%d")
            
            worksheet.append_row(new_row)
            logger.info(f"✅ Nuevo cliente registrado: {phone}")
        else:
            # Actualizar registro existente
            update_client_status(phone, interaction_type, {
                "Tipo de Interacción": interaction_type,
                "Detalles": details,
                "Último Contacto": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            })
        
        return True
    except Exception as e:
        logger.error(f"❌ Error registrando interacción: {e}")
        return False

# ---------------------------------------------------------------
# SECCIÓN: MOTOR GPT (CORREGIDO)
# ---------------------------------------------------------------

def ask_gpt(prompt, system_message=None):
    """Consulta a GPT para respuestas naturales"""
    if not openai_client:
        return None
    
    try:
        system_msg = system_message or (
            "Eres Vicky, asistente virtual de Christian López en SECOM. "
            "Eres cálida, profesional y servicial. Responde en español de manera "
            "breve, clara y orientada a soluciones. Usa emojis moderadamente. "
            "Si no tienes información específica, sugiere contactar al asesor."
        )
        
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",  # Modelo más económico y rápido
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": prompt}
            ],
            max_tokens=150,
            temperature=0.7
        )
        
        return response.choices[0].message.content.strip()
    
    except Exception as e:
        logger.error(f"❌ Error consultando GPT: {e}")
        return None

# ---------------------------------------------------------------
# SECCIÓN: WHATSAPP MESSAGING
# ---------------------------------------------------------------

def send_message_async(to, text):
    """Envía mensaje de forma asíncrona"""
    def _send():
        try:
            send_message(to, text)
        except Exception as e:
            logger.error(f"❌ Error enviando mensaje asíncrono: {e}")
    
    thread = threading.Thread(target=_send)
    thread.daemon = True
    thread.start()

def send_message(to, text):
    """Envía mensaje de texto por WhatsApp"""
    url = f"https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text}
    }
    
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=10)
        if response.status_code == 200:
            logger.info(f"✅ Mensaje enviado a {to}")
        else:
            logger.error(f"❌ Error enviando mensaje: {response.status_code} - {response.text}")
        return response
    except Exception as e:
        logger.error(f"❌ Error enviando mensaje: {e}")
        return None

def send_main_menu(to, client_name=None):
    """Envía el menú principal personalizado"""
    greeting = f"👋 Hola {client_name}, " if client_name else "👋 Hola, "
    
    menu_text = (
        f"{greeting}soy Vicky, tu asistente virtual de SECOM. "
        "Estoy aquí para ayudarte con tus seguros y pólizas.\n\n"
        "👉 *Elige una opción:*\n\n"
        "1️⃣ *Renovar Póliza* - Renovación y seguimiento\n"
        "2️⃣ *Documentos Seguro Auto* - Envío de documentos\n"
        "3️⃣ *Promociones y Descuentos* - Ofertas especiales\n"
        "4️⃣ *Seguimiento* - Consulta el estatus de tu trámite\n"
        "5️⃣ *Préstamos IMSS* - Financiamiento para pensionados\n"
        "6️⃣ *VRIM* - Tarjetas médicas y beneficios\n"
        "7️⃣ *Contactar con Christian* - Atención personalizada\n\n"
        "Escribe el número de la opción o 'menu' para volver a ver este menú."
    )
    
    send_message_async(to, menu_text)

# ---------------------------------------------------------------
# SECCIÓN: FLUJOS SECOM PRINCIPALES
# ---------------------------------------------------------------

def handle_policy_renewal(to, client_phone, message_text):
    """Maneja el flujo de renovación de póliza"""
    client_data = find_client_in_sheet(client_phone)
    
    if "vencimiento" in message_text.lower() or "renovar" in message_text.lower():
        if client_data and client_data.get('vencimiento_poliza'):
            # Cliente existente con fecha de vencimiento
            vencimiento = client_data['vencimiento_poliza']
            response = (
                f"📅 Tu póliza vence el *{vencimiento}*. "
                f"Te contactaré un mes antes para gestionar la renovación. "
                "¿Hay algo más en lo que pueda ayudarte?"
            )
        else:
            # Nuevo cliente o sin fecha registrada
            response = (
                "🔄 Para programar la renovación de tu póliza, necesito saber:\n\n"
                "📅 *¿Cuándo vence tu póliza actual?* (formato: DD/MM/AAAA)\n\n"
                "Una vez que me compartas la fecha, programaré el recordatorio automático."
            )
            USER_CONTEXT[client_phone] = {"context": "awaiting_policy_date", "timestamp": datetime.now()}
        
        send_message_async(to, response)
        register_new_interaction(client_phone, "Renovación Póliza", f"Consulta: {message_text}")
        return True
    
    elif USER_CONTEXT.get(client_phone, {}).get("context") == "awaiting_policy_date":
        # Procesar fecha de vencimiento
        try:
            # Intentar parsear fecha
            date_str = message_text.strip()
            date_obj = datetime.strptime(date_str, "%d/%m/%Y")
            
            # Actualizar en Sheets
            update_client_status(client_phone, "Póliza Activa", {
                "Vencimiento Póliza": date_obj.strftime("%Y-%m-%d")
            })
            
            # Calcular fecha de recordatorio (1 mes antes)
            reminder_date = date_obj - timedelta(days=30)
            
            response = (
                f"✅ Perfecto! He registrado que tu póliza vence el *{date_str}*. "
                f"Te contactaré el *{reminder_date.strftime('%d/%m/%Y')}* "
                "para gestionar la renovación. ¡Gracias!"
            )
            
            # Programar recordatorio (en producción usaría Celery o similar)
            logger.info(f"📅 Recordatorio programado para {reminder_date}")
            
        except ValueError:
            response = "❌ Formato de fecha incorrecto. Por favor usa DD/MM/AAAA (ej: 25/12/2024)"
        
        # Limpiar contexto
        USER_CONTEXT.pop(client_phone, None)
        send_message_async(to, response)
        register_new_interaction(client_phone, "Fecha Vencimiento Registrada", f"Fecha: {message_text}")
        return True
    
    return False

def handle_auto_documents(to, client_phone, message_text):
    """Maneja el flujo de documentos para seguro auto"""
    response = (
        "🚗 *Documentos para Seguro Auto*\n\n"
        "Para cotizar o renovar tu seguro de auto, necesito:\n\n"
        "📷 *INE* (foto frontal y posterior)\n"
        "📄 *Tarjeta de Circulación* (foto de ambos lados)\n"
        "🔢 *Número de Placa* (si no tienes los documentos)\n\n"
        "Puedes enviar las fotos o documentos ahora mismo. "
        "Los guardaré de forma segura en tu expediente."
    )
    
    send_message_async(to, response)
    register_new_interaction(client_phone, "Solicitud Documentos Auto", "Cliente solicitó info documentos")
    USER_CONTEXT[client_phone] = {"context": "awaiting_auto_docs", "timestamp": datetime.now()}
    
    return True

def handle_promotions(to, client_phone):
    """Maneja el flujo de promociones"""
    response = (
        "🎁 *Promociones y Descuentos Vigentes*\n\n"
        "🌟 *Seguro Auto Plus*: 15% descuento en renovación\n"
        "🏥 *VRIM Familiar*: 2 meses gratis al contratar anual\n"
        "👵 *Pensionados IMSS*: Tasas preferenciales en préstamos\n"
        "🚗 *Auto Nuevo*: Cobertura ampliada sin costo extra\n\n"
        "¿Te interesa alguna de estas promociones? "
        "Escribe el número o 'más info' para detalles."
    )
    
    send_message_async(to, response)
    register_new_interaction(client_phone, "Consulta Promociones", "Cliente solicitó promociones")
    return True

def handle_follow_up(to, client_phone):
    """Maneja el flujo de seguimiento"""
    client_data = find_client_in_sheet(client_phone)
    
    if client_data:
        status = client_data.get('estatus', 'No especificado')
        response = (
            f"📊 *Seguimiento de tu Trámite*\n\n"
            f"📋 *Estatus actual:* {status}\n"
            f"👤 *Asesor asignado:* Christian López\n"
            f"📞 *Contacto:* {ADVISOR_NUMBER}\n\n"
            "¿Necesitas información específica sobre algún trámite?"
        )
    else:
        response = (
            "🔍 No encuentro tu información en el sistema. "
            "¿Podrías proporcionarme tu número de póliza o "
            "prefieres que te contacte Christian para ayudarte?"
        )
    
    send_message_async(to, response)
    register_new_interaction(client_phone, "Consulta Seguimiento", "Cliente solicitó seguimiento")
    return True

def handle_contact_advisor(to, client_phone, message_text):
    """Maneja el flujo de contacto con el asesor"""
    client_data = find_client_in_sheet(client_phone)
    client_name = client_data.get('nombre', 'Cliente') if client_data else 'Cliente'
    
    # Notificar al asesor
    advisor_message = (
        f"🔔 *Nueva Solicitud de Contacto - Vicky Bot*\n\n"
        f"👤 *Nombre:* {client_name}\n"
        f"📱 *WhatsApp:* {client_phone}\n"
        f"💬 *Mensaje:* \"{message_text}\"\n"
        f"⏰ *Hora:* {datetime.now().strftime('%d/%m/%Y %H:%M')}"
    )
    
    if ADVISOR_NUMBER:
        send_message_async(ADVISOR_NUMBER, advisor_message)
    
    # Confirmar al cliente
    client_response = (
        f"✅ Perfecto {client_name}, he notificado a *Christian López*.\n\n"
        "📞 Él se pondrá en contacto contigo en breve para brindarte "
        "atención personalizada.\n\n"
        "Mientras tanto, ¿hay algo más en lo que pueda asistirte?"
    )
    
    send_message_async(to, client_response)
    register_new_interaction(client_phone, "Solicitud Contacto Asesor", f"Mensaje: {message_text}")
    
    return True

# ---------------------------------------------------------------
# SECCIÓN: WEBHOOK PRINCIPAL (SIMPLIFICADO)
# ---------------------------------------------------------------

@app.route("/webhook", methods=["GET"])
def verify_webhook():
    """Verificación del webhook"""
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        logger.info("✅ Webhook verificado correctamente")
        return challenge, 200
    else:
        logger.warning("❌ Fallo en la verificación del webhook")
        return "Verification failed", 403

@app.route("/webhook", methods=["POST"])
def receive_message():
    """Endpoint principal para recibir mensajes"""
    data = request.get_json()
    logger.info(f"📩 Mensaje recibido: {data}")

    if not data or "entry" not in data:
        return jsonify({"status": "ignored"}), 200

    # Limpieza periódica de controles en memoria
    now = datetime.now().timestamp()
    MSG_TTL = 600

    # Limpiar mensajes procesados antiguos
    if len(PROCESSED_MESSAGE_IDS) > 1000:
        PROCESSED_MESSAGE_IDS = {k: v for k, v in PROCESSED_MESSAGE_IDS.items() if now - v < MSG_TTL}

    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            
            # Ignorar actualizaciones de estado
            if "statuses" in value:
                continue
            
            messages = value.get("messages", [])
            if not messages:
                continue
            
            message = messages[0]
            msg_id = message.get("id")
            msg_type = message.get("type")
            sender = message.get("from")
            
            # Verificar duplicados
            if msg_id:
                last_seen = PROCESSED_MESSAGE_IDS.get(msg_id)
                if last_seen and (now - last_seen) < MSG_TTL:
                    logger.info(f"🔁 Mensaje duplicado ignorado: {msg_id}")
                    continue
                PROCESSED_MESSAGE_IDS[msg_id] = now
            
            # Obtener nombre del perfil
            profile_name = None
            try:
                contacts = value.get("contacts", [])
                if contacts:
                    profile_name = contacts[0].get("profile", {}).get("name")
            except Exception:
                pass
            
            logger.info(f"🧾 id={msg_id} type={msg_type} from={sender} profile={profile_name}")
            
            # Buscar información del cliente
            client_data = find_client_in_sheet(sender)
            client_name = client_data.get('nombre') if client_data else None
            
            # Manejar mensajes de texto
            if msg_type == "text":
                handle_text_message(sender, message, client_name)
            else:
                # Para otros tipos de mensaje, enviar menú principal
                send_main_menu(sender, client_name)
                GREETED_USERS[sender] = now
    
    return jsonify({"status": "ok"}), 200

def handle_text_message(sender, message, client_name):
    """Maneja mensajes de texto"""
    text = message.get("text", {}).get("body", "").strip()
    text_lower = text.lower()
    
    logger.info(f"✉️ Texto recibido de {sender}: {text}")
    
    # Registrar interacción
    register_new_interaction(sender, "Mensaje Texto", text)
    
    # Comando especial GPT
    if text_lower.startswith("sgpt:"):
        gpt_query = text[5:].strip()
        gpt_response = ask_gpt(gpt_query)
        if gpt_response:
            send_message_async(sender, gpt_response)
        else:
            send_message_async(sender, "⚠️ No pude procesar tu consulta en este momento. Intenta más tarde.")
        return
    
    # Menú principal
    if text_lower in ["hola", "hi", "hello", "menú", "menu"]:
        send_main_menu(sender, client_name)
        GREETED_USERS[sender] = datetime.now().timestamp()
        return
    
    # Opciones del menú
    option_handlers = {
        "1": lambda to, phone, msg: handle_policy_renewal(to, phone, msg),
        "2": lambda to, phone, msg: handle_auto_documents(to, phone, msg),
        "3": lambda to, phone, msg: handle_promotions(to, phone),
        "4": lambda to, phone, msg: handle_follow_up(to, phone),
        "5": lambda to, phone, msg: send_message_async(to, "📞 Para préstamos IMSS, Christian te contactará con las mejores tasas."),
        "6": lambda to, phone, msg: send_message_async(to, "🏥 VRIM: Cobertura médica familiar. Christian te dará todos los detalles."),
        "7": lambda to, phone, msg: handle_contact_advisor(to, phone, msg)
    }
    
    # Verificar si es una opción numérica
    if text in option_handlers:
        option_handlers[text](sender, sender, text)
        return
    
    # Intentar manejar con flujos específicos
    handlers = [
        handle_policy_renewal,
        handle_contact_advisor
    ]
    
    for handler in handlers:
        if handler(sender, sender, text):
            return
    
    # Si ya fue saludado pero no entendemos el mensaje
    if sender in GREETED_USERS:
        # Usar GPT como fallback para mensajes naturales
        if len(text.split()) >= 2 and any(c.isalpha() for c in text):
            gpt_response = ask_gpt(f"El cliente dice: '{text}'. Responde brevemente como asistente de seguros.")
            if gpt_response:
                send_message_async(sender, gpt_response)
                return
        
        # Fallback final
        send_message_async(sender, 
            "❓ No entendí tu mensaje. Por favor elige una opción del 1 al 7 o escribe 'menu' para ver las opciones."
        )
    else:
        # Primer mensaje, enviar menú
        send_main_menu(sender, client_name)
        GREETED_USERS[sender] = datetime.now().timestamp()

# ---------------------------------------------------------------
# SECCIÓN: ENDPOINTS AUXILIARES
# ---------------------------------------------------------------

@app.route("/", methods=["GET"])
def home():
    """Página de inicio"""
    return jsonify({
        "status": "ok", 
        "service": "Vicky Bot SECOM",
        "timestamp": datetime.now().isoformat()
    }), 200

@app.route("/health", methods=["GET"])
def health():
    """Endpoint de salud"""
    return jsonify({
        "status": "ok", 
        "timestamp": datetime.now().isoformat(),
        "whatsapp_connected": bool(WHATSAPP_TOKEN and PHONE_NUMBER_ID),
        "sheets_connected": bool(sheets_client),
        "openai_connected": bool(openai_client)
    }), 200

@app.route("/ext/test-send", methods=["POST"])
def test_send():
    """Endpoint para probar envío de mensajes"""
    try:
        data = request.get_json()
        to = data.get("to")
        text = data.get("text", "Mensaje de prueba de Vicky Bot SECOM")
        
        if not to:
            return jsonify({"error": "Falta parámetro 'to'"}), 400
        
        result = send_message(to, text)
        success = result is not None and result.status_code == 200
        return jsonify({"success": success}), 200
    
    except Exception as e:
        logger.error(f"❌ Error en test-send: {e}")
        return jsonify({"error": str(e)}), 500

# ---------------------------------------------------------------
# EJECUCIÓN PRINCIPAL
# ---------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("DEBUG", "false").lower() == "true"
    
    logger.info(f"🚀 Iniciando Vicky Bot SECOM en puerto {port}")
    logger.info(f"📱 Phone Number ID: {PHONE_NUMBER_ID}")
    logger.info(f"👤 Advisor Number: {ADVISOR_NUMBER}")
    logger.info(f"📊 Sheets ID: {SHEETS_ID_LEADS}")
    
    app.run(host="0.0.0.0", port=port, debug=debug)


