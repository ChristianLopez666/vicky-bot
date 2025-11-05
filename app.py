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

# Estados y datos por usuario
user_state = {}
user_data = {}

# ---------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ---------------------------------------------------------------
# ENVÍO DE MENSAJES WHATSAPP (TEXT)
# ---------------------------------------------------------------
def send_message(to: str, text: str) -> bool:
    try:
        if not META_TOKEN or not WABA_PHONE_ID:
            logging.error("❌ Falta META_TOKEN o WABA_PHONE_ID.")
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
        logging.error(f"❌ Error WhatsApp API {resp.status_code}: {resp.text}")
        return False
    except Exception as e:
        logging.exception(f"💥 Error en send_message: {e}")
        return False


def send_whatsapp_message(to: str, text: str) -> bool:
    return send_message(to, text)


# ---------------------------------------------------------------
# UTILIDADES
# ---------------------------------------------------------------
def interpret_response(text: str) -> str:
    t = (text or "").strip().lower()
    positive = ["sí", "si", "sip", "claro", "ok", "vale", "afirmativo", "yes"]
    negative = ["no", "nop", "negativo", "para nada", "not"]
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


def send_main_menu(phone: str):
    menu = (
        "🏦 *INBURSA - SERVICIOS DISPONIBLES*\n\n"
        "1️⃣ Préstamos IMSS Pensionados (Ley 73)\n"
        "2️⃣ Seguros de Auto\n"
        "3️⃣ Seguros de Vida y Salud\n"
        "4️⃣ Tarjetas Médicas VRIM\n"
        "5️⃣ Financiamiento Empresarial\n"
        "6️⃣ Financiamiento Práctico Empresarial (desde 24 hrs)\n\n"
        "Escribe el número o el nombre del servicio que te interesa."
    )
    send_message(phone, menu)


# ---------------------------------------------------------------
# GPT (opcional por comando "sgpt: ...")
# ---------------------------------------------------------------
def ask_gpt(prompt: str, model: str = "gpt-3.5-turbo", temperature: float = 0.7) -> str:
    try:
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
# EMBUDO – PRÉSTAMO IMSS PENSIONADOS (Opción 1)
# ---------------------------------------------------------------
def funnel_prestamo_imss(user_id: str, user_message: str):
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
            send_whatsapp_message(ADVISOR_NUMBER, formatted)
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
        send_whatsapp_message(ADVISOR_NUMBER, formatted)
        send_main_menu(user_id)
        user_state.pop(user_id, None)
        user_data.pop(user_id, None)
        return jsonify({"status": "ok"})

    send_main_menu(user_id)
    return jsonify({"status": "ok"})


# ---------------------------------------------------------------
# EMBUDO – CRÉDITO EMPRESARIAL (Opción 5) – **Corregido**
# ---------------------------------------------------------------
def funnel_credito_empresarial(user_id: str, user_message: str):
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

    # 🔧 BLOQUE CORREGIDO: cierre del embudo + notificación al 6682478005
    if state == "emp_ciudad":
        datos["ciudad"] = user_message.title()
        user_data[user_id] = datos

        # Mensaje de cierre al prospecto
        send_message(
            user_id,
            "✅ Gracias por la información. Un asesor financiero (Christian López) "
            "se pondrá en contacto contigo en breve para continuar con tu solicitud."
        )

        # Notificación al asesor con los datos del prospecto
        formatted = (
            "🔔 NUEVO PROSPECTO – CRÉDITO EMPRESARIAL\n"
            f"Nombre: {datos.get('nombre','ND')}\n"
            f"Teléfono: {datos.get('telefono','ND')}\n"
            f"Ciudad: {datos.get('ciudad','ND')}\n"
            f"Monto solicitado: ${datos.get('monto_solicitado','ND')}\n"
            f"Actividad: {datos.get('actividad_empresa','ND')}\n"
            f"WhatsApp: {user_id}"
        )
        send_whatsapp_message("6682478005", formatted)  # envío directo al número solicitado

        # Regreso a menú y limpieza de estado
        send_main_menu(user_id)
        user_state.pop(user_id, None)
        user_data.pop(user_id, None)
        return jsonify({"status": "ok"})

    # Fallback interno del embudo
    send_main_menu(user_id)
    return jsonify({"status": "ok"})


# ---------------------------------------------------------------
# EMBUDO – FINANCIAMIENTO PRÁCTICO EMPRESARIAL (Opción 6)
# ---------------------------------------------------------------
def funnel_financiamiento_practico(user_id: str, user_message: str):
    state = user_state.get(user_id, "fp_intro")
    datos = user_data.get(user_id, {})

    # Paso 1 – Intro
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

    # Paso 2 – Confirmar interés
    if state == "fp_confirmar_interes":
        resp = interpret_response(user_message)
        if resp == "negative":
            send_message(
                user_id,
                "Perfecto 👍. Un ejecutivo te contactará para conocer tus necesidades y "
                "ofrecerte otras opciones."
            )
            send_whatsapp_message(
                ADVISOR_NUMBER,
                f"📩 Prospecto NO interesado en Financiamiento Práctico\nNúmero: {user_id}"
            )
            send_main_menu(user_id)
            user_state.pop(user_id, None)
            user_data.pop(user_id, None)
            return jsonify({"status": "ok"})
        if resp == "positive":
            send_message(
                user_id,
                "Excelente 🙌. Comencemos con un *perfilamiento* rápido.\n"
                "1️⃣ ¿Cuál es el *giro de la empresa*?"
            )
            user_state[user_id] = "fp_q1_giro"
            return jsonify({"status": "ok"})
        send_message(user_id, "Responde *sí* o *no* para continuar.")
        return jsonify({"status": "ok"})

    # Cuestionario – 11 preguntas + comentario
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
        # Guardar respuesta actual y avanzar
        datos[state] = user_message
        user_data[user_id] = datos
        next_index = orden.index(state) + 1
        next_state = orden[next_index]
        user_state[user_id] = next_state

        # Pedir la siguiente pregunta correctamente
        if next_state == "fp_comentario":
            # En "fp_comentario" ya no hay siguiente pregunta en el diccionario.
            # Pedimos el comentario explícitamente para evitar duplicados.
            send_message(user_id, "📝 ¿Deseas dejar *algún comentario adicional* para el asesor?")
        else:
            # El diccionario guarda la pregunta del SIGUIENTE paso asociada al estado ACTUAL.
            send_message(user_id, preguntas[state])
        return jsonify({"status": "ok"})

    # Último paso – recibir comentario y notificar
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
        send_whatsapp_message(ADVISOR_NUMBER, formatted)
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
# WEBHOOK – VERIFICACIÓN (GET) Y RECEPCIÓN (POST)
# ---------------------------------------------------------------
@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if mode == "subscribe" and token == VERIFY_TOKEN:
            return challenge, 200
        return "forbidden", 403

    # POST
    try:
        data = request.get_json(force=True, silent=True) or {}
        entry = (data.get("entry") or [{}])[0]
        change = (entry.get("changes") or [{}])[0]
        value = change.get("value") or {}
        messages = value.get("messages") or []

        if not messages:
            return jsonify({"status": "ignored"}), 200

        message = messages[0]
        phone_number = message.get("from")
        mtype = message.get("type")

        if mtype != "text":
            send_message(phone_number, "Por ahora solo puedo procesar mensajes de texto 📩")
            return jsonify({"status": "ok"}), 200

        user_message = (message.get("text") or {}).get("body", "").strip()
        logging.info(f"📱 {phone_number}: {user_message}")

        # Comando GPT
        if is_gpt_command(user_message):
            prompt = user_message.split(":", 1)[1].strip() if ":" in user_message else ""
            if not prompt:
                send_message(phone_number, "Ejemplo: sgpt: ¿Qué ventajas tiene el crédito IMSS?")
                return jsonify({"status": "ok", "source": "gpt"})
            gpt_reply = ask_gpt(prompt)
            send_message(phone_number, gpt_reply)
            return jsonify({"status": "ok", "source": "gpt"})

        # Si está en algún embudo, continuar
        state = user_state.get(phone_number, "")
        if state.startswith("imss_"):
            return funnel_prestamo_imss(phone_number, user_message)
        if state.startswith("emp_"):
            return funnel_credito_empresarial(phone_number, user_message)
        if state.startswith("fp_"):
            return funnel_financiamiento_practico(phone_number, user_message)

        # Menú / opciones
        menu_options = {
            "1": "prestamo_imss",
            "imss": "prestamo_imss",
            "préstamo": "prestamo_imss",
            "prestamo": "prestamo_imss",
            "ley 73": "prestamo_imss",
            "pensión": "prestamo_imss",
            "pension": "prestamo_imss",

            "2": "seguro_auto",
            "auto": "seguro_auto",
            "seguros de auto": "seguro_auto",

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
            "credito empresarial": "empresarial",

            "6": "financiamiento_practico",
            "financiamiento practico": "financiamiento_practico",
            "crédito simple": "financiamiento_practico",
            "credito simple": "financiamiento_practico",
        }

        option = menu_options.get(user_message.lower())

        if option == "prestamo_imss":
            user_state[phone_number] = "imss_beneficios"
            user_data.setdefault(phone_number, {})
            return funnel_prestamo_imss(phone_number, user_message)

        if option == "empresarial":
            user_state[phone_number] = "emp_beneficios"
            user_data.setdefault(phone_number, {})
            return funnel_credito_empresarial(phone_number, user_message)

        if option == "financiamiento_practico":
            user_state[phone_number] = "fp_intro"
            user_data.setdefault(phone_number, {})
            return funnel_financiamiento_practico(phone_number, user_message)

        # Rutas rápidas de menú
        if user_message.lower() in ["menu", "menú", "hola", "buenas", "servicios", "opciones"]:
            user_state.pop(phone_number, None)
            user_data.pop(phone_number, None)
            send_main_menu(phone_number)
            return jsonify({"status": "ok"})

        # Info rápida para opciones 2,3,4 (notificación + texto)
        if option == "seguro_auto":
            send_message(
                phone_number,
                "🚗 *Seguros de Auto Inbursa*\n"
                "✅ Cobertura amplia\n✅ Asistencia vial 24/7\n✅ RC, robo total/parcial\n\n"
                "📞 Un asesor te contactará para cotizar."
            )
            send_whatsapp_message(ADVISOR_NUMBER, f"🚗 Interesado en Seguro Auto · {phone_number}")
            return jsonify({"status": "ok"})

        if option == "seguro_vida":
            send_message(
                phone_number,
                "🏥 *Seguros de Vida y Salud Inbursa*\n"
                "✅ Vida\n✅ Gastos médicos\n✅ Hospitalización\n✅ Atención 24/7\n\n"
                "📞 Un asesor te contactará para explicar coberturas."
            )
            send_whatsapp_message(ADVISOR_NUMBER, f"🏥 Interesado en Vida/Salud · {phone_number}")
            return jsonify({"status": "ok"})

        if option == "vrim":
            send_message(
                phone_number,
                "💳 *Tarjetas Médicas VRIM*\n"
                "✅ Consultas ilimitadas\n✅ Especialistas y laboratorios\n✅ Descuentos en medicamentos\n\n"
                "📞 Un asesor te contactará para explicar beneficios."
            )
            send_whatsapp_message(ADVISOR_NUMBER, f"💳 Interesado en VRIM · {phone_number}")
            return jsonify({"status": "ok"})

        # Fallback a menú
        send_main_menu(phone_number)
        return jsonify({"status": "ok"})

    except Exception as e:
        logging.exception(f"❌ Error en webhook POST: {e}")
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------
# HEALTHCHECK
# ---------------------------------------------------------------
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "Vicky Bot Inbursa"}), 200


# ---------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    logging.info(f"🚀 Iniciando Vicky Bot en puerto {port}")
    app.run(host="0.0.0.0", port=port)
