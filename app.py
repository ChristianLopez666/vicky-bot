# ===============================================================
# VICKY CAMPAÑAS EN REDES – APP PRINCIPAL
# Integración completa con Meta Cloud API (WhatsApp Business)
# Flujo activo: Préstamos IMSS Ley 73
# Autor: Christian López | GPT-5
# ===============================================================

import os
import json
import logging
import requests
import re
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from datetime import datetime

# ---------------------------------------------------------------
# Cargar variables de entorno
# ---------------------------------------------------------------
load_dotenv()

META_TOKEN = os.getenv("META_TOKEN")
WABA_PHONE_ID = os.getenv("WABA_PHONE_ID")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
ADVISOR_NUMBER = os.getenv("ADVISOR_NUMBER", "5216682478005")

# ---------------------------------------------------------------
# Configuración de logging
# ---------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

# ---------------------------------------------------------------
# Inicialización de Flask
# ---------------------------------------------------------------
app = Flask(__name__)

# Diccionarios temporales para gestionar el estado de cada usuario
user_state = {}
user_data = {}

# ---------------------------------------------------------------
# Función: enviar mensaje por WhatsApp (Meta Cloud API)
# ---------------------------------------------------------------
def send_message(to, text):
    """Envía mensajes de texto al usuario vía Meta Cloud API."""
    try:
        url = f"https://graph.facebook.com/v20.0/{WABA_PHONE_ID}/messages"
        headers = {
            "Authorization": f"Bearer {META_TOKEN}",
            "Content-Type": "application/json"
        }
        payload = {
            "messaging_product": "whatsapp",
            "to": str(to),
            "type": "text",
            "text": {"body": text}
        }
        response = requests.post(url, headers=headers, json=payload)
        if response.status_code not in (200, 201):
            logging.warning(f"⚠️ Error al enviar mensaje: {response.text}")
        else:
            logging.info(f"📩 Mensaje enviado correctamente a {to}")
    except Exception as e:
        logging.exception(f"❌ Error en send_message: {e}")

# ---------------------------------------------------------------
# Función auxiliar: extraer número de texto
# ---------------------------------------------------------------
def extract_number(text):
    """Extrae el primer número encontrado dentro del texto."""
    if not text:
        return None
    clean = text.replace(',', '').replace('$', '')
    match = re.search(r'(\d{1,9})(?:\.\d+)?\b', clean)
    if match:
        try:
            if ':' in text:
                return None
            return float(match.group(1))
        except ValueError:
            return None
    return None

# ---------------------------------------------------------------
# Función: interpretar respuestas sí/no
# ---------------------------------------------------------------
def interpret_response(text):
    """Interpreta respuestas afirmativas/negativas."""
    text_lower = (text or '').lower()
    positive_keywords = ['sí', 'si', 'sip', 'claro', 'por supuesto', 'ok', 'vale', 'afirmativo', 'acepto', 'yes']
    negative_keywords = ['no', 'nop', 'negativo', 'para nada', 'no acepto', 'not']
    if any(k in text_lower for k in positive_keywords):
        return 'positive'
    if any(k in text_lower for k in negative_keywords):
        return 'negative'
    return 'neutral'

# ---------------------------------------------------------------
# Función: detectar agradecimientos
# ---------------------------------------------------------------
def is_thankyou_message(text):
    """Detecta mensajes de agradecimiento."""
    text_lower = text.lower().strip()
    thankyou_keywords = [
        'gracias', 'grac', 'gracia', 'thank', 'thanks', 'agradecido', 
        'agradecida', 'agradecimiento', 'te lo agradezco', 'mil gracias'
    ]
    return any(keyword in text_lower for keyword in thankyou_keywords)

# ---------------------------------------------------------------
# Función: validar nombre
# ---------------------------------------------------------------
def is_valid_name(text):
    """Valida que el texto sea un nombre válido."""
    if not text or len(text.strip()) < 2:
        return False
    # Verificar que contenga solo letras, espacios y algunos caracteres especiales comunes en nombres
    if re.match(r'^[a-zA-ZáéíóúÁÉÍÓÚñÑüÜ\s\.\-]+$', text.strip()):
        return True
    return False

# ---------------------------------------------------------------
# Función: validar teléfono
# ---------------------------------------------------------------
def is_valid_phone(text):
    """Valida que el texto sea un teléfono válido."""
    if not text:
        return False
    # Limpiar y verificar formato de teléfono
    clean_phone = re.sub(r'[\s\-\(\)\+]', '', text)
    return re.match(r'^\d{10,15}$', clean_phone) is not None

# ---------------------------------------------------------------
# MENÚ PRINCIPAL MEJORADO
# ---------------------------------------------------------------
def send_main_menu(phone):
    menu = (
        "🏦 *INBURSA - SERVICIOS DISPONIBLES*\n\n"
        "1️⃣ Préstamos IMSS Ley 73\n"
        "2️⃣ Seguros de Auto\n"
        "3️⃣ Seguros de Vida y Salud\n"
        "4️⃣ Tarjetas Médicas VRIM\n"
        "5️⃣ Financiamiento Empresarial\n\n"
        "Escribe el *número* o el *nombre* del servicio que te interesa:"
    )
    send_message(phone, menu)

# ---------------------------------------------------------------
# Función: manejar comando menu
# ---------------------------------------------------------------
def handle_menu_command(phone_number):
    """Maneja el comando menu para reiniciar la conversación"""
    user_state.pop(phone_number, None)
    user_data.pop(phone_number, None)
    
    menu_text = (
        "🔄 *Conversación reiniciada*\n\n"
        "🏦 *INBURSA - SERVICIOS DISPONIBLES*\n\n"
        "1️⃣ Préstamos IMSS Ley 73\n"
        "2️⃣ Seguros de Auto\n"
        "3️⃣ Seguros de Vida y Salud\n"
        "4️⃣ Tarjetas Médicas VRIM\n"
        "5️⃣ Financiamiento Empresarial\n\n"
        "Escribe el *número* o el *nombre* del servicio que te interesa:"
    )
    send_message(phone_number, menu_text)

# ---------------------------------------------------------------
# BLOQUE PRINCIPAL: FLUJO PRÉSTAMO IMSS LEY 73
# ---------------------------------------------------------------
def handle_imss_flow(phone_number, user_message):
    """Gestiona el flujo completo del préstamo IMSS Ley 73."""
    msg = user_message.lower()

    # Detección mejorada de palabras clave IMSS - INCLUYE NÚMERO 1
    imss_keywords = ["préstamo", "prestamo", "imss", "pensión", "pension", "ley 73", "1"]
    
    # Paso 1: activación inicial por palabras clave
    if any(keyword in msg for keyword in imss_keywords):
        current_state = user_state.get(phone_number)
        if current_state not in ["esperando_respuesta_imss", "esperando_monto_solicitado", "esperando_respuesta_nomina"]:
            send_message(phone_number,
                "👋 ¡Hola! Antes de continuar, necesito confirmar algo importante.\n\n"
                "¿Eres pensionado o jubilado del IMSS bajo la Ley 73? (Responde *sí* o *no*)"
            )
            user_state[phone_number] = "esperando_respuesta_imss"
        return True

    # Paso 2: validación de respuesta IMSS
    if user_state.get(phone_number) == "esperando_respuesta_imss":
        intent = interpret_response(msg)
        if intent == 'negative':
            send_message(phone_number,
                "Entiendo. Para el préstamo IMSS Ley 73 es necesario ser pensionado del IMSS. 😔\n\n"
                "Pero tengo otros servicios que pueden interesarte:"
            )
            send_main_menu(phone_number)
            user_state.pop(phone_number, None)
        elif intent == 'positive':
            send_message(phone_number,
                "Excelente 👏\n\n¿Qué monto de préstamo deseas solicitar? (desde $40,000 hasta $650,000)"
            )
            user_state[phone_number] = "esperando_monto_solicitado"
        else:
            send_message(phone_number, "Por favor responde *sí* o *no* para continuar.")
        return True

    # Paso 3: monto solicitado - VALIDACIÓN MÍNIMO $40,000
    if user_state.get(phone_number) == "esperando_monto_solicitado":
        if is_thankyou_message(msg):
            send_message(phone_number,
                "¡Por nada! 😊\n\n"
                "Sigamos con tu solicitud...\n\n"
                "¿Qué monto deseas solicitar? (desde $40,000 hasta $650,000)"
            )
            return True
            
        monto = extract_number(msg)
        if monto is not None:
            if monto < 40000:
                send_message(phone_number,
                    "Por el momento el monto mínimo para aplicar al préstamo es de $40,000 MXN. 💵\n\n"
                    "Si deseas solicitar una cantidad mayor, puedo continuar con tu registro ✅\n"
                    "O si prefieres, puedo mostrarte otras opciones que podrían interesarte:"
                )
                send_main_menu(phone_number)
                user_state.pop(phone_number, None)
            elif monto > 650000:
                send_message(phone_number,
                    "El monto máximo para préstamos IMSS Ley 73 es de $650,000 MXN. 💵\n\n"
                    "Por favor ingresa un monto dentro del rango permitido:"
                )
            else:
                user_data[phone_number] = {"monto_solicitado": monto}
                
                send_message(phone_number,
                    "🎉 *¡FELICIDADES!* Cumples con los requisitos para el préstamo IMSS Ley 73\n\n"
                    f"✅ Monto solicitado: ${monto:,.0f}\n\n"
                    "🌟 *BENEFICIOS DE TU PRÉSTAMO:*\n"
                    "• Monto desde $40,000 hasta $650,000\n"
                    "• Sin aval\n• Sin revisión en Buró\n"
                    "• Descuento directo de tu pensión\n"
                    "• Tasa preferencial"
                )
                
                send_message(phone_number,
                    "💳 *PARA ACCEDER A BENEFICIOS ADICIONALES EXCLUSIVOS*:\n\n"
                    "¿Tienes tu pensión depositada en Inbursa o estarías dispuesto a cambiarla?\n\n"
                    "🌟 *BENEFICIOS ADICIONALES CON NÓMINA INBURSA:*\n"
                    "• Rendimientos del 80% de Cetes\n"
                    "• Devolución del 20% de intereses por pago puntual\n"
                    "• Anticipo de nómina hasta el 50%\n"
                    "• Seguro de vida y Medicall Home (telemedicina 24/7)\n"
                    "• Descuentos en Sanborns y 6,000 comercios\n"
                    "• Retiros sin comisión en +28,000 puntos\n\n"
                    "💡 *No necesitas cancelar tu cuenta actual*\n"
                    "👉 ¿Aceptas cambiar tu nómina a Inbursa? (sí/no)"
                )
                user_state[phone_number] = "esperando_respuesta_nomina"
        else:
            send_message(phone_number, "Por favor indica el monto deseado, ejemplo: 65000")
        return True

    # Paso 4: validación nómina - NO DETENER PROCESO SI RESPONDE NO
    if user_state.get(phone_number) == "esperando_respuesta_nomina":
        if is_thankyou_message(msg):
            send_message(phone_number,
                "¡De nada! 😊\n\n"
                "Para continuar, por favor responde *sí* o *no*:\n\n"
                "¿Aceptas cambiar tu nómina a Inbursa para acceder a beneficios adicionales?"
            )
            return True
            
        intent = interpret_response(msg)
        
        data = user_data.get(phone_number, {})
        monto_solicitado = data.get('monto_solicitado', 'N/D')
        
        if intent == 'positive':
            send_message(phone_number,
                "✅ *¡Excelente decisión!* Al cambiar tu nómina a Inbursa accederás a todos los beneficios adicionales.\n\n"
                "📞 *Christian te contactará en breve* para:\n"
                "• Confirmar los detalles de tu préstamo\n"
                "• Explicarte todos los beneficios de nómina Inbursa\n"
                "• Agendar el cambio de nómina si así lo decides\n\n"
                "¡Gracias por confiar en Inbursa! 🏦"
            )

            mensaje_asesor = (
                f"🔥 *NUEVO PROSPECTO IMSS LEY 73 - NÓMINA ACEPTADA*\n\n"
                f"📞 Número: {phone_number}\n"
                f"💵 Monto solicitado: ${monto_solicitado:,.0f}\n"
                f"🏦 Nómina Inbursa: ✅ *ACEPTADA*\n"
                f"🎯 *Cliente interesado en beneficios adicionales*"
            )
            send_message(ADVISOR_NUMBER, mensaje_asesor)
            
        elif intent == 'negative':
            send_message(phone_number,
                "✅ *¡Perfecto!* Entiendo que por el momento prefieres mantener tu nómina actual.\n\n"
                "📞 *Christian te contactará en breve* para:\n"
                "• Confirmar los detalles de tu préstamo\n"
                "• Explicarte el proceso de desembolso\n\n"
                "💡 *Recuerda que en cualquier momento puedes cambiar tu nómina a Inbursa* "
                "para acceder a los beneficios adicionales cuando lo desees.\n\n"
                "¡Gracias por confiar en Inbursa! 🏦"
            )

            mensaje_asesor = (
                f"📋 *NUEVO PROSPECTO IMSS LEY 73*\n\n"
                f"📞 Número: {phone_number}\n"
                f"💵 Monto solicitado: ${monto_solicitado:,.0f}\n"
                f"🏦 Nómina Inbursa: ❌ *No por ahora*\n"
                f"💡 *Cliente cumple requisitos - Contactar para préstamo básico*"
            )
            send_message(ADVISOR_NUMBER, mensaje_asesor)
        else:
            send_message(phone_number, 
                "Por favor responde *sí* o *no*:\n\n"
                "• *SÍ* - Para acceder a todos los beneficios adicionales con nómina Inbursa\n"
                "• *NO* - Para continuar con tu préstamo manteniendo tu nómina actual"
            )
            return True

        user_state.pop(phone_number, None)
        user_data.pop(phone_number, None)
        return True

    return False

# ---------------------------------------------------------------
# BLOQUE: FLUJO CRÉDITO EMPRESARIAL - MEJORADO CON DATOS DE CONTACTO
# ---------------------------------------------------------------
def handle_business_flow(phone_number, user_message):
    """Gestiona el flujo completo de crédito empresarial."""
    msg = user_message.lower()

    # Paso 1: Inicio del flujo empresarial
    if user_state.get(phone_number) == "inicio_empresarial":
        send_message(phone_number,
            "🏢 *Financiamiento Empresarial Inbursa*\n\n"
            "Impulsa el crecimiento de tu negocio con:\n\n"
            "✅ Créditos desde $100,000 hasta $100,000,000\n"
            "✅ Tasas preferenciales\n"
            "✅ Plazos flexibles\n"
            "✅ Asesoría especializada\n\n"
            "Para comenzar, ¿qué tipo de crédito necesitas?\n\n"
            "• Capital de trabajo\n"
            "• Maquinaria y equipo\n" 
            "• Remodelación de local\n"
            "• Expansión de negocio\n"
            "• Otro (especifica)"
        )
        user_state[phone_number] = "esperando_tipo_credito"
        return True

    # Paso 2: Capturar tipo de crédito
    if user_state.get(phone_number) == "esperando_tipo_credito":
        user_data[phone_number] = {"tipo_credito": user_message}
        send_message(phone_number,
            "📊 Perfecto. ¿A qué se dedica tu empresa? (giro o actividad principal)"
        )
        user_state[phone_number] = "esperando_giro_empresa"
        return True

    # Paso 3: Capturar giro de la empresa
    if user_state.get(phone_number) == "esperando_giro_empresa":
        user_data[phone_number]["giro_empresa"] = user_message
        send_message(phone_number,
            "💼 ¿Qué monto de crédito necesitas?\n\n"
            "Monto mínimo: $100,000 MXN\n"
            "Monto máximo: $100,000,000 MXN\n"
            "Ejemplo: 250000"
        )
        user_state[phone_number] = "esperando_monto_empresarial"
        return True

    # Paso 4: Capturar monto solicitado
    if user_state.get(phone_number) == "esperando_monto_empresarial":
        monto = extract_number(msg)
        if monto is not None:
            if monto < 100000:
                send_message(phone_number,
                    "El monto mínimo para crédito empresarial es de $100,000 MXN. 💰\n\n"
                    "Si deseas solicitar un monto mayor, por favor ingrésalo:"
                )
                return True
            elif monto > 100000000:
                send_message(phone_number,
                    "El monto máximo para crédito empresarial es de $100,000,000 MXN. 💰\n\n"
                    "Por favor ingresa un monto dentro del rango permitido:"
                )
                return True
            else:
                user_data[phone_number]["monto_solicitado"] = monto
                send_message(phone_number,
                    f"✅ Monto registrado: ${monto:,.0f}\n\n"
                    "👤 *Datos de contacto*\n\n"
                    "¿Cuál es tu nombre completo?"
                )
                user_state[phone_number] = "esperando_nombre_empresarial"
        else:
            send_message(phone_number, "Por favor ingresa un monto válido, ejemplo: 250000")
        return True

    # ✅ NUEVO PASO 5: Capturar nombre completo
    if user_state.get(phone_number) == "esperando_nombre_empresarial":
        if is_valid_name(user_message):
            user_data[phone_number]["nombre_contacto"] = user_message.title()
            send_message(phone_number,
                f"✅ Nombre registrado: {user_message.title()}\n\n"
                "🏙️ ¿En qué ciudad se encuentra tu empresa?"
            )
            user_state[phone_number] = "esperando_ciudad_empresarial"
        else:
            send_message(phone_number,
                "Por favor ingresa un nombre válido (solo letras y espacios):\n\n"
                "Ejemplo: Juan Pérez García"
            )
        return True

    # ✅ NUEVO PASO 6: Capturar ciudad
    if user_state.get(phone_number) == "esperando_ciudad_empresarial":
        user_data[phone_number]["ciudad_empresa"] = user_message.title()
        send_message(phone_number,
            f"✅ Ciudad registrada: {user_message.title()}\n\n"
            "📅 ¿Qué día y horario prefieres para que te contacte un especialista?\n\n"
            "Ejemplo: Lunes a viernes de 9am a 2pm"
        )
        user_state[phone_number] = "esperando_contacto_empresarial"
        return True

    # Paso 7: Capturar horario de contacto y finalizar
    if user_state.get(phone_number) == "esperando_contacto_empresarial":
        user_data[phone_number]["horario_contacto"] = user_message
        
        data = user_data.get(phone_number, {})
        
        send_message(phone_number,
            "🎉 *¡Excelente!* Hemos registrado tu solicitud de financiamiento empresarial.\n\n"
            "📞 *Un especialista en negocios te contactará* en el horario indicado para:\n\n"
            "• Analizar tu proyecto a detalle\n"
            "• Explicarte las mejores opciones de crédito\n"
            "• Orientarte sobre los requisitos y documentación\n\n"
            "¡Gracias por considerar a Inbursa para impulsar tu empresa! 🏢"
        )

        # Notificar al asesor con información completa
        mensaje_asesor = (
            f"🏢 *NUEVO PROSPECTO EMPRESARIAL - INFORMACIÓN COMPLETA*\n\n"
            f"👤 Nombre: {data.get('nombre_contacto', 'N/D')}\n"
            f"📞 Teléfono: {phone_number}\n"
            f"🏙️ Ciudad: {data.get('ciudad_empresa', 'N/D')}\n"
            f"📊 Tipo de crédito: {data.get('tipo_credito', 'N/D')}\n"
            f"🏭 Giro empresa: {data.get('giro_empresa', 'N/D')}\n"
            f"💵 Monto solicitado: ${data.get('monto_solicitado', 'N/D'):,.0f}\n"
            f"📅 Horario contacto: {data.get('horario_contacto', 'N/D')}\n\n"
            f"🎯 *Cliente potencial para crédito empresarial*"
        )
        send_message(ADVISOR_NUMBER, mensaje_asesor)
        
        user_state.pop(phone_number, None)
        user_data.pop(phone_number, None)
        return True

    return False

# ---------------------------------------------------------------
# FLUJO PARA OPCIONES DEL MENÚ
# ---------------------------------------------------------------
def handle_menu_options(phone_number, user_message):
    """Maneja las opciones del menú principal."""
    msg = user_message.lower().strip()
    
    menu_options = {
        '1': 'imss',
        'préstamo': 'imss',
        'prestamo': 'imss',
        'imss': 'imss',
        'ley 73': 'imss',
        '2': 'seguro_auto',
        'seguro auto': 'seguro_auto',
        'seguros de auto': 'seguro_auto',
        'auto': 'seguro_auto',
        '3': 'seguro_vida',
        'seguro vida': 'seguro_vida',
        'seguros de vida': 'seguro_vida',
        'seguro salud': 'seguro_vida',
        'vida': 'seguro_vida',
        '4': 'vrim',
        'tarjetas médicas': 'vrim',
        'tarjetas medicas': 'vrim',
        'vrim': 'vrim',
        '5': 'empresarial',
        'financiamiento empresarial': 'empresarial',
        'empresa': 'empresarial',
        'negocio': 'empresarial',
        'pyme': 'empresarial',
        'crédito empresarial': 'empresarial',
        'credito empresarial': 'empresarial'
    }
    
    option = menu_options.get(msg)
    
    if option == 'imss':
        return handle_imss_flow(phone_number, "préstamo")
    elif option == 'seguro_auto':
        send_message(phone_number,
            "🚗 *Seguros de Auto Inbursa*\n\n"
            "Protege tu auto con las mejores coberturas:\n\n"
            "✅ Cobertura amplia contra todo riesgo\n"
            "✅ Asistencia vial las 24 horas\n"
            "✅ Responsabilidad civil\n"
            "✅ Robo total y parcial\n\n"
            "📞 Un asesor se comunicará contigo para cotizar tu seguro."
        )
        send_message(ADVISOR_NUMBER, f"🚗 NUEVO INTERESADO EN SEGURO DE AUTO\n📞 {phone_number}")
        return True
    elif option == 'seguro_vida':
        send_message(phone_number,
            "🏥 *Seguros de Vida y Salud Inbursa*\n\n"
            "Protege a tu familia y tu salud:\n\n"
            "✅ Seguro de vida\n"
            "✅ Gastos médicos mayores\n"
            "✅ Hospitalización\n"
            "✅ Atención médica las 24 horas\n\n"
            "📞 Un asesor se comunicará contigo para explicarte las coberturas."
        )
        send_message(ADVISOR_NUMBER, f"🏥 NUEVO INTERESADO EN SEGURO VIDA/SALUD\n📞 {phone_number}")
        return True
    elif option == 'vrim':
        send_message(phone_number,
            "💳 *Tarjetas Médicas VRIM*\n\n"
            "Accede a la mejor atención médica:\n\n"
            "✅ Consultas médicas ilimitadas\n"
            "✅ Especialistas y estudios de laboratorio\n"
            "✅ Medicamentos con descuento\n"
            "✅ Atención dental y oftalmológica\n\n"
            "📞 Un asesor se comunicará contigo para explicarte los beneficios."
        )
        send_message(ADVISOR_NUMBER, f"💳 NUEVO INTERESADO EN TARJETAS VRIM\n📞 {phone_number}")
        return True
    elif option == 'empresarial':
        user_state[phone_number] = "inicio_empresarial"
        return handle_business_flow(phone_number, "inicio")
    
    return False

# ---------------------------------------------------------------
# Endpoint de verificación de Meta Webhook
# ---------------------------------------------------------------
@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        logging.info("✅ Webhook verificado correctamente.")
        return challenge, 200
    logging.warning("❌ Verificación de webhook fallida.")
    return "Forbidden", 403

# ---------------------------------------------------------------
# Endpoint principal para recepción de mensajes
# ---------------------------------------------------------------
@app.route("/webhook", methods=["POST"])
def receive_message():
    try:
        data = request.get_json()
        logging.info(f"📩 Datos recibidos: {json.dumps(data, ensure_ascii=False)}")

        entry = data.get("entry", [])[0]
        change = entry.get("changes", [])[0]
        value = change.get("value", {})
        messages = value.get("messages", [])

        if not messages:
            return jsonify({"status": "ignored"}), 200

        message = messages[0]
        phone_number = message.get("from")
        message_type = message.get("type")

        if message_type == "text":
            user_message = message["text"]["body"].strip()
            
            logging.info(f"📱 Mensaje de {phone_number}: '{user_message}'")

            if user_message.lower() in ["menu", "menú", "men", "opciones", "servicios"]:
                handle_menu_command(phone_number)
                return jsonify({"status": "ok"}), 200

            if is_thankyou_message(user_message):
                send_message(phone_number,
                    "¡De nada! 😊\n\n"
                    "Quedo a tus órdenes para cualquier otra cosa.\n\n"
                    "¿Hay algo más en lo que pueda ayudarte?"
                )
                return jsonify({"status": "ok"}), 200

            if user_state.get(phone_number) in ["esperando_respuesta_imss", "esperando_monto_solicitado", "esperando_respuesta_nomina"]:
                if handle_imss_flow(phone_number, user_message):
                    return jsonify({"status": "ok"}), 200

            if user_state.get(phone_number) in ["inicio_empresarial", "esperando_tipo_credito", 
                                              "esperando_giro_empresa", "esperando_monto_empresarial",
                                              "esperando_nombre_empresarial", "esperando_ciudad_empresarial",
                                              "esperando_contacto_empresarial"]:
                if handle_business_flow(phone_number, user_message):
                    return jsonify({"status": "ok"}), 200

            if handle_menu_options(phone_number, user_message):
                return jsonify({"status": "ok"}), 200

            if user_message.lower() in ["hola", "hi", "hello", "buenas", "buenos días", "buenas tardes"]:
                send_message(phone_number,
                    "👋 ¡Hola! Soy *Vicky*, tu asistente virtual de Inbursa.\n\n"
                    "🏦 *SERVICIOS DISPONIBLES:*\n"
                    "1️⃣ Préstamos IMSS Ley 73\n"
                    "2️⃣ Seguros de Auto\n"
                    "3️⃣ Seguros de Vida y Salud\n"
                    "4️⃣ Tarjetas Médicas VRIM\n"
                    "5️⃣ Financiamiento Empresarial\n\n"
                    "Escribe el *número* o el *nombre* del servicio que te interesa.\n\n"
                    "También puedes escribir *menú* en cualquier momento."
                )
            else:
                send_message(phone_number,
                    "👋 Hola, soy *Vicky*, tu asistente de Inbursa.\n\n"
                    "No entendí tu mensaje. Te puedo ayudar con:\n\n"
                    "🏦 *SERVICIOS DISPONIBLES:*\n"
                    "• Préstamos IMSS (escribe '1' o 'préstamo')\n"  
                    "• Seguros de Auto ('2' o 'seguro auto')\n"
                    "• Seguros de Vida ('3' o 'seguro vida')\n"
                    "• Tarjetas Médicas VRIM ('4' o 'vrim')\n"
                    "• Financiamiento Empresarial ('5' o 'empresa')\n\n"
                    "Escribe *menú* para ver todas las opciones organizadas."
                )
            return jsonify({"status": "ok"}), 200

        else:
            send_message(phone_number, 
                "Por ahora solo puedo procesar mensajes de texto 📩\n\n"
                "Escribe *menú* para ver los servicios disponibles."
            )
            return jsonify({"status": "ok"}), 200

    except Exception as e:
        logging.exception(f"❌ Error en receive_message: {e}")
        return jsonify({"error": str(e)}), 500

# ---------------------------------------------------------------
# Endpoint de salud
# ---------------------------------------------------------------
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "Vicky Bot Inbursa"}), 200

# ---------------------------------------------------------------
# Ejecución principal
# ---------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    logging.info(f"🚀 Iniciando Vicky Bot en puerto {port}")
    app.run(host="0.0.0.0", port=port)
