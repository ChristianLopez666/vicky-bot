# Vicky Bot — Fase 1 (Flask + WhatsApp Cloud API)

Servicio Flask listo para Render.com que atiende el webhook de WhatsApp, gestiona menú, funneles y reenvía medios al asesor mediante **download → upload → send** (requisito de la API).

---

## Variables de entorno (Render → *Environment*)
| Variable | Requerida | Descripción |
|---|---|---|
| `META_TOKEN` | ✅ | Token de la app con permisos `whatsapp_business_messaging` |
| `PHONE_NUMBER_ID` | ✅ | ID del número de WhatsApp Business |
| `VERIFY_TOKEN` | ✅ | Se usa en la verificación GET del webhook |
| `META_APP_SECRET` | Opcional | Activa validación HMAC SHA-256 del webhook |
| `ADMIN_TOKEN` | Opcional | Token para `/admin/status` |
| `ADVISOR_NOTIFY_NUMBER` | ✅ | Número E.164 al que se reenvían medios |
| `PORT` | Opcional | `10000` en Render |
| `FLASK_DEBUG` | Opcional | `false` en producción |
| `TZ` | Opcional | `America/Mazatlan` |

> **Nota:** Define todas las variables en Render **antes** de desplegar. No subas secretos al repo.

---

## Endpoints
- `GET /health` → `200 OK`
- `GET /webhook` → Handshake de verificación (`hub.verify_token` vs `VERIFY_TOKEN`)
- `POST /webhook` → Recepción de mensajes; soporta `text`, `interactive`, `image`, `document`, `video`, `audio`
- `GET /admin/status` → Estado del servicio (**requiere** header `X-Admin-Token: <ADMIN_TOKEN>`)

---

## Despliegue en Render (Infra as code)
1. Sube este repo (con `app.py`, `requirements.txt`, `render.yaml`, `Procfile`).
2. En Render → **New +** → **From YAML** → selecciona `render.yaml`.
3. Configura las **Environment Variables** indicadas arriba.
4. Render instalará dependencias y levantará `web: python app.py`.
5. Copia la URL pública del servicio para configurarla como **Webhook URL** en Meta:
   - **Callback URL:** `https://<tu-servicio>.onrender.com/webhook`
   - **Verify Token:** el valor de `VERIFY_TOKEN`

---

## Pruebas rápidas
```bash
curl -sSf https://<tu-servicio>.onrender.com/health
# -> OK
```

Para verificar webhook desde Meta, asegúrate de que `VERIFY_TOKEN` coincida.
El endpoint `POST /webhook` responderá `EVENT_RECEIVED` si procesa el payload.

---

## Local (opcional)
```bash
python -m venv .venv && . .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export META_TOKEN=... PHONE_NUMBER_ID=... VERIFY_TOKEN=...
python app.py
# http://127.0.0.1:5000/health
```

---

## Troubleshooting
- **Duplicación de mensajes en debug:** el archivo ya desactiva el *reloader* (`use_reloader=False`).
- **403 en reenvío de medios:** recuerda que se hace `download → upload → send`. Si falla, revisa permisos y tamaño.
- **403 firma inválida:** si defines `META_APP_SECRET`, Meta firmará el webhook; el servicio lo validará. Si no, deja la var vacía.
- **502 en Render:** revisa logs, variables y que el puerto sea `10000` (Render lo inyecta pero aquí se fija por env).

---

## Licencia
Uso interno COHIFIS / Christian López.
