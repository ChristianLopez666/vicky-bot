# radar_events.py — enchufe Vicky SECOM → Radar, contrato 1.1 (commit f78ea40)
#
# Este modulo existe porque hoy SECOM tira los hechos que mas importan: Meta
# informa por webhook cada sent/delivered/read/failed y el codigo solo escribe
# un warning; el wamid se extrae al enviar y se pierde. La auditoria forense
# del 2026-09-04 lo dejo verificado -- 140 plantillas salieron entre el 28 y el
# 31 de agosto y la pestana ENVIO_STATUS no registro ninguna.
#
# Alcance deliberado: este archivo NO envia nada a Radar todavia. Solo define
# el sobre del contrato, la regla de identidad de los eventos y la bitacora
# durable donde se anotan antes de intentar entregarlos. El emisor se conecta
# despues y arranca apagado (RADAR_EMIT_ENABLED), de modo que anotar eventos
# no puede cambiar el comportamiento comercial del bot.
#
# Referencia normativa: "Enchufe Vicky SECOM + Vicky Redes -> Radar, contrato
# 1.1", commit f78ea40 del repositorio de Work.
from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger("vicky-secom.radar")

CONTRACT_VERSION = "1.1"
SOURCE = "vicky_secom"

# Namespace fijo del contrato. NO cambiar: de el depende que el mismo hecho
# produzca siempre el mismo event_id, que es la llave de deduplicacion del
# lado de Radar.
EVENT_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://cohifis.com.mx/vicky/events")

# Pestana de la hoja de leads donde vive la bitacora. Se crea sola con
# _ensure_tab(), el mismo mecanismo ya probado en produccion para CONTROL y
# CONVERSACIONES. No se reutiliza ENVIO_STATUS: esa pestana quedo con 498
# asientos de marzo y un formato de 5 columnas que no cabe el contrato.
EVENTS_TAB = "EVENTOS_RADAR"
EVENTS_HEADER = [
    "event_id", "event_type", "occurred_at", "source", "phone_number_id",
    "lead_id", "phone_last10", "request_id", "wamid", "template", "campaign",
    "delivery_status", "error_code", "backfill",
    "radar_state", "radar_attempts", "radar_last_try", "payload_json",
]

# Estados de entrega hacia Radar (columna radar_state).
PENDIENTE = "PENDIENTE"
ENVIADO = "ENVIADO"
RECHAZADO = "RECHAZADO"

EVENT_TYPES = {
    "message_requested",
    "message_sent",
    "message_delivered",
    "message_read",
    "message_failed",
    "message_inbound",
    "advisor_notified",
    "lead_status_changed",
}

# Solo estos dos admiten backfill:true (contrato 1.1, "Excepcion cerrada para
# el historico de agosto").
BACKFILL_TYPES = {"message_sent", "message_failed"}

DELIVERY_STATUSES = {"requested", "sent", "delivered", "read", "failed", None}


# ==========================
# Tiempo canonico
# ==========================
def canonical_ts(value: Any = None) -> str:
    """Devuelve una marca UTC con Z y milisegundos, como exige el contrato.

    Acepta datetime, epoch Unix (int o str de digitos), o None para "ahora".
    Un datetime sin zona se interpreta como UTC: todo lo que este repo escribe
    nace de datetime.utcnow(), nunca de hora local.
    """
    if value is None:
        dt = datetime.now(timezone.utc)
    elif isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    elif isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
        dt = datetime.fromtimestamp(int(value), tz=timezone.utc)
    else:
        raise ValueError(f"canonical_ts no sabe interpretar {value!r}")
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def canonical_backfill_ts(sheet_value: str) -> str:
    """Normaliza una fecha de la hoja a la forma canonica del historico.

    El contrato es explicito: Radar rechaza un backfill cuyo occurred_at no
    termine en .000Z, porque de esa cadena depende la llave lead_id:occurred_at
    y dos cargas que la formateen distinto duplicarian el evento.

    La hoja guarda "2026-08-28 23:10:28" -- sin Z y sin milisegundos, pero en
    UTC: lo escribe _utc_now_iso(), no la hora local de Mazatlan. Se interpreta
    como UTC y se trunca a segundo.
    """
    raw = str(sheet_value or "").strip()
    if not raw:
        raise ValueError("fecha de hoja vacia")
    normalized = raw.replace("T", " ").replace("Z", "").strip()
    # Descarta fraccion de segundo si la hubiera: el truncado es a segundo.
    normalized = normalized.split(".")[0]
    try:
        dt = datetime.strptime(normalized, "%Y-%m-%d %H:%M:%S")
    except ValueError as exc:
        raise ValueError(f"fecha de hoja no interpretable: {sheet_value!r}") from exc
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


# ==========================
# Identidad de los eventos
# ==========================
def event_id_for(event_type: str, clave: str, source: str = SOURCE) -> str:
    """UUID v5 determinista: el mismo hecho produce siempre el mismo id.

    Determinista y no aleatorio a proposito. El estado en memoria de este
    servicio no sobrevive a un reinicio de Render, asi que un uuid4 generado
    al vuelo haria que un reintento despues de reiniciar se viera en Radar
    como un hecho nuevo. Con esta formula, reintentar es gratis.
    """
    clave = str(clave or "").strip()
    if not clave:
        raise ValueError(f"clave vacia para event_id de {event_type}")
    return str(uuid.uuid5(EVENT_NAMESPACE, f"{source}|{event_type}|{clave}"))


def clave_for(event_type: str, *, wamid: str = "", request_id: str = "",
              lead_id: str = "", occurred_at: str = "", estatus: str = "",
              backfill: bool = False) -> str:
    """Tabla de llaves del contrato 1.1, seccion 6."""
    wamid = (wamid or "").strip()
    request_id = (request_id or "").strip()

    if event_type == "message_requested":
        return request_id
    if event_type == "message_sent":
        # Historico: puede no haber wamid ni request_id (la hoja solo guardo
        # fecha y plantilla), asi que la llave es el par lead+fecha.
        return f"{lead_id}:{occurred_at}" if backfill else wamid
    if event_type in ("message_delivered", "message_read", "message_inbound"):
        return wamid
    if event_type in ("message_failed", "advisor_notified"):
        # El historico de fallos SI exige wamid real del log; en vivo, si el
        # envio murio antes de que Meta respondiera, no hay wamid y la llave
        # cae al request_id.
        return wamid or request_id
    if event_type == "lead_status_changed":
        return f"{lead_id}:{estatus}:{occurred_at}"
    raise ValueError(f"event_type desconocido: {event_type}")


# ==========================
# Sobre
# ==========================
def build_event(
    event_type: str,
    *,
    lead_id: str,
    phone_e164: str = "",
    phone_last10: str = "",
    name: Optional[str] = None,
    phone_number_id: str = "",
    occurred_at: Optional[str] = None,
    request_id: Optional[str] = None,
    wamid: Optional[str] = None,
    direction: str = "outbound",
    template: Optional[str] = None,
    campaign: Optional[str] = None,
    text: Optional[str] = None,
    delivery_status: Optional[str] = None,
    error_code: Optional[int] = None,
    error_title: Optional[str] = None,
    advisor_notification: Optional[Dict[str, Any]] = None,
    status_change: Optional[Dict[str, Any]] = None,
    backfill: bool = False,
    trace: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Arma el sobre 1.1 y calcula su event_id. No hace entrada/salida."""
    if event_type not in EVENT_TYPES:
        raise ValueError(f"event_type desconocido: {event_type}")
    if backfill and event_type not in BACKFILL_TYPES:
        raise ValueError(f"backfill no permitido para {event_type}")
    if delivery_status not in DELIVERY_STATUSES:
        raise ValueError(f"delivery.status invalido: {delivery_status!r}")
    lead_id = str(lead_id or "").strip()
    if not lead_id:
        raise ValueError(f"{event_type} sin lead_id; la identidad es obligatoria")

    occurred_at = occurred_at or canonical_ts()
    clave = clave_for(
        event_type, wamid=wamid or "", request_id=request_id or "",
        lead_id=lead_id, occurred_at=occurred_at, estatus=(
            (status_change or {}).get("current") or ""
        ), backfill=backfill,
    )

    event: Dict[str, Any] = {
        "contract_version": CONTRACT_VERSION,
        "event_id": event_id_for(event_type, clave),
        "event_type": event_type,
        "occurred_at": occurred_at,
        "source": SOURCE,
        "backfill": bool(backfill),
        "channel": {"type": "whatsapp", "phone_number_id": str(phone_number_id or "")},
        "lead": {
            "lead_id": lead_id,
            "phone_e164": str(phone_e164 or ""),
            "phone_last10": str(phone_last10 or ""),
            "name": name or None,
        },
        "message": {
            "request_id": request_id or None,
            "wamid": wamid or None,
            "direction": direction,
            "template": template or None,
            "campaign": campaign or None,
            "text": (str(text)[:500] if text else None),
        },
        "delivery": {
            "status": delivery_status,
            "error_code": error_code,
            "error_title": error_title or None,
        },
        "trace": trace or {},
    }
    if advisor_notification is not None:
        event["advisor_notification"] = advisor_notification
    if status_change is not None:
        event["status_change"] = status_change
    return event


# ==========================
# Bitacora durable
# ==========================
class EventLog:
    """Anota cada evento en Sheets antes de intentar entregarlo a Radar.

    Es la pieza que convierte "se envio" en un hecho recuperable. Sin ella el
    emisor no tendria de donde reintentar tras un reinicio, que es justo el
    escenario en el que hoy se pierde todo.

    Sincrono a proposito, siguiendo el patron ya probado del resto del archivo:
    el cliente de Sheets (httplib2) no es seguro entre hilos y aqui se usa
    desde el hilo de la peticion. El volumen es de una docena de eventos por
    hora, no justifica estrenar concurrencia sobre ese cliente.

    Nunca propaga excepciones: un fallo de bitacora no puede tumbar una
    respuesta al prospecto ni un envio.
    """

    def __init__(self, appender: Callable[[str, List[Any]], Optional[int]],
                 dedupe_size: int = 512):
        # appender(tab, fila) -> numero de fila. Se inyecta desde app.py para que este
        # modulo no dependa de Google ni de la configuracion global.
        self._append = appender
        self._lock = threading.Lock()
        self._seen: deque = deque(maxlen=dedupe_size)
        self._seen_set: set = set()

    def _already_seen(self, event_id: str) -> bool:
        with self._lock:
            if event_id in self._seen_set:
                return True
            if len(self._seen) >= self._seen.maxlen:
                self._seen_set.discard(self._seen[0])
            self._seen.append(event_id)
            self._seen_set.add(event_id)
            return False

    def record(self, event: Dict[str, Any]) -> Optional[int]:
        """Anota el evento. Devuelve el numero de fila escrito, o None.

        El numero de fila lo entrega Sheets en la respuesta del append; con el
        se puede marcar despues el resultado de la entrega a Radar sin releer
        la pestana entera.

        La ventana anti-repeticion es en memoria y por eso parcial: tras un
        reinicio, un webhook reentregado por Meta puede volver a anotarse. No
        es un problema de correccion -- el event_id es determinista y Radar lo
        descarta como duplicado -- solo de prolijidad en la bitacora.
        """
        event_id = str(event.get("event_id") or "")
        if not event_id:
            log.warning("evento sin event_id; no se anota")
            return None
        if self._already_seen(event_id):
            return None

        lead = event.get("lead") or {}
        message = event.get("message") or {}
        delivery = event.get("delivery") or {}
        channel = event.get("channel") or {}
        fila = [
            event_id,
            event.get("event_type", ""),
            event.get("occurred_at", ""),
            event.get("source", ""),
            channel.get("phone_number_id", ""),
            lead.get("lead_id", ""),
            lead.get("phone_last10", ""),
            message.get("request_id") or "",
            message.get("wamid") or "",
            message.get("template") or "",
            message.get("campaign") or "",
            delivery.get("status") or "",
            "" if delivery.get("error_code") is None else str(delivery.get("error_code")),
            "TRUE" if event.get("backfill") else "FALSE",
            PENDIENTE,
            "0",
            "",
            json.dumps(event, ensure_ascii=False)[:45000],
        ]
        try:
            return self._append(EVENTS_TAB, fila)
        except Exception:
            log.exception("no se pudo anotar el evento %s en %s", event_id, EVENTS_TAB)
            return None


# ==========================
# Lectura de statuses de Meta
# ==========================
def statuses_from_value(value: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extrae de un `value` de webhook los estados en forma normalizada.

    Devuelve dicts con wamid, status, recipient, occurred_at canonico y el
    primer error si lo hay. No decide nada: quien llama resuelve la identidad
    del prospecto y arma el evento.
    """
    salida: List[Dict[str, Any]] = []
    for st in (value.get("statuses") or []):
        if not isinstance(st, dict):
            continue
        wamid = str(st.get("id") or "").strip()
        status = str(st.get("status") or "").strip().lower()
        if not wamid or not status:
            continue
        errores = st.get("errors") or []
        primero = errores[0] if isinstance(errores, list) and errores else {}
        if not isinstance(primero, dict):
            primero = {}
        try:
            occurred_at = canonical_ts(st.get("timestamp"))
        except Exception:
            occurred_at = canonical_ts()
        salida.append({
            "wamid": wamid,
            "status": status,
            "recipient": re.sub(r"\D", "", str(st.get("recipient_id") or "")),
            "occurred_at": occurred_at,
            "error_code": primero.get("code"),
            "error_title": primero.get("title"),
        })
    return salida


STATUS_TO_EVENT = {
    "sent": "message_sent",
    "delivered": "message_delivered",
    "read": "message_read",
    "failed": "message_failed",
}


# ==========================
# Cliente HTTP hacia Radar
# ==========================
class RadarClient:
    """Entrega un evento al endpoint de Radar segun el contrato 1.1.

    Arranca apagado: sin RADAR_EMIT_ENABLED=true no sale ni una peticion. Ese
    interruptor vive de este lado a proposito, para poder desconectar el
    enchufe sin depender de que Radar ponga VICKY_EVENTS_ACCEPTING=false.

    No reintenta por su cuenta. El evento ya quedo anotado en la bitacora
    durable con radar_state; reintentar es responsabilidad de un barrido
    posterior sobre las filas PENDIENTE, no de la peticion que lo origino.
    """

    def __init__(self, *, url: str, token: str, hmac_secret: str,
                 dispatch_token: str = "", enabled: bool = False,
                 timeout: float = 8.0, poster: Optional[Callable] = None):
        self.url = (url or "").strip()
        self.token = (token or "").strip()
        self.hmac_secret = (hmac_secret or "").strip()
        self.dispatch_token = (dispatch_token or "").strip()
        self.enabled = bool(enabled)
        self.timeout = timeout
        # Inyectable para poder probar sin red.
        self._post = poster

    def configured(self) -> bool:
        return bool(self.enabled and self.url and self.token and self.hmac_secret)

    def headers_for(self, cuerpo: bytes, *, delivery_id: str, timestamp: str) -> Dict[str, str]:
        """Cabeceras del contrato 1.1, seccion 3.

        La firma cubre `timestamp.delivery_id.cuerpo_crudo`, no solo el cuerpo:
        eso ata cada firma a un instante y a un intento concreto, de modo que
        una peticion capturada no puede reutilizarse indefinidamente.
        """
        import hashlib
        import hmac as _hmac

        base = f"{timestamp}.{delivery_id}.".encode("utf-8") + cuerpo
        firma = _hmac.new(self.hmac_secret.encode("utf-8"), base, hashlib.sha256).hexdigest()
        cabeceras = {
            "Content-Type": "application/json; charset=utf-8",
            "X-Vicky-Contract": CONTRACT_VERSION,
            "X-Vicky-Source": SOURCE,
            "X-Vicky-Token": self.token,
            "X-Vicky-Timestamp": timestamp,
            # Cambia en cada intento HTTP. No confundir con message.request_id,
            # que es de negocio y permanece igual durante los reintentos.
            "X-Vicky-Delivery-Id": delivery_id,
            "X-Vicky-Signature": f"sha256={firma}",
        }
        if self.dispatch_token:
            # Radar vive como Site privado: el gateway puede responder 401
            # antes de ejecutar el Worker si falta esta credencial.
            cabeceras["OAI-Sites-Authorization"] = f"Bearer {self.dispatch_token}"
        return cabeceras

    def send(self, event: Dict[str, Any]) -> str:
        """Devuelve el radar_state resultante: ENVIADO, RECHAZADO o PENDIENTE."""
        if not self.configured():
            return PENDIENTE

        cuerpo = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        delivery_id = str(uuid.uuid4())
        timestamp = str(int(datetime.now(timezone.utc).timestamp()))
        cabeceras = self.headers_for(cuerpo, delivery_id=delivery_id, timestamp=timestamp)

        poster = self._post
        if poster is None:
            import requests
            poster = requests.post

        try:
            resp = poster(self.url, data=cuerpo, headers=cabeceras, timeout=self.timeout)
        except Exception as exc:
            log.warning("Radar inalcanzable (%s); el evento queda pendiente", type(exc).__name__)
            return PENDIENTE

        codigo = getattr(resp, "status_code", 0)
        if codigo == 200:
            return ENVIADO
        if codigo in (400, 413):
            log.error(
                "Radar rechazo el evento %s con %s: %s",
                event.get("event_id"), codigo, str(getattr(resp, "text", ""))[:300],
            )
            return RECHAZADO
        if codigo in (401, 403):
            # Credencial o firma incorrectas. Se apaga el emisor: seguir
            # intentando solo acumula rechazos y ruido.
            log.error("Radar rechazo la autenticacion (%s); emisor apagado", codigo)
            self.enabled = False
            return PENDIENTE
        log.warning("Radar respondio %s; el evento queda pendiente", codigo)
        return PENDIENTE
