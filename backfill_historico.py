# backfill_historico.py — transforma el historico de agosto al sobre 1.1
#
# Los fallos de entrega de la campana de agosto no existen en ningun otro
# lado: la hoja marca las seis filas como ENVIADO_VIDA_TEMPORAL y Meta los
# reporto por webhook, donde el codigo de entonces solo escribia un warning.
# La unica copia esta en el JSON extraido de los logs de Render el 2026-09-05,
# antes de que la retencion de 30 dias los borrara.
#
# Los ~140 envios son distintos: su fuente es la HOJA, no los logs. El
# contrato lo fija asi (message_sent historico se llavea con
# lead_id:occurred_at y puede carecer de wamid), y menos mal, porque los logs
# de este servicio solo conservan desde el 2026-08-30 20:01 UTC: los envios
# del 28 y 29 de agosto -- 104 de los 140 -- ya no estan ahi.
#
# Este modulo NO carga nada. Solo construye los sobres y se niega a construir
# los que no puede sustentar con el dato real. La carga es un paso aparte,
# manual y posterior a la prueba de aceptacion del contrato.
#
# Decision cerrada (Don Chiwy, 2026-09-06): se cargan UNICAMENTE los seis
# fallos dirigidos a prospectos. Los dos 131047 dirigidos al asesor quedan en
# el JSON como evidencia pero no se precargan, para no contaminar la metrica
# de fallos del prospecto; ampliar advisor_notified al backfill se descarto
# expresamente.
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Mapping

import radar_events


class BackfillIncompleto(ValueError):
    """Falta un dato real y no se puede inferir sin inventarlo."""


def cargar_extraccion(ruta: str) -> Dict[str, Any]:
    with open(ruta, encoding="utf-8") as fh:
        return json.load(fh)


def construir_fallos_de_prospecto(
    extraccion: Mapping[str, Any],
    lead_ids_por_telefono: Mapping[str, str],
    *,
    phone_number_id: str,
) -> List[Dict[str, Any]]:
    """Convierte los fallos a prospecto en sobres message_failed de backfill.

    `lead_ids_por_telefono` mapea los ultimos 10 digitos al LEAD_ID ya
    presente en la hoja. No se sella nada aqui: sellar es lo que hace Vicky al
    seleccionar una fila para enviar, y estas filas se enviaron en agosto. Un
    telefono sin LEAD_ID conocido aborta en vez de generar uno nuevo, porque
    inventarlo ahora crearia una identidad que Radar no podria conciliar con
    la fila real.

    Deliberadamente NO se rellenan `template` ni `campaign`. El log de envio
    no imprime el wamid, asi que no hay forma de atar cada fallo con la
    plantilla concreta sin deducirlo del contexto; el contrato exige conservar
    los datos reales del log y no inferirlos.
    """
    fallos = extraccion.get("fallos_a_prospecto") or []
    if not fallos:
        raise BackfillIncompleto("la extraccion no trae fallos_a_prospecto")

    eventos: List[Dict[str, Any]] = []
    for fallo in fallos:
        wamid = str(fallo.get("wamid") or "").strip()
        if not wamid:
            raise BackfillIncompleto("fallo historico sin wamid; es la llave del evento")

        last10 = str(fallo.get("phone_last10") or "").strip()
        lead_id = str(lead_ids_por_telefono.get(last10) or "").strip()
        if not lead_id:
            raise BackfillIncompleto(
                f"sin LEAD_ID conocido para el telefono terminado en {last10[-4:]}; "
                "no se genera uno nuevo"
            )

        codigo = fallo.get("error_code")
        if codigo is None:
            raise BackfillIncompleto(f"fallo {wamid[:24]} sin error_code real")

        # La fecha sale del timestamp de Meta, no de la hoja: es el instante en
        # que ocurrio el hecho. Radar ordena por esta fecha y rechaza cualquier
        # backfill que no termine en .000Z.
        occurred_at = radar_events.canonical_ts(fallo["meta_timestamp_unix"])
        if not occurred_at.endswith(".000Z"):
            raise BackfillIncompleto(f"fecha no canonica para {wamid[:24]}: {occurred_at}")

        eventos.append(radar_events.build_event(
            "message_failed",
            lead_id=lead_id,
            phone_e164=str(fallo.get("recipient_id") or ""),
            phone_last10=last10,
            phone_number_id=phone_number_id,
            occurred_at=occurred_at,
            wamid=wamid,
            direction="outbound",
            delivery_status="failed",
            error_code=int(codigo),
            error_title=str(fallo.get("error_title") or "") or None,
            backfill=True,
            trace={
                "service": "vicky-bot-secom",
                "origen": "backfill_logs_render",
                "extraido_el": (extraccion.get("_meta") or {}).get("extraido_el"),
            },
        ))

    ids = [e["event_id"] for e in eventos]
    if len(set(ids)) != len(ids):
        raise BackfillIncompleto("dos eventos historicos comparten event_id")
    return eventos


# Estatus de la hoja que representan un envio de plantilla ya realizado. Se
# listan explicitamente en vez de aceptar "cualquier ESTATUS que empiece por
# ENVIADO": un estatus nuevo no debe colarse al historico por parecerse.
ESTATUS_DE_ENVIO = {"ENVIADO_VIDA_TEMPORAL"}


def construir_envios_de_hoja(
    filas: List[Mapping[str, str]],
    *,
    phone_number_id: str,
) -> List[Dict[str, Any]]:
    """Convierte las filas enviadas de la hoja en message_sent de backfill.

    La fuente es la hoja, no los logs de Render, y asi lo fija el contrato:
    un message_sent historico se llavea con lead_id:occurred_at y puede
    carecer de wamid y de request_id. Esto importa porque los logs de este
    servicio solo conservan desde el 2026-08-30 20:01 UTC -- los envios del 28
    y 29 de agosto ya no estan ahi, y aun asi son reconstruibles.

    Cada fila debe traer LEAD_ID, ESTATUS y LAST_MESSAGE_AT. La fecha se
    normaliza con canonical_backfill_ts: la hoja la guarda en UTC sin Z, y
    Radar rechaza cualquier backfill que no termine en .000Z.

    Se excluye toda fila cuyo ESTATUS no este en ESTATUS_DE_ENVIO. No es un
    tecnicismo: cuando un prospecto respondio, el bot reescribio su ESTATUS y
    su LAST_MESSAGE_AT en la misma llamada, asi que esa fecha ya no es la del
    envio sino la de la respuesta. Emitir un message_sent con ella lo fecharia
    mal, y la fecha real de esos casos no esta en ningun lado.
    """
    eventos: List[Dict[str, Any]] = []
    vistos: Dict[str, str] = {}

    for fila in filas:
        estatus = str(fila.get("ESTATUS") or "").strip().upper()
        if estatus not in ESTATUS_DE_ENVIO:
            continue

        lead_id = str(fila.get("LEAD_ID") or "").strip()
        if not lead_id:
            raise BackfillIncompleto(
                f"fila con ESTATUS {estatus} sin LEAD_ID; no se genera uno nuevo"
            )

        try:
            occurred_at = radar_events.canonical_backfill_ts(fila.get("LAST_MESSAGE_AT", ""))
        except ValueError as exc:
            raise BackfillIncompleto(f"{lead_id}: {exc}") from exc

        clave = f"{lead_id}:{occurred_at}"
        if clave in vistos:
            raise BackfillIncompleto(f"clave repetida en el historico: {clave}")
        vistos[clave] = lead_id

        last10 = re.sub(r"\D", "", str(fila.get("WhatsApp") or ""))[-10:]
        eventos.append(radar_events.build_event(
            "message_sent",
            lead_id=lead_id,
            phone_e164=f"521{last10}" if len(last10) == 10 else "",
            phone_last10=last10,
            phone_number_id=phone_number_id,
            occurred_at=occurred_at,
            direction="outbound",
            delivery_status="sent",
            backfill=True,
            trace={"service": "vicky-bot-secom", "origen": "backfill_hoja", "estatus": estatus},
        ))

    if not eventos:
        raise BackfillIncompleto("ninguna fila califica como envio historico")
    return eventos


def en_lotes(eventos: List[Dict[str, Any]], tamano: int = 50) -> List[Dict[str, Any]]:
    """Parte los eventos en cuerpos para /events/batch (maximo 50 por peticion)."""
    if tamano < 1 or tamano > 50:
        raise ValueError("el contrato admite entre 1 y 50 eventos por lote")
    return [{"events": eventos[i:i + tamano]} for i in range(0, len(eventos), tamano)]
