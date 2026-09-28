# radar_campanas.py -- campanas lanzadas desde Radar Comercial.
#
# Radar decide A QUIEN; Vicky sigue enviando por su cola de siempre (la hoja
# de leads + el cron de /ext/auto-send-one), con los frenos que ya tiene:
# kill switch, auto-pausa por fallos, reserva de fila y eventos a Radar.
#
# Dos piezas nuevas en la hoja, ninguna reemplaza a otra:
#   - Pestana CAMPANAS_RADAR: la "receta" de cada campana (plantilla, idioma,
#     imagen, parametros y estatus al enviar). Es lo que antes vivia escrito
#     en el comando del cron.
#   - Columna CAMPANA_RADAR en la hoja de leads: marca que fila pertenece a
#     que campana de Radar. El cron manda a cada fila la plantilla de SU
#     campana; las filas sin marca siguen el camino de siempre.
#
# Este modulo es puro (sin Google ni Flask) para poder probarlo solo.
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

CAMPANA_COLUMN = "CAMPANA_RADAR"
CAMPANAS_TAB = "CAMPANAS_RADAR"
CAMPANAS_HEADER = [
    "ID", "NOMBRE", "TEMPLATE", "LANGUAGE", "IMAGE_URL",
    "PARAMS_FROM_ROW", "SUCCESS_STATUS", "ACTIVA", "ACTUALIZADA",
]

# No se vuelve a encolar a quien recibio un mensaje hace menos de esto: Meta
# limita los mensajes de marketing por persona (error 131049) y castiga el
# reenvio en menos de 24 h.
REENCOLAR_MIN_HORAS = 48

MAX_ITEMS = 100
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
LEAD_RE = re.compile(r"^SC-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
TEMPLATE_RE = re.compile(r"^[a-z0-9_]{1,512}$")
LANGUAGE_RE = re.compile(r"^[a-zA-Z]{2,3}(_[a-zA-Z]{2,4})?$")
SUCCESS_STATUS_RE = re.compile(r"^[A-Z0-9_]{1,64}$")


class CampanaInvalida(ValueError):
    pass


def validar_campana(body: Dict[str, Any]) -> Dict[str, Any]:
    """Valida y normaliza la receta que manda Radar."""
    cid = str(body.get("id") or "").strip()
    if not ID_RE.match(cid):
        raise CampanaInvalida("id invalido: minusculas, numeros, guion o guion bajo (max 40)")
    template = str(body.get("template") or "").strip()
    if not TEMPLATE_RE.match(template):
        raise CampanaInvalida("template invalido (nombre de plantilla de Meta)")
    language = str(body.get("language") or "es_MX").strip()
    if not LANGUAGE_RE.match(language):
        raise CampanaInvalida("language invalido")
    success = str(body.get("success_status") or "").strip()
    if not SUCCESS_STATUS_RE.match(success):
        raise CampanaInvalida("success_status invalido: solo A-Z, 0-9 y guion bajo")
    image_url = str(body.get("image_url") or "").strip()
    if image_url and not image_url.startswith("https://"):
        raise CampanaInvalida("image_url debe ser https")
    params = body.get("params_from_row")
    if params is not None and not (isinstance(params, (dict, list)) and params):
        raise CampanaInvalida("params_from_row debe ser objeto o lista no vacios")
    return {
        "id": cid,
        "nombre": str(body.get("nombre") or cid).strip()[:120],
        "template": template,
        "language": language,
        "image_url": image_url,
        "params_from_row": params,
        "success_status": success,
        "activa": bool(body.get("activa", True)),
    }


def fila_de_campana(c: Dict[str, Any], ahora_iso: str) -> List[str]:
    return [
        c["id"], c["nombre"], c["template"], c["language"], c["image_url"],
        json.dumps(c["params_from_row"], ensure_ascii=False) if c["params_from_row"] is not None else "",
        c["success_status"], "SI" if c["activa"] else "NO", ahora_iso,
    ]


def leer_campanas(filas: List[List[str]]) -> Dict[str, Dict[str, Any]]:
    """Lee la pestana CAMPANAS_RADAR (sin encabezado) a un dict por ID.

    Una fila mal escrita a mano no tumba a las demas: se ignora.
    """
    salida: Dict[str, Dict[str, Any]] = {}
    for f in filas:
        f = list(f) + [""] * (len(CAMPANAS_HEADER) - len(f))
        cid = str(f[0]).strip()
        if not cid:
            continue
        try:
            params = json.loads(f[5]) if str(f[5]).strip() else None
        except ValueError:
            continue
        salida[cid] = {
            "id": cid, "nombre": f[1], "template": str(f[2]).strip(), "language": str(f[3]).strip() or "es_MX",
            "image_url": str(f[4]).strip(), "params_from_row": params,
            "success_status": str(f[6]).strip(), "activa": str(f[7]).strip().upper() == "SI",
        }
    return salida


def _ultimos10(telefono: str) -> str:
    d = re.sub(r"\D", "", str(telefono or ""))
    return d[-10:] if len(d) >= 10 else ""


def _parse(ts: str) -> Optional[datetime]:
    """Misma lectura que _parse_dt_maybe de app.py. Vicky escribe
    LAST_MESSAGE_AT con datetime.utcnow().isoformat() (UTC sin zona)."""
    s = str(ts or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s[:-1] + "+00:00" if s.endswith("Z") else s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def planear_encolado(
    headers: List[str],
    rows: List[List[str]],
    campana_id: str,
    items: List[Dict[str, Any]],
    ahora: Optional[datetime] = None,
) -> Tuple[List[Dict[str, Any]], Dict[int, Dict[str, str]], List[Dict[str, str]]]:
    """Decide que hacer con cada persona que manda Radar.

    Devuelve (resultados, actualizaciones_por_fila, filas_nuevas):
      - Si el telefono ya esta en la hoja se REUSA esa fila (Vicky reconoce a
        quien contesta por la PRIMERA fila con ese telefono, asi que una fila
        duplicada haria que la platica use datos viejos).
      - Se respeta el LEAD_ID que ya tenga la fila; si no tiene, se usa el que
        propone Radar. Radar liga a la persona con el que se devuelve.
      - No se toca a quien esta enviandose ahora, ya esta en cola de Radar, o
        recibio mensaje hace menos de REENCOLAR_MIN_HORAS.
    """
    ahora = ahora or datetime.now(timezone.utc)
    idx = {h: i for i, h in enumerate(headers)}
    i_wa = idx.get("WhatsApp")
    if i_wa is None or "ESTATUS" not in idx or "LAST_MESSAGE_AT" not in idx:
        raise CampanaInvalida("la hoja no tiene las columnas WhatsApp, ESTATUS y LAST_MESSAGE_AT")

    def celda(row: List[str], nombre: str) -> str:
        j = idx.get(nombre)
        return str(row[j]).strip() if j is not None and j < len(row) else ""

    primera_fila: Dict[str, int] = {}
    for n, row in enumerate(rows, start=2):
        t = _ultimos10(row[i_wa] if i_wa < len(row) else "")
        if t and t not in primera_fila:
            primera_fila[t] = n

    resultados: List[Dict[str, Any]] = []
    actualizar: Dict[int, Dict[str, str]] = {}
    nuevas: List[Dict[str, str]] = []
    vistos: set = set()
    limite = ahora - timedelta(hours=REENCOLAR_MIN_HORAS)

    for it in items:
        tel = _ultimos10(it.get("telefono", ""))
        propuesto = str(it.get("lead_id") or "").strip()
        base = {"telefono": tel, "lead_id": None, "accion": "omitido", "motivo": ""}
        if len(tel) != 10:
            resultados.append({**base, "motivo": "telefono_invalido"})
            continue
        if not LEAD_RE.match(propuesto):
            resultados.append({**base, "motivo": "lead_id_invalido"})
            continue
        if tel in vistos:
            resultados.append({**base, "motivo": "repetido_en_lote"})
            continue
        vistos.add(tel)

        n = primera_fila.get(tel)
        if n is None:
            nuevas.append({
                "Nombre": str(it.get("nombre") or "").strip()[:120],
                "WhatsApp": tel,
                "ESTATUS": "PENDIENTE",
                "LEAD_ID": propuesto,
                CAMPANA_COLUMN: campana_id,
            })
            resultados.append({**base, "lead_id": propuesto, "accion": "nuevo"})
            continue

        row = rows[n - 2]
        estatus = celda(row, "ESTATUS").upper()
        campana_actual = celda(row, CAMPANA_COLUMN)
        lead_existente = celda(row, "LEAD_ID")
        if estatus == "ENVIANDO":
            resultados.append({**base, "lead_id": lead_existente or None, "motivo": "enviandose"})
            continue
        if estatus == "PENDIENTE" and campana_actual:
            resultados.append({**base, "lead_id": lead_existente or None, "motivo": "ya_en_cola"})
            continue
        ultimo = _parse(celda(row, "LAST_MESSAGE_AT"))
        if ultimo and ultimo > limite:
            resultados.append({**base, "lead_id": lead_existente or None, "motivo": "contactado_recientemente"})
            continue

        cambios = {CAMPANA_COLUMN: campana_id, "ESTATUS": "PENDIENTE", "LAST_MESSAGE_AT": ""}
        if "retry_at" in idx:
            cambios["retry_at"] = ""
        lead = lead_existente
        if not lead and "LEAD_ID" in idx:
            cambios["LEAD_ID"] = propuesto
            lead = propuesto
        actualizar[n] = cambios
        resultados.append({**base, "lead_id": lead or propuesto, "accion": "reencolado"})

    return resultados, actualizar, nuevas


def fila_elegible(
    headers: List[str], row: List[str], ahora: datetime,
    parse_dt: Callable[[str], Optional[datetime]],
) -> bool:
    """Mismas reglas que el cron de siempre: con WhatsApp, sin envio previo,
    estatus vacio o PENDIENTE, y sin reintento programado a futuro."""
    idx = {h: i for i, h in enumerate(headers)}

    def celda(nombre: str) -> str:
        j = idx.get(nombre)
        return str(row[j]).strip() if j is not None and j < len(row) else ""

    if not celda("WhatsApp") or celda("LAST_MESSAGE_AT"):
        return False
    if celda("ESTATUS").upper() not in ("", "PENDIENTE"):
        return False
    reintento = parse_dt(celda("retry_at")) if "retry_at" in idx else None
    if reintento is not None and reintento > ahora.replace(tzinfo=reintento.tzinfo):
        return False
    return True
