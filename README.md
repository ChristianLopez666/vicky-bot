# 🤖 Vicky Bot

Asistente de **Christian López**, integrado con **WhatsApp Cloud API** y **OpenAI GPT**.  
Permite atención automática de clientes, menú de opciones y respuestas inteligentes.

---

## 🚀 Funcionalidad

- Respuesta automática en WhatsApp.
- Menú inicial (pensiones, seguros, préstamos, etc.).
- Integración con GPT para interpretar texto libre.
- Notificación al asesor (`ADVISOR_NUMBER`) cuando corresponde.
- Deploy automático en **Render**.

---

## 📂 Estructura del proyecto

.
├── app.py
├── config_env.py
├── core_router.py
├── core_whatsapp.py
├── integrations_gpt.py
├── requirements.txt
├── Procfile
├── render.yaml
├── .env.sample
└── README.md

makefile
Copiar código

---

## ⚙️ Variables de entorno

Configura en Render → **Environment** (y en `.env` en local):

```ini
# Meta / WhatsApp
VERIFY_TOKEN=vicky-verify-2025
META_TOKEN=your_meta_token_here
META_APP_SECRET=your_meta_app_secret_here
PHONE_NUMBER_ID=your_phone_number_id_here
WA_API_VERSION=v20.0

# Asesor
ADVISOR_NUMBER=5216682478005

# OpenAI
OPENAI_API_KEY=your_openai_api_key_here
GPT_MODEL=gpt-4o-mini

# Flask / Render
PORT=5000
LOG_LEVEL=INFO
PYTHON_VERSION=3.11.9
🖥️ Uso en local
Clona el repo:

bash
Copiar código
git clone https://github.com/ChristianLopez666/vicky-bot.git
cd vicky-bot
Crea entorno virtual:

bash
Copiar código
python -m venv venv
source venv/bin/activate   # Linux/Mac
venv\Scripts\activate      # Windows
Instala dependencias:

bash
Copiar código
pip install -r requirements.txt
Ejecuta en local:

bash
Copiar código
python app.py
Expón con ngrok:

bash
Copiar código
ngrok http 5000
Configura tu Webhook en Meta con la URL de ngrok.

☁️ Deploy en Render
Sube todo el repo a GitHub.

En Render:

New Web Service → conecta el repo.

Build Command: pip install -r requirements.txt

Start Command: gunicorn app:app --bind 0.0.0.0:$PORT

Revisa que el servicio quede Live.

Configura la URL de Render en tu App de Meta (/webhook).

✅ Pruebas rápidas
Saludo inicial: envía “hola” → muestra menú.

Opción numérica: envía “2” → muestra seguros de auto.

Texto libre: envía “Quiero un préstamo de 50 mil” → GPT responde.

Notificación a Christian: envía “8” → llega mensaje al asesor.

📌 Notas
No subas tu .env real a GitHub, usa solo .env.sample.

GPT solo responde si tienes OPENAI_API_KEY configurada en Render.

Meta puede tardar 1–2 minutos en propagar cambios de webhook.

yaml
Copiar código

---

¿Quieres que te prepare también un **checklist paso a paso** (tipo bitácora) para que no se te pase nada en la activación de Vicky Bot Fase 1 + GPT?







Preguntar a ChatGPT




