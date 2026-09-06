# backfill_historico.py — transforma el historico de agosto al sobre 1.1
#
# Los fallos de entrega de la campana de agosto no existen en ningun otro
# lado: la hoja marca las seis filas como ENVIADO_VIDA_TEMPORAL y Meta los
# reporto por webhook, donde el codigo de entonces solo escribia un warning.
# La unica copia esta en el JSON extraido de los logs de Render el 2026-09-05,
# antes de que la retencion de 30 dias los borrara.
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
