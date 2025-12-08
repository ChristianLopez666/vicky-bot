# app.py — Vicky SECOM (Vicky WAPI + Campañas + Recordatorios + Forward Docs)
from __future__ import annotations
import os, re, json, time, logging, threading
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, List, Tuple

import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# Google Sheets API
try:
    from google.oauth2.service_account import Credentials as ServiceAccountCredentials
    from googleapiclient.discovery import build as gbuild
    GOOGLE_AVAILABLE = True
except ImportError:
    ServiceAccountCredentials = None
    gbuild = None
    GOOGLE_AVAILABLE = False

# OpenAI opcional
try:
    import openai
    OPENAI_AVAILABLE = True
except ImportError:
    openai = None
    OPENAI_AVAILABLE = False

# ==========================
# Carga entorno
# ==========================
load_dotenv()

def _get(name: str, default: str = "") -> str:
    return (os.getenv(name, default) or "").strip()

META_TOKEN = _get("META_TOKEN") or _get("WHATSAPP_TOKEN")
WABA_PHONE_ID = _get("WABA_PHONE_ID") or _get("PHONE_NUMBER_ID")
VERIFY_TOKEN = _get("VERIFY_TOKEN")
ADVISOR_NUMBER = _get("ADVISOR_NUMBER") or _get("ADVISOR_WHATSAPP")
PORT = int(_get("PORT", "5000"))

SHEETS_ID_LEADS = _get("SHEETS_ID_LEADS") or _get("SHEET_ID_SECOM")
SHEETS_TITLE_LEADS = _get("SHEETS_TITLE_LEADS") or _get("SHEET_TITLE_SECOM", "Prospectos SECOM Auto")
GOOGLE_CREDENTIALS_JSON = _get("GOOGLE_CREDENTIALS_JSON")

OPENAI_API_KEY = _get("OPENAI_API_KEY")
if OPENAI_AVAILABLE and OPENAI_API_KEY:
    openai.api_key = OPENAI_API_KEY

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("vicky-secom")

app = Flask(__name__)

# ==========================
# Cache de idiomas de plantillas
# ==========================
_template_lang_cache: Dict[str, str] = {
    # Mapeo estático de plantillas a idiomas
    # Si no está en cache, usaremos el valor por defecto
}

def _detect_template_language(template_name: str, to_number: str) -> Optional[str]:
    """Detección robusta de idioma para plantilla SIN ENVIAR MENSAJES REALES."""
    # Primero revisar cache
    if template_name in _template_lang_cache:
        log.info(f"✅ Idioma desde cache para {template_name}: {_template_lang_cache[template_name]}")
        return _template_lang_cache[template_name]
    
    # Lógica de detección sin envío real
    # Por ahora, usamos mapeo estático basado en patrones
    template_lower = template_name.lower()
    
    # Detectar idioma basado en nombre de plantilla
    if any(word in template_lower for word in ["es_", "spanish", "español", "mx", "latam"]):
        lang = "es_MX"
    elif any(word in template_lower for word in ["en_", "english", "us", "uk"]):
        lang = "en_US"
    else:
        # Idioma por defecto
        lang = "es_MX"
    
    # Guardar en cache para futuras llamadas
    _template_lang_cache[template_name] = lang
    log.info(f"🔤 Idioma detectado para {template_name}: {lang} (sin envío real)")
    return lang

# ==========================
# Estado (normalizado por últimos 10 dígitos)
# ==========================
_user_state: Dict[str, str] = {}
_user_data: Dict[str, Dict[str, Any]] = {}

def _normalize_phone(phone: str) -> str:
    if not phone:
        return ""
    digits = re.sub(r"\D", "", phone)
    return digits[-10:] if len(digits) >= 10 else digits

def get_state(phone: str) -> str:
    return _user_state.get(_normalize_phone(phone), "")

def set_state(phone: str, value: str) -> None:
    key = _normalize_phone(phone)
    if value:
        _user_state[key] = value
    elif key in _user_state:
        del _user_state[key]

def get_data(phone: str) -> Dict[str, Any]:
    key = _normalize_phone(phone)
    if key not in _user_data:
        _user_data[key] = {}
    return _user_data[key]

# ==========================
# WhatsApp helpers
# ==========================
WPP_API_URL = f"https://graph.facebook.com/v21.0/{WABA_PHONE_ID}/messages" if WABA_PHONE_ID else None

def interpret_response(text: str) -> str:
    if not text:
        return "neutral"
    t = text.lower().strip()
    pos = ["sí", "si", "claro", "ok", "de acuerdo", "vale", "afirmativo", "correcto", "yes"]
    neg = ["no", "nel", "nop", "negativo", "no quiero", "no gracias", "no interesa"]
    if any(p in t for p in pos):
        return "positive"
    if any(n in t for n in neg):
        return "negative"
    return "neutral"

def extract_number(text: str) -> Optional[float]:
    if not text:
        return None
    clean = text.replace(",", "").replace("$", "").strip()
    m = re.search(r"(\d+(\.\d+)?)", clean)
    try:
        return float(m.group(1)) if m else None
    except (ValueError, AttributeError):
        return None

def _headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {META_TOKEN}",
        "Content-Type": "application/json",
        "User-Agent": "VickyBot-SECOM/1.0"
    }

def _should_retry(status: int) -> bool:
    return status == 429 or 500 <= status < 600

def _backoff(attempt: int) -> None:
    time.sleep(min(2**attempt, 10))  # Máximo 10 segundos

def send_message(to: str, text: str) -> bool:
    """Envía mensaje de texto simple."""
    if not (META_TOKEN and WPP_API_URL):
        log.error("❌ WhatsApp API no configurada")
        return False

    if not to or not text:
        log.error("❌ Destino o texto vacío")
        return False

    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text[:4096]},
    }

    for attempt in range(3):
        try:
            r = requests.post(
                WPP_API_URL,
                headers=_headers(),
                json=payload,
                timeout=15
            )
            
            if r.status_code == 200:
                log.info(f"📤 Mensaje enviado a {to}: {text[:80]}...")
                return True
            
            log.warning(f"⚠️ Error send_message {r.status_code}: {r.text[:200]}")
            
            if _should_retry(r.status_code) and attempt < 2:
                _backoff(attempt)
                continue
                
            return False
            
        except requests.exceptions.Timeout:
            log.warning(f"⏱️ Timeout en intento {attempt+1} para {to}")
            if attempt < 2:
                _backoff(attempt)
                continue
            return False
        except Exception as e:
            log.error(f"❌ Error en send_message: {e}")
            if attempt < 2:
                _backoff(attempt)
                continue
            return False
    
    return False

def _validate_and_reconstruct_components(user_components: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Valida y reconstruye componentes de plantilla 100% compatible con Meta."""
    if not user_components:
        return []

    reconstructed = []
    header_found = False
    body_found = False
    button_count = 0
    
    for comp in user_components:
        comp_type = comp.get("type")
        if not comp_type:
            continue
            
        # HEADER
        if comp_type == "header":
            header_found = True
            parameters = comp.get("parameters", [])
            # Validar que los parámetros del header sean válidos
            valid_params = []
            for param in parameters:
                if isinstance(param, dict):
                    param_type = param.get("type", "text")
                    if param_type == "text":
                        if "text" in param:
                            valid_params.append({"type": "text", "text": str(param["text"])})
                    elif param_type == "currency":
                        if "currency" in param and "amount_1000" in param:
                            valid_params.append({
                                "type": "currency",
                                "currency": {"code": str(param["currency"]), "fallback_value": str(param.get("fallback_value", ""))},
                                "amount_1000": int(param["amount_1000"])
                            })
                    elif param_type == "date_time":
                        if "date_time" in param:
                            valid_params.append({
                                "type": "date_time",
                                "date_time": {"calendar": str(param["date_time"])}
                            })
                    elif param_type == "image" or param_type == "video" or param_type == "document":
                        if "image" in param or "video" in param or "document" in param:
                            valid_params.append(param)
            
            reconstructed.append({
                "type": "header",
                "parameters": valid_params if valid_params else []
            })
        
        # BODY (obligatorio en todas las plantillas)
        elif comp_type == "body":
            body_found = True
            parameters = comp.get("parameters", [])
            # Validar parámetros del body
            valid_params = []
            for param in parameters:
                if isinstance(param, dict) and "type" in param:
                    if param["type"] == "text" and "text" in param:
                        valid_params.append({"type": "text", "text": str(param["text"])})
                elif isinstance(param, str):
                    valid_params.append({"type": "text", "text": str(param)})
            
            reconstructed.append({
                "type": "body",
                "parameters": valid_params
            })
        
        # FOOTER
        elif comp_type == "footer":
            parameters = comp.get("parameters", [])
            if parameters:
                valid_params = []
                for param in parameters:
                    if isinstance(param, dict) and "type" in param:
                        if param["type"] == "text" and "text" in param:
                            valid_params.append({"type": "text", "text": str(param["text"])})
                reconstructed.append({
                    "type": "footer",
                    "parameters": valid_params
                })
        
        # BUTTONS
        elif comp_type == "button":
            sub_type = comp.get("sub_type", "quick_reply")
            index = str(comp.get("index", button_count))
            
            # Normalizar índice para Meta
            try:
                index_int = int(index)
                if index_int < 0 or index_int > 2:  # Meta solo permite 0-2 para botones
                    index = str(button_count % 3)
            except ValueError:
                index = str(button_count % 3)
            
            button_data = {
                "type": "button",
                "sub_type": sub_type,
                "index": index,
            }
            
            # Añadir parámetros según tipo de botón
            parameters = comp.get("parameters", [])
            if sub_type == "quick_reply":
                if parameters and isinstance(parameters, list):
                    for param in parameters:
                        if isinstance(param, dict) and param.get("type") == "payload":
                            button_data["parameters"] = [param]
                            break
            elif sub_type == "url":
                if parameters and isinstance(parameters, list):
                    for param in parameters:
                        if isinstance(param, dict) and param.get("type") == "text":
                            button_data["parameters"] = [param]
                            break
            elif sub_type == "call_to_action":
                if parameters and isinstance(parameters, list):
                    for param in parameters:
                        if isinstance(param, dict) and param.get("type") == "payload":
                            button_data["parameters"] = [param]
                            break
            
            reconstructed.append(button_data)
            button_count += 1
    
    # Body es obligatorio en todas las plantillas - si no existe, crearlo vacío
    if not body_found:
        reconstructed.append({
            "type": "body",
            "parameters": []
        })
    
    # Ordenar componentes según especificación de Meta
    ordered = []
    for comp_type in ["header", "body", "footer", "button"]:
        for comp in reconstructed:
            if comp["type"] == comp_type:
                ordered.append(comp)
    
    log.debug(f"🔧 Componentes reconstruidos: {[c['type'] for c in ordered]}")
    return ordered

def send_template_message(
    to_number: str,
    template_name: str,
    components: List[Dict[str, Any]] = None,
    namespace: str = None,
    language_code: str = None,
) -> Dict[str, Any]:
    """
    Envía plantilla de WhatsApp con manejo robusto de errores.
    """
    if not (META_TOKEN and WABA_PHONE_ID):
        log.error("❌ WhatsApp API no configurada para plantillas")
        return {"error": "WhatsApp API not configured"}

    # 1. Validar template_name
    if not template_name or not isinstance(template_name, str):
        log.error("❌ Nombre de plantilla inválido")
        return {"error": "Invalid template name"}

    # 2. Detección de idioma
    if language_code:
        selected_lang = language_code
        log.info(f"🔤 Usando idioma forzado: {selected_lang}")
    else:
        selected_lang = _detect_template_language(template_name, to_number)
        if not selected_lang:
            selected_lang = "es_MX"  # Idioma por defecto

    # 3. Validación y reconstrucción de componentes
    final_components = []
    if components:
        final_components = _validate_and_reconstruct_components(components)
        log.info(f"🔧 Componentes finales para {template_name}: {len(final_components)}")

    # 4. Construcción del payload
    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": selected_lang},
        }
    }

    if final_components:
        payload["template"]["components"] = final_components
    
    if namespace:
        payload["template"]["namespace"] = namespace

    # 5. Envío con manejo de reintentos y delays para evitar saturación
    url = f"https://graph.facebook.com/v21.0/{WABA_PHONE_ID}/messages"
    
    for attempt in range(3):
        try:
            # Delay inteligente para evitar saturación
            if attempt > 0:
                time.sleep(0.2)
            
            resp = requests.post(
                url,
                headers=_headers(),
                json=payload,
                timeout=15,
            )
            
            try:
                response_json = resp.json()
            except ValueError:
                response_json = {"raw_response": resp.text}
            
            if resp.status_code == 200 and "error" not in str(response_json).lower():
                log.info(f"✅ Plantilla '{template_name}' enviada a {to_number} en {selected_lang}")
                return response_json
            else:
                log.error(f"❌ Error {resp.status_code} enviando plantilla: {response_json}")
                
                if _should_retry(resp.status_code) and attempt < 2:
                    _backoff(attempt)
                    continue
                    
                return {"error": f"HTTP {resp.status_code}", "details": response_json}
                
        except requests.exceptions.Timeout:
            log.warning(f"⏱️ Timeout enviando plantilla (intento {attempt+1})")
            if attempt < 2:
                _backoff(attempt)
                continue
            return {"error": "Timeout sending template"}
        except Exception as e:
            log.error(f"❌ Error enviando plantilla: {e}")
            if attempt < 2:
                _backoff(attempt)
                continue
            return {"error": str(e)}
    
    return {"error": "Failed after 3 attempts"}

def _send_media(to: str, mtype: str, media_id: str, filename: Optional[str] = None, caption: str = "") -> bool:
    """Reenvía un media existente (id) al número indicado."""
    if not (META_TOKEN and WPP_API_URL):
        log.error("❌ WhatsApp API no configurada para media")
        return False

    if mtype not in ("image", "document", "audio", "video"):
        log.error(f"❌ Tipo de media no soportado: {mtype}")
        return False

    media_obj: Dict[str, Any] = {"id": media_id}
    if filename and mtype == "document":
        media_obj["filename"] = filename
    if caption:
        media_obj["caption"] = caption[:1024]

    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": mtype,
        mtype: media_obj,
    }

    try:
        r = requests.post(WPP_API_URL, headers=_headers(), json=payload, timeout=20)
        if r.status_code == 200:
            log.info(f"📤 Media reenviado a {to} ({mtype})")
            return True
        log.warning(f"⚠️ Error {r.status_code} al reenviar media: {r.text[:200]}")
        return False
    except Exception as e:
        log.error(f"❌ Error en _send_media: {e}")
        return False

def forward_media_to_advisor(origin_phone: str, mtype: str, msg: Dict[str, Any]) -> None:
    """Reenvía el archivo recibido al asesor."""
    if not ADVISOR_NUMBER:
        return
    
    try:
        media = msg.get(mtype, {})
        media_id = media.get("id")
        if not media_id:
            log.warning("⚠️ No se encontró media_id para reenviar")
            return
        
        filename = media.get("filename")
        caption = f"Documento de {origin_phone} - {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}"
        
        success = _send_media(ADVISOR_NUMBER, mtype, media_id, filename=filename, caption=caption)
        if success:
            log.info(f"✅ Media reenviado al asesor {ADVISOR_NUMBER}")
        else:
            log.warning(f"⚠️ Falló reenvío al asesor {ADVISOR_NUMBER}")
            
    except Exception as e:
        log.error(f"❌ Error reenviando media al asesor: {e}")

# ==========================
# Google Sheets (SECOM) - lectura/escritura
# ==========================
sheets = None
google_ready = False

if GOOGLE_AVAILABLE and GOOGLE_CREDENTIALS_JSON and SHEETS_ID_LEADS:
    try:
        info = json.loads(GOOGLE_CREDENTIALS_JSON)
        creds = ServiceAccountCredentials.from_service_account_info(
            info,
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        sheets = gbuild("sheets", "v4", credentials=creds)
        google_ready = True
        log.info("✅ Google Sheets configurado (RW)")
    except Exception as e:
        log.error(f"❌ Error configurando Google Sheets: {e}")
        google_ready = False
else:
    log.warning("⚠️ Google Sheets no disponible - verificar configuraciones")

def _col_letter(col: int) -> str:
    """Convierte índice de columna (1-based) a letra de Excel."""
    result = ""
    while col > 0:
        col, rem = divmod(col - 1, 26)
        result = chr(65 + rem) + result
    return result

def _find_col(headers: List[str], names: List[str]) -> Optional[int]:
    """Encuentra índice de columna por nombres posibles."""
    if not headers:
        return None
    
    lower_headers = [h.strip().lower() for h in headers]
    for name in names:
        name_lower = name.strip().lower()
        if name_lower in lower_headers:
            return lower_headers.index(name_lower)
    return None

def _get_sheet_headers_and_rows() -> Tuple[List[str], List[List[str]]]:
    """Obtiene encabezados y filas de la hoja de leads."""
    if not (google_ready and sheets and SHEETS_ID_LEADS and SHEETS_TITLE_LEADS):
        return [], []
    
    try:
        rng = f"{SHEETS_TITLE_LEADS}!A:Z"
        res = sheets.spreadsheets().values().get(
            spreadsheetId=SHEETS_ID_LEADS,
            range=rng
        ).execute()
        
        rows = res.get("values", [])
        if not rows:
            return [], []
            
        headers = rows[0]
        data_rows = rows[1:] if len(rows) > 1 else []
        return headers, data_rows
        
    except Exception as e:
        log.error(f"❌ Error obteniendo datos de Sheets: {e}")
        return [], []

def _batch_update_cells(row_index: int, updates: Dict[str, str], headers: List[str]) -> bool:
    """Actualiza múltiples celdas en una fila por nombre de columna."""
    if not (google_ready and sheets and SHEETS_ID_LEADS and SHEETS_TITLE_LEADS):
        return False
    
    if row_index < 2:  # Fila 2 es la primera de datos
        return False
    
    if not updates:
        return True

    header_lower = [h.strip().lower() for h in headers]
    data_ranges = []

    for key, value in updates.items():
        key_lower = key.strip().lower()
        if key_lower in header_lower:
            col_idx = header_lower.index(key_lower) + 1  # 1-based
            col_letter = _col_letter(col_idx)
            cell_range = f"{SHEETS_TITLE_LEADS}!{col_letter}{row_index}"
            data_ranges.append({
                "range": cell_range,
                "values": [[str(value)]]
            })

    if not data_ranges:
        return True

    body = {
        "valueInputOption": "RAW",
        "data": data_ranges,
    }
    
    try:
        sheets.spreadsheets().values().batchUpdate(
            spreadsheetId=SHEETS_ID_LEADS,
            body=body
        ).execute()
        log.debug(f"✅ Celdas actualizadas en fila {row_index}")
        return True
    except Exception as e:
        log.error(f"❌ Error en _batch_update_cells: {e}")
        return False

def match_client_in_sheets(phone: str) -> Optional[Dict[str, Any]]:
    """Busca cliente en hoja por WhatsApp."""
    if not google_ready:
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
        if not target:
            return None

        for row in rows:
            if len(row) <= idx_wa:
                continue
            
            row_phone = _normalize_phone(row[idx_wa])
            if row_phone == target:
                nombre = row[idx_name] if idx_name is not None and len(row) > idx_name else ""
                return {"nombre": nombre.strip(), "found": True}
        
        return None
        
    except Exception as e:
        log.error(f"❌ Error en match_client_in_sheets: {e}")
        return None

def _touch_last_inbound(phone: str) -> None:
    """Marca última actividad entrante en Google Sheets."""
    if not google_ready:
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
        if not target:
            return

        for offset, row in enumerate(rows, start=2):
            if len(row) <= idx_wa:
                continue
            
            row_phone = _normalize_phone(row[idx_wa])
            if row_phone == target:
                updates = {
                    headers[idx_last]: datetime.utcnow().isoformat()
                }
                _batch_update_cells(offset, updates, headers)
                log.debug(f"✅ Actualizado LastInboundAt para {phone}")
                break
                
    except Exception as e:
        log.error(f"❌ Error en _touch_last_inbound: {e}")

# ==========================
# GPT Integration
# ==========================
def gpt_process_message(phone: str, text: str, match: Optional[Dict[str, Any]], state: str) -> Optional[str]:
    """Procesa mensaje con GPT cuando no hay estado activo."""
    if not (OPENAI_AVAILABLE and OPENAI_API_KEY):
        return None

    # Solo procesar si no hay estado activo
    if state and state != "":
        return None
        
    if len(text.strip()) < 3:
        return None

    # Evitar procesar comandos simples
    simple_commands = ["hola", "menu", "menú", "ayuda", "info", "opciones", 
                      "1", "2", "3", "4", "5", "6", "7", "si", "no", "gracias"]
    if text.lower().strip() in simple_commands:
        return None

    try:
        system_prompt = """Eres Vicky, asistente virtual de Inbursa especializada en productos financieros y seguros. 
        Eres profesional, cálida y directa. Tu objetivo es ayudar a los clientes con:
        - Préstamos IMSS (Ley 73)
        - Seguros de Auto
        - Seguros de Vida/Salud
        - Tarjeta médica VRIM
        - Crédito Empresarial
        - Financiamiento Práctico
        
        Responde de manera útil y concisa. Si no sabes algo específico, sugiere contactar con un asesor.
        Mantén un tono amable pero profesional. Responde en español.
        
        Si el cliente pregunta por algo fuera de estos temas, sugiere amablemente contactar con un asesor humano."""

        completion = openai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text}
            ],
            temperature=0.4,
            max_tokens=500
        )
        
        answer = completion.choices[0].message.content.strip()
        log.info(f"🧠 GPT respondió a {phone}: {answer[:100]}...")
        return answer
        
    except Exception as e:
        log.error(f"❌ Error en GPT: {e}")
        return None

# ==========================
# Menú y helpers
# ==========================
def send_main_menu(phone: str) -> None:
    """Envía menú principal."""
    menu = (
        "Vicky Bot — Inbursa\n"
        "Elige una opción:\n"
        "1) Préstamo IMSS (Ley 73)\n"
        "2) Seguro de Auto (cotización)\n"
        "3) Seguros de Vida / Salud\n"
        "4) Tarjeta médica VRIM\n"
        "5) Crédito Empresarial\n"
        "6) Financiamiento Práctico\n"
        "7) Contactar con Christian\n\n"
        "Escribe el número u opción (ej. 'imss', 'auto', 'empresarial', 'contactar')."
    )
    send_message(phone, menu)

def notify_advisor(msg: str) -> None:
    """Notifica al asesor."""
    if ADVISOR_NUMBER and msg:
        send_message(ADVISOR_NUMBER, msg)

# ==========================
# Embudos
# ==========================
def imss_start(phone: str, match: Optional[Dict[str, Any]]) -> None:
    """Inicia embudo IMSS."""
    set_state(phone, "imss_beneficios")
    send_message(
        phone,
        "🟩 *Préstamo IMSS Ley 73*\n"
        "Te ayudo a revisar si calificas para un préstamo con tasa preferencial. "
        "¿Te interesa conocer requisitos? (responde *sí* o *no*)."
    )

def imss_next(phone: str, text: str) -> None:
    """Procesa siguiente paso en embudo IMSS."""
    st = get_state(phone)
    data = get_data(phone)

    if st == "imss_beneficios":
        if interpret_response(text) == "positive":
            set_state(phone, "imss_pension")
            send_message(phone, "¿Cuál es tu *pensión mensual* aproximada?")
        else:
            send_message(
                phone,
                "Sin problema. Si deseas continuar después, escribe *1* o *imss*."
            )
            set_state(phone, "")
            
    elif st == "imss_pension":
        monto = extract_number(text)
        if not monto:
            send_message(phone, "Indícame un monto aproximado válido, por favor.")
            return
        data["imss_pension"] = monto
        set_state(phone, "imss_nombre")
        send_message(phone, "¿Cuál es tu *nombre completo*?")
        
    elif st == "imss_nombre":
        data["imss_nombre"] = text.strip()
        set_state(phone, "")
        send_message(
            phone,
            "✅ Gracias. Un asesor validará tu información y te contactará."
        )
        notify_advisor(
            f"🔔 Lead IMSS\nWhatsApp: {phone}\n"
            f"Nombre: {data.get('imss_nombre', '')}\n"
            f"Pensión: {data.get('imss_pension', '')}"
        )

def emp_start(phone: str, match: Optional[Dict[str, Any]]) -> None:
    """Inicia embudo empresarial."""
    set_state(phone, "emp_confirma")
    send_message(
        phone,
        "🏢 *Crédito Empresarial*\n"
        "¿Eres empresario(a) o representante de una empresa? (responde *sí* o *no*)."
    )

def emp_next(phone: str, text: str) -> None:
    """Procesa siguiente paso en embudo empresarial."""
    st = get_state(phone)
    data = get_data(phone)

    if st == "emp_confirma":
        if interpret_response(text) != "positive":
            send_message(
                phone,
                "Entendido. Si cambias de opinión, escribe *5* o *empresarial*."
            )
            set_state(phone, "")
            return
        set_state(phone, "emp_giro")
        send_message(phone, "¿A qué *se dedica* tu empresa?")
        
    elif st == "emp_giro":
        data["emp_giro"] = text.strip()
        set_state(phone, "emp_monto")
        send_message(phone, "¿Qué *monto* necesitas? (mínimo $100,000)")
        
    elif st == "emp_monto":
        monto = extract_number(text)
        if not monto or monto < 100000:
            send_message(
                phone,
                "El monto mínimo es $100,000. Indícame un monto igual o mayor."
            )
            return
        data["emp_monto"] = monto
        set_state(phone, "emp_nombre")
        send_message(phone, "¿Tu *nombre completo*?")
        
    elif st == "emp_nombre":
        data["emp_nombre"] = text.strip()
        set_state(phone, "emp_ciudad")
        send_message(phone, "¿En qué *ciudad* está tu empresa?")
        
    elif st == "emp_ciudad":
        data["emp_ciudad"] = text.strip()
        set_state(phone, "")
        resumen = (
            "✅ Gracias. Un asesor te contactará.\n"
            f"- Nombre: {data.get('emp_nombre', '')}\n"
            f"- Ciudad: {data.get('emp_ciudad', '')}\n"
            f"- Giro: {data.get('emp_giro', '')}\n"
            f"- Monto: ${data.get('emp_monto', 0):,.0f}"
        )
        send_message(phone, resumen)
        notify_advisor(f"🔔 Lead Empresarial\nWhatsApp: {phone}\n{resumen}")

def fp_start(phone: str, match: Optional[Dict[str, Any]]) -> None:
    """Inicia embudo financiamiento práctico."""
    set_state(phone, "fp_monto")
    send_message(
        phone, 
        "💳 *Financiamiento Práctico*\n¿Qué monto necesitas?"
    )

def fp_next(phone: str, text: str) -> None:
    """Procesa siguiente paso en financiamiento práctico."""
    st = get_state(phone)
    data = get_data(phone)

    if st == "fp_monto":
        monto = extract_number(text)
        if not monto:
            send_message(phone, "Indícame un monto válido, por favor.")
            return
        data["fp_monto"] = monto
        set_state(phone, "")
        send_message(phone, "✅ Gracias. Un asesor revisará tu solicitud.")
        notify_advisor(f"🔔 Lead Financiamiento Práctico\nWhatsApp: {phone}\nMonto: ${monto:,.0f}")

def auto_start(phone: str, match: Optional[Dict[str, Any]]) -> None:
    """Inicia embudo seguro de auto."""
    set_state(phone, "auto_intro")
    send_message(
        phone,
        "🚗 *Seguro de Auto*\n"
        "Envíame por favor:\n"
        "• Foto de tu INE\n"
        "• Tarjeta de circulación o placa\n"
        "• Si tienes póliza actual, foto donde se vea la fecha de vencimiento.\n"
        "Cuando lo envíes, te confirmaré recepción y procesaré la cotización."
    )

def auto_next(phone: str, text: str) -> None:
    """Procesa siguiente paso en seguro de auto."""
    st = get_state(phone)
    intent = interpret_response(text)

    if st == "auto_intro":
        if ("vencimiento" in text.lower() or "vence" in text.lower() or 
            "fecha" in text.lower()):
            set_state(phone, "auto_vencimiento_fecha")
            send_message(
                phone,
                "¿Cuál es la *fecha de vencimiento* de tu póliza actual? (AAAA-MM-DD)"
            )
        elif intent == "negative":
            set_state(phone, "auto_vencimiento_fecha")
            send_message(
                phone,
                "Entendido 👍 Para apoyarte cuando se acerque la fecha, dime "
                "¿cuándo vence tu póliza actual? (AAAA-MM-DD)"
            )
        else:
            send_message(
                phone,
                "Perfecto ✅ Puedes enviarme desde ahora las fotos de tus documentos para cotizar."
            )
            
    elif st == "auto_vencimiento_fecha":
        set_state(phone, "")
        send_message(
            phone,
            "✅ Gracias. Tomo nota de la fecha para recordarte antes del vencimiento."
        )
        notify_advisor(f"🔔 Cliente SECOM {phone} indicó fecha de vencimiento: {text}")

# ==========================
# Router principal
# ==========================
def route_command(phone: str, text: str, match: Optional[Dict[str, Any]]) -> None:
    """Enruta comandos y mensajes."""
    t = (text or "").strip().lower()

    # Comandos directos
    if t in ("1", "imss", "ley 73", "prestamo imss", "préstamo imss", "pension", "pensión"):
        imss_start(phone, match)
        return
        
    if t in ("2", "auto", "seguro auto", "seguro de auto"):
        auto_start(phone, match)
        return
        
    if t in ("3", "vida", "salud", "seguro de vida", "seguro de salud"):
        send_message(
            phone,
            "🧬 En breve un asesor te comparte opciones de Vida / Salud."
        )
        notify_advisor(f"🔔 Vida/Salud — Solicitud de contacto\nWhatsApp: {phone}")
        send_main_menu(phone)
        return
        
    if t in ("4", "vrim", "tarjeta medica", "tarjeta médica"):
        send_message(
            phone,
            "🩺 En breve un asesor te comparte información de la tarjeta médica VRIM."
        )
        notify_advisor(f"🔔 VRIM — Solicitud de contacto\nWhatsApp: {phone}")
        send_main_menu(phone)
        return
        
    if t in ("5", "empresarial", "credito empresarial", "crédito empresarial", "pyme"):
        emp_start(phone, match)
        return
        
    if t in ("6", "financiamiento practico", "financiamiento práctico", "credito simple", "crédito simple"):
        fp_start(phone, match)
        return
        
    if t in ("7", "contactar", "asesor", "contactar con christian"):
        notify_advisor(f"🔔 Contacto directo solicitado\nWhatsApp: {phone}")
        send_message(
            phone,
            "✅ Listo. Avisé a Christian para que te contacte personalmente."
        )
        send_main_menu(phone)
        return
        
    if t in ("menu", "menú", "inicio", "hola", "help", "ayuda"):
        set_state(phone, "")
        send_main_menu(phone)
        return

    # Revisar estado actual
    st = get_state(phone)
    intent = interpret_response(text)

    # Campaña SECOM Auto
    if st == "campaign_secom_auto":
        if intent == "positive":
            send_message(
                phone,
                "Perfecto ✅ Iniciemos con la revisión gratuita de tu seguro de auto."
            )
            set_state(phone, "")
            auto_start(phone, match)
        elif intent == "negative":
            send_message(
                phone,
                "Gracias por responder 🙌. Si más adelante deseas una revisión, escribe *2* o *auto*."
            )
            set_state(phone, "")
            send_main_menu(phone)
        else:
            send_message(
                phone,
                "Solo para confirmar, ¿te interesa la revisión gratuita de tu seguro de auto? "
                "Responde *sí* o *no*, o escribe *menú*."
            )
        return

    # Campaña IMSS Ley 73
    if st == "campaign_imss_ley73":
        if intent == "positive":
            send_message(
                phone,
                "Perfecto ✅ Revisemos tu opción de *Préstamo IMSS Ley 73*."
            )
            set_state(phone, "")
            imss_start(phone, match)
        elif intent == "negative":
            send_message(
                phone,
                "Entendido 🙌. Si luego te interesa, escribe *1* o *imss*."
            )
            set_state(phone, "")
            send_main_menu(phone)
        else:
            send_message(
                phone,
                "¿Te interesa que revisemos si calificas para un préstamo IMSS Ley 73? "
                "Responde *sí* o *no*, o escribe *menú*."
            )
        return

    # Flujos activos
    if st.startswith("imss_"):
        imss_next(phone, text)
    elif st.startswith("emp_"):
        emp_next(phone, text)
    elif st.startswith("fp_"):
        fp_next(phone, text)
    elif st.startswith("auto_"):
        auto_next(phone, text)
    else:
        # Sin estado → Intentar GPT primero
        gpt_response = gpt_process_message(phone, text, match, st)
        if gpt_response:
            send_message(phone, gpt_response)
            time.sleep(1)
            send_main_menu(phone)
        else:
            # Fallback al menú principal
            send_message(phone, "No entendí tu mensaje. ¿Te ayudo con alguna de estas opciones?")
            send_main_menu(phone)

# ==========================
# Webhook
# ==========================
@app.get("/webhook")
def webhook_verify():
    """Verifica webhook de Meta."""
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge", "")
    
    if mode == "subscribe" and token == VERIFY_TOKEN:
        log.info("✅ Webhook verificado correctamente")
        return challenge, 200
    
    log.warning("❌ Webhook verification failed")
    return "forbidden", 403

@app.post("/webhook")
def webhook_receive():
    """Recibe mensajes de WhatsApp."""
    try:
        # Verificar tamaño del payload
        if request.content_length and request.content_length > 1024 * 1024:  # 1MB
            log.warning("⚠️ Payload demasiado grande")
            return jsonify({"ok": True}), 200

        payload = request.get_json(force=True, silent=True) or {}
        
        # Log simplificado para evitar spam
        if payload.get("object") == "whatsapp_business_account":
            log.debug(f"📥 Webhook recibido: {payload.get('entry', [{}])[0].get('id', 'unknown')}")
        else:
            log.info(f"📥 Webhook recibido: {json.dumps(payload)[:300]}...")

        entry = (payload.get("entry") or [{}])[0]
        changes = (entry.get("changes") or [{}])[0]
        value = changes.get("value", {})
        messages = value.get("messages", [])

        # Si no hay messages (solo statuses), salimos
        if not messages:
            return jsonify({"ok": True}), 200

        msg = messages[0]
        phone = msg.get("from")
        if not phone:
            return jsonify({"ok": True}), 200

        # Registrar última actividad
        _touch_last_inbound(phone)

        match = match_client_in_sheets(phone)
        mtype = msg.get("type")

        if mtype == "text":
            text = msg.get("text", {}).get("body", "")
            log.info(f"💬 Texto de {phone}: {text!r}")

            # GPT directo (comando especial)
            if text.lower().startswith("sgpt:") and OPENAI_AVAILABLE and OPENAI_API_KEY:
                prompt = text.split("sgpt:", 1)[1].strip()
                try:
                    completion = openai.chat.completions.create(
                        model="gpt-4o-mini",
                        messages=[{"role": "user", "content": prompt}],
                        temperature=0.4,
                    )
                    answer = completion.choices[0].message.content.strip()
                    send_message(phone, answer)
                    return jsonify({"ok": True}), 200
                except Exception as e:
                    log.error(f"❌ Error OpenAI: {e}")
                    send_message(
                        phone,
                        "Hubo un detalle al procesar tu mensaje, intenta de nuevo."
                    )
                    return jsonify({"ok": True}), 200

            route_command(phone, text, match)
            return jsonify({"ok": True}), 200

        if mtype in ("image", "document", "audio", "video"):
            log.info(f"📎 Multimedia recibida de {phone}: {mtype}")
            send_message(
                phone,
                "✅ Archivo recibido. Lo revisaré junto con tu solicitud."
            )
            # Reenvía al asesor
            forward_media_to_advisor(phone, mtype, msg)
            return jsonify({"ok": True}), 200

        log.debug(f"ℹ️ Tipo de mensaje no manejado: {mtype}")
        return jsonify({"ok": True}), 200

    except Exception as e:
        log.error(f"❌ Error en webhook_receive: {e}")
        return jsonify({"ok": True}), 200

# ==========================
# Endpoints externos básicos
# ==========================
@app.get("/health")
def health():
    """Health check básico."""
    return jsonify({
        "status": "ok",
        "service": "Vicky Bot SECOM",
        "timestamp": datetime.utcnow().isoformat(),
    })

@app.get("/ext/health")
def ext_health():
    """Health check extendido."""
    return jsonify({
        "status": "ok",
        "whatsapp_configured": bool(META_TOKEN and WABA_PHONE_ID),
        "google_ready": google_ready,
        "openai_ready": bool(OPENAI_AVAILABLE and OPENAI_API_KEY),
        "advisor_number": bool(ADVISOR_NUMBER),
        "sheets_id": bool(SHEETS_ID_LEADS),
    })

@app.post("/ext/test-send")
def ext_test_send():
    """Endpoint para probar envíos con validación robusta."""
    try:
        data = request.get_json(force=True) or {}
        to = str(data.get("to", "")).strip()
        text = str(data.get("text", "")).strip()
        template_name = str(data.get("template_name") or "").strip()
        components = data.get("components") or []
        
        if not to:
            return jsonify({"ok": False, "error": "Falta 'to'"}), 400
        
        result = {}
        ok = False
        
        if template_name:
            # Enviar plantilla con delay para evitar saturación
            time.sleep(0.2)
            result = send_template_message(to, template_name, components)
            
            # Validación robusta del resultado
            if isinstance(result, dict):
                # Comprobar si hay error en la respuesta de Meta
                if "error" in str(result).lower():
                    ok = False
                elif "messages" in result:
                    # Respuesta exitosa de Meta
                    ok = True
                else:
                    # Otra respuesta inesperada
                    ok = False
            else:
                ok = False
                
            log_level = logging.INFO if ok else logging.ERROR
            log.log(log_level, f"📤 Test template '{template_name}' to {to}: {'✅' if ok else '❌'} - {result}")
        else:
            # Enviar texto normal
            text = text or "Prueba desde Vicky SECOM"
            ok = send_message(to, text)
            result = {"type": "text", "sent": ok}
            log.info(f"📤 Test text to {to}: {'✅' if ok else '❌'}")
            
        return jsonify({
            "ok": bool(ok),
            "result": result,
            "timestamp": datetime.utcnow().isoformat()
        }), 200 if ok else 500
        
    except Exception as e:
        log.error(f"❌ Error en /ext/test-send: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

# ==========================
# Worker envíos masivos manual
# ==========================
def _bulk_send_worker_updated(items: List[Dict[str, Any]]) -> None:
    """Worker para envíos masivos."""
    ok = 0
    fail = 0
    
    for i, item in enumerate(items, 1):
        try:
            to = str(item.get("to", "")).strip()
            text = str(item.get("text", "")).strip()
            template_name = str(item.get("template_name") or item.get("template", "")).strip()
            components = item.get("components") or []

            if not to:
                log.warning(f"⏭️ Item {i} inválido: falta 'to'")
                fail += 1
                continue

            if not text and not template_name:
                log.warning(f"⏭️ Item {i} inválido: falta 'text' y 'template_name'")
                fail += 1
                continue

            sent = False
            if template_name:
                # Enviar plantilla con delay
                time.sleep(0.2)
                result = send_template_message(to, template_name, components)
                sent = "error" not in str(result).lower()
                log.info(f"📤 [BULK] Plantilla '{template_name}' a {to} - {'✅' if sent else '❌'}")
            else:
                # Enviar texto
                sent = send_message(to, text)
                log.info(f"📤 [BULK] Texto a {to} - {'✅' if sent else '❌'}")

            if sent:
                ok += 1
                # Establecer estado de campaña si corresponde
                key = _normalize_phone(to)
                if key:
                    low_text = (text or "").lower()
                    if "cliente secom" in low_text and "seguro de auto" in low_text:
                        _user_state[key] = "campaign_secom_auto"
                    elif "préstamo imss" in low_text or "prestamo imss" in low_text:
                        _user_state[key] = "campaign_imss_ley73"
                    elif "campaign" in item:
                        _user_state[key] = f"campaign_{item['campaign']}"
            else:
                fail += 1

            time.sleep(0.5)  # Rate limiting

        except Exception as e:
            fail += 1
            log.error(f"❌ Error en item {i}: {e}")

    log.info(f"🎯 Envío masivo terminado: OK={ok}, FAIL={fail}, TOTAL={len(items)}")
    
    if ADVISOR_NUMBER:
        send_message(
            ADVISOR_NUMBER,
            f"📊 Envío masivo finalizado.\nExitosos: {ok}\nFallidos: {fail}\nTotal: {len(items)}"
        )

@app.post("/ext/send-promo")
def ext_send_promo():
    """Endpoint para campañas con soporte para plantillas Meta."""
    try:
        if not (META_TOKEN and WABA_PHONE_ID):
            return jsonify({
                "queued": False,
                "error": "WhatsApp API no configurada"
            }), 500

        data = request.get_json(force=True) or {}
        items = data.get("items", [])

        # Soporte para formato plano
        if not isinstance(items, list):
            if "template_name" in data or "to" in data:
                items = [data]
            else:
                return jsonify({
                    "queued": False,
                    "error": "Se requiere lista 'items' o campos directos 'to'/'template_name'"
                }), 400

        if not items:
            return jsonify({
                "queued": False,
                "error": "Lista 'items' vacía"
            }), 400

        # Validación básica
        for i, item in enumerate(items):
            to = str(item.get("to", "")).strip()
            text = str(item.get("text", "")).strip()
            template_name = str(item.get("template_name") or item.get("template", "")).strip()
            
            if not to:
                return jsonify({
                    "queued": False,
                    "error": f"Item {i+1} falta campo 'to'"
                }), 400
                
            if not text and not template_name:
                return jsonify({
                    "queued": False,
                    "error": f"Item {i+1} falta 'text' o 'template_name'"
                }), 400

        # Iniciar worker en segundo plano
        t = threading.Thread(
            target=_bulk_send_worker_updated,
            args=(items,),
            daemon=True
        )
        t.start()

        return jsonify({
            "queued": True,
            "count": len(items),
            "timestamp": datetime.utcnow().isoformat(),
            "note": "Procesando en segundo plano"
        }), 202

    except Exception as e:
        log.error(f"❌ Error en /ext/send-promo: {e}")
        return jsonify({"queued": False, "error": str(e)}), 500

# ==========================
# Envío masivo SECOM desde Sheets
# ==========================
def _bulk_send_from_sheets_worker_updated(
    message_template: str,
    template_name: str,
    components: List[Dict[str, Any]],
    use_sheet_message: bool,
    limit: Optional[int] = None,
) -> None:
    """Worker para envío masivo desde Google Sheets."""
    if not (google_ready and sheets and SHEETS_ID_LEADS and SHEETS_TITLE_LEADS):
        log.error("[SECOM-PROMO] Google Sheets no configurado")
        return

    try:
        headers, rows = _get_sheet_headers_and_rows()
        if not headers or not rows:
            log.warning("[SECOM-PROMO] Hoja vacía")
            return

        idx_name = _find_col(headers, ["Nombre", "CLIENTE", "Cliente"])
        idx_wa = _find_col(headers, ["WhatsApp", "Whatsapp", "WHATSAPP", "Telefono", "Teléfono", "CELULAR"])
        idx_status = _find_col(headers, ["Status", "ESTATUS"])
        idx_first = _find_col(headers, ["FirstSentAt", "FIRST_SENT_AT"])
        idx_msg_base = _find_col(headers, ["Mensaje_Base", "MENSAJE_BASE"])

        if idx_wa is None:
            log.error("[SECOM-PROMO] No se encontró columna de WhatsApp")
            return

        now_iso = datetime.utcnow().isoformat()
        enviados = 0
        fallidos = 0
        total = min(len(rows), limit) if limit else len(rows)

        for offset, row in enumerate(rows, start=2):
            if limit is not None and enviados >= limit:
                break

            if len(row) <= idx_wa:
                continue

            phone_raw = row[idx_wa]
            norm = _normalize_phone(str(phone_raw))
            if not norm:
                continue

            # Verificar si ya se envió
            first_val = row[idx_first] if idx_first is not None and len(row) > idx_first else ""
            if str(first_val).strip():
                continue

            # Verificar status
            status_val = row[idx_status] if idx_status is not None and len(row) > idx_status else ""
            status_up = str(status_val).strip().upper()
            if status_up in ("NO_INTERESADO", "NO INTERESADO", "CERRADO", "BLOQUEADO"):
                continue

            name = row[idx_name].strip() if idx_name is not None and len(row) > idx_name else ""

            to = str(phone_raw).strip()
            if not to.startswith("52"):
                to = f"52{norm}"

            sent = False
            
            if template_name:
                # Usar plantilla Meta
                current_components = components or []
                
                # Si hay nombre, agregarlo como parámetro
                if name and not current_components:
                    current_components = [{
                        "type": "body",
                        "parameters": [{"type": "text", "text": name}]
                    }]
                
                # Delay para evitar saturación
                time.sleep(0.2)
                result = send_template_message(to, template_name, current_components)
                sent = "error" not in str(result).lower()
                log.info(f"📤 [SECOM-PROMO] Plantilla '{template_name}' a {to} - {'✅' if sent else '❌'}")
                
            elif message_template or use_sheet_message:
                # Usar mensaje de texto
                msg = ""
                if use_sheet_message and idx_msg_base is not None and len(row) > idx_msg_base:
                    msg = str(row[idx_msg_base] or "").strip()
                if not msg:
                    msg = str(message_template or "").strip()
                if not msg:
                    continue

                msg = msg.replace("{{nombre}}", name if name else "Hola")
                sent = send_message(to, msg)
                log.info(f"📤 [SECOM-PROMO] Texto a {to} - {'✅' if sent else '❌'}")

            if sent:
                updates = {"FirstSentAt": now_iso}
                if idx_status is not None:
                    updates[headers[idx_status]] = "ENVIADO_INICIAL"
                _batch_update_cells(offset, updates, headers)
                enviados += 1
            else:
                fallidos += 1

            time.sleep(60)  # Rate limiting para evitar bloqueos

        log.info(f"[SECOM-PROMO] Finalizado. Enviados={enviados}, Fallidos={fallidos}, Total={total}")
        
        if ADVISOR_NUMBER:
            send_message(
                ADVISOR_NUMBER,
                f"📊 Envío masivo SECOM finalizado.\nExitosos: {enviados}\nFallidos: {fallidos}\nTotal procesados: {total}"
            )

    except Exception as e:
        log.error(f"❌ Error en _bulk_send_from_sheets_worker_updated: {e}")

@app.post("/ext/send-promo-secom")
def ext_send_promo_secom():
    """Endpoint para envío masivo SECOM desde Google Sheets."""
    try:
        if not (META_TOKEN and WABA_PHONE_ID):
            return jsonify({"ok": False, "error": "WhatsApp API no configurada"}), 500
        if not google_ready:
            return jsonify({"ok": False, "error": "Google Sheets no configurado"}), 500

        data = request.get_json(force=True) or {}
        message_template = (data.get("message") or "").strip()
        template_name = (data.get("template_name") or "").strip()
        components = data.get("components") or []
        use_sheet_message = bool(data.get("use_sheet_message", True))
        limit = data.get("limit")

        if not message_template and not template_name:
            return jsonify({
                "ok": False,
                "error": "Debes enviar 'message' o 'template_name'."
            }), 400

        # Iniciar worker en segundo plano
        t = threading.Thread(
            target=_bulk_send_from_sheets_worker_updated,
            args=(message_template, template_name, components, use_sheet_message, limit),
            daemon=True
        )
        t.start()

        return jsonify({
            "ok": True,
            "status": "queued",
            "timestamp": datetime.utcnow().isoformat(),
            "note": "Procesando en segundo plano (1 mensaje/minuto)"
        }), 202

    except Exception as e:
        log.error(f"❌ Error en /ext/send-promo-secom: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

# ==========================
# Recordatorios 3 y 5 días
# ==========================
def _parse_iso(dt_str: str) -> Optional[datetime]:
    """Parsea string ISO a datetime."""
    if not dt_str:
        return None
    try:
        return datetime.fromisoformat(str(dt_str).replace("Z", "+00:00").replace("Z", ""))
    except ValueError:
        return None

def _start_reminders_worker() -> None:
    """Worker que revisa y envía recordatorios cada hora."""
    if not google_ready:
        log.info("[REMINDERS] Google Sheets no configurado - worker no iniciado")
        return

    def worker():
        log.info("[REMINDERS] Worker iniciado")
        while True:
            try:
                headers, rows = _get_sheet_headers_and_rows()
                if not headers or not rows:
                    time.sleep(3600)
                    continue

                idx_wa = _find_col(headers, ["WhatsApp", "Whatsapp", "WHATSAPP", "Telefono", "Teléfono", "CELULAR"])
                idx_status = _find_col(headers, ["Status", "ESTATUS"])
                idx_first = _find_col(headers, ["FirstSentAt", "FIRST_SENT_AT"])
                idx_rem3 = _find_col(headers, ["Reminder3Sent", "REMINDER3", "REM3"])
                idx_rem5 = _find_col(headers, ["Reminder5Sent", "REMINDER5", "REM5"])
                idx_last = _find_col(headers, ["LastInboundAt", "LAST_INBOUND_AT", "LAST_MESSAGE_AT"])
                idx_name = _find_col(headers, ["Nombre", "CLIENTE", "Cliente"])

                if idx_wa is None or idx_first is None:
                    time.sleep(3600)
                    continue

                now = datetime.utcnow()
                reminders_sent = 0

                for offset, row in enumerate(rows, start=2):
                    if len(row) <= idx_wa:
                        continue

                    phone_raw = row[idx_wa]
                    norm = _normalize_phone(str(phone_raw))
                    if not norm:
                        continue

                    first_val = row[idx_first] if len(row) > idx_first else ""
                    first_dt = _parse_iso(str(first_val).strip())
                    if not first_dt:
                        continue

                    # Verificar status
                    status_val = row[idx_status] if idx_status is not None and len(row) > idx_status else ""
                    status_up = str(status_val).strip().upper()
                    if status_up in ("NO_INTERESADO", "NO INTERESADO", "CERRADO", "BLOQUEADO"):
                        continue

                    days = (now - first_dt).days

                    rem3_val = row[idx_rem3] if idx_rem3 is not None and len(row) > idx_rem3 else ""
                    rem5_val = row[idx_rem5] if idx_rem5 is not None and len(row) > idx_rem5 else ""

                    last_in_val = row[idx_last] if idx_last is not None and len(row) > idx_last else ""
                    last_in_dt = _parse_iso(str(last_in_val).strip())

                    # Considerar inactivo si nunca respondió o respondió antes del primer envío
                    inactive = (last_in_dt is None) or (last_in_dt <= first_dt)

                    name = row[idx_name] if idx_name is not None and len(row) > idx_name else "Hola"
                    name = str(name).strip() or "Hola"

                    to = str(phone_raw).strip()
                    if not to.startswith("52"):
                        to = f"52{norm}"

                    # Recordatorio 3 días
                    if days >= 3 and str(rem3_val).strip().upper() != "YES" and inactive:
                        msg3 = (
                            f"{name}, solo para recordarte que tenemos lista tu propuesta de seguro de auto "
                            "con beneficio especial para ti. Si gustas te ayudo a revisarla por aquí mismo. 🚗"
                        )
                        if send_message(to, msg3):
                            updates = {"Reminder3Sent": "YES"}
                            if idx_status is not None:
                                updates[headers[idx_status]] = "RECORDATORIO_3D"
                            _batch_update_cells(offset, updates, headers)
                            reminders_sent += 1
                            log.info(f"[REMINDERS] Recordatorio 3d enviado a {to}")

                    # Recordatorio 5 días
                    if days >= 5 and str(rem5_val).strip().upper() != "YES" and inactive:
                        msg5 = (
                            f"{name}, confirmo si aún te interesa aprovechar tu beneficio preferencial "
                            "en tu seguro de auto. Si quieres retomamos tu trámite por este medio. ✅"
                        )
                        if send_message(to, msg5):
                            updates = {"Reminder5Sent": "YES"}
                            if idx_status is not None:
                                updates[headers[idx_status]] = "RECORDATORIO_5D"
                            _batch_update_cells(offset, updates, headers)
                            reminders_sent += 1
                            log.info(f"[REMINDERS] Recordatorio 5d enviado a {to}")

                if reminders_sent > 0:
                    log.info(f"[REMINDERS] Enviados {reminders_sent} recordatorios en este ciclo")

                time.sleep(3600)  # Esperar 1 hora

            except Exception as e:
                log.error(f"❌ Error en ciclo de recordatorios: {e}")
                time.sleep(3600)

    # Iniciar worker en segundo plano
    threading.Thread(target=worker, daemon=True).start()

# Iniciar worker de recordatorios
_start_reminders_worker()

# ==========================
# Arranque local
# ==========================
if __name__ == "__main__":
    log.info(f"🚀 Iniciando Vicky Bot SECOM en puerto {PORT}")
    log.info(f"📞 WhatsApp configurado: {bool(META_TOKEN and WABA_PHONE_ID)}")
    log.info(f"📊 Google Sheets listo: {google_ready}")
    log.info(f"🧠 OpenAI listo: {bool(OPENAI_AVAILABLE and OPENAI_API_KEY)}")
    log.info(f"👤 Asesor: {ADVISOR_NUMBER or 'No configurado'}")
    
    app.run(host="0.0.0.0", port=PORT, debug=False)
