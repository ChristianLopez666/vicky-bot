import json
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

# === 🆕 BLOQUE 1: utilidades de medios (reenviar fotos/documentos y transcribir audios) ===
def _get_media_url(media_id: str) -> str | None:
    try:
        resp = requests.get(
            f"https://graph.facebook.com/v21.0/{media_id}",
            headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"},
            timeout=8,
        )
        if resp.status_code == 200:
            return resp.json().get("url")
        logging.warning(f"[WA media url] {resp.status_code}: {resp.text[:180]}")
    except Exception as e:
        logging.error(f"[WA media url] error: {e}")
    return None

def _download_media_bytes(url: str) -> bytes | None:
    try:
        r = requests.get(url, headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"}, timeout=12)
        if r.status_code == 200:
            return r.content
        logging.warning(f"[WA media dl] {r.status_code}: {r.text[:180]}")
    except Exception as e:
        logging.error(f"[WA media dl] error: {e}")
    return None

def send_media_image(to: str, media_id: str, caption: str = ""):
    url = f"https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "image",
        "image": {"id": media_id, **({"caption": caption} if caption else {})}
    }
    resp = requests.post(url, headers=headers, json=payload)
    logging.info(f"[WA send image] {resp.status_code} - {resp.text[:180]}")

def send_media_document(to: str, media_id: str, caption: str = ""):
    url = f"https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "document",
        "document": {"id": media_id, **({"caption": caption} if caption else {})}
    }
    resp = requests.post(url, headers=headers, json=payload)
    logging.info(f"[WA send doc] {resp.status_code} - {resp.text[:180]}")

def transcribe_audio_media(media_id: str) -> str | None:
    """
    Descarga el audio de WhatsApp y lo transcribe con Whisper si hay OPENAI_API_KEY.
    """
    if not OPENAI_API_KEY:
        return None
    url = _get_media_url(media_id)
    if not url:
        return None
    blob = _download_media_bytes(url)
    if not blob:
        return None

    files = {
        "file": ("audio.ogg", blob, "audio/ogg"),
    }
    data = {"model": "whisper-1", "response_format": "text", "language": "es"}
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
    try:
        r = requests.post("https://api.openai.com/v1/audio/transcriptions",
                          headers=headers, data=data, files=files, timeout=30)
        if r.status_code == 200:
            return r.text.strip()
        logging.warning(f"[Whisper] {r.status_code}: {r.text[:200]}")
    except Exception as e:
        logging.error(f"[Whisper] error: {e}")
    return None
# === FIN BLOQUE 1 ===

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

            # === 🆕 BLOQUE 2: manejo de medios antes de filtrar por 'text' ===
            if msg_type == "image":
                media_id = (message.get("image") or {}).get("id")
                caption = (message.get("image") or {}).get("caption", "") or ""
                try:
                    if ADVISOR_NUMBER and ADVISOR_NUMBER != sender and media_id:
                        send_media_image(
                            ADVISOR_NUMBER,
                            media_id,
                            caption=f"📎 Imagen recibida de {profile_name or sender}. {('Nota: ' + caption) if caption else ''}"
                        )
                except Exception as e:
                    logging.error(f"❌ Error reenviando imagen: {e}")

                send_message(sender, "✅ ¡Gracias! Recibí la imagen. Si es para **seguro de auto**, con INE y tarjeta de circulación (o placa) ya puedo cotizar. ¿Deseas que avance?")
                continue

            if msg_type == "document":
                media_id = (message.get("document") or {}).get("id")
                filename = (message.get("document") or {}).get("filename", "")
                try:
                    if ADVISOR_NUMBER and ADVISOR_NUMBER != sender and media_id:
                        send_media_document(
                            ADVISOR_NUMBER,
                            media_id,
                            caption=f"📄 Documento recibido de {profile_name or sender} {f'({filename})' if filename else ''}"
                        )
                except Exception as e:
                    logging.error(f"❌ Error reenviando documento: {e}")

                send_message(sender, "✅ ¡Gracias! Recibí tu documento. En breve lo reviso.")
                continue

            if msg_type == "audio" or (msg_type == "voice"):
                media_id = (message.get("audio") or {}).get("id")
                transcript = transcribe_audio_media(media_id) if media_id else None
                if transcript:
                    send_message(sender, f"🗣️ Transcripción: {transcript}")
                else:
                    send_message(sender, "No pude transcribir tu nota de voz. ¿Podrías intentar de nuevo o escribir el mensaje?")
                continue
            # === FIN BLOQUE 2 ===

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

            # ⚠️ Nuevo: si cliente ya seleccionó opción y ahora dice "gracias" o algo breve, se responderá con GPT si hay contexto
            has_last_intent = sender in LAST_INTENT and LAST_INTENT[sender].get("opt") != "gpt"
            is_short_phrase = any(ch.isalpha() for ch in text_norm) and len(text_norm.split()) <= 3
            is_natural_query = (
                (not is_numeric_option)
                and (not is_menu)
                and any(ch.isalpha() for ch in text_norm)
                and (len(text_norm.split()) >= 3 or has_last_intent)
            )

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
                        if ADVISOR_NUMBER:
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

# >>> VX: CONFIG & UTILS (NO TOCAR)
try:
    vx_get_env
except NameError:
    def vx_get_env(name, default=None):
        import os
        return os.getenv(name, default)

try:
    vx_normalize_phone
except NameError:
    def vx_normalize_phone(raw):
        import re
        if not raw:
            return ""
        phone = re.sub(r"[^\d]", "", str(raw))
        phone = re.sub(r"^(52|521)", "", phone)
        return phone[-10:] if len(phone) >= 10 else phone

try:
    vx_last10
except NameError:
    def vx_last10(phone):
        return vx_normalize_phone(phone)

try:
    vx_Settings
except NameError:
    class vx_Settings:
        def __init__(self):
            self.META_TOKEN = vx_get_env("META_TOKEN")
            self.WABA_PHONE_ID = vx_get_env("WABA_PHONE_ID")
            self.VERIFY_TOKEN = vx_get_env("VERIFY_TOKEN")
            self.OPENAI_API_KEY = vx_get_env("OPENAI_API_KEY")
            self.REDIS_URL = vx_get_env("REDIS_URL")
            self.GOOGLE_CREDENTIALS_JSON = vx_get_env("GOOGLE_CREDENTIALS_JSON")
            self.SHEETS_ID_LEADS = vx_get_env("SHEETS_ID_LEADS")
            self.SHEETS_TITLE_LEADS = vx_get_env("SHEETS_TITLE_LEADS")
            self.ADVISOR_WHATSAPP = vx_get_env("ADVISOR_WHATSAPP")

# >>> VX: LOGGING (NO TOCAR)
try:
    vx_setup_logging
except NameError:
    def vx_setup_logging():
        import logging
        logger = logging.getLogger()
        if not logger.hasHandlers():
            logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        return logging.getLogger("vx")

# >>> VX: REDIS (NO TOCAR, OPCIONAL)
try:
    vx_get_redis
except NameError:
    def vx_get_redis():
        try:
            url = vx_get_env("REDIS_URL")
            if not url:
                return None
            import redis
            return redis.from_url(url)
        except Exception:
            return None

# >>> VX: WHATSAPP CLIENT (NO TOCAR)
try:
    vx_wa_send_text
except NameError:
    def vx_wa_send_text(to_e164: str, body: str):
        import requests, logging
        token = vx_get_env("META_TOKEN")
        phone_id = vx_get_env("WABA_PHONE_ID")
        if not token or not phone_id or not to_e164:
            logging.getLogger("vx").warning("vx_wa_send_text: falta config")
            return False
        url = f"https://graph.facebook.com/v20.0/{phone_id}/messages"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        payload = {
            "messaging_product": "whatsapp",
            "to": to_e164,
            "type": "text",
            "text": {"body": body}
        }
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=9)
            logging.getLogger("vx").info(f"vx_wa_send_text: {resp.status_code} {resp.text[:160]}")
            return resp.status_code == 200
        except Exception as e:
            logging.getLogger("vx").error(f"vx_wa_send_text error: {e}")
            return False

try:
    vx_wa_mark_read
except NameError:
    def vx_wa_mark_read(message_id: str):
        import requests, logging
        token = vx_get_env("META_TOKEN")
        phone_id = vx_get_env("WABA_PHONE_ID")
        if not token or not phone_id or not message_id:
            logging.getLogger("vx").warning("vx_wa_mark_read: falta config")
            return False
        url = f"https://graph.facebook.com/v20.0/{phone_id}/messages"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        payload = {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": message_id
        }
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=9)
            logging.getLogger("vx").info(f"vx_wa_mark_read: {resp.status_code} {resp.text[:120]}")
            return resp.status_code == 200
        except Exception as e:
            logging.getLogger("vx").error(f"vx_wa_mark_read error: {e}")
            return False

# >>> VX: GPT (NO TOCAR)
try:
    vx_gpt_reply
except NameError:
    def vx_gpt_reply(user_text: str, system_text: str = None) -> str:
        import logging
        api_key = vx_get_env("OPENAI_API_KEY")
        if not api_key:
            return "No tengo IA disponible en este momento. Por favor elige una opción del menú."
        try:
            import openai
            client = openai.OpenAI(api_key=api_key)
            system = system_text or (
                "Eres Vicky, asistente de Christian López. Responde en español, breve, clara y orientada al siguiente paso."
            )
            completion = client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_text},
                ],
                max_tokens=120,
                temperature=0.2,
            )
            return completion.choices[0].message.content.strip()
        except Exception as e:
            logging.getLogger("vx").error(f"vx_gpt_reply error: {e}")
            return "No tengo IA disponible en este momento. Por favor elige una opción del menú."

# >>> VX: SHEETS (NO TOCAR)
try:
    vx_sheet_find_by_phone
except NameError:
    def vx_sheet_find_by_phone(last10: str):
        import logging
        import json
        try:
            creds_json = vx_get_env("GOOGLE_CREDENTIALS_JSON")
            sheets_id = vx_get_env("SHEETS_ID_LEADS")
            sheets_title = vx_get_env("SHEETS_TITLE_LEADS")
            if not creds_json or not sheets_id or not sheets_title or not last10:
                return None
            import gspread
            from gspread import service_account_from_dict
            creds_dict = json.loads(creds_json)
            client = service_account_from_dict(creds_dict)
            sheet = client.open_by_key(sheets_id)
            ws = sheet.worksheet(sheets_title)
            rows = ws.get_all_records()
            for row in rows:
                wa = str(row.get("WhatsApp", ""))
                if vx_last10(wa) == last10:
                    return row
            return None
        except Exception as e:
            logging.getLogger("vx").error(f"vx_sheet_find_by_phone error: {e}")
            return None

# >>> VX: MENU BUILDER (NO TOCAR)
try:
    vx_menu_text
except NameError:
    def vx_menu_text(customer_name: str = None) -> str:
        base = (
            "Hola, soy Vicky, asistente de Christian López. Estoy aquí para ayudarte.\n\n"
            "1) Asesoría en pensiones IMSS\n"
            "2) Seguro de auto (Amplia PLUS, Amplia, Limitada) — solicita INE y tarjeta de circulación o número de placa\n"
            "3) Seguros de vida y salud\n"
            "4) Tarjetas médicas VRIM\n"
            "5) Préstamos a pensionados IMSS ($10,000 a $650,000)\n"
            "6) Financiamiento empresarial (incluye financiamiento para tus clientes)\n"
            "7) Nómina empresarial\n"
            "8) Contactar con Christian (te notifico para que te atienda)\n\n"
            "¿En qué te ayudo?"
        )
        if customer_name:
            return f"Hola {customer_name}, " + base[5:]
        return base

# >>> VX: ROUTES /ext (NO TOCAR)
try:
    vx_ext_routes_registered
except NameError:
    vx_ext_routes_registered = True
    from flask import request, jsonify

    @app.get("/ext/health")
    def vx_ext_health():
        return jsonify({"status": "ok"})

    @app.get("/ext/webhook")
    def vx_ext_webhook_get():
        import logging
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        verify_token = vx_get_env("VERIFY_TOKEN")
        if mode == "subscribe" and token == verify_token:
            logging.getLogger("vx").info("vx_ext_webhook: verificado OK")
            return challenge or "OK", 200
        else:
            logging.getLogger("vx").warning("vx_ext_webhook: verificación fallida")
            return "Verification failed", 403

    @app.post("/ext/webhook")
    def vx_ext_webhook_post():
        import logging, json
        try:
            payload = request.get_json(force=True, silent=True)
            if not payload:
                return jsonify({"status": "ignored"}), 200
            entry = payload.get("entry", [{}])[0]
            changes = entry.get("changes", [{}])
            if not changes or "value" not in changes[0]:
                return jsonify({"status": "ignored"}), 200
            value = changes[0]["value"]
            msgs = value.get("messages", [])
            if not msgs:
                return jsonify({"status": "ignored"}), 200
            msg = msgs[0]
            from_number = msg.get("from")
            message_id = msg.get("id")
            body = ""
            if msg.get("type") == "text":
                body = msg.get("text", {}).get("body", "") or ""
            else:
                body = ""
            last10 = vx_last10(from_number)
            customer = None
            sheet_row = None
            if last10:
                sheet_row = vx_sheet_find_by_phone(last10)
                if sheet_row and "Nombre" in sheet_row:
                    customer = str(sheet_row["Nombre"])
            menu_text = vx_menu_text(customer)
            vx_wa_send_text(from_number, menu_text)
            if message_id:
                vx_wa_mark_read(message_id)
            return jsonify({"status": "ok"}), 200
        except Exception as e:
            logging.getLogger("vx").error(f"vx_ext_webhook_post error: {e}")
            return jsonify({"status": "ok"}), 200

    @app.post("/ext/test-send")
    def vx_ext_test_send():
        import logging
        try:
            data = request.get_json(force=True, silent=True)
            to = data.get("to")
            text = data.get("text")
            ok = vx_wa_send_text(to, text)
            return jsonify({"ok": ok}), 200
        except Exception as e:
            logging.getLogger("vx").error(f"vx_ext_test_send error: {e}")
            return jsonify({"ok": False, "error": str(e)}), 200


    @app.post("/ext/send-promo")
    def vx_ext_send_promo():
        import logging
        try:
            data = request.get_json(force=True, silent=True) or {}
            to = data.get("to")
            text = data.get("text")

            if not to or not text:
                return jsonify({"ok": False, "error": "Faltan parámetros"}), 400

            # Permitir lista o string único
            if isinstance(to, str):
                targets = [to]
            elif isinstance(to, list):
                targets = to
            else:
                return jsonify({"ok": False, "error": "Formato inválido en 'to'"}), 400

            results = {}
            for phone in targets:
                ok = vx_wa_send_text(str(phone), text)
                results[str(phone)] = "ok" if ok else "fail"

            return jsonify({"ok": True, "msg": "Envío en proceso", "results": results}), 200
        except Exception as e:
            logging.getLogger("vx").error(f"vx_ext_send_promo error: {e}")
            return jsonify({"ok": False, "error": str(e)}), 500
            # ============================================================
# 📌 Endpoint auxiliar para probar conexión con SECOM (Google Sheets)
# ============================================================

@app.route("/ext/test-secom", methods=["GET"])
def test_secom():
    """
    Endpoint de prueba para validar conexión con Google Sheets y matching de número.
    Uso: https://vicky-bot-x6wt.onrender.com/ext/test-secom?phone=5216681234567
    """
    try:
        from google.oauth2.service_account import Credentials
        import gspread

        # Cargar credenciales de Google desde variable de entorno
        google_creds = os.getenv("GOOGLE_CREDENTIALS_JSON")
        if not google_creds:
            return jsonify({"error": "GOOGLE_CREDENTIALS_JSON no configurado"}), 500

        creds = Credentials.from_service_account_info(json.loads(google_creds))
        client = gspread.authorize(creds)

        # Abrir hoja
        sheet_id = os.getenv("SHEETS_ID_LEADS")
        sheet_title = os.getenv("SHEETS_TITLE_LEADS")
        if not sheet_id or not sheet_title:
            return jsonify({"error": "Variables de entorno SECOM faltantes"}), 500

        sheet = client.open_by_key(sheet_id).worksheet(sheet_title)

        # Obtener número de la query
        phone = request.args.get("phone")
        if not phone:
            return jsonify({"error": "Debes enviar ?phone=NUMERO"}), 400

        phone_last10 = phone[-10:]

        # Buscar coincidencia en hoja
        records = sheet.get_all_records()
        for row in records:
            if "WhatsApp" in row and str(row["WhatsApp"])[-10:] == phone_last10:
                return jsonify({
                    "ok": True,
                    "match": {
                        "nombre": row.get("Nombre", ""),
                        "whatsapp": row.get("WhatsApp", ""),
                        "rfc": row.get("RFC", ""),
                        "beneficio": "Hasta 60% de descuento en seguro de auto 🚗"
                    }
                })

        return jsonify({"ok": False, "message": "No se encontró coincidencia"})

    except Exception as e:
        logging.error(f"❌ Error en /ext/test-secom: {e}")
        return jsonify({"error": str(e)}), 500

