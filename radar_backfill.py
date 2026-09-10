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
# mismo repositorio (auditoria 2026-08-14/18).
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


def validar_lote(eventos: List[Dict[str, Any]]) -> None:
    """Verificaciones de forma antes de gastar una peticion HTTP.

    No decide si los datos son correctos -- eso ya lo hizo
    backfill_historico.py al construirlos y las pruebas de ese modulo lo
    verifican. Esto solo evita mandar algo que el contrato rechazaria de
    entrada: lote vacio, demasiado grande, o mezcla de fuentes/numero (el
    contrato exige "la misma fuente y el mismo phone_number_id" dentro de
    un lote).
    """
    if not eventos:
        raise ValueError("lote vacio")
    if len(eventos) > MAX_EVENTOS_POR_LOTE:
        raise ValueError(
            f"lote de {len(eventos)} eventos excede el maximo de {MAX_EVENTOS_POR_LOTE}"
        )
    fuentes = {e.get("source") for e in eventos}
    if len(fuentes) > 1:
        raise ValueError(f"el lote mezcla fuentes distintas: {fuentes}")
    numeros = {(e.get("channel") or {}).get("phone_number_id") for e in eventos}
    if len(numeros) > 1:
        raise ValueError(f"el lote mezcla phone_number_id distintos: {numeros}")

    cuerpo = json.dumps({"events": eventos}, ensure_ascii=False).encode("utf-8")
    if len(cuerpo) > MAX_BYTES_POR_LOTE:
        raise ValueError(
            f"lote de {len(cuerpo)} bytes excede el maximo de {MAX_BYTES_POR_LOTE}"
        )


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
        }

    try:
        cuerpo_resp = resp.json()
    except Exception:
        cuerpo_resp = {"_raw": str(getattr(resp, "text", ""))[:1000]}

    return {
        "status_code": getattr(resp, "status_code", None),
        "body": cuerpo_resp,
        "elapsed_ms": round((time.monotonic() - t0) * 1000),
        "enviados": len(eventos),
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
    salio de backfill_historico.py (construir_fallos_de_prospecto() o
    construir_envios_de_hoja() + en_lotes()). Este modulo no los valida
    semanticamente -- solo de forma (validar_lote) -- ni los construye.

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
    """
    if not (url and token and hmac_secret):
        raise ValueError(
            "faltan credenciales de Radar (url/token/hmac_secret); no se ejecuta nada"
        )
    if not batches:
        raise ValueError("no hay lotes que cargar")

    for lote in batches:
        validar_lote(lote)

    url_lote = derivar_url_de_lote(url, batch_url_override)
    cliente = radar_events.RadarClient(
        url=url, token=token, hmac_secret=hmac_secret,
        dispatch_token=dispatch_token, enabled=True, poster=poster,
    )

    resultados: List[Dict[str, Any]] = []
    total_enviados = 0
    for i, lote in enumerate(batches):
        r = _post_lote(cliente, lote, url_lote, poster)
        r["indice_lote"] = i
        resultados.append(r)
        if r.get("status_code") == 200:
            total_enviados += len(lote)
        else:
            # Se detiene aqui a proposito -- ver docstring.
            break

    completo = len(resultados) == len(batches) and all(
        r.get("status_code") == 200 for r in resultados
    )

    return {
        "url_lote": url_lote,
        "total_lotes": len(batches),
        "lotes_intentados": len(resultados),
        "lotes_completados": sum(1 for r in resultados if r.get("status_code") == 200),
        "eventos_totales_a_cargar": sum(len(b) for b in batches),
        "eventos_enviados": total_enviados,
        "completo": completo,
        "resultados": resultados,
    }
