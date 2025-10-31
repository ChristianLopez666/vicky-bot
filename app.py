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
# Google helpers - MEJORADOS
# =========================
def sheet_match_by_last10(last10: str) -> Optional[Dict[str, Any]]:
    """Busca en Google Sheets por los últimos 10 dígitos del teléfono - CORREGIDO"""
    if not (google_ready and sheets_client and GOOGLE_SHEET_ID and GOOGLE_SHEET_NAME):
        log.warning("Google no está listo para buscar en Sheets")
        return None
    
    try:
        sh = sheets_client.open_by_key(GOOGLE_SHEET_ID)
        ws = sh.worksheet(GOOGLE_SHEET_NAME)
        rows = ws.get_all_values()
        
        log.info(f"🔍 Buscando teléfono {last10} en {len(rows)} filas...")
        
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
        
    except Exception as e:
        log.exception(f"Error leyendo Google Sheets: {str(e)}")
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
# RAG light (Auto) — lectura PDF desde Drive
# =========================
_manual_auto_cache = {"text": None, "file_id": None, "loaded_at": None}

def _find_auto_manual_file_id() -> Optional[str]:
    if not (google_ready and drive_client and MANUALES_VICKY_FOLDER_ID):
        return None
    try:
        q = (
            f"'{MANUALES_VICKY_FOLDER_ID}' in parents and "
            "mimeType='application/pdf' and trashed=false"
        )
        resp = drive_client.files().list(q=q, fields="files(id, name)", pageSize=50).execute()
        files = resp.get("files", [])
        # Prioriza nombres que sugieran auto/coberturas
        for f in files:
            name = (f.get("name") or "").lower()
            if "auto" in name or "cobertura" in name:
                return f["id"]
        return files[0]["id"] if files else None
    except Exception:
        log.exception("Error buscando manual Auto")
        return None

def _download_pdf_text(file_id: str) -> Optional[str]:
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
        pages = []
        for p in reader.pages:
            try:
                pages.append(p.extract_text() or "")
            except Exception:
                pages.append("")
        text = "\n".join(pages)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip() or None
    except Exception:
        log.exception("No se pudo extraer texto PDF (Auto)")
        return None

def ensure_auto_manual_text(force_reload: bool = False) -> Optional[str]:
    if not client_oa:
        return None
    if not force_reload and _manual_auto_cache.get("text"):
        return _manual_auto_cache["text"]
    fid = _find_auto_manual_file_id()
    if not fid:
        return None
    text = _download_pdf_text(fid)
    if text:
        _manual_auto_cache.update({"text": text, "file_id": fid, "loaded_at": datetime.utcnow()})
        log.info("[rag-auto] Manual cacheado")
    return text

def answer_auto_from_manual(question: str) -> Optional[str]:
    if not client_oa:
        return None
    manual_text = ensure_auto_manual_text()
    if not manual_text:
        return None
    # Heurística: filtra párrafos relevantes
    keys = ["amplia plus", "amplia", "cobertura", "asistencia", "cristales", "auto de reemplazo", "deducible"]
    parts = []
    for ln in manual_text.split("\n"):
        low = ln.lower()
        if any(k in low for k in keys):
            parts.append(ln.strip())
            if len(" ".join(parts)) > 8000:
                break
    focus = "\n".join(parts) if parts else manual_text[:9000]
    try:
        prompt = (
            "Responde SOLO con base en el texto del manual (auto). "
            "Sé preciso, en español, y usa viñetas si ayuda. "
            "Si no aparece en el manual, di: 'No está indicado en el manual'.\n\n"
            f"Pregunta: {question}\n\n"
            f"Manual (extracto):\n{focus}\n\n"
            "==\nRespuesta:"
        )
        res = client_oa.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
        )
        ans = (res.choices[0].message.content or "").strip()
        return ans[:1500] if ans else None
    except Exception:
        log.exception("Error RAG-auto")
        return None

# =========================
# Menú y flujos
# =========================
MAIN_MENU = (
    "🟦 *Vicky Bot — Inbursa*\n"
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
                f"Teléfono: {ctx.get('imss_telefono','ND')}\n"
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
        if NOTIFICAR_ASESOR:
            formatted = (
                "🔔 NUEVO PROSPECTO – PRÉSTAMO IMSS\n"
                f"Nombre: {ctx.get('imss_nombre','')}\n"
                f"WhatsApp: {phone}\n"
                f"Teléfono: {ctx.get('imss_telefono','ND')}\n"
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
                f"Teléfono: {ctx.get('emp_telefono','ND')}\n"
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

    # --- RAG light para preguntas de AUTO (coberturas) ---
    if any(k in t for k in ["amplia plus", "amplia+", "cobertura", "coberturas", "cristales", "asistencia", "auto de reemplazo"]):
        rag_ans = answer_auto_from_manual(text or t)
        if rag_ans:
            send_message(phone, rag_ans)
            return
    # -----------------------------------------------------

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

    # Fallback GPT (OpenAI 1.x)
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
# Webhook - CORREGIDO
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

        # DEBUG: Log del número recibido
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
# Endpoints auxiliares
# =========================
@app.get("/ext/health")
def ext_health():
    return jsonify({
        "status": "ok",
        "timestamp": datetime.utcnow().isoformat(),
        "whatsapp_configured": bool(META_TOKEN and WABA_PHONE_ID),
        "google_ready": google_ready,
        "openai_ready": bool(client_oa is not None),
        "sheet_name": GOOGLE_SHEET_NAME,
        "manuales_folder": bool(MANUALES_VICKY_FOLDER_ID),
    }), 200

@app.get("/ext/diagnostico-google")
def ext_diagnostico_google():
    """Diagnóstico completo de Google Drive/Sheets"""
    diagnostico = {
        "paso_1_credenciales_basicas": {
            "google_ready": google_ready,
            "sheets_client": sheets_client is not None,
            "drive_client": drive_client is not None
        },
        "paso_2_variables_entorno": {
            "GOOGLE_SHEET_ID": bool(GOOGLE_SHEET_ID),
            "GOOGLE_SHEET_NAME": bool(GOOGLE_SHEET_NAME),
            "GOOGLE_CREDENTIALS_JSON": bool(GOOGLE_CREDENTIALS_JSON),
            "MANUALES_VICKY_FOLDER_ID": bool(MANUALES_VICKY_FOLDER_ID)
        },
        "paso_3_prueba_sheets": "no_iniciado",
        "paso_4_prueba_drive": "no_iniciado"
    }
    
    # Paso 3: Probar Sheets
    if sheets_client and GOOGLE_SHEET_ID:
        try:
            sh = sheets_client.open_by_key(GOOGLE_SHEET_ID)
            ws = sh.worksheet(GOOGLE_SHEET_NAME)
            rows = ws.get_all_values()
            diagnostico["paso_3_prueba_sheets"] = f"✅ OK - {len(rows)} filas encontradas"
        except Exception as e:
            diagnostico["paso_3_prueba_sheets"] = f"❌ ERROR: {str(e)}"
    
    # Paso 4: Probar Drive
    if drive_client and MANUALES_VICKY_FOLDER_ID:
        try:
            q = f"'{MANUALES_VICKY_FOLDER_ID}' in parents and mimeType='application/pdf'"
            files = drive_client.files().list(q=q).execute()
            diagnostico["paso_4_prueba_drive"] = f"✅ OK - {len(files.get('files', []))} PDFs encontrados"
        except Exception as e:
            diagnostico["paso_4_prueba_drive"] = f"❌ ERROR: {str(e)}"
    
    return jsonify(diagnostico)

@app.get("/ext/debug-busqueda")
def ext_debug_busqueda():
    """Debug específico para la búsqueda en Sheets"""
    test_number = "5216681620521"  # Tu número
    last10 = _normalize_last10(test_number)
    
    debug_info = {
        "numero_original": test_number,
        "last10_buscado": last10,
        "resultado": None
    }
    
    if sheets_client and GOOGLE_SHEET_ID:
        try:
            sh = sheets_client.open_by_key(GOOGLE_SHEET_ID)
            ws = sh.worksheet(GOOGLE_SHEET_NAME)
            rows = ws.get_all_values()
            
            debug_info["total_filas"] = len(rows)
            debug_info["primeras_filas"] = rows[:3]  # Muestra primeras filas
            
            # Buscar manualmente
            for i, row in enumerate(rows):
                row_text = " | ".join(str(cell) for cell in row)
                if last10 in row_text:
                    debug_info["resultado"] = {
                        "fila": i + 1,
                        "contenido": row,
                        "texto_completo": row_text
                    }
                    break
                    
        except Exception as e:
            debug_info["error"] = str(e)
    
    return jsonify(debug_info)

@app.post("/ext/test-send")
def ext_test_send():
    try:
        data = request.get_json(force=True) or {}
        to = str(data.get("to", "")).strip()
        text = str(data.get("text", "")).strip()
        if not to or not text:
            return jsonify({"ok": False, "error": "Faltan 'to' y/o 'text'"}), 400
        ok = send_message(to, text)
        return jsonify({"ok": bool(ok)}), 200
    except Exception as e:
        log.exception("Error en /ext/test-send")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.get("/ext/debug-notify")
def ext_debug_notify():
    """Endpoint para probar notificaciones con diferentes números"""
    test_numbers = [
        "5216682478005",
        "5216681922865", 
        "6682478005",
        "6681922865"
    ]
    
    results = {}
    for num in test_numbers:
        test_msg = f"🔧 TEST: Notificación a {num} - {datetime.now().strftime('%H:%M:%S')}"
        success = send_message(num, test_msg)
        results[num] = success
        time.sleep(2)
    
    return jsonify({"ok": True, "results": results})

@app.get("/ext/manuales")
def ext_manuales():
    try:
        files = list_drive_manuals(MANUALES_VICKY_FOLDER_ID)
        return jsonify({"ok": True, "files": files}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.get("/health")
def health():
    return jsonify({"status": "ok", "service": "Vicky Bot SECOM"}), 200

# =========================
# Arranque local
# =========================
if __name__ == "__main__":
    log.info(f"Vicky SECOM en puerto {PORT}")
    log.info(f"WhatsApp configurado: {bool(META_TOKEN and WABA_PHONE_ID)}")
    log.info(f"Google listo: {google_ready}")
    log.info(f"OpenAI listo: {bool(client_oa is not None)}")
    app.run(host="0.0.0.0", port=PORT, debug=False)


