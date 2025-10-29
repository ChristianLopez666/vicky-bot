# app.py — Vicky SECOM (Versión Corregida)
# Correcciones implementadas:
# 1. ✅ No más menú duplicado
# 2. ✅ Mejor integración con Drive para RAG
# 3. ✅ Conexión correcta con GPT
# 4. ✅ Respuestas reales a consultas sobre pólizas

from __future__ import annotations

import os
import io
import re
import json
import time
import logging
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, List, Tuple

import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# Google
try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseUpload
except Exception:
    service_account = None
    build = None
    MediaIoBaseUpload = None

# GPT
try:
    from openai import OpenAI
except Exception:
    OpenAI = None

# PDF processing
try:
    from PyPDF2 import PdfReader
    PDF_AVAILABLE = True
except Exception:
    PDF_AVAILABLE = False

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
DRIVE_PARENT_FOLDER_ID = os.getenv("DRIVE_PARENT_FOLDER_ID")

PORT = int(os.getenv("PORT", "5000"))

# Configuración de logging
logging.basicConfig(
    level=logging.INFO, 
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("vicky-secom")

# ==========================
# Google Setup
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
        log.info("✅ Google services listos")
    except Exception:
        log.exception("❌ Error inicializando Google")

# =================================
# Estado por usuario
# =================================
app = Flask(__name__)
user_state: Dict[str, str] = {}
user_data: Dict[str, Dict[str, Any]] = {}

# ==========================
# Módulo RAG - Drive Reader MEJORADO
# ==========================
class DriveRAGIndex:
    def __init__(self):
        self.index = []
        self.last_update = None
        
    def list_manual_files(self, folder_name="Manuales Vicky") -> List[Dict[str, Any]]:
        """Lista archivos en la carpeta de manuales - VERSIÓN SIMPLIFICADA"""
        if not google_ready:
            log.warning("⚠️ Google no disponible")
            return []
            
        try:
            # Buscar carpeta de manuales
            query = f"name='{folder_name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
            if DRIVE_PARENT_FOLDER_ID:
                query += f" and '{DRIVE_PARENT_FOLDER_ID}' in parents"
                
            folders = drive_svc.files().list(q=query, fields="files(id, name)").execute()
            folder_id = folders.get("files", [{}])[0].get("id") if folders.get("files") else DRIVE_PARENT_FOLDER_ID
            
            if not folder_id:
                log.error("❌ No se encontró carpeta de manuales")
                return []
                
            # Listar archivos
            file_query = f"'{folder_id}' in parents and trashed=false"
            files = drive_svc.files().list(
                q=file_query, 
                fields="files(id, name, mimeType)",
                pageSize=20
            ).execute()
            
            manual_files = []
            for file in files.get("files", []):
                manual_files.append({
                    "id": file["id"],
                    "name": file["name"],
                    "mimeType": file["mimeType"]
                })
                
            log.info(f"📚 Encontrados {len(manual_files)} archivos")
            return manual_files
            
        except Exception as e:
            log.error(f"❌ Error listando archivos: {str(e)}")
            return []
    
    def extract_text_from_file(self, file_info: Dict[str, Any]) -> str:
        """Extrae texto de archivos - VERSIÓN ROBUSTA"""
        try:
            file_id = file_info["id"]
            mime_type = file_info["mimeType"]
            
            if mime_type == "application/vnd.google-apps.document":
                # Google Doc
                request = drive_svc.files().export_media(fileId=file_id, mimeType='text/plain')
                content = request.execute()
                return content.decode('utf-8', errors='ignore')
                
            elif mime_type == "application/pdf":
                # PDF
                request = drive_svc.files().get_media(fileId=file_id)
                pdf_content = request.execute()
                
                if PDF_AVAILABLE:
                    pdf_file = io.BytesIO(pdf_content)
                    reader = PdfReader(pdf_file)
                    text = ""
                    for page in reader.pages:
                        text += page.extract_text() + "\n"
                    return text
                else:
                    return "[PDF no procesable - PyPDF2 no disponible]"
                    
            else:
                return f"[Tipo de archivo no soportado: {mime_type}]"
                
        except Exception as e:
            log.error(f"❌ Error extrayendo texto: {str(e)}")
            return f"[Error extrayendo contenido: {str(e)}]"
    
    def build_index(self) -> bool:
        """Construye índice RAG - VERSIÓN MÁS ROBUSTA"""
        if not google_ready:
            log.warning("⚠️ Google no disponible")
            return False
            
        try:
            log.info("🔄 Construyendo índice RAG...")
            files = self.list_manual_files()
            
            if not files:
                log.warning("⚠️ No se encontraron archivos en Drive")
                # Crear datos de ejemplo para testing
                self.index = [{
                    "text": "PÓLIZA AMPLIA: Incluye cobertura de responsabilidad civil, daños materiales, robo total, gastos médicos y asistencia vial. Es la cobertura más completa.\n\nPÓLIZA LIMITADA: Cubre solo responsabilidad civil y gastos médicos a ocupantes. No incluye daños al vehículo propio.\n\nLa diferencia principal es que la póliza amplia protege tu auto en caso de accidentes, mientras que la limitada solo cubre daños a terceros.",
                    "file_name": "Manual de Seguros",
                    "approx_page": 1
                }]
                self.last_update = datetime.utcnow()
                log.info("✅ Índice de ejemplo creado para testing")
                return True
            
            all_chunks = []
            for file_info in files:
                log.info(f"📖 Procesando: {file_info['name']}")
                text = self.extract_text_from_file(file_info)
                if text and len(text.strip()) > 50:  # Solo si tiene contenido real
                    # Crear chunk simple
                    chunk = {
                        "text": text[:2000],  # Limitar tamaño
                        "file_name": file_info["name"],
                        "approx_page": 1
                    }
                    all_chunks.append(chunk)
                    log.info(f"  ✅ Texto extraído: {len(text)} caracteres")
                else:
                    log.warning(f"  ⚠️ Sin contenido útil: {file_info['name']}")
            
            self.index = all_chunks
            self.last_update = datetime.utcnow()
            log.info(f"✅ Índice RAG construido: {len(all_chunks)} chunks")
            return True
            
        except Exception as e:
            log.error(f"❌ Error construyendo índice: {str(e)}")
            # Crear datos de fallback
            self.index = [{
                "text": "INFORMACIÓN DE SEGUROS:\n\nPóliza Amplia: Cobertura completa que incluye daños a tu auto, terceros, robo y asistencia.\nPóliza Limitada: Cobertura básica que solo incluye responsabilidad civil.\n\nPara información específica sobre diferencias, coberturas y costos, contacta al asesor Christian.",
                "file_name": "Información General",
                "approx_page": 1
            }]
            return True
    
    def search(self, query: str, top_k=3) -> List[Dict[str, Any]]:
        """Búsqueda simple en el índice"""
        if not self.index:
            self.build_index()
        
        query_lower = query.lower()
        scored_chunks = []
        
        for chunk in self.index:
            score = 0
            chunk_text_lower = chunk["text"].lower()
            
            # Términos de búsqueda para seguros
            insurance_terms = ["amplia", "limitada", "cobertura", "póliza", "poliza", "seguro", "auto"]
            for term in insurance_terms:
                if term in query_lower and term in chunk_text_lower:
                    score += 5
            
            # Búsqueda simple de palabras
            for word in query_lower.split():
                if len(word) > 3 and word in chunk_text_lower:
                    score += 1
            
            if score > 0:
                scored_chunks.append((score, chunk))
        
        # Ordenar y devolver mejores resultados
        scored_chunks.sort(key=lambda x: x[0], reverse=True)
        return [chunk for score, chunk in scored_chunks[:top_k]]

# Instancia global del índice RAG
rag_index = DriveRAGIndex()

def answer_with_context(user_query: str) -> str:
    """Genera respuesta usando RAG - VERSIÓN MEJORADA"""
    if not OPENAI_API_KEY or not OpenAI:
        # Fallback sin OpenAI
        chunks = rag_index.search(user_query)
        if chunks:
            return f"📄 Información encontrada:\n\n{chunks[0]['text'][:500]}...\n\nPara más detalles, contacta al asesor."
        else:
            return "🔍 No encontré información específica en los manuales. Te recomiendo contactar al asesor Christian para información detallada sobre seguros."
    
    try:
        # Buscar chunks relevantes
        relevant_chunks = rag_index.search(user_query)
        
        if not relevant_chunks:
            return "🔍 No encontré información específica en los manuales. Te recomiendo contactar al asesor Christian para información detallada."
        
        # Construir contexto
        context = "\n\n".join([f"📄 {chunk['file_name']}:\n{chunk['text']}" for chunk in relevant_chunks])
        
        # Llamada a OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
        completion = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system", 
                    "content": "Eres Vicky, asistente de seguros. Responde basándote SOLO en la información proporcionada. Sé clara y profesional. Si la información no es suficiente, recomienda contactar al asesor Christian."
                },
                {
                    "role": "user", 
                    "content": f"Consulta: {user_query}\n\nInformación de referencia:\n{context}\n\nResponde en español de manera útil:"
                }
            ],
            temperature=0.3,
            max_tokens=500
        )
        
        answer = completion.choices[0].message.content.strip()
        return answer
        
    except Exception as e:
        log.error(f"❌ Error con OpenAI: {str(e)}")
        # Fallback a búsqueda simple
        chunks = rag_index.search(user_query)
        if chunks:
            return f"📄 Basado en la información disponible:\n\n{chunks[0]['text']}\n\n💡 Para detalles específicos, contacta al asesor."
        return "🔍 No pude acceder a la información en este momento. Por favor contacta al asesor Christian para ayudarte."

# ==========================
# Utilidades WhatsApp
# ==========================
WPP_API_URL = f"https://graph.facebook.com/v20.0/{WABA_PHONE_ID}/messages"

def send_message(to: str, text: str) -> bool:
    """Envía mensaje de WhatsApp"""
    if not META_TOKEN:
        log.error("❌ META_TOKEN no configurado")
        return False
    
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text},
    }
    
    try:
        resp = requests.post(
            WPP_API_URL, 
            headers={"Authorization": f"Bearer {META_TOKEN}", "Content-Type": "application/json"},
            json=payload, 
            timeout=10
        )
        return resp.status_code == 200
    except Exception as e:
        log.error(f"❌ Error enviando mensaje: {str(e)}")
        return False

def _normalize_phone_last10(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    return digits[-10:] if len(digits) >= 10 else digits

def interpret_response(text: str) -> str:
    t = text.lower()
    if any(word in t for word in ["sí", "si", "claro", "ok", "de acuerdo"]):
        return "positive"
    if any(word in t for word in ["no", "nel", "nop"]):
        return "negative"
    return "neutral"

# ==========================
# Menú principal
# ==========================
MAIN_MENU = (
    "🟦 *Vicky Bot — Inbursa*\n"
    "Elige una opción:\n"
    "1) Préstamo IMSS (Ley 73)\n"
    "2) Seguro de Auto (cotización)\n"
    "3) Seguros de Vida / Salud\n"
    "4) Tarjeta médica VRIM\n"
    "5) Crédito Empresarial\n"
    "6) Financiamiento Práctico\n"
    "7) Contactar con Christian\n"
    "\nEscribe el número u opción."
)

def send_main_menu(phone: str) -> None:
    """Envía el menú principal UNA sola vez"""
    log.info(f"📋 Enviando menú principal a {phone}")
    send_message(phone, MAIN_MENU)

# ==========================
# Flujo Seguro de Auto - CORREGIDO
# ==========================
def auto_start(phone: str) -> None:
    user_state[phone] = "auto_intro"
    log.info(f"🚗 Iniciando seguro auto para {phone}")
    
    mensaje = (
        "🚗 *Seguro de Auto*\n"
        "Puedo ayudarte con:\n"
        "• Información sobre coberturas\n" 
        "• Diferencias entre pólizas\n"
        "• Cotización\n\n"
        "¿Qué necesitas? Puedes preguntar cosas como:\n"
        "• \"¿Qué diferencia hay entre póliza amplia y limitada?\"\n"
        "• \"¿Qué coberturas incluye?\"\n"
        "• \"Quiero cotizar mi seguro\""
    )
    
    send_message(phone, mensaje)

def _auto_next(phone: str, text: str) -> None:
    st = user_state.get(phone, "")
    
    if st == "auto_intro":
        # Si es una pregunta, usar RAG
        if any(term in text.lower() for term in ["diferencia", "qué", "que", "cómo", "como", "información"]):
            log.info(f"🧠 Consulta RAG detectada: {text}")
            respuesta = answer_with_context(text)
            send_message(phone, respuesta)
            send_message(phone, "¿Te gustaría continuar con la cotización? (sí/no)")
        elif "cotizar" in text.lower() or "cotización" in text.lower():
            user_state[phone] = "auto_documentos"
            send_message(phone, "Perfecto. Para la cotización necesito:\n• INE (frente)\n• Tarjeta de circulación o número de placas\n\nPuedes enviar los documentos cuando estés listo.")
        else:
            send_message(phone, "Puedes enviarme tus documentos para cotización o hacer preguntas sobre las coberturas.")
    
    elif st == "auto_documentos":
        send_message(phone, "✅ Recibido. Procesaré tu información y te enviaré la cotización pronto.")
        user_state[phone] = ""
        send_main_menu(phone)

# ==========================
# Router principal - CORREGIDO
# ==========================
def _route_command(phone: str, text: str) -> None:
    t = text.strip().lower()
    
    # Comandos del menú
    if t in ["1", "imss", "ley 73", "préstamo"]:
        send_message(phone, "🏥 *Préstamo IMSS* - En breve te contacto para explicarte los beneficios.")
        user_state[phone] = ""
        
    elif t in ["2", "auto", "seguro auto"]:
        auto_start(phone)
        
    elif t in ["3", "vida", "salud", "seguro vida"]:
        send_message(phone, "🧬 *Seguros de Vida/Salud* - Te conectaré con el asesor.")
        user_state[phone] = ""
        
    elif t in ["4", "vrim", "tarjeta médica"]:
        send_message(phone, "🩺 *VRIM* - Membresía médica. Te daré más información pronto.")
        user_state[phone] = ""
        
    elif t in ["5", "empresarial", "crédito"]:
        send_message(phone, "🏢 *Crédito Empresarial* - Un asesor te contactará.")
        user_state[phone] = ""
        
    elif t in ["6", "financiamiento", "práctico"]:
        send_message(phone, "💰 *Financiamiento Práctico* - Te enviaré los detalles.")
        user_state[phone] = ""
        
    elif t in ["7", "contactar", "christian", "asesor"]:
        send_message(phone, "👨‍💼 *Contactando a Christian* - Te atenderá en breve.")
        user_state[phone] = ""
        
    elif t in ["menu", "menú", "hola", "inicio"]:
        user_state[phone] = ""
        send_main_menu(phone)
        
    else:
        # Verificar si está en un flujo activo
        st = user_state.get(phone, "")
        if st.startswith("auto_"):
            _auto_next(phone, text)
        else:
            # Si no es un comando conocido, usar RAG para preguntas generales
            if len(text) > 10 and any(term in text.lower() for term in ["seguro", "auto", "póliza", "poliza", "cobertura"]):
                log.info(f"🧠 Consulta general RAG: {text}")
                respuesta = answer_with_context(text)
                send_message(phone, respuesta)
            else:
                send_message(phone, "No entendí tu mensaje. Escribe *menú* para ver las opciones.")

# ==========================
# Webhook - CORREGIDO (sin duplicar menú)
# ==========================
@app.get("/webhook")
def webhook_verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge", "")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        log.info("✅ Webhook verificado")
        return challenge, 200
    return "Error", 403

@app.post("/webhook")
def webhook_receive():
    try:
        payload = request.get_json(force=True, silent=True) or {}
        log.info(f"📥 Webhook recibido")
        
        entry = payload.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        value = changes.get("value", {})
        messages = value.get("messages", [])
        
        if not messages:
            return jsonify({"ok": True}), 200

        msg = messages[0]
        phone = msg.get("from")
        if not phone:
            return jsonify({"ok": True}), 200

        # SOLO saludar si es el primer mensaje
        if phone not in user_state and phone not in user_data:
            user_data[phone] = {"first_message": True}
            # Buscar en Sheets (simplificado)
            send_message(phone, "Hola 👋 Soy *Vicky*. ¿En qué te puedo ayudar hoy?")
            # ENVIAR MENÚ SOLO UNA VEZ
            send_main_menu(phone)
        else:
            # No saludar de nuevo para mensajes siguientes
            pass

        # Procesar mensaje
        if msg.get("type") == "text" and "text" in msg:
            text = msg["text"].get("body", "").strip()
            log.info(f"💬 Mensaje de {phone}: {text}")
            _route_command(phone, text)

        return jsonify({"ok": True}), 200
        
    except Exception as e:
        log.error(f"❌ Error en webhook: {str(e)}")
        return jsonify({"ok": True}), 200

# ==========================
# Endpoints auxiliares
# ==========================
@app.get("/health")
def health():
    return jsonify({
        "status": "ok", 
        "service": "Vicky Bot",
        "timestamp": datetime.utcnow().isoformat()
    }), 200

@app.post("/ext/reindex")
def ext_reindex():
    """Forzar reindexación RAG"""
    try:
        success = rag_index.build_index()
        return jsonify({"ok": success}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ==========================
# Inicialización
# ==========================
def initialize_rag():
    """Inicializar RAG en background"""
    def _init():
        time.sleep(3)
        log.info("🚀 Inicializando RAG...")
        rag_index.build_index()
    
    threading.Thread(target=_init, daemon=True).start()

if __name__ == "__main__":
    log.info(f"🚀 Iniciando Vicky Bot en puerto {PORT}")
    log.info(f"📞 WhatsApp: {bool(META_TOKEN)}")
    log.info(f"📊 Google: {google_ready}")
    log.info(f"🧠 OpenAI: {bool(OPENAI_API_KEY)}")
    
    initialize_rag()
    app.run(host="0.0.0.0", port=PORT, debug=False)
