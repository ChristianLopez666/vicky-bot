"""Ejecucion unica de aceptacion contra Radar, sin tocar el emisor general.

Nada aqui llama a la red real: `poster` siempre se inyecta como un doble de
prueba. Lo que se fija es la forma exacta de los seis checks -- que peticion
sale, con que cabeceras, y que criterio decide si pasaron -- y que ni el
modulo ni el endpoint puedan encender el emisor general por accidente.
"""

import hashlib
import hmac
import json
from unittest.mock import patch

import pytest

import app as vicky
import radar_acceptance as ra
import radar_events


PHONE_ID = "1045543821971905"
OTRO_PHONE_ID = "876953768824165"
LEAD_ID = "SC-real-precargado"


class FakeResp:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class PosterFalso:
    """Responde segun lo que el propio evento del cuerpo declara, para poder
    simular servidor sin necesitar un servidor real."""

    def __init__(self):
        self.llamadas = []
        self._vistos = set()

    def __call__(self, url, data=None, headers=None, timeout=None):
        self.llamadas.append({"url": url, "data": data, "headers": dict(headers or {})})
        evento = json.loads(data)

        firma = (headers or {}).get("X-Vicky-Signature", "")
        if firma == ra.FIRMA_INVALIDA:
            return FakeResp(401, {"ok": False, "error": "invalid_signature"})

        source_hdr = (headers or {}).get("X-Vicky-Source", "")
        if source_hdr != "vicky_secom":
            return FakeResp(403, {"ok": False, "error": "source_not_authorized_for_token"})

        event_id = evento["event_id"]
        if event_id in self._vistos:
            return FakeResp(200, {"ok": True, "event_id": event_id, "duplicate": True})
        self._vistos.add(event_id)

        lead_matched = evento["lead"]["lead_id"] != ra.LEAD_ID_DESCONOCIDO
        return FakeResp(200, {
            "ok": True, "event_id": event_id, "duplicate": False,
            "lead_matched": lead_matched,
        })


@pytest.fixture
def poster():
    return PosterFalso()


def _run(poster, **overrides):
    kwargs = dict(
        url="https://radar.test/api/v1/vicky/events",
        token="tok-secom", hmac_secret="sec-secom", dispatch_token="disp-secom",
        phone_number_id=PHONE_ID, known_lead_id=LEAD_ID,
        other_phone_number_id=OTRO_PHONE_ID, poster=poster,
    )
    kwargs.update(overrides)
    return ra.run(**kwargs)


class TestSeisChecks:
    def test_corre_los_seis_checks_en_orden(self, poster):
        reporte = _run(poster)
        nombres = [r["check"] for r in reporte["resultados"]]
        assert nombres == [
            "firma_invalida", "valido", "duplicado",
            "lead_desconocido", "aislamiento_fuente", "aislamiento_numero",
        ]

    def test_sin_other_phone_number_id_se_omite_el_sexto(self, poster):
        reporte = _run(poster, other_phone_number_id="")
        nombres = [r["check"] for r in reporte["resultados"]]
        assert "aislamiento_numero" not in nombres
        assert len(nombres) == 5

    def test_los_cinco_con_criterio_duro_pasan_contra_un_servidor_correcto(self, poster):
        reporte = _run(poster)
        con_criterio = [r for r in reporte["resultados"] if r["paso"] is not None]
        assert len(con_criterio) == 5
        assert all(r["paso"] for r in con_criterio), con_criterio


class TestFirmaInvalida:
    def test_manda_una_firma_que_no_es_la_real(self, poster):
        _run(poster)
        firmas = [c["headers"]["X-Vicky-Signature"] for c in poster.llamadas]
        assert firmas[0] == ra.FIRMA_INVALIDA

    def test_el_hmac_real_nunca_hubiera_dado_esa_firma(self):
        cuerpo = b'{"a":1}'
        real = hmac.new(b"sec-secom", b"1.d.".replace(b"d", b"delivery") + cuerpo, hashlib.sha256).hexdigest()
        assert f"sha256={real}" != ra.FIRMA_INVALIDA


class TestValidoYDuplicado:
    def test_valido_y_duplicado_comparten_event_id(self, poster):
        reporte = _run(poster)
        validos = [r for r in reporte["resultados"] if r["check"] in ("valido", "duplicado")]
        assert validos[0]["obtenido"]["body"]["event_id"] == validos[1]["obtenido"]["body"]["event_id"]

    def test_duplicado_reenvia_el_mismo_cuerpo_no_uno_nuevo(self, poster):
        _run(poster)
        cuerpos = [json.loads(c["data"]) for c in poster.llamadas]
        # posiciones: 0 firma_invalida, 1 valido, 2 duplicado
        assert cuerpos[1]["event_id"] == cuerpos[2]["event_id"]

    def test_informa_lead_matched_sin_exigirlo(self, poster):
        reporte = _run(poster)
        valido = next(r for r in reporte["resultados"] if r["check"] == "valido")
        assert valido["lead_matched_informativo"] is True
        # el pase no depende de lead_matched, solo de duplicate:false
        assert valido["paso"] is True


class TestLeadDesconocido:
    def test_usa_la_identidad_reservada_no_un_prospecto_real(self, poster):
        _run(poster)
        cuerpos = [json.loads(c["data"]) for c in poster.llamadas]
        desconocido = cuerpos[3]
        assert desconocido["lead"]["lead_id"] == ra.LEAD_ID_DESCONOCIDO
        assert desconocido["lead"]["lead_id"] != LEAD_ID

    def test_nunca_se_confunde_con_un_lead_id_real(self):
        assert ra.LEAD_ID_DESCONOCIDO.startswith("SC-00000000")


class TestAislamiento:
    def test_aislamiento_por_fuente_usa_credenciales_de_secom_pero_declara_redes(self, poster):
        _run(poster)
        llamada = poster.llamadas[4]
        cuerpo = json.loads(llamada["data"])
        assert llamada["headers"]["X-Vicky-Source"] == "vicky_redes"
        assert cuerpo["source"] == "vicky_redes"
        # la firma SI se calcula con el HMAC real de SECOM, no uno inventado
        assert llamada["headers"]["X-Vicky-Signature"] != ra.FIRMA_INVALIDA

    def test_aislamiento_por_numero_usa_el_phone_number_id_de_redes(self, poster):
        _run(poster)
        cuerpo6 = json.loads(poster.llamadas[5]["data"])
        assert cuerpo6["channel"]["phone_number_id"] == OTRO_PHONE_ID
        # pero la fuente sigue siendo secom -- solo cambia el numero
        assert cuerpo6["source"] == "vicky_secom"

    def test_aislamiento_por_numero_no_exige_un_resultado_concreto(self, poster):
        reporte = _run(poster)
        numero = next(r for r in reporte["resultados"] if r["check"] == "aislamiento_numero")
        assert numero["paso"] is None


class TestValidaciones:
    def test_sin_credenciales_no_ejecuta_nada(self, poster):
        with pytest.raises(ValueError):
            ra.run(url="", token="", hmac_secret="", dispatch_token="",
                   phone_number_id=PHONE_ID, known_lead_id=LEAD_ID, poster=poster)
        assert poster.llamadas == []

    def test_sin_phone_number_id_no_ejecuta_nada(self, poster):
        with pytest.raises(ValueError):
            ra.run(url="https://radar.test", token="t", hmac_secret="s", dispatch_token="",
                   phone_number_id="", known_lead_id=LEAD_ID, poster=poster)
        assert poster.llamadas == []


class TestUnServidorInalcanzable:
    def test_una_excepcion_de_red_se_reporta_sin_propagar(self):
        def revienta(*a, **k):
            raise ConnectionError("sin ruta")

        reporte = _run(revienta)
        assert all(r["obtenido"]["status_code"] is None for r in reporte["resultados"])
        assert all(r["paso"] is not True for r in reporte["resultados"])


# ==========================================================================
# El endpoint: no toca el emisor general, exige su propio token
# ==========================================================================
@pytest.fixture
def client():
    vicky.app.config["TESTING"] = True
    with vicky.app.test_client() as c:
        yield c


class TestEndpoint:
    def test_sin_token_configurado_responde_401(self, client):
        with patch.object(vicky, "RADAR_ACCEPTANCE_TOKEN", ""):
            resp = client.post("/ext/radar/acceptance-test",
                                headers={"X-Radar-Acceptance-Token": "cualquiera"})
        assert resp.status_code == 401

    def test_con_token_incorrecto_responde_401_y_no_llama_a_radar_acceptance(self, client):
        with patch.object(vicky, "RADAR_ACCEPTANCE_TOKEN", "correcto"), \
             patch.object(vicky.radar_acceptance, "run") as run_mock:
            resp = client.post("/ext/radar/acceptance-test",
                                headers={"X-Radar-Acceptance-Token": "incorrecto"})
        assert resp.status_code == 401
        assert run_mock.call_count == 0

    def test_con_token_correcto_invoca_run_con_las_credenciales_existentes(self, client):
        with patch.object(vicky, "RADAR_ACCEPTANCE_TOKEN", "correcto"), \
             patch.object(vicky._radar_client, "url", "https://radar.test/events"), \
             patch.object(vicky._radar_client, "token", "tok-existente"), \
             patch.object(vicky._radar_client, "hmac_secret", "sec-existente"), \
             patch.object(vicky._radar_client, "dispatch_token", "disp-existente"), \
             patch.object(vicky.radar_acceptance, "run",
                          return_value={"aprobados": 5, "checks_con_criterio_duro": 5,
                                        "resultados": []}) as run_mock:
            resp = client.post("/ext/radar/acceptance-test",
                                headers={"X-Radar-Acceptance-Token": "correcto"})

        assert resp.status_code == 200
        run_mock.assert_called_once()
        _, kwargs = run_mock.call_args
        assert kwargs["url"] == "https://radar.test/events"
        assert kwargs["token"] == "tok-existente"
        assert kwargs["hmac_secret"] == "sec-existente"
        assert kwargs["dispatch_token"] == "disp-existente"

    def test_nunca_toca_el_interruptor_general(self, client):
        """El nucleo de todo el pedido: correr esto no puede encender el
        emisor real para el trafico comercial normal."""
        estado_antes = vicky._radar_client.enabled
        with patch.object(vicky, "RADAR_ACCEPTANCE_TOKEN", "correcto"), \
             patch.object(vicky._radar_client, "url", "https://radar.test/events"), \
             patch.object(vicky._radar_client, "token", "t"), \
             patch.object(vicky._radar_client, "hmac_secret", "s"), \
             patch.object(vicky.radar_acceptance, "run",
                          return_value={"aprobados": 0, "checks_con_criterio_duro": 0,
                                        "resultados": []}):
            client.post("/ext/radar/acceptance-test",
                        headers={"X-Radar-Acceptance-Token": "correcto"})
        assert vicky._radar_client.enabled == estado_antes

    def test_sin_credenciales_de_radar_responde_400_sin_llamar_a_run(self, client):
        with patch.object(vicky, "RADAR_ACCEPTANCE_TOKEN", "correcto"), \
             patch.object(vicky._radar_client, "url", ""), \
             patch.object(vicky.radar_acceptance, "run") as run_mock:
            resp = client.post("/ext/radar/acceptance-test",
                                headers={"X-Radar-Acceptance-Token": "correcto"})
        assert resp.status_code == 400
        assert run_mock.call_count == 0

    def test_una_excepcion_de_run_responde_500_sin_tumbar_el_proceso(self, client):
        with patch.object(vicky, "RADAR_ACCEPTANCE_TOKEN", "correcto"), \
             patch.object(vicky._radar_client, "url", "https://radar.test/events"), \
             patch.object(vicky._radar_client, "token", "t"), \
             patch.object(vicky._radar_client, "hmac_secret", "s"), \
             patch.object(vicky.radar_acceptance, "run", side_effect=RuntimeError("boom")):
            resp = client.post("/ext/radar/acceptance-test",
                                headers={"X-Radar-Acceptance-Token": "correcto"})
        assert resp.status_code == 500


class TestHeadersForConSourceOverride:
    def test_sin_override_usa_la_fuente_del_modulo(self):
        cliente = radar_events.RadarClient(url="u", token="t", hmac_secret="s", enabled=True)
        cab = cliente.headers_for(b"{}", delivery_id="d", timestamp="1")
        assert cab["X-Vicky-Source"] == "vicky_secom"

    def test_con_override_declara_la_fuente_pedida(self):
        cliente = radar_events.RadarClient(url="u", token="t", hmac_secret="s", enabled=True)
        cab = cliente.headers_for(b"{}", delivery_id="d", timestamp="1", source="vicky_redes")
        assert cab["X-Vicky-Source"] == "vicky_redes"

    def test_el_override_no_cambia_la_firma_calculada_con_el_mismo_cuerpo(self):
        """La firma cubre timestamp+delivery_id+cuerpo, no la cabecera de
        fuente: por diseno, cambiar solo el rotulo de fuente no invalida la
        firma -- es la credencial (token/HMAC) la que prueba identidad, no
        este campo. Por eso el check de aislamiento tiene sentido: la firma
        sigue siendo valida y aun asi Radar debe rechazar por el token."""
        cliente = radar_events.RadarClient(url="u", token="t", hmac_secret="s", enabled=True)
        a = cliente.headers_for(b"{}", delivery_id="d", timestamp="1")
        b = cliente.headers_for(b"{}", delivery_id="d", timestamp="1", source="vicky_redes")
        assert a["X-Vicky-Signature"] == b["X-Vicky-Signature"]
