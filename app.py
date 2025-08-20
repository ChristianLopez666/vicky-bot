# app.py - Vicky Bot (Fase 1) WhatsApp Cloud API + Flask + Google Sheets
import os
import json
import re
import datetime as dt
from flask import Flask, request
import requests
import gspread
from google.oauth2.service_account import Credentials

# ---------- Config ----------
META_TOKEN = os.getenv("META_TOKEN")  # token con permisos whatsapp_business_messaging y whatsapp_business_management
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")  # p. ej. 712597741555047
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "vicky-verify-2025")

# Google Sheets
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON", "")
GSHEET_PROSPECTS_ID = os.getenv("GSHEET_PROSPECTS_ID", "")
GSHEET_SOLICITUDES_ID = os.getenv("GSHEET_SOLICITUDES_ID", "")
GSHEET_PROSPECTS_TITLE = os.getenv("GSHEET_PROSPECTS_TITLE", "Prospectos SECOM Auto")
GSHEET_SOLICITUDES_TITLE = os.getenv("GSHEET_SOLICITUDES_TITLE", "Solicitudes Vicky")
GSHEET_PROSPECTS_WORKSHEET = os.getenv("GSHEET_PROSPECTS_WORKSHEET", "")  # opcional
GSHEET_SOLICITUDES_WORKSHEET = os.getenv("GSHEET_SOLICITUDES_WORKSHEET", "")  # opcional

# Notificación al asesor
ADVISOR_NOTIFY_NUMBER = os.getenv("ADVISOR_NOTIFY_NUMBER", "5216682478005")

app = Flask(__name__)

# ---------- Utilidades ----------
def normalize_digits(value: str) -> str:
    if value is None:
        return ""
    numbers = re.sub(r"\D+", "", str(value))
    return numbers[-10:] if len(numbers) >= 10 else numbers

def build_menu(name_hint=None, matched=False):
    saludo = f"Hola {name_hint} 👋" if name_hint else "Hola 👋"
    intro = "Soy Vicky. Estoy aquí para ayudarte. "
    intro += "¡Te tengo identificado! ✔️" if matched else "Por ahora te atenderé con nuestro menú general."
    menu = (
        f"{saludo}\n{intro}\n\n"
        "Menú principal:\n"
        "1) Asesoría en Pensiones IMSS (Ley 73)\n"
        "2) Seguro de Auto (Amplia PLUS / Amplia / Limitada)\n"
        "3) Seguros de Vida y Salud\n"
        "4) Tarjetas Médicas VRIM\n"
        "5) Préstamos a Pensionados IMSS ($10,000 a $650,000)\n"
        "6) Financiamiento Empresarial (incluye financiamiento para tus clientes)\n"
        "7) Nómina Empresarial\n"
        "8) Contactar con Christian\n\n"
        "👉 Responde con el número de opción."
    )
    return menu

def send_whatsapp_text(to_number: str, message: str):
    if not META_TOKEN or not PHONE_NUMBER_ID:
        print("[WARN] META_TOKEN o PHONE_NUMBER_ID no configurados.")
        return
    url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {META_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp","to": to_number,"type": "text","text": {"body": message}}
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=30)
        if r.status_code >= 400:
            print("[ERROR] WhatsApp:", r.status_code, r.text)
    except Exception as e:
        print("[EXC] WhatsApp:", e)

def get_gspread_client():
    if not GOOGLE_CREDENTIALS_JSON:
        raise RuntimeError("Falta GOOGLE_CREDENTIALS_JSON")
    try:
        sa_info = json.loads(GOOGLE_CREDENTIALS_JSON)
    except Exception as e:
        raise RuntimeError(f"GOOGLE_CREDENTIALS_JSON no es JSON válido: {e}")
    scopes = ["https://www.googleapis.com/auth/spreadsheets","https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(sa_info, scopes=scopes)
    return gspread.authorize(creds)

def open_spreadsheet(gc, spreadsheet_id: str, title_fallback: str):
    if spreadsheet_id:
        return gc.open_by_key(spreadsheet_id)
    return gc.open(title_fallback)

def open_worksheet(sh, preferred_title: str):
    if preferred_title:
        return sh.worksheet(preferred_title)
    return sh.get_worksheet(0)

def find_client_by_phone(gc, last10: str):
    sh = open_spreadsheet(gc, GSHEET_PROSPECTS_ID, GSHEET_PROSPECTS_TITLE)
    ws = open_worksheet(sh, GSHEET_PROSPECTS_WORKSHEET)
    records = ws.get_all_records()
    phone_headers = {"whatsapp","telefono","teléfono","celular","phone"}
    name_headers  = {"nombre","name","cliente"}

    for row in records:
        candidate_phone = None
        for key, val in row.items():
            if key.strip().lower() in phone_headers:
                candidate_phone = normalize_digits(val)
                break
        if candidate_phone is None:
            for val in row.values():
                cand = normalize_digits(val)
                if len(cand) >= 10:
                    candidate_phone = cand
                    break
        if candidate_phone and candidate_phone.endswith(last10):
            nombre = None
            for key, val in row.items():
                if key.strip().lower() in name_headers:
                    nombre = str(val).strip()
                    break
            return {"nombre": nombre, "matched": True, "row": row}
    return {"nombre": None, "matched": False, "row": None}

def log_solicitud(gc, wa_id: str, texto: str, opcion: str, nombre_detectado: str = None):
    try:
        sh = open_spreadsheet(gc, GSHEET_SOLICITUDES_ID, GSHEET_SOLICITUDES_TITLE)
        ws = open_worksheet(sh, GSHEET_SOLICITUDES_WORKSHEET)
        now = dt.datetime.now()
        ws.append_row([now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S"), wa_id, nombre_detectado or "", opcion, texto])
    except Exception as e:
        print("[WARN] No se pudo registrar en Solicitudes:", e)

# ---------- Rutas ----------
@app.get("/")
def health():
    return "ok", 200

@app.get("/webhook")
def verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    return "Error de verificación", 403

@app.post("/webhook")
def inbound():
    data = request.get_json(silent=True, force=True) or {}
    try:
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                messages = value.get("messages", [])
                contacts = value.get("contacts", [])
                if not messages:
                    continue

                msg = messages[0]
                wa_id = contacts[0].get("wa_id") if contacts else None
                from_number = msg.get("from") or wa_id
                text = ""
                msg_type = msg.get("type")

                if msg_type == "text":
                    text = msg.get("text", {}).get("body", "").strip()
                elif msg_type == "button":
                    text = msg.get("button", {}).get("text", "").strip()
                elif msg_type == "interactive":
                    interactive = msg.get("interactive", {})
                    if interactive.get("type") == "list_reply":
                        text = interactive.get("list_reply", {}).get("title", "").strip()
                    elif interactive.get("type") == "button_reply":
                        text = interactive.get("button_reply", {}).get("title", "").strip()

                if not from_number:
                    continue

                # Buscar en Sheets
                gc = None
                nombre = None
                matched = False
                try:
                    gc = get_gspread_client()
                    last10 = normalize_digits(from_number)
                    found = find_client_by_phone(gc, last10)
                    nombre = found.get("nombre")
                    matched = found.get("matched", False)
                except Exception as e:
                    print("[WARN] Sheets no disponible:", e)

                # Ruteo por opciones
                option = re.sub(r"\D+", "", text) if text else ""
                if option in {"1","2","3","4","5","6","7","8"}:
                    if gc:
                        log_solicitud(gc, from_number, text, option, nombre)
                    if option == "1":
                        send_whatsapp_text(from_number,
                            "✔️ Pensiones IMSS (Ley 73)\n"
                            "Para comenzar, compárteme: CURP, NSS y fecha de nacimiento. "
                            "Con eso preparo un diagnóstico inicial.")
                    elif option == "2":
                        send_whatsapp_text(from_number,
                            "✔️ Seguro de Auto\n"
                            "Envía foto de tu INE y tarjeta de circulación o tu número de placas. "
                            "Te cotizo en Amplia PLUS, Amplia y Limitada.")
                    elif option == "3":
                        send_whatsapp_text(from_number,
                            "✔️ Seguros de Vida y Salud\n"
                            "Indícame tu edad, ocupación y si buscas protección individual o familiar.")
                    elif option == "4":
                        send_whatsapp_text(from_number,
                            "✔️ Tarjetas Médicas VRIM\n"
                            "Te envío la información y beneficios. ¿Para cuántas personas la requieres?")
                    elif option == "5":
                        send_whatsapp_text(from_number,
                            "✔️ Préstamos a Pensionados IMSS ($10,000 a $650,000)\n"
                            "Indícame tu edad, monto aproximado y pensión neta mensual. "
                            "La propuesta es tentativa y depende de capacidad de pago.")
                    elif option == "6":
                        send_whatsapp_text(from_number,
                            "✔️ Financiamiento Empresarial\n"
                            "¿Buscas crédito para tu negocio o financiamiento para tus clientes? "
                            "Cuéntame el monto aproximado y el uso.")
                    elif option == "7":
                        send_whatsapp_text(from_number,
                            "✔️ Nómina Empresarial\n"
                            "Puedo detallar beneficios y requisitos. ¿Cuántos colaboradores manejas?")
                    elif option == "8":
                        send_whatsapp_text(from_number,
                            "Listo, notificaré a Christian para que te contacte. 🙌")
                        if ADVISOR_NOTIFY_NUMBER:
                            name_txt = f"{nombre}" if nombre else "No identificado"
                            send_whatsapp_text(ADVISOR_NOTIFY_NUMBER,
                                f"🔔 *Contacto solicitado*\nCliente: {name_txt}\nWhatsApp: {from_number}")
                else:
                    benefits = ""
                    if matched:
                        benefits = ("\n\nBeneficio especial para *Seguro de Auto*: "
                                    "hasta *60% de descuento* ✔️. Transferible a familiares "
                                    "en el mismo domicilio.")
                    send_whatsapp_text(from_number, build_menu(name_hint=nombre, matched=matched) + benefits)

        return "EVENT_RECEIVED", 200
    except Exception as e:
        print("[EXC] inbound:", e)
        return "error", 200

if __name__ == "__main__":
    # Para desarrollo local (Render usará gunicorn con -b 0.0.0.0:$PORT)
    port = int(os.getenv("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
