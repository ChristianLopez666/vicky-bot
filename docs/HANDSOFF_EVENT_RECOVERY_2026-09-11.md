# Base hands-off: reintento recuperable del registro de eventos

Base GitHub: `5738bdf796c4e882ab3d4ebe2bc6db3a420ded62`.

## Defecto y corrección

`EventLog.record` marcaba un evento como visto antes de escribirlo. Una excepción de almacenamiento o la ausencia de acuse de fila podían bloquear el siguiente intento dentro del proceso.

La comprobación, escritura y confirmación ahora se serializan. Solo una escritura con número de fila confirmado incorpora el evento a la ventana de deduplicación. Si falla el primer escritor, otro intento puede guardar el evento. Se conserva el tamaño acotado de la ventana y el identificador determinista que permite a Radar deduplicar después de un reinicio.

## Validación

Se agregaron 7 pruebas unittest sin dependencias externas ni llamadas de red: excepción/reintento, acuse ausente, concurrencia con éxito, concurrencia con primer fallo, expulsión de caché, identidad tras reinicio y rechazo de identificador vacío.

Código base: 3 pruebas fallan por supresión incorrecta del reintento. Código corregido: 7/7 pasan. La suite nueva puede ejecutarse mediante `python3 -m unittest discover -s tests -p test_radar_event_log_recovery.py -v` desde la raíz del repositorio; también se recoge mediante el workflow pytest existente.

## Límites

Esto no crea por sí mismo una cola externa ni un barrido programado: permite que una repetición llegue al almacenamiento, pero no inventa un reintento que nunca se produzca. La deduplicación de esta clase sigue siendo por proceso; tras reiniciar puede repetirse una fila, con el mismo event_id para la deduplicación final de Radar.

El lock cubre la escritura síncrona para evitar dos guardados concurrentes. En una demora de Sheets puede aumentar la espera de otras escrituras del mismo EventLog; no se agregaron hilos ni nuevas lecturas. No se modificaron los tiempos de espera del cliente, el contrato 1.1, la identidad de prospectos, los flags, el emisor general ni la carga histórica.

No se realizaron envíos, escrituras productivas, cambios de datos, merge o deploy. El SHA productivo actual y la recuperación completa del conector requieren verificación en Render/Radar. Antes de promover: suite completa y revisión según el gobierno vigente. Reversión mediante revert del commit, sin migraciones.

La finalidad es avanzar hacia operación hands-off sin perder evidencia. Quedan fuera de este cambio la activación del emisor, el barrido durable, el acuse del webhook y la aceptación integral Vicky–Radar.
