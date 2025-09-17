Instalación local (venv, pip install -r requirements.txt).

Copiar .env.sample a .env y rellenar.

Correr local: python app.py (o flask run).

Probar sin Meta: python tests_probar_webhook.py.

Deploy en Render con este repo:

Build: pip install -r requirements.txt

Start: gunicorn app:app --bind 0.0.0.0:$PORT

Troubleshooting:

ImportError WA_API_VERSION: confirma que existe en config_env.py y .env.

400/401 al enviar: revisa WHATSAPP_TOKEN y PHONE_NUMBER_ID.

Sin GPT/Sheets: el bot sigue operativo (menú + opciones + notificación).