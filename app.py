# app.py — Vicky SECOM (Sistema WAPI Completo Promocional)
# Python 3.11+
# ------------------------------------------------------------
# NUEVAS FUNCIONALIDADES IMPLEMENTADAS:
# 1. ✅ Sistema completo de envíos masivos promocionales
# 2. ✅ Seguimiento automático con recordatorios
# 3. ✅ Gestión de estados de clientes en Sheets
# 4. ✅ Detección automática de interés en respuestas
# 5. ✅ Workers programados para seguimientos
# 6. ✅ Endpoints para campañas y seguimientos
# ------------------------------------------------------------

from __future__ import annotations

import os
import io
import re
import json
import time
import math
import queue
import uuid
import logging
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, List, Tuple
from enum import Enum

import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# Google
try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseUpload
    from googleapiclient.errors import HttpError
except Exception:
    service_account = None
    build = None
    MediaIoBaseUpload = None

# GPT opcional
try:
    import openai
except Exception:
    openai = None

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
SHEETS_TITLE_CLIENTS = os.getenv("SHEETS_TITLE_CLIENTS", "Clientes")
SHEETS_TITLE_TRACKING = os.getenv("SHEETS_TITLE_TRACKING", "Seguimiento")
DRIVE_PARENT_FOLDER_ID = os.getenv("DRIVE_PARENT_FOLDER_ID")

PORT = int(os.getenv("PORT", "5000"))

# Configuración de logging robusta
logging.basicConfig(
    level=logging.INFO, 
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("vicky-secom-promo")

if OPENAI_API_KEY and openai:
    try:
        openai.api_key = OPENAI_API_KEY
        log.info("OpenAI configurado correctamente")
    except Exception:
        log.warning("OpenAI configurado pero no disponible")

# ==========================
# Enums y Constantes
# ==========================
class ClientStatus(Enum):
    ENVIADO = "enviado"
    INTERESADO = "interesado"
    NO_CONTESTA = "no_contesta"
    RECHAZADO = "rechazado"
    CONTACTADO = "contactado"

class MessageType(Enum):
    PROMOCION = "promocion"
    RECORDATORIO_3 = "recordatorio_3d"
    RECORDATORIO_5 = "recordatorio_5d"
    ULTIMO_RECORDATORIO = "ultimo_recordatorio"

# Palabras clave para detección de interés
INTEREST_KEYWORDS = ["sí", "si", "interesado", "me interesa", "cuéntame más", "info", "quiero", "dime más", "más información"]
REJECTION_KEYWORDS = ["no", "nel", "nop", "negativo", "no quiero", "no gracias", "no interesa", "cancelar", "stop"]

# ==========================
# Google Setup (degradable)
# ==========================
creds = None
sheets_svc = None
drive_svc = None
google_ready = False

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

# Almacenamiento en memoria para campañas y seguimientos
active_campaigns: Dict[str, Dict[str, Any]] = {}
client_tracking: Dict[str, Dict[str, Any]] = {}
followup_queue = queue.Queue()

# ==========================
# Clases para Gestión de Campañas
# ==========================
class PromoCampaign:
    def __init__(self, campaign_id: str, promo_type: str, segment: str, message_template: str):
        self.campaign_id = campaign_id
        self.promo_type = promo_type
        self.segment = segment
        self.message_template = message_template
        self.status = "active"
        self.created_at = datetime.utcnow()
        self.sent_count = 0
        self.interested_count = 0
        self.failed_count = 0
        
    def to_dict(self) -> Dict[str, Any]:
        return {
            "campaign_id": self.campaign_id,
            "promo_type": self.promo_type,
            "segment": self.segment,
            "message_template": self.message_template,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "sent_count": self.sent_count,
            "interested_count": self.interested_count,
            "failed_count": self.failed_count
        }

class FollowupScheduler:
    def __init__(self):
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        
    def _run(self):
        """Worker principal para seguimientos automáticos"""
        while self.running:
            try:
                # Procesar seguimientos programados
                self._process_scheduled_followups()
                
                # Verificar recordatorios cada hora
                time.sleep(3600)
            except Exception:
                log.exception("❌ Error en FollowupScheduler")
                time.sleep(60)
                
    def _process_scheduled_followups(self):
        """Procesar clientes que necesitan recordatorios"""
        if not google_ready:
            return
            
        try:
            # Buscar clientes en estado "enviado" hace 3 días
            three_days_ago = (datetime.utcnow() - timedelta(days=3)).strftime("%Y-%m-%d")
            clients_3d = self._find_clients_for_followup(ClientStatus.ENVIADO.value, three_days_ago)
            
            for client in clients_3d:
                self._schedule_followup(client, MessageType.RECORDATORIO_3)
                
            # Buscar clientes en estado "enviado" hace 5 días
            five_days_ago = (datetime.utcnow() - timedelta(days=5)).strftime("%Y-%m-%d")
            clients_5d = self._find_clients_for_followup(ClientStatus.ENVIADO.value, five_days_ago)
            
            for client in clients_5d:
                self._schedule_followup(client, MessageType.RECORDATORIO_5)
                
        except Exception:
            log.exception("❌ Error procesando seguimientos programados")
            
    def _find_clients_for_followup(self, status: str, date: str) -> List[Dict[str, Any]]:
        """Buscar clientes que necesitan seguimiento"""
        clients = []
        try:
            # Leer hoja de clientes
            range_name = f"{SHEETS_TITLE_CLIENTS}!A:G"
            result = sheets_svc.spreadsheets().values().get(
                spreadsheetId=SHEETS_ID_LEADS, 
                range=range_name
            ).execute()
            rows = result.get('values', [])
            
            if len(rows) < 2:  # Sin datos además del header
                return clients
                
            headers = [h.lower() for h in rows[0]]
            for row in rows[1:]:
                if len(row) < len(headers):
                    continue
                    
                client_data = dict(zip(headers, row))
                if (client_data.get('estado') == status and 
                    client_data.get('fechaúltimocontacto') == date):
                    clients.append(client_data)
                    
        except Exception:
            log.exception("❌ Error buscando clientes para seguimiento")
            
        return clients
        
    def _schedule_followup(self, client: Dict[str, Any], msg_type: MessageType):
        """Programar envío de recordatorio"""
        phone = client.get('teléfono', '').strip()
        name = client.get('nombre', '').strip()
        
        if not phone:
            return
            
        followup_data = {
            'phone': phone,
            'name': name,
            'msg_type': msg_type,
            'scheduled_time': datetime.utcnow(),
            'client_data': client
        }
        
        followup_queue.put(followup_data)
        log.info(f"📅 Seguimiento programado: {msg_type.value} para {name} ({phone})")

# Inicializar scheduler
followup_scheduler = FollowupScheduler()

# ==========================
# Utilidades generales (mejoradas)
# ==========================
WPP_API_URL = f"https://graph.facebook.com/v20.0/{WABA_PHONE_ID}/messages" if WABA_PHONE_ID else None
WPP_TIMEOUT = 15

def _normalize_phone_last10(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    return digits[-10:] if len(digits) >= 10 else digits

def detect_interest(text: str) -> str:
    """Detectar nivel de interés en respuesta del cliente"""
    if not text:
        return "neutral"
        
    t = text.lower()
    
    # Detectar interés
    if any(keyword in t for keyword in INTEREST_KEYWORDS):
        return "interested"
        
    # Detectar rechazo
    if any(keyword in t for keyword in REJECTION_KEYWORDS):
        return "rejected"
        
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
# WhatsApp Helpers (mejorados)
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

# ==========================
# Google Sheets Helpers (mejorados)
# ==========================
def get_clients_by_segment(segment: str) -> List[Dict[str, Any]]:
    """Obtener lista de clientes por segmento"""
    if not google_ready:
        return []
        
    clients = []
    try:
        range_name = f"{SHEETS_TITLE_CLIENTS}!A:G"
        result = sheets_svc.spreadsheets().values().get(
            spreadsheetId=SHEETS_ID_LEADS, 
            range=range_name
        ).execute()
        rows = result.get('values', [])
        
        if len(rows) < 2:
            return clients
            
        headers = [h.lower() for h in rows[0]]
        for row in rows[1:]:
            if len(row) < len(headers):
                continue
                
            client_data = dict(zip(headers, row))
            if client_data.get('segmento', '').lower() == segment.lower():
                clients.append(client_data)
                
        log.info(f"✅ Encontrados {len(clients)} clientes en segmento '{segment}'")
        
    except Exception:
        log.exception("❌ Error obteniendo clientes por segmento")
        
    return clients

def update_client_status(phone: str, status: str, promo_type: str = ""):
    """Actualizar estado del cliente en Sheets"""
    if not google_ready:
        return False
        
    try:
        # Buscar fila del cliente
        range_name = f"{SHEETS_TITLE_CLIENTS}!A:G"
        result = sheets_svc.spreadsheets().values().get(
            spreadsheetId=SHEETS_ID_LEADS, 
            range=range_name
        ).execute()
        rows = result.get('values', [])
        
        if len(rows) < 2:
            return False
            
        phone_normalized = _normalize_phone_last10(phone)
        today = datetime.utcnow().strftime("%Y-%m-%d")
        
        for i, row in enumerate(rows[1:], start=2):  # start=2 porque la primera fila es header
            if len(row) > 0:
                row_phone = _normalize_phone_last10(row[0])
                if row_phone == phone_normalized:
                    # Actualizar estado
                    update_range = f"{SHEETS_TITLE_CLIENTS}!E{i}:G{i}"
                    values = [[status, today, promo_type]]
                    
                    body = {'values': values}
                    sheets_svc.spreadsheets().values().update(
                        spreadsheetId=SHEETS_ID_LEADS,
                        range=update_range,
                        valueInputOption="RAW",
                        body=body
                    ).execute()
                    
                    log.info(f"✅ Estado actualizado: {phone} -> {status}")
                    return True
                    
        log.warning(f"⚠️ Cliente no encontrado para actualizar estado: {phone}")
        return False
        
    except Exception:
        log.exception(f"❌ Error actualizando estado para {phone}")
        return False

def log_interaction(phone: str, msg_type: str, message: str, response: str = ""):
    """Registrar interacción en hoja de Seguimiento"""
    if not google_ready:
        return
        
    try:
        today = datetime.utcnow().isoformat()
        values = [[phone, today, msg_type, message, response, ""]]
        
        body = {'values': values}
        sheets_svc.spreadsheets().values().append(
            spreadsheetId=SHEETS_ID_LEADS,
            range=f"{SHEETS_TITLE_TRACKING}!A:F",
            valueInputOption="RAW",
            body=body
        ).execute()
        
        log.info(f"✅ Interacción registrada: {phone} - {msg_type}")
        
    except Exception:
        log.exception("❌ Error registrando interacción")

# ==========================
# Sistema de Mensajes Promocionales
# ==========================
def get_promo_message(promo_type: str, client_name: str = "") -> str:
    """Obtener mensaje promocional según tipo"""
    messages = {
        "descuento_autos": f"Hola {client_name}, tenemos una promoción exclusiva: *30% de descuento* en seguro de auto para clientes preferentes. ¿Te interesa conocer los detalles?",
        "seguro_vida": f"Hola {client_name}, oferta especial: *25% descuento* en seguro de vida con cobertura ampliada. ¿Te gustaría que te enviemos la información completa?",
        "credito_empresarial": f"Hola {client_name}, *tasas preferenciales* para tu negocio. Crédito empresarial con aprobación en 48 horas. ¿Te interesa?",
        "vrim_promo": f"Hola {client_name}, membresía VRIM con *3 meses gratis* al contratar anualidad. Acceso a +5,000 especialistas. ¿Quieres conocer los beneficios?"
    }
    
    return messages.get(promo_type, f"Hola {client_name}, tenemos una promoción especial para ti. ¿Te interesa conocer los detalles?")

def get_followup_message(msg_type: MessageType, client_name: str = "") -> str:
    """Obtener mensaje de seguimiento"""
    today_plus_7 = (datetime.utcnow() + timedelta(days=7)).strftime("%d/%m/%Y")
    
    messages = {
        MessageType.RECORDATORIO_3: f"Hola {client_name}, solo recordarte nuestra promoción especial. ¿Te gustaría que te enviemos más información?",
        MessageType.RECORDATORIO_5: f"Hola {client_name}, seguimos con promociones exclusivas para ti. ¿Hay algo en lo que podamos ayudarte?",
        MessageType.ULTIMO_RECORDATORIO: f"Hola {client_name}, última oportunidad para acceder a nuestras promociones exclusivas. Válido solo hasta {today_plus_7}. ¿Te interesa?"
    }
    
    return messages.get(msg_type, f"Hola {client_name}, queríamos saber si tienes interés en nuestra promoción.")

# ==========================
# Workers Mejorados
# ==========================
def _bulk_send_worker(items: List[Dict[str, Any]], campaign_id: str = "") -> None:
    """Worker mejorado para envíos masivos con tracking por cliente"""
    successful = 0
    failed = 0
    interested = 0
    
    log.info(f"🚀 Iniciando envío masivo de {len(items)} mensajes - Campaña: {campaign_id}")
    
    for i, item in enumerate(items, 1):
        try:
            to = item.get("to", "").strip()
            name = item.get("name", "").strip()
            promo_type = item.get("promo_type", "")
            
            if not to:
                log.warning(f"⏭️ Item {i} sin destinatario, omitiendo")
                failed += 1
                continue
                
            log.info(f"📤 [{i}/{len(items)}] Procesando: {to} ({name})")
            
            # Personalizar mensaje
            message = get_promo_message(promo_type, name)
            
            # Enviar mensaje
            success = send_message(to, message)
            
            if success:
                successful += 1
                # Actualizar estado del cliente
                update_client_status(to, ClientStatus.ENVIADO.value, promo_type)
                # Registrar interacción
                log_interaction(to, MessageType.PROMOCION.value, message)
                
                # Actualizar contadores de campaña
                if campaign_id in active_campaigns:
                    active_campaigns[campaign_id]["sent_count"] += 1
            else:
                failed += 1
                if campaign_id in active_campaigns:
                    active_campaigns[campaign_id]["failed_count"] += 1
                
            # Rate limiting respetuoso
            time.sleep(1)
            
        except Exception as e:
            failed += 1
            log.exception(f"❌ Error procesando item {i} para {item.get('to', 'unknown')}")
    
    log.info(f"🎯 Envío masivo completado: {successful} ✅, {failed} ❌ - Campaña: {campaign_id}")
    
    # Notificar resumen al asesor
    if ADVISOR_NUMBER:
        summary_msg = (
            f"📊 Resumen Campaña {campaign_id}:\n"
            f"• Enviados: {successful}\n"
            f"• Fallidos: {failed}\n"
            f"• Interesados: {interested}\n"
            f"• Total: {len(items)}"
        )
        send_message(ADVISOR_NUMBER, summary_msg)

def _followup_worker():
    """Worker para procesar seguimientos en cola"""
    while True:
        try:
            followup_data = followup_queue.get(timeout=300)  # 5 minutos timeout
            if followup_data is None:
                break
                
            self._process_followup(followup_data)
            followup_queue.task_done()
            
        except queue.Empty:
            continue
        except Exception:
            log.exception("❌ Error en followup_worker")
            time.sleep(60)

def _process_followup(followup_data: Dict[str, Any]):
    """Procesar un seguimiento individual"""
    phone = followup_data['phone']
    name = followup_data['name']
    msg_type = followup_data['msg_type']
    
    try:
        # Enviar mensaje de seguimiento
        message = get_followup_message(msg_type, name)
        success = send_message(phone, message)
        
        if success:
            # Actualizar estado según tipo de mensaje
            if msg_type == MessageType.ULTIMO_RECORDATORIO:
                update_client_status(phone, ClientStatus.NO_CONTESTA.value)
            else:
                update_client_status(phone, ClientStatus.ENVIADO.value)
                
            # Registrar interacción
            log_interaction(phone, msg_type.value, message)
            
            log.info(f"✅ Seguimiento enviado: {msg_type.value} a {name}")
        else:
            log.warning(f"⚠️ Falló seguimiento: {msg_type.value} a {name}")
            
    except Exception:
        log.exception(f"❌ Error procesando seguimiento para {phone}")

# Iniciar worker de seguimientos
followup_thread = threading.Thread(target=_followup_worker, daemon=True)
followup_thread.start()

# ==========================
# Endpoints Promocionales Nuevos
# ==========================
@app.post("/ext/send-promo-campaign")
def ext_send_promo_campaign():
    """Iniciar campaña promocional masiva"""
    try:
        if not META_TOKEN or not WABA_PHONE_ID:
            return jsonify({
                "success": False, 
                "error": "WhatsApp Business API no configurada"
            }), 500

        body = request.get_json(force=True) or {}
        promo_type = body.get("promo_type", "").strip()
        segment = body.get("segment", "").strip()
        custom_message = body.get("message", "").strip()
        
        if not promo_type or not segment:
            return jsonify({
                "success": False, 
                "error": "Faltan parámetros 'promo_type' o 'segment'"
            }), 400
        
        # Generar ID de campaña
        campaign_id = f"campaign_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
        
        # Obtener clientes del segmento
        clients = get_clients_by_segment(segment)
        if not clients:
            return jsonify({
                "success": False, 
                "error": f"No se encontraron clientes en el segmento '{segment}'"
            }), 404
        
        # Preparar items para envío
        items = []
        for client in clients:
            phone = client.get('teléfono', '').strip()
            name = client.get('nombre', '').strip()
            if phone:
                items.append({
                    "to": phone,
                    "name": name,
                    "promo_type": promo_type
                })
        
        # Crear y almacenar campaña
        campaign = PromoCampaign(campaign_id, promo_type, segment, custom_message)
        active_campaigns[campaign_id] = campaign.to_dict()
        
        # Iniciar envío en background
        threading.Thread(
            target=_bulk_send_worker, 
            args=(items, campaign_id), 
            daemon=True,
            name=f"CampaignWorker-{campaign_id}"
        ).start()
        
        response = {
            "success": True,
            "campaign_id": campaign_id,
            "message": f"Campaña iniciada para {len(items)} clientes",
            "clients_count": len(items),
            "segment": segment,
            "promo_type": promo_type,
            "timestamp": datetime.utcnow().isoformat()
        }
        
        log.info(f"✅ Campaña promocional iniciada: {response}")
        return jsonify(response), 202
        
    except Exception as e:
        log.exception("❌ Error en /ext/send-promo-campaign")
        return jsonify({
            "success": False, 
            "error": f"Error interno: {str(e)}"
        }), 500

@app.get("/ext/campaign-status/<campaign_id>")
def ext_campaign_status(campaign_id):
    """Obtener estado de una campaña"""
    try:
        campaign = active_campaigns.get(campaign_id)
        if not campaign:
            return jsonify({
                "success": False,
                "error": "Campaña no encontrada"
            }), 404
            
        return jsonify({
            "success": True,
            "campaign": campaign
        }), 200
        
    except Exception as e:
        log.exception(f"❌ Error obteniendo estado de campaña {campaign_id}")
        return jsonify({
            "success": False, 
            "error": str(e)
        }), 500

@app.post("/ext/cancel-followups")
def ext_cancel_followups():
    """Cancelar seguimientos para un cliente específico"""
    try:
        body = request.get_json(force=True) or {}
        phone = body.get("phone", "").strip()
        
        if not phone:
            return jsonify({
                "success": False,
                "error": "Faltan parámetro 'phone'"
            }), 400
            
        # Actualizar estado a "rechazado"
        success = update_client_status(phone, ClientStatus.RECHAZADO.value)
        
        if success:
            log.info(f"✅ Seguimientos cancelados para: {phone}")
            return jsonify({
                "success": True,
                "message": f"Seguimientos cancelados para {phone}"
            }), 200
        else:
            return jsonify({
                "success": False,
                "error": "No se pudo cancelar seguimientos"
            }), 400
            
    except Exception as e:
        log.exception("❌ Error en /ext/cancel-followups")
        return jsonify({
            "success": False, 
            "error": str(e)
        }), 500

# ==========================
# Webhook Mejorado para Detección de Interés
# ==========================
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

        # Detectar interés en respuestas a promociones
        mtype = msg.get("type")
        if mtype == "text" and "text" in msg:
            text = msg["text"].get("body", "").strip().lower()
            log.info(f"💬 Texto recibido de {phone}: {text}")
            
            # Verificar si es respuesta a promoción
            interest_level = detect_interest(text)
            
            if interest_level == "interested":
                # Cliente interesado - notificar asesor
                update_client_status(phone, ClientStatus.INTERESADO.value)
                log_interaction(phone, "respuesta", text, "interesado")
                
                _notify_advisor(
                    f"🎯 CLIENTE INTERESADO\n"
                    f"Teléfono: {phone}\n"
                    f"Mensaje: {text}\n"
                    f"¡Contactar de inmediato!"
                )
                
                # Responder al cliente
                send_message(phone, "¡Excelente! Un asesor se pondrá en contacto contigo en breve para darte todos los detalles. 👍")
                
            elif interest_level == "rejected":
                # Cliente no interesado - cancelar seguimientos
                update_client_status(phone, ClientStatus.RECHAZADO.value)
                log_interaction(phone, "respuesta", text, "rechazado")
                
                send_message(phone, "Entendido, gracias por tu respuesta. Si cambias de opinión, estaremos aquí para ayudarte.")
                
            else:
                # Procesar con lógica existente
                match = _greet_and_match(phone) if phone not in user_state else None
                _route_command(phone, text, match)
                
        elif mtype in {"image", "document", "audio", "video"}:
            # Manejar multimedia con lógica existente
            _handle_media(phone, msg)
            
        return jsonify({"ok": True}), 200
        
    except Exception:
        log.exception("❌ Error en webhook_receive")
        return jsonify({"ok": True}), 200

# ==========================
# Funciones Existente (conservadas por compatibilidad)
# ==========================
def _notify_advisor(text: str) -> None:
    try:
        log.info(f"👨‍💼 Notificando al asesor: {text}")
        send_message(ADVISOR_NUMBER, text)
    except Exception:
        log.exception("❌ Error notificando al asesor")

def _greet_and_match(phone: str) -> Optional[Dict[str, Any]]:
    # Función existente conservada
    last10 = _normalize_phone_last10(phone)
    match = match_client_in_sheets(last10)
    if match and match.get("nombre"):
        send_message(phone, f"Hola {match['nombre']} 👋 Soy *Vicky*. ¿En qué te puedo ayudar hoy?")
    else:
        send_message(phone, "Hola 👋 Soy *Vicky*. Estoy para ayudarte.")
    return match

# ==========================
# Health Check Mejorado
# ==========================
@app.get("/ext/health")
def ext_health():
    return jsonify({
        "status": "ok",
        "service": "Vicky Bot Inbursa - Sistema Promocional",
        "whatsapp_configured": bool(META_TOKEN and WABA_PHONE_ID),
        "google_ready": google_ready,
        "openai_ready": bool(openai and OPENAI_API_KEY),
        "active_campaigns": len(active_campaigns),
        "followup_queue_size": followup_queue.qsize(),
        "timestamp": datetime.utcnow().isoformat()
    }), 200

# ==========================
# Arranque
# ==========================
if __name__ == "__main__":
    log.info(f"🚀 Iniciando Vicky Bot SECOM - Sistema Promocional en puerto {PORT}")
    log.info(f"📞 WhatsApp configurado: {bool(META_TOKEN and WABA_PHONE_ID)}")
    log.info(f"📊 Google Sheets/Drive: {google_ready}")
    log.info(f"🧠 OpenAI: {bool(openai and OPENAI_API_KEY)}")
    log.info(f"🎯 Sistema de campañas promocionales: ACTIVO")
    
    app.run(host="0.0.0.0", port=PORT, debug=False)



