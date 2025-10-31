# app.py — Vicky Bot SECOM (Render-ready) - VERSIÓN DEFINITIVA
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

# Google APIs
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from PyPDF2 import PdfReader

# OpenAI SDK 1.x
from openai import OpenAI

# =========================
# Entorno y logging
# =========================
load_dotenv()

# Configuración de logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("vicky-secom")

# Variables de entorno - USANDO TUS VARIABLES EXACTAS
META_TOKEN = os.getenv("META_TOKEN", "").strip()
WABA_PHONE_ID = os.getenv("WABA_PHONE_ID", "").strip()
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "").strip()
ADVISOR_NUMBER = os.getenv("ADVISOR_NUMBER", "").strip()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("GPT_MODEL", "gpt-4o-mini").strip()

# Google Sheets - VARIABLES CORRECTAS DE RENDER
GOOGLE_SHEET_ID = os.getenv("SHEET_ID_SECOM", "").strip()
GOOGLE_SHEET_NAME = os.getenv("SHEET_TITLE_SECOM", "Prospectos SECOM Auto").strip()
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON", "").strip()

# Drive - VARIABLE CORRECTA
MANUALES_VICKY_FOLDER_ID = os.getenv("DRIVE_FOLDER_ID", "").strip()

NOTIFICAR_ASESOR = os.getenv("NOTIFICAR_ASESOR", "true").lower() == "true"
PORT = int(os.getenv("PORT", "5000"))

# =========================
# Clientes externos
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

# Google Sheets + Drive
sheets_client = None
drive_client = None
google_ready = False
google_error = "No inicializado"

def initialize_google_services():
    """Inicializa los servicios de Google"""
    global sheets_client, drive_client, google_ready, google_error
    
    try:
        # Verificar credenciales
        if not GOOGLE_CREDENTIALS_JSON:
            google_error = "GOOGLE_CREDENTIALS_JSON no configurado"
            log.error(google_error)
            return False
        
        # Parsear credenciales
        try:
            credentials_info = json.loads(GOOGLE_CREDENTIALS_JSON)
            log.info("✅ Credenciales JSON parseadas correctamente")
        except json.JSONDecodeError as e:
            google_error = f"Error parseando JSON: {str(e)}"
            log.error(google_error)
            return False
        
        # Scopes necesarios
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets.readonly",
            "https://www.googleapis.com/auth/drive.readonly",
        ]
        
        # Crear credenciales
        creds = Credentials.from_service_account_info(credentials_info, scopes=scopes)
        
        # Inicializar Sheets
        sheets_client = gspread.authorize(creds)
        log.info("✅ Cliente de Sheets autorizado")
        
        # Inicializar Drive
        drive_client = build("drive", "v3", credentials=creds, cache_discovery=False)
        log.info("✅ Cliente de Drive construido")
        
        # Verificar Sheets
        if GOOGLE_SHEET_ID:
            try:
                sheet = sheets_client.open_by_key(GOOGLE_SHEET_ID)
                worksheet = sheet.worksheet(GOOGLE_SHEET_NAME)
                test_data = worksheet.get_all_values()
                log.info(f"✅ Sheets verificado - {len(test_data)} filas")
            except Exception as e:
                google_error = f"Error accediendo a Sheets: {str(e)}"
                log.error(google_error)
                return False
        else:
            log.warning("⚠️ SHEET_ID_SECOM no configurado")
        
        # Verificar Drive
        if MANUALES_VICKY_FOLDER_ID:
            try:
                drive_client.files().get(fileId=MANUALES_VICKY_FOLDER_ID).execute()
                log.info("✅ Drive verificado")
            except Exception as e:
                log.warning(f"⚠️ Drive folder no accesible: {str(e)}")
        else:
            log.warning("⚠️ DRIVE_FOLDER_ID no configurado")
        
        google_ready = True
        google_error = "✅ Servicios Google inicializados"
        log.info("🚀 Google Services listos")
        return True
        
    except Exception as e:
        google_error = f"Error inicializando Google: {str(e)}"
        log.error(google_error)
        return False

# Inicializar Google
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
        log.error("WhatsApp no configurado")
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
            log.info(f"✅ Notificación enviada al asesor")
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
    """Busca en Google Sheets por teléfono"""
    if not google_ready:
        log.warning(f"Google no listo: {google_error}")
        return None
    
    if not (sheets_client and GOOGLE_SHEET_ID):
        log.warning("Faltan configuraciones de Sheets")
        return None
    
    try:
        log.info(f"Buscando teléfono {last10}")
        
        sh = sheets_client.open_by_key(GOOGLE_SHEET_ID)
        ws = sh.worksheet(GOOGLE_SHEET_NAME)
        rows = ws.get_all_values()
        
        log.info(f"Sheet cargado - {len(rows)} filas")
        
        # Buscar en filas
        for i, row in enumerate(rows, start=1):
            row_text = " | ".join(str(cell) for cell in row)
            all_digits_in_row = re.sub(r"\D", "", row_text)
            
            if last10 and last10 in all_digits_in_row:
                log.info(f"✅ Match en fila {i}")
                
                # Buscar nombre
                nombre = ""
                for cell in row:
                    cell_str = str(cell).strip()
                    if (cell_str and len(cell_str) > 2 and 
                        not re.search(r"\d", cell_str) and
                        cell_str.lower() not in ['nombre', 'cliente', 'prospecto', 'teléfono']):
                        nombre = cell_str
                        break
                
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
        
        log.warning(f"No match para {last10}")
        return None
        
    except Exception as e:
        log.error(f"Error leyendo Sheets: {str(e)}")
        return None

def list_drive_manuals(folder_id: str) -> List[Dict[str, str]]:
    if not google_ready or not drive_client or not folder_id:
        return []
    
    try:
        query = f"'{folder_id}' in parents and mimeType='application/pdf' and trashed=false"
        result = drive_client.files().list(
            q=query, 
            fields="files(id, name, webViewLink)",
            pageSize=20
        ).execute()
        
        files = result.get("files", [])
        log.info(f"Encontrados {len(files)} PDFs")
        
        output = []
        for file in files:
            link = file.get("webViewLink", "")
            if not link:
                link = f"https://drive.google.com/file/d/{file['id']}/view"
            
            output.append({
                "id": file["id"],
                "name": file["name"],
                "webViewLink": link
            })
        
        return output
        
    except Exception as e:
        log.error(f"Error listando manuales: {str(e)}")
        return []

# =========================
# RAG light (Auto)
# =========================
_manual_auto_cache = {"text": None, "file_id": None, "loaded_at": None}

def _find_auto_manual_file_id() -> Optional[str]:
    if not google_ready or not drive_client or not MANUALES_VICKY_FOLDER_ID:
        return None
    
    try:
        files = list_drive_manuals(MANUALES_VICKY_FOLDER_ID)
        if not files:
            log.warning("No se encontraron PDFs")
            return None
        
        # Buscar archivo de auto
        for file in files:
            name = (file.get("name") or "").lower()
            if any(keyword in name for keyword in ["auto", "cobertura", "vehicular", "automovil"]):
                log.info(f"Manual seleccionado: {file['name']}")
                return file["id"]
        
        # Usar el primero si no hay match
        if files:
            log.info(f"Usando primer PDF: {files[0]['name']}")
            return files[0]["id"]
        
        return None
        
    except Exception as e:
        log.error(f"Error buscando manual: {str(e)}")
        return None

def _download_pdf_text(file_id: str) -> Optional[str]:
    if not google_ready or not drive_client:
        return None
    
    try:
        from googleapiclient.http import MediaIoBaseDownload
        
        request = drive_client.files().get_media(fileId=file_id)
        file_handle = io.BytesIO()
        downloader = MediaIoBaseDownload(file_handle, request)
        
        done = False
        while not done:
            status, done = downloader.next_chunk()
        
        file_handle.seek(0)
        
        # Extraer texto
        reader = PdfReader(file_handle)
        pages_text = []
        
        for page in reader.pages:
            try:
                text = page.extract_text()
                if text and text.strip():
                    pages_text.append(text.strip())
            except Exception:
                continue
        
        full_text = "\n\n".join(pages_text)
        
        if not full_text.strip():
            return None
        
        # Limpiar texto
        full_text = re.sub(r'\s+', ' ', full_text)
        return full_text.strip()
        
    except Exception as e:
        log.error(f"Error procesando PDF: {str(e)}")
        return None

def ensure_auto_manual_text(force_reload: bool = False) -> Optional[str]:
    if not client_oa:
        return None
    
    if not force_reload and _manual_auto_cache.get("text"):
        cache_age = datetime.utcnow() - _manual_auto_cache.get("loaded_at", datetime.utcnow())
        if cache_age < timedelta(hours=24):
            return _manual_auto_cache["text"]
    
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
        log.info("Manual cacheado")
    
    return text

def answer_auto_from_manual(question: str) -> Optional[str]:
    if not client_oa:
        return None
    
    manual_text = ensure_auto_manual_text()
    if not manual_text:
        return "⚠️ No tengo acceso al manual de seguros. Por favor contacta al asesor para información detallada."
    
    try:
        # Buscar secciones relevantes
        keywords = ["amplia plus", "amplia", "cobertura", "asistencia", "cristales", "auto de reemplazo", "deducible"]
        relevant_sections = []
        
        for line in manual_text.split("\n"):
            line_lower = line.lower()
            if any(keyword in line_lower for keyword in keywords):
                relevant_sections.append(line.strip())
            if len("\n".join(relevant_sections)) > 6000:
                break
        
        context_text = "\n".join(relevant_sections) if relevant_sections else manual_text[:8000]
        
        prompt = f"""Responde sobre seguros de auto usando SOLO esta información:

{context_text}

Pregunta: {question}

Respuesta clara y concisa:"""
        
        response = client_oa.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=800
        )
        
        answer = (response.choices[0].message.content or "").strip()
        return answer if answer else "No pude encontrar información específica en el manual."
        
    except Exception as e:
        log.error(f"Error RAG: {str(e)}")
        return "⚠️ Error procesando la consulta. Contacta al asesor."

# =========================
# Menú y flujos
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
            formatted = (
                "🔔 NUEVO PROSPECTO – PRÉSTAMO IMSS\n"
                f"Nombre: {ctx.get('imss_nombre','')}\n"
                f"WhatsApp: {phone}\n"
                f"Ciudad: {ctx.get('imss_ciudad','')}\n"
                f"Monto solicitado: ${ctx.get('imss_monto',0):,.0f}\n"
                f"Nómina Inbursa: {ctx.get('imss_nomina','no')}"
            )
            notify_advisor(formatted)
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
            send_message(phone, "Perfecto. Envía documentos o escribe la fecha de vencimiento (AAAA-MM-DD).")
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
        if NOTIFICAR_ASESOR:
            formatted = (
                "🔔 NUEVO PROSPECTO – PRÉSTAMO IMSS\n"
                f"Nombre: {ctx.get('imss_nombre','')}\n"
                f"WhatsApp: {phone}\n"
                f"Ciudad: {ctx.get('imss_ciudad','')}\n"
                f"Monto solicitado: ${ctx.get('imss_monto',0):,.0f}\n"
                f"Nómina Inbursa: {ctx.get('imss_nomina','no')}"
            )
            notify_advisor(formatted)
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
        if NOTIFICAR_ASESOR:
            formatted = (
                "🔔 NUEVO PROSPECTO – CRÉDITO EMPRESARIAL\n"
                f"Nombre: {ctx.get('emp_nombre','')}\n"
                f"Ciudad: {ctx.get('emp_ciudad','')}\n"
                f"Monto solicitado: ${ctx.get('emp_monto',0):,.0f}\n"
                f"Actividad: {ctx.get('emp_giro','')}\n"
                f"WhatsApp: {phone}"
            )
            notify_advisor(formatted)
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

    # RAG para preguntas de auto
    if any(k in t for k in ["amplia plus", "amplia+", "cobertura", "coberturas", "cristales", "asistencia", "auto de reemplazo"]):
        rag_ans = answer_auto_from_manual(text or t)
        if rag_ans:
            send_message(phone, rag_ans)
            return

    if t in ("menu", "menú", "inicio", "hola"):
        user_state[phone] = ""
        send_main_menu(phone)
        return

    if t in ("1", "préstamo", "prestamo", "préstamo imss", "prestamo imss", "ley 73"):
        flow_prestamo_imss(phone, match)
        return
    if t in ("2", "auto", "seguro auto", "cotización auto", "cotizacion auto"):
        flow_auto_start(phone, match)
        return
    if t in ("3", "vida", "salud", "seguro de vida", "seguro de salud"):
        flow_vida_salud(phone)
        return
    if t in ("4", "vrim", "membresía médica", "membresia medica"):
        flow_vrim(phone)
        return
    if t in ("5", "asesoría imss", "asesoria imss", "imss", "pensión", "pension"):
        flow_imss_info(phone, match)
        return
    if t in ("6", "financiamiento", "empresarial", "crédito empresarial", "credito empresarial"):
        flow_empresarial(phone, match)
        return
    if t in ("7", "contactar", "asesor", "contactar con christian"):
        flow_contacto(phone)
        return

    st = user_state.get(phone, "")
    if st.startswith("imss_"):
        if st in {"imss_q1", "imss_pension", "imss_monto", "imss_nombre", "imss_ciudad", "imss_nomina"}:
            flow_imss_next(phone, text)
        else:
            flow_prestamo_imss_next(phone, text)
        return
    if st.startswith("auto_"):
        flow_auto_next(phone, text)
        return
    if st.startswith("emp_"):
        flow_empresarial_next(phone, text)
        return

    # Fallback GPT
    if client_oa:
        def _gpt_reply():
            try:
                prompt = (
                    "Eres Vicky, una asistente amable y profesional. "
                    "Responde en español, breve y con emojis si corresponde. "
                    f"Mensaje del usuario: {text or ''}"
                )
                res = client_oa.chat.completions.create(
                    model=OPENAI_MODEL,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.4,
                )
                answer = (res.choices[0].message.content or "").strip()
                send_message(phone, answer or "¿Te puedo ayudar con algo más? Escribe *menú*.")
            except Exception:
                send_main_menu(phone)
        threading.Thread(target=_gpt_reply, daemon=True).start()
    else:
        send_message(phone, "No te entendí bien. Escribe *menú* para ver opciones.")

# =========================
# Webhook
# =========================
@app.get("/webhook")
def webhook_verify():
    try:
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge", "")
        if mode == "subscribe" and token == VERIFY_TOKEN:
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
# Endpoints auxiliares
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
        "sheet_configured": bool(GOOGLE_SHEET_ID),
        "drive_configured": bool(MANUALES_VICKY_FOLDER_ID),
        "variables_usadas": {
            "SHEET_ID_SECOM": GOOGLE_SHEET_ID[:10] + "..." if GOOGLE_SHEET_ID else "No",
            "SHEET_TITLE_SECOM": GOOGLE_SHEET_NAME,
            "DRIVE_FOLDER_ID": MANUALES_VICKY_FOLDER_ID[:10] + "..." if MANUALES_VICKY_FOLDER_ID else "No"
        }
    }), 200

@app.get("/health")
def health():
    return jsonify({"status": "ok", "service": "Vicky Bot SECOM"}), 200

# =========================
# Arranque local
# =========================
if __name__ == "__main__":
    log.info(f"🚀 Vicky SECOM en puerto {PORT}")
    log.info(f"📱 WhatsApp: {bool(META_TOKEN and WABA_PHONE_ID)}")
    log.info(f"🔧 Google: {google_ready} - {google_error}")
    log.info(f"🤖 OpenAI: {bool(client_oa)}")
    log.info(f"📊 Sheets: {GOOGLE_SHEET_NAME}")
    log.info(f"📁 Drive: {bool(MANUALES_VICKY_FOLDER_ID)}")
    app.run(host="0.0.0.0", port=PORT, debug=False)

