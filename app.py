import os
import json
import re
import logging
import openai
import gspread
from flask import Flask, request, jsonify
from oauth2client.service_account import ServiceAccountCredentials

# -----------------------------
# App & logging
# -----------------------------
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# -----------------------------
# Variables de entorno
# -----------------------------
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON", "")
SHEET_NAME = os.getenv("SHEET_NAME", "Prospectos SECOM Auto")
WORKSHEET_NAME = os.getenv("WORKSHEET_NAME", "")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "vicky-verify-token")
BRAND_NAME = os.getenv("BRAND_NAME", "Christian López")

# -----------------------------
# Utilidades
# -----------------------------
def normalize_number(raw):
    digits = re.sub(r"\D", "", str(raw or ""))
    return digits[-10:] if len(digits) >= 10 else digits

def get_gspread_client():
    if not GOOGLE_CREDENTIALS_JSON.strip():
        raise RuntimeError("Falta GOOGLE_CREDENTIALS_JSON")
    info = json.loads(GOOGLE_CREDENTIALS_JSON)
    scope = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(info, scope)
    return gspread.authorize(creds)

def open_ws():
    gc = get_gspread_client()
    sh = gc.open(SHEET_NAME)
    return sh.worksheet(WORKSHEET_NAME) if WORKSHEET_NAME else sh.sheet1

def find_contact(ws, last10):
    rows = ws.get_all_values()
    if not rows:
        return None

    headers = [h.strip().lower() for h in rows[0]]
    phone_idx = name_idx = None

    for i, h in enumerate(headers):
        if any(k in h for k in ["whatsapp", "tel", "telefono", "número", "numero", "celular"]):
            phone_idx = i
        if any(k in h for k in ["nombre", "cliente", "name"]):
            name_idx = i

    for r in rows[1:]:
        candidate = (
            normalize_number(r[phone_idx]) if phone_idx is not None and phone_idx < len(r)
            else normalize_number(" ".join(r))
        )
        if candidate == last10:
            nombre = None
            if name_idx is not None and name_idx < len(r):
                nombre = (r[name_idx] or "").strip() or None
            if not nombre:
                nombre = r[0].strip() if r and r[0] else "Cliente"
            return {"nombre": nombre}
    return None

def menu_general():
    return (
        "¡Hola! Soy *Vicky*, asistente de *{brand}*.\n\n"
        "Puedo ayudarte con:\n"
        "1) Asesoría en pensiones IMSS\n"
        "2) Seguros de auto (Amplia PLUS, Amplia, Limitada)\n"
        "3) Seguros de vida y salud\n"
        "4) Tarjetas médicas VRIM\n"
        "5) Préstamos a pensionados IMSS\n"
        "6) Financiamiento y nómina empresarial\n\n"
        "Escribe el número de opción para continuar."
    ).format(brand=BRAND_NAME)

def menu_personalizado(nombre):
    return (
        f"¡Hola *{nombre}*! Soy *Vicky*, asistente de *{BRAND_NAME}*.\n\n"
        "Veo tu interés en *Seguros de Auto*. Puedo ofrecerte hasta *60% de descuento* "
        "y el beneficio es transferible a familiares en tu mismo domicilio.\n\n"
        "Opciones:\n"
        "1) Cotizar seguro de auto\n"
        "2) Ver requisitos (INE y tarjeta de circulación o número de placa)\n"
        f"3) Hablar con {BRAND_NAME}\n\n"
        "Escribe la opción para continuar."
    )

def respuesta_para(numero_last10):
    try:
        ws = open_ws()
        hit = find_contact(ws, numero_last10)
        return menu_personalizado(hit["nombre"]) if hit else menu_general()
    except Exception:
        logging.exception("Error consultando Google Sheets")
        return "Soy *Vicky*. No pude consultar la base, te muestro el menú general:\n\n" + menu_general()

# -----------------------------
# Endpoints
# -----------------------------
@app.route("/", methods=["GET"])
def root():
    return "Bot Vicky activo 🚀"

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "service": "vicky", "phase": "1"})

@app.route("/webhook", methods=["GET"])
def verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        logging.info("✅ Webhook verificado")
        return challenge, 200
    logging.warning("❌ Verificación fallida")
    return "Forbidden", 403

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        incoming = request.get_json(force=True)
        entry = incoming.get("entry", [])[0]
        changes = entry.get("changes", [])[0]
        value = changes.get("value", {})
        messages = value.get("messages", [])

        if messages:
            msg = messages[0]
            frm = msg.get("from")
            body = msg.get("text", {}).get("body", "") or msg.get("button", {}).get("text", "")
            last10 = normalize_number(frm)
            reply = respuesta_para(last10)

            return jsonify({
                "messaging_product": "whatsapp",
                "to": frm,
                "type": "text",
                "text": {
                    "body": reply
                }
            }), 200

    except Exception as e:
        logging.exception("❌ Error en webhook")
        return "error", 500

    return jsonify({"status": "ignored"}), 200

# -----------------------------
# Main (solo local)
# -----------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    # Para desarrollo local (Render usará gunicorn con -b 0.0.0.0:$PORT)
    port = int(os.getenv("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
