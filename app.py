import os
import json
import logging
import requests
import re
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from datetime import datetime
import openai

# ---------------------------------------------------------------
# Cargar variables de entorno
# ---------------------------------------------------------------
load_dotenv()

META_TOKEN = os.getenv("META_TOKEN")
WABA_PHONE_ID = os.getenv("WABA_PHONE_ID")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
ADVISOR_NUMBER = os.getenv("ADVISOR_NUMBER", "5216682478005")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

openai.api_key = OPENAI_API_KEY

app = Flask(__name__)

user_state = {}
user_data = {}

# ---------------------------------------------------------------
# FUNCIÓN SEND_MESSAGE MEJORADA - ÚNICA MODIFICACIÓN CRÍTICA
# ---------------------------------------------------------------
def send_message(to, text):
    """Envía mensajes de texto al usuario vía Meta Cloud API - VERSIÓN MEJORADA"""
    try:
        # Validación de variables críticas
        if not META_TOKEN:
            logging.error("❌ META_TOKEN no configurado - No se puede enviar mensaje")
            return False
        if not WABA_PHONE_ID:
            logging.error("❌ WABA_PHONE_ID no configurado - No se puede enviar mensaje")
            return False
            
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
        
        logging.info(f"📤 Intentando enviar mensaje a {to}: {text[:50]}...")
        response = requests.post(url, headers=headers, json=payload, timeout=10)
        
        if response.status_code in (200, 201):
            logging.info(f"✅ Mensaje enviado CORRECTAMENTE a {to}")
            return True
        else:
            logging.error(f"❌ Error API Meta al enviar a {to}: {response.status_code} - {response.text}")
            return False
            
    except Exception as e:
        logging.exception(f"💥 Error CRÍTICO en send_message para {to}: {e}")
        return False

def send_whatsapp_message(to, text):
    return send_message(to, text)

# ---------------------------------------------------------------
# ENDPOINT DE DIAGNÓSTICO TEMPORAL - SOLO PARA DEBUGGING
# ---------------------------------------------------------------
@app.route("/debug-notification", methods=["GET", "POST"])
def debug_notification():
    """Endpoint temporal para probar notificaciones al asesor"""
    if request.method == "GET":
        return jsonify({
            "service": "Debug Notificaciones Vicky",
            "advisor_number": ADVISOR_NUMBER,
            "variables_configuradas": {
                "META_TOKEN": bool(META_TOKEN),
                "WABA_PHONE_ID": bool(WABA_PHONE_ID),
                "ADVISOR_NUMBER": ADVISOR_NUMBER
            }
        }), 200
    
    # POST: Probar envío de notificación real
    try:
        test_message = (
            f"🔔 PRUEBA: Notificación de Vicky Bot\n"
            f"📞 Para: {ADVISOR_NUMBER}\n"
            f"🕐 Hora: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"✅ Si recibes esto, las notificaciones funcionan"
        )
        
        success = send_message(ADVISOR_NUMBER, test_message)
        
        return jsonify({
            "notification_test": {
                "sent_to": ADVISOR_NUMBER,
                "success": success,
                "timestamp": datetime.now().isoformat(),
                "message_preview": test_message[:100] + "..."
            }
        }), 200
        
    except Exception as e:
        logging.error(f"❌ Error en debug-notification: {e}")
        return jsonify({"error": str(e)}), 500

# ---------------------------------------------------------------
# MANTENER TODO EL RESTO DEL CÓDIGO EXACTAMENTE IGUAL
# ---------------------------------------------------------------
def extract_number(text):
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

def interpret_response(text):
    text_lower = (text or '').lower()
    positive_keywords = ['sí', 'si', 'sip', 'claro', 'por supuesto', 'ok', 'vale', 'afirmativo', 'acepto', 'yes']
    negative_keywords = ['no', 'nop', 'negativo', 'para nada', 'no acepto', 'not']
    if any(k in text_lower for k in positive_keywords):
        return 'positive'
    if any(k in text_lower for k in negative_keywords):
        return 'negative'
    return 'neutral'

def is_valid_name(text):
    if not text or len(text.strip()) < 2:
        return False
    if re.match(r'^[a-zA-ZáéíóúÁÉÍÓÚñÑüÜ\s\.\-]+$', text.strip()):
        return True
    return False

def is_valid_phone(text):
    if not text:
        return False
    clean_phone = re.sub(r'[\s\-\(\)\+]', '', text)
    return re.match(r'^\d{10,15}$', clean_phone) is not None

def send_main_menu(phone):
    menu = (
        "🏦 *INBURSA - SERVICIOS DISPONIBLES*\n\n"
        "1️⃣ Préstamos IMSS Pensionados (Ley 73)\n"
        "2️⃣ Seguros de Auto\n"
        "3️⃣ Seguros de Vida y Salud\n"
        "4️⃣ Tarjetas Médicas VRIM\n"
        "5️⃣ Financiamiento Empresarial\n\n"
        "Escribe el *número* o el *nombre* del servicio que te interesa:"
    )
    send_message(phone, menu)

def ask_gpt(prompt, model="gpt-3.5-turbo", temperature=0.7):
    try:
        response = openai.ChatCompletion.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
            max_tokens=400
        )
        return response.choices[0].message["content"].strip()
    except Exception as e:
        logging.exception(f"Error con OpenAI: {e}")
        return "Lo siento, ocurrió un error al consultar GPT."

def is_gpt_command(msg):
    return re.match(r'^\s*gpt\s*:', msg.lower())

def funnel_prestamo_imss(user_id, user_message):
    state = user_state.get(user_id, "menu_mostrar_beneficios")
    datos = user_data.get(user_id, {})

    # Paso 0: Mostrar beneficios y preguntar si es pensionado
    if state == "menu_mostrar_beneficios":
        send_message(user_id,
            "💰 *Beneficios del Préstamo para Pensionados IMSS (Ley 73)*\n"
            "- Montos desde $40,000 hasta $650,000\n"
            "- Descuento vía pensión (sin buró de crédito)\n"
            "- Plazos de 12 a 60 meses\n"
            "- Depósito directo a tu cuenta\n"
            "- Sin aval ni garantía"
        )
        send_message(user_id,
            "🏦 *Beneficios adicionales si recibes tu pensión en Inbursa:*\n"
            "- Tasas preferenciales y pagos más bajos\n"
            "- Acceso a seguro de vida sin costo\n"
            "- Anticipo de nómina disponible\n"
            "- Atención personalizada 24/7\n\n"
            "*(Estos beneficios son adicionales y no son obligatorios para obtener tu crédito.)*"
        )
        send_message(user_id,
            "¿Eres pensionado o jubilado del IMSS bajo la Ley 73?"
        )
        user_state[user_id] = "pregunta_pensionado"
        return jsonify({"status": "ok", "funnel": "prestamo_imss"})

    # Paso 1: Pregunta pensionado
    if state == "pregunta_pensionado":
        resp = interpret_response(user_message)
        if resp == "negative":
            send_main_menu(user_id)
            user_state.pop(user_id, None)
            user_data.pop(user_id, None)
            return jsonify({"status": "ok", "funnel": "prestamo_imss"})
        elif resp == "positive":
            send_message(user_id,
                "¿Cuánto recibes aproximadamente al mes por concepto de pensión?"
            )
            user_state[user_id] = "pregunta_monto_pension"
            return jsonify({"status": "ok", "funnel": "prestamo_imss"})
        else:
            send_message(user_id, "Por favor responde *sí* o *no* para continuar.")
            return jsonify({"status": "ok", "funnel": "prestamo_imss"})

    # Paso 2: Monto de pensión
    if state == "pregunta_monto_pension":
        monto_pension = extract_number(user_message)
        if monto_pension is None:
            send_message(user_id, "Indica el monto mensual que recibes por pensión, ejemplo: 6500")
            return jsonify({"status": "ok", "funnel": "prestamo_imss"})
        if monto_pension < 5000:
            send_message(user_id,
                "Por ahora los créditos disponibles aplican a pensiones a partir de $5,000.\n"
                "Pero puedo notificar a nuestro asesor para ofrecerte otra opción sin compromiso. ¿Deseas que lo haga?"
            )
            user_state[user_id] = "pregunta_ofrecer_asesor"
            user_data[user_id] = {"pension_mensual": monto_pension}
            return jsonify({"status": "ok", "funnel": "prestamo_imss"})
        user_data[user_id] = {"pension_mensual": monto_pension}
        send_message(user_id,
            "Perfecto 👏 ¿Qué monto de préstamo te gustaría solicitar? (mínimo $40,000)"
        )
        user_state[user_id] = "pregunta_monto_solicitado"
        return jsonify({"status": "ok", "funnel": "prestamo_imss"})

    # Paso 2b: Ofrecer asesor por pensión baja
    if state == "pregunta_ofrecer_asesor":
        resp = interpret_response(user_message)
        if resp == "positive":
            send_message(user_id,
                "¡Listo! Un asesor te contactará para ofrecerte opciones alternativas. Gracias por confiar en nosotros 🙌."
            )
            datos = user_data.get(user_id, {})
            formatted = (
                f"🔔 NUEVO PROSPECTO – PRÉSTAMO IMSS\n"
                f"Nombre: {datos.get('nombre','N/D')}\n"
                f"Número WhatsApp: {user_id}\n"
                f"Pensión mensual: ${datos.get('pension_mensual','N/D'):,.0f}\n"
                f"Estatus: Pensión baja, requiere opciones alternativas"
            )
            send_whatsapp_message(ADVISOR_NUMBER, formatted)
            send_message(user_id, "¡Listo! Además, tenemos otros servicios financieros que podrían interesarte: 👇")
            send_main_menu(user_id)
            user_state.pop(user_id, None)
            user_data.pop(user_id, None)
            return jsonify({"status": "ok", "funnel": "prestamo_imss"})
        else:
            send_message(user_id, "Perfecto, si deseas podemos continuar con otros servicios.")
            send_message(user_id, "¡Listo! Además, tenemos otros servicios financieros que podrían interesarte: 👇")
            send_main_menu(user_id)
            user_state.pop(user_id, None)
            user_data.pop(user_id, None)
            return jsonify({"status": "ok", "funnel": "prestamo_imss"})

    # Paso 3: Monto solicitado
    if state == "pregunta_monto_solicitado":
        monto_solicitado = extract_number(user_message)
        if monto_solicitado is None or monto_solicitado < 40000:
            send_message(user_id, "Indica el monto que deseas solicitar (mínimo $40,000), ejemplo: 65000")
            return jsonify({"status": "ok", "funnel": "prestamo_imss"})
        user_data[user_id]["monto_solicitado"] = monto_solicitado
        send_message(user_id,
            "¿Cuál es tu nombre completo?"
        )
        user_state[user_id] = "pregunta_nombre"
        return jsonify({"status": "ok", "funnel": "prestamo_imss"})

    # Paso 4: Pregunta nombre
    if state == "pregunta_nombre":
        user_data[user_id]["nombre"] = user_message.title()
        send_message(user_id,
            "¿Cuál es tu teléfono de contacto?"
        )
        user_state[user_id] = "pregunta_telefono"
        return jsonify({"status": "ok", "funnel": "prestamo_imss"})

    # Paso 5: Pregunta teléfono
    if state == "pregunta_telefono":
        user_data[user_id]["telefono_contacto"] = user_message
        send_message(user_id,
            "¿En qué ciudad vives?"
        )
        user_state[user_id] = "pregunta_ciudad"
        return jsonify({"status": "ok", "funnel": "prestamo_imss"})

    # Paso 6: Pregunta ciudad
    if state == "pregunta_ciudad":
        user_data[user_id]["ciudad"] = user_message.title()
        send_message(user_id,
            "¿Ya recibes tu pensión en Inbursa?"
        )
        user_state[user_id] = "pregunta_nomina_inbursa"
        return jsonify({"status": "ok", "funnel": "prestamo_imss"})

    # Paso 7: Nómina Inbursa
    if state == "pregunta_nomina_inbursa":
        resp = interpret_response(user_message)
        if resp == "positive":
            send_message(user_id,
                "Excelente, con Inbursa tendrás acceso a beneficios adicionales y atención prioritaria."
            )
            user_data[user_id]["nomina_inbursa"] = "Sí"
        elif resp == "negative":
            send_message(user_id,
                "No hay problema 😊, los beneficios adicionales solo aplican si tienes la nómina con nosotros,\n"
                "pero puedes cambiarte cuando gustes, sin costo ni compromiso."
            )
            user_data[user_id]["nomina_inbursa"] = "No"
        else:
            send_message(user_id, "Por favor responde *sí* o *no* para continuar.")
            return jsonify({"status": "ok", "funnel": "prestamo_imss"})
        send_message(user_id,
            "¡Listo! 🎉 Tu crédito ha sido preautorizado.\n"
            "Un asesor financiero (Christian López) se pondrá en contacto contigo para continuar con el trámite.\n"
            "Gracias por tu confianza 🙌."
        )
        datos = user_data.get(user_id, {})
        formatted = (
            f"🔔 NUEVO PROSPECTO – PRÉSTAMO IMSS\n"
            f"Nombre: {datos.get('nombre','N/D')}\n"
            f"Número WhatsApp: {user_id}\n"
            f"Teléfono contacto: {datos.get('telefono_contacto','N/D')}\n"
            f"Ciudad: {datos.get('ciudad','N/D')}\n"
            f"Monto solicitado: ${datos.get('monto_solicitado','N/D'):,.0f}\n"
            f"Estatus: Preautorizado\n"
            f"Observación: Nómina Inbursa: {datos.get('nomina_inbursa','N/D')}"
        )
        send_whatsapp_message(ADVISOR_NUMBER, formatted)
        send_message(user_id, "¡Listo! Además, tenemos otros servicios financieros que podrían interesarte: 👇")
        send_main_menu(user_id)
        user_state.pop(user_id, None)
        user_data.pop(user_id, None)
        return jsonify({"status": "ok", "funnel": "prestamo_imss"})

    send_main_menu(user_id)
    return jsonify({"status": "ok", "funnel": "prestamo_imss"})

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
        entry = data.get("entry", [])[0]
        change = entry.get("changes", [])[0]
        value = change.get("value", {})
        messages = value.get("messages", [])

        if not messages:
            return jsonify({"status": "ignored"}), 200

        message = messages[0]
        phone_number = message.get("from")
        message_type = message.get("type")
        user_message = ""
        if message_type == "text":
            user_message = message["text"]["body"].strip()
        else:
            send_message(phone_number, 
                "Por ahora solo puedo procesar mensajes de texto 📩\n\n"
                "Escribe *menú* para ver los servicios disponibles."
            )
            return jsonify({"status": "ok"}), 200

        logging.info(f"📱 Mensaje de {phone_number}: '{user_message}'")

        # GPT SOLO BAJO COMANDO (en cualquier parte del bot)
        if is_gpt_command(user_message):
            prompt = user_message.split(":",1)[1].strip()
            if not prompt:
                send_message(phone_number, "Para consultar GPT, escribe por ejemplo:\ngpt: ¿Qué ventajas tiene el crédito IMSS?")
                return jsonify({"status": "ok", "source": "gpt"})
            gpt_reply = ask_gpt(prompt)
            send_message(phone_number, gpt_reply)
            return jsonify({"status": "ok", "source": "gpt"})

        menu_options = {
            "1": "prestamo_imss",
            "préstamo": "prestamo_imss",
            "prestamo": "prestamo_imss",
            "imss": "prestamo_imss",
            "ley 73": "prestamo_imss",
            "pension": "prestamo_imss",
            "pensión": "prestamo_imss",
            "2": "seguro_auto",
            "seguro auto": "seguro_auto",
            "seguros de auto": "seguro_auto",
            "auto": "seguro_auto",
            "3": "seguro_vida",
            "seguro vida": "seguro_vida",
            "seguros de vida": "seguro_vida",
            "seguro salud": "seguro_vida",
            "vida": "seguro_vida",
            "4": "vrim",
            "tarjetas médicas": "vrim",
            "tarjetas medicas": "vrim",
            "vrim": "vrim",
            "5": "empresarial",
            "financiamiento empresarial": "empresarial",
            "empresa": "empresarial",
            "negocio": "empresarial",
            "pyme": "empresarial",
            "crédito empresarial": "empresarial",
            "credito empresarial": "empresarial"
        }

        option = menu_options.get(user_message.lower())

        # FLUJO IMSS: Si está en embudo, seguir el estado
        current_state = user_state.get(phone_number)
        if current_state and ("prestamo_imss" in current_state or "pregunta_" in current_state):
            return funnel_prestamo_imss(phone_number, user_message)

        # Opción 1: Iniciar embudo IMSS
        if option == "prestamo_imss":
            user_state[phone_number] = "menu_mostrar_beneficios"
            return funnel_prestamo_imss(phone_number, user_message)

        # Otros servicios - menú estándar
        if option == "seguro_auto":
            send_message(phone_number,
                "🚗 *Seguros de Auto Inbursa*\n\n"
                "Protege tu auto con las mejores coberturas:\n\n"
                "✅ Cobertura amplia contra todo riesgo\n"
                "✅ Asistencia vial las 24 horas\n"
                "✅ Responsabilidad civil\n"
                "✅ Robo total y parcial\n\n"
                "📞 Un asesor se comunicará contigo para cotizar tu seguro."
            )
            send_whatsapp_message(ADVISOR_NUMBER, f"🚗 NUEVO INTERESADO EN SEGURO DE AUTO\n📞 {phone_number}")
            return jsonify({"status": "ok", "funnel": "menu"})
        if option == "seguro_vida":
            send_message(phone_number,
                "🏥 *Seguros de Vida y Salud Inbursa*\n\n"
                "Protege a tu familia y tu salud:\n\n"
                "✅ Seguro de vida\n"
                "✅ Gastos médicos mayores\n"
                "✅ Hospitalización\n"
                "✅ Atención médica las 24 horas\n\n"
                "📞 Un asesor se comunicará contigo para explicarte las coberturas."
            )
            send_whatsapp_message(ADVISOR_NUMBER, f"🏥 NUEVO INTERESADO EN SEGURO VIDA/SALUD\n📞 {phone_number}")
            return jsonify({"status": "ok", "funnel": "menu"})
        if option == "vrim":
            send_message(phone_number,
                "💳 *Tarjetas Médicas VRIM*\n\n"
                "Accede a la mejor atención médica:\n\n"
                "✅ Consultas médicas ilimitadas\n"
                "✅ Especialistas y estudios de laboratorio\n"
                "✅ Medicamentos con descuento\n"
                "✅ Atención dental y oftalmológica\n\n"
                "📞 Un asesor se comunicará contigo para explicarte los beneficios."
            )
            send_whatsapp_message(ADVISOR_NUMBER, f"💳 NUEVO INTERESADO EN TARJETAS VRIM\n📞 {phone_number}")
            return jsonify({"status": "ok", "funnel": "menu"})
        if option == "empresarial":
            send_message(phone_number,
                "🏢 *Financiamiento Empresarial Inbursa*\n\n"
                "Impulsa el crecimiento de tu negocio con:\n\n"
                "✅ Créditos desde $100,000 hasta $100,000,000\n"
                "✅ Tasas preferenciales\n"
                "✅ Plazos flexibles\n"
                "✅ Asesoría especializada\n\n"
                "📞 Un asesor se pondrá en contacto contigo para analizar tu proyecto."
            )
            send_whatsapp_message(ADVISOR_NUMBER, f"🏢 NUEVO INTERESADO EN FINANCIAMIENTO EMPRESARIAL\n📞 {phone_number}")
            return jsonify({"status": "ok", "funnel": "menu"})

        # Comando de menú
        if user_message.lower() in ["menu", "menú", "men", "opciones", "servicios"]:
            user_state.pop(phone_number, None)
            user_data.pop(phone_number, None)
            send_main_menu(phone_number)
            return jsonify({"status": "ok", "funnel": "menu"})

        if user_message.lower() in ["hola", "hi", "hello", "buenas", "buenos días", "buenas tardes"]:
            send_main_menu(phone_number)
            return jsonify({"status": "ok", "funnel": "menu"})

        send_main_menu(phone_number)
        return jsonify({"status": "ok", "funnel": "menu"})

    except Exception as e:
        logging.exception(f"❌ Error en receive_message: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "Vicky Bot Inbursa"}), 200

def send_campaign_message(phone_number, nombre):
    """
    Envía un mensaje tipo plantilla promocional usando la API de WhatsApp Business.
    La plantilla se llama "credito_imss_promocion_1" en idioma "es_MX".
    El nombre del prospecto se incluye como parámetro {{1}}.
    """
    try:
        url = f"https://graph.facebook.com/v20.0/{WABA_PHONE_ID}/messages"
        headers = {
            "Authorization": f"Bearer {META_TOKEN}",
            "Content-Type": "application/json"
        }
        payload = {
            "messaging_product": "whatsapp",
            "to": str(phone_number),
            "type": "template",
            "template": {
                "name": "credito_imss_promocion_1",
                "language": {"code": "es_MX"},
                "components": [
                    {
                        "type": "body",
                        "parameters": [
                            {"type": "text", "text": str(nombre)}
                        ]
                    }
                ]
            }
        }
        response = requests.post(url, headers=headers, json=payload)
        response.raise_for_status()
        logging.info(f"✅ Mensaje campaña enviado a {phone_number} ({nombre})")
    except Exception as e:
        logging.exception(f"❌ Error en send_campaign_message: {e}")

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    logging.info(f"🚀 Iniciando Vicky Bot en puerto {port}")
    app.run(host="0.0.0.0", port=port)








