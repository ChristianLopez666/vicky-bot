import os
import json
import logging
import requests
import re
import threading
import time
from datetime import datetime
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# Google APIs
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload

# OpenAI (opcional)
import openai

# ---------------------------------------------------------------
# CARGAR VARIABLES DE ENTORNO
# ---------------------------------------------------------------
load_dotenv()

META_TOKEN = os.getenv("META_TOKEN")
WABA_PHONE_ID = os.getenv("WABA_PHONE_ID")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
ADVISOR_NUMBER = os.getenv("ADVISOR_NUMBER", "5216682478005")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Google
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")
SHEETS_ID_LEADS = os.getenv("SHEETS_ID_LEADS")
SHEETS_TITLE_LEADS = os.getenv("SHEETS_TITLE_LEADS", "Prospectos SECOM Auto")
DRIVE_PARENT_FOLDER_ID = os.getenv("DRIVE_PARENT_FOLDER_ID")

# App
PORT = int(os.getenv("PORT", 5000))

# ---------------------------------------------------------------
# CONFIGURACIÓN INICIAL
# ---------------------------------------------------------------
app = Flask(__name__)

# Estados y datos por usuario
user_state = {}
user_data = {}

# Servicios Google
sheets_service = None
drive_service = None

# Configurar OpenAI si está disponible
if OPENAI_API_KEY:
    openai.api_key = OPENAI_API_KEY

# ---------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(threadName)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

# ---------------------------------------------------------------
# SETUP GOOGLE APIS
# ---------------------------------------------------------------
def setup_google_services():
    """Configura los servicios de Google Sheets y Drive"""
    global sheets_service, drive_service
    
    try:
        if not GOOGLE_CREDENTIALS_JSON:
            logging.error("❌ GOOGLE_CREDENTIALS_JSON no configurado")
            return False
        
        creds_info = json.loads(GOOGLE_CREDENTIALS_JSON)
        credentials = service_account.Credentials.from_service_account_info(creds_info)
        
        sheets_service = build("sheets", "v4", credentials=credentials)
        drive_service = build("drive", "v3", credentials=credentials)
        
        logging.info("✅ Servicios Google configurados correctamente")
        return True
    except Exception as e:
        logging.exception(f"💥 Error configurando servicios Google: {e}")
        return False

# Inicializar servicios Google al startup
if GOOGLE_CREDENTIALS_JSON:
    setup_google_services()

# ---------------------------------------------------------------
# HELPERS WHATSAPP
# ---------------------------------------------------------------
def send_message_with_retry(to: str, text: str, max_retries: int = 3) -> bool:
    """
    Envía mensaje WhatsApp con reintentos exponenciales
    """
    for attempt in range(max_retries):
        try:
            if not META_TOKEN or not WABA_PHONE_ID:
                logging.error("❌ Falta META_TOKEN o WABA_PHONE_ID")
                return False

            url = f"https://graph.facebook.com/v20.0/{WABA_PHONE_ID}/messages"
            headers = {
                "Authorization": f"Bearer {META_TOKEN}",
                "Content-Type": "application/json",
            }
            payload = {
                "messaging_product": "whatsapp",
                "to": str(to),
                "type": "text",
                "text": {"body": text},
            }
            
            resp = requests.post(url, headers=headers, json=payload, timeout=15)
            
            if resp.status_code in (200, 201):
                logging.info(f"✅ Mensaje enviado a {to}")
                return True
            elif resp.status_code == 429:
                wait_time = (2 ** attempt) + 1
                logging.warning(f"⏳ Rate limit, reintento {attempt + 1} en {wait_time}s")
                time.sleep(wait_time)
                continue
            else:
                logging.error(f"❌ Error WhatsApp API {resp.status_code}: {resp.text}")
                return False
                
        except Exception as e:
            logging.exception(f"💥 Error en send_message (intento {attempt + 1}): {e}")
            if attempt < max_retries - 1:
                time.sleep((2 ** attempt) + 1)
    
    return False

def send_message(to: str, text: str) -> bool:
    """Wrapper para compatibilidad con código existente"""
    return send_message_with_retry(to, text)

def send_template_message(to: str, template_name: str, params: list = None) -> bool:
    """
    Envía plantilla WhatsApp con reintentos
    """
    for attempt in range(3):
        try:
            if not META_TOKEN or not WABA_PHONE_ID:
                logging.error("❌ Falta META_TOKEN o WABA_PHONE_ID")
                return False

            url = f"https://graph.facebook.com/v20.0/{WABA_PHONE_ID}/messages"
            headers = {
                "Authorization": f"Bearer {META_TOKEN}",
                "Content-Type": "application/json",
            }
            
            components = []
            if params:
                components = [{
                    "type": "body",
                    "parameters": [{"type": "text", "text": str(param)} for param in params]
                }]
            
            payload = {
                "messaging_product": "whatsapp",
                "to": str(to),
                "type": "template",
                "template": {
                    "name": template_name,
                    "language": {"code": "es_MX"},
                    "components": components
                }
            }
            
            resp = requests.post(url, headers=headers, json=payload, timeout=15)
            
            if resp.status_code in (200, 201):
                logging.info(f"✅ Plantilla {template_name} enviada a {to}")
                return True
            elif resp.status_code == 429:
                wait_time = (2 ** attempt) + 1
                logging.warning(f"⏳ Rate limit en plantilla, reintento en {wait_time}s")
                time.sleep(wait_time)
                continue
            else:
                logging.error(f"❌ Error plantilla WhatsApp {resp.status_code}: {resp.text}")
                return False
                
        except Exception as e:
            logging.exception(f"💥 Error en send_template_message (intento {attempt + 1}): {e}")
            if attempt < 2:
                time.sleep((2 ** attempt) + 1)
    
    return False

# ---------------------------------------------------------------
# HELPERS GOOGLE
# ---------------------------------------------------------------
def match_client_in_sheets(phone_last10: str) -> dict:
    """
    Busca cliente en Sheets por últimos 10 dígitos del teléfono
    Retorna dict con datos del cliente si hay match
    """
    if not sheets_service or not SHEETS_ID_LEADS:
        return None
    
    try:
        # Obtener todas las filas de la hoja
        result = sheets_service.spreadsheets().values().get(
            spreadsheetId=SHEETS_ID_LEADS,
            range=SHEETS_TITLE_LEADS
        ).execute()
        
        rows = result.get('values', [])
        if not rows:
            return None
        
        # Buscar en las filas (asumiendo que el teléfono está en alguna columna)
        for i, row in enumerate(rows):
            for cell in row:
                if phone_last10 in str(cell):
                    return {
                        "row_index": i + 1,  # Sheets es 1-indexed
                        "nombre": row[0] if len(row) > 0 else "ND",
                        "telefono": row[1] if len(row) > 1 else "ND",
                        "full_row": row
                    }
        
        return None
    except Exception as e:
        logging.exception(f"💥 Error buscando en Sheets: {e}")
        return None

def write_followup_to_sheets(row_index: int, note: str, date_iso: str = None):
    """
    Escribe seguimiento en Sheets
    """
    if not sheets_service or not SHEETS_ID_LEADS:
        return False
    
    try:
        if not date_iso:
            date_iso = datetime.now().isoformat()
        
        # Asumiendo que la columna de seguimientos es la última + 1
        range_name = f"{SHEETS_TITLE_LEADS}!Z{row_index}"  # Ajustar según estructura
        
        values = [[f"{date_iso}: {note}"]]
        
        body = {
            'values': values
        }
        
        result = sheets_service.spreadsheets().values().update(
            spreadsheetId=SHEETS_ID_LEADS,
            range=range_name,
            valueInputOption="RAW",
            body=body
        ).execute()
        
        logging.info(f"✅ Seguimiento escrito en fila {row_index}")
        return True
    except Exception as e:
        logging.exception(f"💥 Error escribiendo en Sheets: {e}")
        return False

def upload_to_drive(file_name: str, file_bytes: bytes, mime_type: str, folder_name: str) -> str:
    """
    Sube archivo a Drive y retorna fileId
    """
    if not drive_service or not DRIVE_PARENT_FOLDER_ID:
        return None
    
    try:
        # Buscar o crear carpeta del cliente
        folder_query = f"name='{folder_name}' and '{DRIVE_PARENT_FOLDER_ID}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false"
        folder_result = drive_service.files().list(q=folder_query).execute()
        
        if folder_result.get('files'):
            folder_id = folder_result['files'][0]['id']
        else:
            # Crear nueva carpeta
            folder_metadata = {
                'name': folder_name,
                'mimeType': 'application/vnd.google-apps.folder',
                'parents': [DRIVE_PARENT_FOLDER_ID]
            }
            folder = drive_service.files().create(body=folder_metadata, fields='id').execute()
            folder_id = folder.get('id')
        
        # Subir archivo
        file_metadata = {
            'name': file_name,
            'parents': [folder_id]
        }
        
        media = MediaInMemoryUpload(file_bytes, mimetype=mime_type, resumable=True)
        file = drive_service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id,webViewLink'
        ).execute()
        
        file_id = file.get('id')
        web_link = file.get('webViewLink')
        
        logging.info(f"✅ Archivo subido a Drive: {file_id}")
        return web_link or file_id
        
    except Exception as e:
        logging.exception(f"💥 Error subiendo a Drive: {e}")
        return None

# ---------------------------------------------------------------
# UTILIDADES
# ---------------------------------------------------------------
def interpret_response(text: str) -> str:
    t = (text or "").strip().lower()
    positive = ["sí", "si", "sip", "claro", "ok", "vale", "afirmativo", "yes", "correcto"]
    negative = ["no", "nop", "negativo", "para nada", "not", "incorrecto"]
    if any(k in t for k in positive):
        return "positive"
    if any(k in t for k in negative):
        return "negative"
    return "neutral"

def extract_number(text: str):
    if not text:
        return None
    clean = text.replace(",", "").replace("$", "").replace(" ", "")
    m = re.search(r"(\d{1,12})(\.\d+)?", clean)
    if not m:
        return None
    try:
        return float(m.group(1) + (m.group(2) or ""))
    except Exception:
        return None

def get_client_folder_name(phone: str, client_data: dict = None) -> str:
    """Genera nombre de carpeta para el cliente"""
    if client_data and client_data.get("nombre"):
        nombre = client_data["nombre"]
        # Extraer apellido y nombre
        parts = nombre.split()
        if len(parts) >= 2:
            apellido = parts[0]
            nombre_pila = parts[1]
            last_4 = phone[-4:]
            return f"{apellido}_{nombre_pila}_{last_4}"
    
    # Fallback: usar solo el teléfono
    return f"Cliente_{phone[-8:]}"

def send_main_menu(phone: str):
    menu = (
        "🏦 *INBURSA - SERVICIOS DISPONIBLES*\n\n"
        "1️⃣ Préstamos IMSS Pensionados (Ley 73)\n"
        "2️⃣ Seguros de Auto\n"
        "3️⃣ Seguros de Vida y Salud\n"
        "4️⃣ Tarjetas Médicas VRIM\n"
        "5️⃣ Financiamiento Empresarial\n"
        "6️⃣ Financiamiento Práctico Empresarial (desde 24 hrs)\n"
        "7️⃣ Contactar con Christian\n\n"
        "Escribe el número o el nombre del servicio que te interesa."
    )
    send_message(phone, menu)

# ---------------------------------------------------------------
# GPT (opcional por comando "sgpt: ...")
# ---------------------------------------------------------------
def ask_gpt(prompt: str, model: str = "gpt-3.5-turbo", temperature: float = 0.7) -> str:
    try:
        if not OPENAI_API_KEY:
            return "Servicio GPT no configurado."
            
        resp = openai.ChatCompletion.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
            max_tokens=400,
        )
        return resp.choices[0].message["content"].strip()
    except Exception as e:
        logging.exception(f"Error OpenAI: {e}")
        return "Lo siento, ocurrió un error al consultar GPT."

def is_gpt_command(msg: str) -> bool:
    return (msg or "").strip().lower().startswith("sgpt:")

# ---------------------------------------------------------------
# EMBUDOS EXISTENTES (PRESERVADOS)
# ---------------------------------------------------------------
def funnel_prestamo_imss(user_id: str, user_message: str):
    """Embudo IMSS Ley 73 - Preservado del código original"""
    state = user_state.get(user_id, "imss_beneficios")
    datos = user_data.get(user_id, {})

    if state == "imss_beneficios":
        send_message(
            user_id,
            "💰 *Préstamo para Pensionados IMSS (Ley 73)*\n"
            "- Montos desde $40,000 hasta $650,000\n"
            "- Descuento vía pensión\n"
            "- Plazos de 12 a 60 meses\n"
            "- Depósito directo a tu cuenta\n"
            "- Sin aval ni garantía\n\n"
            "🏦 *Beneficios adicionales si recibes tu pensión en Inbursa*\n"
            "- Tasas preferenciales\n"
            "- Acceso a seguro de vida sin costo\n"
            "- Anticipo de nómina disponible\n"
            "- Atención personalizada 24/7\n\n"
            "(Los beneficios de nómina son *adicionales* y *no obligatorios*)."
        )
        send_message(user_id, "¿Eres pensionado o jubilado del IMSS bajo la Ley 73?")
        user_state[user_id] = "imss_preg_pensionado"
        return jsonify({"status": "ok", "funnel": "prestamo_imss"})

    if state == "imss_preg_pensionado":
        resp = interpret_response(user_message)
        if resp == "negative":
            send_main_menu(user_id)
            user_state.pop(user_id, None)
            user_data.pop(user_id, None)
            return jsonify({"status": "ok"})
        if resp == "positive":
            send_message(user_id, "¿Cuánto recibes aproximadamente al mes por concepto de pensión?")
            user_state[user_id] = "imss_preg_monto_pension"
            return jsonify({"status": "ok"})
        send_message(user_id, "Por favor responde *sí* o *no* para continuar.")
        return jsonify({"status": "ok"})

    if state == "imss_preg_monto_pension":
        monto = extract_number(user_message)
        if monto is None:
            send_message(user_id, "Indica el monto mensual que recibes por pensión (ej. 6500).")
            return jsonify({"status": "ok"})
        datos["pension_mensual"] = monto
        user_data[user_id] = datos
        if monto < 5000:
            send_message(
                user_id,
                "Por ahora los créditos aplican a pensiones a partir de $5,000.\n"
                "Puedo notificar a nuestro asesor para ofrecerte otra opción. ¿Deseas que lo haga?"
            )
            user_state[user_id] = "imss_ofrecer_asesor"
            return jsonify({"status": "ok"})
        send_message(user_id, "Perfecto 👏 ¿Qué monto de préstamo te gustaría solicitar (mínimo $40,000)?")
        user_state[user_id] = "imss_preg_monto_solicitado"
        return jsonify({"status": "ok"})

    if state == "imss_ofrecer_asesor":
        resp = interpret_response(user_message)
        if resp == "positive":
            formatted = (
                "🔔 NUEVO PROSPECTO – PRÉSTAMO IMSS\n"
                f"WhatsApp: {user_id}\n"
                f"Pensión mensual: ${datos.get('pension_mensual','ND')}\n"
                "Estatus: Pensión baja, requiere opciones alternativas"
            )
            send_message(ADVISOR_NUMBER, formatted)
            send_message(user_id, "¡Listo! Un asesor te contactará con opciones alternativas.")
            send_main_menu(user_id)
            user_state.pop(user_id, None)
            user_data.pop(user_id, None)
            return jsonify({"status": "ok"})
        send_message(user_id, "Perfecto, si deseas podemos continuar con otros servicios.")
        send_main_menu(user_id)
        user_state.pop(user_id, None)
        user_data.pop(user_id, None)
        return jsonify({"status": "ok"})

    if state == "imss_preg_monto_solicitado":
        monto_sol = extract_number(user_message)
        if monto_sol is None or monto_sol < 40000:
            send_message(user_id, "Indica el monto que deseas solicitar (mínimo $40,000), ej. 65000.")
            return jsonify({"status": "ok"})
        datos["monto_solicitado"] = monto_sol
        user_data[user_id] = datos
        send_message(user_id, "¿Cuál es tu *nombre completo*?")
        user_state[user_id] = "imss_preg_nombre"
        return jsonify({"status": "ok"})

    if state == "imss_preg_nombre":
        datos["nombre"] = user_message.title()
        user_data[user_id] = datos
        send_message(user_id, "¿Cuál es tu *teléfono de contacto*?")
        user_state[user_id] = "imss_preg_telefono"
        return jsonify({"status": "ok"})

    if state == "imss_preg_telefono":
        datos["telefono_contacto"] = user_message.strip()
        user_data[user_id] = datos
        send_message(user_id, "¿En qué *ciudad* vives?")
        user_state[user_id] = "imss_preg_ciudad"
        return jsonify({"status": "ok"})

    if state == "imss_preg_ciudad":
        datos["ciudad"] = user_message.title()
        user_data[user_id] = datos
        send_message(user_id, "¿Ya recibes tu pensión en *Inbursa*? (Sí/No)")
        user_state[user_id] = "imss_preg_nomina_inbursa"
        return jsonify({"status": "ok"})

    if state == "imss_preg_nomina_inbursa":
        resp = interpret_response(user_message)
        datos["nomina_inbursa"] = "Sí" if resp == "positive" else "No" if resp == "negative" else "ND"
        if resp not in ("positive", "negative"):
            send_message(user_id, "Por favor responde *sí* o *no* para continuar.")
            return jsonify({"status": "ok"})
        send_message(
            user_id,
            "✅ ¡Listo! Tu crédito ha sido *preautorizado*.\n"
            "Un asesor financiero (Christian López) se pondrá en contacto contigo."
        )
        formatted = (
            "🔔 NUEVO PROSPECTO – PRÉSTAMO IMSS\n"
            f"Nombre: {datos.get('nombre','ND')}\n"
            f"WhatsApp: {user_id}\n"
            f"Teléfono: {datos.get('telefono_contacto','ND')}\n"
            f"Ciudad: {datos.get('ciudad','ND')}\n"
            f"Monto solicitado: ${datos.get('monto_solicitado','ND')}\n"
            f"Nómina Inbursa: {datos.get('nomina_inbursa','ND')}"
        )
        send_message(ADVISOR_NUMBER, formatted)
        send_main_menu(user_id)
        user_state.pop(user_id, None)
        user_data.pop(user_id, None)
        return jsonify({"status": "ok"})

    send_main_menu(user_id)
    return jsonify({"status": "ok"})

def funnel_credito_empresarial(user_id: str, user_message: str):
    """Embudo Crédito Empresarial - Preservado del código original"""
    state = user_state.get(user_id, "emp_beneficios")
    datos = user_data.get(user_id, {})

    if state == "emp_beneficios":
        send_message(
            user_id,
            "🏢 *Crédito Empresarial Inbursa*\n"
            "- Financiamiento desde $100,000 hasta $100,000,000\n"
            "- Tasas preferenciales y plazos flexibles\n"
            "- Sin aval con buen historial\n"
            "- Apoyo a PYMES, comercios y empresas consolidadas\n\n"
            "¿Eres empresario o representas una empresa?"
        )
        user_state[user_id] = "emp_confirmacion"
        return jsonify({"status": "ok", "funnel": "empresarial"})

    if state == "emp_confirmacion":
        resp = interpret_response(user_message)
        lowered = (user_message or "").lower()
        if resp == "positive" or any(k in lowered for k in ["empresario", "empresa", "negocio", "pyme", "comercio"]):
            send_message(user_id, "¿A qué *se dedica* tu empresa?")
            user_state[user_id] = "emp_actividad"
            return jsonify({"status": "ok"})
        if resp == "negative":
            send_main_menu(user_id)
            user_state.pop(user_id, None)
            user_data.pop(user_id, None)
            return jsonify({"status": "ok"})
        send_message(user_id, "Responde *sí* o *no* para continuar.")
        return jsonify({"status": "ok"})

    if state == "emp_actividad":
        datos["actividad_empresa"] = user_message.title()
        user_data[user_id] = datos
        send_message(user_id, "¿Qué *monto* deseas solicitar? (mínimo $100,000)")
        user_state[user_id] = "emp_monto"
        return jsonify({"status": "ok"})

    if state == "emp_monto":
        monto_solicitado = extract_number(user_message)
        if monto_solicitado is None or monto_solicitado < 100000:
            send_message(user_id, "Indica el monto (mínimo $100,000), ej. 250000.")
            return jsonify({"status": "ok"})
        datos["monto_solicitado"] = monto_solicitado
        user_data[user_id] = datos
        send_message(user_id, "¿Cuál es tu *nombre completo*?")
        user_state[user_id] = "emp_nombre"
        return jsonify({"status": "ok"})

    if state == "emp_nombre":
        datos["nombre"] = user_message.title()
        user_data[user_id] = datos
        send_message(user_id, "¿Cuál es tu *número telefónico*?")
        user_state[user_id] = "emp_telefono"
        return jsonify({"status": "ok"})

    if state == "emp_telefono":
        datos["telefono"] = user_message.strip()
        user_data[user_id] = datos
        send_message(user_id, "¿En qué *ciudad* está ubicada tu empresa?")
        user_state[user_id] = "emp_ciudad"
        return jsonify({"status": "ok"})

    if state == "emp_ciudad":
        datos["ciudad"] = user_message.title()
        user_data[user_id] = datos

        send_message(
            user_id,
            "✅ Gracias por la información. Un asesor financiero (Christian López) "
            "se pondrá en contacto contigo en breve para continuar con tu solicitud."
        )

        formatted = (
            "🔔 NUEVO PROSPECTO – CRÉDITO EMPRESARIAL\n"
            f"Nombre: {datos.get('nombre','ND')}\n"
            f"Teléfono: {datos.get('telefono','ND')}\n"
            f"Ciudad: {datos.get('ciudad','ND')}\n"
            f"Monto solicitado: ${datos.get('monto_solicitado','ND')}\n"
            f"Actividad: {datos.get('actividad_empresa','ND')}\n"
            f"WhatsApp: {user_id}"
        )
        send_message(ADVISOR_NUMBER, formatted)

        send_main_menu(user_id)
        user_state.pop(user_id, None)
        user_data.pop(user_id, None)
        return jsonify({"status": "ok"})

    send_main_menu(user_id)
    return jsonify({"status": "ok"})

def funnel_financiamiento_practico(user_id: str, user_message: str):
    """Embudo Financiamiento Práctico - Preservado del código original"""
    state = user_state.get(user_id, "fp_intro")
    datos = user_data.get(user_id, {})

    if state == "fp_intro":
        send_message(
            user_id,
            "💼 *Financiamiento Práctico Empresarial – Inbursa*\n\n"
            "⏱️ *Aprobación desde 24 horas*\n"
            "💰 *Crédito simple sin garantía* desde $100,000 MXN\n"
            "🏢 Para empresas y *personas físicas con actividad empresarial*.\n\n"
            "¿Deseas conocer si puedes acceder a este financiamiento? (Sí/No)"
        )
        user_state[user_id] = "fp_confirmar_interes"
        return jsonify({"status": "ok", "funnel": "financiamiento_practico"})

    if state == "fp_confirmar_interes":
        resp = interpret_response(user_message)
        if resp == "negative":
            send_message(
                user_id,
                "Perfecto 👍. Un ejecutivo te contactará para conocer tus necesidades y "
                "ofrecerte otras opciones."
            )
            send_message(
                ADVISOR_NUMBER,
                f"📩 Prospecto NO interesado en Financiamiento Práctico\nNúmero: {user_id}"
            )
            send_main_menu(user_id)
            user_state.pop(user_id, None)
            user_data.pop(user_id, None)
            return jsonify({"status": "ok"})
        if resp == "positive":
            send_message(user_id, "Excelente 🙌. Comencemos con un *perfilamiento* rápido.\n"
                                  "1️⃣ ¿Cuál es el *giro de la empresa*?")
            user_state[user_id] = "fp_q1_giro"
            return jsonify({"status": "ok"})
        send_message(user_id, "Responde *sí* o *no* para continuar.")
        return jsonify({"status": "ok"})

    preguntas = {
        "fp_q1_giro": "2️⃣ ¿Qué *antigüedad fiscal* tiene la empresa?",
        "fp_q2_antiguedad": "3️⃣ ¿Es *persona física con actividad empresarial* o *persona moral*?",
        "fp_q3_tipo": "4️⃣ ¿Qué *edad tiene el representante legal*?",
        "fp_q4_edad": "5️⃣ ¿Buró de crédito empresa y accionistas al día? (Responde *positivo* o *negativo*).",
        "fp_q5_buro": "6️⃣ ¿Aproximadamente *cuánto factura al año* la empresa?",
        "fp_q6_facturacion": "7️⃣ ¿Tiene *facturación constante* en los últimos seis meses? (Sí/No)",
        "fp_q7_constancia": "8️⃣ ¿Cuánto es el *monto de financiamiento* que requiere?",
        "fp_q8_monto": "9️⃣ ¿Cuenta con la *opinión de cumplimiento positiva* ante el SAT?",
        "fp_q9_opinion": "🔟 ¿Qué *tipo de financiamiento* requiere?",
        "fp_q10_tipo": "1️⃣1️⃣ ¿Cuenta con financiamiento actualmente? ¿Con quién?",
        "fp_q11_actual": "📝 ¿Deseas dejar *algún comentario adicional* para el asesor?",
    }

    orden = [
        "fp_q1_giro", "fp_q2_antiguedad", "fp_q3_tipo", "fp_q4_edad", "fp_q5_buro",
        "fp_q6_facturacion", "fp_q7_constancia", "fp_q8_monto", "fp_q9_opinion",
        "fp_q10_tipo", "fp_q11_actual", "fp_comentario"
    ]

    if state in orden[:-1]:
        datos[state] = user_message
        user_data[user_id] = datos
        next_index = orden.index(state) + 1
        next_state = orden[next_index]
        user_state[user_id] = next_state

        if next_state == "fp_comentario":
            send_message(user_id, preguntas["fp_q11_actual"])
        else:
            send_message(user_id, preguntas[next_state])
        return jsonify({"status": "ok"})

    if state == "fp_comentario":
        datos["comentario"] = user_message
        formatted = (
            "🔔 *NUEVO PROSPECTO – FINANCIAMIENTO PRÁCTICO EMPRESARIAL*\n\n"
            f"📱 WhatsApp: {user_id}\n"
            f"🏢 Giro: {datos.get('fp_q1_giro','ND')}\n"
            f"📆 Antigüedad Fiscal: {datos.get('fp_q2_antiguedad','ND')}\n"
            f"👤 Tipo de Persona: {datos.get('fp_q3_tipo','ND')}\n"
            f"🧑‍⚖️ Edad Rep. Legal: {datos.get('fp_q4_edad','ND')}\n"
            f"📊 Buró empresa/accionistas: {datos.get('fp_q5_buro','ND')}\n"
            f"💵 Facturación anual: {datos.get('fp_q6_facturacion','ND')}\n"
            f"📈 6 meses constantes: {datos.get('fp_q7_constancia','ND')}\n"
            f"🎯 Monto requerido: {datos.get('fp_q8_monto','ND')}\n"
            f"🧾 Opinión SAT: {datos.get('fp_q9_opinion','ND')}\n"
            f"🏦 Tipo de financiamiento: {datos.get('fp_q10_tipo','ND')}\n"
            f"💼 Financiamiento actual: {datos.get('fp_q11_actual','ND')}\n"
            f"💬 Comentario: {datos.get('comentario','Ninguno')}"
        )
        send_message(ADVISOR_NUMBER, formatted)
        send_message(
            user_id,
            "✅ Gracias por la información. Un asesor financiero (Christian López) "
            "se pondrá en contacto contigo en breve para continuar con tu solicitud."
        )
        send_main_menu(user_id)
        user_state.pop(user_id, None)
        user_data.pop(user_id, None)
        return jsonify({"status": "ok"})

    send_main_menu(user_id)
    return jsonify({"status": "ok"})

# ---------------------------------------------------------------
# FLUJO SEGURO AUTO (SECOM)
# ---------------------------------------------------------------
def funnel_seguro_auto(user_id: str, user_message: str):
    """Embudo Seguro de Auto con gestión de documentos"""
    state = user_state.get(user_id, "auto_intro")
    datos = user_data.get(user_id, {})

    if state == "auto_intro":
        send_message(
            user_id,
            "🚗 *Seguros de Auto Inbursa*\n\n"
            "✅ Cobertura amplia\n"
            "✅ Asistencia vial 24/7\n"
            "✅ Responsabilidad Civil\n"
            "✅ Robo total/parcial\n"
            "✅ Daños materiales\n\n"
            "Para cotizar, necesitamos:\n"
            "1. INE (por ambos lados)\n"
            "2. Tarjeta de circulación o número de placas\n\n"
            "¿Deseas proceder con la cotización?"
        )
        user_state[user_id] = "auto_confirmar"
        return jsonify({"status": "ok", "funnel": "seguro_auto"})

    if state == "auto_confirmar":
        resp = interpret_response(user_message)
        if resp == "positive":
            send_message(
                user_id,
                "Perfecto 🙌\n\n"
                "Por favor envía:\n"
                "1. 📸 INE (frente y vuelta)\n"
                "2. 🚗 Tarjeta de circulación o número de placas\n\n"
                "Puedes enviar las fotos/documentos en este chat."
            )
            user_state[user_id] = "auto_esperando_docs"
            return jsonify({"status": "ok"})
        elif resp == "negative":
            send_message(
                user_id,
                "Entendido. ¿Te gustaría que te contactemos en una fecha específica "
                "o cuando tu seguro actual esté por vencer?"
            )
            user_state[user_id] = "auto_recordatorio"
            return jsonify({"status": "ok"})
        else:
            send_message(user_id, "Por favor responde *sí* o *no* para continuar.")
            return jsonify({"status": "ok"})

    if state == "auto_recordatorio":
        # Programar recordatorio
        fecha_match = re.search(r'(\d{1,2}[/-]\d{1,2}[/-]\d{4})', user_message)
        if fecha_match:
            fecha_vencimiento = fecha_match.group(1)
            datos["fecha_vencimiento"] = fecha_vencimiento
            user_data[user_id] = datos
            
            # Guardar en Sheets para recordatorio
            client_match = match_client_in_sheets(user_id[-10:])
            if client_match:
                write_followup_to_sheets(
                    client_match["row_index"],
                    f"Recordatorio seguro auto - Vence: {fecha_vencimiento}",
                    datetime.now().isoformat()
                )
            
            send_message(
                user_id,
                f"✅ Perfecto, te contactaremos 30 días antes de tu vencimiento ({fecha_vencimiento})."
            )
        else:
            send_message(
                user_id,
                "✅ Te contactaremos en 7 días para verificar tu interés. "
                "También puedes escribirnos cuando necesites la cotización."
            )
        
        send_main_menu(user_id)
        user_state.pop(user_id, None)
        return jsonify({"status": "ok"})

    if state == "auto_esperando_docs":
        # El manejo de documentos se hace en el webhook principal
        send_message(
            user_id,
            "✅ He recibido tu documentación. Un asesor revisará la información "
            "y te enviará la cotización en breve."
        )
        send_main_menu(user_id)
        user_state.pop(user_id, None)
        return jsonify({"status": "ok"})

    send_main_menu(user_id)
    return jsonify({"status": "ok"})

# ---------------------------------------------------------------
# GESTIÓN DE MEDIA/DOCUMENTOS
# ---------------------------------------------------------------
def download_media(media_id: str) -> bytes:
    """Descarga media de WhatsApp Cloud API"""
    try:
        url = f"https://graph.facebook.com/v20.0/{media_id}"
        headers = {"Authorization": f"Bearer {META_TOKEN}"}
        resp = requests.get(url, headers=headers, timeout=30)
        if resp.status_code == 200:
            media_url = resp.json().get("url")
            if media_url:
                media_resp = requests.get(media_url, headers=headers, timeout=30)
                if media_resp.status_code == 200:
                    return media_resp.content
        logging.error(f"❌ Error descargando media {media_id}: {resp.text}")
        return None
    except Exception as e:
        logging.exception(f"💥 Error en download_media: {e}")
        return None

def handle_media_message(user_id: str, message: dict):
    """Maneja mensajes multimedia y los sube a Drive"""
    try:
        media_type = message.get("type")
        media_id = None
        
        if media_type == "image":
            media_id = message["image"]["id"]
            mime_type = "image/jpeg"
            file_ext = "jpg"
        elif media_type == "document":
            media_id = message["document"]["id"]
            mime_type = message["document"].get("mime_type", "application/octet-stream")
            file_ext = message["document"].get("filename", "documento").split('.')[-1]
        elif media_type == "audio":
            media_id = message["audio"]["id"]
            mime_type = "audio/ogg"
            file_ext = "ogg"
        elif media_type == "video":
            media_id = message["video"]["id"]
            mime_type = "video/mp4"
            file_ext = "mp4"
        else:
            logging.warning(f"⚠️ Tipo de media no soportado: {media_type}")
            return False

        # Descargar media
        file_bytes = download_media(media_id)
        if not file_bytes:
            return False

        # Buscar datos del cliente para nombre de carpeta
        client_match = match_client_in_sheets(user_id[-10:])
        folder_name = get_client_folder_name(user_id, client_match)
        
        # Crear nombre de archivo
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        file_name = f"{media_type}_{timestamp}.{file_ext}"

        # Subir a Drive
        drive_link = upload_to_drive(file_name, file_bytes, mime_type, folder_name)
        
        if drive_link:
            # Notificar al asesor
            notify_text = (
                f"📎 *Nuevo documento recibido*\n"
                f"👤 Cliente: {user_id}\n"
                f"📂 Tipo: {media_type}\n"
                f"🔗 Drive: {drive_link}\n"
                f"📁 Carpeta: {folder_name}"
            )
            send_message(ADVISOR_NUMBER, notify_text)
            
            # Confirmar al cliente
            send_message(user_id, "✅ Documento recibido y en proceso. Te contactaremos pronto.")
            return True
        else:
            send_message(user_id, "❌ Error al procesar el documento. Intenta nuevamente.")
            return False

    except Exception as e:
        logging.exception(f"💥 Error en handle_media_message: {e}")
        send_message(user_id, "❌ Error al procesar el archivo. Intenta más tarde.")
        return False

# ---------------------------------------------------------------
# ENDPOINTS PRINCIPALES
# ---------------------------------------------------------------
@app.route("/webhook", methods=["GET"])
def webhook_get():
    """Verificación del webhook"""
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    
    if mode == "subscribe" and token == VERIFY_TOKEN:
        logging.info("✅ Webhook verificado")
        return challenge, 200
    else:
        logging.warning("❌ Verificación fallida")
        return "Verification failed", 403

@app.route("/webhook", methods=["POST"])
def webhook_post():
    """Manejador principal de mensajes"""
    try:
        data = request.get_json()
        logging.info(f"📨 Webhook recibido: {json.dumps(data, indent=2)}")
        
        if not data:
            return jsonify({"status": "error", "message": "No data"}), 400

        # Procesar entrada de WhatsApp
        entry = data.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        value = changes.get("value", {})
        messages = value.get("messages", [])
        
        if not messages:
            return jsonify({"status": "ok", "message": "No messages"}), 200

        message = messages[0]
        user_id = message.get("from")
        message_type = message.get("type")
        
        if not user_id:
            return jsonify({"status": "error", "message": "No user_id"}), 400

        logging.info(f"👤 Mensaje de {user_id}: tipo={message_type}")

        # Manejar mensajes de texto
        if message_type == "text":
            text = message["text"]["body"].strip()
            
            # Comando GPT
            if is_gpt_command(text):
                prompt = text[5:].strip()
                if prompt:
                    response = ask_gpt(prompt)
                    send_message(user_id, response)
                return jsonify({"status": "ok"})
            
            # Saludos o menú
            lowered = text.lower()
            if any(g in lowered for g in ["hola", "hi", "hello", "buenas", "menú", "menu", "opciones"]):
                user_state.pop(user_id, None)
                user_data.pop(user_id, None)
                send_main_menu(user_id)
                return jsonify({"status": "ok"})
            
            # Router por estado actual
            current_state = user_state.get(user_id, "")
            
            if current_state.startswith("imss_"):
                return funnel_prestamo_imss(user_id, text)
            elif current_state.startswith("emp_"):
                return funnel_credito_empresarial(user_id, text)
            elif current_state.startswith("fp_"):
                return funnel_financiamiento_practico(user_id, text)
            elif current_state.startswith("auto_"):
                return funnel_seguro_auto(user_id, text)
            else:
                # Router por opciones del menú
                if any(k in lowered for k in ["1", "imss", "préstamo", "prestamo", "ley 73", "pensión", "pension"]):
                    return funnel_prestamo_imss(user_id, text)
                elif any(k in lowered for k in ["2", "auto", "seguro auto", "seguros auto"]):
                    return funnel_seguro_auto(user_id, text)
                elif any(k in lowered for k in ["3", "vida", "salud", "seguro vida", "seguro salud"]):
                    send_message(user_id, "🏥 *Seguros de Vida y Salud*\n\nContamos con coberturas personalizadas para ti y tu familia. Un asesor te contactará con los detalles.")
                    send_message(ADVISOR_NUMBER, f"👤 Cliente interesado en Seguro Vida/Salud\n📱 WhatsApp: {user_id}")
                    send_main_menu(user_id)
                    return jsonify({"status": "ok"})
                elif any(k in lowered for k in ["4", "vrim", "tarjeta médica", "tarjeta medica"]):
                    send_message(user_id, "🏥 *Tarjetas Médicas VRIM*\n\nAcceso a más de 10,000 establecimientos médicos. Un asesor te contactará con los beneficios.")
                    send_message(ADVISOR_NUMBER, f"👤 Cliente interesado en VRIM\n📱 WhatsApp: {user_id}")
                    send_main_menu(user_id)
                    return jsonify({"status": "ok"})
                elif any(k in lowered for k in ["5", "empresarial", "pyme", "empresa"]):
                    return funnel_credito_empresarial(user_id, text)
                elif any(k in lowered for k in ["6", "financiamiento práctico", "financiamiento practico", "crédito simple", "credito simple"]):
                    return funnel_financiamiento_practico(user_id, text)
                elif any(k in lowered for k in ["7", "contactar", "asesor", "christian", "humano"]):
                    send_message(user_id, "✅ Un asesor (Christian López) se pondrá en contacto contigo en breve.")
                    send_message(ADVISOR_NUMBER, f"🔔 SOLICITUD DE CONTACTO DIRECTO\n📱 WhatsApp: {user_id}\n💬 Motivo: Contacto directo solicitado")
                    send_main_menu(user_id)
                    return jsonify({"status": "ok"})
                else:
                    # Buscar match en Sheets y saludar personalizado
                    client_match = match_client_in_sheets(user_id[-10:])
                    if client_match:
                        nombre = client_match.get("nombre", "cliente")
                        send_message(user_id, f"¡Hola {nombre}! 👋 Bienvenido nuevamente a Inbursa SECOM.")
                    else:
                        send_message(user_id, "¡Hola! 👋 Soy Vicky, tu asistente virtual de Inbursa SECOM.")
                    send_main_menu(user_id)
                    return jsonify({"status": "ok"})

        # Manejar mensajes multimedia
        elif message_type in ["image", "document", "audio", "video"]:
            success = handle_media_message(user_id, message)
            return jsonify({"status": "ok", "media_processed": success})

        else:
            logging.info(f"⚠️ Tipo de mensaje no manejado: {message_type}")
            return jsonify({"status": "ok"})

    except Exception as e:
        logging.exception(f"💥 Error en webhook_post: {e}")
        return jsonify({"status": "error", "message": "Internal error"}), 500

# ---------------------------------------------------------------
# ENDPOINTS AUXILIARES
# ---------------------------------------------------------------
@app.route("/health", methods=["GET"])
def health():
    """Health check principal"""
    return jsonify({
        "status": "ok",
        "service": "Vicky Bot Inbursa",
        "timestamp": datetime.now().isoformat()
    })

@app.route("/ext/health", methods=["GET"])
def ext_health():
    """Health check externo"""
    return jsonify({"status": "ok"})

@app.route("/ext/test-send", methods=["POST"])
def test_send():
    """Endpoint para probar envío de mensajes"""
    try:
        data = request.get_json()
        to = data.get("to")
        text = data.get("text")
        
        if not to or not text:
            return jsonify({"error": "Faltan 'to' o 'text'"}), 400
        
        success = send_message(to, text)
        return jsonify({"ok": success})
    except Exception as e:
        logging.exception(f"💥 Error en test-send: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/ext/send-promo", methods=["POST"])
def send_promo():
    """Envío masivo no bloqueante"""
    def send_promo_thread():
        try:
            data = request.get_json()
            phones = data.get("phones", [])
            message = data.get("message", "")
            template = data.get("template")
            
            for phone in phones:
                if template:
                    send_template_message(phone, template)
                else:
                    send_message(phone, message)
                time.sleep(1)  # Rate limiting
                
            logging.info(f"✅ Promo enviada a {len(phones)} contactos")
        except Exception as e:
            logging.exception(f"💥 Error en send_promo_thread: {e}")
    
    thread = threading.Thread(target=send_promo_thread, daemon=True)
    thread.start()
    
    return jsonify({"status": "processing", "message": "Envío iniciado en segundo plano"})

# ---------------------------------------------------------------
# INICIALIZACIÓN
# ---------------------------------------------------------------
# ---------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    logging.info(f"🚀 Iniciando Vicky Bot en puerto {port}")
    app.run(host="0.0.0.0", port=port)
