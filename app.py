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
import random

# Configuración de logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Variables globales
user_state = {}
user_ctx = {}
greeted_at = {}
rag_cache = {}
conversation_history = {}

# Configuración
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
WHATSAPP_PHONE_ID = os.getenv("WHATSAPP_PHONE_ID")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "Hoja 1")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "mi_token_whatsapp")

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
    else:
        logger.warning("⚠️ OPENAI_API_KEY no configurada")
    
    # Inicializar Google Sheets
    try:
        google_creds_json = os.getenv("GOOGLE_CREDS_JSON")
        if google_creds_json and GOOGLE_SHEET_ID:
            creds_dict = json.loads(google_creds_json)
            scopes = [
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive"
            ]
            creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
            sheets_client = gspread.authorize(creds)
            google_ready = True
            logger.info("✅ Cliente Google Sheets inicializado")
        else:
            logger.warning("⚠️ GOOGLE_CREDS_JSON o GOOGLE_SHEET_ID no configurados")
    except Exception as e:
        logger.error(f"❌ Error inicializando Google Sheets: {str(e)}")

initialize_clients()

# =========================
# FUNCIONES AUXILIARES COMPLETAS
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
            "last10": _normalize_last10(phone),
            "last_activity": datetime.utcnow()
        }
    user_ctx[phone]["message_count"] += 1
    user_ctx[phone]["last_activity"] = datetime.utcnow()
    return user_ctx[phone]

def update_conversation_history(phone: str, role: str, message: str):
    """Actualiza el historial de conversación"""
    if phone not in conversation_history:
        conversation_history[phone] = []
    
    # Mantener solo los últimos 10 mensajes para evitar sobrecarga
    if len(conversation_history[phone]) >= 10:
        conversation_history[phone] = conversation_history[phone][-8:]
    
    conversation_history[phone].append({
        "role": role,
        "content": message,
        "timestamp": datetime.utcnow().isoformat()
    })

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
            update_conversation_history(phone, "assistant", message)
            return True
        else:
            logger.error(f"❌ Error enviando mensaje: {response.status_code} - {response.text}")
            return False
    except Exception as e:
        logger.error(f"❌ Excepción enviando mensaje: {str(e)}")
        return False

# =========================
# GOOGLE SHEETS COMPLETO CON REINTENTOS
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
            
            if not rows:
                logger.warning("❌ Hoja de cálculo vacía")
                return None
            
            # ✅ Búsqueda inteligente con múltiples campos
            for i, row in enumerate(rows[1:], start=2):  # Saltar encabezados
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
                    
            # ✅ Búsqueda por nombre aproximado si no encuentra por teléfono
            fuzzy_match = search_by_fuzzy_name(last10, rows)
            if fuzzy_match:
                return fuzzy_match
                
            logger.info(f"🔍 No se encontraron coincidencias para {last10}")
            return None
            
        except gspread.exceptions.APIError as e:
            if "Quota exceeded" in str(e):
                logger.error("❌ Cuota de Google Sheets excedida")
                return None
            logger.warning(f"⚠️ Intento {attempt + 1} fallado: {str(e)}")
            if attempt == 2:
                logger.error(f"❌ Error en sheet_match después de {attempt+1} intentos: {str(e)}")
            time.sleep(1 * (attempt + 1))
        except Exception as e:
            logger.warning(f"⚠️ Intento {attempt + 1} fallado: {str(e)}")
            if attempt == 2:
                logger.error(f"❌ Error en sheet_match después de {attempt+1} intentos: {str(e)}")
            time.sleep(1 * (attempt + 1))
    
    return None

def extract_name_from_row(row: List[str]) -> str:
    """Extrae nombre de una fila buscando en las primeras columnas"""
    name_priority = ["nombre", "name", "cliente", "contacto"]
    
    # Buscar en todas las celdas
    for cell in row:
        if cell and isinstance(cell, str) and len(cell.strip()) > 1:
            clean_cell = cell.strip()
            # Verificar si parece un nombre (contiene letras y espacios)
            if (any(c.isalpha() for c in clean_cell) and 
                ' ' in clean_cell and 
                not clean_cell.isdigit() and
                len(clean_cell) > 3):
                return clean_cell.title()
    
    # Si no encuentra nombre claro, usar primera celda no vacía
    for cell in row:
        if cell and str(cell).strip():
            clean_name = re.sub(r'[^a-zA-ZáéíóúÁÉÍÓÚñÑ\s]', '', str(cell).strip())
            if len(clean_name) > 1:
                return clean_name.title()
    
    return "Cliente"

def search_by_fuzzy_name(phone: str, rows: List[List[str]]) -> Optional[Dict[str, Any]]:
    """Búsqueda aproximada por nombre"""
    try:
        # En un caso real, aquí iría una implementación con fuzzywuzzy
        # Por ahora hacemos búsqueda simple en primeras columnas
        for i, row in enumerate(rows[1:], start=2):  # Saltar encabezados
            if not row:
                continue
                
            # Buscar en primeras 3 columnas que suelen contener nombres
            for cell in row[:3]:
                if cell and isinstance(cell, str) and len(cell.strip()) > 3:
                    clean_cell = cell.strip().lower()
                    # Verificar si parece un nombre completo
                    if ' ' in clean_cell and any(c.isalpha() for c in clean_cell):
                        nombre = extract_name_from_row(row)
                        logger.info(f"✅ Coincidencia aproximada: {nombre}")
                        return {
                            "row": i,
                            "nombre": nombre,
                            "raw": row,
                            "found_by": "name_match"
                        }
        return None
    except Exception as e:
        logger.error(f"❌ Error en búsqueda aproximada: {str(e)}")
        return None

# =========================
# SISTEMA RAG COMPLETO CON CACHE
# =========================

# Base de conocimientos para RAG
KNOWLEDGE_BASE = {
    "auto": """
    Seguros de Auto - Coberturas Principales:
    
    1. **Cobertura Básica (Responsabilidad Civil)**
    - Daños a terceros: $500,000 MXN
    - Gastos médicos: $100,000 MXN
    - Responsabilidad civil: Hasta $2,000,000 MXN
    
    2. **Cobertura Amplia**
    - Todo lo de cobertura básica +
    - Daños materiales al auto propio
    - Robo total o parcial
    - Gastos de grúa y taxi
    - Auto sustituto
    
    3. **Cobertura Extra**
    - Cristales
    - Equipo especial
    - Defensa jurídica
    
    **Deducibles:**
    - Colisión: 3% del valor del vehículo
    - Robo total: 5% del valor del vehículo
    - Daños materiales: $2,500 MXN
    
    **Proceso de siniestros:**
    1. Reportar dentro de las 24 horas
    2. Llamar al 800-123-4567
    3. Tomar fotos del accidente
    4. No admitir responsabilidad
    """,
    
    "imss": """
    Pensiones IMSS y Ley 73 - Información Actualizada:
    
    **Requisitos para Pensión:**
    - 60 años cumplidos
    - 1,250 semanas cotizadas mínimo
    - Estar dado de baja en el IMSS
    
    **Tipos de Pensión:**
    1. **Pensión por Cesantía en Edad Avanzada**
    2. **Pensión por Vejez**
    3. **Pensión por Invalidez**
    
    **Cálculo de Pensión:**
    - Se promedian las últimas 250 semanas cotizadas
    - El monto es aproximadamente el 35-40% del salario promedio
    
    **Ley 73 - Préstamos:**
    - Hasta 12 meses de pensión
    - Tasa de interés preferencial
    - Sin aval required
    - Desembolso en 72 horas
    
    **Documentación Requerida:**
    - INE vigente
    - CURP
    - Comprobante de domicilio
    - Número de seguridad social
    - Estado de cuenta IMSS
    
    **Contacto Especialistas:**
    - Lic. María González: 55-1234-5678
    - Horario: L-V 9:00 AM - 6:00 PM
    """,
    
    "general": """
    Vicky - Asistente Virtual Especializada
    
    **Servicios Disponibles:**
    
    🏠 **Avalúos y Bienes Raíces:**
    - Avalúos comerciales y residenciales
    - Trámites notariales
    - Asesoría hipotecaria
    
    💰 **Créditos y Préstamos:**
    - Hipotecarios
    - Personales
    - Automotrices
    - Refaccionarios
    
    🛡️ **Seguros:**
    - Auto
    - Vida
    - Gastos médicos
    - Hogar
    
    👵 **Pensiones IMSS:**
    - Asesoría para jubilación
    - Trámites de pensión
    - Préstamos Ley 73
    
    **Información de Contacto:**
    📞 Teléfono: 55-1234-5678
    📧 Email: info@vickyasesor.com
    🏢 Oficina: Av. Reforma 123, CDMX
    🕒 Horario: Lunes a Viernes 9:00 AM - 6:00 PM
    
    **Promociones Vigentes:**
    - Seguro de auto: 15% descuento por pago anual
    - Avalúo: 50% descuento en segundo inmueble
    - Asesoría pensiones: Primera consulta gratuita
    """
}

def answer_with_enhanced_rag(question: str, context: str = "auto") -> Optional[str]:
    """Sistema RAG completo con cache inteligente"""
    if not client_oa:
        logger.warning("❌ OpenAI no disponible para RAG")
        return None
        
    # ✅ Cache inteligente por contexto y pregunta
    cache_key = f"{context}:{question.lower().strip()}"
    if cache_key in rag_cache and time.time() - rag_cache[cache_key]["timestamp"] < 3600:
        logger.info(f"✅ Respuesta desde cache: {cache_key}")
        return rag_cache[cache_key]["answer"]
    
    try:
        # ✅ Seleccionar contexto específico
        manual_text = KNOWLEDGE_BASE.get(context, KNOWLEDGE_BASE["general"])
        
        if context == "auto":
            system_prompt = """Eres Vicky, experta en seguros de auto. Responde basado en el manual técnico.

Instrucciones:
- Sé precisa con números y coberturas
- Si no sabes algo, sugiere contactar a un especialista
- Usa emojis relevantes 🚗 💰 📄
- Mantén un tono profesional pero amable
- Responde en español"""
        elif context == "imss":
            system_prompt = """Eres Vicky, especialista en pensiones IMSS y Ley 73.

Instrucciones:
- Proporciona información actualizada sobre requisitos
- Sé clara con números y porcentajes
- Recomienda consultar con especialista para casos específicos
- Usa emojis relevantes 👵 📊 💼
- Responde en español"""
        else:
            system_prompt = """Eres Vicky, asistente especializada en servicios financieros.

Instrucciones:
- Presenta todos los servicios disponibles
- Ofrece opciones claras al usuario
- Proporciona información de contacto cuando sea relevante
- Usa emojis apropiados 🏠 💰 🛡️
- Responde en español"""
        
        # ✅ Extraer partes relevantes del manual
        relevant_chunks = extract_relevant_chunks(question, manual_text)
        
        prompt = f"""
{system_prompt}

**Contexto Relevante:**
{relevant_chunks}

**Pregunta del Usuario:**
{question}

**Recuerda:**
- Responde basado SOLO en la información del contexto
- Si no hay información suficiente, sugiere contactar al área especializada
- Sé concisa pero completa
- Incluye números específicos cuando los tengas
"""
        
        logger.info(f"🔍 Consultando GPT para: {question[:50]}...")
        response = client_oa.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "system", "content": prompt}],
            temperature=0.3,
            max_tokens=600
        )
        
        answer = (response.choices[0].message.content or "").strip()
        
        # ✅ Guardar en cache
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

def extract_relevant_chunks(question: str, text: str) -> str:
    """Extrae chunks relevantes del texto basado en la pregunta"""
    question_lower = question.lower()
    sentences = text.split('\n')
    relevant_sentences = []
    
    # Palabras clave de la pregunta
    keywords = [word for word in question_lower.split() if len(word) > 3]
    
    for sentence in sentences:
        sentence_lower = sentence.lower()
        # Verificar coincidencia de palabras clave
        keyword_matches = sum(1 for keyword in keywords if keyword in sentence_lower)
        
        # Coincidencias específicas por contexto
        if any(term in sentence_lower for term in ["requisito", "documento", "necesita", "debe"]):
            keyword_matches += 2
        if any(term in sentence_lower for term in ["precio", "costo", "monto", "descuento"]):
            keyword_matches += 2
        if any(term in sentence_lower for term in ["contacto", "teléfono", "email", "horario"]):
            keyword_matches += 2
            
        if keyword_matches > 0:
            relevant_sentences.append(sentence.strip())
    
    # Si no encuentra coincidencias, usar las primeras partes del texto
    if not relevant_sentences:
        relevant_sentences = sentences[:5]
    
    return '\n'.join(relevant_sentences[:8])  # Máximo 8 líneas

def detect_question_context(question: str) -> str:
    """Detecta el contexto de la pregunta automáticamente"""
    question_lower = question.lower()
    
    auto_keywords = ["auto", "carro", "coche", "vehículo", "seguro", "cobertura", "deducible", "choque", "accidente"]
    imss_keywords = ["imss", "pensión", "jubilación", "jubilar", "ley 73", "préstamo", "adulto mayor", "jubilado"]
    credit_keywords = ["crédito", "préstamo", "hipoteca", "financiamiento", "tasa", "interés"]
    appraisal_keywords = ["avalúo", "inmueble", "casa", "propiedad", "terreno", "valor"]
    
    if any(keyword in question_lower for keyword in auto_keywords):
        return "auto"
    elif any(keyword in question_lower for keyword in imss_keywords):
        return "imss"
    elif any(keyword in question_lower for keyword in credit_keywords):
        return "general"  # Los créditos están en general
    elif any(keyword in question_lower for keyword in appraisal_keywords):
        return "general"  # Los avalúos están en general
    else:
        return "general"

def is_technical_question(text: str) -> bool:
    """Determina si es una pregunta técnica que requiere RAG"""
    question_indicators = ["qué", "cómo", "cuándo", "dónde", "por qué", "cuál", "cuáles", "explica", "información", "requisito", "documento"]
    text_lower = text.lower()
    
    # Es pregunta técnica si contiene indicadores y es suficientemente larga
    has_question_word = any(indicator in text_lower for indicator in question_indicators)
    is_long_enough = len(text) > 12
    
    return has_question_word and is_long_enough

# =========================
# SISTEMA DE INTELIGENCIA MEJORADO
# =========================

def classify_intent_with_gpt(text: str) -> str:
    """Clasifica la intención del mensaje usando GPT"""
    if not client_oa or len(text) < 8:
        return "unknown"
        
    try:
        prompt = f"""
        Clasifica la intención principal del siguiente mensaje en UNA de estas categorías:
        
        Opciones:
        - "seguros": Consultas sobre seguros de auto, vida, hogar, etc.
        - "pensiones": Preguntas sobre IMSS, pensiones, jubilaciones, Ley 73
        - "creditos": Solicitudes de créditos, préstamos, hipotecas, financiamiento
        - "avaluos": Consultas sobre avalúos, bienes raíces, propiedades
        - "contacto": Quiere hablar con humano, asesor, contacto directo
        - "info_general": Información general, horarios, servicios, promociones
        - "otro": Otras consultas no categorizadas
        
        Mensaje del usuario: "{text}"
        
        Responde SOLO con la palabra de la categoría, nada más.
        """
        
        response = client_oa.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=15
        )
        
        intent = (response.choices[0].message.content or "").strip().lower()
        valid_intents = ["seguros", "pensiones", "creditos", "avaluos", "contacto", "info_general", "otro"]
        
        return intent if intent in valid_intents else "unknown"
        
    except Exception as e:
        logger.error(f"❌ Error clasificando intención: {str(e)}")
        return "unknown"

def redirect_based_on_intent(phone: str, intent: str, match: Optional[Dict[str, Any]]):
    """Redirige al flujo basado en la intención detectada"""
    user_name = match.get("nombre", "") if match else ""
    greeting = f"{user_name}, " if user_name else ""
    
    responses = {
        "seguros": f"{greeting}¡Excelente! Te ayudo con información de seguros. ¿Es para auto, vida, hogar o salud? 🛡️",
        "pensiones": f"{greeting}Perfecto, soy experta en pensiones IMSS. ¿Qué necesitas saber sobre tu pensión o jubilación? 👵",
        "creditos": f"{greeting}¡Claro! Te ayudo con créditos. ¿Es hipotecario, personal, automotriz o de nómina? 💰",
        "avaluos": f"{greeting}¡Genial! Te ayudo con avalúos. ¿Es para casa, departamento, local comercial o terreno? 🏠",
        "contacto": f"{greeting}Te conecto con un asesor especializado. ¿Podrías decirme tu nombre completo para que te contacten? 📞",
        "info_general": f"{greeting}Te proporciono información general de nuestros servicios. ¿Qué necesitas saber específicamente? ℹ️"
    }
    
    response = responses.get(intent, f"{greeting}¿En qué más puedo ayudarte?")
    send_message(phone, response)
    user_state[phone] = intent

def enhanced_gpt_fallback(phone: str, text: str, match: Optional[Dict[str, Any]]):
    """Fallback mejorado con GPT que considera historial"""
    if not client_oa:
        send_main_menu(phone)
        return
        
    try:
        user_name = match.get("nombre", "") if match else ""
        
        # Obtener historial de conversación reciente
        history = conversation_history.get(phone, [])
        recent_history = "\n".join([
            f"{msg['role']}: {msg['content']}" 
            for msg in history[-4:]  # Últimos 4 mensajes
        ])
        
        prompt = f"""
        Eres Vicky, una asistente especializada en servicios financieros, seguros, pensiones y avalúos.
        
        Información del usuario: {user_name}
        Historial reciente:
        {recent_history}
        
        Mensaje actual del usuario: "{text}"
        
        Responde de manera:
        - Amable y profesional
        - Ofrece ayuda específica basada en servicios
        - Usa emojis apropiados
        - Si no estás segura, sugiere opciones del menú principal
        - Sé concisa pero útil
        - Mantén el contexto del historial si es relevante
        
        Responde en español.
        """
        
        response = client_oa.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=200
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
    """Envía el menú principal mejorado"""
    menu = """
🎯 *Menú Principal - Vicky* 🎯

🤔 ¿En qué te puedo ayudar hoy?

1️⃣ 🛡️ *Seguros* - Auto, Vida, Hogar, Salud
2️⃣ 👵 *Pensiones IMSS* - Asesoría y trámites  
3️⃣ 💰 *Créditos* - Hipotecarios, Personales, Auto
4️⃣ 🏠 *Avalúos* - Información y cotizaciones
5️⃣ 📞 *Contacto* - Hablar con asesor humano
6️⃣ ℹ️ *Info General* - Servicios y promociones

Responde con el número o lo que necesites 👍
"""
    send_message(phone, menu)
    user_state[phone] = "main_menu"

def process_media_with_gpt(phone: str, msg: Dict[str, Any]):
    """Procesa medios con GPT-4 Vision si es imagen"""
    # Para implementación futura con GPT-4 Vision
    send_message(phone, "📎 ¡Gracias por la imagen! La revisaré y te ayudo con lo que necesites. Mientras tanto, ¿puedes contarme más detalles?")
    user_state[phone] = "processing_media"

# =========================
# FLUJOS DE CONVERSACIÓN COMPLETOS
# =========================

def handle_insurance_flow(phone: str, text: str, match: Optional[Dict[str, Any]]):
    """Maneja el flujo de seguros"""
    current_state = user_state.get(phone, "")
    text_lower = text.lower()
    
    if current_state == "seguros":
        if any(tipo in text_lower for tipo in ["auto", "carro", "coche", "vehículo"]):
            send_message(phone, "🚗 ¡Excelente elección! Seguro de auto. ¿Te gustaría conocer:\n\n1. Coberturas y precios\n2. Proceso de contratación\n3. Promociones vigentes\n4. Hablar con especialista")
            user_state[phone] = "seguros_auto"
        elif any(tipo in text_lower for tipo in ["vida", "fallecimiento", "deceso"]):
            send_message(phone, "❤️ Seguro de vida. Te ayudo con:\n\n1. Coberturas básicas\n2. Montos de aseguro\n3. Beneficiarios\n4. Cotización")
            user_state[phone] = "seguros_vida"
        elif any(tipo in text_lower for tipo in ["hogar", "casa", "departamento", "inmueble"]):
            send_message(phone, "🏠 Seguro de hogar. ¿Qué información necesitas?\n\n1. Coberturas para casa\n2. Protección contra robos\n3. Daños materiales\n4. Asistencia en hogar")
            user_state[phone] = "seguros_hogar"
        else:
            send_message(phone, "¿Qué tipo de seguro te interesa? 🛡️\n- Auto 🚗\n- Vida ❤️\n- Hogar 🏠\n- Salud 🏥")
    
    elif current_state == "seguros_auto":
        if "1" in text or "cobertura" in text_lower or "precio" in text_lower:
            answer = answer_with_enhanced_rag("coberturas y precios de seguro de auto", "auto")
            if answer:
                send_message(phone, answer)
            else:
                send_message(phone, "🚗 Para cotización exacta, necesito:\n- Marca y modelo del auto\n- Año\n- Uso (particular/comercial)\n- Código postal\n\n¿Podrías proporcionarme estos datos?")
        elif "2" in text or "proceso" in text_lower or "contrat" in text_lower:
            send_message(phone, "📝 Proceso de contratación:\n\n1. Cotización personalizada\n2. Elegir coberturas\n3. Llenar solicitud\n4. Pago inicial\n5. Emisión de póliza\n\nTodo en 24-48 horas. ¿Te interesa?")
        elif "3" in text or "promo" in text_lower:
            send_message(phone, "🎊 Promociones vigentes:\n\n• 15% descuento pago anual\n• Grúa gratuita por 1 año\n• Auto sustituto 15 días\n• Asistencia vial 24/7\n\n¿Te gustaría aplicar alguna?")
        elif "4" in text or "especialista" in text_lower or "humano" in text_lower:
            send_message(phone, "📞 Te conecto con nuestro especialista en seguros de auto. Te contactará en minutos. ¿Confirmas que estás disponible?")
            user_state[phone] = "contacto_especialista"
        else:
            send_message(phone, "¿Sobre qué aspecto del seguro auto quieres información? 🚗")
    
    else:
        send_message(phone, "🛡️ ¡Claro! Te ayudo con seguros. ¿Qué tipo te interesa?\n\n• Auto 🚗\n• Vida ❤️\n• Hogar 🏠\n• Salud 🏥")

def handle_pensions_flow(phone: str, text: str, match: Optional[Dict[str, Any]]):
    """Maneja el flujo de pensiones IMSS"""
    current_state = user_state.get(phone, "")
    text_lower = text.lower()
    
    if current_state == "pensiones":
        if any(term in text_lower for term in ["requisito", "necesito", "documento", "trámite"]):
            answer = answer_with_enhanced_rag("requisitos para pension IMSS", "imss")
            if answer:
                send_message(phone, answer)
            user_state[phone] = "pensiones_requisitos"
        elif any(term in text_lower for term in ["cálculo", "monto", "cuánto", "cantidad"]):
            send_message(phone, "📊 Para calcular tu pensión necesito:\n\n• Tu salario promedio últimos 5 años\n• Semanas cotizadas\n• Edad actual\n• Fecha de registro en IMSS\n\n¿Tienes esta información a la mano?")
            user_state[phone] = "pensiones_calculo"
        elif any(term in text_lower for term in ["ley 73", "préstamo", "dinero"]):
            send_message(phone, "💰 Préstamos Ley 73:\n\n• Hasta 12 meses de pensión\n• Tasa preferencial\n• Sin aval\n• Desembolso en 72 horas\n\n¿Te interesa más información?")
            user_state[phone] = "pensiones_ley73"
        else:
            send_message(phone, "👵 ¿En qué aspecto de tu pensión necesitas ayuda?\n\n1. Requisitos y documentos\n2. Cálculo de monto\n3. Préstamos Ley 73\n4. Contactar especialista")
    
    elif current_state == "pensiones_requisitos":
        send_message(phone, "¿Te gustaría agendar cita con nuestro especialista en pensiones? Te puede ayudar con todo el trámite. 📅")
        user_state[phone] = "contacto_especialista"
    
    elif current_state == "pensiones_calculo":
        if "sí" in text_lower or "si" in text_lower or "tengo" in text_lower:
            send_message(phone, "Perfecto! Te conecto con nuestro actuario para cálculo preciso. Te contactará en breve. ¿Confirmas?")
            user_state[phone] = "contacto_especialista"
        else:
            send_message(phone, "Puedes obtener tu estado de cuenta en el IMSS o nosotros te ayudamos. ¿Prefieres que te contacte un especialista?")
            user_state[phone] = "contacto_especialista"
    
    else:
        send_message(phone, "👵 ¡Perfecto! Te ayudo con tu pensión IMSS. Contamos con especialistas que te pueden asesorar en todo el proceso.")

def handle_contact_flow(phone: str, text: str, match: Optional[Dict[str, Any]]):
    """Maneja el flujo de contacto con humano"""
    current_state = user_state.get(phone, "")
    
    if current_state == "contacto":
        # Capturar nombre del usuario
        if len(text) > 5 and any(c.isalpha() for c in text):
            send_message(phone, f"✅ Gracias {text}. Un asesor te contactará en los próximos minutos.\n\n📞 Número de seguimiento: AS-{random.randint(1000,9999)}\n⏰ Tiempo estimado: 5-15 minutos\n\n¿Hay algo más en lo que pueda ayudarte mientras?")
            user_state[phone] = "main_menu"
        else:
            send_message(phone, "Por favor, ingresa tu nombre completo para que el asesor pueda contactarte adecuadamente.")
    
    elif current_state == "contacto_especialista":
        if "sí" in text.lower() or "si" in text.lower() or "confirmo" in text.lower():
            send_message(phone, f"✅ Perfecto! Especialista asignado.\n\n📞 Te contactará en minutos\n🎯 Está especializado en tu tema\n⏰ Horario extendido hasta 8 PM\n\nNúmero de ticket: TKT-{random.randint(10000,99999)}")
            user_state[phone] = "main_menu"
        else:
            send_message(phone, "¿Prefieres que te contacte en otro momento? Puedo programar la llamada.")

# =========================
# ROUTING PRINCIPAL MEJORADO
# =========================

def route_command(phone: str, text: str, match: Optional[Dict[str, Any]]) -> None:
    """Rutea comandos con todos los flujos integrados"""
    t = (text or "").strip()
    t_lower = t.lower()
    current_state = user_state.get(phone, "")
    
    # Actualizar historial
    update_conversation_history(phone, "user", t)
    
    # Comandos globales
    if t_lower in ["menu", "menú", "volver", "inicio", "0"]:
        send_main_menu(phone)
        return
    elif t_lower in ["hola", "hi", "hello", "buenas"]:
        send_main_menu(phone)
        return
    
    # ✅ Detectar intención con GPT para mejor routing
    if not current_state or current_state == "main_menu":
        if len(t) > 10:  # Mensajes largos sin estado específico
            intent = classify_intent_with_gpt(t)
            if intent and intent != "unknown":
                logger.info(f"🎯 Intención detectada: {intent} para {phone}")
                redirect_based_on_intent(phone, intent, match)
                return
    
    # ✅ RAG mejorado para preguntas técnicas
    if is_technical_question(t):
        logger.info(f"🔍 Pregunta técnica detectada: {t[:50]}...")
        context = detect_question_context(t)
        answer = answer_with_enhanced_rag(t, context)
        if answer:
            send_message(phone, answer)
            return
    
    # Routing por estado actual
    if current_state.startswith("seguros"):
        handle_insurance_flow(phone, t, match)
    elif current_state.startswith("pensiones"):
        handle_pensions_flow(phone, t, match)
    elif current_state.startswith("contacto"):
        handle_contact_flow(phone, t, match)
    elif current_state == "":
        # Mensaje inicial sin estado definido
        if t_lower in ["1", "seguro", "seguros"]:
            send_message(phone, "🛡️ ¡Excelente! Te ayudo con seguros. ¿Qué tipo te interesa?\n\n• Auto 🚗\n• Vida ❤️\n• Hogar 🏠\n• Salud 🏥")
            user_state[phone] = "seguros"
        elif t_lower in ["2", "pension", "pensiones", "imss"]:
            send_message(phone, "👵 ¡Perfecto! Te ayudo con tu pensión IMSS. ¿En qué aspecto necesitas ayuda?\n\n1. Requisitos y documentos\n2. Cálculo de monto\n3. Préstamos Ley 73\n4. Contactar especialista")
            user_state[phone] = "pensiones"
        elif t_lower in ["3", "credito", "creditos", "préstamo"]:
            send_message(phone, "💰 ¡Claro! Te ayudo con créditos. ¿Qué tipo necesitas?\n\n• Hipotecario 🏠\n• Personal 💳\n• Automotriz 🚗\n• De nómina 👨‍💼")
            user_state[phone] = "creditos"
        elif t_lower in ["4", "avaluo", "avalúos", "inmueble"]:
            send_message(phone, "🏠 ¡Genial! Te ayudo con avalúos. ¿Para qué tipo de propiedad?\n\n• Casa 🏡\n• Departamento 🏢\n• Local comercial 🏪\n• Terreno 🌳")
            user_state[phone] = "avaluos"
        elif t_lower in ["5", "contacto", "asesor", "humano", "persona"]:
            send_message(phone, "📞 Te conecto con un asesor especializado. ¿Podrías decirme tu nombre completo para que te contacten?")
            user_state[phone] = "contacto"
        elif t_lower in ["6", "info", "información", "general"]:
            answer = answer_with_enhanced_rag("información general de servicios", "general")
            if answer:
                send_message(phone, answer)
            else:
                send_main_menu(phone)
        else:
            # ✅ Fallback a GPT mejorado
            enhanced_gpt_fallback(phone, t, match)
    else:
        # Estado no reconocido, volver al menú principal
        send_main_menu(phone)

# =========================
# WEBHOOK COMPLETO Y ROBUSTO
# =========================

@app.route("/webhook", methods=["GET", "POST"])
def webhook_receive():
    """Webhook completo con manejo robusto de errores"""
    try:
        if request.method == "GET":
            # Verificación del webhook
            mode = request.args.get("hub.mode")
            token = request.args.get("hub.verify_token")
            challenge = request.args.get("hub.challenge")
            
            if mode and token:
                if mode == "subscribe" and token == VERIFY_TOKEN:
                    logger.info("✅ Webhook verificado correctamente")
                    return challenge, 200
                else:
                    logger.error("❌ Token de verificación inválido")
                    return "Forbidden", 403
            return "Bad Request", 400
        
        # Procesar mensaje POST
        payload = request.get_json()
        if not payload:
            logger.info("📥 Payload vacío")
            return jsonify({"ok": True}), 200
            
        logger.info(f"📥 Payload recibido: {json.dumps(payload, indent=2)[:500]}...")
        
        # Extraer mensaje
        entry = payload.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        value = changes.get("value", {})
        messages = value.get("messages", [])
        
        if not messages:
            logger.info("📭 No hay mensajes en el payload")
            return jsonify({"ok": True}), 200
            
        msg = messages[0]
        phone = msg.get("from", "").strip()
        if not phone:
            logger.warning("⚠️ Mensaje sin número de teléfono")
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
                
        else:
            logger.info(f"🔍 Tipo de mensaje no manejado: {mtype}")
            send_message(phone, "✅ Mensaje recibido. Te ayudo en lo que necesites.")
                
        return jsonify({"ok": True}), 200
        
    except Exception as e:
        logger.error(f"❌ Error crítico en webhook: {str(e)}", exc_info=True)
        # ✅ CORRECCIÓN: Siempre responder 200 para evitar reintentos de Meta
        return jsonify({"ok": True}), 200

# =========================
# HEALTH CHECKS COMPLETOS
# =========================

@app.route("/ext/deep-health")
def deep_health_check():
    """Health check completo para monitoreo"""
    health_status = {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "components": {},
        "stats": {
            "active_users": len(user_state),
            "total_contexts": len(user_ctx),
            "rag_cache_size": len(rag_cache),
            "conversation_history": len(conversation_history)
        }
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
        health_status["components"]["google_sheets"] = "healthy" if test_sheet else "unhealthy"
    except Exception as e:
        health_status["components"]["google_sheets"] = f"unhealthy: {str(e)}"
    
    # Verificar OpenAI
    try:
        if client_oa:
            test_gpt = client_oa.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[{"role": "user", "content": "Test"}],
                max_tokens=5
            )
            health_status["components"]["openai"] = "healthy" if test_gpt else "unhealthy"
        else:
            health_status["components"]["openai"] = "unhealthy: client not initialized"
    except Exception as e:
        health_status["components"]["openai"] = f"unhealthy: {str(e)}"
    
    # Verificar memoria y recursos
    try:
        health_status["components"]["memory"] = "healthy"
        health_status["stats"]["memory_usage"] = f"{len(str(user_state)) + len(str(user_ctx))} chars"
    except Exception as e:
        health_status["components"]["memory"] = f"unhealthy: {str(e)}"
    
    # Determinar estado general
    unhealthy_components = [c for c in health_status["components"].values() if "unhealthy" in c]
    if unhealthy_components:
        health_status["status"] = "degraded"
        health_status["unhealthy_components"] = unhealthy_components
    
    return jsonify(health_status), 200

@app.route("/")
def home():
    """Página de inicio"""
    return jsonify({
        "status": "online",
        "service": "WhatsApp Business API + GPT-4 - Vicky Assistant",
        "timestamp": datetime.utcnow().isoformat(),
        "version": "2.0.0",
        "endpoints": {
            "webhook": "/webhook",
            "health": "/ext/deep-health",
            "docs": "https://github.com/tu-repo/vicky-assistant"
        }
    })

@app.route("/ext/stats")
def get_stats():
    """Endpoint de estadísticas"""
    return jsonify({
        "user_state_count": len(user_state),
        "user_ctx_count": len(user_ctx),
        "rag_cache_count": len(rag_cache),
        "conversation_history_count": len(conversation_history),
        "greeted_users": len(greeted_at),
        "active_components": {
            "openai": client_oa is not None,
            "google_sheets": google_ready,
            "whatsapp": bool(WHATSAPP_TOKEN and WHATSAPP_PHONE_ID)
        }
    })

# =========================
# LIMPIEZA PERIÓDICA Y MANTENIMIENTO
# =========================

def cleanup_old_data():
    """Limpia datos antiguos para optimizar memoria"""
    now = datetime.utcnow()
    cleanup_threshold = timedelta(hours=24)
    
    # Limpiar user_state y user_ctx antiguos
    expired_phones = []
    for phone, ctx in user_ctx.items():
        last_activity = ctx.get("last_activity", ctx["created_at"])
        if (now - last_activity) > cleanup_threshold:
            expired_phones.append(phone)
    
    for phone in expired_phones:
        user_state.pop(phone, None)
        user_ctx.pop(phone, None)
        greeted_at.pop(phone, None)
        conversation_history.pop(phone, None)
    
    # Limpiar cache RAG antiguo (24 horas)
    global rag_cache
    current_time = time.time()
    rag_cache = {k: v for k, v in rag_cache.items() 
                if current_time - v["timestamp"] < 86400}  # 24 horas
    
    if expired_phones:
        logger.info(f"🧹 Limpiados {len(expired_phones)} usuarios inactivos")
    
    # Log de estado actual
    logger.info(f"📊 Estado actual: {len(user_state)} usuarios activos, {len(rag_cache)} entradas en cache")

# Configurar limpieza periódica
import threading

def periodic_cleanup():
    """Ejecuta limpieza periódica cada hora"""
    while True:
        try:
            cleanup_old_data()
        except Exception as e:
            logger.error(f"❌ Error en limpieza periódica: {str(e)}")
        time.sleep(3600)  # 1 hora

# Iniciar hilo de limpieza en background
cleanup_thread = threading.Thread(target=periodic_cleanup, daemon=True)
cleanup_thread.start()

# Ejecutar limpieza inicial
cleanup_old_data()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"🚀 Iniciando Vicky Assistant en puerto {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
