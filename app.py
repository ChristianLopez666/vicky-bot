# ============================================================
# VICKY BOT SECOM/WAPI — app.py FINAL
# Arquitectura unificada FASE 3
# SECOM + WAPI + GPT + RAG + DRIVE + Worker SECOM
# ============================================================

import os
import json
import time
import base64
import logging
import threading
from datetime import datetime
from flask import Flask, request, jsonify

import requests
import pytz

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

import openai


# ============================================================
# CONFIG: FLASK
# ============================================================
app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False


# ============================================================
# LOAD ENV VARIABLES (RENDER)
# ============================================================

META_TOKEN = os.getenv("META_TOKEN")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
WABA_PHONE_ID = os.getenv("WABA_PHONE_ID")
WA_API_VERSION = os.getenv("WA_API_VERSION", "v20.0")

ADVISOR_NUMBER = os.getenv("ADVISOR_NUMBER")
ADVISOR_WHATSAPP = os.getenv("ADVISOR_WHATSAPP")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GPT_MODEL = os.getenv("GPT_MODEL", "gpt-4o-mini")

GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")

SHEET_ID_SECOM = os.getenv("SHEET_ID_SECOM")
SHEET_TITLE_SECOM = os.getenv("SHEET_TITLE_SECOM")

GSHEET_PROSPECTS_ID = os.getenv("GSHEET_PROSPECTS_ID")
GSHEET_SOLICITUDES_ID = os.getenv("GSHEET_SOLICITUDES_ID")
SHEETS_TITLE_LEADS = os.getenv("SHEETS_TITLE_LEADS", "Hoja1")

DRIVE_FOLDER_ID = os.getenv("DRIVE_FOLDER_ID")
ID_MANUAL_IMSS = os.getenv("ID_MANUAL_IMSS")

TZ = os.getenv("TZ", "America/Mazatlan")


# ============================================================
# TIMEZONE
# ============================================================
tz = pytz.timezone(TZ)


# ============================================================
# LOGGING
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logging.info("🔥 Vicky SECOM/WAPI — FASE 3 iniciando…")


# ============================================================
# GOOGLE CREDS (JSON embebido como string)
# ============================================================
try:
    google_creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
    GOOGLE_CREDS = Credentials.from_service_account_info(
        google_creds_dict,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]
    )
    logging.info("✔ Credenciales Google cargadas correctamente.")
except Exception as e:
    logging.error(f"❌ Error cargando credenciales Google: {e}")
    GOOGLE_CREDS = None


# ============================================================
# HELPER: ENVIAR MENSAJE SIMPLE A WHATSAPP
# ============================================================
def send_message(to: str, text: str):
    """
    Enviar un mensaje de texto simples usando la API oficial de Meta.
    """
    try:
        url = f"https://graph.facebook.com/{WA_API_VERSION}/{WABA_PHONE_ID}/messages"

        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": text}
        }

        headers = {
            "Authorization": f"Bearer {META_TOKEN}",
            "Content-Type": "application/json"
        }

        r = requests.post(url, json=payload, headers=headers)
        if r.status_code in [200, 201]:
            logging.info(f"✔ Mensaje enviado a {to}")
            return True
        else:
            logging.error(f"❌ Error enviando mensaje: {r.text}")
            return False

    except Exception as e:
        logging.error(f"❌ Excepción en send_message: {e}")
        return False
# ============================================================
# BLOQUE 2 — HELPERS UNIVERSALES + META HELPERS
# ============================================================

# -------------------------------
# Helper: Normalizar número
# -------------------------------
def normalize_number(num: str) -> str:
    """
    Asegura que el número está en formato internacional 521XXXXXXXXXX
    y elimina espacios o caracteres no válidos.
    """
    n = "".join([c for c in num if c.isdigit()])
    if n.startswith("52"):
        return n
    if len(n) == 10:
        return "521" + n
    return n


# -------------------------------
# Helper: Fecha/hora con TZ
# -------------------------------
def now_str():
    return datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")


# -------------------------------
# Helper: Rate limit (Básico)
# -------------------------------
last_request_time = 0

def respect_rate_limit(min_seconds=1):
    """
    Evita hacer requests consecutivos que puedan gatillar rate limit de Meta.
    Tu worker SECOM usa 60 segundos, aquí solo aplicamos un mínimo de seguridad.
    """
    global last_request_time
    now = time.time()

    if now - last_request_time < min_seconds:
        time.sleep(min_seconds - (now - last_request_time))

    last_request_time = time.time()


# -------------------------------
# Helper: Enviar plantilla de WhatsApp
# -------------------------------
def send_template_message(to: str, template_name: str, components: list):
    """
    Enviar una plantilla aprobada por Meta.
    """
    try:
        respect_rate_limit()

        url = f"https://graph.facebook.com/{WA_API_VERSION}/{WABA_PHONE_ID}/messages"

        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": "es_MX"},
                "components": components
            }
        }

        headers = {
            "Authorization": f"Bearer {META_TOKEN}",
            "Content-Type": "application/json"
        }

        r = requests.post(url, json=payload, headers=headers)
        resp = r.json()

        if r.status_code in [200, 201]:
            logging.info(f"✔ Plantilla '{template_name}' enviada a {to}")
            return True

        logging.error(f"❌ Error plantilla {template_name} → {resp}")
        return False

    except Exception as e:
        logging.error(f"❌ Excepción en send_template_message: {e}")
        return False


# -------------------------------
# Helper: Descargar archivo desde Meta
# -------------------------------
def download_media(media_id: str):
    """
    Descarga archivo desde Meta (imagen/PDF/audio/video)
    y regresa bytes + MIME type.
    """
    try:
        # 1) Obtener URL temporal
        url = f"https://graph.facebook.com/{WA_API_VERSION}/{media_id}"
        headers = {"Authorization": f"Bearer {META_TOKEN}"}
        r = requests.get(url, headers=headers)

        if r.status_code != 200:
            logging.error(f"❌ Error obteniendo media URL: {r.text}")
            return None, None

        media_url = r.json().get("url")

        # 2) Descargar contenido binario
        r2 = requests.get(media_url, headers=headers)

        if r2.status_code != 200:
            logging.error(f"❌ Error descargando media: {r2.text}")
            return None, None

        return r2.content, r2.headers.get("Content-Type")

    except Exception as e:
        logging.error(f"❌ Excepción en download_media: {e}")
        return None, None

# -------------------------------
# Helper: Respuesta segura
# -------------------------------
def safe_reply(text: str) -> str:
    """
    Garantiza que nunca se responda con None o vacío.
    """
    if not text or text.strip() == "":
        return "Estoy leyendo tu mensaje, dame un momento por favor."
    return text
# ============================================================
# BLOQUE 3 — GOOGLE SHEETS CLIENT (Lectura, Escritura, Matching)
# ============================================================

def get_sheets_service():
    """
    Devuelve el cliente de Google Sheets.
    """
    try:
        service = build("sheets", "v4", credentials=GOOGLE_CREDS)
        return service.spreadsheets()
    except Exception as e:
        logging.error(f"❌ Error creando cliente de Google Sheets: {e}")
        return None


# -------------------------------
# Sanitizar encabezados
# -------------------------------
def clean_header(h):
    if not h:
        return ""
    h = h.strip().lower()
    h = h.replace(" ", "_")
    h = h.replace("á", "a").replace("é", "e").replace("í", "i").replace("ó", "o").replace("ú", "u")
    return h


# -------------------------------
# Leer una hoja completa
# -------------------------------
def read_sheet(sheet_id: str, sheet_title: str):
    """
    Regresa (rows, header)
    """
    try:
        service = get_sheets_service()
        if not service:
            return [], []

        range_name = f"{sheet_title}!A1:Z9999"
        result = service.values().get(
            spreadsheetId=sheet_id,
            range=range_name
        ).execute()

        values = result.get("values", [])
        if not values:
            return [], []

        header = [clean_header(h) for h in values[0]]
        rows = values[1:]

        return rows, header

    except Exception as e:
        logging.error(f"❌ Error leyendo hoja {sheet_title}: {e}")
        return [], []


# -------------------------------
# Update row (escritura en SECOM)
# -------------------------------
def update_secom_row(row_number: int, data: dict):
    """
    Escribe diccionario en una fila específica de SECOM.
    row_number es 1-based en Sheets.
    """
    try:
        values = [[data.get(k, "") for k in data.keys()]]
        range_name = f"{SHEET_TITLE_SECOM}!A{row_number}:Z{row_number}"

        body = {"values": values}

        service = get_sheets_service()
        service.values().update(
            spreadsheetId=SHEET_ID_SECOM,
            range=range_name,
            valueInputOption="RAW",
            body=body
        ).execute()

        logging.info(f"✔ Fila SECOM actualizada: {row_number}")

    except Exception as e:
        logging.error(f"❌ Error actualizando fila SECOM {row_number}: {e}")


# -------------------------------
# Convertir una fila a diccionario
# -------------------------------
def row_to_dict(row, header):
    d = {}
    for i, h in enumerate(header):
        d[h] = row[i] if i < len(row) else ""
    return d


# -------------------------------
# Matching híbrido SECOM
# -------------------------------
def match_secom_number(phone: str):
    """
    Coincidencia híbrida:
    1. Match exacto del número completo
    2. Match por últimos 10 dígitos
    """
    try:
        rows, header = read_sheet(SHEET_ID_SECOM, SHEET_TITLE_SECOM)
        if not rows:
            return None, None, None

        phone = normalize_number(phone)
        last10 = phone[-10:]

        exact_match = None
        partial_match = None

        for idx, row in enumerate(rows):
            data = row_to_dict(row, header)
            ws = data.get("whatsapp", "").strip()

            if not ws:
                continue

            ws_norm = normalize_number(ws)

            # 1. Exact match
            if ws_norm == phone:
                exact_match = (idx, data, ws_norm)
                break

            # 2. Last 10 match si no hay exacto
            if ws_norm.endswith(last10):
                partial_match = (idx, data, ws_norm)

        return exact_match or partial_match or (None, None, None)

    except Exception as e:
        logging.error(f"❌ Error en match_secom_number: {e}")
        return None, None, None

# -------------------------------
# Lectores adicionales (Prospectos / Solicitudes)
# -------------------------------
def read_prospects_sheet():
    return read_sheet(GSHEET_PROSPECTS_ID, SHEETS_TITLE_LEADS)


def read_solicitudes_sheet():
    return read_sheet(GSHEET_SOLICITUDES_ID, SHEETS_TITLE_LEADS)
# ============================================================
# BLOQUE 4 — GPT ENGINE (Identidad + Respuesta Inteligente)
# ============================================================

# Inicializar cliente OpenAI
openai.api_key = OPENAI_API_KEY


# -------------------------------
# Identidad oficial de Vicky
# -------------------------------
VICKY_IDENTITY = """
Eres Vicky, asistente inteligente de Christian López (asesor financiero en México).
Tu trabajo es atender clientes con calidez, claridad y profesionalismo.

Reglas de comportamiento:
- Responde siempre con empatía y tono humano.
- Sé breve si la pregunta es simple. Sé detallada si el cliente lo requiere.
- No uses tecnicismos innecesarios.
- Mantén atención al contexto financiero (seguros, préstamos, TPV, autos).
- Evita sonar robótica.
- No exageres ni prometas cosas que no puedas cumplir.
- No digas 'soy un modelo de lenguaje'. Tú eres Vicky, punto.
- Si el usuario es un prospecto SECOM, ofrece apoyo inmediato y personalizado.
- Si el usuario pide información técnica del IMSS o seguros → activa RAG cuando esté disponible.
- No compartas información sensible.
- Mantén siempre una actitud de servicio y resolución.

Objetivo:
Ayudar, guiar y convertir prospectos en clientes de forma natural y profesional.
"""


# -------------------------------
# GPT Reply Engine
# -------------------------------
def gpt_reply(user_input: str, context: str = "") -> str:
    """
    Genera una respuesta inteligente basada en:
    - identidad de Vicky
    - mensaje del usuario
    - contexto opcional
    Si GPT falla, regresa un fallback seguro.
    """
    try:
        prompt = f"""
        {VICKY_IDENTITY}

        Contexto:
        {context}

        Mensaje del usuario:
        {user_input}

        Responde como Vicky.
        """

        completion = openai.chat.completions.create(
            model=GPT_MODEL,
            messages=[
                {"role": "system", "content": VICKY_IDENTITY},
                {"role": "user", "content": prompt}
            ],
            temperature=0.55,
            max_tokens=350
        )

        reply = completion.choices[0].message["content"]
        if reply:
            return reply.strip()

        return "Estoy procesando tu mensaje, dame un momento por favor."

    except Exception as e:
        logging.error(f"❌ Error en GPT: {e}")
        return "Estoy analizando tu mensaje, dame un momento por favor."
# ============================================================
# BLOQUE 5 — PROCESADOR PRINCIPAL DEL MENSAJE
# ============================================================

# -------------------------------
# Guardar archivos en Google Drive
# -------------------------------
def save_file_to_drive(content: bytes, mime: str, filename: str):
    try:
        service = build("drive", "v3", credentials=GOOGLE_CREDS)

        file_metadata = {
            "name": filename,
            "parents": [DRIVE_FOLDER_ID]
        }

        media = googleapiclient.http.MediaInMemoryUpload(content, mimetype=mime)

        f = service.files().create(
            body=file_metadata,
            media_body=media,
            fields="id"
        ).execute()

        logging.info(f"📁 Archivo guardado en Drive: {filename}")
        return f.get("id")

    except Exception as e:
        logging.error(f"❌ Error guardando archivo en Drive: {e}")
        return None


# -------------------------------
# Procesar mensaje principal
# -------------------------------
def process_user_message(phone: str, message: str, media_id=None) -> str:
    """
    Este es el núcleo de Vicky SECOM/WAPI.
    Decide cómo responder según:
    - tipo de mensaje
    - coincidencia SECOM
    - flujo comercial
    - RAG
    - GPT
    """

    phone_norm = normalize_number(phone)
    logging.info(f"📥 Mensaje de {phone_norm}: {message}")

    # ---------------------------------------------------------
    # 1. Si envía archivo → Guardar en Drive
    # ---------------------------------------------------------
    if media_id:
        content, mime = download_media(media_id)
        if content and mime:
            ext = mime.split("/")[-1]
            filename = f"file_{phone_norm}_{int(time.time())}.{ext}"
            save_file_to_drive(content, mime, filename)

        return "📄 Archivo recibido correctamente, gracias. ¿En qué más te puedo apoyar?"

    # ---------------------------------------------------------
    # 2. Matching SECOM híbrido
    # ---------------------------------------------------------
    idx, data, ws = match_secom_number(phone_norm)

    secom_context = None
    es_cliente_secom = False

    if idx is not None and data:
        es_cliente_secom = True
        secom_context = f"Eres un prospecto en SECOM. Datos: {data}"
    else:
        secom_context = "Usuario nuevo. No está en SECOM."

    # ---------------------------------------------------------
    # 3. Menú automático SIEMPRE (tu selección 2A)
    # ---------------------------------------------------------
    menu_text = (
        "Hola 👋 Soy *Vicky*, asistente de Christian.\n"
        "Aquí tienes las opciones disponibles:\n\n"
        "1️⃣ Seguro de Auto\n"
        "2️⃣ Seguro de Vida\n"
        "3️⃣ Préstamos IMSS\n"
        "4️⃣ Terminal Punto de Venta (TPV)\n"
        "5️⃣ Tarjeta Médica VRIM\n"
        "6️⃣ Créditos Empresariales\n"
        "7️⃣ Contactar con Christian\n\n"
        "¿En qué puedo ayudarte?"
    )

    # Caso especial: saludo o inicio
    if message.lower() in ["hola", "buenos días", "buenas tardes", "menu", "hey", "buenas", "inicio"]:
        return menu_text

    # ---------------------------------------------------------
    # 4. RAG automático si detecta temas técnicos
    # ---------------------------------------------------------
    temas_rag = ["imss", "modalidad", "semanas", "ley 73", "prestamo", "requisitos", "beneficios"]

    if any(t in message.lower() for t in temas_rag):
        try:
            rag_response = rag_manual_search(message)
            if rag_response:
                return rag_response
        except:
            pass

    # ---------------------------------------------------------
    # 5. Si es prospecto SECOM → priorizar atención
    # ---------------------------------------------------------
    if es_cliente_secom:
        # GPT con contexto SECOM
        return gpt_reply(message, context=secom_context)

    # ---------------------------------------------------------
    # 6. Cliente nuevo → GPT con contexto general
    # ---------------------------------------------------------
    return gpt_reply(message, context="Usuario nuevo sin coincidencia en SECOM.")
# ============================================================
# BLOQUE 6 — WORKER SECOM (ENVÍO MASIVO)
# ============================================================

def load_secom_sheet():
    """
    Carga la hoja SECOM y regresa:
    (rows, header, raw_values)
    """
    rows, header = read_sheet(SHEET_ID_SECOM, SHEET_TITLE_SECOM)
    return rows, header


# ------------------------------------------------------------
# Filtro de prospectos válidos
# ------------------------------------------------------------
def filter_valid_secom_prospects(rows):
    """
    Reglas:
    - Debe tener WhatsApp
    - Estado distinto de 'NO_INTERESADO' y 'CERRADO'
    - Si tiene FirstSentAt, NO reenviar
    """
    valid = []

    for idx, row in enumerate(rows):
        data = row_to_dict(row, ["whatsapp", "nombre", "estado", "intentoss", "firstsentat", "plantilla", "promo"])

        phone = data.get("whatsapp", "").strip()
        if not phone:
            continue

        estado = data.get("estado", "").upper()

        if estado in ["NO_INTERESADO", "CERRADO"]:
            continue

        # No volver a enviar si ya tiene FirstSentAt
        first_sent = data.get("firstsentat", "").strip()
        if first_sent:
            continue

        valid.append((idx, data, phone))

    return valid


# ------------------------------------------------------------
# WORKER PRINCIPAL SECOM
# ------------------------------------------------------------
def secom_worker():
    """
    Flujo completo:
    1) Cargar SECOM
    2) Filtrar prospectos válidos
    3) Enviar plantilla 1x1
    4) Respetar rate limit de Meta (seguridad adicional)
    5) Actualizar estado en Google Sheets
    6) Notificar al asesor
    """

    rows, header = load_secom_sheet()
    if not rows:
        logging.warning("⚠ No hay filas en SECOM.")
        return {"ok": False, "msg": "No hay filas SECOM"}

    valid = filter_valid_secom_prospects(rows)
    total = len(valid)
    logging.info(f"🔍 Prospectos válidos: {total}")

    enviados = 0
    fallidos = 0

    for idx, data, phone in valid:

        nombre = data.get("nombre", "").strip()
        plantilla = data.get("plantilla", "").strip()
        promo = data.get("promo", "").strip()

        if not plantilla:
            logging.warning(f"⚠ Sin plantilla en fila {idx+1}")
            continue

        phone_norm = normalize_number(phone)
        now = datetime.utcnow().strftime("%Y-%m-%d")

        # Componentes dinámicos
        components = [
            {
                "type": "body",
                "parameters": [
                    {"type": "text", "text": nombre or "amigo"}
                ]
            }
        ]

        ok = send_template_message(phone_norm, plantilla, components)

        # ------------------------
        # Actualizar fila
        # ------------------------
        updated = {
            "Nombre": nombre,
            "Whatsapp": phone_norm,
            "Estado": "ENVIADO" if ok else "ERROR_ENVIO",
            "Intentos": str(int(data.get("intentoss", "0") or "0") + 1),
            "LastSentAt": now,
            "Plantilla": plantilla,
            "Promo": promo
        }

        # Guardar FirstSentAt solo si fue exitoso
        if ok:
            updated["FirstSentAt"] = now

        update_secom_row(idx + 1, updated)

        if ok:
            enviados += 1
        else:
            fallidos += 1

        # RATE LIMIT real → 60 segundos
        logging.info("⏱️ Esperando 60 segundos para evitar baneo…")
        time.sleep(60)

    # ------------------------
    # Notificar al asesor
    # ------------------------
    try:
        msg = (
            f"🟢 *SECOM Finalizado*\n"
            f"Enviados: {enviados}\n"
            f"Fallidos: {fallidos}"
        )
        send_message(ADVISOR_WHATSAPP, msg)
    except:
        logging.warning("⚠ No se pudo notificar al asesor")

    return {
        "ok": True,
        "enviados": enviados,
        "fallidos": fallidos
    }

# ------------------------------------------------------------
# ENDPOINT: Iniciar Worker SECOM
# ------------------------------------------------------------
@app.post("/ext/send-promo-secom")
def ext_send_promo_secom():
    """
    Lanza el SECOM Worker en background,
    para no bloquear el request.
    """
    threading.Thread(target=secom_worker).start()
    return jsonify({"ok": True, "msg": "Worker SECOM iniciado"})
    # ============================================================
# BLOQUE 7 — WEBHOOK WHATSAPP (VERIFICACIÓN + RECEPCIÓN)
# ============================================================


# ------------------------------------------------------------
# VERIFICACIÓN DEL WEBHOOK (GET)
# ------------------------------------------------------------
@app.get("/webhook")
def verify_webhook():
    """
    Meta verifica el webhook enviando GET.
    Debemos responder con 'hub.challenge' si el token coincide.
    """
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if token == VERIFY_TOKEN:
        return challenge

    return "Token inválido", 403


# ------------------------------------------------------------
# WEBHOOK PRINCIPAL (POST)
# ------------------------------------------------------------
@app.post("/webhook")
def webhook():
    try:
        data = request.get_json(silent=True)

        if not data:
            return jsonify({"status": "no_data"}), 200

        entry = data.get("entry", [])
        if not entry:
            return jsonify({"status": "no_entry"}), 200

        changes = entry[0].get("changes", [])
        if not changes:
            return jsonify({"status": "no_changes"}), 200

        value = changes[0].get("value", {})
        messages = value.get("messages", [])

        if not messages:
            return jsonify({"status": "no_messages"}), 200

        msg = messages[0]
        phone = msg.get("from", "")
        msg_type = msg.get("type", "")

        user_text = ""
        media_id = None

        # ----------------------------------------------------
        # 1. Texto
        # ----------------------------------------------------
        if msg_type == "text":
            user_text = msg["text"]["body"]

        # ----------------------------------------------------
        # 2. Multimedia (imagen, documento, audio)
        # ----------------------------------------------------
        elif msg_type in ["image", "document", "audio", "video"]:
            media = msg.get(msg_type, {})
            media_id = media.get("id")
            mime = media.get("mime_type", "")
            user_text = f"Archivo recibido ({msg_type})."

        # ----------------------------------------------------
        # 3. Botones y listas de Meta
        # ----------------------------------------------------
        elif msg_type == "interactive":
            inter = msg.get("interactive", {})
            if "button_reply" in inter:
                user_text = inter["button_reply"]["title"]
            elif "list_reply" in inter:
                user_text = inter["list_reply"]["title"]

        # ----------------------------------------------------
        # 4. No reconocido
        # ----------------------------------------------------
        else:
            user_text = "No pude procesar tu mensaje, ¿podrías repetirlo?"

        # ----------------------------------------------------
        # 5. PROCESAR MENSAJE (BLOQUE 5)
        # ----------------------------------------------------
        reply = process_user_message(
            phone=phone,
            message=user_text,
            media_id=media_id
        )

        # Enviar la respuesta
        send_message(phone, reply)

        return jsonify({"status": "ok"}), 200

    except Exception as e:
        logging.error(f"❌ Error en webhook: {e}")
        return jsonify({"status": "error"}), 200
        # ============================================================
# BLOQUE 8 — ENDPOINTS AUXILIARES /ext/*
# ============================================================


# ------------------------------------------------------------
# /ext/health → usado por Render
# ------------------------------------------------------------
@app.get("/ext/health")
def ext_health():
    """
    Render llama este endpoint para verificar que el servicio
    está activo y responde sin errores.
    """
    return jsonify({"status": "ok"}), 200



# ------------------------------------------------------------
# /ext/test-send → enviar mensaje manual
# ------------------------------------------------------------
@app.get("/ext/test-send")
def ext_test_send():
    """
    Envía un mensaje manual vía URL:
    /ext/test-send?to=5216682478005&msg=Hola
    """
    to = request.args.get("to")
    msg = request.args.get("msg", "Mensaje de prueba ✔️")

    if not to:
        return jsonify({"ok": False, "error": "Falta parámetro 'to'"}), 400

    ok = send_message(to, msg)

    return jsonify({
        "ok": ok,
        "to": to,
        "msg": msg
    }), 200



# ------------------------------------------------------------
# /ext/manuales → Consulta RAG del manual IMSS
# ------------------------------------------------------------
@app.post("/ext/manuales")
def ext_manual_rag():
    """
    Permite consultar manual IMSS Ley 73 vía POST:
    {
        "query": "¿Cuáles son los requisitos?"
    }
    """
    body = request.get_json() or {}
    query = body.get("query", "")

    if not query:
        return jsonify({"ok": False, "msg": "Falta 'query'"}), 400

    try:
        respuesta = rag_manual_search(query)
        return jsonify({"ok": True, "respuesta": respuesta})
    except Exception as e:
        logging.error(f"❌ Error RAG manual: {e}")
        return jsonify({"ok": False, "error": "Error procesando manual"}), 500



# ------------------------------------------------------------
# /ext/drive-files → listar archivos del folder de Drive
# ------------------------------------------------------------
@app.get("/ext/drive-files")
def ext_drive_files():
    """
    Lista archivos de la carpeta principal en Google Drive.
    """
    try:
        service = build("drive", "v3", credentials=GOOGLE_CREDS)
        results = service.files().list(
            q=f"'{DRIVE_FOLDER_ID}' in parents",
            fields="files(id, name, mimeType)"
        ).execute()

        return jsonify({
            "ok": True,
            "files": results.get("files", [])
        })

    except Exception as e:
        logging.error(f"❌ Error listando archivos Drive: {e}")
        return jsonify({"ok": False, "error": "No se pudieron listar archivos"}), 500


