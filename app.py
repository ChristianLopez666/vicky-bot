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

# 🧠 CAMBIO MÍNIMO: sets en memoria para controlar duplicados y saludo único
PROCESSED_MESSAGE_IDS = set()
GREETED_USERS = set()

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

    if "entry" in data:
        for entry in data["entry"]:
            if "changes" in entry:
                for change in entry["changes"]:
                    if "value" in change and "messages" in change["value"]:
                        for message in change["value"]["messages"]:
                            # 🧠 CAMBIO MÍNIMO: evitar reprocesar el mismo mensaje
                            msg_id = message.get("id")
                            if msg_id in PROCESSED_MESSAGE_IDS:
                                logging.info(f"🔁 Duplicado ignorado: {msg_id}")
                                continue
                            PROCESSED_MESSAGE_IDS.add(msg_id)
                            if len(PROCESSED_MESSAGE_IDS) > 5000:
                                PROCESSED_MESSAGE_IDS.clear()

                            if message.get("type") == "text":
                                sender = message["from"]
                                text = message["text"]["body"].strip().lower()
                                logging.info(f"Mensaje de {sender}: {text}")

                                # 🧠 CAMBIO MÍNIMO: saludar solo la primera vez
                                if sender not in GREETED_USERS:
                                    send_message(
                                        sender,
                                        "👋 Hola, soy Vicky, asistente de Christian López. Estoy aquí para ayudarte.\n\n👉 Elige una opción del menú:"
                                    )
                                    GREETED_USERS.add(sender)
                                else:
                                    # Si el usuario pide menú nuevamente, no repetir saludo
                                    if text in ["menu", "menú", "hola"]:
                                        send_message(
                                            sender,
                                            "👉 Elige una opción del menú:"
                                        )
                                    else:
                                        logging.info("📌 Mensaje recibido (sin saludo repetido).")
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

# ========= AUTO INSURANCE FLOW (seguro de auto) PEGADO AQUÍ =========
AUTO_STATE = {}  # {sender: {"step": str, "data": {"images": int, "placa": str}}}
# steps: None | "auto_collecting"

def handle_auto_insurance_flow(sender, message, text_norm):
    """
    Flujo mínimo:
    - Intención (seguro auto, carro, vehículo) -> pedir INE + tarjeta, o factura, o placa.
    - Si recibe 2 imágenes -> asumimos INE+tarjeta.
    - Si recibe 1 imagen + la palabra 'factura' -> suficiente.
    - Si recibe una placa válida -> suficiente.
    - Al completar, notifica al asesor y confirma al cliente.
    """
    st = AUTO_STATE.get(sender, {"step": None, "data": {"images": 0}})
    t = (text_norm or "").lower().strip()

    # disparadores de intención
    intent_auto = any(k in t for k in ["seguro de auto", "seguro de carro", "seguro para mi carro", "seguro de vehículo", "seguro de vehiculo", "auto", "carro"])

    # 1) inicio del flujo
    if intent_auto and st["step"] is None:
        st["step"] = "auto_collecting"
        st["data"] = {"images": 0}
        AUTO_STATE[sender] = st
        send_message(
            sender,
            "🚗 Para cotizar tu *seguro de auto*, envíame **una** de estas opciones:\n"
            "• 📸 *Foto de INE* **y** 📸 *foto de tarjeta de circulación*,\n"
            "• 📸 *Foto de la factura* del vehículo, **o**\n"
            "• 🔤 *Número de placa* (ej.: ABC123A / VXY1234 / XYZ-12-34).\n\n"
            "Con cualquiera de estas opciones puedo avanzar. 👍"
        )
        return True

    # 2) si el flujo está activo, procesar imágenes/documentos/placa
    if st["step"] == "auto_collecting":
        # a) mensaje con imagen o documento
        msg_type = message.get("type")
        if msg_type in ("image", "document"):
            st["data"]["images"] += 1
            AUTO_STATE[sender] = st

            if st["data"]["images"] >= 2:
                # asumimos INE + tarjeta de circulación
                finalize_auto_flow(sender, images=True, placa=None)
                return True
            else:
                send_message(
                    sender,
                    "📎 Recibí tu archivo. Si es *factura*, con esa imagen basta. "
                    "Si no, envía también la *foto de la tarjeta de circulación*."
                )
                return True

        # b) posible placa en texto
        placa = extract_placa_mx(t)
        if placa:
            finalize_auto_flow(sender, images=False, placa=placa)
            return True

        # c) texto dice 'factura' pero sin imagen: recordar enviar foto
        if "factura" in t:
            send_message(sender, "Por favor, envía la *foto de la factura* para continuar. 📸")
            return True

        # d) repite intención pero ya está activo
        if intent_auto:
            send_message(
                sender,
                "Solo necesito una de estas: *INE + tarjeta de circulación*, o *foto de la factura*, o *número de placa*. "
                "Envíame la que te sea más fácil. 😉"
            )
            return True

    # si no lo manejé aquí, dejo seguir al resto de tu lógica
    return False


def extract_placa_mx(texto):
    """
    Heurística simple para placas MX (varían por estado).
    Aceptamos patrones típicos como ABC123A, ABC1234, ABC-12-34, etc.
    """
    import re
    t = texto.upper().replace(" ", "")
    patrones = [
        r"\b[A-Z]{3}\d{3}[A-Z]\b",   # ABC123A
        r"\b[A-Z]{3}\d{4}\b",        # ABC1234
        r"\b[A-Z]{3}-\d{2}-\d{2}\b", # ABC-12-34
        r"\b[A-Z]{1,3}\d{3,4}\b"     # más laxo
    ]
    for p in patrones:
        m = re.search(p, t)
        if m:
            return m.group(0)
    return None


def finalize_auto_flow(sender, images, placa):
    """
    Cierra el flujo: notifica al asesor y confirma al cliente.
    """
    try:
        notify_advisor_auto(sender, images, placa)
    except Exception as e:
        logging.warning(f"Notificación asesor (auto) falló: {e}")

    if images:
        msg = "📨 ¡Listo! Recibí tus *documentos* para el seguro de auto y notifiqué a Christian."
    else:
        msg = f"📨 ¡Listo! Registré tu *placa* ({placa}) para el seguro de auto y notifiqué a Christian."

    send_message(sender, msg + " En breve te contactará para la cotización.")
    # limpiar estado
    AUTO_STATE[sender] = {"step": None, "data": {"images": 0}}


def notify_advisor_auto(user_phone, images, placa):
    """
    Envía WhatsApp PRIVADO al asesor con el motivo 'Seguro de auto'.
    Usa ADVISOR_NUMBER (o ADVISOR_NOTIFY_NUMBER como respaldo).
    """
    advisor = os.getenv("ADVISOR_NUMBER") or os.getenv("ADVISOR_NOTIFY_NUMBER")
    if not advisor:
        logging.warning("ADVISOR_NUMBER/ADVISOR_NOTIFY_NUMBER no configurado; no se notificó al asesor (auto).")
        return

    if images:
        detalle = "Cliente envió *documentos* (INE/tarjeta o factura)."
    else:
        detalle = f"Cliente envió *placa*: {placa}"

    body = (
        "🔔 *Vicky Bot — Seguro de auto*\n"
        f"• Cliente (wa): {user_phone}\n"
        f"• Detalle: {detalle}\n"
        "— Favor de contactar y continuar con la cotización."
    )
    send_message(advisor, body)
# ========= FIN AUTO INSURANCE FLOW =========

# Endpoint de salud
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
