import os 
import logging
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# Cargar variables de entorno
load_dotenv()

# Configuración de logging
logging.basicConfig(level=logging.INFO)

# Inicializar Flask
app = Flask(__name__)

# Variables de entorno
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
WHATSAPP_TOKEN = os.getenv("META_TOKEN")  # ✅ Ajustado para Render
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
ADVISOR_NUMBER = os.getenv("ADVISOR_NUMBER", "5216682478005")  # Notificación privada al asesor
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")  # ✅ GPT (opcional)

# 🧠 Controles en memoria
PROCESSED_MESSAGE_IDS = set()
GREETED_USERS = set()
LAST_INTENT = {}   # último intent (para motivo de contacto)
USER_CONTEXT = {}  # estado por usuario {wa_id: {"ctx": str, "ts": float}}

# --------- GPT fallback robusto (opcional) ----------
def gpt_reply(user_text: str) -> str | None:
    """
    Devuelve respuesta breve usando GPT si OPENAI_API_KEY existe.
    1) Intenta /v1/responses (model gpt-4o-mini)
    2) Si falla, intenta /v1/chat/completions
    Timeout a 9s para reducir timeouts.
    Si hay 429 (cuota), devuelve mensaje amable.
    """
    if not OPENAI_API_KEY:
        return None

    system_prompt = (
        "Eres Vicky, asistente de Christian López (asesor financiero de Inbursa). "
        "Responde en español, breve, clara y orientada al siguiente paso. "
        "Si faltan datos para cotizar, pide solo lo necesario. "
        "Evita cifras inventadas. Si preguntan por opciones, sugiere escribir 'menu'."
    )

    # Header base + (opcional) proyecto si está definido en env
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json"
    }
    try:
        project_id = os.getenv("OPENAI_PROJECT_ID")
        if project_id:
            headers["OpenAI-Project"] = project_id
    except Exception:
        pass

    # 1) /v1/responses (recomendado)
    try:
        resp = requests.post(
            "https://api.openai.com/v1/responses",
            headers=headers,
            json={
                "model": "gpt-4o-mini",
                "input": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_text}
                ],
                "max_output_tokens": 220,
                "temperature": 0.3,
            },
            timeout=9,
        )
        if resp.status_code == 200:
            data = resp.json()
            out = (data.get("output", [{}])[0].get("content", [{}])[0].get("text")
                   if "output" in data else None)
            if out:
                return out.strip()
            # Compatibilidad por si viniera en 'choices'
            ch = data.get("choices", [{}])[0].get("message", {}).get("content")
            if ch:
                return ch.strip()
        elif resp.status_code == 429:
            logging.warning(f"[GPT responses] 429: {resp.text[:200]}")
            return ("Estoy recibiendo muchas consultas ahora mismo. "
                    "Puedo avanzar con una orientación breve: si deseas **seguro de vida y salud**, "
                    "te preparo una cotización personalizada; compárteme *edad*, *ciudad* y si buscas "
                    "*temporal* o *vitalicio*. Escribe 'menu' para ver más opciones.")
        else:
            logging.warning(f"[GPT responses] {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        logging.error(f"[GPT responses] error: {e}")

    # 2) /v1/chat/completions (compatibilidad)
    try:
        resp = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers=headers,
            json={
                "model": "gpt-4o-mini",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_text},
                ],
                "max_tokens": 220,
                "temperature": 0.3,
            },
            timeout=9,
        )
        if resp.status_code == 200:
            data = resp.json()
            msg = data.get("choices", [{}])[0].get("message", {}).get("content")
            return msg.strip() if msg else None
        elif resp.status_code == 429:
            logging.warning(f"[GPT chat] 429: {resp.text[:200]}")
            return ("En este momento el servicio de IA alcanzó su límite de uso. "
                    "Mientras tanto: para **seguro de vida**, dime *edad*, *ciudad* y si te interesa "
                    "*temporal* o *vitalicio*, y te guío. Escribe 'menu' para ver opciones.")
        else:
            logging.warning(f"[GPT chat] {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        logging.error(f"[GPT chat] error: {e}")

    return None
# ----------------------------------------------------

# Endpoint de verificación
@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        logging.info("Webhook verificado correctamente ✅")
        return challenge, 200
    else:
        logging.warning("Fallo en la verificación del webhook ❌")
        return "Verification failed", 403

# Endpoint para recibir mensajes
@app.route("/webhook", methods=["POST"])
def receive_message():
    data = request.get_json()
    logging.info(f"📩 Mensaje recibido: {data}")

    if not data or "entry" not in data:
        return jsonify({"status": "ignored"}), 200

    from time import time
    now = time()

    global PROCESSED_MESSAGE_IDS, GREETED_USERS, LAST_INTENT, USER_CONTEXT
    if isinstance(PROCESSED_MESSAGE_IDS, set):
        PROCESSED_MESSAGE_IDS = {}
    if isinstance(GREETED_USERS, set):
        GREETED_USERS = {}

    MSG_TTL = 600
    GREET_TTL = 24 * 3600
    CTX_TTL = 4 * 3600

    if len(PROCESSED_MESSAGE_IDS) > 5000:
        PROCESSED_MESSAGE_IDS = {k: v for k, v in PROCESSED_MESSAGE_IDS.items() if now - v < MSG_TTL}
    if len(GREETED_USERS) > 5000:
        GREETED_USERS = {k: v for k, v in GREETED_USERS.items() if now - v < GREET_TTL}
    if len(LAST_INTENT) > 5000:
        LAST_INTENT = {k: v for k, v in LAST_INTENT.items() if now - v.get("ts", now) < GREET_TTL}
    if len(USER_CONTEXT) > 5000:
        USER_CONTEXT = {k: v for k, v in USER_CONTEXT.items() if now - v.get("ts", now) < CTX_TTL}

    MENU_TEXT = (
        "👉 Elige una opción del menú:\n"
        "1) Asesoría en pensiones IMSS (Ley 73 / Modalidad 40 / Modalidad 10)\n"
        "2) Seguros de auto (Amplia PLUS, Amplia, Limitada)\n"
        "3) Seguros de vida y salud\n"
        "4) Tarjetas médicas VRIM\n"
        "5) Préstamos a pensionados IMSS (a partir de $40,000 pesos hasta $650,000)\n"
        "6) Financiamiento empresarial y nómina empresarial\n"
        "7) Contactar con Christian\n"
        "\nEscribe el número de la opción o 'menu' para volver a ver el menú."
    )

    OPTION_RESPONSES = {
        "1": "🧓 Asesoría en pensiones IMSS. Cuéntame tu caso (Ley 73, M40, M10) y te guío paso a paso.",
        "2": "🚗 Seguro de auto. Envíame *foto de tu INE* y *tarjeta de circulación* o tu *número de placa* para cotizar.",
        "3": "🛡️ Seguros de vida y salud. Te preparo una cotización personalizada.",
        "4": "🩺 Tarjetas médicas VRIM. Te comparto información y precios.",
        "5": "💳 Préstamos a pensionados IMSS. Monto *a partir de $40,000* y hasta $650,000. Dime tu pensión aproximada y el monto deseado.",
        "6": "🏢 Financiamiento empresarial y nómina. ¿Qué necesitas: *crédito*, *factoraje* o *nómina*?",
        "7": "📞 ¡Listo! He notificado a Christian para que te contacte y te dé seguimiento."
    }

    OPTION_TITLES = {
        "1": "Asesoría en pensiones IMSS",
        "2": "Seguros de auto",
        "3": "Seguros de vida y salud",
        "4": "Tarjetas médicas VRIM",
        "5": "Préstamos a pensionados IMSS",
        "6": "Financiamiento/nómina empresarial",
        "7": "Contacto con Christian"
    }

    KEYWORD_INTENTS = [
        (("pension", "pensión", "imss", "modalidad 40", "modalidad 10", "ley 73"), "1"),
        (("auto", "seguro de auto", "placa", "tarjeta de circulación", "coche", "carro"), "2"),
        (("vida", "seguro de vida", "salud", "gastos médicos", "planes de seguro"), "3"),
        (("vrim", "tarjeta médica", "membresía médica"), "4"),
        (("préstamo", "prestamo", "pensionado", "préstamo imss", "prestamo imss"), "5"),
        (("financiamiento", "factoraje", "nómina", "nomina", "empresarial", "crédito empresarial", "credito empresarial"), "6"),
        (("contacto", "contactar", "asesor", "christian", "llámame", "quiero hablar"), "7"),
    ]

    def infer_option_from_text(t: str):
        for keywords, opt in KEYWORD_INTENTS:
            if any(k in t for k in keywords):
                return opt
        return None

    # ---- Procesar SOLO el primer mensaje válido por payload ----
    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            val = change.get("value", {})

            if "statuses" in val:
                continue

            messages = val.get("messages", [])
            if not messages:
                continue

            message = messages[0]
            msg_id = message.get("id")
            msg_type = message.get("type")
            sender = message.get("from")
            business_phone = val.get("metadata", {}).get("display_phone_number")

            profile_name = None
            try:
                profile_name = (val.get("contacts", [{}])[0].get("profile", {}) or {}).get("name")
            except Exception:
                profile_name = None

            logging.info(f"🧾 id={msg_id} type={msg_type} from={sender} profile={profile_name}")

            if msg_id:
                last_seen = PROCESSED_MESSAGE_IDS.get(msg_id)
                if last_seen and (now - last_seen) < MSG_TTL:
                    logging.info(f"🔁 Duplicado ignorado: {msg_id}")
                    continue
                PROCESSED_MESSAGE_IDS[msg_id] = now

            if business_phone and sender and sender.endswith(business_phone):
                logging.info("🪞 Echo desde business_phone ignorado")
                continue

            if msg_type != "text":
                logging.info(f"ℹ️ Mensaje no-texto ignorado: {msg_type}")
                continue

            text = message.get("text", {}).get("body", "") or ""
            text_norm = text.strip().lower()
            logging.info(f"✉️ Texto normalizado: {text_norm}")

            # -------- Contexto por usuario (financiamiento) --------
            from time import time as _t
            user_ctx = USER_CONTEXT.get(sender)
            if user_ctx and (now - user_ctx.get("ts", now) < 4 * 3600):
                ctx = user_ctx.get("ctx")
                if ctx == "financiamiento":
                    if any(k in text_norm for k in ("crédito", "credito")):
                        send_message(sender, "🏦 Crédito empresarial: monto y plazo a medida. Compárteme *antigüedad del negocio*, *ingresos aproximados* y *RFC* para iniciar.")
                        LAST_INTENT[sender] = {"opt": "6", "title": "Crédito empresarial", "ts": now}
                        USER_CONTEXT[sender] = {"ctx": "financiamiento", "ts": _t()}
                        continue
                    if "factoraje" in text_norm:
                        send_message(sender, "📄 Factoraje: adelantamos el cobro de tus facturas. Dime *promedio mensual de facturación* y *RFC*.")
                        LAST_INTENT[sender] = {"opt": "6", "title": "Factoraje", "ts": now}
                        USER_CONTEXT[sender] = {"ctx": "financiamiento", "ts": _t()}
                        continue
                    if any(k in text_norm for k in ("nómina", "nomina")):
                        send_message(sender, "👥 Nómina empresarial: dispersión de sueldos y beneficios. ¿Cuántos colaboradores tienes y periodicidad de pago?")
                        LAST_INTENT[sender] = {"opt": "6", "title": "Nómina empresarial", "ts": now}
                        USER_CONTEXT[sender] = {"ctx": "financiamiento", "ts": _t()}
                        continue
            # -------------------------------------------------------

            # ---------- GPT primero para consultas naturales ----------
            is_numeric_option = text_norm in OPTION_RESPONSES
            is_menu = text_norm in ("hola", "menú", "menu")
            is_natural_query = (not is_numeric_option) and (not is_menu) and any(ch.isalpha() for ch in text_norm) and (len(text_norm.split()) >= 3)

            if is_natural_query:
                ai = gpt_reply(text)
                if ai:
                    send_message(sender, ai)
                    LAST_INTENT[sender] = {"opt": "gpt", "title": "Consulta abierta", "ts": now}
                    continue
            # ---------------------------------------------------------

            # Opción 1–7 (o inferida por keywords)
            option = text_norm if is_numeric_option else infer_option_from_text(text_norm)
            if option:
                send_message(sender, OPTION_RESPONSES[option])
                LAST_INTENT[sender] = {"opt": option, "title": OPTION_TITLES.get(option), "ts": now}
                if option == "6":
                    USER_CONTEXT[sender] = {"ctx": "financiamiento", "ts": now}
                if option == "7":
                    motive = LAST_INTENT.get(sender, {}).get("title") or "No especificado"
                    notify_text = (
                        "🔔 *Vicky Bot – Solicitud de contacto*\n"
                        f"- Nombre: {profile_name or 'No disponible'}\n"
                        f"- WhatsApp del cliente: {sender}\n"
                        f"- Motivo: {motive}\n"
                        f"- Mensaje original: \"{text.strip()}\""
                    )
                    try:
                        if ADVISOR_NUMBER and ADVISOR_NUMBER != sender:
                            send_message(ADVISOR_NUMBER, notify_text)
                            logging.info(f"📨 Notificación privada enviada al asesor {ADVISOR_NUMBER}")
                    except Exception as e:
                        logging.error(f"❌ Error notificando al asesor: {e}")
                continue

            # Saludos/menú
            first_greet_ts = GREETED_USERS.get(sender)
            if not first_greet_ts or (now - first_greet_ts) >= GREET_TTL:
                if is_menu:
                    send_message(
                        sender,
                        "👋 Hola, soy Vicky, asistente de Christian López. Estoy aquí para ayudarte.\n\n" + MENU_TEXT
                    )
                else:
                    send_message(sender, MENU_TEXT)
                GREETED_USERS[sender] = now
                continue

            if is_menu:
                send_message(sender, MENU_TEXT)
                continue

            # Fallback final
            logging.info("📌 Mensaje recibido (ya saludado). Respuesta guía.")
            send_message(sender, "No te entendí. Escribe 'menu' para ver opciones o elige un número del 1 al 7.")

    return jsonify({"status": "ok"}), 200

# Función para enviar mensajes
def send_message(to, text):
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

    response = requests.post(url, headers=headers, json=payload)
    logging.info(f"Respuesta de WhatsApp API: {response.status_code} - {response.text}")

# Endpoint de salud
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

