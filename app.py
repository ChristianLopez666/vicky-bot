
import os
import json
import time
import logging
from typing import Tuple, Optional, Dict, Any

import requests
from flask import Flask, request, jsonify, abort
import gspread
from google.oauth2.service_account import Credentials

# -----------------------------
# Configuración & Globals
# -----------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("vicky")

app = Flask(__name__)

PROVIDER = os.getenv("WHATSAPP_PROVIDER", "360dialog").lower().strip()  # "360dialog" | "cloud"
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "VICKY_VERIFY_TOKEN")

# 360dialog
D360_API_KEY = os.getenv("D360_API_KEY", "").strip()
D360_URL = os.getenv("D360_URL", "https://waba.360dialog.io/v1/messages").strip()

# Cloud API (Meta)
META_TOKEN = os.getenv("META_TOKEN", "").strip()
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "").strip()  # requerido si PROVIDER=cloud
META_URL = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages" if PHONE_NUMBER_ID else ""

# Google Sheets
GOOGLE_CREDS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON", "")
SHEET_NAME = os.getenv("SHEET_NAME", "Prospectos SECOM Auto")
REQUESTS_SHEET_NAME = os.getenv("REQUESTS_SHEET_NAME", "Solicitudes Vicky")

# Otros
ADVISOR_NUMBER = os.getenv("ADVISOR_NUMBER", "5216682478005")  # e.g., +52 1 668 247 8005 -> "5216682478005"
NOTIFY_WEBHOOK_URL = os.getenv("NOTIFY_WEBHOOK_URL", "").strip()  # opcional (n8n/Zapier)
MENU_AUTO_DISCOUNT = os.getenv("MENU_AUTO_DISCOUNT", "60")

# Memoria efímera por sesión de servidor (para 24h)
SESSION_STATE: Dict[str, Dict[str, Any]] = {}
LAST_SHEET_REFRESH = 0.0
SHEET_CACHE_TTL = 300  # 5 minutos
CONTACTS_BY_LAST10: Dict[str, Dict[str, str]] = {}

# -----------------------------
# Utilidades
# -----------------------------

def normalize_last10(number: str) -> str:
    """Devuelve los últimos 10 dígitos para empatar contra la hoja."""
    digits = "".join([c for c in number if c.isdigit()])
    return digits[-10:] if len(digits) >= 10 else digits

def to_e164_mx(number: str) -> str:
    """Convierte a E.164 MX (521 + 10 dígitos) para enviar mensajes."""
    last10 = normalize_last10(number)
    return f"521{last10}"  # WhatsApp MX usa 52 + 1 + número

def load_contacts_from_sheet(force: bool = False) -> None:
    """Carga y cachea contactos (número -> {nombre, rfc})."""
    global LAST_SHEET_REFRESH, CONTACTS_BY_LAST10
    now = time.time()
    if not force and (now - LAST_SHEET_REFRESH) < SHEET_CACHE_TTL and CONTACTS_BY_LAST10:
        return

    if not GOOGLE_CREDS_JSON:
        logger.warning("GOOGLE_CREDENTIALS_JSON no configurado; no se cargarán contactos.")
        CONTACTS_BY_LAST10 = {}
        LAST_SHEET_REFRESH = now
        return

    try:
        info = json.loads(GOOGLE_CREDS_JSON)
        scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
        creds = Credentials.from_service_account_info(info, scopes=scopes)
        gc = gspread.authorize(creds)
        sh = gc.open(SHEET_NAME)
        ws = sh.sheet1  # primera pestaña
        rows = ws.get_all_records()  # requiere encabezados en la primera fila
        mapping = {}
        for r in rows:
            # Se aceptan columnas: Nombre, RFC, WhatsApp (flexible, insensible a mayúsculas)
            name = r.get("Nombre") or r.get("nombre") or r.get("NOMBRE") or r.get("Cliente") or r.get("cliente")
            rfc = r.get("RFC") or r.get("rfc") or r.get("Rfc")
            phone = r.get("WhatsApp") or r.get("Whatsapp") or r.get("whatsapp") or r.get("Número") or r.get("Telefono") or r.get("telefono") or r.get("Teléfono")
            if phone:
                last10 = normalize_last10(str(phone))
                mapping[last10] = {"nombre": str(name or "").strip(), "rfc": str(rfc or "").strip()}
        CONTACTS_BY_LAST10 = mapping
        LAST_SHEET_REFRESH = now
        logger.info(f"Contactos cargados: {len(CONTACTS_BY_LAST10)}")
    except Exception as e:
        logger.exception(f"Error cargando Google Sheet: {e}")
        CONTACTS_BY_LAST10 = {}
        LAST_SHEET_REFRESH = now

def append_request_row(kind: str, from_last10: str, payload: Dict[str, Any]) -> None:
    """Escribe la solicitud en la hoja de 'Solicitudes Vicky' (crea pestaña si no existe)."""
    if not GOOGLE_CREDS_JSON:
        logger.warning("GOOGLE_CREDENTIALS_JSON no configurado; no se registrará la solicitud.")
        return
    try:
        info = json.loads(GOOGLE_CREDS_JSON)
        scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
        creds = Credentials.from_service_account_info(info, scopes=scopes)
        gc = gspread.authorize(creds)
        sh = gc.open(SHEET_NAME)
        try:
            ws = sh.worksheet(REQUESTS_SHEET_NAME)
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title=REQUESTS_SHEET_NAME, rows=1000, cols=20)
            ws.append_row(["timestamp", "tipo", "numero_last10", "nombre", "rfc", "datos_json"])
        contacto = CONTACTS_BY_LAST10.get(from_last10, {})
        ws.append_row([
            time.strftime("%Y-%m-%d %H:%M:%S"),
            kind,
            from_last10,
            contacto.get("nombre", ""),
            contacto.get("rfc", ""),
            json.dumps(payload, ensure_ascii=False)
        ])
    except Exception as e:
        logger.exception(f"Error registrando solicitud en Google Sheet: {e}")

def get_contact_by_number(incoming_number: str) -> Dict[str, str]:
    load_contacts_from_sheet()
    return CONTACTS_BY_LAST10.get(normalize_last10(incoming_number), {})

def ensure_session(from_last10: str) -> Dict[str, Any]:
    if from_last10 not in SESSION_STATE:
        SESSION_STATE[from_last10] = {"stage": "menu", "data": {}}
    return SESSION_STATE[from_last10]

# -----------------------------
# Envío de mensajes
# -----------------------------

def send_whatsapp_text(to_number: str, text: str) -> Tuple[bool, Optional[str]]:
    """
    Envía un mensaje de texto por WhatsApp usando el proveedor configurado.
    - to_number se acepta en cualquier formato; se normaliza a E.164 MX.
    """
    e164 = to_e164_mx(to_number)
    try:
        if PROVIDER == "cloud":
            if not META_TOKEN or not META_URL:
                raise RuntimeError("Falta META_TOKEN o PHONE_NUMBER_ID")
            payload = {
                "messaging_product": "whatsapp",
                "to": e164,
                "type": "text",
                "text": {"body": text},
            }
            headers = {"Authorization": f"Bearer {META_TOKEN}", "Content-Type": "application/json"}
            r = requests.post(META_URL, headers=headers, json=payload, timeout=15)
        else:
            if not D360_API_KEY:
                raise RuntimeError("Falta D360_API_KEY")
            payload = {"to": e164, "type": "text", "text": {"body": text}}
            headers = {"D360-API-KEY": D360_API_KEY, "Content-Type": "application/json"}
            url = D360_URL or "https://waba.360dialog.io/v1/messages"
            r = requests.post(url, headers=headers, json=payload, timeout=15)

        ok = 200 <= r.status_code < 300
        if not ok:
            logger.error("Fallo al enviar WhatsApp: %s %s", r.status_code, r.text[:500])
        return ok, (r.text if r is not None else None)
    except Exception as e:
        logger.exception(f"Excepción enviando WhatsApp: {e}")
        return False, str(e)

def notify_advisor(message: str) -> None:
    """Notifica al asesor. Preferencia: WhatsApp; alternativa: webhook externo."""
    # 1) Webhook externo (si está disponible)
    if NOTIFY_WEBHOOK_URL:
        try:
            _ = requests.post(NOTIFY_WEBHOOK_URL, json={"message": message}, timeout=10)
        except Exception as e:
            logger.warning(f"No se pudo notificar via NOTIFY_WEBHOOK_URL: {e}")
    # 2) WhatsApp directo (requiere plantilla aprobada si fuera de ventana de 24h).
    if ADVISOR_NUMBER:
        send_whatsapp_text(ADVISOR_NUMBER, message)

# -----------------------------
# Mensajería Vicky
# -----------------------------

def build_menu_text(contact: Dict[str, str]) -> str:
    nombre = contact.get("nombre", "").strip()
    saludo_nombre = f"{nombre}, " if nombre else ""
    extra_benef = ""
    if nombre:
        extra_benef = f"\n• Beneficio exclusivo: hasta {MENU_AUTO_DISCOUNT}% de descuento en tu seguro de auto si contratas este mes. ✔️"
    return (
f"¡Hola {saludo_nombre}soy Vicky! 🤖 Estoy para ayudarte con *seguros y financiamiento*.\n"
"Elige una opción (responde con el número):\n\n"
"1) Asesoría en *pensiones IMSS*\n"
"2) *Seguro de auto* (Amplia PLUS / Amplia / Limitada)\n"
"3) *Seguros de vida y salud*\n"
"4) *Tarjetas médicas VRIM*\n"
"5) *Préstamos a pensionados IMSS* ($10,000 a $650,000)\n"
"6) *Financiamiento empresarial* (incluye “financiamiento para tus clientes”)\n"
"7) *Nómina empresarial*\n"
"8) *Contactar con Christian*\n"
f"{extra_benef}\n\n"
"Escribe *MENÚ* en cualquier momento para volver aquí.")

def handle_menu_choice(from_last10: str, text_body: str, contact: Dict[str, str]) -> str:
    t = text_body.strip().lower()
    session = ensure_session(from_last10)
    if t in ("1", "pensiones", "pension", "pensiones imss"):
        session["stage"] = "pensiones"
        return ("Perfecto. Para asesoría en *pensiones IMSS* necesito:\n"
                "• Año de alta al IMSS\n• Semanas cotizadas aproximadas\n• Últimos 5 salarios\n• Si planeas *Modalidad 40* y con qué salario\n\n"
                "Compárteme esos datos o escribe *MENÚ* para regresar. ✔️")
    if t in ("2", "auto", "seguro de auto"):
        session["stage"] = "auto"
        return ("¡Vamos a cotizar tu *seguro de auto*! 🚗\n"
                "Envíame *foto de tu INE* y *tarjeta de circulación*, o escríbeme tu *número de placa*. "
                "Indícame si quieres *Amplia PLUS*, *Amplia* o *Limitada*. ✔️")
    if t in ("3", "vida", "salud", "seguros de vida", "seguros de salud"):
        session["stage"] = "vida_salud"
        return ("Excelente. Para *vida y salud*, dime:\n"
                "• Edad\n• Suma asegurada deseada\n• Si quieres proteger a tu familia o crédito\n\n"
                "Con eso te envío opciones personalizadas. ✔️")
    if t in ("4", "vrim", "tarjetas medicas", "tarjetas médicas"):
        session["stage"] = "vrim"
        return ("Las *tarjetas médicas VRIM* ofrecen atención privada con costos preferentes y red nacional. "
                "Dime cuántos integrantes cubrir (tú, pareja, hijos) y tu ciudad para ver *clínicas cercanas*. ✔️")
    if t in ("5", "prestamo", "préstamo", "prestamos", "préstamos"):
        session["stage"] = "prestamo_monto"
        session["data"] = {}
        return ("Perfecto. *Préstamos a pensionados IMSS* 💵\n"
                "1/2 Escribe el *monto* que necesitas (ej. 120000).")
    if t in ("6", "financiamiento", "empresarial", "financiamiento empresarial"):
        session["stage"] = "financiamiento"
        return ("Para *financiamiento empresarial*, cuéntame:\n"
                "• Uso del crédito (capital de trabajo, equipo, stock)\n• Ingresos mensuales\n• Antigüedad del negocio\n\n"
                "Con eso preparo opciones y requisitos. ✔️")
    if t in ("7", "nomina", "nómina", "nómina empresarial"):
        session["stage"] = "nomina"
        return ("La *nómina empresarial* de Inbursa incluye dispersión, beneficios y soporte. "
                "¿Para cuántos empleados y cuándo quisieras implementarla? ✔️")
    if t in ("8", "contacto", "contactar", "christian"):
        session["stage"] = "contacto"
        return ("Listo, notifico a *Christian López* para que te contacte en breve. ✔️")
    # Cualquier otra cosa, re-enviar menú
    return build_menu_text(contact)

def try_parse_int(text_body: str) -> Optional[int]:
    digits = "".join([c for c in text_body if c.isdigit()])
    return int(digits) if digits else None

def continue_flow(from_last10: str, text_body: str, contact: Dict[str, str]) -> str:
    session = ensure_session(from_last10)
    stage = session.get("stage", "menu")

    if text_body.strip().lower() in ("menu", "menú"):
        session["stage"] = "menu"
        session["data"] = {}
        return build_menu_text(contact)

    if stage == "prestamo_monto":
        monto = try_parse_int(text_body)
        if not monto or monto < 10000 or monto > 650000:
            return "Indícame un *monto* entre $10,000 y $650,000 (solo números)."
        session["data"]["monto"] = monto
        session["stage"] = "prestamo_plazo"
        return "2/2 ¿A cuántos *meses*? (12, 24, 36, 48 o 60)."

    if stage == "prestamo_plazo":
        plazo = try_parse_int(text_body)
        if plazo not in (12, 24, 36, 48, 60):
            return "Escribe un *plazo* válido: 12, 24, 36, 48 o 60."
        session["data"]["plazo"] = plazo
        # Registrar solicitud
        data = {"monto": session["data"]["monto"], "plazo": plazo}
        append_request_row("prestamo_pensionado", from_last10, data)
        # Notificar asesor
        nombre = contact.get("nombre") or "Cliente"
        notify_advisor(f"📣 Solicitud de préstamo: {nombre} ({from_last10}). Monto: ${data['monto']:,} Plazo: {plazo} meses.")
        session["stage"] = "menu"
        session["data"] = {}
        return ("¡Gracias! Generaré una *propuesta aproximada* con base en tu monto y plazo. "
                "En breve te contacto con el detalle. Mientras tanto, escribe *MENÚ* si quieres ver otras opciones. ✔️")

    if stage in ("pensiones", "auto", "vida_salud", "vrim", "financiamiento", "nomina", "contacto"):
        # Registrar textos/libres en hoja para darle seguimiento
        append_request_row(stage, from_last10, {"mensaje": text_body})
        if stage == "contacto":
            nombre = contact.get("nombre") or "Cliente"
            notify_advisor(f"📣 Contacto solicitado por {nombre} ({from_last10}). Mensaje: {text_body[:140]}")
            session["stage"] = "menu"
            return "¡Listo! Christian te contactará muy pronto. ¿Deseas ver el *MENÚ*?"
        return "¡Gracias! Tomo nota. ¿Deseas volver al *MENÚ*?"

    # Default: reenviar menú
    session["stage"] = "menu"
    return build_menu_text(contact)

# -----------------------------
# Webhooks
# -----------------------------

def parse_incoming(payload: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """Intenta extraer (from, text) desde estructuras de Cloud API o 360dialog."""
    # Cloud API (Meta)
    try:
        entry = payload.get("entry", [])[0]
        change = entry.get("changes", [])[0]
        value = change.get("value", {})
        messages = value.get("messages", [])
        if messages:
            msg = messages[0]
            from_number = msg.get("from")
            text = ""
            if "text" in msg and msg["text"].get("body"):
                text = msg["text"]["body"]
            elif "button" in msg and msg["button"].get("text"):
                text = msg["button"]["text"]
            elif "interactive" in msg:
                inter = msg["interactive"]
                if "list_reply" in inter and inter["list_reply"].get("title"):
                    text = inter["list_reply"]["title"]
                elif "button_reply" in inter and inter["button_reply"].get("title"):
                    text = inter["button_reply"]["title"]
            if from_number and text is not None:
                return from_number, text
    except Exception:
        pass

    # 360dialog (on-prem hosted)
    try:
        messages = payload.get("messages", [])
        if messages:
            msg = messages[0]
            from_number = msg.get("from")
            text = ""
            if "text" in msg and isinstance(msg["text"], dict):
                text = msg["text"].get("body", "")
            elif "button" in msg and isinstance(msg["button"], dict):
                text = msg["button"].get("text", "")
            elif "interactive" in msg and isinstance(msg["interactive"], dict):
                inter = msg["interactive"]
                if "list_reply" in inter and isinstance(inter["list_reply"], dict):
                    text = inter["list_reply"].get("title", "")
                elif "button_reply" in inter and isinstance(inter["button_reply"], dict):
                    text = inter["button_reply"].get("title", "")
            if from_number and text is not None:
                return from_number, text
    except Exception:
        pass

    return None, None

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "ts": time.time()})

@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    return "verification failed", 403

@app.route("/webhook", methods=["POST"])
def receive_webhook():
    payload = request.get_json(force=True, silent=True) or {}
    logger.info("Webhook in: %s", str(payload)[:1000])

    from_number, text_body = parse_incoming(payload)
    if not from_number:
        return jsonify({"status": "ignored"}), 200

    from_last10 = normalize_last10(from_number)
    contact = get_contact_by_number(from_number)

    session = ensure_session(from_last10)
    # Si es el primer mensaje o cualquier palabra, mostrará el menú automáticamente (requisito del usuario)
    if session.get("stage") == "menu":
        # Si envía un número 1-8, se procesa como opción. De lo contrario, se muestra el menú.
        if text_body and text_body.strip().lower() in set(["1","2","3","4","5","6","7","8","pensiones","pension","pensiones imss","auto","seguro de auto","vida","salud","seguros de vida","seguros de salud","vrim","tarjetas medicas","tarjetas médicas","prestamo","préstamo","prestamos","préstamos","financiamiento","empresarial","financiamiento empresarial","nomina","nómina","nómina empresarial","contacto","contactar","christian"]):
            reply = handle_menu_choice(from_last10, text_body, contact)
        else:
            reply = build_menu_text(contact)
    else:
        reply = continue_flow(from_last10, text_body, contact)

    ok, _ = send_whatsapp_text(from_number, reply)
    if not ok:
        logger.error("No se pudo enviar respuesta a %s", from_number)
    return jsonify({"status": "ok"}), 200

# -----------------------------
# Arranque local
# -----------------------------

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
