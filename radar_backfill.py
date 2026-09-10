# radar_backfill.py — carga puntual de los 146 eventos historicos a Radar
#
# Los 146 eventos (140 message_sent + 6 message_failed de la campana de
# agosto) ya estan construidos y verificados fuera de este servicio --
# backfill_historico.py los arma; el resultado son dos archivos JSON con
# hash verificado por ambos lados (Work y Code, 2026-09-06 y 2026-09-10).
# Este modulo NO los construye ni los conoce de antemano: recibe los lotes
# ya armados como argumento y solo los entrega a Radar por el endpoint de
# lote del contrato 1.1.
#
# Alcance deliberadamente estrecho (pedido explicito de Don Chiwy,
# 2026-09-10): este cargador SOLO admite eventos con source="vicky_secom",
# event_type en {message_sent, message_failed} y backfill:true. Un lote que
# traiga cualquier otra cosa -- otra fuente, otro tipo de evento, o un
# evento sin la marca de historico -- se rechaza en validar_lote() antes de
# gastar una peticion HTTP. Esto no es solo prolijidad: evita que este
# mecanismo, pensado para una carga de un solo uso, se pueda reutilizar por
# error como una via lateral hacia el emisor comercial de Redes o de
# eventos en vivo.
#
# Regla de diseno, identica a radar_acceptance.py: la habilitacion es local
# a esta llamada. Se construye un RadarClient desechable con enabled=True
# SOLO dentro de load_batches(); nunca se importa ni se muta
# app.py:_radar_client, y jamas se toca RADAR_EMIT_ENABLED. Terminada la
# funcion, el emisor general queda exactamente como estaba.
#
# Los datos de los 146 eventos (nombres, telefonos de prospectos reales) NO
# viven en este repositorio ni se comiten a git: viajan en el cuerpo de la
# peticion HTTP al endpoint protegido que invoca esto (ver
# app.py:/ext/radar/backfill-load), igual que viajarian de cualquier forma
# hacia Radar. Mantener PII fuera del historial de git es deliberado, dado
# el hallazgo previo de exposicion de secretos en un fork publico de este
# mismo repositorio (auditoria 2026-08-14/18). Por la misma razon, nada de
# este modulo escribe el cuerpo de los eventos a un log: los mensajes de
# log y las excepciones solo llevan conteos, indices de lote y conjuntos
# como {"vicky_secom"} o {"1045543821971905"} (phone_number_id del
# canal, no telefono del prospecto) -- nunca nombre ni telefono de un
# prospecto.
from __future__ import annotations

import json
import time
from typing import Any, Callable, Dict, List, Optional

import radar_events

# Maximo de eventos por lote, segun el contrato 1.1 seccion 2
# ("POST /api/v1/vicky/events/batch: Histórico, de 1 a 50 eventos").
MAX_EVENTOS_POR_LOTE = 50

# Tamano maximo de un lote en bytes, segun el mismo contrato ("maximo 1 MiB").
MAX_BYTES_POR_LOTE = 1 * 1024 * 1024

# Lista blanca deliberada de este cargador -- ver nota de alcance arriba.
# No se toma de argumento ni de configuracion: es fija a proposito, para que
# ampliar lo que este endpoint acepta requiera tocar el codigo, no solo
# cambiar el cuerpo de una peticion.
FUENTE_PERMITIDA = radar_events.SOURCE  # "vicky_secom"
TIPOS_PERMITIDOS = frozenset({"message_sent", "message_failed"})

# Nombres posibles de la lista de resultados por evento dentro de la
# respuesta del endpoint de lote. El contrato 1.1 (seccion 7) documenta el
# desglose por evento UNICO (duplicate/lead_matched) pero no el formato
# exacto de la respuesta del endpoint de LOTE -- eso nunca se ha probado
# contra el Radar real (la prueba de aceptacion de 2026-09-09 solo ejercito
# /api/v1/vicky/events, no /events/batch). _resumen_lote() por eso busca de
# forma flexible en varias claves razonables en vez de asumir una sola.
CLAVES_LISTA_POR_EVENTO = ("results", "events", "items", "detalles", "resultados")


def derivar_url_de_lote(url_evento_unico: str, override: str = "") -> str:
    """Deriva la URL del endpoint de lote a partir de la de evento unico.

    El contrato 1.1 (seccion 2) define ambas rutas bajo el mismo prefijo:
    POST /api/v1/vicky/events (uno) y POST /api/v1/vicky/events/batch
    (lote). `_radar_client.url` -- la que ya usa el emisor real y el arnes
    de aceptacion -- apunta a la primera; aqui se deriva la segunda sin
    pedir una variable de entorno nueva, salvo que `override` la fije
    explicitamente (por si Radar decide separarlas en el futuro).
    """
    if override:
        return override.strip()
    base = (url_evento_unico or "").rstrip("/")
    if base.endswith("/events"):
        return base + "/batch"
    raise ValueError(
        f"no se pudo derivar la URL de lote de {url_evento_unico!r}; "
        "pasa RADAR_EVENTS_BATCH_URL explicitamente"
    )


def _eventos_de_lote(lote: Any) -> List[Dict[str, Any]]:
    """Normaliza un lote a la lista simple de eventos que usa el resto del
    modulo.

    `backfill_historico.en_lotes()` -- lo que realmente produjo los 3 lotes
    de envios -- devuelve cada lote como `{"events": [...]}`, ya en la forma
    exacta del cuerpo que espera /events/batch. El lote de fallos se guardo
    con esa misma envoltura. Aceptar tambien una lista simple evita tener que
    desenvolver a mano en cada llamada de prueba.
    """
    if isinstance(lote, dict):
        eventos = lote.get("events")
        if not isinstance(eventos, list):
            raise ValueError("lote en forma de dict debe traer 'events': lista")
        return eventos
    if isinstance(lote, list):
        return lote
    raise ValueError(f"lote con forma no reconocida: {type(lote).__name__}")


def validar_lote(eventos: List[Dict[str, Any]]) -> None:
    """Verificaciones de forma y de alcance antes de gastar una peticion HTTP.

    No decide si los datos son correctos -- eso ya lo hizo
    backfill_historico.py al construirlos y las pruebas de ese modulo lo
    verifican. Esto evita dos cosas distintas:

    1. Lo que el contrato rechazaria de entrada: lote vacio, demasiado
       grande, o mezcla de fuentes/numero (el contrato exige "la misma
       fuente y el mismo phone_number_id" dentro de un lote), o demasiados
       bytes.
    2. Lo que este cargador especificamente NO debe admitir aunque el
       contrato lo permitiera: cualquier fuente distinta de vicky_secom,
       cualquier event_type fuera de {message_sent, message_failed}, o
       cualquier evento sin backfill:true. Ver nota de alcance al inicio
       del archivo.
    """
    if not eventos:
        raise ValueError("lote vacio")
    if len(eventos) > MAX_EVENTOS_POR_LOTE:
        raise ValueError(
            f"lote de {len(eventos)} eventos excede el maximo de {MAX_EVENTOS_POR_LOTE}"
        )

    fuentes = {e.get("source") for e in eventos}
    if fuentes != {FUENTE_PERMITIDA}:
        raise ValueError(
            f"este cargador solo admite source={FUENTE_PERMITIDA!r}; "
            f"el lote trae {fuentes}"
        )

    tipos_no_permitidos = {e.get("event_type") for e in eventos} - TIPOS_PERMITIDOS
    if tipos_no_permitidos:
        raise ValueError(
            f"este cargador solo admite event_type en {sorted(TIPOS_PERMITIDOS)}; "
            f"el lote trae {sorted(tipos_no_permitidos)}"
        )

    sin_backfill = sum(1 for e in eventos if e.get("backfill") is not True)
    if sin_backfill:
        raise ValueError(
            "este cargador solo admite eventos historicos (backfill:true); "
            f"{sin_backfill} evento(s) del lote no lo declaran"
        )

    numeros = {(e.get("channel") or {}).get("phone_number_id") for e in eventos}
    if len(numeros) > 1:
        raise ValueError(f"el lote mezcla phone_number_id distintos: {numeros}")

    cuerpo = json.dumps({"events": eventos}, ensure_ascii=False).encode("utf-8")
    if len(cuerpo) > MAX_BYTES_POR_LOTE:
        raise ValueError(f"lote de {len(cuerpo)} bytes excede el maximo de {MAX_BYTES_POR_LOTE}")


def _resumen_lote(status_code: Optional[int], cuerpo_resp: Any, n_eventos: int) -> Dict[str, Any]:
    """Cuenta aceptados/duplicados/conciliados/no_conciliados/errores de un lote.

    El contrato solo documenta el desglose por evento del endpoint UNICO
    (`duplicate`, `lead_matched`); el de LOTE nunca se ha probado contra el
    Radar real. Por eso esta funcion busca de forma flexible una lista de
    resultados por evento bajo varios nombres posibles (CLAVES_LISTA_POR_EVENTO)
    y, si no la encuentra, NO inventa cifras: marca el lote como aceptado a
    nivel de lote (si status_code fue 200) sin desglosar duplicados ni
    conciliacion, y deja claro que el detalle por evento no estaba
    disponible en la respuesta.
    """
    resumen = {
        "aceptados": 0,
        "duplicados": 0,
        "conciliados": 0,
        "no_conciliados": 0,
        "errores": 0,
        "detalle_por_evento": False,
    }
    if status_code != 200:
        resumen["errores"] = n_eventos
        return resumen

    cuerpo_resp = cuerpo_resp if isinstance(cuerpo_resp, dict) else {}
    lista = None
    for clave in CLAVES_LISTA_POR_EVENTO:
        valor = cuerpo_resp.get(clave)
        if isinstance(valor, list) and valor:
            lista = valor
            break

    if lista is None:
        # Radar acepto el lote (200) pero su respuesta no trae desglose por
        # evento en un formato reconocido. Se cuenta como aceptado a nivel
        # de lote, sin desglosar duplicados/conciliacion.
        resumen["aceptados"] = n_eventos
        return resumen

    resumen["detalle_por_evento"] = True
    for item in lista:
        if not isinstance(item, dict):
            continue
        if item.get("duplicate") is True:
            resumen["duplicados"] += 1
        else:
            resumen["aceptados"] += 1
        if item.get("lead_matched") is True:
            resumen["conciliados"] += 1
        elif item.get("lead_matched") is False:
            resumen["no_conciliados"] += 1
    return resumen


def _post_lote(
    client: "radar_events.RadarClient",
    eventos: List[Dict[str, Any]],
    url_lote: str,
    poster: Optional[Callable],
) -> Dict[str, Any]:
    cuerpo = json.dumps({"events": eventos}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    import uuid
    from datetime import datetime, timezone

    delivery_id = str(uuid.uuid4())
    timestamp = str(int(datetime.now(timezone.utc).timestamp()))
    cabeceras = client.headers_for(cuerpo, delivery_id=delivery_id, timestamp=timestamp)

    enviar = poster or client._post
    if enviar is None:
        import requests
        enviar = requests.post

    t0 = time.monotonic()
    try:
        resp = enviar(url_lote, data=cuerpo, headers=cabeceras, timeout=client.timeout * 3)
    except Exception as exc:
        return {
            "status_code": None,
            "error": f"{type(exc).__name__}: {exc}",
            "elapsed_ms": round((time.monotonic() - t0) * 1000),
            "enviados": len(eventos),
            "resumen": _resumen_lote(None, None, len(eventos)),
        }

    try:
        cuerpo_resp = resp.json()
    except Exception:
        cuerpo_resp = {"_raw": str(getattr(resp, "text", ""))[:1000]}

    status_code = getattr(resp, "status_code", None)
    return {
        "status_code": status_code,
        "body": cuerpo_resp,
        "elapsed_ms": round((time.monotonic() - t0) * 1000),
        "enviados": len(eventos),
        "resumen": _resumen_lote(status_code, cuerpo_resp, len(eventos)),
    }


def load_batches(
    *,
    url: str,
    token: str,
    hmac_secret: str,
    dispatch_token: str,
    batches: List[List[Dict[str, Any]]],
    batch_url_override: str = "",
    poster: Optional[Callable] = None,
) -> Dict[str, Any]:
    """Entrega los lotes ya construidos a Radar, uno por uno.

    `batches` es una lista de lotes; cada lote es la lista de eventos que ya
    salio de backfill_historico.py (construir_fallos_de_prospecto() /
    construir_envios_de_hoja() + en_lotes()). Este modulo no los construye
    -- solo los valida (validar_lote: forma + alcance) y los entrega.

    Las credenciales deben ser las MISMAS que ya usa el emisor real -- se
    leen de _radar_client en app.py y se pasan tal cual. Se envuelven en un
    RadarClient desechable con enabled=True SOLO en esta llamada: el
    _radar_client de app.py nunca se toca, y RADAR_EMIT_ENABLED jamas se lee
    ni se escribe aqui.

    Se detiene en el primer lote que falle (status_code distinto de 200):
    reintentar lotes posteriores despues de un fallo a mitad de carga
    complicaria mas de lo que resuelve para una carga de un solo uso. El
    reporte deja claro cuantos lotes se completaron y cual fallo, para
    reintentar manualmente solo ese.

    Reintentar es seguro por diseno, no por logica añadida aqui: los lotes
    ya traen event_id deterministas (calculados por backfill_historico.py a
    partir de wamid o lead_id:occurred_at). El contrato 1.1 documenta que
    `vicky_events` es append-only con event_id como llave primaria -- un
    evento repetido responde 200 con duplicate:true y no crea otra fila.
    Repetir un lote completo, o solo el que fallo, nunca produce filas
    nuevas para lo que Radar ya acepto.

    Todos los lotes se validan (forma + alcance) ANTES de enviar el
    primero: si el ultimo lote esta mal formado, no queremos haber
    enviado ya los anteriores y quedar a medias por un error evitable.
    """
    if not (url and token and hmac_secret):
        raise ValueError(
            "faltan credenciales de Radar (url/token/hmac_secret); no se ejecuta nada"
        )
    if not batches:
        raise ValueError("no hay lotes que cargar")

    lotes = [_eventos_de_lote(lote) for lote in batches]
    for lote in lotes:
        validar_lote(lote)

    url_lote = derivar_url_de_lote(url, batch_url_override)
    cliente = radar_events.RadarClient(
        url=url, token=token, hmac_secret=hmac_secret,
        dispatch_token=dispatch_token, enabled=True, poster=poster,
    )

    resultados: List[Dict[str, Any]] = []
    total_enviados = 0
    totales: Dict[str, int] = {
        "aceptados": 0, "duplicados": 0, "conciliados": 0,
        "no_conciliados": 0, "errores": 0,
    }
    for i, lote in enumerate(lotes):
        r = _post_lote(cliente, lote, url_lote, poster)
        r["indice_lote"] = i
        resultados.append(r)
        resumen = r.get("resumen") or {}
        for clave in totales:
            totales[clave] += resumen.get(clave, 0)
        if r.get("status_code") == 200:
            total_enviados += len(lote)
        else:
            # Se detiene aqui a proposito -- ver docstring.
            break

    completo = len(resultados) == len(lotes) and all(
        r.get("status_code") == 200 for r in resultados
    )

    return {
        "url_lote": url_lote,
        "total_lotes": len(lotes),
        "lotes_intentados": len(resultados),
        "lotes_completados": sum(1 for r in resultados if r.get("status_code") == 200),
        "eventos_totales_a_cargar": sum(len(b) for b in lotes),
        "eventos_enviados": total_enviados,
        "completo": completo,
        "totales_por_categoria": totales,
        "resultados": resultados,
    }
