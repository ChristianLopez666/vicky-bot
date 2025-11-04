#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Vicky SECOM – modo Wapi
Listo para desplegar en Render con:
  gunicorn app:app --bind 0.0.0.0:$PORT

Dependencias (referencia requirements.txt):
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

INSTRUCCIONES: configura en Render las env vars obligatorias indicadas en el README del proyecto.
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
from cachetools import TTLCache
import pytz

# PDF handling (pypdf / PyPDF2 compatible API)
from PyPDF2 import PdfReader

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
# TENACITY FALLBACK (USAR SIN DEPENDENCIA)
# -------------------------
# El usuario solicitó fallback interno (no instalar tenacity).
try:
    # si por alguna razón tenacity existe, úsalo
    from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
    TENACITY_AVAILABLE = True
except Exception:
    TENACITY_AVAILABLE = False
    def retry(*_args, **_kwargs):
        """Decorador fallback: 3 intentos con backoff exponencial 1,2,4s"""
        def decorator(fn):
            @wraps(fn)
            def wrapper(*args, **kwargs):
                attempts = 3
                delay = 1.0
                last_exc = None
                for i in range(attempts):
                    try:
                        return fn(*args, **kwargs)
                    except Exception as e:
                        last_exc = e
                        if i < attempts - 1:
                            time.sleep(delay)
                            delay = min(delay * 2, 8)
                raise last_exc
            return wrapper
        return decorator
    def stop_after_attempt(n): return None
    def wait_exponential(*a, **k): return None
    def retry_if_exception_type(exc): return None

# -------------------------
# VALIDACIÓN DE ENVS (OBLIGATORIAS)
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

_env = os.environ.copy()
_missing = [k for k in REQUIRED_ENVS if not _env.get(k)]
if _missing:
    for k in _missing:
        logger.critical("Variable de entorno obligatoria faltante: %s", k)
    raise RuntimeError(f"Variables de entorno obligatorias faltantes: {_missing}")

# Cargar variables
META_TOKEN = _env["META_TOKEN"]
WABA_PHONE_ID = _env["WABA_PHONE_ID"]
VERIFY_TOKEN = _env["VERIFY_TOKEN"]
ADVISOR_NUMBER = _env["ADVISOR_NUMBER"]
OPENAI_API_KEY = _env["OPENAI_API_KEY"]
GOOGLE_CREDENTIALS_JSON = _env["GOOGLE_CREDENTIALS_JSON"]
SHEETS_ID_LEADS = _env["SHEETS_ID_LEADS"]
SHEETS_TITLE_LEADS = _env["SHEETS_TITLE_LEADS"]

# Opcionales
LEADS_VICKY_SHEET_ID = _env.get("LEADS_VICKY_SHEET_ID")
LEADS_VICKY_SHEET_TITLE = _env.get("LEADS_VICKY_SHEET_TITLE")
RAG_AUTO_FILE_ID = _env.get("RAG_AUTO_FILE_ID")
RAG_IMSS_FILE_ID = _env.get("RAG_IMSS_FILE_ID")
RAG_AUTO_FILE_NAME = _env.get("RAG_AUTO_FILE_NAME")
RAG_IMSS_FILE_NAME = _env.get("RAG_IMSS_FILE_NAME")
DRIVE_UPLOAD_ROOT_FOLDER_ID = _env.get("DRIVE_UPLOAD_ROOT_FOLDER_ID")

# Mostrar valores en logs enmascarados
def mask_token(t: Optional[str]) -> str:
    if not t: return "MISSING"
    s = str(t)
    return "****" + s[-4:] if len(s) > 4 else "*" * len(s)

def mask_phone(p: Optional[str]) -> str:
    if not p: return "UNKNOWN"
    d = re.sub(r"\D", "", p)
    return ("*" * (len(d)-4) + d[-4:]) if len(d) > 4 else ("*" * max(0, len(d)-1) + d[-1:])

logger.info("Inicializando Vicky SECOM WAPI")
logger.info("META_TOKEN=%s WABA_PHONE_ID=%s ADVISOR_NUMBER=%s", mask_token(META_TOKEN), mask_phone(WABA_PHONE_ID), mask_phone(ADVISOR_NUMBER))

# -------------------------
# CONSTANTES Y CLIENTES
# -------------------------
FLASK_TZ = pytz.timezone("America/Mazatlan")
QPS_LIMIT = 5
PROMO_BATCH_LIMIT = 100

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False

# in-memory
state: Dict[str, Dict[str, Any]] = {}
rag_cache = TTLCache(maxsize=4, ttl=6 * 3600)  # 6h TTL

promo_queue = queue.Queue()
session = requests.Session()
session.headers.update({"Authorization": f"Bearer {META_TOKEN}"})

# Google clients placeholders
sheets_client = None
drive_service = None

# OpenAI init
openai.api_key = OPENAI_API_KEY
OPENAI_MODEL_RAG = "gpt-4o-mini"  # solicitado por el usuario

# -------------------------
# UTILIDADES DE TELÉFONO Y FECHAS
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
# WHATSAPP - ENVIOS (con reintentos fallback)
# -------------------------
WHATSAPP_API_BASE = f"https://graph.facebook.com/v17.0/{WABA_PHONE_ID}/messages"
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

@retry()
def _post_whatsapp(payload: dict) -> dict:
    resp = session.post(WHATSAPP_API_BASE, json=payload, timeout=30)
    if resp.status_code not in (200,201):
        logger.warning("WhatsApp API returned %s: %s", resp.status_code, resp.text)
        resp.raise_for_status()
    return resp.json()

def send_text(to: str, text: str) -> Tuple[bool, dict]:
    to_norm = normalize_msisdn(to)
    payload = {"messaging_product":"whatsapp","to":to_norm,"type":"text","text":{"body":text}}
    try:
        _rate_limit_sleep()
        resp = _post_whatsapp(payload)
        mid = resp.get("messages",[{}])[0].get("id")
        logger.info("Texto enviado a %s mid=%s", mask_phone(to_norm), mid)
        return True, resp
    except Exception as e:
        logger.error("Error enviando texto a %s: %s", mask_phone(to), str(e))
        return False, {"error": str(e)}

def send_template(to: str, template_name: str, language_code: str="es_MX", components: Optional[list]=None) -> Tuple[bool, dict]:
    to_norm = normalize_msisdn(to)
    template = {"name":template_name,"language":{"code":language_code}}
    if components:
        template["components"] = components
    payload = {"messaging_product":"whatsapp","to":to_norm,"type":"template","template":template}
    try:
        _rate_limit_sleep()
        resp = _post_whatsapp(payload)
        mid = resp.get("messages",[{}])[0].get("id")
        logger.info("Template %s enviado a %s mid=%s", template_name, mask_phone(to_norm), mid)
        return True, resp
    except Exception as e:
        logger.error("Error enviando template a %s: %s", mask_phone(to), str(e))
        return False, {"error": str(e)}

def send_image_url(to: str, image_url: str, caption: Optional[str]=None) -> Tuple[bool, dict]:
    to_norm = normalize_msisdn(to)
    payload = {"messaging_product":"whatsapp","to":to_norm,"type":"image","image":{"link":image_url}}
    if caption:
        payload["image"]["caption"] = caption
    try:
        _rate_limit_sleep()
        resp = _post_whatsapp(payload)
        mid = resp.get("messages",[{}])[0].get("id")
        logger.info("Imagen enviada a %s mid=%s url=%s", mask_phone(to_norm), mid, image_url)
        return True, resp
    except Exception as e:
        logger.error("Error enviando imagen a %s: %s", mask_phone(to), str(e))
        return False, {"error": str(e)}

# -------------------------
# GOOGLE SHEETS y DRIVE
# -------------------------
def initialize_google_clients():
    global sheets_client, drive_service
    try:
        creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
        scopes = ["https://www.googleapis.com/auth/spreadsheets","https://www.googleapis.com/auth/drive"]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        sheets_client = gspread.authorize(creds)
        drive_service = build("drive", "v3", credentials=creds, cache_discovery=False)
        logger.info("Cliente Google inicializado")
    except Exception as e:
        logger.critical("Error inicializando Google clients: %s", str(e))
        raise

initialize_google_clients()

SHEET_MIN_FIELDS = ["status","greeted_at","renovacion_vencimiento","recordar_30d","reintento_7d","campaña_origen","notas","wa_last10","nombre"]

def get_leads_worksheet():
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
    data = get_contact_data(row_index)
    prev = data.get("notas","")
    ts = datetime.now(FLASK_TZ).isoformat()
    new_notes = (prev + "\n" + f"[{ts}] {note}").strip()
    set_contact_data(row_index, {"notas": new_notes})

# -------------------------
# DRIVE: descarga de manuales y backup multimedia
# -------------------------
def _drive_find_pdf_by_id(file_id: str) -> Optional[Dict[str,Any]]:
    try:
        f = drive_service.files().get(fileId=file_id, fields="id,name,mimeType").execute()
        if f and f.get("mimeType") == "application/pdf":
            return f
    except Exception as e:
        logger.warning("No se pudo obtener archivo Drive por ID %s: %s", file_id, e)
    return None

def _drive_search_pdf_by_name(name: str) -> Optional[Dict[str,Any]]:
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
            logger.warning("No se encontró manual Drive para domain=%s", domain)
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
    if domain not in rag_cache:
        ok = load_manual_to_cache(domain)
        if not ok:
            return "No encuentro esta información en el manual correspondiente."
    manual = rag_cache.get(domain, {})
    text = manual.get("text","")
    name = manual.get("name","manual")
    if not text:
        return f"No encuentro esta información en el manual {name}."
    query_words = set([w.lower() for w in re.findall(r"\w{3,}", query)])
    paragraphs = [p.strip() for p in re.split(r"\n{1,}", text) if p.strip()]
    scored = []
    for p in paragraphs:
        words = set([w.lower() for w in re.findall(r"\w{3,}", p)])
        score = len(query_words.intersection(words))
        if score > 0:
            scored.append((score, p))
    scored.sort(reverse=True, key=lambda x: x[0])
    context = "\n\n".join([p for _,p in scored[:3]]) if scored else "\n\n".join(paragraphs[:3])
    prompt = (
        f"Eres Vicky, asistente basada en el manual '{name}'. Usa SOLO la información del contexto provisto.\n\n"
        f"Contexto relevante:\n{context}\n\n"
        f"Pregunta: {query}\n\n"
        "Si la respuesta no está en el contexto, responde exactamente: 'No encuentro esta información en el manual correspondiente.'\n"
        f"Al final agrega: 'Esta info proviene del manual {name}'. Responde en español, breve y clara."
    )
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

# -------------------------
# BACKUP MEDIA EN DRIVE
# -------------------------
def backup_media_to_drive(file_bytes: bytes, filename: str, mime_type: str, contact_name: Optional[str], wa_last4: str) -> Optional[str]:
    if not DRIVE_UPLOAD_ROOT_FOLDER_ID:
        logger.info("DRIVE_UPLOAD_ROOT_FOLDER_ID no configurado; omitiendo respaldo")
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
# SALUDO PROTEGIDO Y SHEETS HELPERS
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
    except Exception as e:
        logger.error("Error en should_greet: %s", str(e))
        return True

def mark_greeted(wa_last10: str):
    try:
        row = find_or_create_contact_row(wa_last10)
        now_iso = datetime.now(FLASK_TZ).isoformat()
        set_contact_data(row, {"greeted_at": now_iso})
    except Exception as e:
        logger.error("Error en mark_greeted: %s", str(e))

# -------------------------
# FLUJOS: AUTO e IMSS (resumido y robusto)
# -------------------------
def notify_advisor(wa_id: str, row_index: int, reason: str):
    contact = get_contact_data(row_index)
    nombre = contact.get("nombre") or "Desconocido"
    campaign = contact.get("campaña_origen","")
    body = (
        f"Asesor: nuevo contacto\n"
        f"Nombre: {nombre}\n"
        f"wa_id: {wa_id}\n"
        f"Motivo: {reason}\n"
        f"Campaña: {campaign}\n"
        f"Notas: {contact.get('notas','')[:400]}"
    )
    send_text(ADVISOR_NUMBER, body)

def handle_auto_flow(wa_id: str, text: str, row_index: int):
    st = state.setdefault(wa_id, {"stage":"AUTO_START","updated":time.time()})
    stage = st.get("stage","AUTO_START")
    t = text.strip().lower()
    if stage == "AUTO_START":
        msg = (
            "Planes Auto SECOM:\n\n"
            "1) Amplia Plus\n2) Amplia\n3) Limitada\n\n"
            "Responde con 'INE: <datos>' o 'PLACA: <datos>' para continuar."
        )
        send_text(wa_id, msg)
        st["stage"] = "AUTO_DOCS"
        return
    if stage == "AUTO_DOCS":
        if "ine" in t or t.startswith("ine:"):
            append_note_to_contact(row_index, "INE provisto: " + text)
            send_text(wa_id, "Gracias. Por favor proporciona placa o tarjeta de circulación si la tienes.")
            st["stage"] = "AUTO_PLAN"
            return
        if "placa" in t or "tarjeta" in t:
            append_note_to_contact(row_index, "Placa/tarjeta: " + text)
            send_text(wa_id, "Perfecto. ¿Qué plan te interesa? Responde 1, 2 o 3.")
            st["stage"] = "AUTO_PLAN"
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
                return
            send_text(wa_id, "Selecciona 1,2 o 3. O escribe 'asesor' para contacto humano.")
            return
        append_note_to_contact(row_index, f"Plan elegido: {plan}")
        set_contact_data(row_index, {"status":"en_seguimiento"})
        send_text(wa_id, f"Has seleccionado {plan}. ¿Deseas que un asesor te contacte? Escribe 'asesor'.")
        st["stage"] = "AUTO_RESUMEN"
        return
    if stage == "AUTO_RENOV":
        d = parse_date_from_text(text)
        if d:
            set_contact_data(row_index, {"renovacion_vencimiento": d.isoformat(), "recordar_30d":"TRUE", "reintento_7d":"TRUE"})
            append_note_to_contact(row_index, f"Fecha de renovación: {d.isoformat()}")
            send_text(wa_id, "Fecha guardada. ¿Deseas que un asesor te contacte? Escribe 'asesor'.")
            st["stage"] = "AUTO_RESUMEN"
            return
        send_text(wa_id, "No pude reconocer la fecha. Usa dd-mm-aaaa o aaaa-mm-dd.")
        return
    if stage == "AUTO_RESUMEN":
        if "asesor" in t:
            notify_advisor(wa_id, row_index, "Solicitud contacto desde AUTO")
            send_text(wa_id, "He notificado a un asesor. Te contactarán pronto.")
            st["stage"] = "DONE"
            return
        send_text(wa_id, "Gracias. Si necesitas más, escribe 'menu'.")
        st["stage"] = "DONE"
        return

def handle_imss_flow(wa_id: str, text: str, row_index: int):
    st = state.setdefault(wa_id, {"stage":"IMSS_START","updated":time.time()})
    stage = st.get("stage","IMSS_START")
    t = text.strip().lower()
    if stage == "IMSS_START":
        send_text(wa_id, "IMSS Ley 73: responde 'requisitos', 'cálculo' o 'prestamo'.")
        st["stage"] = "IMSS_QUALIFY"
        return
    if stage == "IMSS_QUALIFY":
        if "requisitos" in t:
            resp = rag_answer("requisitos para pensión IMSS", domain="imss")
            send_text(wa_id, resp)
            st["stage"] = "IMSS_FOLLOW"
            return
        if "cálculo" in t or "calculo" in t:
            send_text(wa_id, "Para calcular necesitamos salario promedio, semanas cotizadas y edad. ¿Quieres asesoría? Responde 'sí' o 'no'.")
            st["stage"] = "IMSS_CALC"
            return
        if "prestamo" in t or "ley 73" in t:
            send_text(wa_id, "Préstamos Ley 73: hasta 12 meses de pensión. ¿Quieres contacto con asesor?")
            st["stage"] = "IMSS_FOLLOW"
            return
        send_text(wa_id, "No entendí. Responde 'requisitos', 'cálculo' o 'prestamo'.")
        return
    if stage == "IMSS_CALC":
        if "sí" in t or "si" in t:
            notify_advisor(wa_id, row_index, "Cliente solicita cálculo IMSS")
            append_note_to_contact(row_index, "Solicitó cálculo IMSS")
            set_contact_data(row_index, {"status":"en_seguimiento"})
            send_text(wa_id, "He notificado a un asesor.")
            st["stage"] = "DONE"
            return
        send_text(wa_id, "Entendido. Si deseas asesor escribe 'asesor'.")
        st["stage"] = "DONE"
        return
    if stage == "IMSS_FOLLOW":
        if "asesor" in t:
            notify_advisor(wa_id, row_index, "Solicitud contacto IMSS")
            send_text(wa_id, "Asesor notificado. Te contactarán.")
            st["stage"] = "DONE"
            return
        send_text(wa_id, "Si necesitas algo más escribe 'menu'.")
        st["stage"] = "DONE"
        return

# -------------------------
# PROMO WORKER (COLA)
# -------------------------
def promo_worker():
    logger.info("Promo worker iniciado")
    while True:
        try:
            job = promo_queue.get()
            if not job:
                time.sleep(1)
                continue
            campaign = job.get("campaign")
            recipients = job.get("recipients", [])
            mode = job.get("mode")
            text = job.get("text")
            template = job.get("template")
            image = job.get("image")
            logger.info("Procesando promo campaign=%s recipients=%d mode=%s", campaign, len(recipients), mode)
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
                            mid = resp.get("messages",[{}])[0].get("id")
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
# ENDPOINTS REQUERIDOS
# -------------------------
@app.before_request
def attach_request_id():
    request_id = request.headers.get("X-Request-Id") or f"{int(time.time()*1000)}"
    g.request_id = request_id
    # Añadir request_id al record (formato del logger incluye request_id)
    # Nota: el formatter del logging usa %(request_id)s; si no está, se añade aquí:
    for h in logging.root.handlers:
        pass

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
        logger.info("Payload vacío")
        return jsonify({"ok": True}), 200
    logger.info("Payload recibido")
    try:
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
                    wa_last4 = re.sub(r"\D","",wa_id)[-4:] if wa_id else "0000"
                    try:
                        row = find_or_create_contact_row(wa_last10)
                    except Exception as e:
                        logger.error("Error accediendo Sheets: %s", str(e))
                        row = None
                    if should_greet(wa_last10):
                        send_text(wa_id, "Hola 👋 Soy Vicky de SECOM. Escribe *AUTO*, *IMSS* o *CONTACTO* para iniciar.")
                        mark_greeted(wa_last10)
                    mtype = msg.get("type")
                    if mtype == "text":
                        text = msg.get("text",{}).get("body","")
                        logger.info("Mensaje de %s: %s", mask_phone(wa_id), text[:200])
                        t_low = text.lower()
                        if "auto" in t_low:
                            handle_auto_flow(wa_id, text, row)
                        elif "imss" in t_low:
                            handle_imss_flow(wa_id, text, row)
                        elif any(k in t_low for k in ["asesor","contacto","llámame","llamame","hablar con christian","christian"]):
                            notify_advisor(wa_id, row, "Solicitud de contacto")
                            send_text(wa_id, "He notificado al asesor. Te contactarán pronto.")
                        elif any(q in t_low for q in ["cómo","como","requisitos","cobertura","qué","que","cuando","cuándo","dónde","donde"]):
                            domain = "auto" if "auto" in t_low or "cobertura" in t_low else "imss" if "imss" in t_low or "pensión" in t_low else None
                            if not domain:
                                send_text(wa_id, "¿Tu duda es sobre *Auto* o *IMSS*? Responde con la palabra correspondiente.")
                            else:
                                resp = rag_answer(text, domain)
                                send_text(wa_id, resp)
                        else:
                            send_text(wa_id, "No entendí. Escribe *AUTO*, *IMSS* o *CONTACTO* para iniciar.")
                    elif mtype in ("image","document","video","audio","sticker"):
                        media_info = msg.get(mtype, {})
                        media_id = media_info.get("id")
                        if media_id:
                            try:
                                media_resp = session.get(f"https://graph.facebook.com/v17.0/{media_id}", params={"fields":"url"}, timeout=20)
                                media_json = media_resp.json()
                                media_url = media_json.get("url")
                                if media_url:
                                    mdata = requests.get(media_url, headers={"Authorization": f"Bearer {META_TOKEN}"}, timeout=30)
                                    b = mdata.content
                                    mime_type = mdata.headers.get("Content-Type","application/octet-stream")
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
                            except Exception:
                                logger.error("Error procesando media: %s", traceback.format_exc())
                                send_text(wa_id, "Recibí tu archivo pero no pude procesarlo completamente.")
                        else:
                            send_text(wa_id, "Archivo recibido. Gracias.")
                    else:
                        send_text(wa_id, "Mensaje recibido. ¿En qué puedo ayudarte?")
        return jsonify({"ok": True}), 200
    except Exception as e:
        logger.error("Error crítico en webhook: %s", traceback.format_exc())
        # Responder 200 para evitar reintentos de Meta
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

# -------------------------
# CARGA INICIAL DE MANUALES EN BACKGROUND
# -------------------------
def background_initial_load():
    for d in ("auto","imss"):
        try:
            load_manual_to_cache(d)
        except Exception:
            logger.warning("No pudo cargar manual inicial %s", d)

bg_thread = threading.Thread(target=background_initial_load, daemon=True)
bg_thread.start()

# -------------------------
# PRUEBAS DE HUMO (comentarios)
# -------------------------
"""
Ejemplos curl:

1) Health:
   curl -s https://<host>/ext/health

2) Test send:
   curl -XPOST https://<host>/ext/test-send -H "Content-Type: application/json" -d '{"to":"5216682478005","text":"Prueba OK"}'

3) Encolar promo texto:
   curl -XPOST https://<host>/ext/send-promo -H "Content-Type: application/json" -d '{"recipients":["5216682478005"],"mode":"text","text":"Promo SECOM ✔️"}'

4) Manuales:
   curl -s https://<host>/ext/manuales

Notas:
- El sistema marca recordar_30d y reintento_7d en Sheets, pero no hay worker persistente para enviar recordatorios (si el dyno se reinicia se pierde). Planee un cron externo para recordatorios periódicos.
- Para producción, añada autenticación en /ext/* (ej. X-Api-Key).
"""

# -------------------------
# RUN (solo en debug; en producción use gunicorn)
# -------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info("Iniciando Flask en puerto %d", port)
    app.run(host="0.0.0.0", port=port, debug=False)


