# app.py - Vicky SECOM (Configuración Real)
from flask import Flask, request, jsonify
import os
import requests
import logging
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)

# Configuración REAL con tus variables
META_TOKEN = os.getenv("META_TOKEN")
WABA_PHONE_ID = os.getenv("WABAPHONE_ID") or os.getenv("PHONE_NUMBER_ID")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN") or os.getenv("yicky-verify.2025")
ADVISOR_NUMBER = os.getenv("ADVISOR_NUMBER") or os.getenv("ADVISOR_WHATSAPP")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Google Sheets REAL
SHEETS_ID_LEADS = os.getenv("SHEETS_ID_LEADS") or os.getenv("GSHEET_PROSPECTS_ID") or os.getenv("ID_DE_SPREADSHEET_LEADS")
SHEETS_TITLE_LEADS = os.getenv("SHEETS_TITLE_LEADS") or os.getenv("GOOGLE_SHEET_NAME") or "Prospectos SECOM Auto"

# Configuración logging
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("vicky-secom")

# Estado de conversación
user_state = {}
user_data = {}

# ==========================
# FUNCIONES PRINCIPALES
# ==========================

def send_message(to, text):
    """Envía mensaje por WhatsApp"""
    if not META_TOKEN or not WABA_PHONE_ID:
        log.error("WhatsApp no configurado")
        return False
    
    url = f"https://graph.facebook.com/v17.0/{WABA_PHONE_ID}/messages"
    headers = {
        "Authorization": f"Bearer {META_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text}
    }
    
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        if response.status_code == 200:
            log.info(f"✅ Mensaje enviado a {to}")
            return True
        else:
            log.error(f"❌ Error enviando mensaje: {response.status_code}")
            return False
    except Exception as e:
        log.error(f"❌ Exception enviando mensaje: {str(e)}")
        return False

def handle_menu(phone, text):
    """Maneja el menú principal"""
    text_lower = text.lower().strip()
    
    # Detectar respuestas a mensajes promocionales PRIMERO
    if any(word in text_lower for word in ["sí", "si", "claro", "ok", "interesa", "cuéntame", "info"]):
        if any(term in text_lower for term in ["seguro", "auto", "coche", "carro"]):
            user_state[phone] = "auto_intro"
            send_message(phone, "🚗 *Perfecto! Seguro de Auto*\nPara cotizar, necesito:\n• INE (frente)\n• Tarjeta de circulación\n\n¿Cuándo vence tu seguro actual? (formato AAAA-MM-DD)")
            return True
    
    # Menú normal
    if text_lower in ["1", "imss", "ley 73", "préstamo imss"]:
        user_state[phone] = "imss_beneficios"
        send_message(phone, "🏥 *Préstamo IMSS Ley 73*\n¿Te interesa conocer requisitos? (sí/no)")
        
    elif text_lower in ["2", "auto", "seguro auto"]:
        user_state[phone] = "auto_intro"
        send_message(phone, "🚗 *Seguro de Auto*\nPara cotizar, necesito:\n• INE (frente)\n• Tarjeta de circulación\n\n¿Cuándo vence tu seguro actual?")
        
    elif text_lower in ["5", "empresarial", "crédito empresarial"]:
        user_state[phone] = "emp_confirma"
        send_message(phone, "🏢 *Crédito Empresarial*\n¿Eres empresario(a)? (sí/no)")
        
    elif text_lower in ["7", "contactar", "asesor", "christian"]:
        send_message(ADVISOR_NUMBER, f"🔔 Cliente solicita contacto: {phone}")
        send_message(phone, "✅ Listo. Christian te contactará pronto.")
        send_main_menu(phone)
        
    elif text_lower in ["menu", "menú", "hola", "inicio"]:
        send_main_menu(phone)
        
    else:
        return False
        
    return True

def send_main_menu(phone):
    """Envía el menú principal"""
    menu_text = """🟦 *Vicky Bot — Inbursa*

Elige una opción:
1) Préstamo IMSS (Ley 73)
2) Seguro de Auto (cotización) 
3) Seguros de Vida / Salud
4) Tarjeta médica VRIM
5) Crédito Empresarial
6) Financiamiento Práctico
7) Contactar con Christian

Escribe el número u opción (ej. 'imss', 'auto', 'empresarial')."""
    send_message(phone, menu_text)

# ==========================
# WEBHOOK ENDPOINTS
# ==========================

@app.route('/webhook', methods=['GET'])
def verify_webhook():
    challenge = request.args.get('hub.challenge')
    token = request.args.get('hub.verify_token')
    
    if token == VERIFY_TOKEN:
        log.info("✅ Webhook verificado")
        return challenge
    log.error("❌ Token de verificación incorrecto")
    return "Error", 403

@app.route('/webhook', methods=['POST'])
def handle_webhook():
    try:
        data = request.get_json()
        log.info("📥 Webhook recibido")
        
        # Buscar mensaje entrante
        entry = data.get('entry', [{}])[0]
        changes = entry.get('changes', [{}])[0]
        value = changes.get('value', {})
        messages = value.get('messages', [])
        
        if not messages:
            log.info("ℹ️ Status update, ignorando")
            return jsonify({"status": "ok"}), 200
            
        message = messages[0]
        phone = message.get('from')
        message_type = message.get('type')
        
        if message_type == 'text':
            text = message['text']['body']
            log.info(f"💬 Mensaje de {phone}: {text}")
            
            # Manejar según estado actual
            current_state = user_state.get(phone, "")
            
            if not current_state:
                # Sin estado - manejar como menú o respuesta promocional
                if not handle_menu(phone, text):
                    send_message(phone, "No entendí. Escribe *menú* para ver opciones.")
            else:
                # Ya está en un flujo - manejar según estado
                if current_state == "imss_beneficios":
                    if text.lower() in ["sí", "si"]:
                        user_state[phone] = "imss_pension"
                        send_message(phone, "¿Cuál es tu pensión mensual? (ej. $8,500)")
                    else:
                        send_message(phone, "Sin problema. Escribe *menú* para otras opciones.")
                        user_state[phone] = ""
                
                elif current_state == "auto_intro":
                    send_message(phone, "✅ Perfecto. Para proceder con la cotización, por favor envía:\n• INE por enfrente\n• Tarjeta de circulación\n\nO escribe *menú* para volver.")
                    user_state[phone] = "auto_documentos"
                
                elif current_state == "emp_confirma":
                    if text.lower() in ["sí", "si"]:
                        user_state[phone] = "emp_giro"
                        send_message(phone, "¿A qué se dedica tu empresa?")
                    else:
                        send_message(phone, "Entendido. Escribe *menú* para otras opciones.")
                        user_state[phone] = ""
        
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
        
        log.info(f"✅ Envío masivo completado: {success_count}/{len(items)}")
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
# INICIALIZACIÓN
# ==========================

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    log.info(f"🚀 Iniciando Vicky SECOM en puerto {port}")
    app.run(host='0.0.0.0', port=port, debug=False)

