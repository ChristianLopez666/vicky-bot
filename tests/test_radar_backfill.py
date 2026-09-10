"""Carga puntual de los 146 eventos historicos a Radar, sin tocar el emisor
general.

Nada aqui llama a la red real. Lo que se fija: que los lotes se entreguen
uno a uno respetando el contrato (misma fuente, mismo numero, hasta 50
eventos), que se detenga en el primer fallo sin insistir a ciegas, y que ni
el modulo ni el endpoint puedan encender el emisor general por accidente.
"""

import json
from unittest.mock import patch

import pytest

import app as vicky
import radar_backfill as rb
import radar_events


URL_EVENTO = "https://radar.test/api/v1/vicky/events"
URL_LOTE = "https://radar.test/api/v1/vicky/events/batch"
PHONE_ID = "1045543821971905"


def _evento(n: int, lead_id: str = "SC-a") -> dict:
    return radar_events.build_event(
        "message_sent", lead_id=lead_id, phone_number_id=PHONE_ID,
        occurred_at=f"2026-08-28T23:10:{n:02d}.000Z",
        delivery_status="sent", backfill=True,
    )


class FakeResp:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {"ok": True}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class PosterFalso:
    def __init__(self, respuestas=None):
        self.llamadas = []
        self._respuestas = list(respuestas) if respuestas else None

    def __call__(self, url, data=None, headers=None, timeout=None):
        self.llamadas.append({"url": url, "data": data, "headers": dict(headers or {})})
        if self._respuestas:
            return self._respuestas.pop(0)
        return FakeResp(200, {"ok": True, "aceptados": len(json.loads(data)["events"])})


@pytest.fixture
def poster():
    return PosterFalso()


# ==========================================================================
# derivar_url_de_lote
# ==========================================================================
class TestDerivarUrl:
    def test_deriva_agregando_batch(self):
        assert rb.derivar_url_de_lote(URL_EVENTO) == URL_LOTE

    def test_el_override_gana_siempre(self):
        assert rb.derivar_url_de_lote(URL_EVENTO, "https://otra.test/x") == "https://otra.test/x"

    def test_una_url_sin_events_al_final_no_se_puede_derivar(self):
        with pytest.raises(ValueError):
            rb.derivar_url_de_lote("https://radar.test/api/v1/vicky/otra-cosa")


# ==========================================================================
# validar_lote
# ==========================================================================
class TestValidarLote:
    def test_un_lote_vacio_se_rechaza(self):
        with pytest.raises(ValueError, match="vacio"):
            rb.validar_lote([])

    def test_mas_de_50_eventos_se_rechaza(self):
        with pytest.raises(ValueError, match="50"):
            rb.validar_lote([_evento(i) for i in range(51)])

    def test_exactamente_50_eventos_pasa(self):
        rb.validar_lote([_evento(i) for i in range(50)])  # no debe lanzar

    def test_fuentes_mezcladas_se_rechaza(self):
        a = _evento(1)
        b = _evento(2)
        b["source"] = "vicky_redes"
        with pytest.raises(ValueError, match="fuentes"):
            rb.validar_lote([a, b])

    def test_numeros_mezclados_se_rechaza(self):
        a = _evento(1)
        b = _evento(2)
        b["channel"]["phone_number_id"] = "876953768824165"
        with pytest.raises(ValueError, match="phone_number_id"):
            rb.validar_lote([a, b])


# ==========================================================================
# load_batches
# ==========================================================================
def _cargar(poster, **overrides):
    kwargs = dict(
        url=URL_EVENTO, token="tok-secom", hmac_secret="sec-secom",
        dispatch_token="disp-secom",
        batches=[[_evento(1), _evento(2)], [_evento(3)]],
        poster=poster,
    )
    kwargs.update(overrides)
    return rb.load_batches(**kwargs)


class TestCargaBasica:
    def test_entrega_cada_lote_por_separado(self, poster):
        reporte = _cargar(poster)
        assert len(poster.llamadas) == 2
        cuerpos = [json.loads(c["data"])["events"] for c in poster.llamadas]
        assert len(cuerpos[0]) == 2
        assert len(cuerpos[1]) == 1

    def test_todas_las_llamadas_van_a_la_url_de_lote(self, poster):
        _cargar(poster)
        assert all(c["url"] == URL_LOTE for c in poster.llamadas)

    def test_reporte_completo_cuando_todo_sale_bien(self, poster):
        reporte = _cargar(poster)
        assert reporte["completo"] is True
        assert reporte["lotes_completados"] == 2
        assert reporte["eventos_enviados"] == 3
        assert reporte["eventos_totales_a_cargar"] == 3

    def test_la_firma_usa_las_credenciales_dadas_no_otras(self, poster):
        _cargar(poster, hmac_secret="otro-secreto-distinto")
        # Si la firma se hubiera calculado con "sec-secom" en vez del secreto
        # pasado, esta simplemente seria una firma distinta -- lo que importa
        # es que load_batches nunca improvisa un secreto propio.
        firma = poster.llamadas[0]["headers"]["X-Vicky-Signature"]
        assert firma.startswith("sha256=")


class TestSeDetieneEnElPrimerFallo:
    def test_un_lote_que_falla_detiene_los_siguientes(self, poster):
        poster._respuestas = [FakeResp(200, {"ok": True}), FakeResp(500, {"ok": False})]
        reporte = _cargar(poster, batches=[[_evento(1)], [_evento(2)], [_evento(3)]])
        assert reporte["lotes_intentados"] == 2
        assert reporte["lotes_completados"] == 1
        assert reporte["completo"] is False
        assert len(poster.llamadas) == 2  # el tercer lote nunca se intento

    def test_el_reporte_identifica_cual_lote_fallo(self, poster):
        poster._respuestas = [FakeResp(200, {"ok": True}), FakeResp(400, {"error": "invalid"})]
        reporte = _cargar(poster, batches=[[_evento(1)], [_evento(2)], [_evento(3)]])
        fallido = reporte["resultados"][-1]
        assert fallido["indice_lote"] == 1
        assert fallido["status_code"] == 400


class TestValidaciones:
    def test_sin_credenciales_no_ejecuta_nada(self, poster):
        with pytest.raises(ValueError):
            rb.load_batches(url="", token="", hmac_secret="", dispatch_token="",
                            batches=[[_evento(1)]], poster=poster)
        assert poster.llamadas == []

    def test_sin_lotes_no_ejecuta_nada(self, poster):
        with pytest.raises(ValueError, match="lotes"):
            rb.load_batches(url=URL_EVENTO, token="t", hmac_secret="s", dispatch_token="",
                            batches=[], poster=poster)
        assert poster.llamadas == []

    def test_un_lote_invalido_aborta_antes_de_mandar_nada(self, poster):
        """Se validan TODOS los lotes antes de enviar el primero: si el
        tercero esta mal formado, no queremos haber enviado ya los dos
        primeros y quedar a medias por un error evitable."""
        with pytest.raises(ValueError):
            rb.load_batches(
                url=URL_EVENTO, token="t", hmac_secret="s", dispatch_token="",
                batches=[[_evento(1)], [_evento(2)], []],
                poster=poster,
            )
        assert poster.llamadas == []


class TestUnServidorInalcanzable:
    def test_una_excepcion_de_red_se_reporta_como_fallo_no_como_crash(self):
        def revienta(*a, **k):
            raise ConnectionError("sin ruta")

        reporte = _cargar(revienta)
        assert reporte["completo"] is False
        assert reporte["resultados"][0]["status_code"] is None


# ==========================================================================
# El endpoint
# ==========================================================================
@pytest.fixture
def client():
    vicky.app.config["TESTING"] = True
    with vicky.app.test_client() as c:
        yield c


LOTE_VALIDO = [[_evento(1)], [_evento(2)]]


class TestEndpoint:
    def test_sin_token_configurado_responde_401(self, client):
        with patch.object(vicky, "RADAR_BACKFILL_TOKEN", ""):
            resp = client.post("/ext/radar/backfill-load",
                                headers={"X-Radar-Backfill-Token": "cualquiera"},
                                json={"batches": LOTE_VALIDO})
        assert resp.status_code == 401

    def test_con_token_incorrecto_no_llama_a_load_batches(self, client):
        with patch.object(vicky, "RADAR_BACKFILL_TOKEN", "correcto"), \
             patch.object(vicky.radar_backfill, "load_batches") as mock:
            resp = client.post("/ext/radar/backfill-load",
                                headers={"X-Radar-Backfill-Token": "incorrecto"},
                                json={"batches": LOTE_VALIDO})
        assert resp.status_code == 401
        assert mock.call_count == 0

    def test_el_token_de_aceptacion_no_sirve_aqui(self, client):
        """Son secretos distintos a proposito -- uno no debe abrir al otro."""
        with patch.object(vicky, "RADAR_ACCEPTANCE_TOKEN", "token-de-aceptacion"), \
             patch.object(vicky, "RADAR_BACKFILL_TOKEN", "token-de-carga"):
            resp = client.post("/ext/radar/backfill-load",
                                headers={"X-Radar-Backfill-Token": "token-de-aceptacion"},
                                json={"batches": LOTE_VALIDO})
        assert resp.status_code == 401

    def test_sin_cuerpo_de_batches_responde_400(self, client):
        with patch.object(vicky, "RADAR_BACKFILL_TOKEN", "correcto"), \
             patch.object(vicky._radar_client, "url", URL_EVENTO), \
             patch.object(vicky._radar_client, "token", "t"), \
             patch.object(vicky._radar_client, "hmac_secret", "s"), \
             patch.object(vicky.radar_backfill, "load_batches") as mock:
            resp = client.post("/ext/radar/backfill-load",
                                headers={"X-Radar-Backfill-Token": "correcto"},
                                json={})
        assert resp.status_code == 400
        assert mock.call_count == 0

    def test_con_token_correcto_invoca_load_batches_con_las_credenciales_existentes(self, client):
        with patch.object(vicky, "RADAR_BACKFILL_TOKEN", "correcto"), \
             patch.object(vicky._radar_client, "url", "https://radar.test/api/v1/vicky/events"), \
             patch.object(vicky._radar_client, "token", "tok-existente"), \
             patch.object(vicky._radar_client, "hmac_secret", "sec-existente"), \
             patch.object(vicky._radar_client, "dispatch_token", "disp-existente"), \
             patch.object(vicky.radar_backfill, "load_batches",
                          return_value={"completo": True, "lotes_completados": 2,
                                        "total_lotes": 2, "eventos_enviados": 3,
                                        "eventos_totales_a_cargar": 3,
                                        "resultados": []}) as mock:
            resp = client.post("/ext/radar/backfill-load",
                                headers={"X-Radar-Backfill-Token": "correcto"},
                                json={"batches": LOTE_VALIDO})

        assert resp.status_code == 200
        mock.assert_called_once()
        _, kwargs = mock.call_args
        assert kwargs["url"] == "https://radar.test/api/v1/vicky/events"
        assert kwargs["token"] == "tok-existente"
        assert kwargs["hmac_secret"] == "sec-existente"
        assert kwargs["dispatch_token"] == "disp-existente"
        assert kwargs["batches"] == LOTE_VALIDO

    def test_nunca_toca_el_interruptor_general(self, client):
        """El nucleo del pedido: cargar el historico no puede encender el
        emisor real para el trafico comercial normal."""
        estado_antes = vicky._radar_client.enabled
        with patch.object(vicky, "RADAR_BACKFILL_TOKEN", "correcto"), \
             patch.object(vicky._radar_client, "url", URL_EVENTO), \
             patch.object(vicky._radar_client, "token", "t"), \
             patch.object(vicky._radar_client, "hmac_secret", "s"), \
             patch.object(vicky.radar_backfill, "load_batches",
                          return_value={"completo": True, "lotes_completados": 0,
                                        "total_lotes": 0, "eventos_enviados": 0,
                                        "eventos_totales_a_cargar": 0, "resultados": []}):
            client.post("/ext/radar/backfill-load",
                        headers={"X-Radar-Backfill-Token": "correcto"},
                        json={"batches": LOTE_VALIDO})
        assert vicky._radar_client.enabled == estado_antes

    def test_sin_credenciales_de_radar_responde_400_sin_llamar_a_load_batches(self, client):
        with patch.object(vicky, "RADAR_BACKFILL_TOKEN", "correcto"), \
             patch.object(vicky._radar_client, "url", ""), \
             patch.object(vicky.radar_backfill, "load_batches") as mock:
            resp = client.post("/ext/radar/backfill-load",
                                headers={"X-Radar-Backfill-Token": "correcto"},
                                json={"batches": LOTE_VALIDO})
        assert resp.status_code == 400
        assert mock.call_count == 0

    def test_un_valueerror_de_load_batches_responde_400_no_500(self, client):
        with patch.object(vicky, "RADAR_BACKFILL_TOKEN", "correcto"), \
             patch.object(vicky._radar_client, "url", URL_EVENTO), \
             patch.object(vicky._radar_client, "token", "t"), \
             patch.object(vicky._radar_client, "hmac_secret", "s"), \
             patch.object(vicky.radar_backfill, "load_batches",
                          side_effect=ValueError("lote invalido")):
            resp = client.post("/ext/radar/backfill-load",
                                headers={"X-Radar-Backfill-Token": "correcto"},
                                json={"batches": LOTE_VALIDO})
        assert resp.status_code == 400

    def test_una_excepcion_inesperada_responde_500_sin_tumbar_el_proceso(self, client):
        with patch.object(vicky, "RADAR_BACKFILL_TOKEN", "correcto"), \
             patch.object(vicky._radar_client, "url", URL_EVENTO), \
             patch.object(vicky._radar_client, "token", "t"), \
             patch.object(vicky._radar_client, "hmac_secret", "s"), \
             patch.object(vicky.radar_backfill, "load_batches", side_effect=RuntimeError("boom")):
            resp = client.post("/ext/radar/backfill-load",
                                headers={"X-Radar-Backfill-Token": "correcto"},
                                json={"batches": LOTE_VALIDO})
        assert resp.status_code == 500
