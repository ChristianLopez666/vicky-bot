#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Vicky SECOM – WAPI (robusto) ✅
Archivo listo para ejecutar con Gunicorn:
  gunicorn app:app --bind 0.0.0.0:$PORT

Objetivo
- Mantener el MENÚ completo (Auto, IMSS, Vida/Salud, VRIM, Préstamos IMSS, Crédito Empresarial, Contacto, Docs Auto, Promos).
- Conectar a OpenAI (RAG) para preguntas abiertas (dominios: auto, imss) usando manuales en Drive si están disponibles.
- Conectar opcionalmente a Google Sheets (solo lectura) para reconocer clientes por últimos 10 dígitos.
- Resiliencia: si Sheets/Drive/OpenAI fallan o no están, Vicky funciona con menú y flujos; registra logs claros.
- No crear/editar filas en Sheets (solo lectura).

Variables de entorno
- OBLIGATORIAS para WhatsApp: META_TOKEN, WABA_PHONE_ID, VERIFY_TOKEN, ADVISOR_NUMBER
- Opcionales (recomendadas):
  * OPENAI_API_KEY
  * GOOGLE_CREDENTIALS_JSON (service account en una sola línea)
  * SHEETS_ID_LEADS  (ej. Prospectos SECOM Auto)
  * SHEETS_TITLE_LEADS (ej. Hoja1)
  * RAG_AUTO_FILE_ID  | RAG_AUTO_FILE_NAME (uno de los dos)
  * RAG_IMSS_FILE_ID  | RAG_IMSS_FILE_NAME (uno de los dos)
  * PORT (Render)
"""

from __future__ import annotations
import os, io, re, json, time, logging, traceback, mimetypes, unicodedata
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, List, Tuple
from functools import wraps

import requests
from flask import Flask, request, jsonify, g

# OpenAI 1.x (opcional)
try:
    from openai import OpenAI
    OPENAI_LIB = True
except Exception:
    OPENAI_LIB = False

# PDF
try:
    from PyPDF2 import PdfReader
    PDF_LIB = True
except Exception:
    PDF_LIB = False

# Google (opcional)
GOOGLE_LIBS_AVAILABLE = True
try:
    import gspread
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload
except Exception:
    GOOGLE_LIBS_AVAILABLE = False

# ----------------------------------------------------------------------------
# LOG
# ----------------------------------------------------------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [%(request_id)s] %(message)s"
)
logger = logging.getLogger("vicky-wapi")

class RequestIdFilter(logging.Filter):
    def filter(self, record):
        try:
            from flask import g as _g
            record.request_id = getattr(_g, "request_id", "no-rid")
        except Exception:
            record.request_id = "no-rid"
        return True

logger.addFilter(RequestIdFilter())

# ----------------------------------------------------------------------------
# TZ
# ----------------------------------------------------------------------------
try:
    from zoneinfo import ZoneInfo
    FLASK_TZ = ZoneInfo("America/Mazatlan")
except Exception:
    from datetime import timezone as _tz
    FLASK_TZ = _tz.utc

def now_iso() -> str:
    try:
        return datetime.now(FLASK_TZ).isoformat(timespec="seconds")
    except Exception:
        return datetime.utcnow().isoformat(timespec="seconds")

# ----------------------------------------------------------------------------
# ENV
# ----------------------------------------------------------------------------
env = os.environ.copy()
META_TOKEN = env.get("META_TOKEN")
WABA_PHONE_ID = env.get("WABA_PHONE_ID")
VERIFY_TOKEN = env.get("VERIFY_TOKEN")
ADVISOR_NUMBER = env.get("ADVISOR_NUMBER")

OPENAI_API_KEY = env.get("OPENAI_API_KEY")
OPENAI_MODEL_RAG = env.get("OPENAI_MODEL_RAG", "gpt-4o-mini")

GOOGLE_CREDENTIALS_JSON = env.get("GOOGLE_CREDENTIALS_JSON")
SHEETS_ID_LEADS = env.get("SHEETS_ID_LEADS")
SHEETS_TITLE_LEADS = env.get("SHEETS_TITLE_LEADS", "Hoja1")

RAG_AUTO_FILE_ID = env.get("RAG_AUTO_FILE_ID")
RAG_AUTO_FILE_NAME = env.get("RAG_AUTO_FILE_NAME")
RAG_IMSS_FILE_ID = env.get("RAG_IMSS_FILE_ID")
RAG_IMSS_FILE_NAME = env.get("RAG_IMSS_FILE_NAME")

WHATSAPP_ENABLED = bool(META_TOKEN and WABA_PHONE_ID and VERIFY_TOKEN and ADVISOR_NUMBER)
OPENAI_ENABLED = bool(OPENAI_LIB and OPENAI_API_KEY)
GOOGLE_ENABLED = bool(GOOGLE_LIBS_AVAILABLE and GOOGLE_CREDENTIALS_JSON)

def mask_phone(p: Optional[str]) -> str:
    d = re.sub(r"\\D", "", p or "")
    return "*" * max(0, len(d)-4) + d[-4:]

logger.info("Boot flags -> whatsapp=%s openai=%s google=%s", WHATSAPP_ENABLED, OPENAI_ENABLED, GOOGLE_ENABLED)

# ----------------------------------------------------------------------------
# HTTP/Flask
# ----------------------------------------------------------------------------
app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False

@app.before_request
def _rid():
    g.request_id = request.headers.get("X-Request-Id") or f"{int(time.time()*1000)}"

# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
session = requests.Session()
if META_TOKEN:
    session.headers.update({"Authorization": f"Bearer {META_TOKEN}"})

def normalize_msisdn(s: str) -> str:
    d = re.sub(r"\\D", "", s or "")
    if d.startswith("521") and len(d) == 13:
        return d
    if len(d) == 10:
        return "521" + d
    if len(d) > 10:
        return "521" + d[-10:]
    return d

def last10(msisdn: str) -> str:
    return re.sub(r"\\D", "", msisdn or "")[-10:]

def norm(s: str) -> str:
    s = (s or "").lower()
    s = unicodedata.normalize("NFD", s)
    return "".join(c for c in s if unicodedata.category(c) != "Mn")

# ----------------------------------------------------------------------------
# WhatsApp senders
# ----------------------------------------------------------------------------
WHATSAPP_API_BASE = f"https://graph.facebook.com/v17.0/{WABA_PHONE_ID}/messages" if WABA_PHONE_ID else None
QPS_LIMIT = 5
_last_send = 0.0

def _pace():
    global _last_send
    now = time.time()
    min_int = 1.0 / QPS_LIMIT
    if now - _last_send < min_int:
        time.sleep(min_int - (now - _last_send))
    _last_send = time.time()

def _wp_post(payload: dict) -> dict:
    if not WHATSAPP_ENABLED or not WHATSAPP_API_BASE:
        raise RuntimeError("WhatsApp no configurado")
    r = session.post(WHATSAPP_API_BASE, json=payload, timeout=30)
    if r.status_code not in (200, 201):
        logger.warning("WhatsApp %s: %s", r.status_code, r.text)
        r.raise_for_status()
    return r.json()

def send_text(to: str, text: str) -> None:
    to = normalize_msisdn(to)
    payload = {"messaging_product":"whatsapp","to":to,"type":"text","text":{"body":text}}
    try:
        _pace()
        resp = _wp_post(payload)
        mid = resp.get("messages",[{}])[0].get("id")
        logger.info("Texto -> %s mid=%s", mask_phone(to), mid)
    except Exception as e:
        logger.error("send_text error -> %s", e)

def send_template(to: str, name: str, language="es_MX", components: Optional[list]=None) -> None:
    to = normalize_msisdn(to)
    payload = {"messaging_product":"whatsapp","to":to,"type":"template","template":{"name":name,"language":{"code":language}}}
    if components:
        payload["template"]["components"] = components
    try:
        _pace()
        resp = _wp_post(payload)
        mid = resp.get("messages",[{}])[0].get("id")
        logger.info("Template -> %s mid=%s", mask_phone(to), mid)
    except Exception as e:
        logger.error("send_template error -> %s", e)

# ----------------------------------------------------------------------------
# Google: Sheets (read-only) & Drive (read-only)
# ----------------------------------------------------------------------------
sheets = None
drive  = None

def init_google():
    global sheets, drive, GOOGLE_ENABLED
    if not GOOGLE_ENABLED:
        return
    try:
        creds = Credentials.from_service_account_info(json.loads(GOOGLE_CREDENTIALS_JSON),
                                                      scopes=[
                                                          "https://www.googleapis.com/auth/spreadsheets.readonly",
                                                          "https://www.googleapis.com/auth/drive.readonly"
                                                      ])
        sheets = gspread.authorize(creds)
        drive = build("drive", "v3", credentials=creds, cache_discovery=False)
        logger.info("Google listo (read-only)")
    except Exception as e:
        GOOGLE_ENABLED = False
        logger.error("Google init error -> %s", e)

init_google()

def get_worksheet_safe():
    if not (GOOGLE_ENABLED and sheets and SHEETS_ID_LEADS and SHEETS_TITLE_LEADS):
        return None
    try:
        sh = sheets.open_by_key(SHEETS_ID_LEADS)
        return sh.worksheet(SHEETS_TITLE_LEADS)
    except Exception as e:
        # No rompas el flujo si 404 o permisos
        logger.error("Sheets 404/permiso (%s). Vicky seguirá sin reconocimiento.", e)
        return None

def find_row_by_last10(wa_last10: str) -> Optional[int]:
    ws = get_worksheet_safe()
    if not ws:
        return None
    try:
        records = ws.get_all_records()
        for i, r in enumerate(records, start=2):
            if str(r.get("wa_last10","")).endswith(wa_last10):
                return i
    except Exception as e:
        logger.error("find_row_by_last10 error -> %s", e)
    return None

def drive_find_pdf(file_id: Optional[str], file_name: Optional[str]) -> Optional[Dict[str,Any]]:
    if not drive:
        return None
    try:
        if file_id:
            f = drive.files().get(fileId=file_id, fields="id,name,mimeType").execute()
            if f and f.get("mimeType") == "application/pdf":
                return f
        if file_name:
            q = f"name = '{file_name}' and mimeType='application/pdf' and trashed=false"
            res = drive.files().list(q=q, pageSize=5, fields="files(id,name,mimeType)").execute()
            files = res.get("files", [])
            if files:
                return files[0]
    except Exception as e:
        logger.error("drive_find_pdf error -> %s", e)
    return None

def drive_download_bytes(file_id: str) -> Optional[bytes]:
    if not drive:
        return None
    try:
        req = drive.files().get_media(fileId=file_id)
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, req)
        done = False
        while not done:
            status, done = downloader.next_chunk()
        fh.seek(0)
        return fh.read()
    except Exception as e:
        logger.error("drive_download_bytes error -> %s", e)
        return None

def pdf_to_text(b: bytes) -> str:
    if not (b and PDF_LIB):
        return ""
    try:
        r = PdfReader(io.BytesIO(b))
        chunks = []
        for p in r.pages:
            try:
                chunks.append(p.extract_text() or "")
            except Exception:
                chunks.append("")
        return "\n".join(chunks).strip()
    except Exception as e:
        logger.error("pdf_to_text error -> %s", e)
        return ""

# ----------------------------------------------------------------------------
# RAG (manual cache + OpenAI)
# ----------------------------------------------------------------------------
client_oa: Optional[OpenAI] = None
if OPENAI_ENABLED:
    try:
        client_oa = OpenAI(api_key=OPENAI_API_KEY)
        logger.info("OpenAI listo")
    except Exception as e:
        logger.error("OpenAI init error -> %s", e)
        client_oa = None

rag_cache: Dict[str, Dict[str, Any]] = {}  # domain -> {text, loaded_at, name}
RAG_TTL = 6 * 3600

def load_manual(domain: str) -> bool:
    # domain in {"auto","imss"}
    file_id  = RAG_AUTO_FILE_ID  if domain == "auto" else RAG_IMSS_FILE_ID
    file_name= RAG_AUTO_FILE_NAME if domain == "auto" else RAG_IMSS_FILE_NAME
    if not drive:
        logger.info("Drive no disponible: RAG sin contexto de manual")
        return False
    found = drive_find_pdf(file_id, file_name)
    if not found:
        logger.info("Manual no encontrado (domain=%s). Usa nombre o id por env.", domain)
        return False
    b = drive_download_bytes(found["id"])
    if not b:
        return False
    text = pdf_to_text(b)
    if not text:
        return False
    rag_cache[domain] = {"text": text, "loaded_at": time.time(), "name": found.get("name")}
    logger.info("Manual cargado: %s (%s chars=%d)", domain, found.get("name"), len(text))
    return True

def answer_with_rag(query: str, domain_hint: Optional[str]) -> str:
    domain = "auto" if domain_hint == "auto" else ("imss" if domain_hint == "imss" else None)
    # Heurística si no hay hint
    if not domain:
        nt = norm(query)
        if any(w in nt for w in ("auto","amplia","cobertura","deducible","choque","rc","daños")):
            domain = "auto"
        elif any(w in nt for w in ("imss","pension","pensión","ley 73","modalidad","semanas")):
            domain = "imss"
        else:
            domain = "auto"  # por defecto
    # Cargar manual si necesario
    cached = rag_cache.get(domain)
    if not cached or (time.time() - cached.get("loaded_at", 0) > RAG_TTL):
        _ = load_manual(domain)
        cached = rag_cache.get(domain)

    context = (cached or {}).get("text","")
    mname   = (cached or {}).get("name", "manual")
    if not (client_oa and context):
        # Fallback sin contexto: respuesta genérica
        return "En este momento no puedo consultar el manual. ¿Te gustaría que te contacte un asesor?"
    # Rank simple por coincidencia de palabras
    words = set(re.findall(r"\\w{3,}", query.lower()))
    paras = [p.strip() for p in re.split(r"\\n{1,}", context) if p.strip()]
    scored = []
    for p in paras:
        w = set(re.findall(r"\\w{3,}", p.lower()))
        s = len(words & w)
        if s > 0:
            scored.append((s, p))
    scored.sort(key=lambda x: x[0], reverse=True)
    top = "\\n\\n".join([p for _,p in scored[:3]]) if scored else "\\n\\n".join(paras[:3])

    prompt = (
        f"Eres Vicky. Responde SOLO con la info del contexto.\\n"
        f"Contexto del manual '{mname}':\\n{top}\\n\\n"
        f"Pregunta del usuario: {query}\\n\\n"
        "Si no está en el contexto, responde exactamente: 'No encuentro esta información en el manual correspondiente.'\\n"
        "Responde breve, en español, claro y amable."
    )
    try:
        res = client_oa.chat.completions.create(
            model=OPENAI_MODEL_RAG,
            messages=[{"role":"user","content": prompt}],
            temperature=0.2,
            max_tokens=300
        )
        ans = (res.choices[0].message.content or "").strip()
        return ans
    except Exception as e:
        logger.error("OpenAI error -> %s", e)
        return "No puedo procesar la consulta ahora. Intenta más tarde."

# ----------------------------------------------------------------------------
# Estados & Router
# ----------------------------------------------------------------------------
user_state: Dict[str, Dict[str, Any]] = {}  # phone -> {flow, stage, last_domain}
def set_state(wa: str, **kw):
    s = user_state.setdefault(wa, {})
    s.update(kw)

def get_state(wa: str) -> Dict[str, Any]:
    return user_state.setdefault(wa, {})

def main_menu_text() -> str:
    return (
        "📋 *Menú principal*\n\n"
        "1) Asesoría en *Pensiones IMSS*\n"
        "2) *SEGURO DE AUTO* (Amplia Plus, Amplia, Limitada)\n"
        "3) *Seguros de Vida y Salud*\n"
        "4) *Tarjetas médicas VRIM*\n"
        "5) *Préstamos IMSS Ley 73* (mín. $40,000)\n"
        "6) *Crédito empresarial*\n"
        "7) *Contactar con Christian*\n"
        "8) *Documentos Auto* (enviar INE/PLACA)\n"
        "9) *Promociones*\n\n"
        "Escribe el número de opción o la palabra clave (AUTO, IMSS, CONTACTO)."
    )

def flow_show_menu(wa_id: str):
    send_text(wa_id, main_menu_text())
    set_state(wa_id, flow="MENU", stage="START")

def notify_advisor(wa_id: str, motivo: str, extra: Optional[str]=None):
    body = (
        "🔔 Nuevo evento en Vicky\n"
        f"Cliente: {wa_id}\n"
        f"Motivo: {motivo}\n"
        f"Notas: {extra or ''}"
    )
    send_text(ADVISOR_NUMBER, body)

def is_question_for_rag(txt_norm: str) -> bool:
    keys = ("cobertura","coberturas","deducible","incluye","diferencia","qué cubre","que cubre",
            "requisito","requisitos","pension","pensión","ley 73","monto","calculo","cálculo","semanas")
    return "?" in txt_norm or any(k in txt_norm for k in keys)

# ---- Flujos resumidos (no destructivos) ----
def flow_auto(wa_id: str, text: str):
    st = get_state(wa_id)
    stage = st.get("stage","START")
    tN = norm(text)
    set_state(wa_id, flow="AUTO", last_domain="auto")

    # RAG si pregunta abierta
    if is_question_for_rag(tN):
        ans = answer_with_rag(text, domain_hint="auto")
        send_text(wa_id, ans)
        return

    if stage == "START":
        send_text(wa_id,
                  "🚗 *Planes Auto SECOM*\n\n1) Amplia Plus\n2) Amplia\n3) Limitada\n\n"
                  "Responde 'INE: <datos>' o 'PLACA: <datos>' para continuar.\n"
                  "Escribe 'coberturas' o 'deducible' para detalles.")
        set_state(wa_id, stage="DOCS")
        return
    if stage == "DOCS":
        if tN.startswith("ine") or "ine:" in tN:
            send_text(wa_id, "Gracias. Ahora envía *PLACA* o 'tarjeta' si la tienes.")
            set_state(wa_id, stage="PLAN")
            return
        if "placa" in tN or "tarjeta" in tN:
            send_text(wa_id, "Perfecto. ¿Qué plan te interesa? Responde 1, 2 o 3.")
            set_state(wa_id, stage="PLAN")
            return
        send_text(wa_id, "No entendí. Responde 'INE: <datos>' o 'PLACA: <datos>'.")
        return
    if stage == "PLAN":
        if any(x in tN for x in ("1","amplia plus")):
            plan = "Amplia Plus"
        elif any(x in tN for x in ("2","amplia")):
            plan = "Amplia"
        elif any(x in tN for x in ("3","limitada")):
            plan = "Limitada"
        else:
            send_text(wa_id, "Selecciona 1, 2 o 3. O escribe 'asesor' para contacto humano.")
            return
        send_text(wa_id, f"Has elegido {plan}. ¿Deseas que un asesor te contacte? Escribe 'asesor'.")
        set_state(wa_id, stage="RESUMEN")
        return
    if stage == "RESUMEN":
        if "asesor" in tN:
            notify_advisor(wa_id, "Solicitud contacto AUTO")
            send_text(wa_id, "He notificado al asesor. Te contactará pronto.")
            set_state(wa_id, stage="DONE")
            return
        send_text(wa_id, "Gracias. Si necesitas más, escribe 'menu'.")
        set_state(wa_id, stage="DONE")
        return

def flow_imss(wa_id: str, text: str):
    st = get_state(wa_id)
    stage = st.get("stage","START")
    tN = norm(text)
    set_state(wa_id, flow="IMSS", last_domain="imss")

    # RAG para preguntas abiertas
    if is_question_for_rag(tN):
        ans = answer_with_rag(text, domain_hint="imss")
        send_text(wa_id, ans)
        return

    if stage == "START":
        send_text(wa_id,
            "🧓 *IMSS Ley 73*: la nómina en Inbursa brinda beneficios extra pero *NO es obligatoria*.\n"
            "Responde *requisitos*, *cálculo* o *préstamo*.")
        set_state(wa_id, stage="QUALIFY")
        return
    if stage == "QUALIFY":
        if "requisit" in tN:
            ans = answer_with_rag("requisitos de pensión ley 73", "imss")
            send_text(wa_id, ans)
            set_state(wa_id, stage="FOLLOW")
            return
        if "calculo" in tN or "cálculo" in tN or "monto" in tN:
            send_text(wa_id, "Para calcular necesito salario promedio, semanas y edad. ¿Quieres asesoría? Responde 'sí' o 'no'.")
            set_state(wa_id, stage="CALC")
            return
        if "prestamo" in tN or "préstamo" in tN or "ley 73" in tN:
            send_text(wa_id, "Préstamos Ley 73: hasta 12 meses de pensión. ¿Deseas que te contacte un asesor?")
            set_state(wa_id, stage="FOLLOW")
            return
        send_text(wa_id, "No entendí. Responde 'requisitos', 'cálculo' o 'préstamo'.")
        return
    if stage == "CALC":
        if "si" in tN or "sí" in tN:
            notify_advisor(wa_id, "Cálculo IMSS solicitado")
            send_text(wa_id, "Quedas *preautorizado* de forma tentativa. Un asesor te contactará pronto.")
            set_state(wa_id, stage="DONE")
            return
        send_text(wa_id, "Entendido. Si deseas asesor escribe 'asesor'.")
        set_state(wa_id, stage="DONE")
        return
    if stage == "FOLLOW":
        if "asesor" in tN or "si" in tN or "sí" in tN:
            notify_advisor(wa_id, "Contacto IMSS")
            send_text(wa_id, "Listo. Avisé al asesor para que te contacte.")
            set_state(wa_id, stage="DONE")
            return
        send_text(wa_id, "Si deseas continuar escribe 'asesor' o 'menu'.")
        set_state(wa_id, stage="DONE")
        return

def flow_contacto(wa_id: str, text: str):
    notify_advisor(wa_id, "Cliente pidió contacto", extra=text[:200])
    send_text(wa_id, "He notificado al asesor. Te contactarán pronto.")
    set_state(wa_id, flow="CONTACTO", stage="DONE")

def flow_empresarial(wa_id: str, text: str):
    send_text(wa_id, "🏢 *Crédito empresarial* — dime ¿qué monto necesitas y a qué se dedica tu empresa?")
    notify_advisor(wa_id, "Interés Crédito Empresarial", extra=text[:200])
    set_state(wa_id, flow="EMPRESARIAL", stage="DONE")

def flow_vida(wa_id: str, text: str):
    send_text(wa_id, "🛡️ *Vida & Salud* — cuéntame qué buscas (vida, gastos médicos, deducible deseado) y te asesoro.")
    notify_advisor(wa_id, "Interés Vida/Salud", extra=text[:200])
    set_state(wa_id, flow="VIDA", stage="DONE")

def flow_vrim(wa_id: str, text: str):
    send_text(wa_id, "🧾 *VRIM* — membresías médicas con consultas y descuentos. ¿Para cuántas personas?")
    notify_advisor(wa_id, "Interés VRIM", extra=text[:200])
    set_state(wa_id, flow="VRIM", stage="DONE")

def flow_prestamo_imss(wa_id: str, text: str):
    send_text(wa_id, "💸 *Préstamos a pensionados IMSS (Ley 73)* — monto mínimo $40,000. ¿Qué monto te interesa y tu edad?")
    notify_advisor(wa_id, "Interés Préstamo IMSS", extra=text[:200])
    set_state(wa_id, flow="PRESTAMO_IMSS", stage="DONE")

# ----------------------------------------------------------------------------
# WEBHOOK
# ----------------------------------------------------------------------------
@app.route("/webhook", methods=["GET"])
def webhook_verify():
    if request.args.get("hub.mode") == "subscribe" and request.args.get("hub.verify_token") == VERIFY_TOKEN:
        return request.args.get("hub.challenge",""), 200
    return "Forbidden", 403

@app.route("/webhook", methods=["POST"])
def webhook_receive():
    payload = request.get_json(silent=True) or {}
    try:
        entries = payload.get("entry", []) or []
        for entry in entries:
            for change in (entry.get("changes", []) or []):
                value = change.get("value", {}) or {}
                for msg in (value.get("messages", []) or []):
                    wa_from = normalize_msisdn(msg.get("from",""))
                    wa_last10 = last10(wa_from)
                    mtype = msg.get("type")

                    # Reconocimiento en Sheets (si aplica). No indispensable para operar.
                    row = find_row_by_last10(wa_last10) if GOOGLE_ENABLED else None

                    # Procesamiento por tipo
                    if mtype == "text":
                        text = msg.get("text",{}).get("body","").strip()
                        tN   = norm(text)
                        logger.info("💬 %s: %s", mask_phone(wa_from), text[:240])

                        # Palabras clave directas
                        if tN in ("menu","menú") or tN in ("hola","buenos dias","buenas tardes","buenas noches"):
                            flow_show_menu(wa_from); continue
                        if "auto" in tN:         flow_auto(wa_from, text); continue
                        if "imss" in tN:         flow_imss(wa_from, text); continue
                        if "contact" in tN or "asesor" in tN or "christian" in tN:
                            flow_contacto(wa_from, text); continue
                        if "empres" in tN:       flow_empresarial(wa_from, text); continue
                        if "vrim" in tN:         flow_vrim(wa_from, text); continue
                        if "vida" in tN or "salud" in tN:
                            flow_vida(wa_from, text); continue
                        if "prestamo" in tN or "préstamo" in tN:
                            flow_prestamo_imss(wa_from, text); continue
                        if any(k in tN for k in ("ine","placa","tarjeta")) and get_state(wa_from).get("flow") == "AUTO":
                            flow_auto(wa_from, text); continue

                        # Pregunta abierta -> RAG según último dominio usado
                        st = get_state(wa_from)
                        last_dom = st.get("last_domain")
                        if is_question_for_rag(tN):
                            ans = answer_with_rag(text, domain_hint=last_dom)
                            send_text(wa_from, ans); continue

                        # Si no cae en nada, muestra menú
                        flow_show_menu(wa_from)

                    elif mtype in ("image","document","video","audio","sticker"):
                        # Agradece y notifica. (No se respalda a Drive en esta versión para mantener permisos read-only)
                        notify_advisor(wa_from, f"Cliente envió {mtype}")
                        send_text(wa_from, "Archivo recibido, gracias. Si deseas continuar escribe 'menu'.")
                    else:
                        flow_show_menu(wa_from)
    except Exception:
        logger.error("Webhook error:\n%s", traceback.format_exc())
    return jsonify({"ok": True}), 200

# ----------------------------------------------------------------------------
# Utilidades
# ----------------------------------------------------------------------------
@app.route("/ext/health", methods=["GET"])
def ext_health():
    return jsonify({
        "status": "ok",
        "whatsapp": WHATSAPP_ENABLED,
        "openai": bool(client_oa),
        "google": GOOGLE_ENABLED,
        "sheets_id_set": bool(SHEETS_ID_LEADS),
        "sheets_title": SHEETS_TITLE_LEADS,
        "rag_auto_has_id_or_name": bool(RAG_AUTO_FILE_ID or RAG_AUTO_FILE_NAME),
        "rag_imss_has_id_or_name": bool(RAG_IMSS_FILE_ID or RAG_IMSS_FILE_NAME)
    }), 200

@app.route("/ext/manuales", methods=["GET"])
def ext_manuales():
    def _stat(domain):
        c = rag_cache.get(domain) or {}
        return {
            "loaded": bool(c),
            "name": c.get("name"),
            "chars": len(c.get("text","")),
            "loaded_at": datetime.fromtimestamp(c.get("loaded_at",0)).isoformat() if c.get("loaded_at") else None
        }
    return jsonify({"auto": _stat("auto"), "imss": _stat("imss")}), 200

@app.route("/ext/test-send", methods=["POST"])
def ext_test_send():
    body = request.get_json(silent=True) or {}
    to = body.get("to")
    text = body.get("text","Prueba Vicky ✔️")
    if not to: return jsonify({"error":"faltó 'to'"}), 400
    send_text(to, text)
    return jsonify({"ok": True}), 200

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    logger.info("Arrancando en puerto %s", port)
    app.run(host="0.0.0.0", port=port, debug=False)
