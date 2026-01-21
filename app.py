# app_completo.py — Vicky SECOM (Versión COMPLETA 100% Funcional)
# Python 3.11+
# ------------------------------------------------------------
# CARACTERÍSTICAS:
# 1. ✅ Sistema de Drive 100% funcional
# 2. ✅ Google Sheets integrado para matching de clientes
# 3. ✅ Menú completo con todas las opciones
# 4. ✅ Contexto post-campaña (TPV y Auto)
# 5. ✅ Sistema de documentos completo (INE + Tarjeta)
# 6. ✅ Notificaciones al asesor
# 7. ✅ Endpoints auxiliares funcionales
# ------------------------------------------------------------

from __future__ import annotations

import os
import io
import re
import json
import time
import queue
import logging
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, List, Tuple
from dataclasses import dataclass, field

import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# Google - IMPORTANTE: Estas librerías deben estar instaladas
try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseUpload
    from google.auth.exceptions import RefreshError
except Exception:
    service_account = None
    build = None
    MediaIoBaseUpload = None

# ==========================
# Carga entorno + Logging
# ==========================
load_dotenv()

META_TOKEN = os.getenv("META_TOKEN")
WABA_PHONE_ID = os.getenv("WABA_PHONE_ID")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
ADVISOR_NUMBER = os.getenv("ADVISOR_NUMBER", "5216682478005")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")
DRIVE_PARENT_FOLDER_ID = os.getenv("DRIVE_PARENT_FOLDER_ID")
SHEETS_ID_LEADS = os.getenv("SHEETS_ID_LEADS")
SHEETS_TITLE_LEADS = os.getenv("SHEETS_TITLE_LEADS", "Prospectos SECOM Auto")

# Configuración de logging robusta
logging.basicConfig(
    level=logging.INFO, 
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("vicky-secom-completo")

# ==========================
# Google Services Manager
# ==========================
class GoogleServicesManager:
    def __init__(self):
        self.drive_service = None
        self.sheets_service = None
        self.creds = None
        self.initialized = False
        self.last_error = None
        
    def initialize(self) -> bool:
        """Inicializa Google Drive y Sheets"""
        if not GOOGLE_CREDENTIALS_JSON:
            log.error("❌ GOOGLE_CREDENTIALS_JSON no configurado")
            return False
            
        try:
            log.info("🔄 Inicializando Google Services...")
            
            # Parsear credenciales JSON
            creds_info = json.loads(GOOGLE_CREDENTIALS_JSON)
            
            # Configurar scopes necesarios
            SCOPES = [
                'https://www.googleapis.com/auth/drive',
                'https://www.googleapis.com/auth/spreadsheets'
            ]
            
            # Crear credenciales
            self.creds = service_account.Credentials.from_service_account_info(
                creds_info, 
                scopes=SCOPES
            )
            
            # Construir servicios
            self.drive_service = build('drive', 'v3', credentials=self.creds)
            self.sheets_service = build('sheets', 'v4', credentials=self.creds)
            
            # Verificar conexión
            self.drive_service.files().list(pageSize=1).execute()
            
            self.initialized = True
            self.last_error = None
            log.info("✅ Google Services inicializados exitosamente")
            return True
            
        except Exception as e:
            self.last_error = str(e)
            log.error(f"❌ Error inicializando Google Services: {str(e)}")
            return False
    
    def get_sheet_rows(self) -> Tuple[List[str], List[List[str]]]:
        """Obtiene headers + rows del Sheet principal"""
        if not self.initialized or not self.sheets_service:
            log.error("❌ Google Sheets no inicializado")
            return [], []
            
        try:
            rng = f"{SHEETS_TITLE_LEADS}!A:Z"
            result = self.sheets_service.spreadsheets().values().get(
                spreadsheetId=SHEETS_ID_LEADS,
                range=rng
            ).execute()
            
            values = result.get('values', [])
            
            if not values:
                log.warning("ℹ️ Sheet vacío")
                return [], []
                
            headers = [h.strip() for h in values[0]]
            rows = values[1:]
            
            log.info(f"✅ Sheet cargado: {len(headers)} columnas, {len(rows)} filas")
            return headers, rows
            
        except Exception as e:
            log.error(f"❌ Error obteniendo datos del Sheet: {str(e)}")
            return [], []
    
    def find_client_in_sheets(self, phone_last10: str) -> Optional[Dict[str, Any]]:
        """Busca cliente por teléfono en Sheets"""
        if not self.initialized:
            log.error("❌ Google Services no inicializado")
            return None
            
        headers, rows = self.get_sheet_rows()
        if not headers:
            return None
        
        # Encontrar índices de columnas
        def find_index(col_name: str) -> Optional[int]:
            col_name_lower = col_name.lower()
            for i, h in enumerate(headers):
                if h.lower() == col_name_lower:
                    return i
            return None
        
        idx_nombre = find_index("Nombre")
        idx_wa = find_index("WhatsApp")
        idx_status = find_index("ESTATUS")
        idx_last = find_index("LAST_MESSAGE_AT")
        
        if idx_wa is None:
            log.warning("⚠️ Columna 'WhatsApp' no encontrada en Sheet")
            return None
        
        # Buscar por teléfono
        for row_num, row in enumerate(rows, start=2):  # Fila 2 es primera de datos
            if idx_wa < len(row):
                wa_cell = row[idx_wa].strip()
                wa_last10 = self._normalize_phone_last10(wa_cell)
                
                if wa_last10 == phone_last10:
                    nombre = row[idx_nombre].strip() if idx_nombre is not None and idx_nombre < len(row) else ""
                    estatus = row[idx_status].strip() if idx_status is not None and idx_status < len(row) else ""
                    last_at = row[idx_last].strip() if idx_last is not None and idx_last < len(row) else ""
                    
                    log.info(f"✅ Cliente encontrado: {nombre} ({phone_last10})")
                    return {
                        "row": row_num,
                        "nombre": nombre,
                        "estatus": estatus,
                        "last_message_at": last_at,
                        "raw": row
                    }
        
        log.info(f"ℹ️ Cliente no encontrado: {phone_last10}")
        return None
    
    def _normalize_phone_last10(self, phone: str) -> str:
        """Normaliza teléfono a últimos 10 dígitos"""
        digits = re.sub(r"\D", "", phone or "")
        return digits[-10:] if len(digits) >= 10 else digits
    
    def create_client_folder(self, nombre_cliente: str, telefono: str) -> Optional[str]:
        """Crea carpeta para cliente en Drive"""
        if not self.initialized or not self.drive_service:
            log.error("❌ Google Drive no inicializado")
            return None
            
        try:
            # Limpiar nombre para carpeta
            nombre_limpio = re.sub(r'[^\w\-_\. ]', '', nombre_cliente)
            if not nombre_limpio:
                nombre_limpio = f"Cliente_{telefono[-4:]}"
                
            nombre_carpeta = f"{nombre_limpio}_{telefono[-4:]}"
            
            # Verificar si ya existe
            query = f"name='{nombre_carpeta}' and mimeType='application/vnd.google-apps.folder' and '{DRIVE_PARENT_FOLDER_ID}' in parents and trashed=false"
            resultados = self.drive_service.files().list(
                q=query,
                spaces='drive',
                fields='files(id, name)'
            ).execute()
            
            archivos = resultados.get('files', [])
            
            if archivos:
                carpeta_id = archivos[0]['id']
                log.info(f"✅ Carpeta existente: {nombre_carpeta} ({carpeta_id})")
                return carpeta_id
            
            # Crear nueva carpeta
            metadata_carpeta = {
                'name': nombre_carpeta,
                'mimeType': 'application/vnd.google-apps.folder',
                'parents': [DRIVE_PARENT_FOLDER_ID]
            }
            
            carpeta = self.drive_service.files().create(
                body=metadata_carpeta,
                fields='id'
            ).execute()
            
            carpeta_id = carpeta.get('id')
            log.info(f"✅ Carpeta creada: {nombre_carpeta} ({carpeta_id})")
            
            return carpeta_id
            
        except Exception as e:
            log.error(f"❌ Error creando carpeta: {str(e)}")
            return None
    
    def upload_to_drive(self, carpeta_id: str, nombre_archivo: str, 
                       contenido_bytes: bytes, mime_type: str) -> Optional[str]:
        """Sube archivo a Drive"""
        if not self.initialized or not self.drive_service:
            log.error("❌ Google Drive no inicializado")
            return None
            
        for attempt in range(3):
            try:
                log.info(f"📤 Subiendo {nombre_archivo} (intento {attempt + 1})")
                
                file_metadata = {
                    'name': nombre_archivo,
                    'parents': [carpeta_id]
                }
                
                media = MediaIoBaseUpload(
                    io.BytesIO(contenido_bytes),
                    mimetype=mime_type,
                    resumable=True
                )
                
                archivo = self.drive_service.files().create(
                    body=file_metadata,
                    media_body=media,
                    fields='id, webViewLink'
                ).execute()
                
                file_id = archivo.get('id')
                file_link = archivo.get('webViewLink')
                
                log.info(f"✅ Archivo subido: {nombre_archivo} ({file_id})")
                return file_link or file_id
                
            except Exception as e:
                log.error(f"❌ Error subiendo archivo (intento {attempt + 1}): {str(e)}")
                
                if attempt < 2:
                    wait_time = 2 ** attempt
                    time.sleep(wait_time)
                else:
                    log.error(f"❌ Máximo de reintentos para {nombre_archivo}")
                    return None
                    
        return None

# Instancia global de Google Services
google_services = GoogleServicesManager()

# ==========================
# Clases para manejo de documentos
# ==========================
@dataclass
class Documento:
    tipo: str  # 'INE_FRENTE', 'TARJETA_CIRCULACION', 'OTRO'
    nombre_archivo: str
    bytes: bytes
    mime_type: str
    timestamp: datetime = field(default_factory=datetime.now)

@dataclass
class EstadoDocumentos:
    telefono: str
    documentos: List[Documento] = field(default_factory=list)
    esperando_ine: bool = True
    esperando_tarjeta: bool = True
    carpeta_drive_id: Optional[str] = None
    intentos_fallidos: int = 0
    
    def agregar_documento(self, doc: Documento):
        self.documentos.append(doc)
        if doc.tipo == 'INE_FRENTE':
            self.esperando_ine = False
        elif doc.tipo == 'TARJETA_CIRCULACION':
            self.esperando_tarjeta = False
            
    def tiene_todos_documentos(self) -> bool:
        return not self.esperando_ine and not self.esperando_tarjeta
    
    def obtener_ultimo_documento(self) -> Optional[Documento]:
        return self.documentos[-1] if self.documentos else None

# Almacenamiento en memoria
estados_documentos: Dict[str, EstadoDocumentos] = {}
user_state: Dict[str, str] = {}
user_data: Dict[str, Dict[str, Any]] = {}

# ==========================
# WhatsApp Helpers
# ==========================
WPP_API_URL = f"https://graph.facebook.com/v20.0/{WABA_PHONE_ID}/messages" if WABA_PHONE_ID else None

def _wpp_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {META_TOKEN}",
        "Content-Type": "application/json"
    }

def send_message(to: str, text: str, max_retries: int = 3) -> bool:
    """Envía mensaje de texto por WhatsApp"""
    if not (META_TOKEN and WPP_API_URL):
        log.error("❌ WhatsApp no configurado")
        return False
        
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text[:4096]}
    }
    
    for attempt in range(max_retries):
        try:
            log.info(f"📤 Enviando a {to} (intento {attempt + 1})")
            response = requests.post(
                WPP_API_URL,
                headers=_wpp_headers(),
                json=payload,
                timeout=15
            )
            
            if response.status_code == 200:
                log.info(f"✅ Mensaje enviado a {to}")
                return True
            else:
                log.warning(f"⚠️ Error {response.status_code}: {response.text[:200]}")
                
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                    
        except Exception as e:
            log.error(f"❌ Error (intento {attempt + 1}): {str(e)}")
            
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                
    return False

def send_template_message(to: str, template_name: str, params: Dict | List) -> bool:
    """Envía plantilla de WhatsApp"""
    if not (META_TOKEN and WPP_API_URL):
        log.error("❌ WhatsApp no configurado para plantillas")
        return False
    
    components = []
    
    # Body parameters
    if isinstance(params, dict):
        body_params = []
        for k, v in params.items():
            body_params.append({
                "type": "text",
                "parameter_name": k,
                "text": str(v)
            })
        if body_params:
            components.append({
                "type": "body",
                "parameters": body_params
            })
    elif isinstance(params, list):
        body_params = [{"type": "text", "text": str(v)} for v in params]
        if body_params:
            components.append({
                "type": "body",
                "parameters": body_params
            })
    
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": "es_MX"},
            **({"components": components} if components else {})
        }
    }
    
    for attempt in range(3):
        try:
            log.info(f"📤 Enviando plantilla '{template_name}' a {to}")
            response = requests.post(
                WPP_API_URL,
                headers=_wpp_headers(),
                json=payload,
                timeout=15
            )
            
            if response.status_code == 200:
                log.info(f"✅ Plantilla enviada a {to}")
                return True
            else:
                log.warning(f"⚠️ Error plantilla: {response.status_code}")
                
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    
        except Exception as e:
            log.error(f"❌ Error plantilla (intento {attempt + 1}): {str(e)}")
            
            if attempt < 2:
                time.sleep(2 ** attempt)
                
    return False

# ==========================
# Funciones utilitarias
# ==========================
def _normalize_phone_last10(phone: str) -> str:
    """Normaliza teléfono a últimos 10 dígitos"""
    digits = re.sub(r"\D", "", phone or "")
    return digits[-10:] if len(digits) >= 10 else digits

def interpret_response(text: str) -> str:
    """Interpreta respuesta del usuario"""
    if not text:
        return "neutral"
    
    t = text.lower()
    pos = ["sí", "si", "claro", "ok", "de acuerdo", "vale", "afirmativo", "correcto", "yes"]
    neg = ["no", "nel", "nop", "negativo", "no quiero", "no gracias", "no interesa", "nope"]
    
    if any(p in t for p in pos):
        return "positive"
    if any(n in t for n in neg):
        return "negative"
    
    return "neutral"

def extract_number(text: str) -> Optional[float]:
    """Extrae número de texto"""
    if not text:
        return None
    
    clean = text.replace(",", "").replace("$", "")
    m = re.search(r"(\d{1,12}(\.\d+)?)", clean)
    
    try:
        return float(m.group(1)) if m else None
    except Exception:
        return None

def _notificar_asesor(mensaje: str):
    """Notifica al asesor"""
    try:
        if ADVISOR_NUMBER:
            log.info(f"👨‍💼 Notificando asesor: {mensaje[:100]}...")
            send_message(ADVISOR_NUMBER, f"🔔 {mensaje}")
    except Exception as e:
        log.error(f"❌ Error notificando asesor: {str(e)}")

# ==========================
# Sistema de manejo de documentos
# ==========================
def _detectar_tipo_documento(nombre_archivo: str, mime_type: str) -> str:
    """Detecta tipo de documento"""
    nombre_lower = nombre_archivo.lower()
    
    # Detectar INE
    if any(keyword in nombre_lower for keyword in ['ine', 'identificacion', 'identificación', 'if', 'id']):
        if 'frente' in nombre_lower or 'front' in nombre_lower:
            return 'INE_FRENTE'
        return 'INE'
    
    # Detectar Tarjeta de Circulación
    if any(keyword in nombre_lower for keyword in ['tarjeta', 'circulacion', 'circulación', 'placas', 'placa']):
        return 'TARJETA_CIRCULACION'
    
    # Detectar por extensión
    if nombre_lower.endswith(('.jpg', '.jpeg', '.png', '.pdf')):
        if 'ine' in nombre_lower:
            return 'INE_FRENTE'
        elif 'tarjeta' in nombre_lower:
            return 'TARJETA_CIRCULACION'
    
    return 'OTRO'

def _descargar_media_whatsapp(media_id: str) -> Tuple[Optional[bytes], Optional[str], Optional[str]]:
    """Descarga archivos desde WhatsApp"""
    if not META_TOKEN:
        return None, None, None
        
    try:
        # Obtener metadata
        url = f"https://graph.facebook.com/v20.0/{media_id}"
        headers = {"Authorization": f"Bearer {META_TOKEN}"}
        
        response = requests.get(url, headers=headers, timeout=30)
        if response.status_code != 200:
            log.error(f"❌ Error metadata: {response.status_code}")
            return None, None, None
            
        metadata = response.json()
        download_url = metadata.get('url')
        mime_type = metadata.get('mime_type', 'application/octet-stream')
        filename = metadata.get('filename', f'documento_{media_id[:8]}')
        
        if not download_url:
            log.error("❌ No hay URL de descarga")
            return None, None, None
        
        # Descargar archivo
        download_response = requests.get(download_url, headers=headers, timeout=60)
        if download_response.status_code != 200:
            log.error(f"❌ Error descarga: {download_response.status_code}")
            return None, None, None
            
        content = download_response.content
        log.info(f"✅ Archivo descargado: {filename} ({len(content)} bytes)")
        
        return content, mime_type, filename
        
    except Exception as e:
        log.error(f"❌ Error descargando media: {str(e)}")
        return None, None, None

def iniciar_embudo_documentos(telefono: str, nombre_cliente: str = "Cliente"):
    """Inicia proceso de recolección de documentos"""
    log.info(f"🚀 Iniciando embudo documentos para {telefono}")
    
    # Crear o obtener estado existente
    if telefono in estados_documentos:
        estado = estados_documentos[telefono]
        estado.esperando_ine = True
        estado.esperando_tarjeta = True
        estado.documentos.clear()
    else:
        estado = EstadoDocumentos(telefono=telefono)
        estados_documentos[telefono] = estado
    
    # Crear carpeta en Drive
    if google_services.initialized:
        carpeta_id = google_services.create_client_folder(nombre_cliente, telefono)
        estado.carpeta_drive_id = carpeta_id
    
    # Enviar instrucciones
    mensaje = f"""🚗 *Proceso de Cotización de Seguro de Auto*

Para generar tu cotización personalizada, necesito que me envíes:

1️⃣ *INE (Frente)*: Foto nítida del frente de tu INE
2️⃣ *Tarjeta de Circulación*: Foto de la tarjeta *o* número de placas

*Instrucciones:*
- Envía los documentos en cualquier orden
- Las fotos deben ser claras y legibles
- Puedes enviarlos como imágenes o documentos

Cuando tengas todos, te confirmaré y comenzaré tu cotización."""
    
    send_message(telefono, mensaje)
    user_state[telefono] = "auto"

def procesar_documento_recibido(telefono: str, media_id: str):
    """Procesa documento recibido"""
    log.info(f"📎 Procesando documento para {telefono}")
    
    if telefono not in estados_documentos:
        send_message(telefono, "⚠️ Primero selecciona 'Seguro de Auto' del menú.")
        return
    
    estado = estados_documentos[telefono]
    
    # Descargar archivo
    contenido, mime_type, nombre_archivo = _descargar_media_whatsapp(media_id)
    if not contenido:
        send_message(telefono, "❌ No pude descargar el archivo. ¿Intentar de nuevo?")
        return
    
    # Detectar tipo
    tipo_documento = _detectar_tipo_documento(nombre_archivo, mime_type)
    
    # Crear documento
    documento = Documento(
        tipo=tipo_documento,
        nombre_archivo=nombre_archivo,
        bytes=contenido,
        mime_type=mime_type
    )
    
    # Agregar al estado
    estado.agregar_documento(documento)
    
    # Subir a Drive
    if estado.carpeta_drive_id and google_services.initialized:
        link = google_services.upload_to_drive(
            estado.carpeta_drive_id,
            nombre_archivo,
            contenido,
            mime_type
        )
        
        if link:
            log.info(f"✅ Documento subido a Drive: {nombre_archivo}")
            tipo_doc_text = "INE (Frente)" if tipo_documento == "INE_FRENTE" else "Tarjeta de Circulación"
            _notificar_asesor(f"Documento recibido de {telefono}\nTipo: {tipo_doc_text}\nDrive: {link}")
        else:
            log.error(f"❌ No se pudo subir: {nombre_archivo}")
            estado.intentos_fallidos += 1
    
    # Feedback al usuario
    if tipo_documento == "INE_FRENTE":
        mensaje = "✅ *INE recibida correctamente*"
        if estado.esperando_tarjeta:
            mensaje += "\n\nAhora envía la *Tarjeta de Circulación* o *número de placas*."
    elif tipo_documento == "TARJETA_CIRCULACION":
        mensaje = "✅ *Tarjeta de Circulación recibida*"
        if estado.esperando_ine:
            mensaje += "\n\nAhora envía el *INE (frente)*."
    else:
        mensaje = f"✅ *Documento recibido*: {nombre_archivo}"
        if estado.esperando_ine:
            mensaje += "\n\nFalta: INE (frente)"
        if estado.esperando_tarjeta:
            mensaje += "\nFalta: Tarjeta de Circulación"
    
    send_message(telefono, mensaje)
    
    # Verificar si completó
    if estado.tiene_todos_documentos():
        log.info(f"🎉 Todos los documentos recibidos para {telefono}")
        
        mensaje_final = """🎉 *¡Todos los documentos recibidos!*

✅ INE (Frente)
✅ Tarjeta de Circulación

*Procesando tu solicitud...*

En breve nuestro asesor Christian se pondrá en contacto para entregarte tu cotización personalizada.

¿Necesitas algo más?"""
        
        send_message(telefono, mensaje_final)
        
        # Notificar al asesor
        _notificar_asesor(f"✅ DOCUMENTOS COMPLETOS\nCliente: {telefono}\nINE y Tarjeta listos\nCarpeta Drive: {estado.carpeta_drive_id or 'No creada'}")
        
        # Limpiar después de 10 minutos
        threading.Timer(600, lambda: estados_documentos.pop(telefono, None)).start()

def manejar_mensaje_auto(telefono: str, texto: str, match: Optional[Dict[str, Any]] = None):
    """Maneja mensajes en contexto auto"""
    if telefono not in estados_documentos:
        nombre = match.get('nombre', 'Cliente') if match else 'Cliente'
        iniciar_embudo_documentos(telefono, nombre)
        return
    
    estado = estados_documentos[telefono]
    
    # Verificar si texto contiene placas
    texto_lower = texto.lower()
    if any(keyword in texto_lower for keyword in ['placas', 'placa', 'numero', 'número']):
        numeros = re.findall(r'\b[A-Z0-9]{3,10}\b', texto.upper())
        if numeros:
            placas = numeros[0]
            
            # Crear documento virtual para placas
            documento = Documento(
                tipo='TARJETA_CIRCULACION',
                nombre_archivo=f'placas_{placas}.txt',
                bytes=placas.encode('utf-8'),
                mime_type='text/plain'
            )
            
            estado.agregar_documento(documento)
            
            mensaje = f"✅ *Placas registradas:* {placas}"
            if estado.esperando_ine:
                mensaje += "\n\nAhora envía el *INE (frente)*."
            
            send_message(telefono, mensaje)
            
            # Verificar si completó
            if estado.tiene_todos_documentos():
                mensaje_final = """🎉 *¡Todos los datos recibidos!*

✅ Placas registradas
✅ INE (Frente)

*Procesando tu solicitud...*

En breve Christian se pondrá en contacto para tu cotización."""
                
                send_message(telefono, mensaje_final)
                _notificar_asesor(f"✅ DATOS COMPLETOS (Placas)\nCliente: {telefono}\nPlacas: {placas}\nINE recibida")
        else:
            send_message(telefono, "Escribe el *número de placas* claramente. Ejemplo: ABC123")
    else:
        send_message(telefono, "Envía los documentos solicitados o escribe el número de placas.")

# ==========================
# Funciones del menú
# ==========================
def enviar_menu_principal(telefono: str):
    """Envía el menú principal"""
    menu = """🟦 *Vicky Bot — Inbursa*

Elige una opción:
1️⃣ Préstamo IMSS (Ley 73)
2️⃣ Seguro de Auto (cotización)
3️⃣ Seguros de Vida / Salud
4️⃣ Tarjeta médica VRIM
5️⃣ Crédito Empresarial
6️⃣ Financiamiento Práctico
7️⃣ Contactar con Christian

*Escribe el número o la opción* (ej. 'auto', 'imss', 'contactar')"""
    
    send_message(telefono, menu)
    user_state[telefono] = "menu"

# ==========================
# Embudos del menú
# ==========================
def manejar_imss(telefono: str, texto: str):
    """Maneja embudo IMSS"""
    estado_actual = user_state.get(telefono, "")
    
    if estado_actual == "":
        user_state[telefono] = "imss_pension"
        send_message(telefono, "🏥 *Préstamo IMSS Ley 73*\n\n¿Cuál es tu *pensión mensual* aproximada? (ej. $8,500)")
    elif estado_actual == "imss_pension":
        pension = extract_number(texto)
        if pension:
            user_data.setdefault(telefono, {})["imss_pension"] = pension
            user_state[telefono] = "imss_monto"
            send_message(telefono, f"✅ Pensión: ${pension:,.0f}\n\n¿Qué *monto* deseas solicitar? (mínimo $40,000)")
        else:
            send_message(telefono, "Escribe un monto válido (ej. 8500)")
    elif estado_actual == "imss_monto":
        monto = extract_number(texto)
        if monto and monto >= 40000:
            user_data.setdefault(telefono, {})["imss_monto"] = monto
            user_state[telefono] = "imss_nombre"
            send_message(telefono, f"✅ Monto: ${monto:,.0f}\n\n¿Tu *nombre completo*?")
        else:
            send_message(telefono, "Monto mínimo $40,000. Escribe un monto válido.")
    elif estado_actual == "imss_nombre":
        user_data.setdefault(telefono, {})["imss_nombre"] = texto
        user_state[telefono] = "imss_ciudad"
        send_message(telefono, f"✅ Nombre registrado\n\n¿En qué *ciudad* vives?")
    elif estado_actual == "imss_ciudad":
        user_data.setdefault(telefono, {})["imss_ciudad"] = texto
        user_state[telefono] = "imss_nomina"
        send_message(telefono, f"✅ Ciudad: {texto}\n\n¿Tienes *nómina Inbursa*? (sí/no)")
    elif estado_actual == "imss_nomina":
        tiene_nomina = interpret_response(texto) == "positive"
        user_data.setdefault(telefono, {})["imss_nomina"] = "Sí" if tiene_nomina else "No"
        
        # Resumen
        datos = user_data.get(telefono, {})
        resumen = f"""✅ *Preautorizado*

Nombre: {datos.get('imss_nombre', '')}
Ciudad: {datos.get('imss_ciudad', '')}
Pensión: ${datos.get('imss_pension', 0):,.0f}
Monto: ${datos.get('imss_monto', 0):,.0f}
Nómina Inbursa: {datos.get('imss_nomina', 'No')}

Un asesor te contactará pronto."""
        
        send_message(telefono, resumen)
        _notificar_asesor(f"🔔 IMSS - Nuevo prospecto\nCliente: {telefono}\n{resumen}")
        
        # Reiniciar estado
        user_state[telefono] = "menu"
        enviar_menu_principal(telefono)

def manejar_empresarial(telefono: str, texto: str):
    """Maneja embudo empresarial"""
    estado_actual = user_state.get(telefono, "")
    
    if estado_actual == "":
        user_state[telefono] = "emp_giro"
        send_message(telefono, "🏢 *Crédito Empresarial*\n\n¿A qué *se dedica* tu empresa?")
    elif estado_actual == "emp_giro":
        user_data.setdefault(telefono, {})["emp_giro"] = texto
        user_state[telefono] = "emp_monto"
        send_message(telefono, f"✅ Giro: {texto}\n\n¿Qué *monto* necesitas? (mínimo $100,000)")
    elif estado_actual == "emp_monto":
        monto = extract_number(texto)
        if monto and monto >= 100000:
            user_data.setdefault(telefono, {})["emp_monto"] = monto
            user_state[telefono] = "emp_nombre"
            send_message(telefono, f"✅ Monto: ${monto:,.0f}\n\n¿Tu *nombre completo*?")
        else:
            send_message(telefono, "Monto mínimo $100,000. Escribe un monto válido.")
    elif estado_actual == "emp_nombre":
        user_data.setdefault(telefono, {})["emp_nombre"] = texto
        user_state[telefono] = "emp_ciudad"
        send_message(telefono, f"✅ Nombre: {texto}\n\n¿Tu *ciudad*?")
    elif estado_actual == "emp_ciudad":
        user_data.setdefault(telefono, {})["emp_ciudad"] = texto
        
        datos = user_data.get(telefono, {})
        resumen = f"""✅ *Solicitud registrada*

Nombre: {datos.get('emp_nombre', '')}
Ciudad: {datos.get('emp_ciudad', '')}
Giro: {datos.get('emp_giro', '')}
Monto: ${datos.get('emp_monto', 0):,.0f}

Un asesor te contactará pronto."""
        
        send_message(telefono, resumen)
        _notificar_asesor(f"🔔 EMPRESARIAL - Nueva solicitud\nCliente: {telefono}\n{resumen}")
        
        user_state[telefono] = "menu"
        enviar_menu_principal(telefono)

# ==========================
# Contexto post-campaña
# ==========================
def _parse_dt_maybe(value: str) -> Optional[datetime]:
    """Parse fecha desde string"""
    if not value:
        return None
    
    v = value.strip()
    try:
        if v.endswith("Z"):
            v = v[:-1] + "+00:00"
        return datetime.fromisoformat(v)
    except Exception:
        return None

def is_auto_context(match: Optional[Dict[str, Any]]) -> bool:
    """Verifica si está en contexto auto post-campaña"""
    if not match:
        return False
    
    estatus = (match.get("estatus") or "").strip().upper()
    valid_status = {"ENVIADO_INICIAL", "ENVIADO_AUTO", "ENVIADO_SEGURO_AUTO"}
    
    if estatus not in valid_status:
        return False
    
    dt = _parse_dt_maybe(match.get("last_message_at") or "")
    if not dt:
        return False
    
    if dt.tzinfo is not None:
        now = datetime.now(dt.tzinfo)
    else:
        now = datetime.utcnow()
    
    return (now - dt) <= timedelta(hours=24)

def handle_auto_context_response(telefono: str, texto: str, match: Dict[str, Any]) -> bool:
    """Maneja respuesta en contexto auto post-campaña"""
    t = texto.lower().strip()
    intent = interpret_response(texto)
    
    if t in ("1", "si", "sí", "ok", "claro") or intent == "positive":
        nombre = match.get("nombre", "").strip() or "Cliente"
        iniciar_embudo_documentos(telefono, nombre)
        return True
    
    if t in ("2", "no", "nel") or intent == "negative":
        nombre = match.get("nombre", "").strip() or "Cliente"
        send_message(telefono, f"Entendido {nombre}. ¿Cuándo *vence tu póliza actual*? (formato AAAA-MM-DD)")
        
        _notificar_asesor(f"🔔 AUTO - NO INTERESADO\nCliente: {telefono}\nNombre: {nombre}")
        return True
    
    return False

# ==========================
# Flask App
# ==========================
app = Flask(__name__)

@app.route('/webhook', methods=['GET'])
def webhook_verify():
    """Verificación webhook"""
    mode = request.args.get('hub.mode')
    token = request.args.get('hub.verify_token')
    challenge = request.args.get('hub.challenge')
    
    if mode == 'subscribe' and token == VERIFY_TOKEN:
        log.info("✅ Webhook verificado")
        return challenge, 200
    
    log.warning("❌ Verificación fallida")
    return 'Error', 403

@app.route('/webhook', methods=['POST'])
def webhook_receive():
    """Recibe mensajes de WhatsApp"""
    try:
        data = request.get_json()
        
        if not data:
            return jsonify({'status': 'ok'}), 200
        
        entry = data.get('entry', [{}])[0]
        changes = entry.get('changes', [{}])[0]
        value = changes.get('value', {})
        messages = value.get('messages', [])
        
        if not messages:
            return jsonify({'status': 'ok'}), 200
        
        message = messages[0]
        telefono = message.get('from')
        tipo_mensaje = message.get('type')
        
        if not telefono:
            return jsonify({'status': 'ok'}), 200
        
        # Buscar match en Sheets
        last10 = _normalize_phone_last10(telefono)
        match = google_services.find_client_in_sheets(last10)
        
        # Manejar documentos/imágenes
        if tipo_mensaje in ['document', 'image']:
            log.info(f"📎 {tipo_mensaje.upper()} de {telefono}")
            
            media_id = None
            if tipo_mensaje == 'document':
                media_id = message.get('document', {}).get('id')
            elif tipo_mensaje == 'image':
                media_id = message.get('image', {}).get('id')
            
            if media_id:
                # Verificar contexto
                estado_actual = user_state.get(telefono, "")
                if estado_actual == "auto" or telefono in estados_documentos:
                    procesar_documento_recibido(telefono, media_id)
                else:
                    send_message(telefono, "Para enviar documentos, selecciona 'Seguro de Auto' (2) del menú.")
            else:
                send_message(telefono, "No pude procesar el archivo.")
        
        # Manejar mensajes de texto
        elif tipo_mensaje == 'text':
            texto = message.get('text', {}).get('body', '').strip()
            log.info(f"💬 Texto de {telefono}: {texto}")
            
            texto_lower = texto.lower()
            
            # ============================================
            # INTERCEPTOR POST-CAMPAÑA (AUTO)
            # ============================================
            if is_auto_context(match):
                if handle_auto_context_response(telefono, texto, match):
                    return jsonify({'status': 'ok'}), 200
            
            # ============================================
            # COMANDOS DEL MENÚ
            # ============================================
            if texto_lower in ['menu', 'menú', 'inicio', 'hola']:
                enviar_menu_principal(telefono)
            
            elif texto_lower in ['2', 'auto', 'seguro de auto', 'seguros de auto', 'cotizar auto']:
                nombre = match.get('nombre', 'Cliente') if match else 'Cliente'
                iniciar_embudo_documentos(telefono, nombre)
            
            elif texto_lower in ['1', 'imss', 'ley 73', 'préstamo', 'prestamo', 'pension', 'pensión']:
                manejar_imss(telefono, texto)
            
            elif texto_lower in ['5', 'empresarial', 'pyme', 'crédito empresarial', 'credito empresarial']:
                manejar_empresarial(telefono, texto)
            
            elif texto_lower in ['3', 'vida', 'salud', 'seguro de vida', 'seguro de salud']:
                send_message(telefono, "🧬 *Seguros de Vida/Salud*\n\nGracias por tu interés. Notificaré al asesor para contactarte.")
                _notificar_asesor(f"🔔 VIDA/SALUD - Solicitud contacto\nCliente: {telefono}")
                enviar_menu_principal(telefono)
            
            elif texto_lower in ['4', 'vrim', 'tarjeta médica', 'tarjeta medica']:
                send_message(telefono, "🩺 *VRIM - Tarjeta Médica*\n\nGracias por tu interés. Notificaré al asesor para darte detalles.")
                _notificar_asesor(f"🔔 VRIM - Solicitud contacto\nCliente: {telefono}")
                enviar_menu_principal(telefono)
            
            elif texto_lower in ['6', 'financiamiento práctico', 'financiamiento practico']:
                send_message(telefono, "💰 *Financiamiento Práctico*\n\nPróximamente disponible. Mientras tanto, ¿te interesa alguna otra opción?")
                enviar_menu_principal(telefono)
            
            elif texto_lower in ['7', 'contactar', 'asesor', 'contactar con christian']:
                send_message(telefono, "✅ Listo. Avisé a Christian para que te contacte.")
                _notificar_asesor(f"🔔 CONTACTO DIRECTO\nCliente solicita hablar\nWhatsApp: {telefono}")
                enviar_menu_principal(telefono)
            
            # ============================================
            # MANEJO DE ESTADOS ACTIVOS
            # ============================================
            else:
                estado_actual = user_state.get(telefono, "")
                
                if estado_actual.startswith("imss_"):
                    manejar_imss(telefono, texto)
                
                elif estado_actual.startswith("emp_"):
                    manejar_empresarial(telefono, texto)
                
                elif estado_actual == "auto" or telefono in estados_documentos:
                    manejar_mensaje_auto(telefono, texto, match)
                
                else:
                    # Mensaje no reconocido
                    send_message(telefono, "No entendí. Escribe *menú* para ver opciones.")
        
        return jsonify({'status': 'ok'}), 200
        
    except Exception as e:
        log.error(f"❌ Error webhook: {str(e)}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

# ==========================
# Endpoints auxiliares
# ==========================
@app.route('/health', methods=['GET'])
def health():
    """Endpoint de salud"""
    return jsonify({
        'status': 'ok',
        'service': 'Vicky Bot SECOM Completo',
        'google_services': google_services.initialized,
        'whatsapp_configured': bool(META_TOKEN and WABA_PHONE_ID),
        'document_states': len(estados_documentos),
        'user_states': len(user_state),
        'timestamp': datetime.now().isoformat()
    }), 200

@app.route('/ext/health', methods=['GET'])
def ext_health():
    """Health endpoint extendido"""
    return jsonify({
        'status': 'ok',
        'whatsapp': bool(META_TOKEN and WABA_PHONE_ID),
        'google': google_services.initialized,
        'drive_parent': DRIVE_PARENT_FOLDER_ID,
        'sheets_id': SHEETS_ID_LEADS
    }), 200

@app.route('/ext/send-promo', methods=['POST'])
def ext_send_promo():
    """Endpoint para envíos masivos"""
    try:
        data = request.get_json() or {}
        items = data.get('items', [])
        
        if not isinstance(items, list) or not items:
            return jsonify({'queued': False, 'error': 'Items inválidos'}), 400
        
        log.info(f"📨 Send-promo recibido: {len(items)} items")
        
        # Procesar en background
        def worker(items_list):
            exitosos = 0
            fallidos = 0
            
            for item in items_list:
                try:
                    to = item.get('to', '').strip()
                    template = item.get('template', '').strip()
                    text = item.get('text', '').strip()
                    
                    if not to:
                        fallidos += 1
                        continue
                    
                    success = False
                    if template:
                        params = item.get('params', {})
                        success = send_template_message(to, template, params)
                    elif text:
                        success = send_message(to, text)
                    
                    if success:
                        exitosos += 1
                    else:
                        fallidos += 1
                    
                    time.sleep(0.5)
                    
                except Exception:
                    fallidos += 1
            
            log.info(f"🎯 Envío masivo: {exitosos} ✅, {fallidos} ❌")
            
            if ADVISOR_NUMBER:
                summary = f"📊 Resumen envío masivo:\n• Exitosos: {exitosos}\n• Fallidos: {fallidos}\n• Total: {len(items_list)}"
                send_message(ADVISOR_NUMBER, summary)
        
        threading.Thread(target=worker, args=(items,), daemon=True).start()
        
        return jsonify({
            'queued': True,
            'message': f'Procesando {len(items)} mensajes',
            'timestamp': datetime.now().isoformat()
        }), 202
        
    except Exception as e:
        log.error(f"❌ Error send-promo: {str(e)}")
        return jsonify({'queued': False, 'error': str(e)}), 500

@app.route('/ext/test-send', methods=['POST'])
def ext_test_send():
    """Endpoint para pruebas"""
    try:
        data = request.get_json() or {}
        to = data.get('to', '').strip()
        text = data.get('text', '').strip()
        
        if not to or not text:
            return jsonify({'ok': False, 'error': 'Faltan parámetros'}), 400
        
        success = send_message(to, text)
        
        return jsonify({'ok': success}), 200
        
    except Exception as e:
        log.error(f"❌ Error test-send: {str(e)}")
        return jsonify({'ok': False, 'error': str(e)}), 500

@app.route('/debug/drive', methods=['GET'])
def debug_drive():
    """Debug de Drive"""
    return jsonify({
        'initialized': google_services.initialized,
        'last_error': google_services.last_error,
        'parent_folder': DRIVE_PARENT_FOLDER_ID
    }), 200

@app.route('/debug/documentos/<telefono>', methods=['GET'])
def debug_documentos(telefono: str):
    """Debug de documentos"""
    estado = estados_documentos.get(telefono)
    
    if estado:
        return jsonify({
            'telefono': estado.telefono,
            'documentos': len(estado.documentos),
            'esperando_ine': estado.esperando_ine,
            'esperando_tarjeta': estado.esperando_tarjeta,
            'tiene_todos': estado.tiene_todos_documentos(),
            'carpeta_drive': estado.carpeta_drive_id,
            'intentos_fallidos': estado.intentos_fallidos
        }), 200
    else:
        return jsonify({
            'telefono': telefono,
            'estado': 'no_en_embudo'
        }), 200

# ==========================
# Inicialización
# ==========================
@app.before_first_request
def initialize():
    """Inicializa servicios"""
    log.info("🚀 Inicializando Vicky Bot SECOM Completo...")
    
    # Inicializar Google Services
    if GOOGLE_CREDENTIALS_JSON:
        success = google_services.initialize()
        if success:
            log.info("✅ Google Services inicializados")
        else:
            log.error(f"❌ Error Google Services: {google_services.last_error}")
    else:
        log.warning("⚠️ Google credentials no configuradas")
    
    log.info("✅ Bot listo")

# ==========================
# Punto de entrada
# ==========================
if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    
    log.info(f"🚀 Iniciando en puerto {port}")
    log.info(f"📞 WhatsApp: {'✅' if META_TOKEN and WABA_PHONE_ID else '❌'}")
    log.info(f"📊 Google: {'✅' if GOOGLE_CREDENTIALS_JSON else '❌'}")
    
    app.run(host='0.0.0.0', port=port, debug=False)
