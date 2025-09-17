📖 Vicky Bot – FASE 1

Asistente automatizado en WhatsApp usando Flask + Meta Cloud API + Google Sheets + GPT.
Este proyecto permite recibir y responder mensajes de WhatsApp, almacenar datos en Google Sheets y generar respuestas inteligentes con GPT.

🚀 Arquitectura del Sistema
bot-vicky/
│
├── app.py                 # Punto de entrada principal (Flask + Webhook)
├── config_env.py          # Configuración y variables de entorno
├── core_router.py         # Lógica de enrutamiento de mensajes
├── core_whatsapp.py       # Cliente para la API de WhatsApp
├── integrations_gpt.py    # Cliente para GPT
├── leer_secom.py          # Lector de datos de Google Sheets
├── render_client.py       # Cliente de despliegue Render
├── deploy_vicky.py        # Script auxiliar de despliegue
├── probar_webhook.py      # Script para pruebas locales de Webhook
│
├── requirements.txt       # Dependencias del sistema
├── Procfile               # Configuración de Render/Gunicorn
├── render.yaml            # Configuración de Render
├── .env                   # Variables de entorno (local)
├── .env.sample            # Ejemplo de variables de entorno
├── .gitignore             # Archivos ignorados en Git
│
├── tests_...              # Scripts de prueba
└── README.md              # Documentación

⚙️ Configuración del Entorno

Clonar el repositorio:

git clone https://github.com/ChristianLopez666/vicky-bot.git
cd vicky-bot


Crear y activar entorno virtual:

python -m venv venv
venv\Scripts\activate   # En Windows
source venv/bin/activate  # En Linux/Mac


Instalar dependencias:

pip install -r requirements.txt

🔑 Variables de Entorno

Configurar en .env local y en Render:

# Meta WhatsApp
VERIFY_TOKEN=tu_verify_token
WHATSAPP_TOKEN=tu_token_permanente
PHONE_NUMBER_ID=tu_phone_number_id

# OpenAI
OPENAI_API_KEY=tu_openai_key

# Google Sheets
GOOGLE_SHEET_ID=tu_id_google_sheet
GOOGLE_CREDENTIALS_JSON={"tipo":"service_account","project_id":"..."}

🌐 Webhook Flask

Endpoint de verificación:
GET /webhook → responde con el challenge de Meta si el VERIFY_TOKEN coincide.

Endpoint de mensajes:
POST /webhook → recibe mensajes de WhatsApp, los enruta y responde.

📲 Integración con Meta Cloud API

Autenticación vía Authorization: Bearer WHATSAPP_TOKEN.

Envío de mensajes: POST https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages.

Recibe mensajes a través del webhook de Flask.

📑 Integración con Google Sheets

Lectura/escritura usando gspread.

Autenticación vía GOOGLE_CREDENTIALS_JSON.

Se espera que la hoja tenga columnas estándar: Nombre, RFC, WhatsApp, Estado.

🤖 Integración con GPT

Usa la librería openai con OPENAI_API_KEY.

Permite generar respuestas naturales en base al mensaje recibido.

Manejo de tokens y contexto simple (una sesión por usuario).

🧪 Pruebas Locales

Ejecutar Flask:

python app.py


Exponer con ngrok:

ngrok http 5000


Configurar el Webhook en Meta con la URL de ngrok.

Simular mensajes:

python probar_webhook.py

☁️ Despliegue en Render

Subir código al repo GitHub.

Render detecta Procfile y ejecuta:

gunicorn app:app --bind 0.0.0.0:$PORT


Configurar variables de entorno en Render.

Revisar logs en caso de error:

render logs

✅ Checklist de Verificación

 Webhook verificado en Meta.

 Flask responde en /health.

 Render despliega sin errores.

 WhatsApp recibe y envía mensajes.

 GPT responde de forma coherente.

 Google Sheets guarda datos.

📘 Ejemplo de Flujo

Usuario envía: "Hola Vicky"

Webhook recibe en app.py → enrutado a core_router.

GPT genera respuesta: "Hola, soy Vicky, asistente de Christian López. ¿Cómo puedo ayudarte?"

Se envía respuesta vía core_whatsapp.py.

Datos del usuario se registran en Google Sheets.