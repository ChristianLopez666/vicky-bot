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
import openai
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
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

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
# CONFIGURACIÓN GPT MEJORADA
# ---------------------------------------------------------------
openai.api_key = OPENAI_API_KEY

def consultar_gpt(mensaje, contexto=""):
    """Consulta a GPT para interpretación de intenciones mejorada"""
    try:
        if not OPENAI_API_KEY:
            return interpret_response_tradicional(mensaje)
            
        prompt = f"""
        Eres un asistente especializado en préstamos IMSS. 
        Contexto: {contexto}
        
        Mensaje del usuario: "{mensaje}"
        
        Responde ÚNICAMENTE con:
        - "positive" si la intención es AFIRMATIVA (sí, acepto, quiero, me interesa)
        - "negative" si la intención es NEGATIVA (no, rechazo, no quiero)
        - "neutral" si no es claro
        
        Ejemplos:
        "sí" → "positive"
        "claro que sí" → "positive" 
        "no" → "negative"
        "para nada" → "negative"
        "quizás" → "neutral"
        "hola" → "neutral"
        """
        
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "Eres un clasificador de intenciones para préstamos IMSS."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=10,
            temperature=0.1
        )
        
        resultado = response.choices[0].message['content'].strip().lower()
        return resultado if resultado in ['positive', 'negative', 'neutral'] else 'neutral'
        
    except Exception as e:
        logging.error(f"❌ Error GPT: {e}")
        return interpret_response_tradicional(mensaje)

def interpret_response_tradicional(text):
    """Lógica tradicional de interpretación como respaldo"""
    text_lower = (text or '').lower()
    positive_keywords = ['sí', 'si', 'sip', 'claro', 'por supuesto', 'ok', 'vale', 'afirmativo', 'acepto', 'yes', 'correcto', 'por su puesto']
    negative_keywords = ['no', 'nop', 'negativo', 'para nada', 'no acepto', 'not', 'nel', 'negativo']
    
    if any(k in text_lower for k in positive_keywords):
        return 'positive'
    if any(k in text_lower for k in negative_keywords):
        return 'negative'
    return 'neutral'

def interpret_response(text, contexto=""):
    """Interpreta respuestas afirmativas/negativas con GPT como respaldo."""
    intent_tradicional = interpret_response_tradicional(text)
    
    if intent_tradicional != 'neutral':
        return intent_tradicional
    
    return consultar_gpt(text, contexto)

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

def is_thankyou_message(text):
    """Detecta mensajes de agradecimiento."""
    text_lower = text.lower().strip()
    thankyou_keywords = [
        'gracias', 'grac', 'gracia', 'thank', 'thanks', 'agradecido', 
        'agradecida', 'agradecimiento', 'te lo agradezco', 'mil gracias'
    ]
    return any(keyword in text_lower for keyword in thankyou_keywords)

def is_valid_name(text):
    """Valida que el texto sea un nombre válido."""
    if not text or len(text.strip()) < 2:
        return False
    if re.match(r'^[a-zA-ZáéíóúÁÉÍÓÚñÑüÜ\s\.\-]+$', text.strip()):
        return True
    return False

def is_valid_phone(text):
    """Valida que el texto sea un teléfono válido."""
    if not text:
        return False
    clean_phone = re.sub(r'[\s\-\(\)\+]', '', text)
    return re.match(r'^\d{10,15}$', clean_phone) is not None

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

def handle_imss_flow(phone_number, user_message):
    """Gestiona el flujo completo del préstamo IMSS Ley 73."""
    msg = user_message.lower()

    imss_keywords = ["préstamo", "prestamo", "imss", "pensión", "pension", "ley 73", "1"]
    
    if any(keyword in msg for keyword in imss_keywords):
        current_state = user_state.get(phone_number)
        if current_state not in ["esperando_respuesta_imss", "esperando_monto_solicitado", "esperando_respuesta_nomina", "esperando_nombre_imss", "esperando_telefono_imss", "esperando_ciudad_imss"]:
            send_message(phone_number,
                "👋 ¡Hola! Antes de continuar, necesito confirmar algo importante.\n\n"
                "¿Eres pensionado o jubilado del IMSS bajo la Ley 73? (Responde *sí* o *no*)"
            )
            user_state[phone_number] = "esperando_respuesta_imss"
        return True

    if user_state.get(phone_number) == "esperando_respuesta_imss":
        intent = interpret_response(msg, "confirmar si es pensionado IMSS")
        if intent == 'negative':
            send_message(phone_number,
                "Entiendo. Para el préstamo IMSS Ley 73 es necesario ser pensionado del IMSS. 😔\n\n"
                "Pero tengo otros servicios que pueden interesarte:"
            )
            send_main_menu(phone_number)
            user_state.pop(phone_number, None)
        elif intent == 'positive':
            send_message(phone_number,
                "Excelente 👏\n\n¿Qué monto de préstamo deseas solicitar?"
            )
            user_state[phone_number] = "esperando_monto_solicitado"
        else:
            send_message(phone_number, 
                "Por favor responde *sí* o *no* para continuar:\n\n"
                "• *SÍ* - Si eres pensionado del IMSS\n"
                "• *NO* - Si no eres pensionado"
            )
        return True

    if user_state.get(phone_number) == "esperando_monto_solicitado":
        if is_thankyou_message(msg):
            send_message(phone_number,
                "¡Por nada! 😊\n\n"
                "Sigamos con tu solicitud...\n\n"
                "¿Qué monto deseas solicitar?"
            )
            return True
            
        monto = extract_number(msg)
        if monto is not None:
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

    if user_state.get(phone_number) == "esperando_respuesta_nomina":
        if is_thankyou_message(msg):
            send_message(phone_number,
                "¡De nada! 😊\n\n"
                "Para continuar, por favor responde *sí* o *no*:\n\n"
                "¿Aceptas cambiar tu nómina a Inbursa para acceder a beneficios adicionales?"
            )
            return True
            
        intent = interpret_response(msg, "cambio de nómina a Inbursa")
        
        data = user_data.get(phone_number, {})
        monto_solicitado = data.get('monto_solicitado', 'N/D')
        
        if intent == 'positive' or intent == 'negative':
            if intent == 'positive':
                send_message(phone_number,
                    "✅ *¡Excelente decisión!* Al cambiar tu nómina a Inbursa accederás a todos los beneficios adicionales.\n\n"
                    "Ahora necesitamos algunos datos de contacto:\n\n"
                    "👤 ¿Cuál es tu nombre completo?"
                )
            else:
                send_message(phone_number,
                    "✅ *¡Perfecto!* Entiendo que por el momento prefieres mantener tu nómina actual.\n\n"
                    "Ahora necesitamos algunos datos de contacto:\n\n"
                    "👤 ¿Cuál es tu nombre completo?"
                )
            
            user_data[phone_number]["nomina_inbursa"] = "ACEPTADA" if intent == 'positive' else "NO POR AHORA"
            user_state[phone_number] = "esperando_nombre_imss"
            
        else:
            send_message(phone_number, 
                "Por favor responde *sí* o *no*:\n\n"
                "• *SÍ* - Para acceder a todos los beneficios adicionales con nómina Inbursa\n"
                "• *NO* - Para continuar con tu préstamo manteniendo tu nómina actual"
            )
        return True

    if user_state.get(phone_number) == "esperando_nombre_imss":
        if is_valid_name(user_message):
            user_data[phone_number]["nombre_contacto"] = user_message.title()
            send_message(phone_number,
                f"✅ Nombre registrado: {user_message.title()}\n\n"
                "📞 ¿En qué número telefónico podemos contactarte?\n\n"
                "💡 Puedes proporcionar el mismo número de WhatsApp o uno diferente"
            )
            user_state[phone_number] = "esperando_telefono_imss"
        else:
            send_message(phone_number,
                "Por favor ingresa un nombre válido (solo letras y espacios):\n\n"
                "Ejemplo: Juan Pérez García"
            )
        return True

    if user_state.get(phone_number) == "esperando_telefono_imss":
        if is_valid_phone(user_message):
            user_data[phone_number]["telefono_contacto"] = user_message
            send_message(phone_number,
                f"✅ Teléfono registrado: {user_message}\n\n"
                "🏙️ ¿En qué ciudad te encuentras?"
            )
            user_state[phone_number] = "esperando_ciudad_imss"
        else:
            send_message(phone_number,
                "Por favor ingresa un número de teléfono válido (10 dígitos mínimo):\n\n"
                "Ejemplo: 6681234567 o +526681234567"
            )
        return True

    if user_state.get(phone_number) == "esperando_ciudad_imss":
        user_data[phone_number]["ciudad"] = user_message.title()
        
        data = user_data.get(phone_number, {})
        monto_solicitado = data.get('monto_solicitado', 'N/D')
        nomina_status = data.get('nomina_inbursa', 'N/D')
        nombre_contacto = data.get('nombre_contacto', 'N/D')
        telefono_contacto = data.get('telefono_contacto', 'N/D')
        ciudad = data.get('ciudad', 'N/D')

        send_message(phone_number,
            "🎉 *¡Excelente!* Hemos registrado tu solicitud de préstamo IMSS Ley 73.\n\n"
            "📞 *Christian te contactará en breve* para:\n\n"
            "• Confirmar los detalles de tu préstamo\n"
            "• Explicarte el proceso de desembolso\n"
            "• Agendar el cambio de nómina si así lo decidiste\n\n"
            "¡Gracias por confiar en Inbursa! 🏦"
        )

        mensaje_asesor = (
            f"🔥 *NUEVO PROSPECTO IMSS LEY 73 - INFORMACIÓN COMPLETA*\n\n"
            f"👤 Nombre: {nombre_contacto}\n"
            f"📞 Teléfono WhatsApp: {phone_number}\n"
            f"📱 Teléfono contacto: {telefono_contacto}\n"
            f"🏙️ Ciudad: {ciudad}\n"
            f"💵 Monto solicitado: ${monto_solicitado:,.0f}\n"
            f"🏦 Nómina Inbursa: {nomina_status}\n\n"
            f"🎯 *Cliente listo para contacto inmediato*"
        )
        send_message(ADVISOR_NUMBER, mensaje_asesor)
        
        user_state.pop(phone_number, None)
        user_data.pop(phone_number, None)
        return True

    return False

def handle_business_flow(phone_number, user_message):
    """Gestiona el flujo completo de crédito empresarial."""
    msg = user_message.lower()

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

    if user_state.get(phone_number) == "esperando_tipo_credito":
        user_data[phone_number] = {"tipo_credito": user_message}
        send_message(phone_number,
            "📊 Perfecto. ¿A qué se dedica tu empresa? (giro o actividad principal)"
        )
        user_state[phone_number] = "esperando_giro_empresa"
        return True

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

    if user_state.get(phone_number) == "esperando_nombre_empresarial":
        if is_valid_name(user_message):
            user_data[phone_number]["nombre_contacto"] = user_message.title()
            send_message(phone_number,
                f"✅ Nombre registrado: {user_message.title()}\n\n"
                "📞 ¿En qué número telefónico podemos contactarte?\n\n"
                "💡 Puedes proporcionar el mismo número de WhatsApp o uno diferente"
            )
            user_state[phone_number] = "esperando_telefono_empresarial"
        else:
            send_message(phone_number,
                "Por favor ingresa un nombre válido (solo letras y espacios):\n\n"
                "Ejemplo: Juan Pérez García"
            )
        return True

    if user_state.get(phone_number) == "esperando_telefono_empresarial":
        if is_valid_phone(user_message):
            user_data[phone_number]["telefono_contacto"] = user_message
            send_message(phone_number,
                f"✅ Teléfono registrado: {user_message}\n\n"
                "🏙️ ¿En qué ciudad se encuentra tu empresa?"
            )
            user_state[phone_number] = "esperando_ciudad_empresarial"
        else:
            send_message(phone_number,
                "Por favor ingresa un número de teléfono válido (10 dígitos mínimo):\n\n"
                "Ejemplo: 6681234567 o +526681234567"
            )
        return True

    if user_state.get(phone_number) == "esperando_ciudad_empresarial":
        user_data[phone_number]["ciudad_empresa"] = user_message.title()
        send_message(phone_number,
            f"✅ Ciudad registrada: {user_message.title()}\n\n"
            "📅 ¿Qué día y horario prefieres para que te contacte un especialista?\n\n"
            "Ejemplo: Lunes a viernes de 9am a 2pm"
        )
        user_state[phone_number] = "esperando_contacto_empresarial"
        return True

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

        mensaje_asesor = (
            f"🏢 *NUEVO PROSPECTO EMPRESARIAL - INFORMACIÓN COMPLETA*\n\n"
            f"👤 Nombre: {data.get('nombre_contacto', 'N/D')}\n"
            f"📞 Teléfono WhatsApp: {phone_number}\n"
            f"📱 Teléfono contacto: {data.get('telefono_contacto', phone_number)}\n"
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

            if user_state.get(phone_number) in ["esperando_respuesta_imss", "esperando_monto_solicitado", "esperando_respuesta_nomina", "esperando_nombre_imss", "esperando_telefono_imss", "esperando_ciudad_imss"]:
                if handle_imss_flow(phone_number, user_message):
                    return jsonify({"status": "ok"}), 200

            if user_state.get(phone_number) in ["inicio_empresarial", "esperando_tipo_credito", 
                                              "esperando_giro_empresa", "esperando_monto_empresarial",
                                              "esperando_nombre_empresarial", "esperando_telefono_empresarial",
                                              "esperando_ciudad_empresarial", "esperando_contacto_empresarial"]:
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

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "Vicky Bot Inbursa"}), 200

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    logging.info(f"🚀 Iniciando Vicky Bot en puerto {port}")
    app.run(host="0.0.0.0", port=port)
