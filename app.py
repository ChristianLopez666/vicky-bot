# app.py — Vicky Bot SECOM (Render-ready)
# Python 3.10+
# Ejecuta en Render: gunicorn app:app --bind 0.0.0.0:$PORT

import os
import re
import json
import time
import logging
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, List, Tuple

import io
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# Google (sin oauth2client): usa google-auth + gspread + google-api-python-client
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from PyPDF2 import PdfReader  # <- para RAG light (PDF)

# OpenAI SDK 1.x
from openai import OpenAI

# =========================
# Entorno y logging
# =========================
load_dotenv()

META_TOKEN = os.getenv("META_TOKEN", "").strip()
WABA_PHONE_ID = os.getenv("WABA_PHONE_ID", "").strip()
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "").strip()
ADVISOR_NUMBER = os.getenv("ADVISOR_NUMBER", "").strip()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-3.5-turbo-1106")

GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "").strip()
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "Prospectos SECOM Auto").strip()
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON", "").strip()
MANUALES_VICKY_FOLDER_ID = os.getenv("MANUALES_VICKY_FOLDER_ID", "").strip()

NOTIFICAR_ASESOR = os.getenv("NOTIFICAR_ASESOR", "true").lower() == "true"
PORT = int(os.getenv("PORT", "5000"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("vicky-secom")

# =========================
# Clientes externos
# =========================
# WhatsApp
WPP_API_URL = f"https://graph.facebook.com/v20.0/{WABA_PHONE_ID}/messages" if WABA_PHONE_ID else None
WPP_TIMEOUT = 15

# OpenAI 1.x
client_oa: Optional[OpenAI] = None
if OPENAI_API_KEY:
    try:
        client_oa = OpenAI(api_key=OPENAI_API_KEY)
    except Exception:
        log.exception("No se pudo inicializar OpenAI")

# Google Sheets + Drive (solo lectura)
sheets_client = None
drive_client = None
google_ready = False
try:
    if GOOGLE_CREDENTIALS_JSON:
        info = json.loads(GOOGLE_CREDENTIALS_JSON)
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets.readonly",
            "https://www.googleapis.com/auth/drive.readonly",
        ]
        creds = Credentials.from_service_account_info(info, scopes=scopes)
        sheets_client = gspread.authorize(creds)
        drive_client = build("drive", "v3", credentials=creds)
        google_ready = True
        log.info("Google listo (Sheets RO + Drive RO)")
    else:
        log.warning("GOOGLE_CREDENTIALS_JSON ausente. Google deshabilitado.")
except Exception:
    log.exception("Error inicializando Google")

# =========================
# Estado en memoria
# =========================
app = Flask(__name__)
user_state: Dict[str, str] = {}
user_ctx: Dict[str, Dict[str, Any]] = {}
last_sent: Dict[str, str] = {}

# Saludo solo 1 vez por ventana (24h)
greeted_at: Dict[str, datetime] = {}
GREET_WINDOW_HOURS = 24

# =========================
# Sistema de limpieza de estados
# =========================
def cleanup_old_states():
    """Limpia estados antiguos para prevenir memory leaks"""
    current_time = datetime.utcnow()
    max_age = timedelta(days=2)  # Reducir a 2 días para mejor rendimiento
    
    # Limpiar greeted_at
    global greeted_at
    old_phones = [k for k, v in greeted_at.items() 
                 if current_time - v >= max_age]
    for phone in old_phones:
        greeted_at.pop(phone, None)
        user_state.pop(phone, None)
        user_ctx.pop(phone, None)
    
    log.info(f"[Cleanup] Estados limpiados: {len(old_phones)} usuarios antiguos")

def start_cleanup_scheduler():
    def _cleanup_loop():
        while True:
            time.sleep(3600)  # 1 hora
            cleanup_old_states()
    threading.Thread(target=_cleanup_loop, daemon=True).start()

# Iniciar scheduler de limpieza
start_cleanup_scheduler()

# =========================
# Utilidades
# =========================
def _normalize_last10(phone: str) -> str:
    d = re.sub(r"\D", "", phone or "")
    return d[-10:] if len(d) >= 10 else d

def _send_wpp_payload(payload: Dict[str, Any]) -> bool:
    if not (META_TOKEN and WPP_API_URL):
        log.error("WhatsApp no configurado (META_TOKEN/WABA_PHONE_ID).")
        return False
    headers = {"Authorization": f"Bearer {META_TOKEN}", "Content-Type": "application/json"}
    for attempt in range(3):
        try:
            r = requests.post(WPP_API_URL, headers=headers, json=payload, timeout=WPP_TIMEOUT)
            if r.status_code == 200:
                return True
            if r.status_code in (429,) or 500 <= r.status_code < 600:
                time.sleep(2 ** attempt)
                continue
            log.warning(f"WhatsApp {r.status_code}: {r.text[:200]}")
            return False
        except requests.exceptions.Timeout:
            time.sleep(2 ** attempt)
        except Exception:
            log.exception("Error enviando a WhatsApp")
            return False
    return False

def send_message(to: str, text: str) -> bool:
    text = (text or "").strip()
    if not text:
        return False
    if last_sent.get(to) == text:
        return True
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text[:4096]},
    }
    ok = _send_wpp_payload(payload)
    if ok:
        last_sent[to] = text
    return ok

def notify_advisor(text: str) -> None:
    if NOTIFICAR_ASESOR and ADVISOR_NUMBER:
        try:
            send_message(ADVISOR_NUMBER, text)
        except Exception:
            log.exception("Error notificando al asesor")

def interpret_yesno(text: str) -> str:
    t = (text or "").lower()
    pos = ["sí", "si", "claro", "ok", "vale", "de acuerdo", "afirmativo", "correcto"]
    neg = ["no", "nop", "negativo", "no gracias", "no quiero", "nel"]
    if any(w in t for w in pos):
        return "yes"
    if any(w in t for w in neg):
        return "no"
    return "unknown"

def extract_number(text: str) -> Optional[float]:
    if not text:
        return None
    t = text.replace(",", "").replace("$", "")
    m = re.search(r"(\d{1,12}(\.\d+)?)", t)
    try:
        return float(m.group(1)) if m else None
    except Exception:
        return None

def ensure_ctx(phone: str) -> Dict[str, Any]:
    if phone not in user_ctx:
        user_ctx[phone] = {}
    return user_ctx[phone]

# =========================
# Google helpers
# =========================
def sheet_match_by_last10(last10: str) -> Optional[Dict[str, Any]]:
    if not (google_ready and sheets_client and GOOGLE_SHEET_ID and GOOGLE_SHEET_NAME):
        return None
    try:
        sh = sheets_client.open_by_key(GOOGLE_SHEET_ID)
        ws = sh.worksheet(GOOGLE_SHEET_NAME)
        rows = ws.get_all_values()
        for i, row in enumerate(rows, start=1):
            joined = " | ".join(row)
            digits = re.sub(r"\D", "", joined)
            if last10 and last10 in digits:
                nombre = ""
                for c in row:
                    if c and not re.search(r"\d", c):
                        nombre = c.strip()
                        break
                return {"row": i, "nombre": nombre, "raw": row}
        return None
    except Exception:
        log.exception("Error leyendo Google Sheets")
        return None

def list_drive_manuals(folder_id: str) -> List[Dict[str, str]]:
    if not (google_ready and drive_client and folder_id):
        return []
    try:
        q = f"'{folder_id}' in parents and mimeType='application/pdf' and trashed=false"
        resp = drive_client.files().list(q=q, fields="files(id, name, webViewLink)").execute()
        files = resp.get("files", [])
        out = []
        for f in files:
            link = f.get("webViewLink", "")
            if not link:
                meta = drive_client.files().get(fileId=f["id"], fields="webViewLink").execute()
                link = meta.get("webViewLink", "")
            out.append({"id": f["id"], "name": f["name"], "webViewLink": link})
        return out
    except Exception:
        log.exception("Error listando manuales en Drive")
        return []

# =========================
# RAG MEJORADO - Sistema de consulta a manuales
# =========================
_manual_auto_cache = {"text": None, "file_id": None, "loaded_at": None, "file_name": None}

def _find_best_auto_manual() -> Optional[Dict[str, str]]:
    """Encuentra el mejor manual de auto disponible"""
    if not (google_ready and drive_client and MANUALES_VICKY_FOLDER_ID):
        log.error("Google Drive no configurado para RAG")
        return None
    
    try:
        # Buscar TODOS los PDFs en la carpeta
        q = (
            f"'{MANUALES_VICKY_FOLDER_ID}' in parents and "
            "mimeType='application/pdf' and trashed=false"
        )
        resp = drive_client.files().list(
            q=q, 
            fields="files(id, name, modifiedTime)",
            orderBy="modifiedTime desc",
            pageSize=20
        ).execute()
        
        files = resp.get("files", [])
        
        if not files:
            log.warning("No se encontraron PDFs en la carpeta de manuales")
            return None
        
        # Priorizar manuales de auto por nombre
        auto_files = []
        other_files = []
        
        for file in files:
            name = (file.get("name") or "").lower()
            if any(keyword in name for keyword in ["auto", "vehículo", "vehicular", "cobertura", "automóvil"]):
                auto_files.append(file)
            else:
                other_files.append(file)
        
        # Seleccionar el más relevante
        if auto_files:
            selected = auto_files[0]
            # Buscar el más específico
            for file in auto_files:
                name = (file.get("name") or "").lower()
                if "cobertura" in name and "auto" in name:
                    selected = file
                    break
        elif other_files:
            selected = other_files[0]
            log.info(f"[RAG] Usando manual genérico: {selected.get('name')}")
        else:
            return None
            
        return {
            "id": selected["id"],
            "name": selected.get("name", "desconocido"),
            "modified": selected.get("modifiedTime")
        }
        
    except Exception as e:
        log.exception(f"[RAG] Error buscando manuales: {str(e)}")
        return None

def _download_pdf_text_improved(file_id: str) -> Optional[str]:
    """Extrae texto de PDF con mejor manejo de errores"""
    try:
        from googleapiclient.http import MediaIoBaseDownload
        req = drive_client.files().get_media(fileId=file_id)
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, req)
        done = False
        
        while not done:
            status, done = downloader.next_chunk()
            
        fh.seek(0)
        reader = PdfReader(fh)
        text_parts = []
        
        for i, page in enumerate(reader.pages):
            try:
                page_text = page.extract_text() or ""
                if page_text.strip():
                    # Limpiar y normalizar texto
                    cleaned_text = re.sub(r'\s+', ' ', page_text).strip()
                    if len(cleaned_text) > 50:  # Ignorar páginas casi vacías
                        text_parts.append(f"Página {i+1}: {cleaned_text}")
            except Exception as e:
                log.warning(f"[RAG] Error en página {i+1}: {str(e)}")
                continue
        
        full_text = "\n\n".join(text_parts)
        log.info(f"[RAG] Texto extraído: {len(full_text)} caracteres, {len(text_parts)} páginas con contenido")
        return full_text if full_text.strip() else None
        
    except Exception as e:
        log.exception(f"[RAG] Error descargando PDF: {str(e)}")
        return None

def ensure_auto_manual_text(force_reload: bool = False) -> Optional[str]:
    """Sistema de cache mejorado para manuales"""
    cache = _manual_auto_cache
    current_time = datetime.utcnow()
    cache_max_age = timedelta(hours=12)  # Refrescar cada 12 horas
    
    # Verificar cache válido
    if (not force_reload and 
        cache.get("text") and 
        cache.get("loaded_at") and 
        (current_time - cache["loaded_at"]) < cache_max_age):
        log.info("[RAG] Usando manual en cache")
        return cache["text"]
    
    log.info("[RAG] Cargando manual desde Drive...")
    
    # Buscar mejor manual disponible
    manual_info = _find_best_auto_manual()
    if not manual_info:
        log.error("[RAG] No se pudo encontrar ningún manual")
        return None
    
    # Descargar y procesar texto
    text = _download_pdf_text_improved(manual_info["id"])
    if text:
        cache.update({
            "text": text,
            "file_id": manual_info["id"],
            "file_name": manual_info["name"],
            "loaded_at": current_time
        })
        log.info(f"[RAG] Manual cargado: {manual_info['name']} ({len(text)} caracteres)")
        return text
    else:
        log.error(f"[RAG] Falló la extracción de texto: {manual_info['name']}")
        return None

def answer_auto_from_manual(question: str) -> Optional[str]:
    """Sistema RAG mejorado para consultas técnicas"""
    if not client_oa:
        log.warning("OpenAI no disponible para RAG")
        return None
    
    # 1. Obtener texto del manual
    manual_text = ensure_auto_manual_text()
    if not manual_text:
        log.warning("[RAG] No hay texto de manual disponible")
        return None
    
    # 2. Estrategia de búsqueda inteligente
    question_lower = question.lower()
    
    # Palabras clave dinámicas basadas en la pregunta
    dynamic_keys = []
    base_keys = ["amplia plus", "amplia", "cobertura", "asistencia", "cristales", 
                "auto de reemplazo", "deducible", "responsabilidad", "robo", 
                "daños", "gastos médicos", "muerte", "invalidez", "terceros",
                "comparación", "vs", "diferencia", "incluye"]
    
    # Extraer palabras clave de la pregunta
    for key in base_keys:
        if key in question_lower:
            dynamic_keys.append(key)
    
    search_keys = dynamic_keys if dynamic_keys else base_keys
    
    # 3. Búsqueda semántica mejorada
    sections = manual_text.split('\n\n')
    relevant_sections = []
    
    for section in sections:
        if not section.strip() or len(section.strip()) < 20:
            continue
            
        section_lower = section.lower()
        relevance_score = 0
        
        # Calcular relevancia
        for key in search_keys:
            if key in section_lower:
                relevance_score += 2  # Peso mayor por coincidencia exacta
                
        # Bonus por términos importantes
        important_terms = ["amplia plus", "comparación", "vs", "diferencia"]
        for term in important_terms:
            if term in section_lower:
                relevance_score += 3
        
        if relevance_score > 0:
            relevant_sections.append((section, relevance_score))
    
    # 4. Seleccionar contenido más relevante
    if relevant_sections:
        # Ordenar por relevancia y tomar top 6
        relevant_sections.sort(key=lambda x: x[1], reverse=True)
        selected_content = "\n\n".join([section for section, score in relevant_sections[:6]])
        log.info(f"[RAG] Encontradas {len(relevant_sections)} secciones relevantes")
    else:
        # Fallback: buscar términos generales
        fallback_content = []
        for section in sections:
            if any(term in section.lower() for term in ["auto", "vehículo", "seguro", "cobertura", "póliza"]):
                fallback_content.append(section)
        selected_content = "\n\n".join(fallback_content[:8]) if fallback_content else manual_text[:6000]
        log.info("[RAG] Usando contenido de fallback")
    
    # 5. Limitar tamaño para el contexto
    if len(selected_content) > 6000:
        selected_content = selected_content[:6000] + "\n\n[... texto truncado por longitud ...]"
    
    # 6. Consulta a OpenAI con prompt mejorado
    try:
        prompt = (
            "Eres Vicky, una especialista en seguros de auto de Inbursa. "
            "Responde ÚNICAMENTE con base en la información del manual técnico proporcionado. "
            "SÉ PRECISA y no inventes información.\n\n"
            "REGLAS ESTRICTAS:\n"
            "1. Si la información NO está en el manual, di: 'No encontré esta información específica en el manual oficial'\n"
            "2. Usa viñetas (•) para listar coberturas y características\n"
            "3. Sé específica en comparaciones: menciona QUÉ incluye una cobertura vs otra\n"
            "4. Si el manual tiene tablas comparativas, descríbelas claramente\n"
            "5. Mantén la respuesta entre 100-500 palabras\n\n"
            f"PREGUNTA DEL CLIENTE: {question}\n\n"
            "INFORMACIÓN DEL MANUAL TÉCNICO:\n"
            "═══════════════════════════════════════════════════════════════\n"
            f"{selected_content}\n"
            "═══════════════════════════════════════════════════════════════\n\n"
            "RESPUESTA BASADA EN EL MANUAL:"
        )
        
        response = client_oa.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=800
        )
        
        answer = (response.choices[0].message.content or "").strip()
        
        if answer and not answer.startswith("No encontré") and len(answer) > 30:
            log.info(f"[RAG] Respuesta generada exitosamente ({len(answer)} caracteres)")
            return answer
        else:
            log.warning("[RAG] Respuesta insuficiente o no encontrada en manual")
            return None
            
    except Exception as e:
        log.exception(f"[RAG] Error en consulta OpenAI: {str(e)}")
        return None

# =========================
# Menú y flujos
# =========================
MAIN_MENU = (
    "🟦 *Vicky Bot — Inbursa*\n"
    "Elige una opción:\n"
    "1) Asesoría en pensiones IMSS\n"
    "2) Cotizador de seguro de auto\n"
    "3) Seguros de vida y salud\n"
    "4) Membresía médica VRIM\n"
    "5) Préstamos a pensionados IMSS ($10,000 a $650,000)\n"
    "6) Financiamiento empresarial\n"
    "7) Contactar con Christian\n\n"
    "Escribe el número u opción (ej. 'imss', 'auto', 'empresarial', 'contactar')."
)

def send_main_menu(phone: str) -> None:
    send_message(phone, MAIN_MENU)

def greet_with_match(phone: str, *, do_greet: bool = True) -> Optional[Dict[str, Any]]:
    """
    Saluda solo si no se saludó en la última ventana (24h).
    Guarda el match en contexto para reutilizarlo.
    """
    last10 = _normalize_last10(phone)
    match = sheet_match_by_last10(last10)

    now = datetime.utcnow()
    must_greet = do_greet and (
        phone not in greeted_at or (now - greeted_at.get(phone, now)) >= timedelta(hours=GREET_WINDOW_HOURS)
    )

    if must_greet:
        if match and match.get("nombre"):
            send_message(phone, f"Hola {match['nombre']} 👋 Soy *Vicky*. ¿En qué te puedo ayudar hoy?")
        else:
            send_message(phone, "Hola 👋 Soy *Vicky*. Estoy para ayudarte.")
        greeted_at[phone] = now

    # guarda en contexto
    ctx = ensure_ctx(phone)
    ctx["match"] = match
    return match

def flow_imss_info(phone: str, match: Optional[Dict[str, Any]]) -> None:
    user_state[phone] = "imss_q1"
    send_message(phone, "🟩 *Asesoría IMSS*\n¿Deseas conocer requisitos y cálculo aproximado? (sí/no)")

def flow_imss_next(phone: str, text: str) -> None:
    st = user_state.get(phone, "")
    ctx = ensure_ctx(phone)

    if st == "imss_q1":
        yn = interpret_yesno(text)
        if yn == "yes":
            user_state[phone] = "imss_pension"
            send_message(phone, "¿Cuál es tu *pensión mensual* aproximada? (ej. 8,500)")
        elif yn == "no":
            user_state[phone] = ""
            send_message(phone, "Entendido. Escribe *menú* para ver más opciones.")
        else:
            send_message(phone, "¿Me confirmas con *sí* o *no*?")
    elif st == "imss_pension":
        p = extract_number(text)
        if not p:
            send_message(phone, "No pude leer el monto. Indica tu pensión mensual (ej. 8500).")
            return
        ctx["imss_pension"] = p
        user_state[phone] = "imss_monto"
        send_message(phone, "Gracias. ¿Qué *monto* te gustaría solicitar? (entre $10,000 y $650,000)")
    elif st == "imss_monto":
        m = extract_number(text)
        if not m or m < 10000 or m > 650000:
            send_message(phone, "Ingresa un monto entre $10,000 y $650,000.")
            return
        ctx["imss_monto"] = m
        user_state[phone] = "imss_nombre"
        send_message(phone, "¿Tu *nombre completo*?")
    elif st == "imss_nombre":
        ctx["imss_nombre"] = (text or "").strip()
        user_state[phone] = "imss_ciudad"
        send_message(phone, "¿En qué *ciudad* te encuentras?")
    elif st == "imss_ciudad":
        ctx["imss_ciudad"] = (text or "").strip()
        user_state[phone] = "imss_nomina"
        send_message(phone, "¿Tienes *nómina Inbursa*? (sí/no)\n*No es obligatoria; otorga beneficios adicionales.*")
    elif st == "imss_nomina":
        yn = interpret_yesno(text)
        ctx["imss_nomina"] = ("sí" if yn == "yes" else "no")
        resumen = (
            "✅ *Preautorizado*. Un asesor te contactará.\n"
            f"- Nombre: {ctx.get('imss_nombre','')}\n"
            f"- Ciudad: {ctx.get('imss_ciudad','')}\n"
            f"- Pensión: ${ctx.get('imss_pension',0):,.0f}\n"
            f"- Monto deseado: ${ctx.get('imss_monto',0):,.0f}\n"
            f"- Nómina Inbursa: {ctx.get('imss_nomina','no')}"
        )
        send_message(phone, resumen)
        if NOTIFICAR_ASESOR:
            notify_advisor(f"🔔 IMSS — Prospecto preautorizado\nWhatsApp: {phone}\n{resumen}")
        user_state[phone] = ""
        send_main_menu(phone)

def flow_auto_start(phone: str, match: Optional[Dict[str, Any]]) -> None:
    user_state[phone] = "auto_intro"
    send_message(
        phone,
        "🚗 *Cotizador Auto*\nEnvíame:\n• INE (frente)\n• Tarjeta de circulación *o* número de placas.\n"
        "Si ya tienes póliza, dime la *fecha de vencimiento* (AAAA-MM-DD) para recordarte 30 días antes."
    )

def flow_auto_next(phone: str, text: str) -> None:
    st = user_state.get(phone, "")
    if st == "auto_intro":
        if re.search(r"\d{4}-\d{2}-\d{2}", text or ""):
            user_state[phone] = "auto_vto"
            flow_auto_next(phone, text)
        else:
            send_message(phone, "Perfecto. Envía documentos o escribe la fecha de vencimiento (AAAAA-MM-DD).")
    elif st == "auto_vto":
        try:
            date = datetime.fromisoformat(text.strip()).date()
            objetivo = date - timedelta(days=30)
            send_message(phone, f"✅ Gracias. Te contactaré *un mes antes* ({objetivo.isoformat()}).")
            def _reminder():
                try:
                    time.sleep(7 * 24 * 60 * 60)
                    send_message(phone, "⏰ ¿Deseas que coticemos tu seguro al acercarse el vencimiento?")
                except Exception:
                    pass
            threading.Thread(target=_reminder, daemon=True).start()
            user_state[phone] = ""
            send_main_menu(phone)
        except Exception:
            send_message(phone, "Formato inválido. Usa AAAA-MM-DD (ej. 2025-12-31).")

def flow_vida_salud(phone: str) -> None:
    send_message(phone, "🧬 *Seguros de Vida y Salud* — Gracias por tu interés. Notificaré al asesor para contactarte.")
    notify_advisor(f"🔔 Vida/Salud — Solicitud de contacto\nWhatsApp: {phone}")
    send_main_menu(phone)

def flow_vrim(phone: str) -> None:
    send_message(phone, "🩺 *VRIM* — Membresía médica con cobertura amplia. Notificaré al asesor para darte detalles.")
    notify_advisor(f"🔔 VRIM — Solicitud de contacto\nWhatsApp: {phone}")
    send_main_menu(phone)

def flow_prestamo_imss(phone: str, match: Optional[Dict[str, Any]]) -> None:
    user_state[phone] = "imss_monto_directo"
    send_message(phone, "🟩 *Préstamo IMSS (Ley 73)*\nIndica el *monto* deseado (entre $10,000 y $650,000).")

def flow_prestamo_imss_next(phone: str, text: str) -> None:
    st = user_state.get(phone, "")
    ctx = ensure_ctx(phone)
    if st == "imss_monto_directo":
        m = extract_number(text)
        if not m or m < 10000 or m > 650000:
            send_message(phone, "Ingresa un monto entre $10,000 y $650,000.")
            return
        ctx["imss_monto"] = m
        user_state[phone] = "imss_nombre_directo"
        send_message(phone, "¿Tu *nombre completo*?")
    elif st == "imss_nombre_directo":
        ctx["imss_nombre"] = (text or "").strip()
        user_state[phone] = "imss_ciudad_directo"
        send_message(phone, "¿En qué *ciudad* te encuentras?")
    elif st == "imss_ciudad_directo":
        ctx["imss_ciudad"] = (text or "").strip()
        user_state[phone] = "imss_nomina_directo"
        send_message(phone, "¿Tienes *nómina Inbursa*? (sí/no)\n*No es obligatoria; da beneficios adicionales.*")
    elif st == "imss_nomina_directo":
        yn = interpret_yesno(text)
        ctx["imss_nomina"] = ("sí" if yn == "yes" else "no")
        resumen = (
            "✅ *Preautorizado*. Un asesor te contactará.\n"
            f"- Nombre: {ctx.get('imss_nombre','')}\n"
            f"- Ciudad: {ctx.get('imss_ciudad','')}\n"
            f"- Monto deseado: ${ctx.get('imss_monto',0):,.0f}\n"
            f"- Nómina Inbursa: {ctx.get('imss_nomina','no')}"
        )
        send_message(phone, resumen)
        notify_advisor(f"🔔 IMSS — Solicitud préstamo\nWhatsApp: {phone}\n{resumen}")
        user_state[phone] = ""
        send_main_menu(phone)

def flow_empresarial(phone: str, match: Optional[Dict[str, Any]]) -> None:
    user_state[phone] = "emp_confirma"
    send_message(phone, "🟦 *Financiamiento Empresarial*\n¿Eres empresario(a) o representas una empresa? (sí/no)")

def flow_empresarial_next(phone: str, text: str) -> None:
    st = user_state.get(phone, "")
    ctx = ensure_ctx(phone)
    if st == "emp_confirma":
        yn = interpret_yesno(text)
        if yn != "yes":
            send_message(phone, "Entendido. Si necesitas otra cosa, escribe *menú*.")
            user_state[phone] = ""
            return
        user_state[phone] = "emp_giro"
        send_message(phone, "¿A qué *se dedica* tu empresa?")
    elif st == "emp_giro":
        ctx["emp_giro"] = (text or "").strip()
        user_state[phone] = "emp_monto"
        send_message(phone, "¿Qué *monto* necesitas? (mínimo $100,000)")
    elif st == "emp_monto":
        m = extract_number(text)
        if not m or m < 100000:
            send_message(phone, "El monto mínimo es $100,000. Indica un monto igual o mayor.")
            return
        ctx["emp_monto"] = m
        user_state[phone] = "emp_nombre"
        send_message(phone, "¿Tu *nombre completo*?")
    elif st == "emp_nombre":
        ctx["emp_nombre"] = (text or "").strip()
        user_state[phone] = "emp_ciudad"
        send_message(phone, "¿Tu *ciudad*?")
    elif st == "emp_ciudad":
        ctx["emp_ciudad"] = (text or "").strip()
        resumen = (
            "✅ Gracias. Un asesor te contactará.\n"
            f"- Nombre: {ctx.get('emp_nombre','')}\n"
            f"- Ciudad: {ctx.get('emp_ciudad','')}\n"
            f"- Giro: {ctx.get('emp_giro','')}\n"
            f"- Monto: ${ctx.get('emp_monto',0):,.0f}"
        )
        send_message(phone, resumen)
        notify_advisor(f"🔔 Empresarial — Nueva solicitud\nWhatsApp: {phone}\n{resumen}")
        user_state[phone] = ""
        send_main_menu(phone)

def flow_contacto(phone: str) -> None:
    send_message(phone, "✅ Listo. Avisé a Christian para que te contacte.")
    notify_advisor(f"🔔 Contacto directo — Cliente solicita hablar\nWhatsApp: {phone}")
    send_main_menu(phone)

# =========================
# Router principal
# =========================
def route_command(phone: str, text: str, match: Optional[Dict[str, Any]]) -> None:
    t = (text or "").strip().lower()

    # --- RAG MEJORADO para preguntas de AUTO (coberturas) ---
    if any(k in t for k in ["amplia plus", "amplia", "cobertura", "coberturas", "cristales", 
                        "asistencia", "auto de reemplazo", "deducible", "qué incluye",
                        "qué cubre", "diferencia entre", "vs", "comparar"]):
        rag_ans = answer_auto_from_manual(text or t)
        if rag_ans:
            send_message(phone, rag_ans)
            return
    # -----------------------------------------------------

    if t in ("menu", "menú", "inicio", "hola"):
        user_state[phone] = ""
        send_main_menu(phone)
        return

    if t in ("1", "asesoría imss", "asesoria imss", "imss", "pensión", "pension"):
        flow_imss_info(phone, match)
        return
    if t in ("2", "auto", "seguro auto", "cotización auto", "cotizacion auto"):
        flow_auto_start(phone, match)
        return
    if t in ("3", "vida", "salud", "vida y salud"):
        flow_vida_salud(phone)
        return
    if t in ("4", "vrim", "membresía", "membresia"):
        flow_vrim(phone)
        return
    if t in ("5", "préstamo", "prestamo", "préstamo imss", "prestamo imss"):
        flow_prestamo_imss(phone, match)
        return
    if t in ("6", "empresarial", "empresa", "financiamiento empresarial"):
        flow_empresarial(phone, match)
        return
    if t in ("7", "contactar", "christian", "asesor", "humano"):
        flow_contacto(phone)
        return

    # Si está en flujo, continúa
    st = user_state.get(phone, "")
    if st.startswith("imss_"):
        flow_imss_next(phone, text)
    elif st.startswith("auto_"):
        flow_auto_next(phone, text)
    elif st.startswith("emp_"):
        flow_empresarial_next(phone, text)
    elif st.startswith("imss_monto_directo") or st.startswith("imss_nombre_directo") or st.startswith("imss_ciudad_directo") or st.startswith("imss_nomina_directo"):
        flow_prestamo_imss_next(phone, text)
    else:
        send_message(phone, "No entendí. Escribe *menú* para ver opciones.")

# =========================
# Webhook
# =========================
@app.post("/webhook")
def webhook_receive():
    try:
        payload = request.get_json(force=True, silent=True) or {}
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

        # ✅ NUEVA LÓGICA MEJORADA - GESTIÓN DE ESTADO
        current_time = datetime.utcnow()
        ctx = ensure_ctx(phone)
        
        # Verificar si debemos saludar (solo una vez cada 24h)
        last_greeting = greeted_at.get(phone)
        should_greet = (
            last_greeting is None or 
            (current_time - last_greeting) >= timedelta(hours=GREET_WINDOW_HOURS)
        )
        
        # Obtener match de Google Sheets solo si no existe
        if "match" not in ctx or ctx["match"] is None:
            last10 = _normalize_last10(phone)
            match = sheet_match_by_last10(last10)
            ctx["match"] = match
        else:
            match = ctx["match"]
        
        # Saludar SOLO si corresponde y es el primer mensaje de la conversación
        if should_greet:
            if match and match.get("nombre"):
                send_message(phone, f"Hola {match['nombre']} 👋 Soy *Vicky*. ¿En qué te puedo ayudar hoy?")
            else:
                send_message(phone, "Hola 👋 Soy *Vicky*. Estoy para ayudarte.")
            greeted_at[phone] = current_time
            # NO establecer user_state aquí para evitar reset

        # ... resto del código existente para procesar tipos de mensaje ...
        
        # Logging mejorado
        log.info(f"[Webhook] Mensaje de {phone}: {msg.get('type', 'unknown')}")

        # Procesar tipo de mensaje
        msg_type = msg.get("type", "")
        if msg_type == "text":
            text = (msg.get("text", {}).get("body", "") or "").strip()
            route_command(phone, text, match)
        elif msg_type == "interactive":
            # Botones
            btn = msg.get("interactive", {}).get("button_reply", {}).get("title", "")
            if btn:
                route_command(phone, btn, match)
        elif msg_type in ("image", "document"):
            # Para auto: INE, tarjeta circulación
            send_message(phone, "✅ Recibí tu documento. Lo revisaré y te contacto pronto.")
            notify_advisor(f"🔔 Documento recibido\nWhatsApp: {phone}")
        else:
            send_message(phone, "⚠️ Solo puedo leer texto, imágenes o documentos. Escribe *menú* para opciones.")

        return jsonify({"ok": True}), 200
    except Exception:
        log.exception("Error en webhook_receive")
        return jsonify({"ok": False}), 500

@app.get("/webhook")
def webhook_verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        log.info("Webhook verificado")
        return challenge, 200
    return "Verificación fallida", 403

# =========================
# Health check mejorado
# =========================
@app.get("/ext/health")
def ext_health():
    manual_status = "loaded" if _manual_auto_cache.get("text") else "empty"
    return jsonify({
        "status": "ok",
        "timestamp": datetime.utcnow().isoformat(),
        "whatsapp_configured": bool(META_TOKEN and WABA_PHONE_ID),
        "google_ready": google_ready,
        "openai_ready": bool(client_oa is not None),
        "rag_status": manual_status,
        "rag_file": _manual_auto_cache.get("file_name"),
        "sheet_name": GOOGLE_SHEET_NAME,
        "manuales_folder": bool(MANUALES_VICKY_FOLDER_ID),
    }), 200

@app.get("/")
def home():
    return jsonify({"name": "Vicky Bot SECOM", "version": "2024.12.19"}), 200

# =========================
# Inicio
# =========================
if __name__ == "__main__":
    log.info(f"Iniciando Vicky Bot SECOM en puerto {PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False)

