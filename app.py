import os
import json
import logging
import requests
import re
import threading
import pytz
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from dotenv import load_dotenv

from google.oauth2.service_account import Credentials
import gspread
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# Cargar variables de entorno
load_dotenv()

# Configuración de logging
logging.basicConfig(level=logging.INFO)

# Inicializar Flask
app = Flask(__name__)

# Variables de entorno
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
WHATSAPP_TOKEN = os.getenv("META_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
ADVISOR_WHATSAPP = os.getenv("ADVISOR_WHATSAPP", "5216682478005")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Google Drive base
def _drive_service():
    creds = Credentials.from_service_account_info(json.loads(os.getenv("GOOGLE_CREDENTIALS_JSON")))
    return build("drive", "v3", credentials=creds)

def save_file_to_drive(local_path, filename, folder_id):
    service = _drive_service()
    file_metadata = {"name": filename, "parents": [folder_id]}
    media = MediaFileUpload(local_path, resumable=True)
    uploaded = service.files().create(body=file_metadata, media_body=media, fields="id").execute()
    return uploaded.get("id")

# 🧠 Controles en memoria
PROCESSED_MESSAGE_IDS = {}
GREETED_USERS = {}
LAST_INTENT = {}
USER_CONTEXT = {}
IMSS_MANUAL_CACHE = {"ts": None, "text": None}

# Nuevo: Estados para flujos específicos
USER_FLOWS = {}

MSG_TTL = 600
GREET_TTL = 24 * 3600
CTX_TTL = 4 * 3600

# Funciones WhatsApp
def vx_wa_send_text(to, body):
    url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body}
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=9)
        logging.info(f"vx_wa_send_text {r.status_code} {r.text[:160]}")
        return r.status_code == 200
    except Exception as e:
        logging.error(f"vx_wa_send_text error: {e}")
        return False

def vx_wa_send_interactive(to, body, buttons):
    """Envía mensaje con botones interactivos"""
    url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    
    button_items = []
    for i, button in enumerate(buttons):
        button_items.append({
            "type": "reply",
            "reply": {
                "id": f"btn_{i+1}",
                "title": button
            }
        })
    
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body},
            "action": {
                "buttons": button_items
            }
        }
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=12)
        logging.info(f"vx_wa_send_interactive {r.status_code}")
        return r.status_code == 200
    except Exception as e:
        logging.error(f"vx_wa_send_interactive error: {e}")
        return False

def vx_wa_send_template(to, template, params=None):
    url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    comps = []
    if params:
        comps = [{
            "type": "body",
            "parameters": [{"type": "text", "text": str(v)} for v in params.values()]
        }]
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": template,
            "language": {"code": "es_MX"},
            **({"components": comps} if comps else {})
        }
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=12)
        logging.info(f"vx_wa_send_template {r.status_code} {r.text[:160]}")
        return r.status_code == 200
    except Exception as e:
        logging.error(f"vx_wa_send_template error: {e}")
        return False

# Helpers
def vx_last10(phone: str) -> str:
    if not phone:
        return ""
    p = re.sub(r"[^\d]", "", str(phone))
    p = re.sub(r"^(52|521)", "", p)
    return p[-10:] if len(p) >= 10 else p

def vx_sheet_find_by_phone(last10: str):
    try:
        creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
        sheets_id = os.getenv("SHEETS_ID_LEADS")
        sheets_title = os.getenv("SHEETS_TITLE_LEADS")
        if not creds_json or not sheets_id or not sheets_title:
            return None
        creds = Credentials.from_service_account_info(json.loads(creds_json))
        client = gspread.authorize(creds)
        ws = client.open_by_key(sheets_id).worksheet(sheets_title)
        rows = ws.get_all_records()
        for row in rows:
            if vx_last10(row.get("WhatsApp", "")) == last10:
                return row
        return None
    except Exception as e:
        logging.error(f"vx_sheet_find_by_phone error: {e}")
        return None

def notify_advisor(prospect_data, flow_type):
    """Notifica al asesor sobre nuevo prospecto calificado"""
    if not ADVISOR_WHATSAPP:
        logging.warning("No hay número de asesor configurado")
        return False
    
    if flow_type == "imss":
        message = f"🎯 NUEVO PROSPECTO - PRÉSTAMO IMSS\n\n"
        message += f"• Nombre: {prospect_data.get('nombre', 'Por confirmar')}\n"
        message += f"• Teléfono: {prospect_data.get('phone')}\n"
        message += f"• Edad: {prospect_data.get('edad')} años\n"
        message += f"• Antigüedad IMSS: {prospect_data.get('antiguedad')} años\n"
        message += f"• Nómina Inbursa: {'Sí' if prospect_data.get('nomina_inbursa') else 'No'}\n"
        message += f"• Cumple requisitos: Sí ✅"
    
    elif flow_type == "empresarial":
        message = f"🏢 NUEVO PROSPECTO - CRÉDITO EMPRESARIAL\n\n"
        message += f"• Nombre: {prospect_data.get('nombre')}\n"
        message += f"• Empresa: {prospect_data.get('empresa')}\n"
        message += f"• Giro: {prospect_data.get('giro')}\n"
        message += f"• Monto: ${prospect_data.get('monto')}\n"
        message += f"• Tiempo operando: {prospect_data.get('tiempo_operacion')}\n"
        message += f"• Teléfono: {prospect_data.get('phone')}\n"
        message += f"• Cita: {prospect_data.get('cita')}"
    
    return vx_wa_send_text(ADVISOR_WHATSAPP, message)

# Flujo Préstamos IMSS Corregido
def start_imss_flow(phone, campaign_source="general"):
    USER_FLOWS[phone] = {
        "flow": "imss",
        "step": "benefits_explanation",
        "data": {"campaign": campaign_source},
        "timestamp": datetime.now()
    }
    
    benefits_text = """🏥 *Préstamo IMSS Ley 73* 

Al cambiar tu nómina o pensión a Inbursa, obtienes *beneficios exclusivos*:

✓ *Tasas de interés preferentes*
✓ *Sin comisiones* por manejo de cuenta  
✓ *Dinero disponible inmediatamente*
✓ *Seguro de vida incluido* sin costo adicional

*¿Te gustaría domiciliar tu pensión en Inbursa para acceder a estos beneficios?*

💡 Recuerda que:
• No es necesario cerrar tu cuenta actual
• Si lo deseas, después de 3 meses puedes regresar tu nómina sin problema"""

    return vx_wa_send_interactive(phone, benefits_text, 
                                ["Sí, quiero los beneficios", "No, prefiero no cambiar"])

def handle_imss_response(phone, message, user_flow):
    step = user_flow["step"]
    data = user_flow["data"]
    
    if step == "benefits_explanation":
        if "sí" in message.lower() or "si" in message.lower() or "quiero" in message.lower():
            user_flow["step"] = "check_requirements"
            user_flow["data"]["nomina_inbursa"] = True
            vx_wa_send_text(phone, "¡Excelente decisión! Ahora verifiquemos que cumples con los requisitos...")
            vx_wa_send_text(phone, "¿Cuál es tu edad?")
        
        else:
            user_flow["step"] = "alternative_products"
            alternative_text = """Entiendo perfectamente. La oferta seguirá disponible por si cambias de opinión.

Mientras tanto, ¿te interesa conocer otros productos disponibles?

• 🚗 Seguros de Auto
• 🏥 Seguros de Vida y Salud  
• 💳 Tarjetas Médicas VRIM

Responde con el número de tu interés:
1. Seguros de Auto
2. Seguros de Vida
3. Tarjetas VRIM"""
            vx_wa_send_text(phone, alternative_text)
    
    elif step == "check_requirements":
        if "edad" not in data:
            try:
                edad = int(message)
                if 18 <= edad <= 70:
                    data["edad"] = edad
                    vx_wa_send_text(phone, "¿Cuántos años de antigüedad tienes en el IMSS?")
                else:
                    vx_wa_send_text(phone, "La edad debe estar entre 18 y 70 años. Por favor, ingresa tu edad nuevamente:")
            except:
                vx_wa_send_text(phone, "Por favor, ingresa tu edad en números:")
        
        elif "antiguedad" not in data:
            try:
                antiguedad = int(message)
                if antiguedad >= 1:
                    data["antiguedad"] = antiguedad
                    data["phone"] = phone
                    
                    # Prospecto calificado - notificar asesor
                    notify_advisor(data, "imss")
                    
                    success_text = f"""✅ *¡Perfecto! Cumples con todos los requisitos*

• Edad: {data['edad']} años ✓
• Antigüedad IMSS: {data['antiguedad']} años ✓  
• Nómina Inbursa: Confirmada ✓

*En este momento notificaré a tu asesor* para que se ponga en contacto contigo y continúe con tu trámite.

📞 Te contactaremos al número: {vx_last10(phone)}"""
                    
                    vx_wa_send_text(phone, success_text)
                    USER_FLOWS.pop(phone, None)  # Finalizar flujo
                    
                else:
                    vx_wa_send_text(phone, "Se requiere al menos 1 año de antigüedad. ¿Cuántos años tienes en el IMSS?")
            except:
                vx_wa_send_text(phone, "Por favor, ingresa los años de antigüedad en números:")

# Flujo Créditos Empresariales
def start_empresarial_flow(phone, campaign_source="general"):
    USER_FLOWS[phone] = {
        "flow": "empresarial", 
        "step": "get_name",
        "data": {"campaign": campaign_source},
        "timestamp": datetime.now()
    }
    
    welcome_text = """🏢 *Créditos Empresariales*

¡Excelente! Vamos a crear un plan *a la medida* de las necesidades de tu negocio.

Para empezar, por favor ingresa tu *nombre completo*:"""
    
    return vx_wa_send_text(phone, welcome_text)

def handle_empresarial_response(phone, message, user_flow):
    step = user_flow["step"]
    data = user_flow["data"]
    
    if step == "get_name":
        data["nombre"] = message
        user_flow["step"] = "get_company"
        vx_wa_send_text(phone, "¿Cuál es el *nombre de tu empresa*?")
    
    elif step == "get_company":
        data["empresa"] = message
        user_flow["step"] = "get_industry" 
        vx_wa_send_text(phone, "¿A qué *giro* se dedica tu negocio?")
    
    elif step == "get_industry":
        data["giro"] = message
        user_flow["step"] = "get_amount"
        vx_wa_send_text(phone, "¿Qué *monto aproximado* requieres para tu negocio?")
    
    elif step == "get_amount":
        data["monto"] = message
        user_flow["step"] = "get_experience"
        vx_wa_send_text(phone, "¿Cuánto *tiempo tiene operando* tu negocio (en años)?")
    
    elif step == "get_experience":
        data["tiempo_operacion"] = message
        user_flow["step"] = "schedule_appointment"
        
        schedule_text = """📅 *Agendemos tu cita con nuestro especialista*

Nuestro asesor analizará tu caso específico y diseñará un plan financiero personalizado.

*Horarios disponibles:*
1. Lunes - 10:00 AM
2. Martes - 2:00 PM  
3. Miércoles - 4:00 PM
4. Jueves - 11:00 AM
5. Viernes - 3:00 PM

Responde con el *número* de tu horario preferido:"""
        
        vx_wa_send_text(phone, schedule_text)
    
    elif step == "schedule_appointment":
        time_slots = {
            "1": "Lunes - 10:00 AM",
            "2": "Martes - 2:00 PM", 
            "3": "Miércoles - 4:00 PM",
            "4": "Jueves - 11:00 AM",
            "5": "Viernes - 3:00 PM"
        }
        
        if message in time_slots:
            data["cita"] = time_slots[message]
            data["phone"] = phone
            
            # Notificar al asesor
            notify_advisor(data, "empresarial")
            
            confirmation_text = f"""✅ *Cita confirmada*

📅 *Fecha:* {data['cita']}
👨‍💼 *Especialista:* Asesor Empresarial
📞 *Contacto:* {vx_last10(phone)}

*Nuestro asesor se contactará contigo* en el horario agendado para:
• Analizar tu caso específico
• Diseñar tu plan financiero personalizado
• Explicarte todas las opciones disponibles

💼 *Recomendación:* Ten a la mano documentación de tu empresa para la reunión."""
            
            vx_wa_send_text(phone, confirmation_text)
            USER_FLOWS.pop(phone, None)  # Finalizar flujo
        else:
            vx_wa_send_text(phone, "Por favor, elige una opción del 1 al 5:")

# Manejo de mensajes principal
def handle_incoming_message(phone, message):
    # Detectar campañas desde redes sociales
    message_lower = message.lower()
    
    if "préstamoimss" in message_lower or "prestamoimss" in message_lower:
        return start_imss_flow(phone, "redes_sociales")
    
    elif "créditoempresarial" in message_lower or "creditoempresarial" in message_lower:
        return start_empresarial_flow(phone, "redes_sociales")
    
    # Verificar si el usuario está en un flujo activo
    if phone in USER_FLOWS:
        user_flow = USER_FLOWS[phone]
        
        if user_flow["flow"] == "imss":
            handle_imss_response(phone, message, user_flow)
        elif user_flow["flow"] == "empresarial":
            handle_empresarial_response(phone, message, user_flow)
        return
    
    # Menú principal para mensajes no dirigidos a campañas específicas
    menu_text = """¡Hola! Soy Vicky, tu asistente virtual de Inbursa. 🌟

¿En qué te puedo ayudar today?

• 🏥 *Préstamos IMSS* - Con beneficios exclusivos
• 🏢 *Créditos Empresariales* - Planes a la medida  
• 📋 *Otros productos* - Seguros, tarjetas y más

Responde con el número de tu interés:
1. Préstamos IMSS
2. Créditos Empresariales  
3. Otros productos"""
    
    vx_wa_send_text(phone, menu_text)

# Endpoint salud
@app.route("/ext/health")
def ext_health():
    return jsonify({"status": "ok"})

# Endpoint send-promo consolidado
@app.route("/ext/send-promo", methods=["POST"])
def ext_send_promo():
    data = request.get_json(force=True, silent=True) or {}
    to = data.get("to")
    text = data.get("text")
    template = data.get("template")
    params = data.get("params", {})
    use_secom = data.get("secom", False)

    targets = []
    if isinstance(to, str):
        targets = [to]
    elif isinstance(to, list):
        targets = [str(x) for x in to if str(x).strip()]

    if use_secom:
        try:
            creds = Credentials.from_service_account_info(json.loads(os.getenv("GOOGLE_CREDENTIALS_JSON")))
            gs = gspread.authorize(creds)
            sh = gs.open_by_key(os.getenv("SHEETS_ID_LEADS"))
            ws = sh.worksheet(os.getenv("SHEETS_TITLE_LEADS"))
            numbers = [str(r.get("WhatsApp", "")) for r in ws.get_all_records() if r.get("WhatsApp")]
            targets.extend(numbers)
        except Exception as e:
            logging.error(f"Error leyendo SECOM en send-promo: {e}")

    targets = list(set(targets))

    def _worker():
        results = []
        for num in targets:
            ok = False
            try:
                if template:
                    ok = vx_wa_send_template(num, template, params)
                elif text:
                    ok = vx_wa_send_text(num, text)
                results.append({"to": num, "sent": ok})
            except Exception as e:
                logging.error(f"send_promo worker error: {e}")
                results.append({"to": num, "sent": False, "error": str(e)})
        logging.info(f"send_promo done: {results}")

    threading.Thread(target=_worker, daemon=True).start()
    return jsonify({"accepted": True, "count": len(targets)}), 202

# Webhook de WhatsApp
@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        logging.info("✅ Webhook verificado")
        return challenge
    else:
        logging.warning("❌ Verificación fallida")
        return "Verificación fallida", 403

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True, silent=True)
    logging.info(f"📩 Mensaje recibido: {json.dumps(data)[:300]}")

    if data and "entry" in data:
        for entry in data["entry"]:
            for change in entry.get("changes", []):
                if change.get("field") == "messages":
                    value = change.get("value", {})
                    if "messages" in value:
                        for msg in value["messages"]:
                            # Procesar solo mensajes de texto
                            if msg.get("type") == "text":
                                phone = msg.get("from")
                                message = msg.get("text", {}).get("body", "").strip()
                                
                                # Evitar procesar duplicados
                                msg_id = msg.get("id")
                                if msg_id in PROCESSED_MESSAGE_IDS:
                                    continue
                                PROCESSED_MESSAGE_IDS[msg_id] = datetime.now()
                                
                                # Manejar el mensaje
                                handle_incoming_message(phone, message)
    
    return jsonify({"status": "ok"}), 200

# Limpiar mensajes procesados antiguos
def cleanup_processed_messages():
    now = datetime.now()
    expired = [msg_id for msg_id, timestamp in PROCESSED_MESSAGE_IDS.items() 
               if (now - timestamp).total_seconds() > MSG_TTL]
    for msg_id in expired:
        PROCESSED_MESSAGE_IDS.pop(msg_id, None)

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
