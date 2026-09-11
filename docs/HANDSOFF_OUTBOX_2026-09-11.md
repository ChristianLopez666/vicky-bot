# Recuperación automática SECOM → Radar

La bitácora ya guardaba PENDIENTE, pero no existía ningún barrido después de un reinicio. Este cambio usa el proceso web y la hoja existentes, sin servicios ni dependencias nuevas. Con RADAR_EMIT_ENABLED activo, un hilo por proceso inicia tras la primera petición (incluido health) y revisa páginas de 200 filas cada 60 segundos. Intenta hasta 10 eventos por ciclo, aplica espera progresiva hasta 15 minutos y conserva event_id. La identidad de cada fila se comprueba antes de marcarla; una entrega sin acuse exacto permanece pendiente.

El worker tiene su propio cliente Google y reemplaza los hilos de entrega por evento. Relaciona los estados delivered/read/failed con el request_id original mediante source + phone_number_id + lead_id + wamid; si falta o es ambiguo, espera sin inventar la relación. Un webhook sent anterior al recibo original no suprime la copia completa. Registra también los mensajes entrantes asociados a un LEAD_ID existente.

No cambia mensajes comerciales ni activa RADAR_EMIT_ENABLED. No reenvía mensajes WhatsApp. 401/403 mantienen el comportamiento existente: apagan el cliente hasta reinicio/corrección de configuración. Cambios de filas concurrentes pueden ocasionar reintentos, siempre deduplicados por Radar; Sheets no es una cola transaccional. Si la escritura inicial de la bitácora falla antes de que exista una fila, no se garantiza recuperación. Las filas rechazadas antes de corregir Radar necesitan revisión; no se reactivan indiscriminadamente.

Pruebas: tests/test_radar_outbox.py cubre reinicio, acuse perdido, límites, backoff, correlación inequívoca, deduplicación enriquecida, interruptor, payload dañado, fila movida y acuse HTTP exacto. tests/test_radar_outbox_wiring.py cubre mensajes entrantes y persistencia previa. Ejecutar también la suite pytest existente en GitHub Actions antes de integrar.

Activación pendiente: verificar commit activo en Render, comprobar credenciales existentes y aceptación, cargar histórico y revisar resultados, activar emisor y observar EVENTOS_RADAR y D1. Redes requiere su conector y credenciales propios. No se declara la integración integral cerrada con este cambio.

Reversión: revertir commit y reiniciar el servicio con emisor apagado; no borrar EVENTOS_RADAR ni vicky_events.
