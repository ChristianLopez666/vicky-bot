#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Vicky SECOM – modo Wapi
Archivo listo para ejecutar en Render con:
  gunicorn app:app --bind 0.0.0.0:$PORT

Dependencias (referencia para requirements.txt, ajustar según tu entorno):
  Flask==2.3.3
  gunicorn==21.2.0
  requests==2.31.0
  python-dotenv==1.0.0
  openai==1.3.0
  numpy==1.26.4
  rank-bm25==0.2.2
  pdfminer.six==20231228
  pypdf==4.3.1
  google-api-python-client==2.149.0
  google-auth==2.35.0
  google-auth-httplib2==0.2.0
  google-auth-oauthlib==1.1.0
  gspread==5.11.0
  httpx==0.27.2
  PyPDF2==3.0.1

Descripción:
- Implementación robusta y tolerante a entornos incompletos: si faltan variables de entorno
  o servicios (Google, WhatsApp, OpenAI) la app sigue levantando y ofrece respuestas básicas.
- Logs en español; tokens y teléfonos enmascarados.
- Endpoints exactos requeridos: GET/POST /webhook, GET /ext/health, POST /ext/test-send,
  POST /ext/send-promo, GET /ext/manuales
- Cola in-memory para promociones con worker daemon.
- RAG (lectura PDF desde Drive) con cache TTL 6h (simple dict).
- Nota: para funcionalidad completa configure todas las env vars obligatorias en Render.
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
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple
from functools import wraps
import mimetypes

from flask import Flask, request, jsonify, g

import requests
import openai

# PDF extraction (PyPDF2 is in requirements)
from PyPDF2 import PdfReader

# Google APIs (may raise if not installed in environment)
try:
    import gspread
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseUpload
    GOOGLE_LIBS_AVAILABLE = True
except Exception:
    GOOGLE_LIBS_AVAILABLE = False

# -------------------------
# LOGGING - safe request_id injection
# -------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

class RequestIdFilter(logging.Filter):
    def filter(self, record):
        if not hasattr(record, "request_id"):
            record.request_id = getattr(g, "request_id", "no-rid")
        return True

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [%(request_id)s] %(message)s"
)
logger = logging.getLogger("vicky-secom-wapi")
logger.addFilter(RequestIdFilter())

# -------------------------
# TIMEZONE (zoneinfo fallback)
# -------------------------
try:
    from zoneinfo import ZoneInfo
    FLASK_TZ = ZoneInfo("America/Mazatlan")
except Exception:
    logger.warning("ZoneInfo no disponible o zona no encontrada; usando UTC")
    from datetime import timezone as _tz
    FLASK_TZ = _tz.utc

# -------------------------
# SIMPLE RETRY DECORATOR (fallback en lugar de tenacity)
# -------------------------
def retry_decorator(attempts: int = 3, initial_wait: float = 1.0):
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            last_exc = None
            wait = initial_wait
            for i in range(attempts):
                try:
                    return fn(*args, **kwargs)
                except Exception as e:
                    last_exc = e
                    if i < attempts - 1:
                        time.sleep(wait)
                        wait = min(wait * 2, 8)
            raise last_exc
        return wrapper
    return deco

# -------------------------
# ENVIRONMENT CONFIG (non-fatal validation)
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
missing_envs = [k for k in REQUIRED_ENVS if not env.get(k)]
if missing_envs:
    # Log clearly but DO NOT abort; run in degraded mode so server starts.
    for k in missing_envs:
        logger.warning("Variable de entorno obligatoria NO configurada: %s", k)
else:
    logger.info("Todas las env vars obligatorias parecen presentes (verifica en Render).")

# Load envs (may be None)
META_TOKEN = env.get("META_TOKEN")
WABA_PHONE_ID = env.get("WABA_PHONE_ID")
VERIFY_TOKEN = env.get("VERIFY_TOKEN")
ADVISOR_NUMBER = env.get("ADVISOR_NUMBER")
OPENAI_API_KEY = env.get("OPENAI_API_KEY")
GOOGLE_CREDENTIALS_JSON = env.get("GOOGLE_CREDENTIALS_JSON")
SHEETS_ID_LEADS = env.get("SHEETS_ID_LEADS")
SHEETS_TITLE_LEADS = env.get("SHEETS_TITLE_LEADS")

LEADS_VICKY_SHEET_ID = env.get("LEADS_VICKY_SHEET_ID")
LEADS_VICKY_SHEET_TITLE = env.get("LEADS_VICKY_SHEET_TITLE")
RAG_AUTO_FILE_ID = env.get("RAG_AUTO_FILE_ID")
RAG_IMSS_FILE_ID = env.get("RAG_IMSS_FILE_ID")
RAG_AUTO_FILE_NAME = env.get("RAG_AUTO_FILE_NAME")
RAG_IMSS_FILE_NAME = env.get("RAG_IMSS_FILE_NAME")
DRIVE_UPLOAD_ROOT_FOLDER_ID = env.get("DRIVE_UPLOAD_ROOT_FOLDER_ID")

# Flags for enabled services
WHATSAPP_ENABLED = bool(META_TOKEN and WABA_PHONE_ID)
GOOGLE_ENABLED = bool(GOOGLE_LIBS_AVAILABLE and GOOGLE_CREDENTIALS_JSON and SHEETS_ID_LEADS and SHEETS_TITLE_LEADS)
OPENAI_ENABLED = bool(OPENAI_API_KEY)

# Masking utils for logs
def mask_token(t: Optional[str]) -> str:
    if not t: return "MISSING"
    s = str(t)
    return "****" + s[-4:] if len(s) > 4 else "*" * len(s)

def mask_phone(p: Optional[str]) -> str:
    if not p: return "UNKNOWN"
    d = re.sub(r"\D", "", p)
    if len(d) <= 4:
        return "*" * max(0, len(d)-1) + d[-1:]
    return "*" * (len(d)-4) + d[-4:]

logger.info("Arrancando Vicky SECOM WAPI (modo degradado si falta configuración)")
logger.info("WHATSAPP_ENABLED=%s GOOGLE_ENABLED=%s OPENAI_ENABLED=%s", WHATSAPP_ENABLED, GOOGLE_ENABLED, OPENAI_ENABLED)
logger.debug("WABA_PHONE_ID=%s META_TOKEN=%s", mask_phone(WABA_PHONE_ID), mask_token(META_TOKEN))

# -------------------------
# APP, STATE, CACHE, QUEUE
# -------------------------
app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False

QPS_LIMIT = 5
PROMO_BATCH_LIMIT = 100

session = requests.Session()
if META_TOKEN:
    session.headers.update({"Authorization": f"Bearer {META_TOKEN}"})

state: Dict[str, Dict[str, Any]] = {}  # ephemeral in-memory state per wa_id
state_ttl_seconds = 3600

# Simple rag cache with TTL
rag_cache: Dict[str, Dict[str, Any]] = {}  # domain -> {"text":..., "loaded_at":..., "name":...}
RAG_TTL_SECONDS = 6 * 3600

promo_queue = queue.Queue()

# Google clients placeholders
sheets_client = None
drive_service = None

# OpenAI client init
if OPENAI_ENABLED:
    openai.api_key = OPENAI_API_KEY

OPENAI_MODEL_RAG = "gpt-4o-mini"

# -------------------------
# PHONE / DATE HELPERS
# -------------------------
def normalize_msisdn(s: str) -> str:
    if not s: return ""
    digits = re.sub(r"\D", "", s)
    if digits.startswith("521") and len(digits) == 13:
        return digits
    if len(digits) == 10:
        return "521" + digits
    if len(digits) > 10:
        return "521" + digits[-10:]
    return digits

def last10(msisdn: str) -> str:
    d = re.sub(r"\D", "", msisdn or "")
    return d[-10:] if len(d) >= 10 else d

def now_iso() -> str:
    try:
        return datetime.now(FLASK_TZ).isoformat()
    except Exception:
        return datetime.utcnow().isoformat()

def parse_date_from_text(s: str) -> Optional[datetime]:
    if not s: return None
    s = s.strip()
    for fmt in ("%d-%m-%Y","%Y-%m-%d","%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=FLASK_TZ)
        except Exception:
            continue
    m = re.search(r"(\d{2})[-/](\d{2})[-/](\d{4})", s)
    if m:
        try:
            return datetime.strptime(m.group(0), "%d-%m-%Y").replace(tzinfo=FLASK_TZ)
        except Exception:
            pass
    return None

# -------------------------
# WHATSAPP SENDER (tolerant if not configured)
# -------------------------
WHATSAPP_API_BASE = f"https://graph.facebook.com/v17.0/{WABA_PHONE_ID}/messages" if WABA_PHONE_ID else None
_send_lock = threading.Lock()
_last_send_ts = 0.0

def _rate_limit_sleep():
    global _last_send_ts
    with _send_lock:
        now = time.time()
        min_interval = 1.0 / QPS_LIMIT
        delta = now - _last_send_ts
        if delta < min_interval:
            time.sleep(min_interval - delta)
        _last_send_ts = time.time()

@retry_decorator(attempts=3, initial_wait=1.0)
def _post_whatsapp(payload: dict) -> dict:
    if not WHATSAPP_ENABLED or not WHATSAPP_API_BASE:
        raise RuntimeError("WhatsApp no configurado")
    resp = session.post(WHATSAPP_API_BASE, json=payload, timeout=30)
    if resp.status_code not in (200, 201):
        logger.warning("WhatsApp API returned %s: %s", resp.status_code, resp.text)
        resp.raise_for_status()
    return resp.json()

def send_text(to: str, text: str) -> Tuple[bool, dict]:
    to_norm = normalize_msisdn(to)
    payload = {"messaging_product":"whatsapp","to":to_norm,"type":"text","text":{"body":text}}
    if not WHATSAPP_ENABLED:
        logger.info("[SIMULADO] send_text a %s: %s", mask_phone(to_norm), text[:120])
        return True, {"simulated": True}
    try:
        _rate_limit_sleep()
        resp = _post_whatsapp(payload)
        mid = resp.get("messages",[{}])[0].get("id")
        logger.info("Texto enviado a %s mid=%s", mask_phone(to_norm), mid)
        return True, resp
    except Exception as e:
        logger.error("Error enviando texto a %s: %s", mask_phone(to_norm), str(e))
        return False, {"error": str(e)}

def send_template(to: str, template_name: str, language_code: str = "es_MX", components: Optional[list] = None) -> Tuple[bool, dict]:
    to_norm = normalize_msisdn(to)
    payload = {"messaging_product":"whatsapp","to":to_norm,"type":"template","template":{"name":template_name,"language":{"code":language_code}}}
    if components:
        payload["template"]["components"] = components
    if not WHATSAPP_ENABLED:
        logger.info("[SIMULADO] send_template a %s: %s", mask_phone(to_norm), template_name)
        return True, {"simulated": True}
    try:
        _rate_limit_sleep()
        resp = _post_whatsapp(payload)
        mid = resp.get("messages",[{}])[0].get("id")
        logger.info("Template enviado a %s mid=%s", mask_phone(to_norm), mid)
        return True, resp
    except Exception as e:
        logger.error("Error enviando template a %s: %s", mask_phone(to_norm), str(e))
        return False, {"error": str(e)}

def send_image_url(to: str, image_url: str, caption: Optional[str] = None) -> Tuple[bool, dict]:
    to_norm = normalize_msisdn(to)
    payload = {"messaging_product":"whatsapp","to":to_norm,"type":"image","image":{"link":image_url}}
    if caption:
        payload["image"]["caption"] = caption
    if not WHATSAPP_ENABLED:
        logger.info("[SIMULADO] send_image_url a %s: %s", mask_phone(to_norm), image_url)
        return True, {"simulated": True}
    try:
        _rate_limit_sleep()
        resp = _post_whatsapp(payload)
        mid = resp.get("messages",[{}])[0].get("id")
        logger.info("Imagen enviada a %s mid=%s url=%s", mask_phone(to_norm), mid, image_url)
        return True, resp
    except Exception as e:
        logger.error("Error enviando imagen a %s: %s", mask_phone(to_norm), str(e))
        return False, {"error": str(e)}

# -------------------------
# GOOGLE SHEETS & DRIVE helpers (graceful when disabled)
# -------------------------
def initialize_google_clients():
    global sheets_client, drive_service, GOOGLE_ENABLED
    if not GOOGLE_LIBS_AVAILABLE:
        logger.warning("Librerías Google no instaladas; funcionalidad Drive/Sheets deshabilitada")
        GOOGLE_ENABLED = False
        return
    if not GOOGLE_CREDENTIALS_JSON:
        logger.warning("GOOGLE_CREDENTIALS_JSON no configurado; Drive/Sheets deshabilitado")
        GOOGLE_ENABLED = False
        return
    try:
        creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
        scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        sheets_client = gspread.authorize(creds)
        drive_service = build("drive", "v3", credentials=creds, cache_discovery=False)
        GOOGLE_ENABLED = True
        logger.info("Google Sheets & Drive inicializados correctamente")
    except Exception as e:
        logger.error("Error inicializando Google clients: %s", str(e))
        sheets_client = None
        drive_service = None
        GOOGLE_ENABLED = False

# Initialize once at startup (non-fatal)
initialize_google_clients()

SHEET_MIN_FIELDS = ["status","greeted_at","renovacion_vencimiento","recordar_30d","reintento_7d","campaña_origen","notas","wa_last10","nombre"]

def get_leads_worksheet():
    if not GOOGLE_ENABLED or not sheets_client:
        raise RuntimeError("Google Sheets no disponible")
    sh = sheets_client.open_by_key(SHEETS_ID_LEADS)
    try:
        ws = sh.worksheet(SHEETS_TITLE_LEADS)
    except Exception:
        ws = sh.add_worksheet(title=SHEETS_TITLE_LEADS, rows="2000", cols="20")
    header = ws.row_values(1)
    if not header:
        ws.insert_row(SHEET_MIN_FIELDS, index=1)
        header = SHEET_MIN_FIELDS
    missing = [c for c in SHEET_MIN_FIELDS if c not in header]
    if missing:
        header = header + missing
        ws.update('1:1', [header])
    return ws

def find_or_create_contact_row(wa_last10: str) -> int:
    ws = get_leads_worksheet()
    records = ws.get_all_records()
    for i, r in enumerate(records, start=2):
        if str(r.get("wa_last10","")).endswith(wa_last10):
            return i
    header = ws.row_values(1)
    row = {k:"" for k in header}
    row["status"] = "nuevo"
    row["wa_last10"] = wa_last10
    values = [row.get(col,"") for col in header]
    ws.append_row(values)
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
    existing = ws.row_values(row_index)
    new_row = existing + [""] * max(0, len(header) - len(existing))
    for k,v in fields.items():
        if k in header:
            new_row[header.index(k)] = str(v)
    ws.update(f"{row_index}:{row_index}", [new_row])

def append_note_to_contact(row_index: int, note: str):
    try:
        data = get_contact_data(row_index)
        prev = data.get("notas","")
        ts = now_iso()
        new_notes = (prev + "\n" + f"[{ts}] {note}").strip()
        set_contact_data(row_index, {"notas": new_notes})
    except Exception as e:
        logger.warning("No fue posible agregar nota en Sheets: %s", str(e))

# -------------------------
# DRIVE helpers for RAG and media backup
# -------------------------
def _drive_find_pdf_by_id(file_id: str) -> Optional[Dict[str,Any]]:
    if not GOOGLE_ENABLED or not drive_service:
        return None
    try:
        f = drive_service.files().get(fileId=file_id, fields="id,name,mimeType").execute()
        if f and f.get("mimeType") == "application/pdf":
            return f
    except Exception as e:
        logger.warning("No se pudo obtener archivo Drive por ID %s: %s", file_id, e)
    return None

def _drive_search_pdf_by_name(name: str) -> Optional[Dict[str,Any]]:
    if not GOOGLE_ENABLED or not drive_service:
        return None
    try:
        q = f"name = '{name}' and mimeType='application/pdf' and trashed=false"
        res = drive_service.files().list(q=q, pageSize=10, fields="files(id,name,mimeType)").execute()
        files = res.get("files", [])
        if files:
            return files[0]
    except Exception as e:
        logger.warning("Error buscando PDF por nombre %s: %s", name, e)
    return None

def _drive_download_file_bytes(file_id: str) -> Optional[bytes]:
    if not GOOGLE_ENABLED or not drive_service:
        return None
    try:
        request = drive_service.files().get_media(fileId=file_id)
        fh = io.BytesIO()
        from googleapiclient.http import MediaIoBaseDownload
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            status, done = downloader.next_chunk()
        fh.seek(0)
        return fh.read()
    except Exception as e:
        logger.error("Error descargando archivo Drive %s: %s", file_id, e)
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
        try:
            return b.decode('utf-8', errors='ignore')
        except Exception:
            return ""

def load_manual_to_cache(domain: str) -> bool:
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
            logger.info("No se encontró manual Drive para domain=%s", domain)
            return False
        b = _drive_download_file_bytes(found["id"])
        if not b:
            logger.warning("No se pudo descargar manual %s", found.get("name"))
            return False
        text = _extract_text_from_pdf_bytes(b)
        if not text:
            logger.warning("Manual %s sin texto extraído", found.get("name"))
            return False
        rag_cache[domain] = {"text": text, "loaded_at": time.time(), "name": found.get("name")}
        logger.info("Manual cargado en cache domain=%s name=%s chars=%d", domain, found.get("name"), len(text))
        return True
    except Exception as e:
        logger.error("Error cargando manual %s: %s", domain, e)
        return False

def rag_answer(query: str, domain: str = "auto") -> str:
    domain = domain if domain in ("auto","imss") else "auto"
    # TTL enforcement
    cached = rag_cache.get(domain)
    if cached and time.time() - cached.get("loaded_at", 0) > RAG_TTL_SECONDS:
        rag_cache.pop(domain, None)
        cached = None
    if not cached:
        ok = load_manual_to_cache(domain)
        if not ok:
            return "No encuentro esta información en el manual correspondiente."
        cached = rag_cache.get(domain)
    text = cached.get("text","")
    name = cached.get("name","manual")
    if not text:
        return f"No encuentro esta información en el manual {name}."
    query_words = set([w.lower() for w in re.findall(r"\w{3,}", query)])
    paragraphs = [p.strip() for p in re.split(r"\n{1,}", text) if p.strip()]
    scored = []
    for p in paragraphs:
        words = set([w.lower() for w in re.findall(r"\w{3,}", p)])
        score = len(query_words.intersection(words))
        if score > 0:
            scored.append((score,p))
    scored.sort(reverse=True, key=lambda x: x[0])
    context = "\n\n".join([p for _,p in scored[:3]]) if scored else "\n\n".join(paragraphs[:3])
    prompt = (
        f"Eres Vicky, asistente basada en el manual '{name}'. Usa SOLO la información del contexto provisto.\n\n"
        f"Contexto relevante:\n{context}\n\n"
        f"Pregunta: {query}\n\n"
        "Si la respuesta no está en el contexto, responde exactamente: 'No encuentro esta información en el manual correspondiente.'\n"
        f"Al final agrega: 'Esta info proviene del manual {name}'. Responde en español, breve y clara."
    )
    if not OPENAI_ENABLED:
        logger.info("OpenAI no configurado; RAG devolviendo fallback")
        return "No puedo consultar el manual en este momento (servicio de IA no configurado)."
    try:
        resp = openai.ChatCompletion.create(
            model=OPENAI_MODEL_RAG,
            messages=[{"role":"user","content":prompt}],
            temperature=0.2,
            max_tokens=300
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        logger.error("Error llamando OpenAI para RAG: %s", str(e))
        return "No puedo procesar la consulta ahora. Intenta más tarde."

def backup_media_to_drive(file_bytes: bytes, filename: str, mime_type: str, contact_name: Optional[str], wa_last4: str) -> Optional[str]:
    if not (GOOGLE_ENABLED and drive_service and DRIVE_UPLOAD_ROOT_FOLDER_ID):
        logger.info("Drive no configurado; omitiendo respaldo multimedia")
        return None
    try:
        folder_name = f"{(contact_name or 'Cliente')}_{wa_last4}"
        q = f"name = '{folder_name}' and mimeType = 'application/vnd.google-apps.folder' and '{DRIVE_UPLOAD_ROOT_FOLDER_ID}' in parents and trashed=false"
        res = drive_service.files().list(q=q, fields="files(id,name)").execute()
        files = res.get("files", [])
        if files:
            folder_id = files[0]["id"]
        else:
            file_metadata = {"name": folder_name, "mimeType": "application/vnd.google-apps.folder", "parents": [DRIVE_UPLOAD_ROOT_FOLDER_ID]}
            created = drive_service.files().create(body=file_metadata, fields="id").execute()
            folder_id = created["id"]
        fh = io.BytesIO(file_bytes)
        media_body = MediaIoBaseUpload(fh, mimetype=mime_type, resumable=True)
        file_metadata = {"name": filename, "parents": [folder_id]}
        uploaded = drive_service.files().create(body=file_metadata, media_body=media_body, fields="id,webViewLink").execute()
        link = uploaded.get("webViewLink") or f"https://drive.google.com/file/d/{uploaded.get('id')}/view"
        logger.info("Media respaldada en Drive: %s link=%s", filename, link)
        return link
    except Exception as e:
        logger.error("Error subiendo media a Drive: %s", str(e))
        return None

# -------------------------
# GREET LOGIC
# -------------------------
def should_greet(wa_last10: str) -> bool:
    try:
        row = find_or_create_contact_row(wa_last10)
        data = get_contact_data(row)
        greeted_at = data.get("greeted_at")
        if not greeted_at:
            return True
        try:
            dt = datetime.fromisoformat(greeted_at)
            dt = dt.astimezone(FLASK_TZ)
            return (datetime.now(FLASK_TZ) - dt) > timedelta(hours=24)
        except Exception:
            return True
    except Exception:
        # If Sheets not available, fallback to greeting once per process per wa_last10
        in_state = state.get(wa_last10, {})
        last = in_state.get("greeted_at")
        if not last:
            return True
        try:
            dt = datetime.fromisoformat(last)
            return (datetime.now(FLASK_TZ) - dt) > timedelta(hours=24)
        except Exception:
            return True

def mark_greeted(wa_last10: str):
    try:
        row = find_or_create_contact_row(wa_last10)
        set_contact_data(row, {"greeted_at": now_iso()})
    except Exception:
        # store locally
        st = state.setdefault(wa_last10, {})
        st["greeted_at"] = now_iso()

# -------------------------
# SHEETS helpers (ensure fail-safe)
# -------------------------
def find_or_create_contact_row_safe(wa_last10: str) -> Optional[int]:
    try:
        return find_or_create_contact_row(wa_last10)
    except Exception as e:
        logger.debug("find_or_create_contact_row_safe: Sheets no disponible: %s", str(e))
        return None

# -------------------------
# FLOWS (AUTO / IMSS) simplified but robust
# -------------------------
def notify_advisor(wa_id: str, row_index: Optional[int], reason: str):
    nombre = "Desconocido"
    campaña = ""
    notas = ""
    if row_index:
        try:
            contact = get_contact_data(row_index)
            nombre = contact.get("nombre") or nombre
            campaña = contact.get("campaña_origen","")
            notas = contact.get("notas","")
        except Exception:
            pass
    body = (
        f"Asesor: nuevo contacto\n"
        f"Nombre: {nombre}\n"
        f"wa_id: {wa_id}\n"
        f"Motivo: {reason}\n"
        f"Campaña: {campaña}\n"
        f"Notas: {notas[:400]}"
    )
    send_text(ADVISOR_NUMBER, body)

def handle_auto_flow(wa_id: str, text: str, row_index: Optional[int]):
    st = state.setdefault(wa_id, {"stage":"AUTO_START","updated":time.time()})
    stage = st.get("stage","AUTO_START")
    t = (text or "").strip().lower()
    if stage == "AUTO_START":
        msg = ("Planes Auto SECOM:\n\n1) Amplia Plus\n2) Amplia\n3) Limitada\n\n"
               "Responde con 'INE: <datos>' o 'PLACA: <datos>' para continuar.")
        send_text(wa_id, msg)
        st["stage"] = "AUTO_DOCS"
        st["updated"] = time.time()
        return
    if stage == "AUTO_DOCS":
        if "ine" in t or t.startswith("ine:"):
            if row_index:
                append_note_to_contact(row_index, "INE provisto: " + text)
            send_text(wa_id, "Gracias. Por favor proporciona placa o tarjeta de circulación si la tienes.")
            st["stage"] = "AUTO_PLAN"
            st["updated"] = time.time()
            return
        if "placa" in t or "tarjeta" in t:
            if row_index:
                append_note_to_contact(row_index, "Placa/tarjeta: " + text)
            send_text(wa_id, "Perfecto. ¿Qué plan te interesa? Responde 1, 2 o 3.")
            st["stage"] = "AUTO_PLAN"
            st["updated"] = time.time()
            return
        send_text(wa_id, "No entendí. Responde 'INE: <datos>' o 'PLACA: <datos>'.")
        return
    if stage == "AUTO_PLAN":
        if "1" in t or "amplia plus" in t:
            plan = "Amplia Plus"
        elif "2" in t or "amplia" in t:
            plan = "Amplia"
        elif "3" in t or "limitada" in t:
            plan = "Limitada"
        else:
            if "renov" in t or "vencim" in t or "póliza" in t:
                send_text(wa_id, "¿Fecha de vencimiento? Formato dd-mm-aaaa o aaaa-mm-dd")
                st["stage"] = "AUTO_RENOV"
                st["updated"] = time.time()
                return
            send_text(wa_id, "Selecciona 1,2 o 3. O escribe 'asesor' para contacto humano.")
            return
        if row_index:
            append_note_to_contact(row_index, f"Plan elegido: {plan}")
            set_contact_data(row_index, {"status":"en_seguimiento"})
        send_text(wa_id, f"Has seleccionado {plan}. ¿Deseas que un asesor te contacte? Escribe 'asesor'.")
        st["stage"] = "AUTO_RESUMEN"
        st["updated"] = time.time()
        return
    if stage == "AUTO_RENOV":
        d = parse_date_from_text(text)
        if d:
            if row_index:
                set_contact_data(row_index, {"renovacion_vencimiento": d.isoformat(), "recordar_30d":"TRUE", "reintento_7d":"TRUE"})
                append_note_to_contact(row_index, f"Fecha de renovación: {d.isoformat()}")
            send_text(wa_id, "Fecha guardada. ¿Deseas que un asesor te contacte? Escribe 'asesor'.")
            st["stage"] = "AUTO_RESUMEN"
            st["updated"] = time.time()
            return
        send_text(wa_id, "No pude reconocer la fecha. Usa dd-mm-aaaa o aaaa-mm-dd.")
        return
    if stage == "AUTO_RESUMEN":
        if "asesor" in t:
            notify_advisor(wa_id, row_index, "Solicitud contacto desde AUTO")
            send_text(wa_id, "He notificado a un asesor. Te contactarán pronto.")
            st["stage"] = "DONE"
            st["updated"] = time.time()
            return
        send_text(wa_id, "Gracias. Si necesitas más, escribe 'menu'.")
        st["stage"] = "DONE"
        st["updated"] = time.time()
        return

def handle_imss_flow(wa_id: str, text: str, row_index: Optional[int]):
    st = state.setdefault(wa_id, {"stage":"IMSS_START","updated":time.time()})
    stage = st.get("stage","IMSS_START")
    t = (text or "").strip().lower()
    if stage == "IMSS_START":
        send_text(wa_id, "IMSS Ley 73: responde 'requisitos', 'cálculo' o 'prestamo'.")
        st["stage"] = "IMSS_QUALIFY"
        st["updated"] = time.time()
        return
    if stage == "IMSS_QUALIFY":
        if "requisitos" in t:
            resp = rag_answer("requisitos para pensión IMSS", domain="imss")
            send_text(wa_id, resp)
            st["stage"] = "IMSS_FOLLOW"
            st["updated"] = time.time()
            return
        if "cálculo" in t or "calculo" in t:
            send_text(wa_id, "Para calcular necesitamos salario promedio, semanas cotizadas y edad. ¿Quieres asesoría? Responde 'sí' o 'no'.")
            st["stage"] = "IMSS_CALC"
            st["updated"] = time.time()
            return
        if "prestamo" in t or "ley 73" in t:
            send_text(wa_id, "Préstamos Ley 73: hasta 12 meses de pensión. ¿Quieres contacto con asesor?")
            st["stage"] = "IMSS_FOLLOW"
            st["updated"] = time.time()
            return
        send_text(wa_id, "No entendí. Responde 'requisitos', 'cálculo' o 'prestamo'.")
        return
    if stage == "IMSS_CALC":
        if "sí" in t or "si" in t:
            notify_advisor(wa_id, row_index, "Cliente solicita cálculo IMSS")
            if row_index:
                append_note_to_contact(row_index, "Solicitó cálculo IMSS")
                set_contact_data(row_index, {"status":"en_seguimiento"})
            send_text(wa_id, "He notificado a un asesor.")
            st["stage"] = "DONE"
            st["updated"] = time.time()
            return
        send_text(wa_id, "Entendido. Si deseas asesor escribe 'asesor'.")
        st["stage"] = "DONE"
        st["updated"] = time.time()
        return
    if stage == "IMSS_FOLLOW":
        if "asesor" in t:
            notify_advisor(wa_id, row_index, "Solicitud contacto IMSS")
            send_text(wa_id, "Asesor notificado. Te contactarán.")
            st["stage"] = "DONE"
            st["updated"] = time.time()
            return
        send_text(wa_id, "Si necesitas algo más escribe 'menu'.")
        st["stage"] = "DONE"
        st["updated"] = time.time()
        return

# -------------------------
# PROMO WORKER (daemon)
# -------------------------
def promo_worker():
    logger.info("Promo worker iniciado (daemon)")
    while True:
        try:
            job = promo_queue.get()
            if not job:
                time.sleep(1)
                continue
            recipients = job.get("recipients", [])
            mode = job.get("mode")
            text = job.get("text")
            template = job.get("template")
            image = job.get("image")
            logger.info("Procesando promo recipients=%d mode=%s", len(recipients), mode)
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
                            ok = False
                            resp = {"error":"modo desconocido"}
                        if ok:
                            mid = resp.get("messages",[{}])[0].get("id") if isinstance(resp, dict) else None
                            logger.info("Promo enviado a %s mid=%s", mask_phone(r), mid)
                        else:
                            logger.warning("Promo falló a %s resp=%s", mask_phone(r), resp)
                    except Exception:
                        logger.error("Excepción enviando promo a %s: %s", mask_phone(r), traceback.format_exc())
                    time.sleep(max(0.02, 1.0 / QPS_LIMIT))
                idx += PROMO_BATCH_LIMIT
            promo_queue.task_done()
        except Exception:
            logger.error("Error en promo_worker: %s", traceback.format_exc())
            time.sleep(2)

promo_thread = threading.Thread(target=promo_worker, daemon=True)
promo_thread.start()

# -------------------------
# ENDPOINTS EXACTOS
# -------------------------
@app.before_request
def attach_request_id():
    request_id = request.headers.get("X-Request-Id") or f"{int(time.time()*1000)}"
    g.request_id = request_id

@app.route("/webhook", methods=["GET"])
def webhook_verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    logger.info("Verificación webhook solicitada")
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
    logger.info("📥 Payload recibido")
    try:
        entries = payload.get("entry", []) or []
        for entry in entries:
            changes = entry.get("changes", []) or []
            for change in changes:
                value = change.get("value", {}) or {}
                messages = value.get("messages", []) or []
                for msg in messages:
                    wa_from = msg.get("from")
                    wa_id = normalize_msisdn(wa_from)
                    wa_last10 = last10(wa_id)
                    wa_last4 = re.sub(r"\D","",wa_id)[-4:] if wa_id else "0000"
                    # find or create contact row (graceful)
                    row = None
                    try:
                        row = find_or_create_contact_row_safe(wa_last10)
                    except Exception:
                        row = None
                    # saludo protegido
                    try:
                        if should_greet(wa_last10):
                            send_text(wa_id, "Hola 👋 Soy Vicky de SECOM. Escribe *AUTO*, *IMSS* o *CONTACTO* para iniciar.")
                            mark_greeted(wa_last10)
                    except Exception as e:
                        logger.warning("No fue posible evaluar saludo protegido: %s", str(e))
                    mtype = msg.get("type")
                    if mtype == "text":
                        text = msg.get("text",{}).get("body","")
                        logger.info("💬 Mensaje de %s: %s", mask_phone(wa_id), (text or "")[:200])
                        t_low = (text or "").lower()
                        if "auto" in t_low:
                            handle_auto_flow(wa_id, text, row)
                        elif "imss" in t_low:
                            handle_imss_flow(wa_id, text, row)
                        elif any(k in t_low for k in ["asesor","contacto","llámame","llamame","hablar con christian","christian"]):
                            notify_advisor(wa_id, row, "Solicitud de contacto")
                            send_text(wa_id, "He notificado al asesor. Te contactarán pronto.")
                        elif any(q in t_low for q in ["cómo","como","requisitos","cobertura","qué","que","cuando","cuándo","dónde","donde","pensión","pension"]):
                            domain = "auto" if "auto" in t_low or "cobertura" in t_low else ("imss" if "imss" in t_low or "pensión" in t_low or "pension" in t_low else None)
                            if not domain:
                                send_text(wa_id, "¿Tu duda es sobre *Auto* o *IMSS*? Responde con la palabra correspondiente.")
                            else:
                                resp = rag_answer(text, domain)
                                send_text(wa_id, resp)
                        else:
                            send_text(wa_id, "No entendí. Escribe *AUTO*, *IMSS* o *CONTACTO* para iniciar.")
                    elif mtype in ("image","document","video","audio","sticker"):
                        media_info = msg.get(mtype, {}) or {}
                        media_id = media_info.get("id")
                        if media_id:
                            try:
                                media_resp = session.get(f"https://graph.facebook.com/v17.0/{media_id}", params={"fields":"url"}, timeout=20)
                                media_json = media_resp.json()
                                media_url = media_json.get("url")
                                if media_url:
                                    mdata = requests.get(media_url, headers={"Authorization": f"Bearer {META_TOKEN}"} if META_TOKEN else None, timeout=30)
                                    b = mdata.content
                                    mime_type = mdata.headers.get("Content-Type","application/octet-stream")
                                    ext = mimetypes.guess_extension(mime_type) or ""
                                    filename = f"{mtype}_{wa_last4}{ext}"
                                    link = backup_media_to_drive(b, filename, mime_type, contact_name=None, wa_last4=wa_last4)
                                    if link:
                                        if row:
                                            append_note_to_contact(row, f"Media respaldada: {link}")
                                        send_text(wa_id, "Archivo recibido y respaldado. Gracias.")
                                    else:
                                        send_text(wa_id, "Archivo recibido. Gracias.")
                                else:
                                    send_text(wa_id, "Archivo recibido. Gracias.")
                            except Exception:
                                logger.error("Error procesando media: %s", traceback.format_exc())
                                send_text(wa_id, "Recibí tu archivo pero no pude procesarlo completamente.")
                        else:
                            send_text(wa_id, "Archivo recibido. Gracias.")
                    else:
                        send_text(wa_id, "Mensaje recibido. ¿En qué puedo ayudarte?")
        return jsonify({"ok": True}), 200
    except Exception:
        logger.error("❌ Error crítico en webhook: %s", traceback.format_exc())
        # Responder 200 para evitar reintentos de Meta en caso de error interno
        return jsonify({"ok": True}), 200

# -------------------------
# Otros endpoints
# -------------------------
@app.route("/ext/health", methods=["GET"])
def ext_health():
    return jsonify({"status":"ok","whatsapp":WHATSAPP_ENABLED,"google":GOOGLE_ENABLED,"openai":OPENAI_ENABLED}), 200

@app.route("/ext/test-send", methods=["POST"])
def ext_test_send():
    body = request.get_json(silent=True) or {}
    to = body.get("to")
    text = body.get("text","Prueba Vicky")
    if not to:
        return jsonify({"error":"missing 'to' in body"}), 400
    ok, resp = send_text(to, text)
    status = 200 if ok else 500
    return jsonify({"ok": ok, "resp": resp}), status

@app.route("/ext/send-promo", methods=["POST"])
def ext_send_promo():
    job = request.get_json(silent=True)
    if not job:
        return jsonify({"error":"JSON body esperado"}), 400
    recipients = job.get("recipients") or []
    if not isinstance(recipients, list) or not recipients:
        return jsonify({"error":"recipients debe ser lista de msisdn"}), 400
    if len(recipients) > 1000:
        return jsonify({"error":"Máximo 1000 recipients por job"}), 400
    mode = job.get("mode")
    if mode not in ("text","template","image"):
        return jsonify({"error":"mode inválido. 'text'|'template'|'image'"}), 400
    if mode == "text" and not job.get("text"):
        return jsonify({"error":"text requerido para mode=text"}), 400
    if mode == "template" and not job.get("template"):
        return jsonify({"error":"template requerido para mode=template"}), 400
    if mode == "image" and not job.get("image"):
        return jsonify({"error":"image requerido para mode=image"}), 400
    recipients_norm = [normalize_msisdn(r) for r in recipients][:1000]
    promo_job = {"campaign": job.get("campaign"), "recipients": recipients_norm, "mode": mode, "text": job.get("text"), "template": job.get("template"), "image": job.get("image")}
    promo_queue.put(promo_job)
    logger.info("Job promo encolado campaign=%s size=%d", job.get("campaign"), len(recipients_norm))
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

# Background initial load of manuals (non-blocking, optional)
def background_initial_load():
    for d in ("auto","imss"):
        try:
            load_manual_to_cache(d)
        except Exception:
            logger.debug("No pudo cargar manual inicial %s (posible falta de Drive o credenciales)", d)

bg_thread = threading.Thread(target=background_initial_load, daemon=True)
bg_thread.start()

# -------------------------
# HUMO / NOTAS
# -------------------------
"""
Pruebas de humo (curl):

1) Health:
   curl -s https://<host>/ext/health

2) Test send:
   curl -XPOST https://<host>/ext/test-send -H "Content-Type: application/json" -d '{"to":"5216682478005","text":"Prueba OK"}'

3) Encolar promo:
   curl -XPOST https://<host>/ext/send-promo -H "Content-Type: application/json" -d '{"recipients":["5216682478005"],"mode":"text","text":"Promo SECOM ✔️"}'

4) Manuales:
   curl -s https://<host>/ext/manuales

Notas operativas:
- En este archivo la app NO aborta si faltan env vars; en lugar de eso opera en modo degradado:
  - Si falta META_TOKEN o WABA_PHONE_ID, los envíos se simulan.
  - Si falta GOOGLE_CREDENTIALS_JSON o libs, Sheets/Drive se deshabilitan.
  - Si falta OPENAI_API_KEY, RAG devolverá fallback informativo.
- Para comportamiento completo configure en Render las env vars obligatorias.
"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info("Iniciando Flask en puerto %d", port)
    app.run(host="0.0.0.0", port=port, debug=False)





