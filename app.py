#!/usr/bin/env python3
from __future__ import annotations
import os
import re
import io
import json
import time
import math
import logging
import threading
import requests
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from collections import Counter, defaultdict
from functools import wraps

# Google APIs
try:
    from google.oauth2.service_account import Credentials as ServiceAccountCredentials
    from googleapiclient.discovery import build as gbuild
    from googleapiclient.http import MediaIoBaseUpload
except Exception:
    ServiceAccountCredentials = None
    gbuild = None
    MediaIoBaseUpload = None

# PDF parsing
try:
    import PyPDF2
except Exception:
    PyPDF2 = None

# OpenAI
try:
    import openai
except Exception:
    openai = None

from flask import Flask, request, jsonify

# -----------------------
# Config / Env
# -----------------------
def _get(key: str, default: str = "") -> str:
    return (os.getenv(key, default) or "").strip()

META_TOKEN = _get("META_TOKEN")
VERIFY_TOKEN = _get("VERIFY_TOKEN")
WABA_PHONE_ID = _get("WABA_PHONE_ID")
WA_API_VERSION = _get("WA_API_VERSION", "v17.0")
ADVISOR_WHATSAPP = _get("ADVISOR_WHATSAPP")
OPENAI_API_KEY = _get("OPENAI_API_KEY")
GOOGLE_CREDENTIALS_JSON = _get("GOOGLE_CREDENTIALS_JSON")
GSHEET_PROSPECTS_ID = _get("GSHEET_PROSPECTS_ID")
SHEET_TITLE_SECOM = _get("SHEET_TITLE_SECOM")
DRIVE_FOLDER_ID = _get("DRIVE_FOLDER_ID")
ID_MANUAL_IMSS = _get("ID_MANUAL_IMSS")

PORT = int(_get("PORT", "5000"))

if openai and OPENAI_API_KEY:
    openai.api_key = OPENAI_API_KEY

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("vicky-secom")

app = Flask(__name__)

# -----------------------
# Utilities
# -----------------------
def _normalize_phone(phone: str) -> str:
    if not phone:
        return ""
    digits = re.sub(r"\D", "", phone)
    return digits[-10:] if len(digits) >= 10 else digits

def _short_key(phone: str, n: int = 4) -> str:
    norm = _normalize_phone(phone)
    return norm[-n:] if norm else "xxxx"

def safe_json(o: Any) -> str:
    try:
        return json.dumps(o, default=str, ensure_ascii=False)
    except Exception:
        return str(o)

# -----------------------
# Rate limit: 60s per recipient
# -----------------------
_last_sent: Dict[str, datetime] = {}
RATE_LIMIT_SECONDS = 60

def rate_limited(phone: str) -> bool:
    key = _normalize_phone(phone)
    now = datetime.utcnow()
    last = _last_sent.get(key)
    if last and (now - last).total_seconds() < RATE_LIMIT_SECONDS:
        return True
    _last_sent[key] = now
    return False

# -----------------------
# WhatsApp Cloud API helpers
# -----------------------
if WABA_PHONE_ID and WA_API_VERSION:
    WPP_API_URL = f"https://graph.facebook.com/{WA_API_VERSION}/{WABA_PHONE_ID}/messages"
else:
    WPP_API_URL = None

def _headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {META_TOKEN}" if META_TOKEN else "",
        "Content-Type": "application/json",
        "User-Agent": "VickyBot-SECOM/1.0"
    }

def _should_retry(status: int) -> bool:
    return status == 429 or 500 <= status < 600

def _backoff(attempt: int) -> None:
    time.sleep(min(60, 2 ** attempt))

def send_message(to: str, text: str) -> bool:
    if not (META_TOKEN and WPP_API_URL):
        log.error("WhatsApp API not configured")
        return False
    if rate_limited(to):
        log.warning("Rate limited send_message to %s", to)
        return False
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text[:4096]},
    }
    for attempt in range(3):
        try:
            r = requests.post(WPP_API_URL, headers=_headers(), json=payload, timeout=15)
            if r.status_code in (200, 201):
                log.info("Sent text to %s", to)
                return True
            log.warning("send_message error %s %s", r.status_code, r.text[:400])
            if _should_retry(r.status_code) and attempt < 2:
                _backoff(attempt)
                continue
            return False
        except Exception:
            log.exception("Exception in send_message")
            if attempt < 2:
                _backoff(attempt)
                continue
            return False
    return False

def send_template_message(to: str, template_name: str, components: List[Dict[str, Any]]) -> bool:
    if not (META_TOKEN and WPP_API_URL):
        log.error("WhatsApp API not configured for templates")
        return False
    if rate_limited(to):
        log.warning("Rate limited send_template_message to %s", to)
        return False
    if not components:
        components = [{"type": "body", "parameters": []}]
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": "es_MX"},
            "components": components,
        },
    }
    for attempt in range(3):
        try:
            r = requests.post(WPP_API_URL, headers=_headers(), json=payload, timeout=15)
            if r.status_code in (200, 201):
                log.info("Sent template %s to %s", template_name, to)
                return True
            log.warning("send_template_message error %s %s", r.status_code, r.text[:400])
            if _should_retry(r.status_code) and attempt < 2:
                _backoff(attempt)
                continue
            return False
        except Exception:
            log.exception("Exception in send_template_message")
            if attempt < 2:
                _backoff(attempt)
                continue
            return False
    return False

# -----------------------
# Google Sheets + Drive init
# -----------------------
sheets = None
drive = None
google_ready = False
if GOOGLE_CREDENTIALS_JSON and ServiceAccountCredentials and gbuild:
    try:
        info = json.loads(GOOGLE_CREDENTIALS_JSON)
        creds = ServiceAccountCredentials.from_service_account_info(
            info,
            scopes=[
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive",
            ],
        )
        sheets = gbuild("sheets", "v4", credentials=creds)
        drive = gbuild("drive", "v3", credentials=creds)
        google_ready = True
        log.info("Google Sheets and Drive configured")
    except Exception:
        log.exception("Error initializing Google APIs")

# -----------------------
# Sheets helpers
# -----------------------
def _col_letter(col: int) -> str:
    res = ""
    while col > 0:
        col, rem = divmod(col - 1, 26)
        res = chr(65 + rem) + res
    return res

def _find_col(headers: List[str], candidates: List[str]) -> Optional[int]:
    if not headers:
        return None
    low = [h.strip().lower() for h in headers]
    for name in candidates:
        n = name.strip().lower()
        if n in low:
            return low.index(n)
    return None

def _get_sheet_headers_and_rows() -> Tuple[List[str], List[List[str]]]:
    if not (google_ready and sheets and GSHEET_PROSPECTS_ID and SHEET_TITLE_SECOM):
        return [], []
    rng = f"{SHEET_TITLE_SECOM}!A:Z"
    try:
        res = sheets.spreadsheets().values().get(spreadsheetId=GSHEET_PROSPECTS_ID, range=rng).execute()
        rows = res.get("values", [])
        if not rows:
            return [], []
        headers = rows[0]
        data_rows = rows[1:]
        return headers, data_rows
    except Exception:
        log.exception("Error reading sheet")
        return [], []

def _batch_update_cells(row_index: int, updates: Dict[str, str], headers: List[str]) -> None:
    if not (google_ready and sheets and GSHEET_PROSPECTS_ID and SHEET_TITLE_SECOM):
        return
    if row_index < 2:
        return
    header_low = [h.strip().lower() for h in headers]
    data_ranges = []
    for key, value in updates.items():
        key_low = key.strip().lower()
        if key_low in header_low:
            idx = header_low.index(key_low) + 1
        else:
            continue
        col_letter = _col_letter(idx)
        cell_range = f"{SHEET_TITLE_SECOM}!{col_letter}{row_index}"
        data_ranges.append({"range": cell_range, "values": [[str(value)]]})
    if not data_ranges:
        return
    body = {"valueInputOption": "RAW", "data": data_ranges}
    try:
        sheets.spreadsheets().values().batchUpdate(spreadsheetId=GSHEET_PROSPECTS_ID, body=body).execute()
    except Exception:
        log.exception("Error in _batch_update_cells")

def match_client_in_sheets(phone: str) -> Optional[Dict[str, Any]]:
    if not (google_ready and sheets and GSHEET_PROSPECTS_ID and SHEET_TITLE_SECOM):
        return None
    try:
        headers, rows = _get_sheet_headers_and_rows()
        if not headers or not rows:
            return None
        idx_name = _find_col(headers, ["Nombre", "CLIENTE", "Cliente"])
        idx_wa = _find_col(headers, ["WhatsApp", "Whatsapp", "WHATSAPP", "Telefono", "Teléfono", "CELULAR"])
        if idx_wa is None:
            return None
        target = _normalize_phone(phone)
        for row in rows:
            if len(row) <= idx_wa:
                continue
            tel = _normalize_phone(row[idx_wa])
            if tel == target:
                nombre = row[idx_name] if idx_name is not None and len(row) > idx_name else ""
                return {"nombre": nombre}
        return None
    except Exception:
        log.exception("Error in match_client_in_sheets")
        return None

def _touch_last_inbound(phone: str) -> None:
    if not (google_ready and sheets and GSHEET_PROSPECTS_ID and SHEET_TITLE_SECOM):
        return
    try:
        headers, rows = _get_sheet_headers_and_rows()
        if not headers or not rows:
            return
        idx_wa = _find_col(headers, ["WhatsApp", "Whatsapp", "WHATSAPP", "Telefono", "Teléfono", "CELULAR"])
        idx_last = _find_col(headers, ["LastInboundAt", "LAST_INBOUND_AT", "LAST_MESSAGE_AT"])
        if idx_wa is None or idx_last is None:
            return
        target = _normalize_phone(phone)
        for offset, row in enumerate(rows, start=2):
            if len(row) <= idx_wa:
                continue
            if _normalize_phone(row[idx_wa]) == target:
                col_letter = _col_letter(idx_last + 1)
                cell_range = f"{SHEET_TITLE_SECOM}!{col_letter}{offset}"
                body = {"range": cell_range, "majorDimension": "ROWS", "values": [[datetime.utcnow().isoformat()]]}
                sheets.spreadsheets().values().update(spreadsheetId=GSHEET_PROSPECTS_ID, range=cell_range, valueInputOption="RAW", body=body).execute()
                break
    except Exception:
        log.exception("Error registering LastInboundAt")

# -----------------------
# Drive helpers: folder per client, upload
# -----------------------
def _drive_search_folder(name: str, parent_id: str) -> Optional[str]:
    if not (google_ready and drive):
        return None
    try:
        # Build a safe query string without backslashes inside f-string expressions
        safe_name = name.replace("'", "\\'")
        q = "name = '{}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false and '{}' in parents".format(safe_name, parent_id)
        res = drive.files().list(q=q, spaces='drive', fields='files(id,name)', pageSize=10).execute()
        files = res.get("files", [])
        if files:
            return files[0].get("id")
    except Exception:
        log.exception("Error searching folder in Drive")
    return None

def _drive_create_folder(name: str, parent_id: str) -> Optional[str]:
    if not (google_ready and drive):
        return None
    try:
        metadata = {'name': name, 'mimeType': 'application/vnd.google-apps.folder', 'parents': [parent_id]}
        f = drive.files().create(body=metadata, fields='id').execute()
        return f.get('id')
    except Exception:
        log.exception("Error creating folder in Drive")
        return None

def _drive_upload_bytes(filename: str, content: bytes, mime_type: str, parent_id: str) -> Optional[str]:
    if not (google_ready and drive and MediaIoBaseUpload):
        return None
    try:
        fh = io.BytesIO(content)
        media = MediaIoBaseUpload(fh, mimetype=mime_type, resumable=False)
        metadata = {'name': filename, 'parents': [parent_id]}
        file = drive.files().create(body=metadata, media_body=media, fields='id,webViewLink').execute()
        return file.get('id')
    except Exception:
        log.exception("Error uploading file to Drive")
        return None

def ensure_client_folder(phone: str) -> Optional[str]:
    if not DRIVE_FOLDER_ID:
        return None
    short = _short_key(phone, 4)
    folder_name = f"{short}_Cliente"
    try:
        fid = _drive_search_folder(folder_name, DRIVE_FOLDER_ID)
        if fid:
            return fid
        return _drive_create_folder(folder_name, DRIVE_FOLDER_ID)
    except Exception:
        log.exception("Error ensuring client folder")
        return None

# -----------------------
# Media download from WhatsApp Cloud API
# -----------------------
def download_media_from_whatsapp(media_id: str) -> Tuple[Optional[bytes], Optional[str], Optional[str]]:
    """
    Returns (content_bytes, mime_type, filename)
    """
    if not (META_TOKEN and WA_API_VERSION):
        log.error("WhatsApp API not configured for media download")
        return None, None, None
    try:
        # Get media URL + mime
        meta_url = f"https://graph.facebook.com/{WA_API_VERSION}/{media_id}"
        params = {"fields": "url,filename,mime_type"}
        r = requests.get(meta_url, headers={"Authorization": f"Bearer {META_TOKEN}"}, params=params, timeout=15)
        if r.status_code != 200:
            log.warning("Media meta fetch failed %s %s", r.status_code, r.text[:400])
            return None, None, None
        meta = r.json()
        url = meta.get("url")
        mime = meta.get("mime_type") or meta.get("mimeType") or "application/octet-stream"
        filename = meta.get("filename") or f"media_{media_id}"
        if not url:
            log.warning("No media url returned for %s", media_id)
            return None, None, None
        # Download binary
        r2 = requests.get(url, headers={"Authorization": f"Bearer {META_TOKEN}"}, stream=True, timeout=30)
        if r2.status_code != 200:
            log.warning("Media download failed %s", r2.status_code)
            return None, None, None
        content = r2.content
        return content, mime, filename
    except Exception:
        log.exception("Error downloading media from WhatsApp")
        return None, None, None

# -----------------------
# Minimal BM25-like retriever for PDF manual (RAG)
# -----------------------
def _extract_text_from_pdf_bytes(content: bytes) -> str:
    if PyPDF2 is None:
        # Fallback: return empty
        log.warning("PyPDF2 not available, cannot parse PDF")
        return ""
    try:
        reader = PyPDF2.PdfReader(io.BytesIO(content))
        texts = []
        for p in reader.pages:
            try:
                texts.append(p.extract_text() or "")
            except Exception:
                continue
        return "\n".join(texts)
    except Exception:
        log.exception("Error parsing PDF bytes")
        return ""

def _chunk_text(text: str, chunk_size: int = 400, overlap: int = 50) -> List[str]:
    words = text.split()
    chunks = []
    i = 0
    while i < len(words):
        chunk = words[i:i+chunk_size]
        chunks.append(" ".join(chunk))
        i += chunk_size - overlap
    return chunks

def _bm25_score(query: str, docs: List[str]) -> List[Tuple[int, float]]:
    # Simplified BM25-like scoring
    k1 = 1.5
    b = 0.75
    docs_tokens = [d.split() for d in docs]
    N = len(docs)
    avgdl = sum(len(t) for t in docs_tokens) / max(1, N)
    df = Counter()
    for tokens in docs_tokens:
        df.update(set(tokens))
    scores = []
    q_terms = query.split()
    for idx, tokens in enumerate(docs_tokens):
        score = 0.0
        freqs = Counter(tokens)
        dl = len(tokens)
        for term in q_terms:
            if not term:
                continue
            f = freqs.get(term, 0)
            n_q = df.get(term, 0)
            if n_q == 0:
                continue
            idf = math.log((N - n_q + 0.5) / (n_q + 0.5) + 1)
            denom = f + k1 * (1 - b + b * (dl / avgdl))
            score += idf * ((f * (k1 + 1)) / (denom + 1e-9))
        scores.append((idx, score))
    scores.sort(key=lambda x: x[1], reverse=True)
    return scores

def retrieve_manual_passages(query: str, top_k: int = 3) -> List[str]:
    if not (google_ready and drive and ID_MANUAL_IMSS):
        return []
    try:
        # Download file bytes from Drive
        request_media = drive.files().get_media(fileId=ID_MANUAL_IMSS)
        fh = io.BytesIO()
        downloader = None
        try:
            # googleapiclient's MediaIoBaseDownload may be present
            from googleapiclient.http import MediaIoBaseDownload
            downloader = MediaIoBaseDownload(fh, request_media)
            done = False
            while not done:
                status, done = downloader.next_chunk()
        except Exception:
            # fallback to execute -> get directly
            fh = io.BytesIO(request_media.execute())
        content = fh.getvalue()
        text = _extract_text_from_pdf_bytes(content)
        if not text:
            return []
        chunks = _chunk_text(text, chunk_size=350, overlap=50)
        scores = _bm25_score(query.lower(), [c.lower() for c in chunks])
        top = [chunks[idx] for idx, _ in scores[:top_k]]
        return top
    except Exception:
        log.exception("Error retrieving manual IMSS from Drive")
        return []

# -----------------------
# GPT Engine / Intent
# -----------------------
def interpret_response(text: str) -> str:
    if not text:
        return "neutral"
    t = text.lower()
    pos = ["sí", "si", "claro", "ok", "de acuerdo", "vale", "afirmativo", "correcto", "s"]
    neg = ["no", "nel", "nop", "negativo", "no quiero", "no gracias", "no interesa", "n"]
    if any(p in t for p in pos):
        return "positive"
    if any(n in t for n in neg):
        return "negative"
    return "neutral"

def extract_number(text: str) -> Optional[float]:
    if not text:
        return None
    clean = text.replace(",", "").replace("$", "")
    m = re.search(r"(\d+(\.\d+)?)", clean)
    try:
        return float(m.group(1)) if m else None
    except Exception:
        return None

def call_gpt_system_prompt(user_prompt: str, system_prompt: str = "Eres un asistente en español.") -> str:
    if not openai or not OPENAI_API_KEY:
        log.warning("OpenAI not configured")
        return ""
    try:
        # Use chat completion
        resp = openai.ChatCompletion.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.2,
            max_tokens=800,
        )
        # Newer API returns choices with message
        out = ""
        if resp and resp.get("choices"):
            choice = resp["choices"][0]
            if "message" in choice:
                out = choice["message"].get("content", "")
            else:
                out = choice.get("text", "")
        return out.strip()
    except Exception:
        log.exception("Error calling OpenAI")
        return ""

# Cognitive processor: intent + pipeline
def process_text_message(phone: str, text: str, match: Optional[Dict[str, Any]]) -> None:
    txt = (text or "").strip()
    st = interpret_response(txt)
    norm_phone = _normalize_phone(phone)
    # Commands
    t = txt.lower()
    if t.startswith("sgpt:") and openai:
        prompt = txt.split("sgpt:", 1)[1].strip()
        if prompt:
            ans = call_gpt_system_prompt(prompt, system_prompt="Eres un asistente conversacional en español.")
            if ans:
                send_message(phone, ans)
            else:
                send_message(phone, "Lo siento, hubo un problema procesando tu petición.")
        return
    # Read manual IMSS via RAG
    if "manual imss" in t or "ley 73 manual" in t or ("imss" in t and "manual" in t):
        passages = retrieve_manual_passages(txt, top_k=3)
        if passages:
            context = "\n\n".join(passages)
            prompt = f"Contexto extraído del manual IMSS:\n\n{context}\n\nPregunta: {txt}\nResponde de forma breve y clara en español."
            ans = call_gpt_system_prompt(prompt, system_prompt="Eres un asistente experto en IMSS.")
            if ans:
                send_message(phone, ans)
            else:
                send_message(phone, "No pude obtener una respuesta del manual en este momento.")
        else:
            send_message(phone, "No pude acceder al manual IMSS ahora. Intenta más tarde.")
        return
    # Intentual flows similar to the app: quick routing
    if t in ("1", "imss", "ley 73", "prestamo imss", "préstamo imss", "pension", "pensión"):
        send_message(phone, "🟩 *Préstamo IMSS Ley 73*\n¿Te interesa que revisemos si calificas? Responde sí o no.")
        return
    if t in ("2", "auto", "seguro auto", "seguro de auto"):
        send_message(phone, "🚗 *Seguro de Auto*\nEnvíame tu INE y tarjeta de circulación (foto) y te ayudo con la cotización.")
        return
    if t in ("menu", "menú", "inicio", "hola"):
        send_message(phone, "Menú principal:\n1) Préstamo IMSS\n2) Seguro de Auto\n3) Vida/Salud\nEscribe el número o la opción.")
        return
    # Default: if matched in sheets, be personalized
    name = match.get("nombre") if match else None
    if name:
        send_message(phone, f"Hola {name}, recibí tu mensaje: {txt[:240]}. ¿En qué te ayudo?")
    else:
        send_message(phone, f"Hola, recibí tu mensaje: {txt[:240]}. ¿En qué te puedo ayudar?")
    # Optionally notify advisor
    if ADVISOR_WHATSAPP:
        try:
            send_message(ADVISOR_WHATSAPP, f"Mensaje de {phone}: {txt[:400]}")
        except Exception:
            log.exception("Error notifying advisor")

# -----------------------
# Media processor: save to Drive and notify
# -----------------------
def process_media_message(phone: str, mtype: str, msg: Dict[str, Any]) -> None:
    media = msg.get(mtype) or {}
    media_id = media.get("id")
    if not media_id:
        log.warning("No media id for message from %s", phone)
        return
    content, mime, filename = download_media_from_whatsapp(media_id)
    if not content:
        log.warning("Failed to download media %s", media_id)
        return
    client_folder = ensure_client_folder(phone) or DRIVE_FOLDER_ID
    if not client_folder:
        log.warning("No Drive folder to upload media")
        return
    # Ensure filename has safe extension
    if not filename:
        ext = ""
        if "/" in mime:
            ext = "." + mime.split("/")[-1]
        filename = f"{mtype}_{media_id}{ext}"
    # Upload
    fid = _drive_upload_bytes(filename, content, mime or "application/octet-stream", client_folder)
    if fid:
        log.info("Uploaded media to Drive id=%s for phone=%s", fid, phone)
        if ADVISOR_WHATSAPP:
            send_message(ADVISOR_WHATSAPP, f"Documento recibido de {phone} subido a Drive en carpeta {_short_key(phone,4)}_Cliente")
    else:
        log.warning("Failed uploading media to Drive for %s", phone)

# -----------------------
# Webhook processing: background worker per event to return 200 quickly
# -----------------------
def _process_whatsapp_event(payload: Dict[str, Any]) -> None:
    try:
        entry = (payload.get("entry") or [{}])[0]
        changes = (entry.get("changes") or [{}])[0]
        value = changes.get("value", {})
        messages = value.get("messages", [])
        if not messages:
            log.info("No messages in webhook")
            return
        msg = messages[0]
        phone = msg.get("from")
        if not phone:
            log.info("No phone in message")
            return
        log.info("Processing message from %s: type=%s", phone, msg.get("type"))
        # Touch sheets
        _touch_last_inbound(phone)
        match = match_client_in_sheets(phone)
        mtype = msg.get("type")
        # If text
        if mtype == "text":
            text = msg.get("text", {}).get("body", "")
            log.info("Text from %s: %s", phone, text[:300])
            process_text_message(phone, text, match)
            return
        # If media
        if mtype in ("image", "document", "audio", "video", "sticker"):
            log.info("Media from %s type=%s", phone, mtype)
            # Acknowledge quickly
            send_message(phone, "✅ Archivo recibido. Lo guardo y lo reviso en un momento.")
            # Process media: download and upload to Drive
            try:
                process_media_message(phone, mtype, msg)
            except Exception:
                log.exception("Error processing media message")
            return
        # Other types: statuses, contacts, etc.
        log.info("Unhandled message type: %s", mtype)
    except Exception:
        log.exception("Error in _process_whatsapp_event")

# -----------------------
# Flask endpoints (single /webhook GET & POST) and ext/health
# -----------------------
@app.get("/webhook")
def webhook_verify():
    try:
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge", "")
        if mode == "subscribe" and token == VERIFY_TOKEN:
            log.info("Webhook verified")
            return challenge, 200
        log.warning("Webhook verification failed")
        return "forbidden", 403
    except Exception:
        log.exception("Error in webhook_verify")
        return "forbidden", 403

@app.post("/webhook")
def webhook_receive():
    try:
        payload = request.get_json(force=True, silent=True) or {}
        log.info("Webhook received: %s", safe_json(payload)[:800])
        # Kick off processing in background and return 200 immediately
        t = threading.Thread(target=_process_whatsapp_event, args=(payload,), daemon=True)
        t.start()
        return jsonify({"status": "ok"}), 200
    except Exception:
        log.exception("Error in webhook_receive")
        # Always respond ok per requirements
        return jsonify({"status": "ok"}), 200

@app.get("/ext/health")
def ext_health():
    try:
        return jsonify({
            "status": "ok",
            "service": "Vicky Bot SECOM",
            "timestamp": datetime.utcnow().isoformat(),
            "whatsapp_configured": bool(META_TOKEN and WABA_PHONE_ID and WA_API_VERSION),
            "google_ready": google_ready,
            "openai_ready": bool(openai and OPENAI_API_KEY),
        }), 200
    except Exception:
        log.exception("Error in ext_health")
        return jsonify({"status": "ok"}), 200

# -----------------------
# Application entrypoint
# -----------------------
if __name__ == "__main__":
    log.info("Starting Vicky Bot SECOM on port %s", PORT)
    log.info("WhatsApp configured: %s", bool(META_TOKEN and WABA_PHONE_ID and WA_API_VERSION))
    log.info("Google ready: %s", google_ready)
    log.info("OpenAI ready: %s", bool(openai and OPENAI_API_KEY))
    # Note: Render uses gunicorn; this allows running locally via python app.py
    app.run(host="0.0.0.0", port=PORT, debug=False)
