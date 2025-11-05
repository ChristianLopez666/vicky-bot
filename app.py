#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Vicky SECOM – modo Wapi
Complete self-contained application ready to run with Gunicorn:
  gunicorn app:app --bind 0.0.0.0:$PORT

Features:
- Extended menu (1-9) and send_main_menu()
- Mini-handlers for Pensiones, Auto, Vida/Salud/VRIM, IMSS Ley 73, Personales, Tarjetas, Empresarial, Nómina, Contacto
- RAG based on OpenAI 1.x client (client.chat.completions.create)
- Google Sheets & Drive integration with circuit-breaker for repeated 404s
- WhatsApp Cloud API sending with simulation mode if credentials missing
- Endpoints preserved: /webhook (GET, POST), /ext/health (GET), /ext/test-send (POST), /ext/send-promo (POST), /ext/manuales (GET)
"""

from __future__ import annotations
import os
import io
import json
import time
import re
import unicodedata
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
from openai import OpenAI
from PyPDF2 import PdfReader

# Optional Google libs
try:
    import gspread
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseUpload
    GOOGLE_LIBS_AVAILABLE = True
except Exception:
    GOOGLE_LIBS_AVAILABLE = False

# -------------------------
# Logging safe for threads
# -------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()


class RequestIdFilter(logging.Filter):
    def filter(self, record):
        try:
            from flask import g as _g
            record.request_id = getattr(_g, "request_id", "no-rid")
        except Exception:
            record.request_id = "no-rid"
        return True


logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [%(request_id)s] %(message)s"
)
logger = logging.getLogger("vicky-secom-wapi")
logger.addFilter(RequestIdFilter())

# -------------------------
# Timezone
# -------------------------
try:
    from zoneinfo import ZoneInfo
    FLASK_TZ = ZoneInfo("America/Mazatlan")
except Exception:
    logger.warning("ZoneInfo not available; using UTC")
    from datetime import timezone as _tz
    FLASK_TZ = _tz.utc

# -------------------------
# Simple retry decorator
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
# Environment variables and flags
# -------------------------
env = os.environ.copy()
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

WHATSAPP_ENABLED = bool(META_TOKEN and WABA_PHONE_ID)
GOOGLE_ENABLED = bool(GOOGLE_LIBS_AVAILABLE and GOOGLE_CREDENTIALS_JSON and SHEETS_ID_LEADS and SHEETS_TITLE_LEADS)
OPENAI_ENABLED = bool(OPENAI_API_KEY)

logger.info("Flags: WHATSAPP=%s GOOGLE=%s OPENAI=%s", WHATSAPP_ENABLED, GOOGLE_ENABLED, OPENAI_ENABLED)

# -------------------------
# OpenAI 1.x client initialization
# -------------------------
client: Optional[OpenAI] = None
OPENAI_MODEL_RAG = "gpt-4o-mini"
if OPENAI_ENABLED:
    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        logger.info("OpenAI client initialized (1.x)")
    except Exception as e:
        logger.error("OpenAI init error: %s", e)
        client = None

# -------------------------
# Flask app and global state
# -------------------------
app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False

QPS_LIMIT = 5
PROMO_BATCH_LIMIT = 100

session = requests.Session()
if META_TOKEN:
    session.headers.update({"Authorization": f"Bearer {META_TOKEN}"})

# state keyed by wa_last10
state: Dict[str, Dict[str, Any]] = {}
RAG_TTL_SECONDS = 6 * 3600
rag_cache: Dict[str, Dict[str, Any]] = {}
promo_queue = queue.Queue()

# Google clients placeholders
sheets_client = None
drive_service = None
GOOGLE_REASON: Optional[str] = None

# Circuit-breaker for repeated 404s
_sheets_404_hits = 0
_sheets_404_window_start = 0.0

# -------------------------
# Utilities: normalization, masking
# -------------------------
def norm(s: str) -> str:
    s = (s or "").lower()
    s = unicodedata.normalize("NFD", s)
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


def mask_token(t: Optional[str]) -> str:
    if not t:
        return "MISSING"
    s = str(t)
    return "****" + s[-4:] if len(s) > 4 else "*" * len(s)


def mask_phone(p: Optional[str]) -> str:
    if not p:
        return "UNKNOWN"
    d = re.sub(r"\D", "", p)
    if len(d) <= 4:
        return "*" * max(0, len(d)-1) + d[-1:]
    return "*" * (len(d)-4) + d[-4:]


# -------------------------
# MAIN MENU (A)
# -------------------------
MAIN_MENU_ITEMS = [
    {"n":"1","key":"pensiones","title":"Asesoría en pensiones IMSS","aliases":["pensiones","pension","asesoria imss"]},
    {"n":"2","key":"auto","title":"Seguros de Auto (Amplia Plus/Amplia/Limitada)","aliases":["auto","seguro auto","cotizar auto"]},
    {"n":"3","key":"vida_salud","title":"Seguros de Vida y Salud / VRIM","aliases":["vida","salud","vrim","seguro vida","seguro salud"]},
    {"n":"4","key":"imss_ley73","title":"Préstamos a Pensionados IMSS (Ley 73)","aliases":["imss","ley 73","prestamo imss","pensionados"]},
    {"n":"5","key":"personales","title":"Préstamos Personales","aliases":["prestamo personal","personales","credito personal"]},
    {"n":"6","key":"tc","title":"Tarjetas de Crédito (canalización)","aliases":["tarjeta","tarjetas","tc","credito"]},
    {"n":"7","key":"empresarial","title":"Financiamiento Empresarial","aliases":["empresa","empresarial","credito empresarial","leasing","factoraje"]},
    {"n":"8","key":"nomina","title":"Nómina Empresarial","aliases":["nomina","payroll","servicio de nomina"]},
    {"n":"9","key":"contacto","title":"Contactar con Christian","aliases":["contacto","asesor","llamame","llámame","hablar con christian","christian"]},
]


def send_main_menu(wa_id: str):
    lines = ["Menú principal:"]
    for it in MAIN_MENU_ITEMS:
        lines.append(f"{it['n']}) {it['title']}")
    lines.append("— Responde con número o palabra clave. Escribe 0 o 'menu' para volver aquí.")
    send_text(wa_id, "\n".join(lines))


# -------------------------
# Phone/date helpers
# -------------------------
def normalize_msisdn(s: str) -> str:
    if not s:
        return ""
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


# -------------------------
# WhatsApp senders
# -------------------------
WHATSAPP_API_BASE = f"https://graph.facebook.com/v17.0/{WABA_PHONE_ID}/messages" if WABA_PHONE_ID else None

_send_lock = threading.Lock()
_last_send_ts = 0.0


def _rate_limit_sleep():
    global _last_send_ts
    with _send_lock:
        now_ts = time.time()
        min_interval = 1.0 / QPS_LIMIT
        delta = now_ts - _last_send_ts
        if delta < min_interval:
            time.sleep(min_interval - delta)
        _last_send_ts = time.time()


@retry_decorator(attempts=3, initial_wait=1.0)
def _post_whatsapp(payload: dict) -> dict:
    if not WHATSAPP_ENABLED or not WHATSAPP_API_BASE:
        raise RuntimeError("WhatsApp not configured")
    resp = session.post(WHATSAPP_API_BASE, json=payload, timeout=30)
    if resp.status_code not in (200, 201):
        logger.warning("WhatsApp API returned %s: %s", resp.status_code, resp.text)
        resp.raise_for_status()
    return resp.json()


def send_text(to: str, text: str) -> Tuple[bool, dict]:
    to_norm = normalize_msisdn(to)
    payload = {"messaging_product": "whatsapp", "to": to_norm, "type": "text", "text": {"body": text}}
    if not WHATSAPP_ENABLED:
        logger.info("[SIMULATED] send_text to %s: %s", to_norm, text[:140])
        return True, {"simulated": True}
    try:
        _rate_limit_sleep()
        resp = _post_whatsapp(payload)
        mid = resp.get("messages", [{}])[0].get("id")
        logger.info("Text sent to %s mid=%s", mask_phone(to_norm), mid)
        return True, resp
    except Exception as e:
        logger.error("Error sending text to %s: %s", mask_phone(to_norm), str(e))
        return False, {"error": str(e)}


def send_template(to: str, template_name: str, language_code: str = "es_MX", components: Optional[list] = None) -> Tuple[bool, dict]:
    to_norm = normalize_msisdn(to)
    payload = {"messaging_product": "whatsapp", "to": to_norm, "type": "template", "template": {"name": template_name, "language": {"code": language_code}}}
    if components:
        payload["template"]["components"] = components
    if not WHATSAPP_ENABLED:
        logger.info("[SIMULATED] send_template to %s: %s", to_norm, template_name)
        return True, {"simulated": True}
    try:
        _rate_limit_sleep()
        resp = _post_whatsapp(payload)
        mid = resp.get("messages", [{}])[0].get("id")
        logger.info("Template sent to %s mid=%s", mask_phone(to_norm), mid)
        return True, resp
    except Exception as e:
        logger.error("Error sending template to %s: %s", mask_phone(to_norm), str(e))
        return False, {"error": str(e)}


def send_image_url(to: str, image_url: str, caption: Optional[str] = None) -> Tuple[bool, dict]:
    to_norm = normalize_msisdn(to)
    payload = {"messaging_product": "whatsapp", "to": to_norm, "type": "image", "image": {"link": image_url}}
    if caption:
        payload["image"]["caption"] = caption
    if not WHATSAPP_ENABLED:
        logger.info("[SIMULATED] send_image_url to %s: %s", to_norm, image_url)
        return True, {"simulated": True}
    try:
        _rate_limit_sleep()
        resp = _post_whatsapp(payload)
        mid = resp.get("messages", [{}])[0].get("id")
        logger.info("Image sent to %s mid=%s", mask_phone(to_norm), mid)
        return True, resp
    except Exception as e:
        logger.error("Error sending image to %s: %s", mask_phone(to_norm), str(e))
        return False, {"error": str(e)}


# -------------------------
# Google Sheets & Drive (init + helpers)
# -------------------------
def initialize_google_clients():
    global sheets_client, drive_service, GOOGLE_ENABLED, GOOGLE_REASON
    if not GOOGLE_LIBS_AVAILABLE:
        GOOGLE_ENABLED = False
        GOOGLE_REASON = "gspread/google libs not installed"
        logger.warning("Google libs not available")
        return
    if not GOOGLE_CREDENTIALS_JSON:
        GOOGLE_ENABLED = False
        GOOGLE_REASON = "GOOGLE_CREDENTIALS_JSON missing"
        logger.warning("GOOGLE_CREDENTIALS_JSON not configured")
        return
    try:
        creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
        scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        sheets_client = gspread.authorize(creds)
        drive_service = build("drive", "v3", credentials=creds, cache_discovery=False)
        GOOGLE_ENABLED = True
        GOOGLE_REASON = None
        logger.info("Google Sheets & Drive initialized")
    except Exception as e:
        s = str(e)
        GOOGLE_ENABLED = False
        GOOGLE_REASON = s
        if "404" in s.lower() or "notfound" in s.lower():
            GOOGLE_REASON = f"Sheets 404/permission. Expected sheet title: {SHEETS_TITLE_LEADS}"
            _note_google_404(GOOGLE_REASON)
            logger.error("Google Sheets 404: check SHEETS_ID_LEADS and share with service account. EXPECTED_TITLE=%s", SHEETS_TITLE_LEADS)
        else:
            _note_google_404("Google init error: " + s)
            logger.error("Google init error: %s", s)


# initialize once
initialize_google_clients()

SHEET_MIN_FIELDS = ["status", "greeted_at", "renovacion_vencimiento", "recordar_30d", "reintento_7d", "campaña_origen", "notas", "wa_last10", "nombre"]


def _note_google_404(reason: str):
    global GOOGLE_REASON, _sheets_404_hits, _sheets_404_window_start, GOOGLE_ENABLED
    GOOGLE_REASON = reason
    now_ts = time.time()
    if now_ts - _sheets_404_window_start > 60:
        _sheets_404_window_start = now_ts
        _sheets_404_hits = 0
    _sheets_404_hits += 1
    logger.warning("Google Sheets issue noted: %s (hit %d)", reason, _sheets_404_hits)
    if _sheets_404_hits >= 3:
        GOOGLE_ENABLED = False
        def _reactivate():
            global GOOGLE_ENABLED, _sheets_404_hits, _sheets_404_window_start
            GOOGLE_ENABLED = True
            _sheets_404_hits = 0
            _sheets_404_window_start = time.time()
            logger.info("Reactivating Google after circuit-breaker pause")
        threading.Timer(120, _reactivate).start()
        logger.warning("Temporarily disabling Google (circuit breaker). Operating in memory for 120s.")


def get_leads_worksheet():
    if not GOOGLE_ENABLED or not sheets_client:
        raise RuntimeError("Google Sheets not available")
    try:
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
    except Exception as e:
        s = str(e)
        reason = f"Sheets error: {s} EXPECTED_TAB={SHEETS_TITLE_LEADS}"
        _note_google_404(reason)
        logger.error("Error getting worksheet: %s", s)
        raise


def find_or_create_contact_row(wa_last10: str) -> int:
    ws = get_leads_worksheet()
    records = ws.get_all_records()
    for i, r in enumerate(records, start=2):
        if str(r.get("wa_last10", "")).endswith(wa_last10):
            return i
    header = ws.row_values(1)
    row = {k: "" for k in header}
    row["status"] = "nuevo"
    row["wa_last10"] = wa_last10
    values = [row.get(col, "") for col in header]
    ws.append_row(values)
    return len(ws.get_all_values())


def find_contact_row_by_last10(wa_last10: str) -> Optional[int]:
    try:
        ws = get_leads_worksheet()
        records = ws.get_all_records()
        for i, r in enumerate(records, start=2):
            if str(r.get("wa_last10", "")).endswith(wa_last10):
                return i
        return None
    except Exception as e:
        logger.debug("find_contact_row_by_last10 fallback (Sheets off): %s", str(e))
        return None


def find_or_create_contact_row_safe(wa_last10: str) -> Optional[int]:
    try:
        return find_or_create_contact_row(wa_last10)
    except Exception as e:
        s = str(e)
        if "404" in s.lower() or "notfound" in s.lower():
            _note_google_404("Sheets 404 when creating row")
        logger.warning("Could not create row in Sheets: %s", s)
        return None


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
    for k, v in fields.items():
        if k in header:
            new_row[header.index(k)] = str(v)
    ws.update(f"{row_index}:{row_index}", [new_row])


def append_note_to_contact(row_index: int, note: str):
    try:
        data = get_contact_data(row_index)
        prev = data.get("notas", "")
        ts = now_iso()
        new_notes = (prev + "\n" + f"[{ts}] {note}").strip()
        set_contact_data(row_index, {"notas": new_notes})
    except Exception as e:
        logger.warning("Could not append note to Sheets: %s", str(e))


# -------------------------
# Drive helpers used by RAG and backup
# -------------------------
def _drive_find_pdf_by_id(file_id: str) -> Optional[Dict[str, Any]]:
    if not GOOGLE_ENABLED or not drive_service:
        return None
    try:
        f = drive_service.files().get(fileId=file_id, fields="id,name,mimeType").execute()
        if f and f.get("mimeType") == "application/pdf":
            return f
    except Exception as e:
        logger.warning("Could not get Drive file by ID %s: %s", file_id, e)
    return None


def _drive_search_pdf_by_name(name: str) -> Optional[Dict[str, Any]]:
    if not GOOGLE_ENABLED or not drive_service:
        return None
    try:
        q = f"name = '{name}' and mimeType='application/pdf' and trashed=false"
        res = drive_service.files().list(q=q, pageSize=10, fields="files(id,name,mimeType)").execute()
        files = res.get("files", [])
        if files:
            return files[0]
    except Exception as e:
        logger.warning("Error searching PDF by name %s: %s", name, e)
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
        logger.error("Error downloading Drive file %s: %s", file_id, e)
        return None


def backup_media_to_drive(file_bytes: bytes, filename: str, mime_type: str, contact_name: Optional[str], wa_last4: str) -> Optional[str]:
    if not (GOOGLE_ENABLED and drive_service and DRIVE_UPLOAD_ROOT_FOLDER_ID):
        logger.info("Drive not configured; skipping backup")
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
        logger.info("Media backed up to Drive: %s link=%s", filename, link)
        return link
    except Exception as e:
        logger.error("Error uploading media to Drive: %s", str(e))
        return None


# -------------------------
# RAG using OpenAI 1.x
# -------------------------
def rag_answer(query: str, domain: str = "auto") -> str:
    domain = domain if domain in ("auto", "imss") else "auto"
    cached = rag_cache.get(domain)
    if cached and time.time() - cached.get("loaded_at", 0) > RAG_TTL_SECONDS:
        rag_cache.pop(domain, None)
        cached = None
    if not cached:
        ok = load_manual_to_cache(domain)
        if not ok:
            return "No encuentro esta información en el manual correspondiente."
        cached = rag_cache.get(domain)
    text = cached.get("text", "")
    name = cached.get("name", "manual")
    if not text:
        return f"No encuentro esta información en el manual {name}."
    query_words = set([w.lower() for w in re.findall(r"\w{3,}", query or "")])
    paragraphs = [p.strip() for p in re.split(r"\n{1,}", text) if p.strip()]
    scored = []
    for p in paragraphs:
        words = set([w.lower() for w in re.findall(r"\w{3,}", p)])
        score = len(query_words.intersection(words))
        if score > 0:
            scored.append((score, p))
    scored.sort(reverse=True, key=lambda x: x[0])
    context = "\n\n".join([p for _, p in scored[:3]]) if scored else "\n\n".join(paragraphs[:3])
    prompt = (
        f"Eres Vicky, asistente basada en el manual '{name}'. Usa SOLO la información del contexto provisto.\n\n"
        f"Contexto relevante:\n{context}\n\n"
        f"Pregunta: {query}\n\n"
        "Si la respuesta no está en el contexto, responde exactamente: 'No encuentro esta información en el manual correspondiente.'\n"
        f"Al final agrega: 'Esta info proviene del manual {name}'. Responde en español, breve y clara."
    )
    if not client:
        logger.info("OpenAI client not configured; returning fallback")
        return "No puedo consultar el manual en este momento (servicio de IA no configurado)."
    try:
        resp = client.chat.completions.create(
            model=OPENAI_MODEL_RAG,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=300
        )
        answer = (resp.choices[0].message.content or "").strip()
        return answer
    except Exception as e:
        logger.error("Error calling OpenAI for RAG: %s", e)
        return "No puedo procesar la consulta ahora. Intenta más tarde."


# -------------------------
# notify_advisor (shared)
# -------------------------
def notify_advisor(wa_id: str, row_index: Optional[int], reason: str):
    nombre = "Desconocido"
    campaña = ""
    notas = ""
    if row_index:
        try:
            contact = get_contact_data(row_index)
            nombre = contact.get("nombre") or nombre
            campaña = contact.get("campaña_origen", "")
            notas = contact.get("notas", "")
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


# -------------------------
# should_greet & mark_greeted (fix for crash)
# -------------------------
def should_greet(wa_last10: str) -> bool:
    try:
        row = None
        if GOOGLE_ENABLED:
            try:
                row = find_contact_row_by_last10(wa_last10)
            except Exception as e:
                logger.debug("should_greet: find_contact_row_by_last10 error: %s", str(e))
                row = None
        if not row:
            st = state.get(wa_last10, {})
            last = st.get("greeted_at")
            if not last:
                return True
            try:
                dt = datetime.fromisoformat(last)
                return (datetime.now(FLASK_TZ) - dt) > timedelta(hours=24)
            except Exception:
                return True
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
        logger.warning("should_greet fallback error: %s", str(e))
        st = state.get(wa_last10, {})
        last = st.get("greeted_at")
        if not last:
            return True
        try:
            dt = datetime.fromisoformat(last)
            return (datetime.now(FLASK_TZ) - dt) > timedelta(hours=24)
        except Exception:
            return True


def mark_greeted(wa_last10: str):
    try:
        row = None
        if GOOGLE_ENABLED:
            try:
                row = find_contact_row_by_last10(wa_last10)
            except Exception:
                row = None
        if row:
            set_contact_data(row, {"greeted_at": now_iso()})
        else:
            st = state.setdefault(wa_last10, {})
            st["greeted_at"] = now_iso()
    except Exception as e:
        logger.warning("mark_greeted fallback: %s", str(e))
        st = state.setdefault(wa_last10, {})
        st["greeted_at"] = now_iso()


# -------------------------
# Mini-handlers (complete) - user requested handlers
# -------------------------
def handle_vida_salud_flow(wa_id, text, row_index):
    wa_key = last10(wa_id)
    st = state.setdefault(wa_key, {"stage": "VIDA_START", "updated": time.time()})
    tN = norm(text or "")
    if st["stage"] == "VIDA_START":
        send_text(wa_id, "¿Qué te interesa? 1) Vida  2) Salud/VRIM  3) Ahorro/Protección")
        st["stage"] = "VIDA_PICK"
        st["updated"] = time.time()
        return
    if st["stage"] == "VIDA_PICK":
        if "2" in tN or "vrim" in tN or "salud" in tN:
            send_text(wa_id, "VRIM: membresía médica con atención privada. ¿Me regalas tu nombre para contactarte?")
            st["stage"] = "VIDA_NAME"
            st["updated"] = time.time()
            return
        if "1" in tN or "vida" in tN:
            send_text(wa_id, "Seguro de vida: ¿monto asegurado deseado y edad?")
            st["stage"] = "VIDA_DATA"
            st["updated"] = time.time()
            return
        send_text(wa_id, "¿Cuál te interesa? (1 Vida / 2 VRIM)")
        return
    if st["stage"] == "VIDA_NAME":
        if row_index:
            append_note_to_contact(row_index, f"VRIM interesado. Nombre: {text}")
        notify_advisor(wa_id, row_index, "Interés VRIM")
        send_text(wa_id, "¡Gracias! Un asesor te contactará. Escribe 0 para menú.")
        st["stage"] = "DONE"
        st["updated"] = time.time()
        return
    if st["stage"] == "VIDA_DATA":
        if row_index:
            append_note_to_contact(row_index, f"Vida datos: {text}")
        notify_advisor(wa_id, row_index, "Interés vida")
        send_text(wa_id, "¡Perfecto! Te contactaremos. Escribe 0 para menú.")
        st["stage"] = "DONE"
        st["updated"] = time.time()
        return


def handle_personales_flow(wa_id, text, row_index):
    wa_key = last10(wa_id)
    st = state.setdefault(wa_key, {"stage": "PERS_START", "updated": time.time()})
    if st["stage"] == "PERS_START":
        send_text(wa_id, "Préstamos personales: ¿qué monto y plazo buscas? (ej. 80,000 a 24 meses)")
        st["stage"] = "PERS_DATA"
        st["updated"] = time.time()
        return
    if st["stage"] == "PERS_DATA":
        if row_index:
            append_note_to_contact(row_index, f"Prestamo personal solicitado: {text}")
            set_contact_data(row_index, {"status": "en_seguimiento"})
        notify_advisor(wa_id, row_index, "Préstamo personal")
        send_text(wa_id, "Listo. Te contactaremos en breve. Escribe 0 para menú.")
        st["stage"] = "DONE"
        st["updated"] = time.time()
        return


def handle_tc_flow(wa_id, text, row_index):
    wa_key = last10(wa_id)
    st = state.setdefault(wa_key, {"stage": "TC_START", "updated": time.time()})
    if st["stage"] == "TC_START":
        send_text(wa_id, "Tarjetas de crédito: tomaré tus datos y un asesor te contacta. ¿Cuál es tu nombre?")
        st["stage"] = "TC_NAME"
        st["updated"] = time.time()
        return
    if st["stage"] == "TC_NAME":
        if row_index:
            append_note_to_contact(row_index, f"TC - Nombre: {text}")
        notify_advisor(wa_id, row_index, "Tarjeta de crédito")
        send_text(wa_id, "¡Gracias! Te contactamos pronto. Escribe 0 para menú.")
        st["stage"] = "DONE"
        st["updated"] = time.time()
        return


def handle_empresarial_flow(wa_id, text, row_index):
    wa_key = last10(wa_id)
    st = state.setdefault(wa_key, {"stage": "EMP_START", "updated": time.time()})
    tN = norm(text or "")
    if st["stage"] == "EMP_START":
        send_text(wa_id, "¿Qué tipo? 1) Línea de crédito  2) Arrendamiento  3) Factoraje")
        st["stage"] = "EMP_TYPE"
        st["updated"] = time.time()
        return
    if st["stage"] == "EMP_TYPE":
        st["tipo"] = "línea" if "1" in tN else ("arrendamiento" if "2" in tN else ("factoraje" if "3" in tN else "na"))
        send_text(wa_id, "¿A qué se dedica tu empresa?")
        st["stage"] = "EMP_GIRO"
        st["updated"] = time.time()
        return
    if st["stage"] == "EMP_GIRO":
        st["giro"] = text
        send_text(wa_id, "¿Monto aproximado?")
        st["stage"] = "EMP_MONTO"
        st["updated"] = time.time()
        return
    if st["stage"] == "EMP_MONTO":
        if row_index:
            append_note_to_contact(row_index, f"Empresarial - {st.get('tipo')} - Giro: {st.get('giro')} - Monto: {text}")
        notify_advisor(wa_id, row_index, "Financiamiento empresarial")
        send_text(wa_id, "Perfecto. Un asesor te contactará. Escribe 0 para menú.")
        st["stage"] = "DONE"
        st["updated"] = time.time()
        return


def handle_nomina_flow(wa_id, text, row_index):
    wa_key = last10(wa_id)
    st = state.setdefault(wa_key, {"stage": "NOM_START", "updated": time.time()})
    if st["stage"] == "NOM_START":
        send_text(wa_id, "Nómina empresarial Inbursa: ¿cuántos empleados manejas?")
        st["stage"] = "NOM_SIZE"
        st["updated"] = time.time()
        return
    if st["stage"] == "NOM_SIZE":
        if row_index:
            append_note_to_contact(row_index, f"Nómina - Empleados: {text}")
        notify_advisor(wa_id, row_index, "Nómina empresarial")
        send_text(wa_id, "Gracias. Te contactaremos para detalles. Escribe 0 para menú.")
        st["stage"] = "DONE"
        st["updated"] = time.time()
        return


def handle_pensiones_flow(wa_id, text, row_index):
    wa_key = last10(wa_id)
    st = state.setdefault(wa_key, {"stage": "PEN_START", "updated": time.time()})
    tN = norm(text or "")
    if st["stage"] == "PEN_START":
        send_text(wa_id, "Opciones: 1) Modalidad 40  2) Semanas  3) Cálculo  4) Consulta general")
        st["stage"] = "PEN_Q"
        st["updated"] = time.time()
        return
    if st["stage"] == "PEN_Q":
        send_text(wa_id, rag_answer(text or "", domain="imss"))
        return


FLOW_HANDLERS = {
    "auto": None,  # to be assigned after definition of handle_auto_flow (which may be in other code)
    "imss_ley73": None,  # same for imss
    "vida_salud": handle_vida_salud_flow,
    "personales": handle_personales_flow,
    "tc": handle_tc_flow,
    "empresarial": handle_empresarial_flow,
    "nomina": handle_nomina_flow,
    "pensiones": handle_pensiones_flow,
    "contacto": lambda wa_id, text, row: (notify_advisor(wa_id, row, "Solicitud contacto"), send_text(wa_id, "He notificado al asesor. Te contactarán pronto.")),
}

# Note: handle_auto_flow and handle_imss_flow might have been implemented earlier/elsewhere; if not present, define minimal versions:
def handle_auto_flow(wa_id: str, text: str, row_index: Optional[int]):
    wa_key = last10(wa_id)
    st = state.setdefault(wa_key, {"stage": "AUTO_START", "updated": time.time()})
    stage = st.get("stage", "AUTO_START")
    tN = norm(text or "")
    # Info triggers
    info_triggers = ("cobertura", "coberturas", "deducible", "rc", "robo", "asistencia", "daños", "que cubre", "incluye")
    if any(k in tN for k in info_triggers):
        send_text(wa_id, rag_answer(text or "", "auto"))
        return
    if stage == "AUTO_START":
        msg = ("Planes Auto SECOM:\n\n1) Amplia Plus\n2) Amplia\n3) Limitada\n\n"
               "Si quieres coberturas o deducibles, escríbelo (ej. coberturas) y te explico.\n"
               "Responde con: INE: <...> y PLACA: <...> para avanzar. Escribe 'renovación' para guardar fecha.")
        send_text(wa_id, msg)
        st["stage"] = "AUTO_DOCS"
        st["updated"] = time.time()
        return
    if stage == "AUTO_DOCS":
        t = text or ""
        if "ine" in t.lower() or "placa" in t.lower():
            if row_index:
                append_note_to_contact(row_index, f"AUTO datos: {t}")
            send_text(wa_id, "Gracias. Continuamos.")
            st["stage"] = "AUTO_PLAN"
            st["updated"] = time.time()
            return
        send_text(wa_id, "Por favor envía INE: <datos> o PLACA: <datos> para continuar.")
        return
    if stage == "AUTO_PLAN":
        t = (text or "").lower()
        if "1" in t or "amplia plus" in t:
            plan = "Amplia Plus"
        elif "2" in t or "amplia" in t:
            plan = "Amplia"
        elif "3" in t or "limitada" in t:
            plan = "Limitada"
        else:
            if "renov" in t or "vencim" in t:
                st["stage"] = "AUTO_RENOV"
                send_text(wa_id, "Indica fecha de vencimiento (dd-mm-aaaa).")
                st["updated"] = time.time()
                return
            send_text(wa_id, "Selecciona 1,2 o 3 o escribe 'asesor'.")
            return
        if row_index:
            append_note_to_contact(row_index, f"Plan elegido: {plan}")
            set_contact_data(row_index, {"status": "en_seguimiento"})
        send_text(wa_id, f"Has seleccionado {plan}. ¿Deseas que un asesor te contacte? Escribe 'asesor'.")
        st["stage"] = "AUTO_RESUMEN"
        st["updated"] = time.time()
        return
    if stage == "AUTO_RENOV":
        d = parse_date_from_text(text or "")
        if d:
            if row_index:
                set_contact_data(row_index, {"renovacion_vencimiento": d.isoformat(), "recordar_30d": "TRUE", "reintento_7d": "TRUE"})
                append_note_to_contact(row_index, f"Fecha renovación: {d.isoformat()}")
            send_text(wa_id, "Fecha guardada. ¿Deseas que un asesor te contacte? Escribe 'asesor'.")
            st["stage"] = "AUTO_RESUMEN"
            st["updated"] = time.time()
            return
        send_text(wa_id, "Formato inválido. Usa dd-mm-aaaa.")
        return
    if stage == "AUTO_RESUMEN":
        t = (text or "").lower()
        if "asesor" in t:
            notify_advisor(wa_id, row_index, "Solicitud contacto AUTO")
            send_text(wa_id, "He notificado al asesor. Te contactarán pronto.")
            st["stage"] = "DONE"
            st["updated"] = time.time()
            return
        send_text(wa_id, "Gracias. Escribe 0 para volver al menú.")
        st["stage"] = "DONE"
        st["updated"] = time.time()
        return


def handle_imss_flow(wa_id: str, text: str, row_index: Optional[int]):
    wa_key = last10(wa_id)
    st = state.setdefault(wa_key, {"stage": "IMSS_START", "updated": time.time()})
    stage = st.get("stage", "IMSS_START")
    tN = norm(text or "")
    info_triggers_imss = ("requisito", "requisitos", "modalidad", "semanas", "pension", "ley 73", "monto", "calculo", "prestamo")
    if any(k in tN for k in info_triggers_imss):
        send_text(wa_id, rag_answer(text or "", "imss"))
        return
    if stage == "IMSS_START":
        send_text(wa_id, "IMSS Ley 73: los beneficios de nómina son adicionales y NO obligatorios. Responde 'requisitos', 'cálculo' o 'prestamo'.")
        st["stage"] = "IMSS_QUALIFY"
        st["updated"] = time.time()
        return
    if stage == "IMSS_QUALIFY":
        t = (text or "").lower()
        if "requisit" in t:
            send_text(wa_id, rag_answer("requisitos para pensión IMSS", domain="imss"))
            st["stage"] = "IMSS_FOLLOW"
            st["updated"] = time.time()
            return
        if "cálcul" in t or "calculo" in t or "monto" in t:
            send_text(wa_id, "Para calcular necesitamos salario promedio, semanas cotizadas y edad. ¿Quieres asesoría? (sí/no)")
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
        t = (text or "").lower()
        if "sí" in t or "si" in t:
            send_text(wa_id, "Quedas pre-autorizado de forma tentativa. Si cambias tu nómina con nosotros obtienes beneficios extra, pero no es requisito. ¿Deseas que un asesor te contacte?")
            notify_advisor(wa_id, row_index, "Cliente solicita cálculo IMSS")
            if row_index:
                append_note_to_contact(row_index, "Solicitó cálculo IMSS")
                set_contact_data(row_index, {"status": "en_seguimiento"})
            send_text(wa_id, "He notificado a un asesor.")
            st["stage"] = "DONE"
            st["updated"] = time.time()
            return
        send_text(wa_id, "Entendido. Escribe 'asesor' si quieres contacto.")
        st["stage"] = "DONE"
        st["updated"] = time.time()
        return
    if stage == "IMSS_FOLLOW":
        t = (text or "").lower()
        if "asesor" in t:
            send_text(wa_id, "Quedas pre-autorizado de forma tentativa. Si cambias tu nómina con nosotros obtienes beneficios extra, pero no es requisito. ¿Deseas que un asesor te contacte?")
            notify_advisor(wa_id, row_index, "Solicitud contacto IMSS")
            send_text(wa_id, "Asesor notificado. Te contactarán.")
            st["stage"] = "DONE"
            st["updated"] = time.time()
            return
        send_text(wa_id, "Si necesitas algo más escribe 'menu'.")
        st["stage"] = "DONE"
        st["updated"] = time.time()
        return


# assign handlers for those keys
FLOW_HANDLERS["auto"] = handle_auto_flow
FLOW_HANDLERS["imss_ley73"] = handle_imss_flow

# -------------------------
# Promo worker (unchanged)
# -------------------------
def promo_worker():
    logger.info("Promo worker started")
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
            logger.info("Processing promo recipients=%d mode=%s", len(recipients), mode)
            idx = 0
            while idx < len(recipients):
                batch = recipients[idx:idx + PROMO_BATCH_LIMIT]
                for r in batch:
                    try:
                        if mode == "text":
                            ok, resp = send_text(r, text)
                        elif mode == "template":
                            ok, resp = send_template(r, template.get("name"), template.get("language", "es_MX"), components=template.get("components"))
                        elif mode == "image":
                            ok, resp = send_image_url(r, image.get("url"), caption=image.get("caption"))
                        else:
                            ok = False
                            resp = {"error": "unknown mode"}
                        if ok:
                            mid = resp.get("messages", [{}])[0].get("id") if isinstance(resp, dict) else None
                            logger.info("Promo sent to %s mid=%s", r, mid)
                        else:
                            logger.warning("Promo failed to %s resp=%s", r, resp)
                    except Exception:
                        logger.error("Exception sending promo to %s: %s", r, traceback.format_exc())
                    time.sleep(max(0.02, 1.0 / QPS_LIMIT))
                idx += PROMO_BATCH_LIMIT
            promo_queue.task_done()
        except Exception:
            logger.error("Error in promo_worker: %s", traceback.format_exc())
            time.sleep(2)


promo_thread = threading.Thread(target=promo_worker, daemon=True)
promo_thread.start()

# -------------------------
# Webhook endpoints & router
# -------------------------
@app.before_request
def attach_request_id():
    request_id = request.headers.get("X-Request-Id") or f"{int(time.time() * 1000)}"
    g.request_id = request_id


@app.route("/webhook", methods=["GET"])
def webhook_verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    logger.info("Webhook verification request")
    if mode and token:
        if mode == "subscribe" and token == VERIFY_TOKEN:
            logger.info("Webhook verified")
            return challenge, 200
        else:
            logger.warning("Invalid verify token")
            return "Forbidden", 403
    return "Bad Request", 400


@app.route("/webhook", methods=["POST"])
def webhook_receive():
    payload = request.get_json(silent=True)
    if not payload:
        logger.info("Empty payload")
        return jsonify({"ok": True}), 200
    logger.info("📥 Payload received")
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
                    wa_last4 = re.sub(r"\D", "", wa_id)[-4:] if wa_id else "0000"

                    # search-only (no create)
                    row = None
                    if GOOGLE_ENABLED:
                        try:
                            row = find_contact_row_by_last10(wa_last10)
                        except Exception as e:
                            logger.debug("find_contact_row_by_last10 error: %s", str(e))
                            row = None

                    mtype = msg.get("type")
                    # greet if unknown
                    if row is None and mtype == "text":
                        text_preview = (msg.get("text", {}).get("body", "") or "")[:200]
                        tN_preview = norm(text_preview)
                        try:
                            if should_greet(wa_last10):
                                send_text(wa_id, "Hola 👋 Soy Vicky. Escribe *AUTO*, *IMSS* o *CONTACTO* para iniciar.")
                                mark_greeted(wa_last10)
                        except Exception as e:
                            logger.warning("should_greet error: %s", str(e))
                        if any(k in tN_preview for k in ("auto", "imss", "asesor", "contacto", "llamame", "llámame", "christian")):
                            try:
                                row = find_or_create_contact_row_safe(wa_last10)
                            except Exception as e:
                                logger.warning("Could not create row: %s", str(e))
                        if row is None and not any(k in tN_preview for k in ("auto", "imss", "asesor", "contacto", "llamame", "llámame", "christian")):
                            continue

                    # Now process by type
                    if mtype == "text":
                        text = msg.get("text", {}).get("body", "")
                        tN = norm(text or "")

                        # Global menu
                        if tN in ("menu", "0"):
                            send_main_menu(wa_id)
                            continue

                        # ensure row when entering flows
                        def _ensure_row():
                            nonlocal row
                            if row is None:
                                try:
                                    row = find_or_create_contact_row_safe(wa_last10)
                                except Exception:
                                    row = None

                        # number selection 1-9
                        if re.fullmatch(r"[1-9]", (text or "").strip()):
                            _ensure_row()
                            idx = int((text or "").strip()) - 1
                            if 0 <= idx < len(MAIN_MENU_ITEMS):
                                key = MAIN_MENU_ITEMS[idx]["key"]
                                handler = FLOW_HANDLERS.get(key)
                                if handler:
                                    handler(wa_id, text, row)
                                else:
                                    send_text(wa_id, "Opción no disponible.")
                                continue

                        # alias mapping
                        matched = False
                        for it in MAIN_MENU_ITEMS:
                            for alias in it["aliases"]:
                                if alias in tN:
                                    _ensure_row()
                                    handler = FLOW_HANDLERS.get(it["key"])
                                    if handler:
                                        handler(wa_id, text, row)
                                    else:
                                        send_text(wa_id, "Opción no disponible.")
                                    matched = True
                                    break
                            if matched:
                                break
                        if matched:
                            continue

                        # Open questions -> RAG heuristics
                        info_auto = ("cobertura", "coberturas", "deducible", "rc", "robo", "asistencia", "daños", "que cubre", "que incluye", "qué incluye")
                        info_imss = ("requisito", "requisitos", "modalidad", "semanas", "pension", "ley 73", "monto", "calculo", "prestamo")
                        if any(k in tN for k in info_auto):
                            send_text(wa_id, rag_answer(text or "", domain="auto"))
                            continue
                        if any(k in tN for k in info_imss):
                            send_text(wa_id, rag_answer(text or "", domain="imss"))
                            continue

                        # route to flows
                        if "auto" in tN:
                            _ensure_row()
                            handle_auto_flow(wa_id, text, row)
                            continue
                        if "imss" in tN:
                            _ensure_row()
                            handle_imss_flow(wa_id, text, row)
                            continue
                        if any(k in tN for k in ("vida", "salud", "vrim")):
                            _ensure_row()
                            handle_vida_salud_flow(wa_id, text, row)
                            continue
                        if any(k in tN for k in ("prestamo", "personales", "credito personal")):
                            _ensure_row()
                            handle_personales_flow(wa_id, text, row)
                            continue
                        if any(k in tN for k in ("tarjeta", "tarjetas", "tc", "credito")):
                            _ensure_row()
                            handle_tc_flow(wa_id, text, row)
                            continue
                        if any(k in tN for k in ("empresa", "empresarial", "factoraje", "leasing")):
                            _ensure_row()
                            handle_empresarial_flow(wa_id, text, row)
                            continue
                        if any(k in tN for k in ("nomina", "payroll")):
                            _ensure_row()
                            handle_nomina_flow(wa_id, text, row)
                            continue
                        if any(k in tN for k in ("pensiones", "pension", "asesoria imss")):
                            _ensure_row()
                            handle_pensiones_flow(wa_id, text, row)
                            continue

                        send_main_menu(wa_id)
                        continue

                    elif mtype in ("image", "document", "video", "audio", "sticker"):
                        media_info = msg.get(mtype, {}) or {}
                        media_id = media_info.get("id")
                        if media_id:
                            try:
                                media_resp = session.get(f"https://graph.facebook.com/v17.0/{media_id}", params={"fields": "url"}, timeout=20)
                                media_json = media_resp.json()
                                media_url = media_json.get("url")
                                if media_url:
                                    mdata = requests.get(media_url, headers={"Authorization": f"Bearer {META_TOKEN}"} if META_TOKEN else None, timeout=30)
                                    b = mdata.content
                                    mime_type = mdata.headers.get("Content-Type", "application/octet-stream")
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
                                logger.error("Error processing media: %s", traceback.format_exc())
                                send_text(wa_id, "Recibí tu archivo pero no pude procesarlo completamente.")
                        else:
                            send_text(wa_id, "Archivo recibido. Gracias.")
                    else:
                        send_text(wa_id, "Mensaje recibido. ¿En qué puedo ayudarte?")
        return jsonify({"ok": True}), 200
    except Exception:
        logger.error("Critical error in webhook: %s", traceback.format_exc())
        return jsonify({"ok": True}), 200


# -------------------------
# Other endpoints
# -------------------------
@app.route("/ext/health", methods=["GET"])
def ext_health():
    sa_email = None
    try:
        if GOOGLE_CREDENTIALS_JSON:
            sa_email = json.loads(GOOGLE_CREDENTIALS_JSON).get("client_email")
    except Exception:
        sa_email = None
    return jsonify({
        "status": "ok",
        "whatsapp": WHATSAPP_ENABLED,
        "google": GOOGLE_ENABLED,
        "openai": bool(client),
        "sheets_id_suffix": (SHEETS_ID_LEADS[-6:] if SHEETS_ID_LEADS else None),
        "sheets_title_expected": SHEETS_TITLE_LEADS,
        "service_account_email": sa_email,
        "google_reason": GOOGLE_REASON,
        "waba_phone_id_suffix": (WABA_PHONE_ID[-6:] if WABA_PHONE_ID else None),
        "openai_model": OPENAI_MODEL_RAG,
    }), 200


@app.route("/ext/test-send", methods=["POST"])
def ext_test_send():
    body = request.get_json(silent=True) or {}
    to = body.get("to")
    text = body.get("text", "Prueba Vicky")
    if not to:
        return jsonify({"error": "missing 'to' in body"}), 400
    ok, resp = send_text(to, text)
    status = 200 if ok else 500
    return jsonify({"ok": ok, "resp": resp}), status


@app.route("/ext/send-promo", methods=["POST"])
def ext_send_promo():
    job = request.get_json(silent=True)
    if not job:
        return jsonify({"error": "JSON body expected"}), 400
    recipients = job.get("recipients") or []
    if not isinstance(recipients, list) or not recipients:
        return jsonify({"error": "recipients must be list of msisdn"}), 400
    if len(recipients) > 1000:
        return jsonify({"error": "Max 1000 recipients per job"}), 400
    mode = job.get("mode")
    if mode not in ("text", "template", "image"):
        return jsonify({"error": "mode invalid. 'text'|'template'|'image'"}), 400
    if mode == "text" and not job.get("text"):
        return jsonify({"error": "text required for mode=text"}), 400
    if mode == "template" and not job.get("template"):
        return jsonify({"error": "template required for mode=template"}), 400
    if mode == "image" and not job.get("image"):
        return jsonify({"error": "image required for mode=image"}), 400
    recipients_norm = [normalize_msisdn(r) for r in recipients][:1000]
    promo_job = {"campaign": job.get("campaign"), "recipients": recipients_norm, "mode": mode, "text": job.get("text"), "template": job.get("template"), "image": job.get("image")}
    promo_queue.put(promo_job)
    logger.info("Promo job queued campaign=%s size=%d", job.get("campaign"), len(recipients_norm))
    return jsonify({"queued": True, "batch_size": len(recipients_norm)}), 202


@app.route("/ext/manuales", methods=["GET"])
def ext_manuales():
    status = {}
    for d in ("auto", "imss"):
        cached = rag_cache.get(d)
        status[d] = {
            "loaded": bool(cached),
            "name": cached.get("name") if cached else None,
            "chars": len(cached.get("text")) if cached else 0,
            "loaded_at": datetime.fromtimestamp(cached.get("loaded_at")).isoformat() if cached else None
        }
    return jsonify(status), 200


# Background initial load of manuals (non-blocking)
def background_initial_load():
    for d in ("auto", "imss"):
        try:
            load_manual_to_cache(d)
        except Exception:
            logger.debug("Could not load manual %s", d)


bg_thread = threading.Thread(target=background_initial_load, daemon=True)
bg_thread.start()

# -------------------------
# Run (only if executed directly)
# -------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info("Starting Flask on port %d", port)
    app.run(host="0.0.0.0", port=port, debug=False)


