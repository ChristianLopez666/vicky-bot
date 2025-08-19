
# Vicky Bot (Flask) – 360dialog / Cloud API + Google Sheets

Listo para desplegar en Render y conectar a WhatsApp (360dialog o Meta Cloud API).

## 1) Requisitos previos
- **Número oficial**: usarás `+52 1 668 185 5146` en 360dialog (según tu decisión).
- **Cuenta 360dialog** (o Meta Cloud API).
- **Google Sheet** con tu base: **`Prospectos SECOM Auto`** (primera pestaña).
  - Encabezados sugeridos: `Nombre | RFC | WhatsApp` (pueden ser sinónimos).
  - Comparación por **últimos 10 dígitos**.

## 2) Variables de entorno
Crea las variables en Render (o en `.env` local). Usa `.env.sample` como guía.

Obligatorias según proveedor:

**Si usas 360dialog**
- `WHATSAPP_PROVIDER=360dialog`
- `D360_API_KEY=...`
- `D360_URL=https://waba.360dialog.io/v1/messages` (por defecto)

**Si usas Meta Cloud API (alternativa)**
- `WHATSAPP_PROVIDER=cloud`
- `META_TOKEN=...`
- `PHONE_NUMBER_ID=...`

**Comunes**
- `VERIFY_TOKEN` (elige una cadena; la usarás al validar el webhook)
- `GOOGLE_CREDENTIALS_JSON` → pega el *JSON completo* de tu Service Account en **una sola línea** (Render lo admite).
- `SHEET_NAME=Prospectos SECOM Auto`
- `REQUESTS_SHEET_NAME=Solicitudes Vicky`
- `ADVISOR_NUMBER=5216682478005` (tu línea personal)
- `MENU_AUTO_DISCOUNT=60`
- `NOTIFY_WEBHOOK_URL` (opcional; para n8n/Zapier)

> **Compartir la hoja**: en Google Sheets, comparte **`Prospectos SECOM Auto`** con el email de la *service account* (`xxx@xxx.iam.gserviceaccount.com`) **con permisos de editor**.

## 3) Despliegue en Render
1. Crea un *nuevo servicio web* desde tu repo o subiendo el ZIP.
2. En **Build** y **Start**, Render usa automáticamente:
   - Build: `pip install -r requirements.txt`
   - Start: `gunicorn app:app --timeout 120 --workers 2 --threads 4`
3. Configura todas las **Environment Variables** (ver sección anterior).
4. Despliega. Verás tu URL pública: `https://<tu-app>.onrender.com`.

## 4) Webhook de WhatsApp
- En 360dialog, configura el **Webhook URL** a: `https://<tu-app>.onrender.com/webhook`
- **Verify Token**: usa exactamente el valor de tu `VERIFY_TOKEN`.
- En pruebas, 360dialog/Cloud hará `GET /webhook?hub.mode=subscribe&hub.verify_token=...&hub.challenge=...`.
  - Si coincide, responderá con el `challenge` y quedará **verificado**.

## 5) ¿Qué hace Vicky ahora? (Fase 1 completa)
- Responde a *cualquier mensaje* mostrando el **MENÚ** automáticamente (también cuando escriban *MENÚ*).
- **Identifica** al cliente por su **número (últimos 10 dígitos)** en la hoja y, si hay match, personaliza el saludo y muestra beneficio de **hasta 60%** en auto.
- **Menú** con 8 opciones:
  1. Pensiones IMSS
  2. Seguro de auto (Amplia PLUS / Amplia / Limitada)
  3. Seguros de vida y salud
  4. Tarjetas médicas VRIM
  5. Préstamos a pensionados IMSS
  6. Financiamiento empresarial
  7. Nómina empresarial
  8. Contactar con Christian
- **Préstamos**: solicita *monto* y *plazo* (12/24/36/48/60), registra la solicitud en la hoja `Solicitudes Vicky` y **notifica** a Christian (WhatsApp + webhook opcional).
- Registra también los mensajes relevantes de las otras opciones para que des seguimiento.

## 6) Pruebas rápidas
- `GET /health` → debe responder `{ ok: true }`.
- Verifica el webhook en 360dialog.
- Escríbele *desde un número de prueba* (de tus listas). Debe:
  - Mostrar menú.
  - Personalizar saludo si hay match.
  - Responder a las opciones 1–8.
  - En opción 5, pedir *monto* y *plazo*, registrar en la hoja, y notificarte.

## 7) Estructura
- `app.py` → Flask + lógica de flujo + envío WhatsApp + Google Sheets.
- `requirements.txt`, `Procfile`, `render.yaml`.
- `.env.sample` (referencia).

## 8) Errores comunes y soluciones
- **401 al enviar WhatsApp** → revisa `D360_API_KEY` (360dialog) o `META_TOKEN`/`PHONE_NUMBER_ID` (Cloud).
- **403 al verificar webhook** → el `VERIFY_TOKEN` no coincide con el configurado en tu proveedor.
- **Hoja vacía** → verifica compartir la hoja con la *service account* **como editor**.
- **No personaliza saludo** → normaliza los teléfonos a 10 dígitos en la hoja; Vicky hace match por *últimos 10*.

## 9) Personalización rápida
- Cambia el texto del menú en `build_menu_text(...)`.
- Ajusta el descuento mostrado con `MENU_AUTO_DISCOUNT` (env var).
- Modifica los prompts de cada opción en `handle_menu_choice(...)`.
- Agrega más rutas/estados en `continue_flow(...)`.

---

**Hecho para:** Christian López (Grupo Financiero Inbursa).  
**Objetivo Fase 1:** *Listo para producir*.  
Siguiente paso (Fase 2 “SuperVicky”): IA conversacional avanzada, plantillas interactivas, lectura de bases múltiples, y orquestación con n8n/Google Cloud.
