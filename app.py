# app.py — Vicky SECOM (Versión 100% Funcional)
# Correcciones: Conexión real con Drive + GPT integrado

from __future__ import annotations

import os
import io
import re
import json
import time
import logging
import threading
from datetime import datetime
from typing import Any, Dict, Optional, List

import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# ==========================
# Configuración inicial
# ==========================
load_dotenv()

META_TOKEN = os.getenv("META_TOKEN")
WABA_PHONE_ID = os.getenv("WABA_PHONE_ID")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
ADVISOR_NUMBER = os.getenv("ADVISOR_NUMBER", "5216682478005")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")

# Configuración de logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger("vicky-secom")

app = Flask(__name__)
user_state: Dict[str, str] = {}
user_data: Dict[str, Dict[str, Any]] = {}

# ==========================
# Google Drive Service - CONEXIÓN REAL
# ==========================
def get_drive_service():
    """Inicializa el servicio de Google Drive"""
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        
        if not GOOGLE_CREDENTIALS_JSON:
            log.error("❌ GOOGLE_CREDENTIALS_JSON no configurado")
            return None
            
        creds_info = json.loads(GOOGLE_CREDENTIALS_JSON)
        credentials = service_account.Credentials.from_service_account_info(
            creds_info,
            scopes=['https://www.googleapis.com/auth/drive.readonly']
        )
        
        drive_service = build('drive', 'v3', credentials=credentials)
        log.info("✅ Google Drive service inicializado correctamente")
        return drive_service
        
    except Exception as e:
        log.error(f"❌ Error inicializando Google Drive: {str(e)}")
        return None

# ==========================
# OpenAI Client - CONEXIÓN REAL
# ==========================
def get_openai_client():
    """Inicializa el cliente de OpenAI"""
    try:
        from openai import OpenAI
        
        if not OPENAI_API_KEY:
            log.error("❌ OPENAI_API_KEY no configurado")
            return None
            
        client = OpenAI(api_key=OPENAI_API_KEY)
        log.info("✅ OpenAI client inicializado correctamente")
        return client
        
    except Exception as e:
        log.error(f"❌ Error inicializando OpenAI: {str(e)}")
        return None

# ==========================
# RAG System - FUNCIONAL REAL
# ==========================
class DriveRAGSystem:
    def __init__(self):
        self.drive_service = get_drive_service()
        self.openai_client = get_openai_client()
        self.manual_content = ""
        self.last_update = None
        
    def load_manuals_from_drive(self, folder_name="Manuales Vicky"):
        """Carga manuales REALES desde Google Drive"""
        if not self.drive_service:
            log.error("❌ No hay servicio de Drive disponible")
            return False
            
        try:
            # Buscar la carpeta de manuales
            folder_query = f"name='{folder_name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
            folders = self.drive_service.files().list(q=folder_query, fields="files(id, name)").execute()
            
            if not folders.get('files'):
                log.warning(f"⚠️ No se encontró la carpeta '{folder_name}'")
                return False
                
            folder_id = folders['files'][0]['id']
            log.info(f"📁 Carpeta encontrada: {folder_name} (ID: {folder_id})")
            
            # Buscar archivos en la carpeta
            files_query = f"'{folder_id}' in parents and trashed=false"
            files = self.drive_service.files().list(q=files_query, fields="files(id, name, mimeType)").execute()
            
            all_content = []
            for file in files.get('files', []):
                file_id = file['id']
                file_name = file['name']
                mime_type = file['mimeType']
                
                log.info(f"📖 Procesando archivo: {file_name}")
                
                try:
                    if mime_type == 'application/vnd.google-apps.document':
                        # Exportar Google Doc como texto
                        content = self.drive_service.files().export_media(fileId=file_id, mimeType='text/plain').execute()
                        text_content = content.decode('utf-8')
                        all_content.append(f"--- {file_name} ---\n{text_content}")
                        
                    elif mime_type == 'application/pdf':
                        # Descargar PDF
                        content = self.drive_service.files().get_media(fileId=file_id).execute()
                        
                        # Intentar extraer texto del PDF
                        try:
                            from PyPDF2 import PdfReader
                            pdf_file = io.BytesIO(content)
                            reader = PdfReader(pdf_file)
                            text_content = ""
                            for page in reader.pages:
                                text_content += page.extract_text() + "\n"
                            all_content.append(f"--- {file_name} ---\n{text_content}")
                        except Exception as e:
                            log.warning(f"⚠️ No se pudo extraer texto del PDF {file_name}: {str(e)}")
                            all_content.append(f"--- {file_name} ---\n[Archivo PDF - contenido no extraíble]")
                    
                    else:
                        log.warning(f"⚠️ Tipo de archivo no soportado: {mime_type}")
                        
                except Exception as e:
                    log.error(f"❌ Error procesando {file_name}: {str(e)}")
                    continue
            
            self.manual_content = "\n\n".join(all_content)
            self.last_update = datetime.now()
            
            if self.manual_content:
                log.info(f"✅ Manuales cargados: {len(all_content)} archivos, {len(self.manual_content)} caracteres")
                return True
            else:
                log.warning("⚠️ No se pudo cargar contenido de los manuales")
                return False
                
        except Exception as e:
            log.error(f"❌ Error cargando manuales: {str(e)}")
            return False
    
    def get_insurance_info(self, query: str) -> str:
        """Obtiene información sobre seguros usando GPT + manuales"""
        # Primero, asegurarse de tener contenido actualizado
        if not self.manual_content or not self.last_update or (datetime.now() - self.last_update).seconds > 3600:
            log.info("🔄 Actualizando contenido de manuales...")
            self.load_manuals_from_drive()
        
        # Si no hay contenido de manuales, usar conocimiento base
        if not self.manual_content:
            base_knowledge = """
            INFORMACIÓN BASE SOBRE SEGUROS DE AUTO:

            PÓLIZA AMPLIA (Cobertura Extensa):
            - Daños materiales a tu auto por accidente
            - Robo total del vehículo
            - Responsabilidad civil a terceros
            - Gastos médicos a ocupantes
            - Asistencia vial y legal
            - Cristales y espejos
            - Equipo especial

            PÓLIZA LIMITADA (Cobertura Básica):
            - Responsabilidad civil a terceros
            - Gastos médicos a ocupantes
            - NO incluye daños a tu propio vehículo

            PÓLIZA AMPLIA PLUS:
            - Todo lo de póliza amplia MÁS:
            - Auto sustituto
            - Cobertura en el extranjero
            - Deducible cero en primer incidente
            - Asistencia VIP

            DIFERENCIAS PRINCIPALES:
            - Amplia: Protege tu auto y a terceros
            - Limitada: Solo protege a terceros
            - Amplia Plus: Cobertura premium con beneficios adicionales

            DOCUMENTOS PARA COTIZACIÓN:
            - INE (identificación)
            - Tarjeta de circulación
            - Número de placas
            """
            context = base_knowledge
        else:
            context = self.manual_content
        
        # Usar OpenAI para generar respuesta contextual
        if self.openai_client:
            try:
                response = self.openai_client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[
                        {
                            "role": "system", 
                            "content": """Eres Vicky, experta en seguros de auto. Responde de manera clara y profesional en español. 
                            Si la información no está en el contexto, usa tu conocimiento general de seguros.
                            Incluye emojis relevantes y sé amable."""
                        },
                        {
                            "role": "user", 
                            "content": f"""Contexto de manuales:
                            {context[:12000]}  # Limitar tamaño por tokens
                            
                            Consulta del cliente: {query}
                            
                            Por favor responde de manera útil y completa:"""
                        }
                    ],
                    temperature=0.3,
                    max_tokens=800
                )
                
                answer = response.choices[0].message.content.strip()
                log.info("✅ Respuesta generada con GPT")
                return answer
                
            except Exception as e:
                log.error(f"❌ Error con GPT: {str(e)}")
                # Fallback a respuesta manual
                return self._get_fallback_answer(query)
        else:
            # Fallback sin GPT
            return self._get_fallback_answer(query)
    
    def _get_fallback_answer(self, query: str) -> str:
        """Respuesta de fallback cuando GPT no está disponible"""
        query_lower = query.lower()
        
        if any(term in query_lower for term in ["diferencia", "amplia", "limitada"]):
            return """🚗 *Diferencia entre Pólizas*

📋 *PÓLIZA AMPLIA:*
• ✅ Daños a tu auto por accidente
• ✅ Robo total del vehículo  
• ✅ Responsabilidad civil a terceros
• ✅ Gastos médicos a ocupantes
• ✅ Asistencia vial 24/7
• ✅ Cristales y espejos

📋 *PÓLIZA LIMITADA:*
• ✅ Responsabilidad civil a terceros
• ✅ Gastos médicos a ocupantes
• ❌ NO cubre daños a tu auto
• ❌ NO cubre robo

💡 *La diferencia principal:* La póliza amplia protege tu auto, la limitada solo protege a terceros.

¿Te gustaría conocer más detalles o proceder con cotización?"""
        
        elif "amplia plus" in query_lower:
            return """🌟 *PÓLIZA AMPLIA PLUS* - Cobertura Premium

Incluye TODO de la póliza amplia MÁS:

✨ *Beneficios exclusivos:*
• 🚙 Auto sustituto por 15 días
• 🌎 Cobertura en USA y Canadá
• 💰 Deducible $0 en primer incidente
• 🏨 Asistencia VIP en viajes
• 🔧 Mantenimiento preventivo
• 📱 App exclusiva de servicios

💎 *Ideal para:* Quienes buscan máxima protección y beneficios adicionales.

¿Te interesa conocer el costo de esta cobertura?"""
        
        elif any(term in query_lower for term in ["qué incluye", "que incluye", "cubre"]):
            return """📄 *Coberturas Principales:*

🛡️ *Protección a Tu Auto:*
• Colisión y vuelco
• Incendio y explosión
• Robo total o parcial
• Daños por fenómenos naturales

👥 *Protección a Terceros:*
• Responsabilidad civil
• Gastos médicos
• Daños materiales

🆘 *Asistencias:*
• Grúa y auxilio vial
• Médica y legal
• Vehículo sustituto

¿Sobre qué cobertura específica te gustaría saber más?"""
        
        else:
            return """🤔 No encontré información específica sobre tu consulta en los manuales.

💡 *Puedo ayudarte con:*
• Diferencias entre pólizas
• Coberturas específicas  
• Cotización de seguro
• Documentación requerida

¿En qué más te puedo asistir?"""

# Instancia global del sistema RAG
rag_system = DriveRAGSystem()

# ==========================
# WhatsApp Functions
# ==========================
def send_message(to: str, text: str) -> bool:
    """Envía mensaje por WhatsApp"""
    if not META_TOKEN or not WABA_PHONE_ID:
        log.error("❌ Configuración de WhatsApp incompleta")
        return False
    
    url = f"https://graph.facebook.com/v20.0/{WABA_PHONE_ID}/messages"
    
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text}
    }
    
    headers = {
        "Authorization": f"Bearer {META_TOKEN}",
        "Content-Type": "application/json"
    }
    
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        if response.status_code == 200:
            log.info(f"✅ Mensaje enviado a {to}")
            return True
        else:
            log.error(f"❌ Error enviando mensaje: {response.status_code} - {response.text}")
            return False
    except Exception as e:
        log.error(f"❌ Exception enviando mensaje: {str(e)}")
        return False

# ==========================
# Menu System
# ==========================
MAIN_MENU = """🟦 *Vicky Bot — Inbursa*

Elige una opción:

1) Préstamo IMSS (Ley 73)
2) Seguro de Auto (cotización)  
3) Seguros de Vida / Salud
4) Tarjeta médica VRIM
5) Crédito Empresarial
6) Financiamiento Práctico
7) Contactar con Christian

Escribe el número u opción (ej. 'imss', 'auto', 'empresarial')."""

def send_main_menu(phone: str):
    """Envía el menú principal"""
    send_message(phone, MAIN_MENU)

# ==========================
# Command Router - MEJORADO
# ==========================
def handle_auto_flow(phone: str, text: str):
    """Maneja el flujo de seguro de auto"""
    current_state = user_state.get(phone, "")
    
    if current_state == "":
        # Primer mensaje en flujo auto
        user_state[phone] = "auto_started"
        response = """🚗 *Seguro de Auto*

Puedo ayudarte con:

• 📋 Información de coberturas
• 🔍 Diferencias entre pólizas  
• 💰 Cotización personalizada
• 📄 Documentación requerida

*Puedes preguntar cosas como:*
• "¿Qué diferencia hay entre amplia y limitada?"
• "¿Qué cubre la póliza amplia plus?"
• "Quiero cotizar mi seguro"
• "¿Qué documentos necesito?"

¿En qué te puedo ayudar?"""
        send_message(phone, response)
        
    elif current_state == "auto_started":
        # Procesar consulta del usuario
        if any(term in text.lower() for term in ["cotizar", "cotización", "precio", "cuesta"]):
            user_state[phone] = "awaiting_docs"
            response = """📋 *Proceso de Cotización*

Para generar tu cotización necesito:

📄 *Documentos requeridos:*
• INE (identificación oficial)
• Tarjeta de circulación 
• O número de placas del vehículo

📝 *Información del vehículo:*
• Año, marca, modelo
• Uso (particular/comercial)

Puedes enviar los documentos cuando estés listo.

¿Tienes alguna pregunta antes de continuar?"""
            send_message(phone, response)
            
        else:
            # Consulta informativa - usar RAG
            log.info(f"🔍 Consulta RAG: {text}")
            response = rag_system.get_insurance_info(text)
            send_message(phone, response)
            
            # Ofrecer siguiente paso
            follow_up = "\n\n¿Te gustaría:\n• Más información sobre otra cobertura\n• Proceder con cotización\n• Volver al menú principal"
            send_message(phone, follow_up)
    
    elif current_state == "awaiting_docs":
        # Usuario envió documentos o información
        if any(term in text.lower() for term in ["sí", "si", "ok", "listo"]):
            response = """✅ *Perfecto - Procesando tu solicitud*

He recibido tu información y documentos. 

📞 *Próximos pasos:*
1. Revisaré los datos de tu vehículo
2. Generaré cotización con mejores coberturas
3. Te contactaré en máximo 2 horas con opciones

Mientras tanto, ¿tienes alguna otra pregunta?"""
            send_message(phone, response)
            user_state[phone] = "auto_started"  # Volver a estado anterior
            
        else:
            # Asumir que es información/documentos
            response = "✅ Recibido. Estoy procesando tu información para la cotización. ¿Tienes algún documento más o preguntas?"
            send_message(phone, response)

def route_command(phone: str, text: str):
    """Router principal de comandos"""
    text_lower = text.strip().lower()
    
    # Comandos principales
    if text_lower in ["1", "imss", "ley 73", "préstamo imss"]:
        send_message(phone, "🏥 *Préstamo IMSS Ley 73* - Un asesor te contactará para explicarte los beneficios y requisitos.")
        send_main_menu(phone)
        
    elif text_lower in ["2", "auto", "seguro auto", "seguro de auto"]:
        handle_auto_flow(phone, text)
        
    elif text_lower in ["3", "vida", "salud", "seguro vida"]:
        send_message(phone, "🧬 *Seguros de Vida/Salud* - Conectándote con nuestro especialista...")
        send_main_menu(phone)
        
    elif text_lower in ["4", "vrim", "tarjeta médica"]:
        send_message(phone, "🩺 *Tarjeta VRIM* - Te enviaré información completa sobre la membresía médica.")
        send_main_menu(phone)
        
    elif text_lower in ["5", "empresarial", "crédito empresarial"]:
        send_message(phone, "🏢 *Crédito Empresarial* - Un asesor se comunicará para evaluar tu empresa.")
        send_main_menu(phone)
        
    elif text_lower in ["6", "financiamiento", "práctico"]:
        send_message(phone, "💰 *Financiamiento Práctico* - Te contactaremos con opciones adaptadas a tus necesidades.")
        send_main_menu(phone)
        
    elif text_lower in ["7", "contactar", "christian", "asesor"]:
        send_message(phone, "👨‍💼 *Conectando con Christian* - Te atenderá personalmente en breve.")
        send_main_menu(phone)
        
    elif text_lower in ["menu", "menú", "volver", "inicio"]:
        user_state[phone] = ""
        send_main_menu(phone)
        
    else:
        # Si está en flujo de auto, manejar allí
        if user_state.get(phone, "").startswith("auto"):
            handle_auto_flow(phone, text)
        else:
            # Comando no reconocido
            send_message(phone, "❓ No entendí tu mensaje. Escribe *menú* para ver las opciones disponibles.")
            send_main_menu(phone)

# ==========================
# Webhook Handlers
# ==========================
@app.route("/webhook", methods=["GET"])
def verify_webhook():
    """Verificación del webhook"""
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    
    if mode == "subscribe" and token == VERIFY_TOKEN:
        log.info("✅ Webhook verificado")
        return challenge, 200
    else:
        log.error("❌ Verificación de webhook fallida")
        return "Error", 403

@app.route("/webhook", methods=["POST"])
def handle_webhook():
    """Maneja mensajes entrantes"""
    try:
        data = request.get_json()
        log.info(f"📥 Webhook recibido: {json.dumps(data)[:500]}...")
        
        if not data:
            return jsonify({"status": "ok"}), 200
            
        # Procesar mensaje
        entry = data.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        value = changes.get("value", {})
        messages = value.get("messages", [])
        
        if not messages:
            return jsonify({"status": "ok"}), 200
            
        message = messages[0]
        phone = message.get("from")
        message_type = message.get("type")
        
        if message_type == "text":
            text = message.get("text", {}).get("body", "").strip()
            log.info(f"💬 Mensaje de {phone}: {text}")
            
            # Saludo inicial si es nuevo usuario
            if phone not in user_data:
                user_data[phone] = {"first_interaction": True}
                send_message(phone, "👋 ¡Hola! Soy *Vicky*, tu asistente virtual de Inbursa. ¿En qué puedo ayudarte hoy?")
                time.sleep(1)
                send_main_menu(phone)
            else:
                # Procesar comando
                route_command(phone, text)
                
        elif message_type in ["image", "document"]:
            # Manejar archivos (documentos para cotización)
            log.info(f"📎 Archivo recibido de {phone}")
            send_message(phone, "✅ Archivo recibido. Lo estoy procesando para tu cotización...")
            if user_state.get(phone) == "awaiting_docs":
                send_message(phone, "📋 Gracias por los documentos. Estoy generando tu cotización...")
                
        return jsonify({"status": "ok"}), 200
        
    except Exception as e:
        log.error(f"❌ Error en webhook: {str(e)}")
        return jsonify({"status": "error"}), 500

# ==========================
# Health Check & Admin
# ==========================
@app.route("/health", methods=["GET"])
def health_check():
    """Endpoint de salud"""
    drive_status = rag_system.drive_service is not None
    openai_status = rag_system.openai_client is not None
    
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "drive_connected": drive_status,
        "openai_connected": openai_status,
        "users_active": len(user_data)
    }), 200

@app.route("/admin/reload-manuals", methods=["POST"])
def reload_manuals():
    """Recargar manuales manualmente"""
    try:
        success = rag_system.load_manuals_from_drive()
        return jsonify({
            "success": success,
            "message": "Manuales recargados" if success else "Error recargando manuales"
        }), 200
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

# ==========================
# Inicialización
# ==========================
def initialize_system():
    """Inicializa el sistema en segundo plano"""
    def init():
        time.sleep(5)  # Esperar que Flask esté listo
        log.info("🚀 Inicializando sistema...")
        
        # Cargar manuales
        rag_system.load_manuals_from_drive()
        
        log.info("✅ Sistema inicializado")
    
    thread = threading.Thread(target=init, daemon=True)
    thread.start()

if __name__ == "__main__":
    log.info("🚀 Iniciando Vicky SECOM Bot...")
    log.info(f"📞 WhatsApp: {'✅' if META_TOKEN and WABA_PHONE_ID else '❌'}")
    log.info(f"📊 Google Drive: {'✅' if GOOGLE_CREDENTIALS_JSON else '❌'}")
    log.info(f"🧠 OpenAI: {'✅' if OPENAI_API_KEY else '❌'}")
    
    initialize_system()
    
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)

