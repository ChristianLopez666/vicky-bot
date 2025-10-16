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
from oauth2client.service_account import ServiceAccountCredentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from google.oauth2 import service_account
import openai
import io
from datetime import datetime, timedelta
import re

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
        scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
        creds_dict = eval(GOOGLE_SHEETS_CREDENTIALS)
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        client = gspread.authorize(creds)
        sheet = client.open("Prospectos SECOM Auto").sheet1
        return sheet
    except Exception as e:
        logger.error(f"Error configurando Google Sheets: {e}")
        return None

# Configuración de Google Drive
def setup_google_drive():
    """Configura la conexión con Google Drive"""
    try:
        creds_dict = eval(GOOGLE_DRIVE_CREDENTIALS)
        credentials = service_account.Credentials.from_service_account_info(creds_dict)
        drive_service = build('drive', 'v3', credentials=credentials)
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
        # Descargar el archivo
        response = requests.get(file_url)
        if response.status_code != 200:
            logger.error(f"No se pudo descargar el archivo: {response.status_code}")
            return False
        
        file_content = io.BytesIO(response.content)
        
        # Crear nombre de carpeta con formato Apellido_Nombre_####
        folder_name = f"Cliente_{client_number[-4:]}"
        
        # Buscar o crear carpeta
        folder_query = f"name='{folder_name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
        folders = drive_service.files().list(q=folder_query).execute()
        
        if folders['files']:
            folder_id = folders['files'][0]['id']
        else:
            # Crear carpeta si no existe
            folder_metadata = {
                'name': folder_name,
                'mimeType': 'application/vnd.google-apps.folder'
            }
            folder = drive_service.files().create(body=folder_metadata).execute()
            folder_id = folder['id']
        
        # Subir archivo
        file_metadata = {
            'name': filename,
            'parents': [folder_id]
        }
        
        media = MediaIoBaseUpload(file_content, mimetype='application/octet-stream')
        file = drive_service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id'
        ).execute()
        
        logger.info(f"Archivo {filename} subido exitosamente a Drive")
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
        
        # Registrar en Google Sheets
        if sheet:
            next_row = len(sheet.get_all_values()) + 1
            sheet.update(f"A{next_row}:E{next_row}", [[
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                client_name,
                client_number,
                service_type,
                "Activo"
            ]])
        
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
            logger.info(f"Datos recibidos: {data}")
            
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
        
        # Respuestas basadas en opciones del menú
        if text_body == "1":
            response = "🏥 *PENSIONES IMSS*\n\n¿Cumples alguno de estos requisitos?\n\n• 60 años o más\n• 500 semanas cotizadas\n• Trabajaste antes de 1997\n\nSi cumples alguno, ¡podrías tener derecho a tu pensión! Un asesor se contactará contigo."
            send_whatsapp_message(client_number, response)
            notify_advisor(client_number, "Cliente IMSS", "Pensiones IMSS", "Cumple requisitos para pensión")
            
        elif text_body == "2":
            response = "🚗 *SEGUROS DE AUTO*\n\nProtege tu auto con las mejores coberturas:\n\n• Responsabilidad Civil\n• Daños Materiales\n• Robo Total\n• Asistencia Vial\n\nPor favor, envía fotos de:\n1. INE (ambos lados)\n2. Tarjeta de circulación"
            send_whatsapp_message(client_number, response)
            
        elif text_body == "5":
            response = "🏥 *TARJETAS MÉDICAS VRIM*\n\nBeneficios exclusivos para militares:\n\n• Atención médica especializada\n• Medicamentos gratuitos\n• Estudios de laboratorio\n• Consultas con especialistas\n\nUn asesor te contactará para explicarte el proceso."
            send_whatsapp_message(client_number, response)
            notify_advisor(client_number, "Cliente VRIM", "Tarjetas Médicas VRIM")
            
        elif text_body == "7":
            response = "👨‍💼 Te pondré en contacto con Christian, nuestro especialista. Él te atenderá personalmente en breve."
            send_whatsapp_message(client_number, response)
            notify_advisor(client_number, "Cliente Christian", "Contactar Asesor", "Solicita contacto directo con Christian")
            
        else:
            # Usar GPT para respuestas no reconocidas
            gpt_prompt = f"El cliente dijo: '{text_body}'. Responde educadamente como Vicky de SECOM y sugiere las opciones del menú: 1) Pensiones IMSS, 2) Seguros de Auto, 5) Tarjetas médicas VRIM, 7) Contactar a Christian."
            gpt_response = get_gpt_response(gpt_prompt)
            send_whatsapp_message(client_number, gpt_response)
            
    except Exception as e:
        logger.error(f"Error procesando mensaje de texto: {e}")

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
        
        # Descargar y subir a Drive
        filename = f"{message_type}_{client_number}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        
        if upload_to_drive(media_url, filename, client_number):
            logger.info(f"Archivo {filename} procesado exitosamente")
            
            # Notificar al asesor
            file_type = "imagen" if message_type == "image" else "documento"
            notification = f"📎 Se recibió un {file_type} del cliente {client_number}"
            send_whatsapp_message(ADVISOR_NUMBER, notification)
            
        else:
            logger.error(f"Error subiendo archivo a Drive: {filename}")
            
    except Exception as e:
        logger.error(f"Error procesando mensaje multimedia: {e}")

# Ruta de salud para Render
@app.route("/")
def health_check():
    """Endpoint de salud para verificar que la app está funcionando"""
    return jsonify({
        "status": "active",
        "service": "Vicky Bot SECOM",
        "timestamp": datetime.now().isoformat()
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

# GPT (OpenAI o compatible)
try:
    from openai import OpenAI
except Exception:
    OpenAI = None

load_dotenv()

# ---------------------------
# Configuración y logging
# ---------------------------
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')
logger = logging.getLogger("vicky-secom")

# WhatsApp / Meta
WABA_PHONE_ID = os.getenv("WABA_PHONE_ID", "712597741555047")
WABA_TOKEN = os.getenv("WABA_TOKEN", "")
BOT_NUMBER = os.getenv("BOT_NUMBER", "6681922865")  # Número SECOM
ADVISOR_WHATSAPP = os.getenv("ADVISOR_WHATSAPP", "5216682478005")
META_API_BASE = os.getenv("META_API_BASE", "https://graph.facebook.com/v21.0")

# Google Sheets / Drive
SHEETS_ID_LEADS = os.getenv("SHEETS_ID_LEADS", "")
SHEETS_TITLE_LEADS = os.getenv("SHEETS_TITLE_LEADS", "Prospectos SECOM Auto")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON", "")
DRIVE_FOLDER_ID = os.getenv("DRIVE_FOLDER_ID", "")  # Carpeta raíz para respaldos

# GPT
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "")  # Opcional para gateways
GPT_MODEL = os.getenv("GPT_MODEL", "gpt-4o-mini")
GPT_TEMPERATURE = float(os.getenv("GPT_TEMPERATURE", "0.5"))

# Otros
TZ = os.getenv("TIMEZONE", "America/Mazatlan")

# ---------------------------
# Utilidades
# ---------------------------

def normalize_msisdn(msisdn: str) -> str:
    """Regresa E.164 (México) o últimos 10 dígitos para matching en Sheets."""
    digits = ''.join([c for c in msisdn if c.isdigit()])
    if digits.startswith('521') and len(digits) >= 13:
        return '+' + digits
    if digits.startswith('52') and len(digits) >= 12:
        return '+521' + digits[2:]
    if len(digits) == 10:
        return '+52' + digits  # E.164 corto
    if len(digits) > 10 and digits.endswith(digits[-10:]):
        return '+52' + digits[-10:]
    return '+' + digits


def last10(msisdn: str) -> str:
    d = ''.join([c for c in msisdn if c.isdigit()])
    return d[-10:] if len(d) >= 10 else d


# ---------------------------
# Clientes externos (Sheets/Drive/GPT)
# ---------------------------

def build_gspread_client() -> gspread.Client:
    if not GOOGLE_CREDENTIALS_JSON:
        raise RuntimeError("Falta GOOGLE_CREDENTIALS_JSON")
    info = json.loads(GOOGLE_CREDENTIALS_JSON)
    scopes = [
        'https://www.googleapis.com/auth/spreadsheets',
        'https://www.googleapis.com/auth/drive'
    ]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    return gspread.authorize(creds)


def build_drive_service():
    info = json.loads(GOOGLE_CREDENTIALS_JSON)
    scopes = ['https://www.googleapis.com/auth/drive']
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    return build('drive', 'v3', credentials=creds)


def open_leads_sheet(gc: gspread.Client):
    sh = gc.open_by_key(SHEETS_ID_LEADS)
    return sh.worksheet(SHEETS_TITLE_LEADS)


def gpt_client() -> Optional[OpenAI]:
    if not OPENAI_API_KEY or OpenAI is None:
        return None
    if OPENAI_BASE_URL:
        return OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL)
    return OpenAI(api_key=OPENAI_API_KEY)


def gpt_warm_reply(system_prompt: str, user_message: str) -> str:
    """Genera respuesta con tono cálido y profesional."""
    cli = gpt_client()
    if cli is None:
        # Fallback determinista en ausencia de GPT
        return (
            "¡Gracias por escribirnos! 🙌\n\n"
            "Te apoyo con gusto. Si me compartes un poco más de detalle, puedo darte una respuesta inmediata "
            "y acercarte con Christian cuando sea necesario. 😊"
        )
    try:
        resp = cli.chat.completions.create(
            model=GPT_MODEL,
            temperature=GPT_TEMPERATURE,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message}
            ],
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        logger.warning(f"GPT error: {e}")
        return (
            "Gracias por tu mensaje. Estoy procesando tu solicitud en este momento. "
            "Si notas demora, en breve te escribo con la información completa. 🙏"
        )


# ---------------------------
# WhatsApp Cloud API helpers
# ---------------------------

def wa_url(path: str) -> str:
    return f"{META_API_BASE}/{WABA_PHONE_ID}/{path}"


def wa_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {WABA_TOKEN}",
        "Content-Type": "application/json"
    }


def wa_send_text(to: str, body: str) -> bool:
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body}
    }
    r = requests.post(wa_url("messages"), headers=wa_headers(), json=payload, timeout=20)
    ok = r.status_code in (200, 201)
    if not ok:
        logger.warning(f"wa_send_text fail {r.status_code}: {r.text}")
    return ok


def wa_send_template(to: str, name: str, lang: str = "es_MX", components: Optional[List[Dict[str, Any]]] = None) -> bool:
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {"name": name, "language": {"code": lang}}
    }
    if components:
        payload["template"]["components"] = components
    r = requests.post(wa_url("messages"), headers=wa_headers(), json=payload, timeout=20)
    ok = r.status_code in (200, 201)
    if not ok:
        logger.warning(f"wa_send_template fail {r.status_code}: {r.text}")
    return ok


def wa_get_media_url(media_id: str) -> Optional[str]:
    # Paso 1: obtener URL de descarga
    url = f"https://graph.facebook.com/v21.0/{media_id}"
    r = requests.get(url, headers={"Authorization": f"Bearer {WABA_TOKEN}"}, timeout=20)
    if r.status_code != 200:
        logger.warning(f"wa_get_media_url fail {r.status_code}: {r.text}")
        return None
    return r.json().get("url")


def wa_download_media(media_id: str, target_path: str) -> bool:
    media_url = wa_get_media_url(media_id)
    if not media_url:
        return False
    r = requests.get(media_url, headers={"Authorization": f"Bearer {WABA_TOKEN}"}, timeout=60)
    if r.status_code != 200:
        logger.warning(f"wa_download_media file fail {r.status_code}")
        return False
    with open(target_path, 'wb') as f:
        f.write(r.content)
    return True


# ---------------------------
# Drive helpers
# ---------------------------

def drive_upload(local_path: str, drive_folder_id: str, new_name: str) -> Optional[str]:
    service = build_drive_service()
    file_metadata = {"name": new_name, "parents": [drive_folder_id]} if drive_folder_id else {"name": new_name}
    media = MediaFileUpload(local_path, resumable=True)
    file = service.files().create(body=file_metadata, media_body=media, fields="id, webViewLink").execute()
    return file.get("webViewLink")


# ---------------------------
# Sheets helpers
# ---------------------------

def sheet_find_row_by_phone(ws, phone_last10: str) -> Optional[int]:
    # Busca en columna con teléfonos; asume que existe encabezado "whatsapp" o "telefono".
    header = ws.row_values(1)
    try:
        col_idx = header.index("whatsapp") + 1
    except ValueError:
        try:
            col_idx = header.index("telefono") + 1
        except ValueError:
            col_idx = 0
    if col_idx == 0:
        return None
    col = ws.col_values(col_idx)
    for idx, val in enumerate(col, start=1):
        d = ''.join([c for c in val if c.isdigit()]) if val else ''
        if d.endswith(phone_last10):
            return idx
    return None


def sheet_update_status(ws, row: int, status: str):
    header = ws.row_values(1)
    try:
        col_idx = header.index("estatus") + 1
    except ValueError:
        # si no existe, agrega al final
        ws.update_cell(1, len(header) + 1, "estatus")
        col_idx = len(header) + 1
    ws.update_cell(row, col_idx, status)


def sheet_write_value(ws, row: int, field: str, value: str):
    header = ws.row_values(1)
    if field in header:
        col_idx = header.index(field) + 1
    else:
        ws.update_cell(1, len(header) + 1, field)
        col_idx = len(header) + 1
    ws.update_cell(row, col_idx, value)


# ---------------------------
# Motor sencillo de recordatorios (thread)
# ---------------------------
class ReminderWorker(threading.Thread):
    def __init__(self, interval_sec: int = 600):
        super().__init__(daemon=True)
        self.interval = interval_sec
        self.running = True

    def run(self):
        logger.info("ReminderWorker iniciado")
        while self.running:
            try:
                self.tick()
            except Exception as e:
                logger.warning(f"Reminder tick error: {e}")
            time.sleep(self.interval)

    def tick(self):
        if not SHEETS_ID_LEADS:
            return
        gc = build_gspread_client()
        ws = open_leads_sheet(gc)
        header = ws.row_values(1)
        # Columnas esperadas
        campos = ["fecha_vencimiento", "retry_at", "whatsapp", "nombre"]
        for c in campos:
            if c not in header:
                header.append(c)
                ws.update_cell(1, len(header), c)
        rows = ws.get_all_records()
        today = datetime.now().date()
        for i, row in enumerate(rows, start=2):
            try:
                phone = str(row.get("whatsapp") or "")
                nombre = row.get("nombre") or "Cliente"
                venc = row.get("fecha_vencimiento")
                retry_at = row.get("retry_at")
                to = normalize_msisdn(phone)
                # Recordatorio -30 días
                if venc:
                    try:
                        d = datetime.strptime(str(venc), "%Y-%m-%d").date()
                    except ValueError:
                        continue
                    delta = (d - today).days
                    if delta == 30:
                        body = (
                            f"Hola {nombre}, te escribimos para recordarte que tu póliza de auto vence el {d}.\n\n"
                            "Si lo deseas, podemos prepararte una renovación con beneficios y comparar opciones. "
                            "¿Te gustaría que avancemos? 🚗✨"
                        )
                        wa_send_text(to, body)
                # Reintento +7 días
                if retry_at:
                    try:
                        rdate = datetime.strptime(str(retry_at), "%Y-%m-%d").date()
                    except ValueError:
                        continue
                    if rdate == today:
                        body = (
                            f"Hola {nombre}, hace unos días te escribimos para apoyarte con tu póliza/solicitud.\n\n"
                            "¿Deseas que te contacte un asesor con una propuesta sin compromiso? 😊"
                        )
                        wa_send_text(to, body)
                        # Limpiar retry_at
                        sheet_write_value(ws, i, "retry_at", "")
            except Exception as e:
                logger.warning(f"Reminder loop row {i} error: {e}")


reminder_worker = ReminderWorker(interval_sec=600)


# ---------------------------
# Flask App
# ---------------------------
app = Flask(__name__)


@app.route('/ext/health', methods=['GET'])
def health():
    return jsonify({"status": "ok", "service": "vicky-secom"})


@app.route('/ext/test-send', methods=['POST'])
def test_send():
    data = request.json or {}
    to = normalize_msisdn(str(data.get("to", ADVISOR_WHATSAPP)))
    text = data.get("text", "Prueba de envío desde Vicky SECOM ✅")
    ok = wa_send_text(to, text)
    return jsonify({"ok": ok})


# ---------------------------
# Webhook WhatsApp
# ---------------------------
@app.route('/webhook', methods=['GET'])
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    verify_token = os.getenv("VERIFY_TOKEN", "vicky-verify-2025")
    if mode == "subscribe" and token == verify_token:
        return challenge, 200
    return "forbidden", 403


@app.route('/webhook', methods=['POST'])
def receive_webhook():
    payload = request.json
    try:
        entries = payload.get('entry', [])
        for entry in entries:
            changes = entry.get('changes', [])
            for change in changes:
                value = change.get('value', {})
                messages = value.get('messages', [])
                for msg in messages:
                    process_incoming_message(value, msg)
    except Exception as e:
        logger.exception(f"webhook error: {e}")
    return jsonify({"ok": True})


# ---------------------------
# Lógica principal de mensajes
# ---------------------------
MENU_TEXT = (
    "¡Hola! Soy Vicky SECOM 🤖✨\n\n"
    "¿En qué te puedo apoyar hoy?\n\n"
    "1) Renovación de póliza (auto)\n"
    "2) Enviar documentos para cotización (auto)\n"
    "3) Promociones SECOM\n"
    "4) Seguimiento / estado\n"
    "5) Préstamos IMSS (Ley 73)\n"
    "6) Tarjeta Médica VRIM\n"
    "7) Contactar con Christian"
)

SYSTEM_WARM = (
    "Eres Vicky, una asistente cálida, clara y profesional."
    " Respondes en español de México con empatía y brevedad útil."
    " No prometes imposibles; si hace falta un asesor humano, lo indicas y notificas al asesor."
)


def process_incoming_message(value: Dict[str, Any], msg: Dict[str, Any]):
    from_wa = msg.get('from')  # E.164 sin +
    wa_to_notify = normalize_msisdn('+' + from_wa) if not from_wa.startswith('+') else normalize_msisdn(from_wa)

    msg_type = msg.get('type')

    # Texto
    if msg_type == 'text':
        text = msg.get('text', {}).get('body', '').strip()
        handle_text_message(wa_to_notify, text)
        return

    # Documentos / Imágenes (para cotización auto)
    if msg_type in ('document', 'image'):
        handle_media_message(wa_to_notify, msg)
        return

    # Otros tipos
    wa_send_text(wa_to_notify, "Recibí tu mensaje. ¿Podrías decirme si deseas ver el menú? Escribe *menu*. ✨")


def handle_text_message(to: str, text: str):
    t = text.lower()
    if t in ("menu", "hola", "buenas", "inicio", "hola vicky"):
        wa_send_text(to, MENU_TEXT)
        return

    if t.startswith('1'):
        # Renovación
        body = (
            "Perfecto. Para ayudarte con tu renovación 🚗, por favor indícame la *fecha de vencimiento* en formato AAAA-MM-DD.\n\n"
            "Ejemplo: 2025-11-30."
        )
        wa_send_text(to, body)
        return

    if t.startswith('2'):
        body = (
            "Ok. Para cotizar tu seguro de auto necesito:\n\n"
            "• Foto de tu INE\n"
            "• Tarjeta de circulación *o* número de placa\n\n"
            "Puedes enviarlos aquí y yo los canalizo con el asesor. 📄📎"
        )
        wa_send_text(to, body)
        return

    if t.startswith('3'):
        wa_send_text(to, "Puedo enviarte promociones personalizadas según tu registro en SECOM. ¿Deseas continuar? (sí/no)")
        return

    if t.startswith('4'):
        gc = build_gspread_client()
        ws = open_leads_sheet(gc)
        row = sheet_find_row_by_phone(ws, last10(to))
        if not row:
            wa_send_text(to, "No encuentro tu registro todavía. Si gustas, compárteme tu nombre y revisaré con el asesor. 😊")
            return
        sheet_update_status(ws, row, "en seguimiento")
        wa_send_text(to, "Listo. Actualicé tu estatus a *en seguimiento*. Si deseas que te llame Christian, escribe: *opción 7*. ✉️")
        return

    if t.startswith('5'):
        # IMSS Ley 73 (on-demand)
        body = (
            "Excelente. Te ayudo con *Préstamo IMSS (Ley 73)*.\n\n"
            "¿Eres pensionado del IMSS bajo Ley 73? (sí/no)\n\n"
            "*Nota:* Los beneficios adicionales por nómina Inbursa son opcionales y pueden mejorar tu experiencia."
        )
        wa_send_text(to, body)
        return

    if t.startswith('6'):
        wa_send_text(to, "La *Tarjeta Médica VRIM* ofrece acceso a servicios y descuentos de salud. ¿Deseas conocer los planes disponibles? (sí/no)")
        return

    if t.startswith('7'):
        wa_send_text(to, "Perfecto. Le avisaré a Christian para que te contacte a la brevedad. 🙌")
        wa_send_text(ADVISOR_WHATSAPP, f"📣 Cliente solicita contacto: {to}")
        return

    # Flujo inteligente con GPT (tono cálido)
    reply = gpt_warm_reply(SYSTEM_WARM, text)
    wa_send_text(to, reply)


# ---------------------------
# Manejo de archivos entrantes (cotización auto)
# ---------------------------

def handle_media_message(to: str, msg: Dict[str, Any]):
    gc = build_gspread_client()
    ws = open_leads_sheet(gc)
    row = sheet_find_row_by_phone(ws, last10(to))
    if not row:
        wa_send_text(to, "Recibí tu archivo. ¿Podrías confirmarme tu *nombre completo* para asociarlo correctamente? 😊")
        return

    # Obtén media_id y nombre sugerido
    media_id = None
    filename = None
    if msg['type'] == 'document':
        media_id = msg['document']['id']
        filename = msg['document'].get('filename', 'documento.pdf')
    elif msg['type'] == 'image':
        media_id = msg['image']['id']
        filename = f"imagen_{int(time.time())}.jpg"

    if not media_id:
        wa_send_text(to, "No pude obtener el archivo. ¿Podrías reenviarlo, por favor?")
        return

    # Descarga temporal
    os.makedirs('tmp', exist_ok=True)
    local_path = os.path.join('tmp', filename)
    if not wa_download_media(media_id, local_path):
        wa_send_text(to, "Ocurrió un detalle descargando tu archivo. Intentemos nuevamente, por favor. 🙏")
        return

    # Nombre normalizado para Drive
    nombre = "Cliente"
    try:
        nombre = ws.cell(row, ws.row_values(1).index("nombre") + 1).value or "Cliente"
    except Exception:
        pass
    alias = f"{nombre.replace(' ', '_')}_{last10(to)[-4:]}"
    drive_name = f"{alias}_{filename}"

    # Subir a Drive
    link = drive_upload(local_path, DRIVE_FOLDER_ID, drive_name)

    # Registrar en Sheet
    sheet_write_value(ws, row, "archivo_ultimo", drive_name)
    if link:
        sheet_write_value(ws, row, "archivo_link", link)

    # Reenviar al asesor (notificación)
    wa_send_text(ADVISOR_WHATSAPP, f"📎 Doc recibido de {nombre} ({to}) — {drive_name}\n{link or ''}")
    wa_send_text(to, "¡Gracias! Recibí tus documentos y ya los compartí con el asesor. Te escribiré en cuanto tenga la cotización. 🙌")


# ---------------------------
# Endpoints auxiliares SECOM
# ---------------------------
@app.route('/ext/send-promo', methods=['POST'])
def send_promo_async():
    data = request.json or {}
    segment = data.get('segment', 'todos')
    template_name = data.get('template', '')  # si se usa plantilla
    text = data.get('text', '')  # o texto libre

    def worker():
        try:
            gc = build_gspread_client()
            ws = open_leads_sheet(gc)
            rows = ws.get_all_records()
            for row in rows:
                phone = str(row.get('whatsapp') or row.get('telefono') or '')
                to = normalize_msisdn(phone)
                if not to:
                    continue
                if template_name:
                    wa_send_template(to, template_name)
                elif text:
                    wa_send_text(to, text)
                time.sleep(0.2)  # leve pacing
        except Exception as e:
            logger.exception(f"send_promo worker error: {e}")

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"ok": True, "status": "queued"}), 202


@app.route('/ext/set-renewal', methods=['POST'])
def set_renewal():
    data = request.json or {}
    phone = normalize_msisdn(str(data.get('phone', '')))
    date_str = data.get('fecha_vencimiento')  # AAAA-MM-DD
    if not phone or not date_str:
        return jsonify({"ok": False, "error": "phone/fecha_vencimiento requeridos"}), 400
    try:
        _ = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"ok": False, "error": "fecha_vencimiento inválida"}), 400

    gc = build_gspread_client()
    ws = open_leads_sheet(gc)
    row = sheet_find_row_by_phone(ws, last10(phone))
    if not row:
        return jsonify({"ok": False, "error": "no encontrado en SECOM"}), 404
    sheet_write_value(ws, row, "fecha_vencimiento", date_str)
    return jsonify({"ok": True})


@app.route('/ext/set-retry', methods=['POST'])
def set_retry():
    data = request.json or {}
    phone = normalize_msisdn(str(data.get('phone', '')))
    days = int(data.get('days', 7))
    rdate = (datetime.utcnow() + timedelta(days=days)).strftime("%Y-%m-%d")

    gc = build_gspread_client()
    ws = open_leads_sheet(gc)
    row = sheet_find_row_by_phone(ws, last10(phone))
    if not row:
        return jsonify({"ok": False, "error": "no encontrado en SECOM"}), 404
    sheet_write_value(ws, row, "retry_at", rdate)
    return jsonify({"ok": True, "retry_at": rdate})


# ---------------------------
# Autoarranque de worker de recordatorios
# ---------------------------
@app.before_first_request
def start_workers():
    if not reminder_worker.is_alive():
        reminder_worker.start()
        logger.info("Workers iniciados")


# ---------------------------
# Entrypoint local
# ---------------------------
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)))
