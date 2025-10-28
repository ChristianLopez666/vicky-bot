# app.py — Vicky SECOM (Versión 100% Funcional + RAG Integrado)
# Python 3.11+
# ------------------------------------------------------------
# CORRECCIONES APLICADAS:
# 1. ✅ Endpoint /ext/send-promo completamente funcional
# 2. ✅ Eliminación de función duplicada
# 3. ✅ Validación robusta de configuración
# 4. ✅ Logging exhaustivo para diagnóstico
# 5. ✅ Manejo mejorado de errores
# 6. ✅ Worker para envíos masivos
# 7. ✅ MÓDULO RAG INTEGRADO para consultas de manuales
# 8. ✅ Corrección de bucle en estado auto_intro
# 9. ✅ Detección inteligente de consultas para RAG
# ------------------------------------------------------------

from __future__ import annotations

import os
import io
import re
import json
import time
import math
import queue
import logging
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, List, Tuple

import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# Google
try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload
except Exception:
    service_account = None
    build = None
    MediaIoBaseDownload = None
    MediaIoBaseUpload = None

# GPT opcional
try:
    from openai import OpenAI
except Exception:
    OpenAI = None

# ==========================
# Carga entorno + Logging
# ==========================
load_dotenv()

META_TOKEN = os.getenv("META_TOKEN")
WABA_PHONE_ID = os.getenv("WABA_PHONE_ID")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
ADVISOR_NUMBER = os.getenv("ADVISOR_NUMBER", "5216682478005")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")
SHEETS_ID_LEADS = os.getenv("SHEETS_ID_LEADS")
SHEETS_TITLE_LEADS = os.getenv("SHEETS_TITLE_LEADS", "Prospectos SECOM Auto")
DRIVE_PARENT_FOLDER_ID = os.getenv("DRIVE_PARENT_FOLDER_ID")
DRIVE_MANUALES_FOLDER = os.getenv("DRIVE_MANUALES_FOLDER", "Manuales Vicky")

PORT = int(os.getenv("PORT", "5000"))

# Configuración de logging robusta
logging.basicConfig(
    level=logging.INFO, 
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("vicky-secom")

# ==========================
# MÓDULO RAG INTEGRADO
# ==========================
class DriveReader:
    def __init__(self, credentials_json, folder_name=DRIVE_MANUALES_FOLDER):
        self.credentials = service_account.Credentials.from_service_account_info(credentials_json)
        self.service = build('drive', 'v3', credentials=self.credentials)
        self.folder_name = folder_name
        self.folder_id = self._find_folder_id()
        
    def _find_folder_id(self):
        """Localiza la carpeta Manuales Vicky por nombre"""
        try:
            response = self.service.files().list(
                q=f"name='{self.folder_name}' and mimeType='application/vnd.google-apps.folder'",
                spaces='drive',
                fields='files(id, name)'
            ).execute()
            
            if not response.get('files'):
                log.warning(f"Carpeta '{self.folder_name}' no encontrada en Drive")
                return None
                
            return response['files'][0]['id']
        except Exception as e:
            log.error(f"Error buscando carpeta {self.folder_name}: {str(e)}")
            return None
    
    def list_manual_files(self):
        """Lista todos los archivos en la carpeta con metadatos"""
        if not self.folder_id:
            return []
            
        try:
            response = self.service.files().list(
                q=f"'{self.folder_id}' in parents and trashed=false",
                fields='files(id, name, mimeType, modifiedTime, size)',
                orderBy='modifiedTime desc'
            ).execute()
            
            return [
                {
                    'id': file['id'],
                    'name': file['name'],
                    'mimeType': file['mimeType'],
                    'modifiedTime': file['modifiedTime'],
                    'size': file.get('size', '0')
                }
                for file in response.get('files', [])
            ]
        except Exception as e:
            log.error(f"Error listando archivos: {str(e)}")
            return []
    
    def extract_text_from_file(self, file_id, mime_type, file_name):
        """Extrae texto de PDFs y Google Docs"""
        try:
            if mime_type == 'application/pdf':
                return self._extract_pdf_text(file_id)
            elif mime_type == 'application/vnd.google-apps.document':
                return self._extract_google_doc_text(file_id)
            else:
                log.warning(f"Formato no soportado: {mime_type} para {file_name}")
                return ""
        except Exception as e:
            log.error(f"Error extrayendo texto de {file_name}: {str(e)}")
            return ""
    
    def _extract_pdf_text(self, file_id):
        """Extrae texto de PDFs"""
        try:
            request = self.service.files().get_media(fileId=file_id)
            pdf_file = io.BytesIO()
            downloader = MediaIoBaseDownload(pdf_file, request)
            
            done = False
            while not done:
                status, done = downloader.next_chunk()
            
            pdf_file.seek(0)
            
            # Extracción simple de texto de PDF (sin pdfminer)
            text = self._extract_text_from_pdf_bytes(pdf_file.getvalue())
            
            # Limpieza básica
            text = re.sub(r'\n\s*\n', '\n\n', text)
            text = re.sub(r'^\s+|\s+$', '', text, flags=re.MULTILINE)
            
            return text
        except Exception as e:
            log.error(f"Error extrayendo PDF: {str(e)}")
            return ""
    
    def _extract_text_from_pdf_bytes(self, pdf_bytes):
        """Extracción básica de texto de PDF"""
        try:
            # Patrón simple para extraer texto entre paréntesis (common in PDFs)
            text_pattern = re.findall(rb'\((.*?)\)', pdf_bytes)
            text = b' '.join(text_pattern).decode('latin-1', errors='ignore')
            
            # Limpiar caracteres especiales
            text = re.sub(r'\\[a-zA-Z]', ' ', text)
            text = re.sub(r'\s+', ' ', text)
            
            return text.strip()
        except Exception:
            return "No se pudo extraer texto del PDF"
    
    def _extract_google_doc_text(self, file_id):
        """Exporta Google Docs como texto plano"""
        try:
            request = self.service.files().export_media(
                fileId=file_id, 
                mimeType='text/plain'
            )
            text_file = io.BytesIO()
            downloader = MediaIoBaseDownload(text_file, request)
            
            done = False
            while not done:
                status, done = downloader.next_chunk()
            
            return text_file.getvalue().decode('utf-8')
        except Exception as e:
            log.error(f"Error extrayendo Google Doc: {str(e)}")
            return ""

class RAGIndex:
    def __init__(self, drive_reader, openai_api_key):
        self.drive_reader = drive_reader
        self.openai_client = OpenAI(api_key=openai_api_key) if OpenAI and openai_api_key else None
        self.index_cache = None
        self.last_refresh = 0
        self.cache_ttl = 1800  # 30 minutos
        
    def chunk_text(self, text, chunk_size=800, overlap=120):
        """Divide texto en chunks con overlap"""
        words = text.split()
        if not words:
            return []
            
        chunks = []
        
        for i in range(0, len(words), chunk_size - overlap):
            chunk = ' '.join(words[i:i + chunk_size])
            chunks.append(chunk)
            if i + chunk_size >= len(words):
                break
                
        return chunks
    
    def build_index(self):
        """Construye índice de documentos"""
        try:
            files = self.drive_reader.list_manual_files()
            if not files:
                log.warning("No se encontraron archivos en la carpeta de manuales")
                return None
                
            all_chunks = []
            all_metadata = []
            
            for file in files:
                log.info(f"Procesando: {file['name']}")
                text = self.drive_reader.extract_text_from_file(
                    file['id'], file['mimeType'], file['name']
                )
                
                if not text:
                    continue
                    
                chunks = self.chunk_text(text)
                
                for i, chunk in enumerate(chunks):
                    all_chunks.append(chunk)
                    all_metadata.append({
                        'file_name': file['name'],
                        'file_id': file['id'],
                        'chunk_index': i,
                        'modified_time': file['modifiedTime'],
                        'text_preview': chunk[:100] + '...' if len(chunk) > 100 else chunk
                    })
            
            if not all_chunks:
                log.warning("No se encontraron chunks para indexar")
                return None
            
            self.index_cache = {
                'chunks': all_chunks,
                'metadata': all_metadata,
                'build_time': time.time(),
                'doc_count': len(files),
                'chunk_count': len(all_chunks)
            }
            self.last_refresh = time.time()
            
            log.info(f"Índice construido con {len(all_chunks)} chunks de {len(files)} documentos")
            return self.index_cache
            
        except Exception as e:
            log.error(f"Error construyendo índice: {str(e)}")
            return None
    
    def search_simple(self, query, top_k=5):
        """Búsqueda simple por palabras clave"""
        if not self.index_cache or time.time() - self.last_refresh > self.cache_ttl:
            log.info("Refrescando índice...")
            self.build_index()
        
        if not self.index_cache:
            return []
        
        query_words = query.lower().split()
        results = []
        
        for i, chunk in enumerate(self.index_cache['chunks']):
            chunk_lower = chunk.lower()
            score = sum(1 for word in query_words if word in chunk_lower)
            
            if score > 0:
                results.append({
                    'chunk': chunk,
                    'metadata': self.index_cache['metadata'][i],
                    'score': score
                })
        
        # Ordenar por score y tomar los mejores
        results.sort(key=lambda x: x['score'], reverse=True)
        return results[:top_k]
    
    def answer_with_context(self, user_query):
        """Genera respuesta usando contexto de los manuales"""
        try:
            search_results = self.search_simple(user_query, top_k=3)
            
            if not search_results:
                return "🤔 No encontré información específica sobre tu pregunta en los manuales. ¿Podrías reformularla o contactar a Christian para asistencia personalizada?"
            
            context = ""
            for i, result in enumerate(search_results):
                context += f"[De: {result['metadata']['file_name']}]\n{result['chunk']}\n\n"
            
            if self.openai_client:
                prompt = f"""Eres Vicky, asistente de SECOM. Responde de manera CALIDA, CLARA y PRECISA usando SOLO la información del contexto.

CONTEXTO DISPONIBLE:
{context}

PREGUNTA DEL USUARIO: {user_query}

INSTRUCCIONES:
- Responde en español de México, tono profesional pero amable
- Usa EMOJIS relevantes para hacerlo más cálido ✅🤝🌟
- Si la información no es completa, sugiere contactar al asesor
- CITAR la fuente específica (nombre del manual) cuando uses información de ahí
- NUNCA inventes información que no esté en el contexto
- Sé concisa pero útil (máximo 2 párrafos)

RESPUESTA:"""
                
                response = self.openai_client.chat.completions.create(
                    model="gpt-3.5-turbo",
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.3,
                    max_tokens=500
                )
                
                answer = response.choices[0].message.content.strip()
                
                # Asegurar que se cite la fuente
                if not any(f in answer for f in ["Fuente", "Manual", "procedimiento", "según"]):
                    main_source = search_results[0]['metadata']['file_name']
                    answer += f"\n\n📚 Fuente: {main_source}"
                    
                return answer
            else:
                # Fallback sin OpenAI - usar el chunk más relevante
                main_result = search_results[0]
                source = main_result['metadata']['file_name']
                text = main_result['chunk'][:400] + "..." if len(main_result['chunk']) > 400 else main_result['chunk']
                return f"📚 Según {source}:\n\n{text}\n\nPara información más detallada, contacta a Christian 📞"
                
        except Exception as e:
            log.error(f"Error en answer_with_context: {str(e)}")
            return "⚠️ Ocurrió un error al procesar tu consulta. Por favor, intenta de nuevo o contacta a Christian para asistencia inmediata."

# ==========================
# Google Setup (degradable)
# ==========================
creds = None
sheets_svc = None
drive_svc = None
google_ready = False
rag_index = None

if GOOGLE_CREDENTIALS_JSON and service_account and build:
    try:
        info = json.loads(GOOGLE_CREDENTIALS_JSON)
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]
        creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
        sheets_svc = build("sheets", "v4", credentials=creds)
        drive_svc = build("drive", "v3", credentials=creds)
        google_ready = True
        log.info("✅ Google services listos (Sheets + Drive)")
        
        # Inicializar RAG si hay OpenAI
        if OPENAI_API_KEY and OpenAI:
            try:
                drive_reader = DriveReader(info)
                rag_index = RAGIndex(drive_reader, OPENAI_API_KEY)
                # Construir índice inicial en background
                threading.Thread(target=rag_index.build_index, daemon=True).start()
                log.info("✅ RAG Index inicializado en background")
            except Exception as e:
                log.error(f"❌ Error inicializando RAG: {str(e)}")
                rag_index = None
    except Exception:
        log.exception("❌ No fue posible inicializar Google. Modo mínimo activo.")
else:
    log.warning("⚠️ Credenciales de Google no disponibles. Modo mínimo activo.")

# =================================
# Estado por usuario en memoria
# =================================
app = Flask(__name__)
user_state: Dict[str, str] = {}
user_data: Dict[str, Dict[str, Any]] = {}

# ==========================
# Utilidades generales
# ==========================
WPP_API_URL = f"https://graph.facebook.com/v20.0/{WABA_PHONE_ID}/messages" if WABA_PHONE_ID else None
WPP_TIMEOUT = 15

def _normalize_phone_last10(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    return digits[-10:] if len(digits) >= 10 else digits

def interpret_response(text: str) -> str:
    if not text:
        return "neutral"
    t = text.lower()
    pos = ["sí", "si", "claro", "ok", "de acuerdo", "vale", "afirmativo", "correcto"]
    neg = ["no", "nel", "nop", "negativo", "no quiero", "no gracias", "no interesa"]
    if any(p in t for p in pos):
        return "positive"
    if any(n in t for n in neg):
        return "negative"
    return "neutral"

def extract_number(text: str) -> Optional[float]:
    if not text:
        return None
    clean = text.replace(",", "").replace("$", "")
    m = re.search(r"(\d{1,12}(\.\d+)?)", clean)
    try:
        return float(m.group(1)) if m else None
    except Exception:
        return None

def _ensure_user(phone: str) -> Dict[str, Any]:
    if phone not in user_data:
        user_data[phone] = {}
    return user_data[phone]

# ==========================
# WhatsApp Helpers (retries)
# ==========================
def _wpp_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {META_TOKEN}",
        "Content-Type": "application/json"
    }

def _should_retry(status: int) -> bool:
    return status == 429 or 500 <= status < 600

def _backoff(attempt: int) -> None:
    time.sleep(2 ** attempt)

def send_message(to: str, text: str) -> bool:
    """Envía mensaje de texto WPP. Reintentos exponenciales en 429/5xx."""
    if not (META_TOKEN and WPP_API_URL):
        log.error("❌ WhatsApp no configurado (META_TOKEN/WABA_PHONE_ID faltan).")
        return False
    
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text[:4096]},
    }
    
    for attempt in range(3):
        try:
            log.info(f"📤 Enviando mensaje a {to} (intento {attempt + 1})")
            resp = requests.post(WPP_API_URL, headers=_wpp_headers(), json=payload, timeout=WPP_TIMEOUT)
            
            if resp.status_code == 200:
                log.info(f"✅ Mensaje enviado exitosamente a {to}")
                return True
            
            log.warning(f"⚠️ WPP send_message fallo {resp.status_code}: {resp.text[:200]}")
            if _should_retry(resp.status_code) and attempt < 2:
                log.info(f"🔄 Reintentando en {2 ** attempt} segundos...")
                _backoff(attempt)
                continue
            return False
        except requests.exceptions.Timeout:
            log.error(f"⏰ Timeout enviando mensaje a {to} (intento {attempt + 1})")
            if attempt < 2:
                _backoff(attempt)
                continue
            return False
        except Exception as e:
            log.exception(f"❌ Error en send_message a {to}")
            if attempt < 2:
                _backoff(attempt)
                continue
            return False
    return False

def send_template_message(to: str, template_name: str, params: Dict | List) -> bool:
    """Envía plantilla preaprobada."""
    if not (META_TOKEN and WPP_API_URL):
        log.error("❌ WhatsApp no configurado para plantillas.")
        return False
    
    components = []
    if isinstance(params, dict):
        for k, v in params.items():
            components.append({"type": "body", "parameters": [{"type": "text", "text": str(v)}]})
    elif isinstance(params, list):
        components.append({
            "type": "body",
            "parameters": [{"type": "text", "text": str(x)} for x in params]
        })
    
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
    
    for attempt in range(3):
        try:
            log.info(f"📤 Enviando plantilla '{template_name}' a {to} (intento {attempt + 1})")
            resp = requests.post(WPP_API_URL, headers=_wpp_headers(), json=payload, timeout=WPP_TIMEOUT)
            
            if resp.status_code == 200:
                log.info(f"✅ Plantilla '{template_name}' enviada exitosamente a {to}")
                return True
            
            log.warning(f"⚠️ WPP send_template fallo {resp.status_code}: {resp.text[:200]}")
            if _should_retry(resp.status_code) and attempt < 2:
                log.info(f"🔄 Reintentando plantilla en {2 ** attempt} segundos...")
                _backoff(attempt)
                continue
            return False
        except requests.exceptions.Timeout:
            log.error(f"⏰ Timeout enviando plantilla a {to} (intento {attempt + 1})")
            if attempt < 2:
                _backoff(attempt)
                continue
            return False
        except Exception:
            log.exception(f"❌ Error en send_template_message a {to}")
            if attempt < 2:
                _backoff(attempt)
                continue
            return False
    return False

# ==========================
# Google Helpers
# ==========================
def match_client_in_sheets(phone_last10: str) -> Optional[Dict[str, Any]]:
    """Busca el teléfono en cualquier columna del sheet y devuelve dict con rowIndex y nombre si lo encuentra."""
    if not (google_ready and sheets_svc and SHEETS_ID_LEADS and SHEETS_TITLE_LEADS):
        log.warning("⚠️ Sheets no disponible; no se puede hacer matching.")
        return None
    try:
        rng = f"{SHEETS_TITLE_LEADS}!A:Z"
        values = sheets_svc.spreadsheets().values().get(spreadsheetId=SHEETS_ID_LEADS, range=rng).execute()
        rows = values.get("values", [])
        phone_last10 = str(phone_last10)
        
        for idx, row in enumerate(rows, start=1):
            joined = " | ".join(row)
            digits = re.sub(r"\D", "", joined)
            if phone_last10 and phone_last10 in digits:
                nombre = None
                for cell in row:
                    if cell and not re.search(r"\d", cell):
                        nombre = cell.strip()
                        break
                log.info(f"✅ Cliente encontrado en Sheets: {nombre} ({phone_last10})")
                return {"row": idx, "nombre": nombre or "", "raw": row}
        log.info(f"ℹ️ Cliente no encontrado en Sheets: {phone_last10}")
        return None
    except Exception:
        log.exception("❌ Error buscando en Sheets")
        return None

def write_followup_to_sheets(row: int | str, note: str, date_iso: str) -> None:
    """Registra una nota en una hoja 'Seguimiento' (append)."""
    if not (google_ready and sheets_svc and SHEETS_ID_LEADS):
        log.warning("⚠️ Sheets no disponible; no se puede escribir seguimiento.")
        return
    try:
        title = "Seguimiento"
        body = {
            "values": [[str(row), date_iso, note]]
        }
        sheets_svc.spreadsheets().values().append(
            spreadsheetId=SHEETS_ID_LEADS,
            range=f"{title}!A:C",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body=body
        ).execute()
        log.info(f"✅ Seguimiento registrado en Sheets: {note}")
    except Exception:
        log.exception("❌ Error escribiendo seguimiento en Sheets")

def _find_or_create_client_folder(folder_name: str) -> Optional[str]:
    """Ubica/crea subcarpeta dentro de DRIVE_PARENT_FOLDER_ID."""
    if not (google_ready and drive_svc and DRIVE_PARENT_FOLDER_ID):
        log.warning("⚠️ Drive no disponible; no se puede crear carpeta.")
        return None
    try:
        q = f"name = '{folder_name}' and mimeType = 'application/vnd.google-apps.folder' and '{DRIVE_PARENT_FOLDER_ID}' in parents and trashed = false"
        resp = drive_svc.files().list(q=q, fields="files(id, name)").execute()
        items = resp.get("files", [])
        if items:
            log.info(f"✅ Carpeta encontrada: {folder_name}")
            return items[0]["id"]
        meta = {
            "name": folder_name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [DRIVE_PARENT_FOLDER_ID],
        }
        created = drive_svc.files().create(body=meta, fields="id").execute()
        folder_id = created.get("id")
        log.info(f"✅ Carpeta creada: {folder_name} (ID: {folder_id})")
        return folder_id
    except Exception:
        log.exception("❌ Error creando/buscando carpeta en Drive")
        return None

def upload_to_drive(file_name: str, file_bytes: bytes, mime_type: str, folder_name: str) -> Optional[str]:
    """Sube archivo a carpeta del cliente; retorna webViewLink (si posible) o fileId."""
    if not (google_ready and drive_svc and MediaIoBaseUpload):
        log.warning("⚠️ Drive no disponible; no se puede subir archivo.")
        return None
    try:
        folder_id = _find_or_create_client_folder(folder_name)
        if not folder_id:
            return None
        media = MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype=mime_type, resumable=False)
        meta = {"name": file_name, "parents": [folder_id]}
        created = drive_svc.files().create(body=meta, media_body=media, fields="id, webViewLink").execute()
        link = created.get("webViewLink") or created.get("id")
        log.info(f"✅ Archivo subido a Drive: {file_name} -> {link}")
        return link
    except Exception:
        log.exception("❌ Error subiendo archivo a Drive")
        return None

# ==========================
# Menú principal
# ==========================
MAIN_MENU = (
    "🟦 *Vicky Bot — Inbursa*\n"
    "Elige una opción:\n"
    "1) Préstamo IMSS (Ley 73)\n"
    "2) Seguro de Auto (cotización)\n"
    "3) Seguros de Vida / Salud\n"
    "4) Tarjeta médica VRIM\n"
    "5) Crédito Empresarial\n"
    "6) Financiamiento Práctico\n"
    "7) Contactar con Christian\n"
    "\nEscribe el número u opción (ej. 'imss', 'auto', 'empresarial', 'contactar')."
)

def send_main_menu(phone: str) -> None:
    log.info(f"📋 Enviando menú principal a {phone}")
    send_message(phone, MAIN_MENU)

# ==========================
# Embudos (conservados del original)
# ==========================
def _notify_advisor(text: str) -> None:
    try:
        log.info(f"👨‍💼 Notificando al asesor: {text}")
        send_message(ADVISOR_NUMBER, text)
    except Exception:
        log.exception("❌ Error notificando al asesor")

# --- IMSS (opción 1) ---
def imss_start(phone: str, match: Optional[Dict[str, Any]]) -> None:
    user_state[phone] = "imss_beneficios"
    log.info(f"🏥 Iniciando embudo IMSS para {phone}")
    send_message(phone, "🟩 *Préstamo IMSS Ley 73*\nBeneficios clave: trámite rápido, sin aval, pagos fijos y atención personalizada. ¿Te interesa conocer requisitos? (responde *sí* o *no*)")

def _imss_next(phone: str, text: str) -> None:
    st = user_state.get(phone, "")
    data = _ensure_user(phone)
    if st == "imss_beneficios":
        if interpret_response(text) == "positive":
            user_state[phone] = "imss_pension"
            send_message(phone, "¿Cuál es tu *pensión mensual* aproximada? (ej. $8,500)")
        else:
            send_message(phone, "Sin problema. Si deseas volver al menú, escribe *menú*.")
    elif st == "imss_pension":
        pension = extract_number(text)
        if not pension:
            send_message(phone, "No pude leer el monto. Indica tu *pensión mensual* (ej. 8500).")
            return
        data["imss_pension"] = pension
        user_state[phone] = "imss_monto"
        send_message(phone, "Gracias. ¿Qué *monto* te gustaría solicitar? (mínimo $40,000)")
    elif st == "imss_monto":
        monto = extract_number(text)
        if not monto:
            send_message(phone, "Escribe un *monto* (ej. 100000).")
            return
        data["imss_monto"] = monto
        user_state[phone] = "imss_nombre"
        send_message(phone, "Perfecto. ¿Cuál es tu *nombre completo*?")
    elif st == "imss_nombre":
        data["imss_nombre"] = text.strip()
        user_state[phone] = "imss_ciudad"
        send_message(phone, "¿En qué *ciudad* te encuentras?")
    elif st == "imss_ciudad":
        data["imss_ciudad"] = text.strip()
        user_state[phone] = "imss_nomina"
        send_message(phone, "¿Tienes *nómina Inbursa* actualmente? (sí/no)\n*Nota:* No es obligatoria; si la tienes, accedes a *beneficios adicionales*.")
    elif st == "imss_nomina":
        tiene_nomina = interpret_response(text) == "positive"
        data["imss_nomina_inbursa"] = "sí" if tiene_nomina else "no"
        msg = (
            "✅ *Preautorizado*. Un asesor te contactará.\n"
            f"- Nombre: {data.get('imss_nombre','')}\n"
            f"- Ciudad: {data.get('imss_ciudad','')}\n"
            f"- Pensión: ${data.get('imss_pension',0):,.0f}\n"
            f"- Monto deseado: ${data.get('imss_monto',0):,.0f}\n"
            f"- Nómina Inbursa: {data.get('imss_nomina_inbursa','no')}\n"
        )
        send_message(phone, msg)
        _notify_advisor(f"🔔 IMSS — Prospecto preautorizado\nWhatsApp: {phone}\n" + msg)
        user_state[phone] = ""
        send_main_menu(phone)

# --- Crédito Empresarial (opción 5) ---
def emp_start(phone: str, match: Optional[Dict[str, Any]]) -> None:
    user_state[phone] = "emp_confirma"
    log.info(f"🏢 Iniciando embudo empresarial para {phone}")
    send_message(phone, "🟦 *Crédito Empresarial*\n¿Eres empresario(a) o representas una empresa? (sí/no)")

def _emp_next(phone: str, text: str) -> None:
    st = user_state.get(phone, "")
    data = _ensure_user(phone)
    if st == "emp_confirma":
        if interpret_response(text) != "positive":
            send_message(phone, "Entendido. Si deseas volver al menú, escribe *menú*.")
            return
        user_state[phone] = "emp_giro"
        send_message(phone, "¿A qué *se dedica* tu empresa?")
    elif st == "emp_giro":
        data["emp_giro"] = text.strip()
        user_state[phone] = "emp_monto"
        send_message(phone, "¿Qué *monto* deseas? (mínimo $100,000)")
    elif st == "emp_monto":
        monto = extract_number(text)
        if not monto or monto < 100000:
            send_message(phone, "El monto mínimo es $100,000. Indica un monto igual o mayor.")
            return
        data["emp_monto"] = monto
        user_state[phone] = "emp_nombre"
        send_message(phone, "¿Tu *nombre completo*?")
    elif st == "emp_nombre":
        data["emp_nombre"] = text.strip()
        user_state[phone] = "emp_ciudad"
        send_message(phone, "¿Tu *ciudad*?")
    elif st == "emp_ciudad":
        data["emp_ciudad"] = text.strip()
        resumen = (
            "✅ Gracias. Un asesor te contactará.\n"
            f"- Nombre: {data.get('emp_nombre','')}\n"
            f"- Ciudad: {data.get('emp_ciudad','')}\n"
            f"- Giro: {data.get('emp_giro','')}\n"
            f"- Monto: ${data.get('emp_monto',0):,.0f}\n"
        )
        send_message(phone, resumen)
        _notify_advisor(f"🔔 Empresarial — Nueva solicitud\nWhatsApp: {phone}\n" + resumen)
        user_state[phone] = ""
        send_main_menu(phone)

# --- Financiamiento Práctico (opción 6) ---
FP_QUESTIONS = [f"Pregunta {i}" for i in range(1, 12)]
def fp_start(phone: str, match: Optional[Dict[str, Any]]) -> None:
    user_state[phone] = "fp_q1"
    _ensure_user(phone)["fp_answers"] = {}
    log.info(f"💰 Iniciando embudo financiamiento práctico para {phone}")
    send_message(phone, "🟩 *Financiamiento Práctico*\nResponderemos 11 preguntas rápidas.\n1) " + FP_QUESTIONS[0])

def _fp_next(phone: str, text: str) -> None:
    st = user_state.get(phone, "")
    data = _ensure_user(phone)
    if st.startswith("fp_q"):
        idx = int(st.split("_q")[1]) - 1
        data["fp_answers"][f"q{idx+1}"] = text.strip()
        if idx + 1 < len(FP_QUESTIONS):
            user_state[phone] = f"fp_q{idx+2}"
            send_message(phone, f"{idx+2}) {FP_QUESTIONS[idx+1]}")
        else:
            user_state[phone] = "fp_comentario"
            send_message(phone, "¿Algún *comentario adicional*?")
    elif st == "fp_comentario":
        data["fp_comentario"] = text.strip()
        resumen = "✅ Gracias. Un asesor te contactará.\n" + "\n".join(
            f"{k.upper()}: {v}" for k, v in data.get("fp_answers", {}).items()
        )
        if data.get("fp_comentario"):
            resumen += f"\nCOMENTARIO: {data['fp_comentario']}"
        send_message(phone, resumen)
        _notify_advisor(f"🔔 Financiamiento Práctico — Resumen\nWhatsApp: {phone}\n{resumen}")
        user_state[phone] = ""
        send_main_menu(phone)

# --- Seguros de Auto (opción 2) ---
def auto_start(phone: str, match: Optional[Dict[str, Any]]) -> None:
    user_state[phone] = "auto_intro"
    log.info(f"🚗 Iniciando embudo seguro auto para {phone}")
    send_message(phone,
        "🚗 *Seguro de Auto*\nEnvíame por favor:\n• INE (frente)\n• Tarjeta de circulación *o* número de placas\n\nCuando lo envíes, te confirmaré recepción y procesaré la cotización."
    )

def _auto_next(phone: str, text: str) -> None:
    st = user_state.get(phone, "")
    if st == "auto_intro":
        # DETECCIÓN DE CONSULTAS PARA RAG - CORREGIDO
        rag_keywords = ['cobertura', 'coberturas', 'qué cubre', 'que cubre', 'incluye', 'beneficios', 'condiciones', 
                       'póliza', 'poliza', 'endoso', 'deducible', 'asegurado', 'cláusula', 'clausula', 'vigencia',
                       'siniestro', 'reclamación', 'reclamacion', 'procedimiento', 'manual', 'documentación',
                       'diferencia', 'amplia', 'limitada', 'plus', 'básica', 'basica', 'tiempo', 'pagar', 'cuanto']
        
        is_rag_query = any(keyword in text.lower() for keyword in rag_keywords)
        
        if is_rag_query and rag_index:
            log.info(f"🧠 Consulta RAG detectada en auto_intro: {text}")
            respuesta = rag_index.answer_with_context(text)
            send_message(phone, respuesta)
            return
            
        intent = interpret_response(text)
        if "vencimiento" in text.lower() or "vence" in text.lower() or "fecha" in text.lower():
            user_state[phone] = "auto_vencimiento_fecha"
            send_message(phone, "¿Cuál es la *fecha de vencimiento* de tu póliza actual? (formato AAAA-MM-DD)")
            return
        if intent == "negative":
            user_state[phone] = "auto_vencimiento_fecha"
            send_message(phone, "Entendido. Para poder recordarte a tiempo, ¿cuál es la *fecha de vencimiento* de tu póliza? (AAAA-MM-DD)")
            return
        send_message(phone, "Perfecto. Puedes empezar enviando los *documentos* o una *foto* de la tarjeta/placas.")

    elif st == "auto_vencimiento_fecha":
        try:
            fecha = datetime.fromisoformat(text.strip()).date()
            objetivo = fecha - timedelta(days=30)
            write_followup_to_sheets("auto_recordatorio", f"Recordatorio póliza -30d para {phone}", objetivo.isoformat())
            threading.Thread(target=_retry_after_days, args=(phone, 7), daemon=True).start()
            send_message(phone, f"✅ Gracias. Te contactaré *un mes antes* ({objetivo.isoformat()}).")
            user_state[phone] = ""
            send_main_menu(phone)
        except Exception:
            send_message(phone, "Formato inválido. Usa AAAA-MM-DD. Ejemplo: 2025-12-31")

def _retry_after_days(phone: str, days: int) -> None:
    try:
        time.sleep(days * 24 * 60 * 60)
        send_message(phone, "⏰ Seguimos a tus órdenes. ¿Deseas que coticemos tu seguro de auto cuando se acerque el vencimiento?")
        write_followup_to_sheets("auto_reintento", f"Reintento +{days}d enviado a {phone}", datetime.utcnow().isoformat())
    except Exception:
        log.exception("Error en reintento programado")

# ==========================
# Router helpers CON RAG INTEGRADO - CORREGIDO
# ==========================
def _greet_and_match(phone: str) -> Optional[Dict[str, Any]]:
    last10 = _normalize_phone_last10(phone)
    match = match_client_in_sheets(last10)
    if match and match.get("nombre"):
        send_message(phone, f"Hola {match['nombre']} 👋 Soy *Vicky*. ¿En qué te puedo ayudar hoy?")
    else:
        send_message(phone, "Hola 👋 Soy *Vicky*. Estoy para ayudarte.")
    return match

def _route_command(phone: str, text: str, match: Optional[Dict[str, Any]]) -> None:
    t = text.strip().lower()
    
    # DETECCIÓN DE CONSULTAS PARA RAG - MEJORADA
    rag_keywords = ['cobertura', 'coberturas', 'qué cubre', 'que cubre', 'incluye', 'beneficios', 'condiciones', 
                   'póliza', 'poliza', 'endoso', 'deducible', 'asegurado', 'cláusula', 'clausula', 'vigencia',
                   'siniestro', 'reclamación', 'reclamacion', 'procedimiento', 'manual', 'documentación',
                   'diferencia', 'amplia', 'limitada', 'plus', 'básica', 'basica', 'tiempo', 'pagar', 'cuanto',
                   'explicas', 'explícame', 'información', 'informacion', 'detalles', 'características']
    
    is_rag_query = any(keyword in t for keyword in rag_keywords)
    
    # PRIORIDAD: Si es consulta RAG, procesar inmediatamente
    if is_rag_query and rag_index:
        log.info(f"🧠 Consulta RAG detectada: {text}")
        respuesta = rag_index.answer_with_context(text)
        send_message(phone, respuesta)
        return
    
    # Comandos normales del menú
    if t in ("1", "imss", "ley 73", "préstamo", "prestamo", "pension", "pensión"):
        imss_start(phone, match)
    elif t in ("2", "auto", "seguros de auto", "seguro auto"):
        auto_start(phone, match)
    elif t in ("3", "vida", "salud", "seguro de vida", "seguro de salud"):
        send_message(phone, "🧬 *Seguros de Vida/Salud* — Gracias por tu interés. Notificaré al asesor para contactarte.")
        _notify_advisor(f"🔔 Vida/Salud — Solicitud de contacto\nWhatsApp: {phone}")
        send_main_menu(phone)
    elif t in ("4", "vrim", "tarjeta médica", "tarjeta medica"):
        send_message(phone, "🩺 *VRIM* — Membresía médica. Notificaré al asesor para darte detalles.")
        _notify_advisor(f"🔔 VRIM — Solicitud de contacto\nWhatsApp: {phone}")
        send_main_menu(phone)
    elif t in ("5", "empresarial", "pyme", "crédito empresarial", "credito empresarial"):
        emp_start(phone, match)
    elif t in ("6", "financiamiento práctico", "financiamiento practico", "crédito simple", "credito simple"):
        fp_start(phone, match)
    elif t in ("7", "contactar", "asesor", "contactar con christian"):
        _notify_advisor(f"🔔 Contacto directo — Cliente solicita hablar\nWhatsApp: {phone}")
        send_message(phone, "✅ Listo. Avisé a Christian para que te contacte.")
        send_main_menu(phone)
    elif t in ("menu", "menú", "inicio", "hola"):
        user_state[phone] = ""
        send_main_menu(phone)
    else:
        st = user_state.get(phone, "")
        if st.startswith("imss_"):
            _imss_next(phone, text)
        elif st.startswith("emp_"):
            _emp_next(phone, text)
        elif st.startswith("fp_"):
            _fp_next(phone, text)
        elif st.startswith("auto_"):
            _auto_next(phone, text)
        else:
            # Si no hay estado y no es comando reconocido, ofrecer menú
            send_message(phone, "No entendí. Escribe *menú* para ver opciones.")

# ==========================
# Webhook — verificación
# ==========================
@app.get("/webhook")
def webhook_verify():
    try:
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge", "")
        if mode == "subscribe" and token == VERIFY_TOKEN:
            log.info("✅ Webhook verificado exitosamente")
            return challenge, 200
    except Exception:
        log.exception("❌ Error en verificación webhook")
    log.warning("❌ Webhook verification failed")
    return "Error", 403

# ==========================
# Webhook — recepción
# ==========================
def _download_media(media_id: str) -> Tuple[Optional[bytes], Optional[str], Optional[str]]:
    """Descarga bytes, mime_type y filename desde WPP Graph para media_id."""
    if not META_TOKEN:
        return None, None, None
    try:
        meta = requests.get(
            f"https://graph.facebook.com/v20.0/{media_id}",
            headers={"Authorization": f"Bearer {META_TOKEN}"},
            timeout=WPP_TIMEOUT
        )
        if meta.status_code != 200:
            log.warning(f"⚠️ Meta media meta fallo {meta.status_code}: {meta.text[:200]}")
            return None, None, None
        meta_j = meta.json()
        url = meta_j.get("url")
        mime = meta_j.get("mime_type")
        fname = meta_j.get("filename") or f"media_{media_id}"
        if not url:
            return None, None, None
        binr = requests.get(url, headers={"Authorization": f"Bearer {META_TOKEN}"}, timeout=WPP_TIMEOUT)
        if binr.status_code != 200:
            log.warning(f"⚠️ Meta media download fallo {binr.status_code}")
            return None, None, None
        log.info(f"✅ Media descargada: {fname} ({len(binr.content)} bytes)")
        return binr.content, mime, fname
    except Exception:
        log.exception("❌ Error descargando media")
        return None, None, None

def _handle_media(phone: str, msg: Dict[str, Any]) -> None:
    try:
        media_id = None
        if msg.get("type") == "image" and "image" in msg:
            media_id = msg["image"].get("id")
        elif msg.get("type") == "document" and "document" in msg:
            media_id = msg["document"].get("id")
        elif msg.get("type") == "audio" and "audio" in msg:
            media_id = msg["audio"].get("id")
        elif msg.get("type") == "video" and "video" in msg:
            media_id = msg["video"].get("id")

        if not media_id:
            send_message(phone, "Recibí tu archivo, gracias. (No se pudo identificar el contenido).")
            return

        file_bytes, mime, fname = _download_media(media_id)
        if not file_bytes:
            send_message(phone, "Recibí tu archivo, pero hubo un problema procesándolo.")
            return

        last4 = _normalize_phone_last10(phone)[-4:]
        match = match_client_in_sheets(_normalize_phone_last10(phone))
        if match and match.get("nombre"):
            folder_name = f"{match['nombre'].replace(' ', '_')}_{last4}"
        else:
            folder_name = f"Cliente_{last4}"

        link = upload_to_drive(fname, file_bytes, mime or "application/octet-stream", folder_name)
        link_text = link or "(sin link Drive)"

        _notify_advisor(f"🔔 Multimedia recibida\nDesde: {phone}\nArchivo: {fname}\nDrive: {link_text}")
        send_message(phone, "✅ *Recibido y en proceso*. En breve te doy seguimiento.")
    except Exception:
        log.exception("❌ Error manejando multimedia")
        send_message(phone, "Recibí tu archivo, gracias. Si algo falla, lo reviso de inmediato.")

@app.post("/webhook")
def webhook_receive():
    try:
        payload = request.get_json(force=True, silent=True) or {}
        log.info(f"📥 Webhook recibido: {json.dumps(payload, indent=2)[:500]}...")
        
        entry = payload.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        value = changes.get("value", {})
        messages = value.get("messages", [])
        if not messages:
            log.info("ℹ️ Webhook sin mensajes (posible status update)")
            return jsonify({"ok": True}), 200

        msg = messages[0]
        phone = msg.get("from")
        if not phone:
            log.warning("⚠️ Mensaje sin número de teléfono")
            return jsonify({"ok": True}), 200

        log.info(f"📱 Mensaje de {phone}: {msg.get('type', 'unknown')}")

        match = _greet_and_match(phone) if phone not in user_state else None

        mtype = msg.get("type")
        if mtype == "text" and "text" in msg:
            text = msg["text"].get("body", "").strip()
            log.info(f"💬 Texto recibido de {phone}: {text}")

            if text.lower().startswith("sgpt:") and rag_index and rag_index.openai_client:
                prompt = text.split("sgpt:", 1)[1].strip()
                try:
                    log.info(f"🧠 Procesando solicitud GPT para {phone}")
                    completion = rag_index.openai_client.chat.completions.create(
                        model="gpt-4o-mini",
                        messages=[{"role": "user", "content": prompt}],
                        temperature=0.4,
                    )
                    answer = completion.choices[0].message.content.strip()
                    send_message(phone, answer)
                    return jsonify({"ok": True}), 200
                except Exception:
                    log.exception("❌ Error llamando a OpenAI")
                    send_message(phone, "Hubo un detalle al procesar tu solicitud. Intentemos de nuevo.")
                    return jsonify({"ok": True}), 200

            _route_command(phone, text, match)
            return jsonify({"ok": True}), 200

        if mtype in {"image", "document", "audio", "video"}:
            log.info(f"📎 Multimedia recibida de {phone}: {mtype}")
            _handle_media(phone, msg)
            return jsonify({"ok": True}), 200

        log.info(f"ℹ️ Tipo de mensaje no manejado: {mtype}")
        return jsonify({"ok": True}), 200
    except Exception:
        log.exception("❌ Error en webhook_receive")
        return jsonify({"ok": True}), 200

# ==========================
# Endpoints auxiliares MEJORADOS
# ==========================
@app.get("/health")
def health():
    return jsonify({
        "status": "ok", 
        "service": "Vicky Bot Inbursa",
        "timestamp": datetime.utcnow().isoformat()
    }), 200

@app.get("/ext/health")
def ext_health():
    rag_status = "active" if rag_index and rag_index.index_cache else "inactive"
    rag_details = {}
    if rag_index and rag_index.index_cache:
        rag_details = {
            "documents_indexed": rag_index.index_cache.get('doc_count', 0),
            "chunks_indexed": rag_index.index_cache.get('chunk_count', 0),
            "last_refresh": rag_index.last_refresh
        }
    
    return jsonify({
        "status": "ok",
        "whatsapp_configured": bool(META_TOKEN and WABA_PHONE_ID),
        "google_ready": google_ready,
        "openai_ready": bool(rag_index and rag_index.openai_client),
        "rag_status": rag_status,
        "rag_details": rag_details
    }), 200

@app.post("/ext/test-send")
def ext_test_send():
    """Endpoint para pruebas de envío individual"""
    try:
        data = request.get_json(force=True) or {}
        to = str(data.get("to", "")).strip()
        text = str(data.get("text", "")).strip()
        
        if not to or not text:
            return jsonify({
                "ok": False, 
                "error": "Faltan parámetros 'to' o 'text'"
            }), 400
            
        log.info(f"🧪 Test send a {to}: {text}")
        ok = send_message(to, text)
        return jsonify({"ok": bool(ok)}), 200
    except Exception as e:
        log.exception("❌ Error en /ext/test-send")
        return jsonify({
            "ok": False, 
            "error": str(e)
        }), 500

# ==========================
# NUEVO ENDPOINT RAG
# ==========================
@app.post("/ext/reindex")
def force_reindex():
    """Forzar reindexación manual del RAG"""
    try:
        if not rag_index:
            return jsonify({
                "status": "error", 
                "message": "RAG no está inicializado"
            }), 400
            
        log.info("🔄 Forzando reindexación RAG...")
        result = rag_index.build_index()
        
        if result:
            return jsonify({
                "status": "success", 
                "message": "Índice reconstruido exitosamente",
                "documents": result.get('doc_count', 0),
                "chunks": result.get('chunk_count', 0)
            })
        else:
            return jsonify({
                "status": "error", 
                "message": "Error construyendo índice"
            }), 500
            
    except Exception as e:
        log.error(f"❌ Error en reindexación: {str(e)}")
        return jsonify({
            "status": "error", 
            "message": str(e)
        }), 500

def _bulk_send_worker(items: List[Dict[str, Any]]) -> None:
    """Worker mejorado para envíos masivos con logging exhaustivo"""
    successful = 0
    failed = 0
    
    log.info(f"🚀 Iniciando envío masivo de {len(items)} mensajes")
    
    for i, item in enumerate(items, 1):
        try:
            to = item.get("to", "").strip()
            text = item.get("text", "").strip()
            template = item.get("template", "").strip()
            params = item.get("params", [])
            
            if not to:
                log.warning(f"⏭️ Item {i} sin destinatario, omitiendo")
                failed += 1
                continue
                
            log.info(f"📤 [{i}/{len(items)}] Procesando: {to}")
            
            success = False
            if template:
                success = send_template_message(to, template, params)
                log.info(f"   ↳ Plantilla '{template}' a {to}: {'✅' if success else '❌'}")
            elif text:
                success = send_message(to, text)
                log.info(f"   ↳ Mensaje a {to}: {'✅' if success else '❌'}")
            else:
                log.warning(f"   ↳ Item {i} sin contenido válido")
                failed += 1
                continue
            
            if success:
                successful += 1
            else:
                failed += 1
                
            time.sleep(0.5)
            
        except Exception as e:
            failed += 1
            log.exception(f"❌ Error procesando item {i} para {item.get('to', 'unknown')}")
    
    log.info(f"🎯 Envío masivo completado: {successful} ✅, {failed} ❌")
    
    if ADVISOR_NUMBER:
        summary_msg = f"📊 Resumen envío masivo:\n• Exitosos: {successful}\n• Fallidos: {failed}\n• Total: {len(items)}"
        send_message(ADVISOR_NUMBER, summary_msg)

@app.post("/ext/send-promo")
def ext_send_promo():
    """Endpoint CORREGIDO para envíos masivos tipo WAPI"""
    try:
        if not META_TOKEN or not WABA_PHONE_ID:
            log.error("❌ META_TOKEN o WABA_PHONE_ID no configurados")
            return jsonify({
                "queued": False, 
                "error": "WhatsApp Business API no configurada"
            }), 500

        body = request.get_json(force=True) or {}
        items = body.get("items", [])
        
        log.info(f"📨 Recibida solicitud send-promo con {len(items)} items")
        
        if not isinstance(items, list):
            log.warning("❌ Formato inválido: items no es una lista")
            return jsonify({
                "queued": False, 
                "error": "Formato inválido: 'items' debe ser una lista"
            }), 400
            
        if not items:
            log.warning("❌ Lista de items vacía")
            return jsonify({
                "queued": False, 
                "error": "Lista 'items' vacía"
            }), 400

        valid_items = []
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                log.warning(f"⏭️ Item {i} no es un diccionario, omitiendo")
                continue
                
            to = item.get("to", "").strip()
            text = item.get("text", "").strip()
            template = item.get("template", "").strip()
            
            if not to:
                log.warning(f"⏭️ Item {i} sin destinatario, omitiendo")
                continue
                
            if not text and not template:
                log.warning(f"⏭️ Item {i} sin contenido (text o template), omitiendo")
                continue
                
            valid_items.append(item)

        if not valid_items:
            log.warning("❌ No hay items válidos después de la validación")
            return jsonify({
                "queued": False, 
                "error": "No hay items válidos para enviar"
            }), 400

        log.info(f"✅ Validación exitosa: {len(valid_items)} items válidos de {len(items)} recibidos")
        
        threading.Thread(
            target=_bulk_send_worker, 
            args=(valid_items,), 
            daemon=True,
            name="BulkSendWorker"
        ).start()
        
        response = {
            "queued": True,
            "message": f"Procesando {len(valid_items)} mensajes en background",
            "total_received": len(items),
            "valid_items": len(valid_items),
            "timestamp": datetime.utcnow().isoformat()
        }
        
        log.info(f"✅ Envío masivo encolado: {response}")
        return jsonify(response), 202
        
    except Exception as e:
        log.exception("❌ Error crítico en /ext/send-promo")
        return jsonify({
            "queued": False, 
            "error": f"Error interno: {str(e)}"
        }), 500

# ==========================
# Arranque (para desarrollo local)
# En producción usar Gunicorn: `gunicorn app:app --bind 0.0.0.0:$PORT`
# ==========================
if __name__ == "__main__":
    log.info(f"🚀 Iniciando Vicky Bot SECOM en puerto {PORT}")
    log.info(f"📞 WhatsApp configurado: {bool(META_TOKEN and WABA_PHONE_ID)}")
    log.info(f"📊 Google Sheets/Drive: {google_ready}")
    log.info(f"🧠 OpenAI: {bool(rag_index and rag_index.openai_client)}")
    log.info(f"📚 RAG Index: {bool(rag_index and rag_index.index_cache)}")
    
    app.run(host="0.0.0.0", port=PORT, debug=False)




