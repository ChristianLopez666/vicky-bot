# Vicky Bot – Fase 1 (WhatsApp Cloud API + Flask + Google Sheets)

**Cambios clave**  
- Bind correcto para Render: `gunicorn app:app -b 0.0.0.0:$PORT` (en `Procfile` y `render.yaml`).  
- `.env.sample` sin `PORT` (Render asigna el puerto).

**Variables mínimas en Render**  
`META_TOKEN`, `PHONE_NUMBER_ID=712597741555047`, `VERIFY_TOKEN=vicky-verify-2025`,  
`GOOGLE_CREDENTIALS_JSON` (una sola línea), `GSHEET_PROSPECTS_ID`, `GSHEET_SOLICITUDES_ID`,  
`ADVISOR_NOTIFY_NUMBER=5216682478005`.

**Webhook en Meta**  
URL: `https://TU-APP.onrender.com/webhook` — Verify: `vicky-verify-2025` — Suscribir `messages`.

**Flujo**  
Menú inicial, match por últimos 10 dígitos, beneficio de auto, opción 8 notifica, registro en “Solicitudes Vicky”.
