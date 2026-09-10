# radar_acceptance.py — ejecucion unica de aceptacion contra Radar (contrato 1.1)
#
# Nace de un pedido puntual (2026-09-09): con las credenciales de SECOM ya
# guardadas en Radar y en Render, y Radar desplegado con accepting:false,
# falta correr la bateria de aceptacion del contrato -- firma, evento valido,
# duplicado, lead desconocido y aislamiento -- una sola vez, mientras Work
# abre Radar temporalmente y lo vuelve a cerrar.
#
# Regla de diseno que gobierna todo el archivo: esta ejecucion se habilita
# UNICAMENTE dentro de si misma. Construye su propio RadarClient desechable,
# con enabled=True solo en la variable local `cliente` de run(); nunca importa
# ni muta app.py:_radar_client, y jamas toca RADAR_EMIT_ENABLED. Terminada la
# llamada, el objeto se descarta y el emisor general sigue exactamente como
# estaba. Ver app.py:/ext/radar/acceptance-test para el endpoint que invoca
# esto, protegido por su propio secreto (RADAR_ACCEPTANCE_TOKEN).
#
# No decide nada por su cuenta: registra lo que Radar respondio en cada caso
# y compara contra lo que el contrato promete. El juicio final ("aprobamos
# la aceptacion") lo da Work mirando el reporte, no este modulo.
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

import radar_events

# Fuente ajena usada solo en el check de consistencia cabecera/cuerpo
# (aislamiento_fuente): el cuerpo declara esta fuente mientras la cabecera
# sigue declarando la real (vicky_secom, la que prueban el token y el HMAC).
# No es una prueba de aislamiento de credenciales entre SECOM y Redes -- eso
# exige que Redes tenga su propio token real en Radar, y todavia no lo tiene
# (correccion de Work, 2026-09-09).
OTRA_FUENTE = "vicky_redes"

# Identidad reservada para estas pruebas. Es un UUID valido pero jamas
# asignado a un prospecto real (empieza en puros ceros salvo la version/
# variante del UUID, que se fijan para que siga siendo un UUID v4 valido).
# Cualquier fila que Radar guarde bajo este LEAD_ID es identificable como
# prueba y se puede filtrar o borrar sin tocar datos comerciales.
LEAD_ID_DESCONOCIDO = "SC-00000000-0000-4000-8000-000000000000"

# lead_id con formato RS-<uuid5> valido, reservado y nunca real, para el
# check aislamiento_fuente. Correccion de Work (2026-09-10, verificada antes
# de autorizar el merge): las dos corridas anteriores usaban known_lead_id
# (formato SC-), y Radar rechazaba con 400 "Vicky Redes requiere
# lead.lead_id RS-<uuid5>" -- un 400 real, pero por el formato del lead_id,
# no por la inconsistencia cabecera/cuerpo que el check dice probar. Con un
# RS-<uuid5> sintacticamente valido, el 400 solo puede venir de que la
# cabecera declara vicky_secom y el cuerpo declara vicky_redes.
LEAD_ID_REDES_FORMATO_VALIDO = "RS-00000000-0000-5000-8000-000000000000"

# Firma deliberadamente invalida: 64 hex de ceros. Nunca coincide con ningun
# HMAC real, y no depende de acertarle a la firma correcta para diferir de
# ella -- basta con que sea sintacticamente una firma sha256 y no la correcta.
FIRMA_INVALIDA = "sha256=" + "0" * 64

# Telefono de relleno para los checks que no necesitan un prospecto real
# (firma_invalida, lead_desconocido, aislamiento_fuente): cumple el formato
# que Radar exige (521 + 10 digitos) sin corresponder a nadie. El check
# "valido" (y "duplicado", que lo reenvia) usa en su lugar el telefono real
# ligado a known_lead_id, para que la prueba sea representativa de verdad.
TELEFONO_RELLENO_LAST10 = "0000000000"


def _post(
    client: "radar_events.RadarClient",
    event: Dict[str, Any],
    *,
    source_override: Optional[str] = None,
    signature_override: Optional[str] = None,
    poster: Optional[Callable] = None,
) -> Dict[str, Any]:
    """POST de bajo nivel para un check de aceptacion.

    A diferencia de RadarClient.send() -- que colapsa la respuesta al
    tri-estado ENVIADO/RECHAZADO/PENDIENTE que le basta al emisor real -- esto
    devuelve el status y el cuerpo completos de la respuesta de Radar, que es
    justo lo que hace falta para un reporte de aceptacion verificable.
    """
    cuerpo = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    delivery_id = str(uuid.uuid4())
    timestamp = str(int(datetime.now(timezone.utc).timestamp()))
    cabeceras = client.headers_for(
        cuerpo, delivery_id=delivery_id, timestamp=timestamp, source=source_override,
    )
    if signature_override is not None:
        cabeceras["X-Vicky-Signature"] = signature_override

    enviar = poster or client._post
    if enviar is None:
        import requests
        enviar = requests.post

    t0 = time.monotonic()
    try:
        resp = enviar(client.url, data=cuerpo, headers=cabeceras, timeout=client.timeout)
    except Exception as exc:
        return {
            "status_code": None,
            "error": f"{type(exc).__name__}: {exc}",
            "elapsed_ms": round((time.monotonic() - t0) * 1000),
        }

    try:
        cuerpo_resp = resp.json()
    except Exception:
        cuerpo_resp = {"_raw": str(getattr(resp, "text", ""))[:500]}

    return {
        "status_code": getattr(resp, "status_code", None),
        "body": cuerpo_resp,
        "elapsed_ms": round((time.monotonic() - t0) * 1000),
    }


def run(
    *,
    url: str,
    token: str,
    hmac_secret: str,
    dispatch_token: str,
    phone_number_id: str,
    known_lead_id: str,
    known_lead_phone_last10: str,
    other_phone_number_id: str = "",
    poster: Optional[Callable] = None,
) -> Dict[str, Any]:
    """Corre la bateria de aceptacion y devuelve un reporte estructurado.

    Las credenciales (`url`, `token`, `hmac_secret`, `dispatch_token`) deben
    ser las mismas que ya usa el emisor real -- se leen de _radar_client en
    app.py y se pasan tal cual, nunca se piden ni se generan aqui. Este
    modulo solo las envuelve en un RadarClient nuevo con enabled=True local:
    el _radar_client de app.py nunca se toca.

    `known_lead_id` debe ser un LEAD_ID real, ya presente entre los enlaces
    precargados en Radar, para que el check "valido" pueda tambien reportar
    si Radar lo reconocio (lead_matched). `known_lead_phone_last10` debe ser
    el telefono real asociado a ese mismo LEAD_ID en la hoja: Radar valida el
    formato del telefono (521 + 10 digitos) antes de llegar a lo que cada
    check realmente quiere probar, asi que un telefono ausente o inventado
    hace fallar los seis checks por esa razon, no por la que se esta
    evaluando -- exactamente el defecto que este parametro corrige (hallazgo
    del primer intento real de correr esto, 2026-09-10).

    `other_phone_number_id`, si se da, dispara un sexto check opcional
    (aislamiento por numero). Sin el, se omiten los checks que lo requieren.
    """
    if not (url and token and hmac_secret):
        raise ValueError(
            "faltan credenciales de Radar (url/token/hmac_secret); no se ejecuta nada"
        )
    if not phone_number_id:
        raise ValueError("falta phone_number_id de SECOM")
    if not known_lead_id:
        raise ValueError("falta known_lead_id")
    if not known_lead_phone_last10:
        raise ValueError(
            "falta known_lead_phone_last10; sin telefono valido Radar rechaza "
            "el evento en la validacion de formato antes de llegar a lo que "
            "cada check realmente quiere probar"
        )

    cliente = radar_events.RadarClient(
        url=url, token=token, hmac_secret=hmac_secret,
        dispatch_token=dispatch_token, enabled=True, poster=poster,
    )

    resultados: List[Dict[str, Any]] = []

    def _evento(lead_id: str, check: str, phone_last10: str = TELEFONO_RELLENO_LAST10) -> Dict[str, Any]:
        return radar_events.build_event(
            "message_requested",
            lead_id=lead_id,
            phone_e164=f"521{phone_last10}",
            phone_last10=phone_last10,
            phone_number_id=phone_number_id,
            request_id=str(uuid.uuid4()),
            # Segundo hallazgo real (2026-09-10, misma corrida corregida de
            # telefono): Radar exige delivery.status="requested" explicito
            # para message_requested -- lo dice el contrato (seccion 6) y el
            # sobre no lo mandaba porque build_event() deja delivery_status
            # en None si no se pasa.
            delivery_status="requested",
            trace={"origen": "prueba_aceptacion", "check": check},
        )

    # 1. FIRMA INVALIDA -------------------------------------------------
    # Sobre correcto, credenciales correctas, firma deliberadamente rota.
    # Radar debe rechazar antes de mirar el cuerpo: nunca debe validarse un
    # payload cuya firma no coincide con lo que se recibio.
    r1 = _post(cliente, _evento(known_lead_id, "firma_invalida", known_lead_phone_last10),
               signature_override=FIRMA_INVALIDA, poster=poster)
    resultados.append({
        "check": "firma_invalida",
        "esperado": "401 o 403 (firma rechazada antes de procesar el cuerpo)",
        "obtenido": r1,
        "paso": r1.get("status_code") in (401, 403),
    })

    # 2. VALIDO -----------------------------------------------------------
    # known_lead_id debe ser un LEAD_ID confirmado por Work como ya presente
    # en vicky_lead_links (no cualquier LEAD_ID real de la hoja sirve: el
    # criterio exige lead_matched:true, no solo que Radar acepte el evento).
    evento_valido = _evento(known_lead_id, "valido", known_lead_phone_last10)
    r2 = _post(cliente, evento_valido, poster=poster)
    cuerpo2 = r2.get("body") or {}
    resultados.append({
        "check": "valido",
        "esperado": "200, duplicate:false, lead_matched:true",
        "obtenido": r2,
        "paso": (
            r2.get("status_code") == 200
            and cuerpo2.get("duplicate") is False
            and cuerpo2.get("lead_matched") is True
        ),
    })

    # 3. DUPLICADO ----------------------------------------------------------
    # Reenvio EXACTO del mismo evento del check anterior -- mismo event_id,
    # nueva peticion HTTP (nuevo X-Vicky-Delivery-Id, que es de transporte,
    # no de negocio). Debe declararse duplicado, nunca crear una fila nueva.
    r3 = _post(cliente, evento_valido, poster=poster)
    cuerpo3 = r3.get("body") or {}
    resultados.append({
        "check": "duplicado",
        "esperado": "200, duplicate:true, mismo event_id que 'valido'",
        "obtenido": r3,
        "paso": (
            r3.get("status_code") == 200
            and cuerpo3.get("duplicate") is True
            and cuerpo3.get("event_id") == evento_valido["event_id"]
        ),
    })

    # 4. LEAD DESCONOCIDO -----------------------------------------------------
    r4 = _post(cliente, _evento(LEAD_ID_DESCONOCIDO, "lead_desconocido"), poster=poster)
    cuerpo4 = r4.get("body") or {}
    resultados.append({
        "check": "lead_desconocido",
        "esperado": "200, lead_matched:false -- nunca 404",
        "obtenido": r4,
        "paso": r4.get("status_code") == 200 and cuerpo4.get("lead_matched") is False,
    })

    # 5. AISLAMIENTO POR FUENTE (inconsistencia cabecera/cuerpo) ---------------
    # Correccion de Work (2026-09-09): la version anterior de este check
    # declaraba vicky_redes tanto en la cabecera como en el cuerpo, usando el
    # token real de SECOM -- eso no demuestra aislamiento entre fuentes,
    # porque Redes todavia no tiene credenciales propias configuradas en
    # Radar; el resultado no se podia interpretar con certeza. Lo que SI es
    # verificable hoy, sin depender de Redes, es la regla de consistencia
    # interna del contrato (seccion 3): "la fuente de la cabecera debe
    # coincidir con la del cuerpo". Aqui la cabecera declara la fuente real
    # (vicky_secom, la que prueban el token y el HMAC) y el cuerpo declara
    # vicky_redes -- una inconsistencia dentro de la MISMA peticion, que
    # Radar ya rechaza con 400 segun confirmo Work.
    evento_inconsistente = _evento(
        LEAD_ID_REDES_FORMATO_VALIDO, "aislamiento_fuente", known_lead_phone_last10,
    )
    evento_inconsistente["source"] = OTRA_FUENTE
    r5 = _post(cliente, evento_inconsistente, poster=poster)  # sin source_override: la cabecera sigue siendo vicky_secom
    resultados.append({
        "check": "aislamiento_fuente",
        "esperado": (
            "400 (inconsistencia cabecera/cuerpo, con lead_id ya en formato "
            "RS-<uuid5> para que el 400 no pueda venir de ahi). No demuestra "
            "aislamiento de credenciales entre SECOM y Redes -- eso requiere "
            "que Redes tenga su propio token real configurado en Radar."
        ),
        "obtenido": r5,
        "paso": r5.get("status_code") == 400,
    })

    # 6. AISLAMIENTO POR NUMERO -------------------------------------------------
    # channel.phone_number_id declarado es el de OTRO servicio (Redes), con
    # credenciales y fuente correctas de SECOM. No es un dato secreto -- Meta
    # lo entrega en cada webhook. Work confirmo que Radar ya lo rechaza de
    # forma explicita con 403.
    if other_phone_number_id:
        # El evento de este check reemplaza su propio phone_number_id, no el
        # de SECOM usado en los demas.
        evento6 = _evento(known_lead_id, "aislamiento_numero", known_lead_phone_last10)
        evento6["channel"]["phone_number_id"] = other_phone_number_id
        r6 = _post(cliente, evento6, poster=poster)
        resultados.append({
            "check": "aislamiento_numero",
            "esperado": "403 (phone_number_id no autorizado para este token)",
            "obtenido": r6,
            "paso": r6.get("status_code") == 403,
        })

    aprobados = sum(1 for r in resultados if r["paso"] is True)
    con_aserto = sum(1 for r in resultados if r["paso"] is not None)

    return {
        "contrato": radar_events.CONTRACT_VERSION,
        "url": url,
        "known_lead_id": known_lead_id,
        "lead_id_desconocido_usado": LEAD_ID_DESCONOCIDO,
        "total_checks": len(resultados),
        "checks_con_criterio_duro": con_aserto,
        "aprobados": aprobados,
        "resultados": resultados,
    }
