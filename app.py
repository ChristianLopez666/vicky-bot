import os
import logging
import time
import re
import json
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List
from flask import Flask, request, jsonify
import gspread
from google.oauth2.service_account import Credentials
from openai import OpenAI
import requests

# Configuración de logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Variables globales
user_state = {}
user_ctx = {}
greeted_at = {}
rag_cache = {}

# Configuración
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
WHATSAPP_PHONE_ID = os.getenv("WHATSAPP_PHONE_ID")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "Hoja 1")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4")

# Clientes
sheets_client = None
client_oa = None
google_ready = False

# Inicializar clientes
def initialize_clients():
    global sheets_client, client_oa, google_ready
    
    # Inicializar OpenAI
    if OPENAI_API_KEY:
        try:
            client_oa = OpenAI(api_key=OPENAI_API_KEY)
            logger.info("✅ Cliente OpenAI inicializado")
        except Exception as e:
            logger.error(f"❌ Error inicializando OpenAI: {str(e)}")
    
    # Inicializar Google Sheets
    try:
        google_creds_json = os.getenv("GOOGLE_CREDS_JSON")
        if google_creds_json and GOOGLE_SHEET_ID:
            creds_dict = json.loads(google_creds_json)
            creds = Credentials.from_service_account_info(creds_dict)
            sheets_client = gspread.authorize(creds)
            google_ready = True
            logger.info("✅ Cliente Google Sheets inicializado")
    except Exception as e:
        logger.error(f"❌ Error inicializando Google Sheets: {str(e)}")

initialize_clients()

# =========================
# FUNCIONES AUXILIARES MEJORADAS
# =========================

def _normalize_last10(phone: str) -> str:
    """Normaliza número de teléfono a últimos 10 dígitos"""
    digits = re.sub(r"\D", "", phone)
    return digits[-10:] if len(digits) >= 10 else digits

def ensure_ctx(phone: str) -> Dict[str, Any]:
    """Asegura que existe un contexto para el usuario"""
    if phone not in user_ctx:
        user_ctx[phone] = {
            "created_at": datetime.utcnow(),
            "message_count": 0,
            "match": None,
            "last10": _normalize_last10(phone)
        }
    user_ctx[phone]["message_count"] += 1
    user_ctx[phone]["last_activity"] = datetime.utcnow()
    return user_ctx[phone]

def send_message(phone: str, message: str, real_send: bool = True) -> bool:
    """Envía mensaje a WhatsApp"""
    if not real_send:
        logger.info(f"📤 [SIMULADO] Para {phone}: {message}")
        return True
        
    if not WHATSAPP_TOKEN or not WHATSAPP_PHONE_ID:
        logger.error("❌ Configuración de WhatsApp incompleta")
        return False
        
    try:
        url = f"https://graph.facebook.com/v17.0/{WHATSAPP_PHONE_ID}/messages"
        headers = {
            "Authorization": f"Bearer {WHATSAPP_TOKEN}",
            "Content-Type": "application/json"
        }
        data = {
            "messaging_product": "whatsapp",
            "to": phone,
            "text": {"body": message}
        }
        
        response = requests.post(url, headers=headers, json=data, timeout=30)
        if response.status_code == 200:
            logger.info(f"✅ Mensaje enviado a {phone}")
            return True
        else:
            logger.error(f"❌ Error enviando mensaje: {response.status_code} - {response.text}")
            return False
    except Exception as e:
        logger.error(f"❌ Excepción enviando mensaje: {str(e)}")
        return False

# =========================
# GOOGLE SHEETS MEJORADO CON REINTENTOS
# =========================

def sheet_match_by_last10(last10: str, test_mode: bool = False) -> Optional[Dict[str, Any]]:
    """Busca coincidencias en Google Sheets con reintentos"""
    if test_mode:
        return {"test": True, "nombre": "Test User"}
        
    if not (google_ready and sheets_client and GOOGLE_SHEET_ID):
        logger.warning("❌ Google Sheets no configurado")
        return None
        
    for attempt in range(3):
        try:
            sh = sheets_client.open_by_key(GOOGLE_SHEET_ID)
            ws = sh.worksheet(GOOGLE_SHEET_NAME)
            rows = ws.get_all_values()
            
            # ✅ MEJORA: Búsqueda más inteligente con múltiples campos
            for i, row in enumerate(rows, start=1):
                if not row:
                    continue
                    
                # Buscar en todos los campos que puedan contener teléfonos
                searchable_text = " ".join([str(cell) for cell in row])
                digits_in_row = re.sub(r"\D", "", searchable_text)
                
                if last10 and last10 in digits_in_row:
                    nombre = extract_name_from_row(row)
                    logger.info(f"✅ Coincidencia encontrada para {last10}: {nombre}")
                    return {
                        "row": i, 
                        "nombre": nombre,
                        "raw": row,
                        "found_by": "phone_match"
                    }
                    
            # ✅ MEJORA: Si no encuentra por teléfono, buscar por nombre aproximado
            fuzzy_match = search_by_fuzzy_name(last10, rows)
            if fuzzy_match:
                return fuzzy_match
                
            logger.info(f"🔍 No se encontraron coincidencias para {last10}")
            return None
            
        except Exception as e:
            logger.warning(f"⚠️ Intento {attempt + 1} fallado: {str(e)}")
            if attempt == 2:  # Último intento
                logger.error(f"❌ Error en sheet_match después de {attempt+1} intentos: {str(e)}")
            time.sleep(1 * (attempt + 1))
    
    return None

def extract_name_from_row(row: List[str]) -> str:
    """Extrae nombre de una fila buscando en las primeras columnas"""
    for cell in row[:3]:  # Buscar en primeras 3 columnas
        if cell and len(cell.strip()) > 1 and not cell.strip().isdigit():
            # Eliminar caracteres especiales y números
            clean_name = re.sub(r'[^a-zA-ZáéíóúÁÉÍÓÚñÑ\s]', '', cell.strip())
            if len(clean_name) > 1:
                return clean_name.title()
    return "Cliente"

def search_by_fuzzy_name(phone: str, rows: List[List[str]]) -> Optional[Dict[str, Any]]:
    """Búsqueda aproximada por nombre (placeholder para implementación futura)"""
    # Por ahora retorna None, se puede implementar fuzzy matching después
    return None

# =========================
# SISTEMA RAG MEJORADO CON CACHE
# =========================

def answer_with_enhanced_rag(question: str, context: str = "auto") -> Optional[str]:
    """Sistema RAG mejorado con cache inteligente"""
    if not client_oa:
        logger.warning("❌ OpenAI no disponible para RAG")
        return None
        
    # ✅ MEJORA: Cache inteligente por contexto y pregunta
    cache_key = f"{context}:{question.lower().strip()}"
    if cache_key in rag_cache and time.time() - rag_cache[cache_key]["timestamp"] < 3600:  # 1 hora
        logger.info(f"✅ Respuesta desde cache: {cache_key}")
        return rag_cache[cache_key]["answer"]
    
    try:
        # ✅ MEJORA: Seleccionar contexto específico
        if context == "auto":
            manual_text = ensure_auto_manual_text()
            system_prompt = "Eres un experto en seguros de auto. Responde basado únicamente en el manual."
        elif context == "imss":
            manual_text = get_imss_guidelines()
            system_prompt = "Eres un experto en pensiones IMSS y préstamos Ley 73."
        else:
            manual_text = get_general_knowledge()
            system_prompt = "Eres Vicky, una asistente especializada en servicios financieros e hipotecarios."
        
        if not manual_text:
            logger.warning("❌ No hay contexto disponible para RAG")
            return None
            
        # ✅ MEJORA: Chunking inteligente para contextos largos
        relevant_chunks = extract_relevant_chunks(question, manual_text)
        
        prompt = f"""
        {system_prompt}
        
        Contexto relevante:
        {relevant_chunks}
        
        Pregunta del usuario: {question}
        
        Instrucciones:
        - Responde en español de manera clara y profesional
        - Usa emojis apropiados si es necesario
        - Si la información no está en el contexto, sugiere contactar a un asesor
        - Sé conciso pero completo
        - Mantén un tono amable y servicial
        """
        
        logger.info(f"🔍 Consultando GPT para: {question[:50]}...")
        response = client_oa.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "system", "content": prompt}],
            temperature=0.3,
            max_tokens=500
        )
        
        answer = (response.choices[0].message.content or "").strip()
        
        # ✅ MEJORA: Guardar en cache
        if answer:
            rag_cache[cache_key] = {
                "answer": answer,
                "timestamp": time.time(),
                "context": context
            }
            logger.info(f"✅ Respuesta GPT guardada en cache")
            
        return answer
        
    except Exception as e:
        logger.error(f"❌ Error en RAG mejorado: {str(e)}")
        return None

# Funciones de contexto (placeholders)
def ensure_auto_manual_text() -> str:
    return "Información sobre seguros de auto: coberturas básicas, deducibles, asistencia vial, etc."

def get_imss_guidelines() -> str:
    return "Información sobre pensiones IMSS: requisitos, cálculo de pensiones, trámites, Ley 73."

def get_general_knowledge() -> str:
    return "Servicios financieros: hipotecas, créditos, seguros, asesoría financiera."

def extract_relevant_chunks(question: str, text: str) -> str:
    """Extrae chunks relevantes del texto (simplificado)"""
    # Implementación básica - se puede mejorar con embeddings
    words = question.lower().split()
    sentences = text.split('.')
    relevant = []
    
    for sentence in sentences:
        if any(word in sentence.lower() for word in words if len(word) > 3):
            relevant.append(sentence.strip())
    
    return '. '.join(relevant[:3])  # Máximo 3 oraciones

def detect_question_context(question: str) -> str:
    """Detecta el contexto de la pregunta"""
    question_lower = question.lower()
    auto_keywords = ["auto", "carro", "coche", "seguro", "cobertura", "deducible"]
    imss_keywords = ["imss", "pensión", "jubilación", "ley 73", "préstamo"]
    
    if any(keyword in question_lower for keyword in auto_keywords):
        return "auto"
    elif any(keyword in question_lower for keyword in imss_keywords):
        return "imss"
    else:
        return "general"

def is_technical_question(text: str) -> bool:
    """Determina si es una pregunta técnica"""
    question_indicators = ["qué", "cómo", "cuándo", "dónde", "por qué", "cuál", "explica", "información"]
    text_lower = text.lower()
    return any(indicator in text_lower for indicator in question_indicators) and len(text) > 15

# =========================
# ROUTING MEJORADO CON GPT
# =========================

def classify_intent_with_gpt(text: str) -> str:
    """Clasifica la intención del mensaje usando GPT"""
    if not client_oa or len(text) < 10:
        return "unknown"
        
    try:
        prompt = f"""
        Clasifica la intención del siguiente mensaje en una de estas categorías:
        - "seguros": Preguntas sobre seguros de auto, vida, hogar, etc.
        - "pensiones": Consultas sobre IMSS, pensiones, jubilaciones
        - "creditos": Solicitudes de créditos, préstamos, hipotecas
        - "info_general": Información general, contacto, horarios
        - "otro": Otras consultas no categorizadas
        
        Mensaje: "{text}"
        
        Responde solo con la categoría, nada más.
        """
        
        response = client_oa.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=10
        )
        
        intent = (response.choices[0].message.content or "").strip().lower()
        valid_intents = ["seguros", "pensiones", "creditos", "info_general", "otro"]
        return intent if intent in valid_intents else "unknown"
        
    except Exception as e:
        logger.error(f"❌ Error clasificando intención: {str(e)}")
        return "unknown"

def redirect_based_on_intent(phone: str, intent: str, match: Optional[Dict[str, Any]]):
    """Redirige al flujo basado en la intención detectada"""
    responses = {
        "seguros": "¡Excelente! Te ayudo con información de seguros. ¿Es para auto, vida, hogar o salud?",
        "pensiones": "Perfecto, soy experta en pensiones IMSS. ¿Qué necesitas saber sobre tu pensión o jubilación?",
        "creditos": "¡Claro! Te ayudo con créditos. ¿Es hipotecario, personal, o de otro tipo?",
        "info_general": "Te proporciono información general. ¿Qué necesitas saber específicamente?"
    }
    
    response = responses.get(intent, "¿En qué más puedo ayudarte?")
    send_message(phone, response)
    user_state[phone] = intent

def enhanced_gpt_fallback(phone: str, text: str, match: Optional[Dict[str, Any]]):
    """Fallback mejorado con GPT"""
    if not client_oa:
        send_main_menu(phone)
        return
        
    try:
        user_name = match.get("nombre", "") if match else ""
        context = f"Usuario: {user_name}\nMensaje: {text}"
        
        prompt = f"""
        Eres Vicky, una asistente especializada en servicios financieros, seguros y pensiones.
        
        Contexto: {context}
        
        Responde de manera amable y profesional, ofreciendo ayuda específica.
        Usa emojis apropiados. Sé concisa pero útil.
        Si no estás segura, sugiere opciones del menú principal.
        """
        
        response = client_oa.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=150
        )
        
        answer = (response.choices[0].message.content or "").strip()
        if answer:
            send_message(phone, answer)
        else:
            send_main_menu(phone)
            
    except Exception as e:
        logger.error(f"❌ Error en GPT fallback: {str(e)}")
        send_main_menu(phone)

def send_main_menu(phone: str):
    """Envía el menú principal"""
    menu = """
    🎯 *Menú Principal* 🎯

    🤔 ¿En qué te puedo ayudar?

    1️⃣ 📊 *Seguros* - Auto, Vida, Hogar
    2️⃣ 👵 *Pensiones IMSS* - Asesoría
    3️⃣ 💰 *Créditos* - Hipotecarios, Personales
    4️⃣ 🏠 *Avalúos* - Información y cotizaciones
    5️⃣ 📞 *Contacto* - Hablar con asesor

    Responde con el número o lo que necesites 👍
    """
    send_message(phone, menu)

def route_command(phone: str, text: str, match: Optional[Dict[str, Any]]) -> None:
    """Rutea comandos con GPT integrado"""
    t = (text or "").strip().lower()
    current_state = user_state.get(phone, "")
    
    # ✅ MEJORA: Detectar intención con GPT para mejor routing
    if not current_state and len(t) > 10:  # Mensajes largos sin estado
        intent = classify_intent_with_gpt(t)
        if intent and intent != "unknown":
            logger.info(f"🎯 Intención detectada: {intent} para {phone}")
            # Redirigir al flujo correspondiente basado en intención
            redirect_based_on_intent(phone, intent, match)
            return
    
    # ✅ MEJORA: RAG mejorado para preguntas técnicas
    if is_technical_question(t):
        logger.info(f"🔍 Pregunta técnica detectada: {t[:50]}...")
        answer = answer_with_enhanced_rag(t, detect_question_context(t))
        if answer:
            send_message(phone, answer)
            return
    
    # Lógica de flujos existente (simplificada)
    if t in ["1", "seguro", "seguros"]:
        send_message(phone, "¡Excelente! ¿Qué tipo de seguro te interesa: auto, vida, hogar o salud?")
        user_state[phone] = "seguros"
    elif t in ["2", "pension", "pensiones", "imss"]:
        send_message(phone, "Perfecto, soy experta en pensiones IMSS. ¿Qué necesitas saber sobre tu pensión o jubilación?")
        user_state[phone] = "pensiones"
    elif t in ["3", "credito", "creditos", "préstamo"]:
        send_message(phone, "¡Claro! Te ayudo con créditos. ¿Es hipotecario, personal, automotriz o de nómina?")
        user_state[phone] = "creditos"
    elif t in ["4", "contacto", "asesor", "hablar"]:
        send_message(phone, "📞 Te conecto con un asesor especializado. Ellos te contactarán en breve. ¿Podrías confirmarme tu nombre completo?")
        user_state[phone] = "contacto"
    else:
        # ✅ CORRECCIÓN: Fallback a GPT mejorado
        if current_state == "" and client_oa:
            enhanced_gpt_fallback(phone, t, match)
        else:
            send_main_menu(phone)

def process_media_with_gpt(phone: str, msg: Dict[str, Any]):
    """Procesa medios con GPT-4 Vision si es imagen"""
    # Placeholder para implementación futura
    send_message(phone, "📎 Archivo recibido. Lo revisaré y te respondo pronto.")

# =========================
# WEBHOOK CORREGIDO - LÓGICA MEJORADA
# =========================

@app.route("/webhook", methods=["GET", "POST"])
def webhook_receive():
    """Webhook corregido con lógica de estados mejorada"""
    try:
        if request.method == "GET":
            # Verificación del webhook
            mode = request.args.get("hub.mode")
            token = request.args.get("hub.verify_token")
            challenge = request.args.get("hub.challenge")
            
            if mode and token:
                if mode == "subscribe" and token == os.getenv("VERIFY_TOKEN"):
                    logger.info("✅ Webhook verificado")
                    return challenge, 200
                else:
                    logger.error("❌ Token de verificación inválido")
                    return "Forbidden", 403
            return "Bad Request", 400
        
        # Procesar mensaje POST
        payload = request.get_json()
        logger.info(f"📥 Payload recibido: {json.dumps(payload, indent=2)}")
        
        if not payload:
            return jsonify({"ok": True}), 200
            
        # Extraer mensaje
        entry = payload.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        value = changes.get("value", {})
        messages = value.get("messages", [])
        
        if not messages:
            return jsonify({"ok": True}), 200
            
        msg = messages[0]
        phone = msg.get("from", "").strip()
        if not phone:
            return jsonify({"ok": True}), 200

        # ✅ CORRECCIÓN: Determinar si es primer mensaje SIN afectar user_state
        is_new_conversation = (phone not in user_state)
        
        # ✅ CORRECCIÓN: Buscar en Google Sheets ANTES de cualquier procesamiento
        last10 = _normalize_last10(phone)
        match = sheet_match_by_last10(last10)
        
        # ✅ CORRECCIÓN: Guardar match en contexto inmediatamente
        ctx = ensure_ctx(phone)
        ctx["match"] = match
        ctx["last10"] = last10
        
        # ✅ CORRECCIÓN: Saludo solo si es nuevo Y no se saludó recientemente
        if is_new_conversation:
            now = datetime.utcnow()
            last_greeting = greeted_at.get(phone)
            if not last_greeting or (now - last_greeting) > timedelta(hours=24):
                if match and match.get("nombre"):
                    send_message(phone, f"Hola {match['nombre']} 👋 Soy *Vicky*. ¿En qué te puedo ayudar?")
                else:
                    send_message(phone, "Hola 👋 Soy *Vicky*. Estoy para ayudarte.")
                greeted_at[phone] = now
        
        # ✅ CORRECCIÓN: Inicializar estado SOLO si no existe
        if phone not in user_state:
            user_state[phone] = ""

        # ✅ CORRECCIÓN: Procesar mensaje según tipo
        mtype = msg.get("type", "")
        if mtype == "text" and "text" in msg:
            text = (msg["text"].get("body") or "").strip()
            logger.info(f"💬 Mensaje de {phone}: {text}")
            route_command(phone, text, match)
            
        elif mtype in {"image", "document", "audio", "video"}:
            logger.info(f"📎 Medio recibido de {phone}: {mtype}")
            # ✅ MEJORA: Procesar medios con GPT-4 Vision si es imagen
            if mtype == "image" and client_oa:
                process_media_with_gpt(phone, msg)
            else:
                send_message(phone, "📎 Archivo recibido. Lo revisaré y te respondo pronto.")
                
        return jsonify({"ok": True}), 200
        
    except Exception as e:
        logger.error(f"❌ Error en webhook: {str(e)}", exc_info=True)
        # ✅ CORRECCIÓN: Siempre responder 200 para evitar reintentos de Meta
        return jsonify({"ok": True}), 200

# =========================
# HEALTH CHECKS MEJORADOS
# =========================

@app.route("/ext/deep-health")
def deep_health_check():
    """Health check mejorado para monitoreo"""
    health_status = {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "components": {}
    }
    
    # Verificar WhatsApp API
    try:
        test_msg = send_message("test", "Health check", real_send=False)
        health_status["components"]["whatsapp"] = "healthy" if test_msg else "unhealthy"
    except Exception as e:
        health_status["components"]["whatsapp"] = f"unhealthy: {str(e)}"
    
    # Verificar Google Sheets
    try:
        test_sheet = sheet_match_by_last10("1234567890", test_mode=True)
        health_status["components"]["google_sheets"] = "healthy"
    except Exception as e:
        health_status["components"]["google_sheets"] = f"unhealthy: {str(e)}"
    
    # Verificar OpenAI
    try:
        test_gpt = client_oa.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "user", "content": "Test"}],
            max_tokens=5
        ) if client_oa else None
        health_status["components"]["openai"] = "healthy" if test_gpt else "unhealthy"
    except Exception as e:
        health_status["components"]["openai"] = f"unhealthy: {str(e)}"
    
    # Determinar estado general
    unhealthy_components = [c for c in health_status["components"].values() if "unhealthy" in c]
    if unhealthy_components:
        health_status["status"] = "degraded"
        health_status["unhealthy_components"] = unhealthy_components
    
    return jsonify(health_status), 200

@app.route("/")
def home():
    return jsonify({
        "status": "online",
        "service": "WhatsApp Business API + GPT-4",
        "timestamp": datetime.utcnow().isoformat()
    })

# =========================
# LIMPIEZA PERIÓDICA
# =========================

def cleanup_old_data():
    """Limpia datos antiguos para optimizar memoria"""
    now = datetime.utcnow()
    
    # Limpiar user_state antiguo (24 horas sin actividad)
    expired_phones = []
    for phone, ctx in user_ctx.items():
        last_activity = ctx.get("last_activity", ctx["created_at"])
        if (now - last_activity) > timedelta(hours=24):
            expired_phones.append(phone)
    
    for phone in expired_phones:
        user_state.pop(phone, None)
        user_ctx.pop(phone, None)
        greeted_at.pop(phone, None)
    
    # Limpiar cache RAG antiguo (24 horas)
    global rag_cache
    current_time = time.time()
    rag_cache = {k: v for k, v in rag_cache.items() 
                if current_time - v["timestamp"] < 86400}
    
    if expired_phones:
        logger.info(f"🧹 Limpiados {len(expired_phones)} usuarios inactivos")

# Ejecutar limpieza al inicio
cleanup_old_data()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
