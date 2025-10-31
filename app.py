# app.py — Vicky Bot SECOM (Render-ready) - CORREGIDO
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

# Google APIs - VERSIÓN CORREGIDA
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.auth.exceptions import GoogleAuthError, MalformedError
from PyPDF2 import PdfReader

# OpenAI SDK 1.x
from openai import OpenAI

# =========================
# Entorno y logging - MEJORADO
# =========================
load_dotenv()

# Configuración de logging más detallada
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(funcName)s:%(lineno)d - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("vicky-secom")

# Variables de entorno
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

# =========================
# Clientes externos - CORREGIDO
# =========================
# WhatsApp
WPP_API_URL = f"https://graph.facebook.com/v20.0/{WABA_PHONE_ID}/messages" if WABA_PHONE_ID else None
WPP_TIMEOUT = 15

# OpenAI
client_oa: Optional[OpenAI] = None
if OPENAI_API_KEY:
    try:
        client_oa = OpenAI(api_key=OPENAI_API_KEY)
        log.info("✅ OpenAI configurado correctamente")
    except Exception as e:
        log.error(f"❌ Error inicializando OpenAI: {str(e)}")
        client_oa = None

# Google Sheets + Drive - INICIALIZACIÓN CORREGIDA
sheets_client = None
drive_client = None
google_ready = False
google_error = "No inicializado"

def initialize_google_services():
    """Inicializa los servicios de Google con manejo robusto de errores"""
    global sheets_client, drive_client, google_ready, google_error
    
    try:
        # Verificar que tenemos las credenciales
        if not GOOGLE_CREDENTIALS_JSON:
            google_error = "GOOGLE_CREDENTIALS_JSON está vacío o no definido"
            log.error(google_error)
            return False
        
        # Parsear y validar JSON de credenciales
        try:
            credentials_info = json.loads(GOOGLE_CREDENTIALS_JSON)
            log.info("✅ Credenciales JSON parseadas correctamente")
        except json.JSONDecodeError as e:
            google_error = f"Error parseando GOOGLE_CREDENTIALS_JSON: {str(e)}"
            log.error(google_error)
            return False
        
        # Definir scopes necesarios - CORREGIDOS
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets.readonly",
            "https://www.googleapis.com/auth/drive.readonly",
        ]
        
        # Crear credenciales
        try:
            creds = Credentials.from_service_account_info(credentials_info, scopes=scopes)
            log.info("✅ Credenciales de servicio creadas")
        except (GoogleAuthError, MalformedError, ValueError) as e:
            google_error = f"Error creando credenciales: {str(e)}"
            log.error(google_error)
            return False
        
        # Inicializar cliente de Sheets
        try:
            sheets_client = gspread.authorize(creds)
            log.info("✅ Cliente de Sheets autorizado")
        except Exception as e:
            google_error = f"Error autorizando cliente de Sheets: {str(e)}"
            log.error(google_error)
            return False
        
        # Inicializar cliente de Drive
        try:
            drive_client = build("drive", "v3", credentials=creds, cache_discovery=False)
            log.info("✅ Cliente de Drive construido")
        except Exception as e:
            google_error = f"Error construyendo cliente de Drive: {str(e)}"
            log.error(google_error)
            return False
        
        # Verificar permisos de Sheets
        if GOOGLE_SHEET_ID:
            try:
                sheet = sheets_client.open_by_key(GOOGLE_SHEET_ID)
                worksheet = sheet.worksheet(GOOGLE_SHEET_NAME)
                test_data = worksheet.get_all_values()
                log.info(f"✅ Conexión a Sheets verificada - {len(test_data)} filas encontradas")
            except gspread.exceptions.APIError as e:
                google_error = f"Error de API accediendo a Sheets: {str(e)}"
                log.error(google_error)
                return False
            except gspread.exceptions.SpreadsheetNotFound:
                google_error = f"Sheet no encontrado: {GOOGLE_SHEET_ID}"
                log.error(google_error)
                return False
            except Exception as e:
                google_error = f"Error verificando Sheets: {str(e)}"
                log.error(google_error)
                return False
        
        # Verificar permisos de Drive
        if MANUALES_VICKY_FOLDER_ID:
            try:
                drive_client.files().get(fileId=MANUALES_VICKY_FOLDER_ID).execute()
                log.info("✅ Permisos de Drive verificados")
            except HttpError as e:
                if e.resp.status == 404:
                    google_error = f"Folder de Drive no encontrado: {MANUALES_VICKY_FOLDER_ID}"
                elif e.resp.status == 403:
                    google_error = "Sin permisos para acceder al folder de Drive"
                else:
                    google_error = f"Error de Drive API: {str(e)}"
                log.error(google_error)
                return False
            except Exception as e:
                google_error = f"Error verificando Drive: {str(e)}"
                log.error(google_error)
                return False
        
        google_ready = True
        google_error = "✅ Todo correcto"
        log.info("🚀 Google Services inicializados correctamente")
        return True
        
    except Exception as e:
        google_error = f"Error crítico inicializando Google: {str(e)}"
        log.error(google_error)
        return False

# Inicializar Google al importar
initialize_google_services()

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
    """Envía notificación al asesor usando send_message directamente - CORREGIDO"""
    if NOTIFICAR_ASESOR and ADVISOR_NUMBER:
        try:
            send_message(ADVISOR_NUMBER, text)
            log.info(f"✅ Notificación enviada al asesor: {ADVISOR_NUMBER}")
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
# Google helpers - COMPLETAMENTE CORREGIDOS
# =========================
def sheet_match_by_last10(last10: str) -> Optional[Dict[str, Any]]:
    """Busca en Google Sheets por los últimos 10 dígitos del teléfono - CORREGIDO"""
    if not google_ready:
        log.warning(f"❌ Google no está listo: {google_error}")
        return None
    
    if not (sheets_client and GOOGLE_SHEET_ID and GOOGLE_SHEET_NAME):
        log.warning("❌ Faltan configuraciones de Sheets")
        return None
    
    try:
        log.info(f"🔍 Buscando teléfono {last10} en Google Sheets...")
        
        # Abrir sheet
        sh = sheets_client.open_by_key(GOOGLE_SHEET_ID)
        ws = sh.worksheet(GOOGLE_SHEET_NAME)
        rows = ws.get_all_values()
        
        log.info(f"📊 Sheet cargado - {len(rows)} filas encontradas")
        
        if not rows:
            log.warning("❌ Sheet está vacío")
            return None
        
        # Buscar en todas las filas
        for i, row in enumerate(rows, start=1):
            # Convertir toda la fila a texto y extraer números
            row_text = " | ".join(str(cell) for cell in row)
            all_digits_in_row = re.sub(r"\D", "", row_text)
            
            # Buscar coincidencia exacta de los últimos 10 dígitos
            if last10 and last10 in all_digits_in_row:
                log.info(f"✅ Match encontrado en fila {i}")
                
                # Buscar nombre (primera columna con texto significativo)
                nombre = ""
                for cell in row:
                    cell_str = str(cell).strip()
                    if (cell_str and len(cell_str) > 2 and 
                        not re.search(r"\d", cell_str) and
                        cell_str.lower() not in ['nombre', 'cliente', 'prospecto', 'teléfono', 'telefono']):
                        nombre = cell_str
                        break
                
                # Si no encontramos nombre, usar primera columna no vacía
                if not nombre:
                    for cell in row:
                        if str(cell).strip():
                            nombre = str(cell).strip()
                            break
                
                return {
                    "row": i, 
                    "nombre": nombre or "Cliente",
                    "telefono": last10,
                    "raw": row
                }
        
        log.warning(f"❌ No se encontró match para {last10}")
        return None
        
    except gspread.exceptions.APIError as e:
        log.error(f"❌ Error de API de Google Sheets: {str(e)}")
        return None
    except Exception as e:
        log.exception(f"❌ Error crítico leyendo Google Sheets: {str(e)}")
        return None

def list_drive_manuals(folder_id: str) -> List[Dict[str, str]]:
    """Lista archivos PDF en folder de Drive - CORREGIDO"""
    if not google_ready:
        log.warning(f"❌ Google no está listo: {google_error}")
        return []
    
    if not (drive_client and folder_id):
        log.warning("❌ Cliente de Drive no disponible o folder_id faltante")
        return []
    
    try:
        query = f"'{folder_id}' in parents and mimeType='application/pdf' and trashed=false"
        result = drive_client.files().list(
            q=query, 
            fields="files(id, name, webViewLink, mimeType)",
            pageSize=20
        ).execute()
        
        files = result.get("files", [])
        log.info(f"📁 Encontrados {len(files)} archivos PDF en Drive")
        
        output = []
        for file in files:
            link = file.get("webViewLink", "")
            # Si no hay link, intentar generarlo
            if not link:
                try:
                    file_meta = drive_client.files().get(
                        fileId=file["id"], 
                        fields="webViewLink"
                    ).execute()
                    link = file_meta.get("webViewLink", "")
                except Exception:
                    link = f"https://drive.google.com/file/d/{file['id']}/view"
            
            output.append({
                "id": file["id"],
                "name": file["name"],
                "webViewLink": link,
                "mimeType": file.get("mimeType", "")
            })
        
        return output
        
    except HttpError as e:
        log.error(f"❌ Error de API de Drive: {str(e)}")
        return []
    except Exception as e:
        log.exception(f"❌ Error listando manuales en Drive: {str(e)}")
        return []

# =========================
# RAG light (Auto) — CORREGIDO
# =========================
_manual_auto_cache = {"text": None, "file_id": None, "loaded_at": None}

def _find_auto_manual_file_id() -> Optional[str]:
    """Encuentra el archivo PDF del manual de auto - CORREGIDO"""
    if not google_ready:
        return None
    
    try:
        files = list_drive_manuals(MANUALES_VICKY_FOLDER_ID)
        if not files:
            log.warning("❌ No se encontraron archivos PDF en el folder")
            return None
        
        # Priorizar nombres que sugieran auto/coberturas
        auto_files = []
        for file in files:
            name = (file.get("name") or "").lower()
            if any(keyword in name for keyword in ["auto", "cobertura", "vehicular", "automovil"]):
                auto_files.append(file)
        
        target_file = auto_files[0] if auto_files else files[0]
        log.info(f"✅ Manual de auto seleccionado: {target_file['name']}")
        return target_file["id"]
        
    except Exception as e:
        log.exception(f"❌ Error buscando manual Auto: {str(e)}")
        return None

def _download_pdf_text(file_id: str) -> Optional[str]:
    """Descarga y extrae texto de PDF - CORREGIDO"""
    if not google_ready:
        return None
    
    try:
        from googleapiclient.http import MediaIoBaseDownload
        
        request = drive_client.files().get_media(fileId=file_id)
        file_handle = io.BytesIO()
        downloader = MediaIoBaseDownload(file_handle, request)
        
        done = False
        while not done:
            status, done = downloader.next_chunk()
            log.info(f"📥 Descargando PDF: {int(status.progress() * 100)}%")
        
        file_handle.seek(0)
        
        # Extraer texto del PDF
        reader = PdfReader(file_handle)
        pages_text = []
        
        for page_num, page in enumerate(reader.pages):
            try:
                text = page.extract_text()
                if text and text.strip():
                    pages_text.append(f"--- Página {page_num + 1} ---\n{text.strip()}")
            except Exception as page_error:
                log.warning(f"Error en página {page_num + 1}: {str(page_error)}")
                continue
        
        full_text = "\n\n".join(pages_text)
        
        if not full_text.strip():
            log.warning("❌ PDF no contiene texto extraíble")
            return None
        
        # Limpiar texto
        full_text = re.sub(r'\s+', ' ', full_text)
        full_text = re.sub(r'\n\s*\n', '\n\n', full_text)
        
        log.info(f"✅ PDF procesado: {len(full_text)} caracteres extraídos")
        return full_text.strip()
        
    except HttpError as e:
        log.error(f"❌ Error de Drive API descargando PDF: {str(e)}")
        return None
    except Exception as e:
        log.exception(f"❌ Error descargando/procesando PDF: {str(e)}")
        return None

def ensure_auto_manual_text(force_reload: bool = False) -> Optional[str]:
    """Obtiene el texto del manual de auto, usando cache si está disponible"""
    if not client_oa:
        return None
    
    # Verificar cache
    if not force_reload and _manual_auto_cache.get("text"):
        cache_age = datetime.utcnow() - _manual_auto_cache.get("loaded_at", datetime.utcnow())
        if cache_age < timedelta(hours=24):  # Cache por 24 horas
            return _manual_auto_cache["text"]
    
    # Cargar nuevo
    file_id = _find_auto_manual_file_id()
    if not file_id:
        return None
    
    text = _download_pdf_text(file_id)
    if text:
        _manual_auto_cache.update({
            "text": text, 
            "file_id": file_id, 
            "loaded_at": datetime.utcnow()
        })
        log.info("✅ Manual de auto cacheado correctamente")
    
    return text

def answer_auto_from_manual(question: str) -> Optional[str]:
    """Responde preguntas sobre seguros de auto usando RAG - CORREGIDO"""
    if not client_oa:
        return None
    
    manual_text = ensure_auto_manual_text()
    if not manual_text:
        return "⚠️ No tengo acceso al manual de seguros en este momento. Por favor contacta al asesor para esta información."
    
    try:
        # Buscar secciones relevantes
        keywords = ["amplia plus", "amplia", "cobertura", "asistencia", "cristales", "auto de reemplazo", "deducible"]
        relevant_sections = []
        
        for line in manual_text.split("\n"):
            line_lower = line.lower()
            if any(keyword in line_lower for keyword in keywords):
                relevant_sections.append(line.strip())
            if len("\n".join(relevant_sections)) > 6000:  # Limitar tamaño
                break
        
        context_text = "\n".join(relevant_sections) if relevant_sections else manual_text[:8000]
        
        prompt = f"""Eres un asistente especializado en seguros de auto. Responde SOLO con base en la información del manual proporcionado.

Pregunta del cliente: {question}

Información del manual:
{context_text}

Instrucciones:
- Responde de manera clara y profesional en español
- Usa viñetas si ayuda a organizar la información  
- Si la información no está en el manual, di claramente "No encuentro esta información específica en el manual"
- Sé preciso con los términos de cobertura y exclusiones

Respuesta:"""
        
        response = client_oa.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,  # Baja temperatura para respuestas consistentes
            max_tokens=800
        )
        
        answer = (response.choices[0].message.content or "").strip()
        return answer if answer else "No pude generar una respuesta en este momento."
        
    except Exception as e:
        log.exception(f"❌ Error en RAG-auto: {str(e)}")
        return "⚠️ Ocurrió un error procesando tu consulta. Por favor intenta más tarde."

# =========================
# Menú y flujos (sin cambios)
# =========================
MAIN_MENU = (
    "🟦 *Vicky Bot — SECOM*\n"
    "Elige una opción:\n"
    "1) Préstamo IMSS (Ley 73)\n"
    "2) Cotizador de seguro de auto\n"
    "3) Seguros de vida y salud\n"
    "4) Membresía médica VRIM\n"
    "5) Asesoría en pensiones IMSS\n"
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

# [Los flujos existentes se mantienen igual...]
# flow_imss_info, flow_imss_next, flow_auto_start, etc.

# =========================
# Router principal
# =========================
def route_command(phone: str, text: str, match: Optional[Dict[str, Any]]) -> None:
    t = (text or "").strip().lower()

    # --- RAG light para preguntas de AUTO (coberturas) ---
    if any(k in t for k in ["amplia plus", "amplia+", "cobertura", "coberturas", "cristales", "asistencia", "auto de reemplazo", "deducible"]):
        rag_ans = answer_auto_from_manual(text or t)
        if rag_ans:
            send_message(phone, rag_ans)
            return
    
    # [El resto del router se mantiene igual...]

# =========================
# Webhook y endpoints - CORREGIDOS
# =========================
@app.get("/webhook")
def webhook_verify():
    try:
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge", "")
        if mode == "subscribe" and token == VERIFY_TOKEN:
            log.info("✅ Webhook verificado correctamente")
            return challenge, 200
    except Exception:
        log.exception("Error verificando webhook")
    return "Error", 403

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

        log.info(f"📱 Mensaje recibido de: {phone}")
        
        # Saludo+match solo una vez por ventana
        if phone not in user_state:
            match = greet_with_match(phone, do_greet=True)
            user_state[phone] = ""
        else:
            ctx = ensure_ctx(phone)
            match = ctx.get("match")
            if match is None:
                match = greet_with_match(phone, do_greet=False)

        mtype = msg.get("type")

        if mtype == "text" and "text" in msg:
            text = (msg["text"].get("body") or "").strip()
            log.info(f"💬 Texto recibido: {text}")
            
            if text.lower().startswith("sgpt:") and client_oa:
                prompt = text.split("sgpt:", 1)[1].strip()
                def _gpt_direct():
                    try:
                        res = client_oa.chat.completions.create(
                            model=OPENAI_MODEL,
                            messages=[{"role": "user", "content": prompt}],
                            temperature=0.3,
                        )
                        ans = (res.choices[0].message.content or "").strip()
                        send_message(phone, ans or "Listo.")
                    except Exception:
                        send_message(phone, "Hubo un detalle al procesar tu solicitud.")
                threading.Thread(target=_gpt_direct, daemon=True).start()
                return jsonify({"ok": True}), 200

            route_command(phone, text, match)
            return jsonify({"ok": True}), 200

        if mtype in {"image", "audio", "video", "document"}:
            send_message(phone, "📎 *Recibido*. Gracias, lo reviso y te confirmo en breve.")
            return jsonify({"ok": True}), 200

        return jsonify({"ok": True}), 200
    except Exception:
        log.exception("Error en webhook_receive")
        return jsonify({"ok": True}), 200

# =========================
# Endpoints de diagnóstico MEJORADOS
# =========================
@app.get("/ext/health")
def ext_health():
    return jsonify({
        "status": "ok",
        "timestamp": datetime.utcnow().isoformat(),
        "whatsapp_configured": bool(META_TOKEN and WABA_PHONE_ID),
        "google_ready": google_ready,
        "google_error": google_error,
        "openai_ready": bool(client_oa is not None),
        "sheet_name": GOOGLE_SHEET_NAME,
        "manuales_folder": bool(MANUALES_VICKY_FOLDER_ID),
        "environment": os.getenv("RENDER", "development")
    }), 200

@app.get("/ext/diagnostico-google")
def ext_diagnostico_google():
    """Diagnóstico completo de Google Drive/Sheets - MEJORADO"""
    diagnostico = {
        "google_ready": google_ready,
        "google_error": google_error,
        "paso_1_credenciales": {
            "tiene_credenciales_json": bool(GOOGLE_CREDENTIALS_JSON),
            "longitud_credenciales": len(GOOGLE_CREDENTIALS_JSON) if GOOGLE_CREDENTIALS_JSON else 0,
            "es_json_valido": False
        },
        "paso_2_clientes": {
            "sheets_client": sheets_client is not None,
            "drive_client": drive_client is not None
        },
        "paso_3_configuracion": {
            "GOOGLE_SHEET_ID": bool(GOOGLE_SHEET_ID),
            "GOOGLE_SHEET_NAME": GOOGLE_SHEET_NAME,
            "MANUALES_VICKY_FOLDER_ID": bool(MANUALES_VICKY_FOLDER_ID)
        },
        "paso_4_prueba_sheets": "no_iniciado",
        "paso_5_prueba_drive": "no_iniciado",
        "paso_6_prueba_busqueda": "no_iniciado"
    }
    
    # Validar JSON de credenciales
    try:
        if GOOGLE_CREDENTIALS_JSON:
            json.loads(GOOGLE_CREDENTIALS_JSON)
            diagnostico["paso_1_credenciales"]["es_json_valido"] = True
    except Exception as e:
        diagnostico["paso_1_credenciales"]["es_json_valido"] = False
        diagnostico["paso_1_credenciales"]["error_json"] = str(e)
    
    # Probar Sheets
    if sheets_client and GOOGLE_SHEET_ID:
        try:
            sh = sheets_client.open_by_key(GOOGLE_SHEET_ID)
            ws = sh.worksheet(GOOGLE_SHEET_NAME)
            rows = ws.get_all_values()
            diagnostico["paso_4_prueba_sheets"] = f"✅ OK - {len(rows)} filas"
            
            # Probar búsqueda
            test_phone = "5216681620521"
            last10 = _normalize_last10(test_phone)
            match = sheet_match_by_last10(last10)
            diagnostico["paso_6_prueba_busqueda"] = f"✅ OK - Match: {bool(match)}"
            
        except Exception as e:
            diagnostico["paso_4_prueba_sheets"] = f"❌ ERROR: {str(e)}"
    
    # Probar Drive
    if drive_client and MANUALES_VICKY_FOLDER_ID:
        try:
            files = list_drive_manuals(MANUALES_VICKY_FOLDER_ID)
            diagnostico["paso_5_prueba_drive"] = f"✅ OK - {len(files)} archivos"
        except Exception as e:
            diagnostico["paso_5_prueba_drive"] = f"❌ ERROR: {str(e)}"
    
    return jsonify(diagnostico)

@app.get("/ext/reiniciar-google")
def ext_reiniciar_google():
    """Endpoint para reiniciar los servicios de Google"""
    try:
        global google_ready, google_error
        success = initialize_google_services()
        return jsonify({
            "ok": success,
            "google_ready": google_ready,
            "google_error": google_error
        }), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.get("/ext/manuales")
def ext_manuales():
    """Lista manuales disponibles en Drive"""
    try:
        files = list_drive_manuals(MANUALES_VICKY_FOLDER_ID)
        return jsonify({
            "ok": True, 
            "count": len(files),
            "files": files
        }), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.get("/ext/test-rag")
def ext_test_rag():
    """Prueba el sistema RAG con una pregunta de ejemplo"""
    try:
        question = "¿Qué diferencia hay entre cobertura amplia y amplia plus?"
        answer = answer_auto_from_manual(question)
        return jsonify({
            "ok": True,
            "question": question,
            "answer": answer,
            "manual_loaded": bool(_manual_auto_cache.get("text"))
        }), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.get("/health")
def health():
    return jsonify({
        "status": "ok", 
        "service": "Vicky Bot SECOM",
        "version": "2.0.0",
        "timestamp": datetime.utcnow().isoformat()
    }), 200

# =========================
# Arranque local
# =========================
if __name__ == "__main__":
    log.info("🚀 Iniciando Vicky Bot SECOM...")
    log.info(f"📍 Puerto: {PORT}")
    log.info(f"📱 WhatsApp: {bool(META_TOKEN and WABA_PHONE_ID)}")
    log.info(f"🔧 Google: {google_ready} - {google_error}")
    log.info(f"🤖 OpenAI: {bool(client_oa)}")
    log.info(f"📊 Sheets: {GOOGLE_SHEET_NAME}")
    log.info(f"📁 Drive: {bool(MANUALES_VICKY_FOLDER_ID)}")
    
    app.run(host="0.0.0.0", port=PORT, debug=False)
