# app.py - Vicky SECOM (Completo y Estable)
from flask import Flask, request, jsonify
import os
import requests
import logging
import re
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)

# ==========================
# CONFIGURACIÓN
# ==========================
META_TOKEN = os.getenv("META_TOKEN")
WABA_PHONE_ID = os.getenv("WABAPHONE_ID") or os.getenv("PHONE_NUMBER_ID")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN") or os.getenv("yicky-verify.2025")
ADVISOR_NUMBER = os.getenv("ADVISOR_NUMBER")

# Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s [%(name)s] %(message)s')
log = logging.getLogger("vicky-secom")

# Estados
user_state = {}
user_data = {}

# ==========================
# FUNCIONES CORE
# ==========================
def send_message(to, text):
    """Envía mensaje por WhatsApp"""
    if not META_TOKEN or not WABA_PHONE_ID:
        log.error("❌ WhatsApp no configurado")
        return False
    
    url = f"https://graph.facebook.com/v17.0/{WABA_PHONE_ID}/messages"
    headers = {"Authorization": f"Bearer {META_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}
    
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        if response.status_code == 200:
            log.info(f"✅ Mensaje enviado a {to}")
            return True
        else:
            log.error(f"❌ Error {response.status_code} enviando a {to}")
            return False
    except Exception as e:
        log.error(f"❌ Exception enviando mensaje: {str(e)}")
        return False

def interpret_response(text):
    """Interpreta respuestas sí/no"""
    text_lower = text.lower().strip()
    positive = ["sí", "si", "claro", "ok", "de acuerdo", "afirmativo", "correcto", "interesa"]
    negative = ["no", "nel", "nop", "negativo", "no quiero", "no gracias", "no interesa"]
    
    if any(p in text_lower for p in positive):
        return "positive"
    if any(n in text_lower for n in negative):
        return "negative"
    return "neutral"

def extract_number(text):
    """Extrae números de texto"""
    if not text:
        return None
    clean = text.replace(",", "").replace("$", "")
    m = re.search(r"(\d{1,12}(\.\d+)?)", clean)
    try:
        return float(m.group(1)) if m else None
    except:
        return None

def notify_advisor(message):
    """Notifica al asesor"""
    if ADVISOR_NUMBER:
        send_message(ADVISOR_NUMBER, f"🔔 {message}")

# ==========================
# FLUJOS DE CONVERSACIÓN
# ==========================
def send_main_menu(phone):
    menu = """🟦 *Vicky Bot — Inbursa*

Elige una opción:
1) Préstamo IMSS (Ley 73)
2) Seguro de Auto (cotización)
3) Seguros de Vida / Salud
4) Tarjeta médica VRIM
5) Crédito Empresarial
6) Financiamiento Práctico
7) Contactar con Christian

Escribe el número u opción (ej. 'imss', 'auto', 'empresarial')."""
    send_message(phone, menu)

def start_imss(phone):
    user_state[phone] = "imss_beneficios"
    send_message(phone, "🏥 *Préstamo IMSS Ley 73*\nBeneficios: trámite rápido, sin aval, pagos fijos. ¿Te interesa conocer requisitos? (sí/no)")

def handle_imss_response(phone, text):
    state = user_state.get(phone)
    data = user_data.get(phone, {})
    
    if state == "imss_beneficios":
        if interpret_response(text) == "positive":
            user_state[phone] = "imss_pension"
            send_message(phone, "¿Cuál es tu *pensión mensual* aproximada? (ej. $8,500)")
        else:
            send_message(phone, "Sin problema. Escribe *menú* para otras opciones.")
            user_state[phone] = ""
    
    elif state == "imss_pension":
        pension = extract_number(text)
        if pension:
            data["imss_pension"] = pension
            user_data[phone] = data
            user_state[phone] = "imss_monto"
            send_message(phone, "¿Qué *monto* te gustaría solicitar? (mínimo $40,000)")
        else:
            send_message(phone, "No entendí el monto. Ejemplo: 8500")
    
    elif state == "imss_monto":
        monto = extract_number(text)
        if monto and monto >= 40000:
            data["imss_monto"] = monto
            user_data[phone] = data
            user_state[phone] = "imss_nombre"
            send_message(phone, "¿Cuál es tu *nombre completo*?")
        else:
            send_message(phone, "Monto mínimo es $40,000. Escribe un monto válido.")

def start_auto(phone):
    user_state[phone] = "auto_intro"
    send_message(phone, "🚗 *Seguro de Auto*\nPara cotizar necesito:\n• INE (frente)\n• Tarjeta de circulación\n\n¿Tu seguro actual cuándo vence? (AAAA-MM-DD)")

def handle_auto_response(phone, text):
    state = user_state.get(phone)
    
    if state == "auto_intro":
        # Si responde con fecha de vencimiento
        if re.match(r"\d{4}-\d{2}-\d{2}", text):
            send_message(phone, "✅ Fecha registrada. Por favor envía:\n• INE por enfrente\n• Tarjeta de circulación")
            user_state[phone] = "auto_documentos"
        else:
            send_message(phone, "Para cotizar, necesito los documentos o la fecha de vencimiento (AAAA-MM-DD)")

def start_empresarial(phone):
    user_state[phone] = "emp_confirma"
    send_message(phone, "🏢 *Crédito Empresarial*\n¿Eres empresario(a) o representas una empresa? (sí/no)")

def handle_empresarial_response(phone, text):
    state = user_state.get(phone)
    data = user_data.get(phone, {})
    
    if state == "emp_confirma":
        if interpret_response(text) == "positive":
            user_state[phone] = "emp_giro"
            send_message(phone, "¿A qué *se dedica* tu empresa?")
        else:
            send_message(phone, "Entendido. Escribe *menú* para otras opciones.")
            user_state[phone] = ""
    
    elif state == "emp_giro":
        data["emp_giro"] = text
        user_data[phone] = data
        user_state[phone] = "emp_monto"
        send_message(phone, "¿Qué *monto* deseas? (mínimo $100,000)")

# ==========================
# MANEJO DE MENSAJES
# ==========================
def handle_message(phone, text):
    text_lower = text.lower().strip()
    
    # Detectar respuestas a mensajes promocionales
    if user_state.get(phone) is None and interpret_response(text) == "positive":
        if any(term in text_lower for term in ["seguro", "auto", "coche", "carro"]):
            start_auto(phone)
            return
    
    # Comandos del menú principal
    if text_lower in ["1", "imss", "ley 73", "préstamo imss"]:
        start_imss(phone)
    elif text_lower in ["2", "auto", "seguro auto"]:
        start_auto(phone)
    elif text_lower in ["5", "empresarial", "crédito empresarial"]:
        start_empresarial(phone)
    elif text_lower in ["3", "vida", "salud", "seguro vida"]:
        send_message(phone, "🧬 *Seguros Vida/Salud* - Notificando al asesor...")
        notify_advisor(f"Vida/Salud - Cliente {phone} solicita info")
        send_main_menu(phone)
    elif text_lower in ["4", "vrim", "tarjeta médica"]:
        send_message(phone, "🩺 *VRIM* - Notificando al asesor...")
        notify_advisor(f"VRIM - Cliente {phone} solicita info")
        send_main_menu(phone)
    elif text_lower in ["7", "contactar", "asesor", "christian"]:
        send_message(phone, "✅ Christian te contactará pronto.")
        notify_advisor(f"Contacto directo - Cliente {phone} solicita hablar")
        send_main_menu(phone)
    elif text_lower in ["menu", "menú", "hola"]:
        send_main_menu(phone)
        user_state[phone] = ""
    else:
        # Manejar según estado actual
        state = user_state.get(phone)
        if state and state.startswith("imss"):
            handle_imss_response(phone, text)
        elif state and state.startswith("auto"):
            handle_auto_response(phone, text)
        elif state and state.startswith("emp"):
            handle_empresarial_response(phone, text)
        else:
            send_message(phone, "No entendí. Escribe *menú* para ver opciones.")

# ==========================
# ENDPOINTS WEBHOOK
# ==========================
@app.route('/webhook', methods=['GET'])
def verify_webhook():
    token = request.args.get('hub.verify_token')
    if token == VERIFY_TOKEN:
        log.info("✅ Webhook verificado")
        return request.args.get('hub.challenge')
    log.error("❌ Token de verificación incorrecto")
    return "Error", 403

@app.route('/webhook', methods=['POST'])
def handle_webhook():
    try:
        data = request.get_json()
        log.info("📥 Webhook recibido")
        
        entry = data.get('entry', [{}])[0]
        changes = entry.get('changes', [{}])[0]
        value = changes.get('value', {})
        messages = value.get('messages', [])
        
        if not messages:
            return jsonify({"status": "ok"}), 200
            
        message = messages[0]
        phone = message.get('from')
        
        if message.get('type') == 'text':
            text = message['text']['body']
            log.info(f"💬 {phone}: {text}")
            handle_message(phone, text)
        
        return jsonify({"status": "processed"}), 200
        
    except Exception as e:
        log.error(f"❌ Error en webhook: {str(e)}")
        return jsonify({"status": "error"}), 500

# ==========================
# ENDPOINT ENVÍOS MASIVOS
# ==========================
@app.route('/ext/send-promo', methods=['POST'])
def send_promo():
    try:
        data = request.get_json()
        items = data.get('items', [])
        
        log.info(f"📨 Envío masivo: {len(items)} mensajes")
        
        success_count = 0
        for item in items:
            to = item.get('to', '').strip()
            text = item.get('text', '').strip()
            
            if to and text:
                if send_message(to, text):
                    success_count += 1
        
        response = {
            "success": True,
            "sent": success_count,
            "total": len(items),
            "timestamp": datetime.now().isoformat()
        }
        
        log.info(f"✅ Envío masivo: {success_count}/{len(items)} enviados")
        return jsonify(response), 200
        
    except Exception as e:
        log.error(f"❌ Error en envío masivo: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route('/health', methods=['GET'])
def health():
    return jsonify({
        "status": "ok", 
        "service": "Vicky SECOM",
        "timestamp": datetime.now().isoformat()
    })

# ==========================
# INICIO
# ==========================
if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    log.info(f"🚀 Iniciando Vicky SECOM en puerto {port}")
    app.run(host='0.0.0.0', port=port, debug=False)
