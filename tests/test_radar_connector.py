"""Enchufe hacia Radar y los bloqueadores de la auditoria forense 2026-09-04.

Cada prueba fija un hecho que la auditoria encontro roto en produccion:

- F-01  el webhook aceptaba cualquier cuerpo, sin firma de Meta.
- F-02  solo se procesaba messages[0]; los demas se perdian en silencio.
- F-03  los estados sent/delivered/read/failed llegaban y se descartaban.
- F-04  el envio podia duplicarse (sin reserva, con reintento de timeout).
- F-09  la identidad del prospecto era el numero de fila, no el LEAD_ID.

Mas el contrato 1.1 del enchufe (commit f78ea40): identidad determinista de
los eventos, fecha canonica del historico y firma de las peticiones a Radar.

Ninguna prueba toca red, Sheets, Meta ni Radar reales.
"""

import hashlib
import hmac
import json
from unittest.mock import patch

import pytest

import app as vicky
import radar_events


HEADERS = ["Nombre", "WhatsApp", "ESTATUS", "LAST_MESSAGE_AT", "Monto", "LEAD_ID"]
ROW_PENDING = ["chiwy", "6681620521", "", "", "15000", ""]
ROW_SELLADA = ["chiwy", "6681620521", "", "", "15000", "SC-ya-existente"]

PHONE_ID = "1045543821971905"


@pytest.fixture
def client():
    vicky.app.config["TESTING"] = True
    with vicky.app.test_client() as c:
        yield c


@pytest.fixture(autouse=True)
def sin_emisor():
    """El emisor arranca apagado; ninguna prueba debe hablar con Radar."""
    with patch.object(vicky._radar_client, "enabled", False):
        yield


def _webhook_payload(*, messages=None, statuses=None, phone_id=PHONE_ID):
    value = {"messaging_product": "whatsapp", "metadata": {"phone_number_id": phone_id}}
    if messages:
        value["messages"] = messages
    if statuses:
        value["statuses"] = statuses
    return {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": value}]}]}


def _texto(idx):
    return {"from": "5216681620521", "id": f"wamid.M{idx}", "type": "text",
            "text": {"body": f"mensaje {idx}"}}


# ==========================================================================
# F-01 — firma de Meta
# ==========================================================================
class TestFirmaDeMeta:
    def test_sin_secreto_configurado_el_webhook_sigue_atendiendo(self, client):
        """Desplegar la verificacion no puede dejar mudo al bot.

        Si META_APP_SECRET no esta puesto en Render, el evento se procesa y el
        hueco queda visible como ERROR en el log. Es el mismo criterio
        degradable que ya usa el aislamiento por WABA_PHONE_ID.
        """
        with patch.object(vicky, "META_APP_SECRET", ""), \
             patch.object(vicky, "WABA_PHONE_ID", PHONE_ID), \
             patch.object(vicky, "_handle_meta_statuses"), \
             patch.object(vicky, "_handle_inbound_message") as handler:
            resp = client.post("/webhook", json=_webhook_payload(messages=[_texto(1)]))

        assert resp.status_code == 200
        assert handler.call_count == 1

    def test_con_secreto_una_firma_valida_pasa(self, client):
        secreto = "s3cr3t0"
        cuerpo = json.dumps(_webhook_payload(messages=[_texto(1)])).encode("utf-8")
        firma = "sha256=" + hmac.new(secreto.encode(), cuerpo, hashlib.sha256).hexdigest()

        with patch.object(vicky, "META_APP_SECRET", secreto), \
             patch.object(vicky, "WABA_PHONE_ID", PHONE_ID), \
             patch.object(vicky, "_handle_meta_statuses"), \
             patch.object(vicky, "_handle_inbound_message") as handler:
            resp = client.post(
                "/webhook", data=cuerpo,
                headers={"Content-Type": "application/json", "X-Hub-Signature-256": firma},
            )

        assert resp.status_code == 200
        assert handler.call_count == 1

    @pytest.mark.parametrize("cabecera", ["", "sha256=deadbeef", "basura"])
    def test_con_secreto_una_firma_invalida_se_rechaza_con_403(self, client, cabecera):
        """El nucleo de F-01: sin firma valida no hay efectos comerciales."""
        cuerpo = json.dumps(_webhook_payload(messages=[_texto(1)])).encode("utf-8")

        with patch.object(vicky, "META_APP_SECRET", "s3cr3t0"), \
             patch.object(vicky, "WABA_PHONE_ID", PHONE_ID), \
             patch.object(vicky, "_handle_meta_statuses") as statuses, \
             patch.object(vicky, "_handle_inbound_message") as handler:
            resp = client.post(
                "/webhook", data=cuerpo,
                headers={"Content-Type": "application/json", "X-Hub-Signature-256": cabecera},
            )

        assert resp.status_code == 403
        assert handler.call_count == 0
        assert statuses.call_count == 0


# ==========================================================================
# F-02 — todos los mensajes del evento
# ==========================================================================
class TestTodosLosMensajes:
    def test_un_evento_con_tres_mensajes_los_procesa_los_tres(self, client):
        payload = _webhook_payload(messages=[_texto(1), _texto(2), _texto(3)])

        with patch.object(vicky, "META_APP_SECRET", ""), \
             patch.object(vicky, "WABA_PHONE_ID", PHONE_ID), \
             patch.object(vicky, "_handle_meta_statuses"), \
             patch.object(vicky, "_handle_inbound_message") as handler:
            resp = client.post("/webhook", json=payload)

        assert resp.status_code == 200
        assert handler.call_count == 3
        procesados = [c.args[0]["id"] for c in handler.call_args_list]
        assert procesados == ["wamid.M1", "wamid.M2", "wamid.M3"]

    def test_un_mensaje_que_revienta_no_impide_los_siguientes(self, client):
        payload = _webhook_payload(messages=[_texto(1), _texto(2)])

        with patch.object(vicky, "META_APP_SECRET", ""), \
             patch.object(vicky, "WABA_PHONE_ID", PHONE_ID), \
             patch.object(vicky, "_handle_meta_statuses"), \
             patch.object(vicky, "_handle_inbound_message",
                          side_effect=[RuntimeError("boom"), None]) as handler:
            resp = client.post("/webhook", json=payload)

        assert resp.status_code == 200
        assert handler.call_count == 2


# ==========================================================================
# F-03 — los estados de Meta se conservan
# ==========================================================================
def _status(wamid, estado, *, code=None, recipient="5216681620521"):
    st = {"id": wamid, "status": estado, "timestamp": "1788198313", "recipient_id": recipient}
    if code is not None:
        st["errors"] = [{"code": code, "title": f"error {code}"}]
    return st


class TestEstadosDeMeta:
    def test_los_estados_se_procesan_aunque_el_evento_traiga_mensajes(self, client):
        """Antes solo se miraban cuando NO habia mensajes; ahi se perdian."""
        payload = _webhook_payload(
            messages=[_texto(1)], statuses=[_status("wamid.A", "delivered")]
        )

        with patch.object(vicky, "META_APP_SECRET", ""), \
             patch.object(vicky, "WABA_PHONE_ID", PHONE_ID), \
             patch.object(vicky, "_handle_inbound_message"), \
             patch.object(vicky, "_handle_meta_statuses") as statuses:
            client.post("/webhook", json=payload)

        assert statuses.call_count == 1

    @pytest.mark.parametrize("estado,tipo", [
        ("sent", "message_sent"),
        ("delivered", "message_delivered"),
        ("read", "message_read"),
        ("failed", "message_failed"),
    ])
    def test_cada_estado_produce_su_evento(self, estado, tipo):
        value = {"metadata": {"phone_number_id": PHONE_ID},
                 "statuses": [_status("wamid.A", estado, code=131049 if estado == "failed" else None)]}

        with patch.object(vicky, "_lead_identity_for_phone",
                          return_value={"lead_id": "SC-1", "nombre": "Ana", "phone_last10": "6681620521"}), \
             patch.object(vicky, "record_radar_event") as record:
            anotados = vicky._handle_meta_statuses([value])

        assert anotados == 1
        assert record.call_count == 1
        assert record.call_args.kwargs["event_type"] == tipo
        assert record.call_args.kwargs["wamid"] == "wamid.A"
        assert record.call_args.kwargs["delivery_status"] == estado
        if estado == "failed":
            assert record.call_args.kwargs["error_code"] == 131049

    def test_un_estado_sin_lead_id_no_inventa_identidad(self):
        value = {"metadata": {"phone_number_id": PHONE_ID},
                 "statuses": [_status("wamid.A", "delivered")]}

        with patch.object(vicky, "_lead_identity_for_phone",
                          return_value={"lead_id": "", "nombre": "", "phone_last10": "6681620521"}), \
             patch.object(vicky, "record_radar_event") as record:
            vicky._handle_meta_statuses([value])

        assert record.call_count == 0

    def test_la_fecha_del_evento_es_la_de_meta_no_la_de_llegada(self):
        """Radar ordena por occurred_at; un estado tardio no puede fecharse hoy."""
        leidos = radar_events.statuses_from_value(
            {"statuses": [_status("wamid.A", "read")]}
        )
        assert leidos[0]["occurred_at"] == "2026-08-31T17:45:13.000Z"


# ==========================================================================
# Contrato 1.1 — identidad de los eventos
# ==========================================================================
class TestIdentidadDeEventos:
    def test_el_mismo_hecho_produce_siempre_el_mismo_event_id(self):
        """Es la propiedad de la que depende que reintentar sea gratis."""
        a = radar_events.event_id_for("message_sent", "wamid.XYZ")
        b = radar_events.event_id_for("message_sent", "wamid.XYZ")
        assert a == b

    def test_hechos_distintos_producen_ids_distintos(self):
        entregado = radar_events.event_id_for("message_delivered", "wamid.XYZ")
        leido = radar_events.event_id_for("message_read", "wamid.XYZ")
        otro = radar_events.event_id_for("message_delivered", "wamid.OTRO")
        assert len({entregado, leido, otro}) == 3

    def test_una_clave_vacia_no_produce_evento(self):
        with pytest.raises(ValueError):
            radar_events.event_id_for("message_sent", "")

    def test_el_historico_de_envios_se_llavea_por_lead_y_fecha(self):
        """En agosto no se guardo el wamid: la llave no puede depender de el."""
        clave = radar_events.clave_for(
            "message_sent", lead_id="SC-1", occurred_at="2026-08-28T23:10:28.000Z",
            backfill=True,
        )
        assert clave == "SC-1:2026-08-28T23:10:28.000Z"

    def test_el_historico_de_fallos_si_exige_el_wamid_real(self):
        clave = radar_events.clave_for("message_failed", wamid="wamid.REAL", backfill=True)
        assert clave == "wamid.REAL"

    def test_backfill_solo_se_admite_en_envios_y_fallos(self):
        with pytest.raises(ValueError):
            radar_events.build_event("message_read", lead_id="SC-1", wamid="w", backfill=True)

    def test_un_evento_sin_lead_id_se_rechaza(self):
        with pytest.raises(ValueError):
            radar_events.build_event("message_sent", lead_id="", wamid="w")


class TestFechaCanonica:
    def test_la_fecha_de_la_hoja_se_normaliza_a_milisegundos_en_cero(self):
        """Radar rechaza un backfill que no termine en .000Z."""
        assert radar_events.canonical_backfill_ts("2026-08-28 23:10:28") == "2026-08-28T23:10:28.000Z"

    def test_acepta_la_forma_iso_con_t_y_descarta_la_fraccion(self):
        assert radar_events.canonical_backfill_ts("2026-08-28T23:10:28.482913") == "2026-08-28T23:10:28.000Z"

    def test_dos_lecturas_de_la_misma_celda_dan_la_misma_llave(self):
        a = radar_events.canonical_backfill_ts("2026-08-28 23:10:28")
        b = radar_events.canonical_backfill_ts("2026-08-28T23:10:28.000Z")
        assert a == b

    def test_una_fecha_ilegible_se_rechaza_en_vez_de_inventarse(self):
        with pytest.raises(ValueError):
            radar_events.canonical_backfill_ts("ayer por la tarde")

    def test_el_epoch_de_meta_se_convierte_a_utc(self):
        assert radar_events.canonical_ts("1788198313") == "2026-08-31T17:45:13.000Z"


# ==========================================================================
# F-09 — identidad del prospecto
# ==========================================================================
class TestSelladoDeLeadId:
    def test_una_fila_sin_lead_id_se_sella_y_se_persiste(self):
        with patch.object(vicky, "_update_row_cells") as update:
            lead_id = vicky._seal_lead_id(7, HEADERS, list(ROW_PENDING))

        assert lead_id.startswith("SC-")
        update.assert_called_once()
        fila, updates, _ = update.call_args.args
        assert fila == 7
        assert updates == {"LEAD_ID": lead_id}

    def test_una_fila_ya_sellada_se_respeta_sin_reescribirla(self):
        with patch.object(vicky, "_update_row_cells") as update:
            lead_id = vicky._seal_lead_id(7, HEADERS, list(ROW_SELLADA))

        assert lead_id == "SC-ya-existente"
        assert update.call_count == 0

    def test_si_no_se_puede_persistir_igual_se_devuelve_la_identidad(self):
        """Perder el hecho seria peor que no poder atarlo todavia."""
        with patch.object(vicky, "_update_row_cells", side_effect=RuntimeError("sheets caido")):
            lead_id = vicky._seal_lead_id(7, HEADERS, list(ROW_PENDING))
        assert lead_id.startswith("SC-")

    def test_sin_columna_lead_id_no_se_inventa_nada(self):
        sin_columna = ["Nombre", "WhatsApp", "ESTATUS"]
        with patch.object(vicky, "_update_row_cells") as update:
            assert vicky._seal_lead_id(7, sin_columna, ["a", "b", ""]) == ""
        assert update.call_count == 0


# ==========================================================================
# F-04 — idempotencia del envio
# ==========================================================================
class FakeResp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"messages": [{"id": "wamid.ENVIADO"}]}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


@pytest.fixture
def envio_aislado():
    """Aisla /ext/auto-send-one: sin red, sin Sheets, sin bitacora real."""
    row_updates = []
    eventos = []

    with patch.object(vicky, "AUTO_SEND_TOKEN", "auto-secret"), \
         patch.object(vicky, "META_TOKEN", "token"), \
         patch.object(vicky, "WPP_API_URL", "https://graph.test/v20.0/1/messages"), \
         patch.object(vicky, "WABA_PHONE_ID", PHONE_ID), \
         patch.object(vicky, "_is_campaign_paused", return_value=False), \
         patch.object(vicky, "_sheet_get_rows", return_value=(HEADERS, [list(ROW_SELLADA)])), \
         patch.object(vicky, "_update_row_cells",
                      side_effect=lambda rn, upd, hdrs: row_updates.append((rn, dict(upd)))), \
         patch.object(vicky, "append_envio_status"), \
         patch.object(vicky, "_register_send_result", return_value=False), \
         patch.object(vicky, "record_radar_event",
                      side_effect=lambda **kw: eventos.append(kw)):
        yield {"row_updates": row_updates, "eventos": eventos}


def _lanzar(client, body=None):
    return client.post(
        "/ext/auto-send-one",
        json=body or {"template": "vida_temporal_v2", "image_url": "https://x.test/i.png"},
        headers={"X-AUTO-TOKEN": "auto-secret"},
    )


class TestIdempotenciaDelEnvio:
    def test_la_fila_se_reserva_antes_de_llamar_a_meta(self, client, envio_aislado):
        """Nucleo de F-04: antes se enviaba primero y se marcaba despues."""
        orden = []

        def fake_post(url, headers=None, json=None, timeout=None):
            orden.append("meta")
            return FakeResp()

        def registrar(rn, upd, hdrs):
            orden.append(f"sheet:{upd.get('ESTATUS')}")
            envio_aislado["row_updates"].append((rn, dict(upd)))

        with patch.object(vicky.requests, "post", side_effect=fake_post), \
             patch.object(vicky, "_update_row_cells", side_effect=registrar):
            resp = _lanzar(client)

        assert resp.status_code == 200
        assert orden[0] == "sheet:ENVIANDO", f"la reserva debe ir primero, fue {orden}"
        assert "meta" in orden
        assert orden.index("sheet:ENVIANDO") < orden.index("meta")

    def test_si_la_reserva_falla_no_se_envia_nada(self, client, envio_aislado):
        with patch.object(vicky, "_update_row_cells", side_effect=RuntimeError("sheets caido")), \
             patch.object(vicky.requests, "post") as post:
            resp = _lanzar(client)

        assert resp.status_code == 503
        assert resp.get_json()["reason"] == "reserva_fallida"
        assert post.call_count == 0

    def test_un_timeout_no_se_reintenta_y_deja_el_envio_incierto(self, client, envio_aislado):
        """La Cloud API no admite clave de idempotencia: reintentar duplica."""
        with patch.object(vicky.requests, "post",
                          side_effect=vicky.requests.exceptions.Timeout()) as post:
            resp = _lanzar(client)

        cuerpo = resp.get_json()
        assert post.call_count == 1, "un timeout no debe reintentarse en la campana"
        assert cuerpo["uncertain"] is True
        assert cuerpo["estatus"] == "ENVIO_INCIERTO"
        assert envio_aislado["row_updates"][-1][1]["ESTATUS"] == "ENVIO_INCIERTO"

    def test_un_envio_incierto_no_afirma_que_fallo(self, client, envio_aislado):
        with patch.object(vicky.requests, "post", side_effect=vicky.requests.exceptions.Timeout()):
            _lanzar(client)

        tipos = [e["event_type"] for e in envio_aislado["eventos"]]
        assert "message_requested" in tipos
        assert "message_failed" not in tipos, "nadie sabe si llego; no se puede declarar fallo"

    def test_un_envio_correcto_emite_solicitado_y_enviado_con_wamid(self, client, envio_aislado):
        with patch.object(vicky.requests, "post", return_value=FakeResp()):
            resp = _lanzar(client)

        cuerpo = resp.get_json()
        assert cuerpo["sent"] is True
        assert cuerpo["wamid"] == "wamid.ENVIADO"
        assert cuerpo["lead_id"] == "SC-ya-existente"

        tipos = [e["event_type"] for e in envio_aislado["eventos"]]
        assert tipos == ["message_requested", "message_sent"]
        enviado = envio_aislado["eventos"][1]
        assert enviado["wamid"] == "wamid.ENVIADO"
        assert enviado["lead_id"] == "SC-ya-existente"
        # El request_id ata el intento con su resultado.
        assert enviado["request_id"] == envio_aislado["eventos"][0]["request_id"]

    def test_el_estatus_definitivo_se_escribe_al_cerrar_la_reserva(self, client, envio_aislado):
        with patch.object(vicky.requests, "post", return_value=FakeResp()):
            _lanzar(client, {"template": "vida_temporal_v2", "image_url": "https://x.test/i.png",
                             "success_status": "ENVIADO_VIDA_TEMPORAL"})

        estatus = [upd["ESTATUS"] for _, upd in envio_aislado["row_updates"]]
        assert estatus == ["ENVIANDO", "ENVIADO_VIDA_TEMPORAL"]


# ==========================================================================
# Contrato 1.1 — cliente HTTP hacia Radar
# ==========================================================================
class TestClienteRadar:
    def _cliente(self, poster=None, **kw):
        base = dict(url="https://radar.test/api/v1/vicky/events", token="tok",
                    hmac_secret="sec", dispatch_token="disp", enabled=True)
        base.update(kw)
        return radar_events.RadarClient(poster=poster, **base)

    def test_apagado_por_defecto_no_sale_ninguna_peticion(self):
        llamadas = []
        cliente = self._cliente(poster=lambda *a, **k: llamadas.append(a), enabled=False)
        assert cliente.configured() is False
        assert cliente.send({"event_id": "x"}) == radar_events.PENDIENTE
        assert llamadas == []

    def test_sin_secreto_tampoco_emite(self):
        cliente = self._cliente(hmac_secret="")
        assert cliente.configured() is False

    def test_la_firma_cubre_timestamp_delivery_id_y_cuerpo(self):
        cliente = self._cliente()
        cuerpo = b'{"a":1}'
        cab = cliente.headers_for(cuerpo, delivery_id="D1", timestamp="1000")

        esperado = hmac.new(b"sec", b"1000.D1." + cuerpo, hashlib.sha256).hexdigest()
        assert cab["X-Vicky-Signature"] == f"sha256={esperado}"
        assert cab["X-Vicky-Contract"] == "1.1"
        assert cab["X-Vicky-Source"] == "vicky_secom"
        assert cab["X-Vicky-Token"] == "tok"
        assert cab["X-Vicky-Delivery-Id"] == "D1"
        assert cab["OAI-Sites-Authorization"] == "Bearer disp"

    def test_el_delivery_id_cambia_en_cada_intento(self):
        """No confundir con message.request_id, que permanece igual."""
        vistos = set()

        def poster(url, data=None, headers=None, timeout=None):
            vistos.add(headers["X-Vicky-Delivery-Id"])
            return FakeResp(200, {})

        cliente = self._cliente(poster=poster)
        cliente.send({"event_id": "x"})
        cliente.send({"event_id": "x"})
        assert len(vistos) == 2

    @pytest.mark.parametrize("codigo,esperado", [
        (200, radar_events.ENVIADO),
        (400, radar_events.RECHAZADO),
        (413, radar_events.RECHAZADO),
        (429, radar_events.PENDIENTE),
        (500, radar_events.PENDIENTE),
        (503, radar_events.PENDIENTE),
    ])
    def test_cada_respuesta_deja_el_estado_que_corresponde(self, codigo, esperado):
        cliente = self._cliente(poster=lambda *a, **k: FakeResp(codigo, {}))
        assert cliente.send({"event_id": "x"}) == esperado

    def test_un_401_apaga_el_emisor_en_vez_de_insistir(self):
        cliente = self._cliente(poster=lambda *a, **k: FakeResp(401, {}))
        assert cliente.send({"event_id": "x"}) == radar_events.PENDIENTE
        assert cliente.enabled is False

    def test_radar_inalcanzable_deja_el_evento_pendiente(self):
        def revienta(*a, **k):
            raise ConnectionError("sin ruta")

        cliente = self._cliente(poster=revienta)
        assert cliente.send({"event_id": "x"}) == radar_events.PENDIENTE


# ==========================================================================
# Bitacora durable
# ==========================================================================
class TestBitacora:
    def test_anota_la_fila_y_devuelve_su_numero(self):
        escritas = []
        log = radar_events.EventLog(lambda tab, fila: (escritas.append((tab, fila)), 42)[1])
        evento = radar_events.build_event("message_sent", lead_id="SC-1", wamid="wamid.A")

        assert log.record(evento) == 42
        tab, fila = escritas[0]
        assert tab == radar_events.EVENTS_TAB
        assert len(fila) == len(radar_events.EVENTS_HEADER)
        assert fila[radar_events.EVENTS_HEADER.index("radar_state")] == radar_events.PENDIENTE
        assert fila[radar_events.EVENTS_HEADER.index("wamid")] == "wamid.A"

    def test_el_mismo_evento_no_se_anota_dos_veces_seguidas(self):
        escritas = []
        log = radar_events.EventLog(lambda tab, fila: (escritas.append(fila), 1)[1])
        evento = radar_events.build_event("message_read", lead_id="SC-1", wamid="wamid.A")

        log.record(evento)
        log.record(evento)
        assert len(escritas) == 1

    def test_un_fallo_de_bitacora_no_propaga(self):
        def revienta(tab, fila):
            raise RuntimeError("sheets caido")

        log = radar_events.EventLog(revienta)
        evento = radar_events.build_event("message_sent", lead_id="SC-1", wamid="wamid.A")
        assert log.record(evento) is None
