#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Vicky SECOM – modo Wapi
Archivo único listo para ejecutar con Gunicorn:
  gunicorn app:app --bind 0.0.0.0:$PORT

Dependencias (referencia para requirements.txt):
  Flask
  requests
  python-dotenv
  PyPDF2
  google-api-python-client
  google-auth
  google-auth-httplib2
  google-auth-oauthlib
  gspread
  oauth2client
  tenacity
  cachetools
  pytz
  openai

Resumen:
- Manejo robusto de Webhook de WhatsApp Cloud API (GET/POST).
- Envío de texto, plantillas e imágenes con reintentos y control QPS.
- Integración Google Sheets para leads (buscar por últimos 10 dígitos).
- Integración Drive para descargar manuales PDF y respaldar multimedia.
- RAG simple con cache (TTL 6h) y llamada a OpenAI.
- Cola en memoria para campañas/promociones con worker daemon.
- Logging profesional en español; mascar tokens y teléfonos en logs.
- Endpoints exactamente requeridos: /webhook (GET/POST), /ext/health,
  /ext/test-send (POST), /ext/send-promo (POST), /ext/manuales (GET).
"""

from __future__ import annotations
import os
import io
import json
import time
import re
import logging
import queue
import threading
import traceback
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List, Tuple
from functools import wraps
import mimetypes

import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from cachetools import TTLCache
import pytz
from PyPDF2 import PdfReader

from flask import Flask, request, jsonify, g, abort

# Google
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

# OpenAI
import openai

# -------------------------
# CONFIGURACIÓN DE LOGS
# -------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [%(request_id)s] %(message)s"
)
logger = logging.getLogger("vicky-secom-wapi")

# -------------------------
# UTILIDADES GLOBALES
# -------------------------
def gen_request_id() -> str:
    return f"{int(time.time() * 1000)}-{os.getpid()}"

def mask_token(t: Optional[str]) -> str:
    if not t:
        return "MISSING"
    s = str(t)
    if len(s) <= 8:
        return "*" * (len(s) - 2) + s[-2:]
    return "****" + s[-4:]

def mask_phone(s: Optional[str]) -> str:
    if not s:
        return "UNKNOWN"
    digits = re.sub(r"\D", "", s)
    if len(digits) <= 4:
        return "*" * max(0, len(digits)-1) + digits[-1:]
    return "*" * (len(digits) - 4) + digits[-4:]

# Flask context decorator to add request id to logs
def with_request_id(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        request_id = request.headers.get("X-Request-Id") or gen_request_id()
        g.request_id = request_id
        extra = {"request_id": request_id}
        # attach to logger adapter by temporarily setting filter
        old = logger.filters[:]
        try:
            logger = logging.getLogger("vicky-secom-wapi")
            # monkeypatch formatting via filter (simple)
            class ReqIdFilter(logging.Filter):
                def filter(self, record):
                    record.request_id = request_id
                    return True
            logger.addFilter(ReqIdFilter())
            return f(*args, **kwargs)
        finally:
            # cleanup filters
            logger.filters = old
    return wrapped

# Helper for logging with masked sensitive info
def log_info(msg: str, **kw):
    logger.info(msg + " " + " ".join([f"{k}={mask_token(v) if 'token' in k else mask_phone(v) if 'phone' in k else v}" for k,v in kw.items()]))

# -------------------------
# ENV VARS (OBLIGATORIAS)
# -------------------------
REQUIRED_ENVS = [
    "META_TOKEN",
    "WABA_PHONE_ID",
    "VERIFY_TOKEN",
    "ADVISOR_NUMBER",
    "OPENAI_API_KEY",
    "GOOGLE_CREDENTIALS_JSON",
    "SHEETS_ID_LEADS",
    "SHEETS_TITLE_LEADS"
]

env = os.environ.copy()
_missing = [k for k in REQUIRED_ENVS if not env.get(k)]
if _missing:
    for k in _missing:
        logger.critical(f"Variable de entorno obligatoria faltante: {k}")
    raise RuntimeError(f"Variables de entorno obligatorias faltantes: {_missing}")

# Load envs
META_TOKEN = env["META_TOKEN"]
WABA_PHONE_ID = env["WABA_PHONE_ID"]
VERIFY_TOKEN = env["VERIFY_TOKEN"]
ADVISOR_NUMBER = env["ADVISOR_NUMBER"]
OPENAI_API_KEY = env["OPENAI_API_KEY"]
GOOGLE_CREDENTIALS_JSON = env["GOOGLE_CREDENTIALS_JSON"]
SHEETS_ID_LEADS = env["SHEETS_ID_LEADS"]
SHEETS_TITLE_LEADS = env["SHEETS_TITLE_LEADS"]

# Optionals
LEADS_VICKY_SHEET_ID = env.get("LEADS_VICKY_SHEET_ID")
LEADS_VICKY_SHEET_TITLE = env.get("LEADS_VICKY_SHEET_TITLE")
RAG_AUTO_FILE_ID = env.get("RAG_AUTO_FILE_ID")
RAG_IMSS_FILE_ID = env.get("RAG_IMSS_FILE_ID")
RAG_AUTO_FILE_NAME = env.get("RAG_AUTO_FILE_NAME")
RAG_IMSS_FILE_NAME = env.get("RAG_IMSS_FILE_NAME")
DRIVE_UPLOAD_ROOT_FOLDER_ID = env.get("DRIVE_UPLOAD_ROOT_FOLDER_ID")

# Masked logging for startup
logger.info("Inicializando Vicky SECOM WAPI")
logger.info(f"META_TOKEN={mask_token(META_TOKEN)} WABA_PHONE_ID={mask_phone(WABA_PHONE_ID)} ADVISOR_NUMBER={mask_phone(ADVISOR_NUMBER)}")

# -------------------------
# CONSTANTES Y ESTRUCTURAS
# -------------------------
FLASK_TZ = pytz.timezone("America/Mazatlan")
QPS_LIMIT = 5  # QPS allowed for WhatsApp outbound in worker (throttling)
PROMO_BATCH_LIMIT = 100

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False

# In-memory state & caches
state: Dict[str, Dict[str, Any]] = {}  # state[wa_id] = {"stage": ..., "expires_at": ...}
state_ttl_seconds = 60 * 60  # 1 hora por defecto

# RAG cache: domain -> {text, loaded_at}
rag_cache = TTLCache(maxsize=4, ttl=6 * 3600)  # 6 horas

# Promo queue
promo_queue = queue.Queue()

# Google clients placeholders
sheets_client = None
drive_service = None

# OpenAI
openai.api_key = OPENAI_API_KEY

# -------------------------
# UTILIDADES DE TELEFONO
# -------------------------
def normalize_msisdn(s: str) -> str:
    if not s:
        return ""
    digits = re.sub(r"\D", "", s)
    # If already starts with country code e.g., 52...
    if digits.startswith("521") and len(digits) == 13:
        return digits
    # If starts with 52 and 12 digits
    if digits.startswith("52") and len(digits) == 12:
        return "5" + digits[1:] if not digits.startswith("521") else digits
    # If 10 digits (local), prefix 521
    if len(digits) == 10:
        return "521" + digits
    # If 11 digits starting with 1? not expected, fallback
    if len(digits) == 11 and digits.startswith("1"):
        return "52" + digits
    # If already includes country but different, try to normalize to last 11 digits with 521
    if len(digits) > 10:
        return "521" + digits[-10:]
    return digits

def last10(msisdn: str) -> str:
    d = re.sub(r"\D", "", msisdn or "")
    return d[-10:] if len(d) >= 10 else d

# -------------------------
# CLIENTE WHATSAPP (ENVIOS)
# -------------------------
WHATSAPP_API_BASE = f"https://graph.facebook.com/v17.0/{WABA_PHONE_ID}/messages"
session = requests.Session()
session.headers.update({"Authorization": f"Bearer {META_TOKEN}"})

# Retry decorator for HTTP operations (whatsapp send)
def is_transient_exception(exc):
    return isinstance(exc, requests.RequestException)

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8),
       retry=retry_if_exception_type(requests.RequestException))
def _post_whatsapp(payload: dict) -> dict:
    resp = session.post(WHATSAPP_API_BASE, json=payload, timeout=30)
    if resp.status_code not in (200, 201):
        logger.warning(f"WhatsApp API returned {resp.status_code}: {resp.text}")
        resp.raise_for_status()
    return resp.json()

# Rate limiting helper for promo worker
_last_send_ts = 0.0
_send_lock = threading.Lock()

def _rate_limit_sleep():
    global _last_send_ts
    with _send_lock:
        now = time.time()
        min_interval = 1.0 / QPS_LIMIT
        delta = now - _last_send_ts
        if delta < min_interval:
            time.sleep(min_interval - delta)
        _last_send_ts = time.time()

def send_text(to: str, text: str) -> Tuple[bool, dict]:
    to_norm = normalize_msisdn(to)
    payload = {
        "messaging_product": "whatsapp",
        "to": to_norm,
        "type": "text",
        "text": {"body": text}
    }
    try:
        _rate_limit_sleep()
        resp = _post_whatsapp(payload)
        message_id = resp.get("messages", [{}])[0].get("id")
        logger.info(f"Mensaje texto enviado a {mask_phone(to)} message_id={message_id}")
        return True, resp
    except Exception as e:
        logger.error(f"Error enviando texto a {mask_phone(to)}: {str(e)}")
        return False, {"error": str(e)}

def send_template(to: str, template_name: str, language_code: str = "es_MX", components: Optional[list] = None) -> Tuple[bool, dict]:
    to_norm = normalize_msisdn(to)
    payload = {
        "messaging_product": "whatsapp",
        "to": to_norm,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language_code},
        }
    }
    if components:
        payload["template"]["components"] = components
    try:
        _rate_limit_sleep()
        resp = _post_whatsapp(payload)
        message_id = resp.get("messages", [{}])[0].get("id")
        logger.info(f"Template enviado a {mask_phone(to)} message_id={message_id} template={template_name}")
        return True, resp
    except Exception as e:
        logger.error(f"Error enviando template a {mask_phone(to)}: {str(e)}")
        return False, {"error": str(e)}

def send_image_url(to: str, image_url: str, caption: Optional[str] = None) -> Tuple[bool, dict]:
    to_norm = normalize_msisdn(to)
    media = {"link": image_url}
    payload = {
        "messaging_product": "whatsapp",
        "to": to_norm,
        "type": "image",
        "image": media
    }
    if caption:
        payload["image"]["caption"] = caption
    try:
        _rate_limit_sleep()
        resp = _post_whatsapp(payload)
        message_id = resp.get("messages", [{}])[0].get("id")
        logger.info(f"Imagen enviada a {mask_phone(to)} message_id={message_id} url={image_url}")
        return True, resp
    except Exception as e:
        logger.error(f"Error enviando imagen a {mask_phone(to)}: {str(e)}")
        return False, {"error": str(e)}

# -------------------------
# GOOGLE SHEETS & DRIVE
# -------------------------
def initialize_google_clients():
    global sheets_client, drive_service
    try:
        creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        sheets_client = gspread.authorize(creds)
        drive_service = build("drive", "v3", credentials=creds, cache_discovery=False)
        logger.info("Cliente Google inicializado correctamente")
    except Exception as e:
        logger.critical("Error inicializando Google clients: %s", str(e))
        raise

initialize_google_clients()

# Sheets helpers
def open_sheet_by_id(sheet_id: str):
    try:
        sh = sheets_client.open_by_key(sheet_id)
        return sh
    except Exception as e:
        logger.error(f"Error abriendo hoja {sheet_id}: {str(e)}")
        raise

# Ensure columns exist and return header map col_name -> index (1-based)
def ensure_sheet_headers(sheet, desired_cols: List[str]) -> Dict[str,int]:
    try:
        header = sheet.row_values(1)
        if not header:
            # create header
            sheet.insert_row(desired_cols, index=1)
            header = desired_cols
        # fill missing columns at end
        missing = [c for c in desired_cols if c not in header]
        if missing:
            sheet.append_row([])  # trick to ensure sheet extends
            header = header + missing
            sheet.update('1:1', [header])
        # map
        return {col: idx+1 for idx, col in enumerate(header)}
    except Exception as e:
        logger.error("Error en ensure_sheet_headers: %s", str(e))
        raise

# Fields minimal required
SHEET_MIN_FIELDS = ["status","greeted_at","renovacion_vencimiento","recordar_30d","reintento_7d","campaña_origen","notas","wa_last10","nombre"]

def get_leads_worksheet():
    sh = open_sheet_by_id(SHEETS_ID_LEADS)
    try:
        ws = sh.worksheet(SHEETS_TITLE_LEADS)
    except Exception:
        ws = sh.add_worksheet(title=SHEETS_TITLE_LEADS, rows="1000", cols="20")
    ensure_sheet_headers(ws, SHEET_MIN_FIELDS)
    return ws

def find_or_create_contact_row(wa_last10: str) -> int:
    ws = get_leads_worksheet()
    all_values = ws.get_all_records()
    # look for wa_last10 column
    for i, row in enumerate(all_values, start=2):
        if str(row.get("wa_last10","")).strip().endswith(wa_last10):
            return i
    # not found: append new row with wa_last10 and status nuevo
    new_row = {k: "" for k in SHEET_MIN_FIELDS}
    new_row["status"] = "nuevo"
    new_row["wa_last10"] = wa_last10
    new_row["greeted_at"] = ""
    # create row values ordered by header
    header = ws.row_values(1)
    row_values = [new_row.get(col,"") for col in header]
    ws.append_row(row_values)
    return len(ws.get_all_values())

def get_contact_data(row_index: int) -> Dict[str, Any]:
    ws = get_leads_worksheet()
    header = ws.row_values(1)
    row = ws.row_values(row_index)
    data = {}
    for i, col in enumerate(header):
        data[col] = row[i] if i < len(row) else ""
    return data

def set_contact_data(row_index: int, fields: Dict[str, Any]) -> None:
    ws = get_leads_worksheet()
    header = ws.row_values(1)
    # prepare row with existing values
    existing = ws.row_values(row_index)
    new_row = existing + [""] * max(0, len(header) - len(existing))
    for k,v in fields.items():
        if k in header:
            new_row[header.index(k)] = str(v)
    ws.update(f"1:1", [header])  # ensure header (harmless)
    ws.update(f"{row_index}:{row_index}", [new_row])

# append note to 'notas' column with timestamp
def append_note_to_contact(row_index: int, note: str):
    data = get_contact_data(row_index)
    prev = data.get("notas", "")
    ts = datetime.now(FLASK_TZ).isoformat()
    new_notes = (prev + "\n" + f"[{ts}] {note}").strip()
    set_contact_data(row_index, {"notas": new_notes})

# -------------------------
# DRIVE: RAG & MEDIA BACKUP
# -------------------------
def _drive_find_pdf_by_id(file_id: str) -> Optional[Dict[str,Any]]:
    try:
        f = drive_service.files().get(fileId=file_id, fields="id,name,mimeType").execute()
        if f and f.get("mimeType") == "application/pdf":
            return f
    except Exception as e:
        logger.warning(f"No se pudo obtener archivo Drive por ID {file_id}: {e}")
    return None

def _drive_search_pdf_by_name(name: str) -> Optional[Dict[str,Any]]:
    try:
        q = f"name = '{name}' and mimeType='application/pdf' and trashed=false"
        res = drive_service.files().list(q=q, pageSize=10, fields="files(id,name,mimeType)").execute()
        files = res.get("files", [])
        if files:
            return files[0]
    except Exception as e:
        logger.warning(f"Error buscando PDF por nombre {name}: {e}")
    return None

def _drive_download_file_bytes(file_id: str) -> Optional[bytes]:
    try:
        request = drive_service.files().get_media(fileId=file_id)
        fh = io.BytesIO()
        downloader = None
        # fallback streaming
        from googleapiclient.http import MediaIoBaseDownload
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            status, done = downloader.next_chunk()
        fh.seek(0)
        return fh.read()
    except Exception as e:
        logger.error(f"Error descargando archivo Drive {file_id}: {e}")
        return None

def _extract_text_from_pdf_bytes(b: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(b))
        texts = []
        for p in reader.pages:
            try:
                texts.append(p.extract_text() or "")
            except Exception:
                texts.append("")
        return "\n".join(texts).strip()
    except Exception as e:
        logger.error("Error extrayendo texto PDF: %s", str(e))
        # fallback raw bytes decode
        try:
            return b.decode('utf-8', errors='ignore')
        except Exception:
            return ""

def load_manual_to_cache(domain: str) -> bool:
    """
    domain in {'auto','imss'}
    carga el manual (por ID preferente, por nombre fallback) y guarda en rag_cache
    """
    try:
        if domain == "auto":
            file_id = RAG_AUTO_FILE_ID
            file_name = RAG_AUTO_FILE_NAME
        else:
            file_id = RAG_IMSS_FILE_ID
            file_name = RAG_IMSS_FILE_NAME

        found = None
        if file_id:
            found = _drive_find_pdf_by_id(file_id)
        if not found and file_name:
            found = _drive_search_pdf_by_name(file_name)
        if not found:
            logger.warning(f"No se encontró manual Drive para domain={domain}")
            return False
        file_bytes = _drive_download_file_bytes(found["id"])
        if not file_bytes:
            logger.warning(f"No se pudo descargar manual {found.get('name')}")
            return False
        text = _extract_text_from_pdf_bytes(file_bytes)
        if not text:
            logger.warning(f"Manual {found.get('name')} sin texto extraído")
            return False
        rag_cache[domain] = {"text": text, "loaded_at": time.time(), "name": found.get("name")}
        logger.info(f"Manual cargado en cache domain={domain} name={found.get('name')} chars={len(text)}")
        return True
    except Exception as e:
        logger.error(f"Error cargando manual {domain}: {e}")
        return False

def rag_answer(query: str, domain: str = "auto") -> str:
    """
    Responde usando el manual del domain. Si no hay soporte, indica que no está en el manual.
    Uso simple: busca oraciones con coincidencia naive de palabras clave y compone prompt para OpenAI.
    """
    if domain not in ("auto","imss"):
        domain = "auto"
    if domain not in rag_cache:
        ok = load_manual_to_cache(domain)
        if not ok:
            return "No encuentro información en el manual correspondiente en este momento."
    manual = rag_cache.get(domain, {})
    text = manual.get("text","")
    name = manual.get("name","manual")
    if not text:
        return f"No encuentro información en el manual {name}."
    # Simple chunking: split into paragraphs and score by token overlap
    query_words = set([w.lower() for w in re.findall(r"\w{3,}", query)])
    paragraphs = [p.strip() for p in re.split(r"\n{1,}", text) if p.strip()]
    scored = []
    for p in paragraphs:
        words = set([w.lower() for w in re.findall(r"\w{3,}", p)])
        common = query_words.intersection(words)
        score = len(common)
        if score > 0:
            scored.append((score, p))
    # take top 3
    scored.sort(reverse=True, key=lambda x: x[0])
    context = "\n\n".join([p for _,p in scored[:3]]) if scored else "\n\n".join(paragraphs[:3])
    # build prompt
    prompt = (
        f"Eres Vicky, asistente basada en el manual '{name}'. Usa SOLO la información del contexto provisto.\n\n"
        f"Contexto relevante:\n{context}\n\n"
        f"Pregunta: {query}\n\n"
        f"Si la respuesta no está en el contexto, responde exactamente: 'No encuentro esta información en el manual correspondiente.'\n"
        f"Al final agrega: 'Esta info proviene del manual {name}'. Responde en español, de forma breve y clara."
    )
    try:
        resp = openai.ChatCompletion.create(
            model="gpt-4o-mini" if "gpt-4o-mini" in openai.Model.list() else "gpt-4o-mini",
            messages=[{"role":"user","content":prompt}],
            temperature=0.2,
            max_tokens=300
        )
        text_resp = resp.choices[0].message.content.strip()
        return text_resp
    except Exception as e:
        logger.error(f"Error llamando OpenAI para RAG: {e}")
        return f"No puedo procesar la consulta ahora. (Error interno)."

# Media backup to Drive
def backup_media_to_drive(file_bytes: bytes, filename: str, mime_type: str, contact_name: Optional[str], wa_last4: str) -> Optional[str]:
    if not DRIVE_UPLOAD_ROOT_FOLDER_ID:
        logger.info("No DRIVE_UPLOAD_ROOT_FOLDER_ID configurado; omitiendo respaldo")
        return None
    try:
        # create folder name
        folder_name = f"{(contact_name or 'Cliente')}_{wa_last4}"
        # search or create subfolder under root
        q = f"name = '{folder_name}' and mimeType = 'application/vnd.google-apps.folder' and '{DRIVE_UPLOAD_ROOT_FOLDER_ID}' in parents and trashed=false"
        res = drive_service.files().list(q=q, fields="files(id,name)").execute()
        files = res.get("files", [])
        if files:
            folder_id = files[0]["id"]
        else:
            file_metadata = {"name": folder_name, "mimeType": "application/vnd.google-apps.folder", "parents": [DRIVE_UPLOAD_ROOT_FOLDER_ID]}
            created = drive_service.files().create(body=file_metadata, fields="id").execute()
            folder_id = created["id"]
        # upload file
        fh = io.BytesIO(file_bytes)
        media_body = MediaIoBaseUpload(fh, mimetype=mime_type, resumable=True)
        file_metadata = {"name": filename, "parents": [folder_id]}
        uploaded = drive_service.files().create(body=file_metadata, media_body=media_body, fields="id,webViewLink").execute()
        link = uploaded.get("webViewLink") or f"https://drive.google.com/file/d/{uploaded.get('id')}/view"
        logger.info(f"Media respaldada en Drive: {filename} link={link}")
        return link
    except Exception as e:
        logger.error(f"Error subiendo media a Drive: {e}")
        return None

# -------------------------
# HELPERS: SALUDO PROTEGIDO
# -------------------------
def should_greet(wa_last10: str) -> bool:
    """
    Comprueba la hoja greeted_at para no saludar si < 24h.
    Usa la zona FLASK_TZ.
    """
    try:
        row = find_or_create_contact_row(wa_last10)
        data = get_contact_data(row)
        greeted_at = data.get("greeted_at")
        if not greeted_at:
            return True
        try:
            dt = datetime.fromisoformat(greeted_at)
            dt = dt.astimezone(FLASK_TZ)
            if datetime.now(FLASK_TZ) - dt > timedelta(hours=24):
                return True
            return False
        except Exception:
            return True
    except Exception as e:
        logger.error(f"Error en should_greet: {e}")
        return True

def mark_greeted(wa_last10: str):
    try:
        row = find_or_create_contact_row(wa_last10)
        now_iso = datetime.now(FLASK_TZ).isoformat()
        set_contact_data(row, {"greeted_at": now_iso})
    except Exception as e:
        logger.error(f"Error en mark_greeted: {e}")

# -------------------------
# LÓGICA DE FLUJOS (AUTO e IMSS)
# -------------------------
def handle_auto_flow(wa_id: str, text: str, row_index: int):
    st = state.setdefault(wa_id, {"stage":"AUTO_START","updated":time.time()})
    stage = st.get("stage","AUTO_START")
    t = text.strip().lower()
    if stage == "AUTO_START":
        # send brief plans and request INE or placa
        msg = (
            "Planes Auto SECOM:\n\n"
            "1) Amplia Plus – Cobertura completa + defensa jurídica.\n"
            "2) Amplia – Cobertura amplia y robo.\n"
            "3) Limitada – Responsabilidad a terceros.\n\n"
            "Para cotizar necesito tu INE (foto o folio) o la placa/tarjeta de circulación. "
            "Responde con 'INE: <tu texto>' o 'PLACA: <AAA111A'."
        )
        send_text(wa_id, msg)
        st["stage"] = "AUTO_DOCS"
        st["updated"]=time.time()
        return
    if stage == "AUTO_DOCS":
        # check for mention of INE or PLACA
        if t.startswith("ine") or "ine" in t:
            # store brief marker
            append_note_to_contact(row_index, "INE provisto (texto): " + text)
            send_text(wa_id, "Gracias. También por favor proporciona tarjeta de circulación o placa si la tienes (placa o tarjeta).")
            st["stage"]="AUTO_PLAN"
            st["updated"]=time.time()
            return
        if t.startswith("placa") or "placa" in t or "tarjeta" in t:
            append_note_to_contact(row_index, "Placa/tarjeta provista: " + text)
            send_text(wa_id, "Perfecto. ¿Te interesa alguna de las siguientes opciones? Responde 1, 2 o 3.")
            st["stage"]="AUTO_PLAN"
            st["updated"]=time.time()
            return
        # fallback
        send_text(wa_id, "No entendí. Por favor responde con 'INE: <datos>' o 'PLACA: <datos>' para continuar.")
        return
    if stage == "AUTO_PLAN":
        if "1" in t or "amplia plus" in t:
            plan = "Amplia Plus"
        elif "2" in t or "amplia" in t:
            plan = "Amplia"
        elif "3" in t or "limitada" in t:
            plan = "Limitada"
        else:
            # check renewal indication
            if "renov" in t or "vencim" in t or "ya tengo" in t or "póliza" in t:
                # ask for date
                send_text(wa_id, "¿Cuál es la fecha de vencimiento de tu póliza? Formato dd-mm-aaaa o aaaa-mm-dd")
                st["stage"]="AUTO_RENOV"
                st["updated"]=time.time()
                return
            send_text(wa_id, "Selecciona 1, 2 o 3 para elegir un plan. Si quieres hablar con asesor escribe 'asesor'.")
            return
        append_note_to_contact(row_index, f"Plan elegido: {plan}")
        set_contact_data(row_index, {"status":"en_seguimiento"})
        send_text(wa_id, f"Has seleccionado {plan}. Para continuar, ¿quieres que un asesor te contacte? Escribe 'asesor' para que te llamen.")
        st["stage"]="AUTO_RESUMEN"
        st["updated"]=time.time()
        return
    if stage == "AUTO_RENOV":
        # validate date
        d = parse_date_from_text(text)
        if d:
            set_contact_data(row_index, {"renovacion_vencimiento": d.isoformat(), "recordar_30d":"TRUE"})
            append_note_to_contact(row_index, f"Fecha de renovación guardada: {d.isoformat()}")
            set_contact_data(row_index, {"status":"en_seguimiento"})
            send_text(wa_id, f"Gracias. Guardé la fecha {d.date().isoformat()}. Te programo recordatorio ~30 días antes. ¿Quieres hablar con asesor ahora? Responde 'asesor' o 'no'.")
            # indicate reintento_7d if no followup later (simple flag)
            set_contact_data(row_index, {"reintento_7d":"TRUE"})
            st["stage"]="AUTO_RESUMEN"
            st["updated"]=time.time()
            return
        else:
            send_text(wa_id, "No pude reconocer la fecha. Usa dd-mm-aaaa o aaaa-mm-dd. Ejemplo: 30-11-2025 o 2025-11-30")
            return
    if stage == "AUTO_RESUMEN":
        if "asesor" in t or "contacto" in t or "llámame" in t:
            notify_advisor(wa_id, row_index, "Solicitud de contacto desde flujo AUTO")
            send_text(wa_id, "He notificado a un asesor. Te contactarán pronto. Número de ticket interno enviado.")
            set_contact_data(row_index, {"status":"en_seguimiento"})
            st["stage"]="DONE"
            st["updated"]=time.time()
            return
        send_text(wa_id, "Gracias por la información. Si necesitas algo más escribe 'menu' para ver opciones.")
        st["stage"]="DONE"
        st["updated"]=time.time()
        return

def handle_imss_flow(wa_id: str, text: str, row_index: int):
    st = state.setdefault(wa_id, {"stage":"IMSS_START","updated":time.time()})
    stage = st.get("stage","IMSS_START")
    t = text.strip().lower()
    if stage == "IMSS_START":
        send_text(wa_id, "IMSS - Ley 73: ¿Buscas información sobre requisitos, cálculo o préstamos Ley 73? Responde 'requisitos', 'cálculo' o 'prestamo'.")
        st["stage"]="IMSS_QUALIFY"
        st["updated"]=time.time()
        return
    if stage == "IMSS_QUALIFY":
        if "requisitos" in t:
            resp = rag_answer("requisitos para pensión IMSS", domain="imss")
            send_text(wa_id, resp)
            st["stage"]="IMSS_FOLLOW"
            st["updated"]=time.time()
            return
        if "cálculo" in t or "calculo" in t or "monto" in t:
            send_text(wa_id, "Para calcular necesitamos: salario promedio últimos 5 años, semanas cotizadas y edad. ¿Deseas que un asesor te ayude con esto? Responde 'sí' o 'no'.")
            st["stage"]="IMSS_CALC"
            st["updated"]=time.time()
            return
        if "prestamo" in t or "ley 73" in t:
            send_text(wa_id, "Préstamos Ley 73: hasta 12 meses de pensión, tasa preferencial. ¿Quieres que te contacte un asesor para simular monto?")
            st["stage"]="IMSS_FOLLOW"
            st["updated"]=time.time()
            return
        send_text(wa_id, "No entendí. Responde 'requisitos', 'cálculo' o 'prestamo'.")
        return
    if stage == "IMSS_CALC":
        if "sí" in t or "si" in t:
            notify_advisor(wa_id, row_index, "Cliente solicita cálculo IMSS/Ley73")
            append_note_to_contact(row_index, "Solicitó cálculo IMSS/Ley73")
            set_contact_data(row_index, {"status":"en_seguimiento"})
            send_text(wa_id, "He notificado a un asesor. Te contactarán para el cálculo.")
            st["stage"]="DONE"
            st["updated"]=time.time()
            return
        send_text(wa_id, "Entiendo. Si cambias de opinión escribe 'asesor' para que te contacten.")
        st["stage"]="DONE"
        st["updated"]=time.time()
        return
    if stage == "IMSS_FOLLOW":
        if "asesor" in t or "contacto" in t:
            notify_advisor(wa_id, row_index, "Solicitud de contacto desde flujo IMSS")
            send_text(wa_id, "Perfecto. Un asesor te contactará en breve.")
            set_contact_data(row_index, {"status":"en_seguimiento"})
            st["stage"]="DONE"
            st["updated"]=time.time()
            return
        send_text(wa_id, "Si necesitas algo más, escribe 'menu'.")
        st["stage"]="DONE"
        st["updated"]=time.time()
        return

# -------------------------
# UTILIDADES GENERALES
# -------------------------
def parse_date_from_text(s: str) -> Optional[datetime]:
    s = s.strip()
    for fmt in ("%d-%m-%Y","%Y-%m-%d","%d/%m/%Y"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.replace(tzinfo=FLASK_TZ)
        except Exception:
            continue
    # try to find pattern in text
    m = re.search(r"(\d{2})[-/](\d{2})[-/](\d{4})", s)
    if m:
        try:
            dt = datetime.strptime(m.group(0), "%d-%m-%Y")
            return dt.replace(tzinfo=FLASK_TZ)
        except Exception:
            pass
    return None

def notify_advisor(wa_id: str, row_index: int, reason: str):
    # build summary
    contact = get_contact_data(row_index)
    nombre = contact.get("nombre") or "Desconocido"
    last_msg = ""  # could be enhanced
    campaign = contact.get("campaña_origen","")
    body = (
        f"Asesor: nuevo contacto\n"
        f"Nombre: {nombre}\n"
        f"wa_id: {wa_id}\n"
        f"Motivo: {reason}\n"
        f"Campaña: {campaign}\n"
        f"Notas: {contact.get('notas','')[:300]}"
    )
    send_text(ADVISOR_NUMBER, body)

# -------------------------
# PROMO WORKER (COLA IN-MEMORY)
# -------------------------
def promo_worker():
    logger.info("Promo worker iniciado (daemon)")
    while True:
        try:
            job = promo_queue.get()
            if not job:
                time.sleep(1)
                continue
            campaign = job.get("campaign")
            recipients = job.get("recipients",[])
            mode = job.get("mode")
            text = job.get("text")
            template = job.get("template")
            image = job.get("image")
            logger.info(f"Procesando job campaña={campaign} recipients={len(recipients)} mode={mode}")
            # process in batches up to PROMO_BATCH_LIMIT
            idx = 0
            while idx < len(recipients):
                batch = recipients[idx:idx+PROMO_BATCH_LIMIT]
                for r in batch:
                    try:
                        if mode == "text":
                            ok, resp = send_text(r, text)
                        elif mode == "template":
                            ok, resp = send_template(r, template.get("name"), template.get("language","es_MX"), components=template.get("components"))
                        elif mode == "image":
                            ok, resp = send_image_url(r, image.get("url"), caption=image.get("caption"))
                        else:
                            logger.warning(f"Modo desconocido en promo: {mode}")
                            ok = False
                            resp = {"error":"modo desconocido"}
                        if ok:
                            mid = resp.get("messages",[{}])[0].get("id")
                            logger.info(f"Promo enviado a {mask_phone(r)} mid={mid}")
                        else:
                            logger.warning(f"Promo fallo a {mask_phone(r)} resp={resp}")
                    except Exception as e:
                        logger.error(f"Excepción enviando promo a {mask_phone(r)}: {traceback.format_exc()}")
                    time.sleep(max(0.02, 1.0 / QPS_LIMIT))  # small spacing
                idx += PROMO_BATCH_LIMIT
            promo_queue.task_done()
        except Exception as e:
            logger.error("Error en promo_worker: %s", str(e))
            time.sleep(2)

promo_thread = threading.Thread(target=promo_worker, daemon=True)
promo_thread.start()

# -------------------------
# ENDPOINTS EXACTOS
# -------------------------
@app.before_request
def attach_request_id():
    request_id = request.headers.get("X-Request-Id") or gen_request_id()
    g.request_id = request_id
    # ensure logger shows it
    for handler in logging.root.handlers:
        pass  # formatter already uses request_id via filter in init

@app.errorhandler(Exception)
def handle_exception(e):
    logger.error("Excepción no controlada: %s\n%s", str(e), traceback.format_exc())
    return jsonify({"status":"error","message":"Error interno"}), 500

@app.route("/webhook", methods=["GET"])
def webhook_verify():
    # Verificación GET del webhook
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    logger.info("Webhook verify request received")
    if mode and token:
        if mode == "subscribe" and token == VERIFY_TOKEN:
            logger.info("Webhook verificado correctamente")
            return challenge, 200
        else:
            logger.warning("Token de verificación inválido")
            return "Forbidden", 403
    return "Bad Request", 400

@app.route("/webhook", methods=["POST"])
def webhook_receive():
    payload = request.get_json(silent=True)
    if not payload:
        logger.info("Payload vacío recibido")
        return jsonify({"ok": True}), 200
    logger.info(f"Payload recibido tamaño={len(json.dumps(payload))}")
    # parse typical WhatsApp Cloud structure
    entries = payload.get("entry", [])
    for entry in entries:
        changes = entry.get("changes", [])
        for change in changes:
            value = change.get("value", {})
            messages = value.get("messages", []) or []
            contacts = value.get("contacts", []) or []
            for msg in messages:
                wa_from = msg.get("from")
                wa_id = normalize_msisdn(wa_from)
                wa_last10 = last10(wa_id)
                wa_last4 = (re.sub(r"\D","",wa_id)[-4:] if wa_id else "0000")
                # find or create contact row
                try:
                    row = find_or_create_contact_row(wa_last10)
                except Exception as e:
                    logger.error("Error accediendo Sheets para contacto: %s", e)
                    row = None
                # greeting logic
                if should_greet(wa_last10):
                    # greet once
                    greeting = ("Hola 👋 Soy Vicky de SECOM. Escríbe *AUTO*, *IMSS* o *CONTACTO* para comenzar.")
                    send_text(wa_id, greeting)
                    mark_greeted(wa_last10)
                # handle message type
                mtype = msg.get("type")
                if mtype == "text":
                    text = msg.get("text",{}).get("body","")
                    logger.info(f"Mensaje texto de {mask_phone(wa_id)}: {text[:200]}")
                    # Routing simple by keywords
                    t_low = text.lower()
                    if "auto" in t_low:
                        try:
                            handle_auto_flow(wa_id, text, row)
                        except Exception as e:
                            logger.error("Error en flow AUTO: %s", traceback.format_exc())
                            send_text(wa_id, "Lo siento, hubo un error procesando tu solicitud. Intenta más tarde.")
                    elif "imss" in t_low:
                        try:
                            handle_imss_flow(wa_id, text, row)
                        except Exception as e:
                            logger.error("Error en flow IMSS: %s", traceback.format_exc())
                            send_text(wa_id, "Lo siento, hubo un error procesando tu solicitud. Intenta más tarde.")
                    elif any(k in t_low for k in ["asesor","contacto","llámame","llamame","hablar con christian","christian"]):
                        notify_advisor(wa_id, row, "Solicitud de contacto directo por cliente")
                        send_text(wa_id, "He notificado a Christian (asesor). Te contactarán pronto.")
                    elif any(q in t_low for q in ["cómo","como","requisitos","cobertura","qué","que","cuando","cuándo","dónde","donde"]):
                        # attempt RAG: determine domain heuristically
                        domain = "auto" if "auto" in t_low or "cobertura" in t_low or "deducible" in t_low else "imss" if "imss" in t_low or "pensión" in t_low or "pension" in t_low else None
                        if not domain:
                            send_text(wa_id, "¿Tu duda es sobre *Auto* o *IMSS*? Responde con la palabra correspondiente.")
                        else:
                            resp = rag_answer(text, domain)
                            send_text(wa_id, resp)
                    else:
                        # fallback: suggest menu or ask clarification
                        send_text(wa_id, "No entendí completamente. Escribe *AUTO*, *IMSS* o *CONTACTO* para iniciar.")
                elif mtype in ("image","document","audio","video","sticker","interactive"):
                    # handle media: download if available and backup
                    # Whatsapp Cloud sends media id in msg['image']['id'] etc. Need to request /{media_id}
                    media_info = msg.get(mtype, {})
                    media_id = media_info.get("id")
                    if media_id:
                        try:
                            # obtain media URL
                            media_resp = session.get(f"https://graph.facebook.com/v17.0/{media_id}", params={"fields":"url"}, timeout=20)
                            media_json = media_resp.json()
                            media_url = media_json.get("url")
                            if media_url:
                                # download media
                                mdata = requests.get(media_url, headers={"Authorization": f"Bearer {META_TOKEN}"}, timeout=30)
                                b = mdata.content
                                mime_type = mdata.headers.get("Content-Type", "application/octet-stream")
                                ext = mimetypes.guess_extension(mime_type) or ""
                                filename = f"{mtype}_{wa_last4}{ext}"
                                link = backup_media_to_drive(b, filename, mime_type, contact_name=None, wa_last4=wa_last4)
                                if link:
                                    append_note_to_contact(row, f"Media respaldada: {link}")
                                    send_text(wa_id, "Archivo recibido y respaldado. Gracias.")
                                else:
                                    send_text(wa_id, "Archivo recibido. Gracias.")
                            else:
                                send_text(wa_id, "Archivo recibido. Gracias.")
                        except Exception as e:
                            logger.error("Error procesando media: %s", traceback.format_exc())
                            send_text(wa_id, "Recibí tu archivo pero no pude procesarlo completamente.")
                    else:
                        send_text(wa_id, "Archivo recibido. Gracias.")
                else:
                    logger.info("Tipo de mensaje no soportado: %s", mtype)
                    send_text(wa_id, "Mensaje recibido. ¿En qué puedo ayudarte?")
    return jsonify({"ok": True}), 200

@app.route("/ext/health", methods=["GET"])
def ext_health():
    return jsonify({"status":"ok"}), 200

@app.route("/ext/test-send", methods=["POST"])
def ext_test_send():
    body = request.get_json(silent=True) or {}
    to = body.get("to")
    text = body.get("text","Prueba Vicky")
    if not to:
        return jsonify({"error":"missing 'to' in body"}), 400
    ok, resp = send_text(to, text)
    status = 200 if ok else 500
    return jsonify({"ok":ok,"resp":resp}), status

@app.route("/ext/send-promo", methods=["POST"])
def ext_send_promo():
    """
    Encola campaña promocional. Valida, acepta hasta 100 recipientes por request en práctica, pero worker admite batching.
    """
    job = request.get_json(silent=True)
    if not job:
        return jsonify({"error":"JSON body esperado"}), 400
    recipients = job.get("recipients") or job.get("to") or []
    if not isinstance(recipients, list) or not recipients:
        return jsonify({"error":"recipients debe ser lista de msisdn"}), 400
    if len(recipients) > 1000:
        return jsonify({"error":"Máximo 1000 recipients por job"}), 400
    mode = job.get("mode")
    if mode not in ("text","template","image"):
        return jsonify({"error":"mode inválido. 'text'|'template'|'image'"}), 400
    # validate content per mode
    if mode == "text" and not job.get("text"):
        return jsonify({"error":"text requerido para mode=text"}), 400
    if mode == "template" and not job.get("template"):
        return jsonify({"error":"template requerido para mode=template"}), 400
    if mode == "image" and not job.get("image"):
        return jsonify({"error":"image requerido para mode=image"}), 400
    # normalize recipients
    recipients_norm = [normalize_msisdn(r) for r in recipients][:1000]
    promo_job = {
        "campaign": job.get("campaign"),
        "recipients": recipients_norm,
        "mode": mode,
        "text": job.get("text"),
        "template": job.get("template"),
        "image": job.get("image"),
    }
    promo_queue.put(promo_job)
    logger.info(f"Job promo encolado campaign={job.get('campaign')} size={len(recipients_norm)}")
    return jsonify({"queued": True, "batch_size": len(recipients_norm)}), 202

@app.route("/ext/manuales", methods=["GET"])
def ext_manuales():
    status = {}
    for d in ("auto","imss"):
        cached = rag_cache.get(d)
        status[d] = {
            "loaded": bool(cached),
            "name": cached.get("name") if cached else None,
            "chars": len(cached.get("text")) if cached else 0,
            "loaded_at": datetime.fromtimestamp(cached.get("loaded_at")).isoformat() if cached else None
        }
    return jsonify(status), 200

# -------------------------
# INICIALIZACIÓN ADICIONAL Y NOTAS
# -------------------------
# Carga inicial de manuales en background
def background_initial_load():
    for d in ("auto","imss"):
        try:
            load_manual_to_cache(d)
        except Exception as e:
            logger.warning(f"No pudo cargar manual inicial {d}: {e}")

bg_thread = threading.Thread(target=background_initial_load, daemon=True)
bg_thread.start()

# -------------------------
# PRUEBAS DE HUMO (Comentarios)
# -------------------------
"""
Ejemplos curl para pruebas:

1) Health:
   curl -s https://<host>/ext/health

2) Test send:
   curl -XPOST https://<host>/ext/test-send -H "Content-Type: application/json" -d '{"to":"5216682478005","text":"Prueba OK"}'

3) Encolar promo texto:
   curl -XPOST https://<host>/ext/send-promo -H "Content-Type: application/json" -d '{"recipients":["5216682478005"],"mode":"text","text":"Promo SECOM ✔️"}'

4) Manuales:
   curl -s https://<host>/ext/manuales

Notas de implementación:
- El worker de promos respeta QPS y reintentos básicos.
- El recordatorio 30d y reintento_7d se marcan en Sheets, pero no hay worker persistente para enviar recordatorios en este repo (si el dyno se reinicia se pierde). Se recomienda programar un cron externo o cron job en la nube para evaluar campos "renovacion_vencimiento" y "recordar_30d".
- Endpoints /ext/* no requieren auth por diseño (entorno controlado). Para producción agregar validación por token en header "X-Api-Key".
- Para proteger secretos se enmascaran en los logs.
- Asegúrese de configurar correctamente GOOGLE_CREDENTIALS_JSON (service account JSON en una sola línea) y permisos Drive/Sheets.
"""

# -------------------------
# Lanzador (solo para debug, Gunicorn usará app)
# -------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"Iniciando Flask en puerto {port}")
    app.run(host="0.0.0.0", port=port, debug=False)

