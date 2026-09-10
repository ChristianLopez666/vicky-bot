"""Ejecucion unica de aceptacion contra Radar, sin tocar el emisor general.

Nada aqui llama a la red real: `poster` siempre se inyecta como un doble de
prueba. Lo que se fija es la forma exacta de los seis checks -- que peticion
sale, con que cabeceras, y que criterio decide si pasaron -- y que ni el
modulo ni el endpoint puedan encender el emisor general por accidente.
"""

import hashlib
import hmac
import json
import re
from unittest.mock import patch

import pytest

import app as vicky
import radar_acceptance as ra
import radar_events


PHONE_ID = "1045543821971905"
OTRO_PHONE_ID = "876953768824165"
# Mismo formato que el LEAD_ID real confirmado por Work el 2026-09-09
# (SC-0088db7a-385f-4f48-bbae-2aa79ae92c5d, Antonio Cota Lugo) -- aqui se usa
# un valor de prueba propio para no acoplar la suite a un dato de produccion.
LEAD_ID = "SC-real-precargado"


class FakeResp:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class PosterFalso:
    """Simula el comportamiento de Radar confirmado por Work el 2026-09-09,
    en el mismo orden de validacion: firma, consistencia cabecera/cuerpo,
    numero autorizado, y solo entonces duplicado/lead_matched."""

    def __init__(self):
        self.llamadas = []
        self._vistos = set()

    def __call__(self, url, data=None, headers=None, timeout=None):
        headers = dict(headers or {})
        self.llamadas.append({"url": url, "data": data, "headers": headers})
        evento = json.loads(data)

        firma = headers.get("X-Vicky-Signature", "")
        if firma == ra.FIRMA_INVALIDA:
            return FakeResp(401, {"ok": False, "error": "invalid_signature"})

        fuente_cabecera = headers.get("X-Vicky-Source", "")
        fuente_cuerpo = evento.get("source", "")
        if fuente_cabecera != fuente_cuerpo:
            return FakeResp(400, {"ok": False, "error": "source_header_body_mismatch"})

        numero = (evento.get("channel") or {}).get("phone_number_id", "")
        if numero != PHONE_ID:
            return FakeResp(403, {"ok": False, "error": "phone_number_id_not_authorized"})

        # Replica el hallazgo real del 2026-09-10: Radar exige 521 + 10
        # digitos en lead.phone_e164 y rechaza con 400 antes de mirar nada
        # mas si el formato no cuadra. El primer intento real de correr esta
        # bateria no mandaba telefono en ningun evento y los seis checks
        # fallaron por esto, no por lo que cada uno pretendia probar.
        telefono = (evento.get("lead") or {}).get("phone_e164", "")
        if not re.fullmatch(r"521\d{10}", telefono):
            return FakeResp(400, {
                "ok": False, "error": "invalid_payload",
                "detail": "lead.phone_e164 debe usar 521 + 10 dígitos.",
            })

        event_id = evento["event_id"]
        if event_id in self._vistos:
            return FakeResp(200, {"ok": True, "event_id": event_id, "duplicate": True})
        self._vistos.add(event_id)

        lead_matched = evento["lead"]["lead_id"] == LEAD_ID
        return FakeResp(200, {
            "ok": True, "event_id": event_id, "duplicate": False,
            "lead_matched": lead_matched,
        })


@pytest.fixture
def poster():
    return PosterFalso()


LEAD_PHONE_LAST10 = "6681735052"  # telefono de prueba, formato real


def _run(poster, **overrides):
    kwargs = dict(
        url="https://radar.test/api/v1/vicky/events",
        token="tok-secom", hmac_secret="sec-secom", dispatch_token="disp-secom",
        phone_number_id=PHONE_ID, known_lead_id=LEAD_ID,
        known_lead_phone_last10=LEAD_PHONE_LAST10,
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

    def test_los_seis_checks_pasan_contra_un_servidor_correcto(self, poster):
        """Desde la correccion de Work (2026-09-09) los seis checks tienen
        criterio duro: ninguno queda en None."""
        reporte = _run(poster)
        assert all(r["paso"] is not None for r in reporte["resultados"])
        assert all(r["paso"] for r in reporte["resultados"]), reporte["resultados"]
        assert reporte["aprobados"] == 6


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

    def test_valido_exige_lead_matched_true(self, poster):
        """Correccion de Work (2026-09-09): known_lead_id ahora es un LEAD_ID
        confirmado como ya enlazado, asi que el check exige lead_matched:true,
        no solo duplicate:false."""
        reporte = _run(poster)
        valido = next(r for r in reporte["resultados"] if r["check"] == "valido")
        assert valido["obtenido"]["body"]["lead_matched"] is True
        assert valido["paso"] is True

    def test_valido_falla_si_el_lead_id_no_esta_enlazado(self, poster):
        """Si known_lead_id no coincide con lo que Radar reconoce, el check
        debe fallar -- ya no basta con que el evento se acepte."""
        reporte = _run(poster, known_lead_id="SC-no-enlazado-todavia")
        valido = next(r for r in reporte["resultados"] if r["check"] == "valido")
        assert valido["obtenido"]["body"]["lead_matched"] is False
        assert valido["paso"] is False

    def test_el_telefono_del_evento_valido_usa_el_formato_521_mas_10(self, poster):
        """Hallazgo del 2026-09-10: el primer intento real no mandaba
        telefono en ningun evento y los seis checks fallaron por formato,
        no por lo que cada uno pretendia probar."""
        _run(poster)
        cuerpo_valido = json.loads(poster.llamadas[1]["data"])
        assert cuerpo_valido["lead"]["phone_e164"] == f"521{LEAD_PHONE_LAST10}"
        assert cuerpo_valido["lead"]["phone_last10"] == LEAD_PHONE_LAST10

    def test_un_telefono_sin_el_formato_521_mas_10_lo_rechaza_el_poster(self, poster):
        """Contraprueba directa sobre el doble de Radar: confirma que el
        propio simulador (no solo run()) habria atrapado el defecto del
        2026-09-10 si algo se hubiera colado sin pasar por _evento()."""
        vacio = {"lead": {"phone_e164": "", "lead_id": LEAD_ID}, "event_id": "x",
                 "channel": {"phone_number_id": PHONE_ID}, "source": "vicky_secom"}
        resp = poster(url="https://radar.test", data=json.dumps(vacio),
                      headers={"X-Vicky-Source": "vicky_secom", "X-Vicky-Signature": "sha256=abc"})
        assert resp.status_code == 400


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
    """Rediseño del 2026-09-09 (correccion de Work): la version anterior de
    'aislamiento_fuente' declaraba vicky_redes tanto en cabecera como en
    cuerpo usando el token real de SECOM -- eso no demuestra aislamiento de
    credenciales entre fuentes, porque Redes todavia no tiene su propio
    token configurado en Radar. Lo que SI es verificable hoy es la regla de
    consistencia interna del contrato: la cabecera debe declarar lo mismo
    que el cuerpo. Aqui la cabecera queda en vicky_secom (la real, la que
    prueban el token y el HMAC) y solo el cuerpo declara vicky_redes."""

    def test_aislamiento_por_fuente_deja_la_cabecera_en_secom_y_el_cuerpo_en_redes(self, poster):
        _run(poster)
        llamada = poster.llamadas[4]
        cuerpo = json.loads(llamada["data"])
        assert llamada["headers"]["X-Vicky-Source"] == "vicky_secom"
        assert cuerpo["source"] == "vicky_redes"

    def test_aislamiento_por_fuente_usa_la_firma_real_no_una_invalida(self, poster):
        _run(poster)
        llamada = poster.llamadas[4]
        assert llamada["headers"]["X-Vicky-Signature"] != ra.FIRMA_INVALIDA

    def test_aislamiento_por_fuente_exige_400_por_inconsistencia(self, poster):
        reporte = _run(poster)
        fuente = next(r for r in reporte["resultados"] if r["check"] == "aislamiento_fuente")
        assert fuente["obtenido"]["status_code"] == 400
        assert fuente["paso"] is True

    def test_aislamiento_por_fuente_no_afirma_probar_isolamiento_de_redes(self, poster):
        """La aclaracion de Work debe quedar escrita en el reporte, no solo
        en un comentario del codigo."""
        reporte = _run(poster)
        fuente = next(r for r in reporte["resultados"] if r["check"] == "aislamiento_fuente")
        assert "no demuestra aislamiento" in fuente["esperado"].lower()

    def test_aislamiento_por_numero_usa_el_phone_number_id_de_redes(self, poster):
        _run(poster)
        cuerpo6 = json.loads(poster.llamadas[5]["data"])
        assert cuerpo6["channel"]["phone_number_id"] == OTRO_PHONE_ID
        # pero la fuente sigue siendo secom -- solo cambia el numero
        assert cuerpo6["source"] == "vicky_secom"

    def test_aislamiento_por_numero_exige_403_exacto(self, poster):
        """Correccion de Work: Radar ya implementa esto de forma explicita,
        asi que el check pasa a exigir 403 exacto, no solo reportar."""
        reporte = _run(poster)
        numero = next(r for r in reporte["resultados"] if r["check"] == "aislamiento_numero")
        assert numero["obtenido"]["status_code"] == 403
        assert numero["paso"] is True

    def test_aislamiento_por_numero_falla_si_no_es_403(self, poster):
        def poster_permisivo(url, data=None, headers=None, timeout=None):
            return FakeResp(200, {"ok": True, "event_id": json.loads(data)["event_id"],
                                   "duplicate": False, "lead_matched": True})

        reporte = _run(poster_permisivo)
        numero = next(r for r in reporte["resultados"] if r["check"] == "aislamiento_numero")
        assert numero["paso"] is False


class TestValidaciones:
    def test_sin_credenciales_no_ejecuta_nada(self, poster):
        with pytest.raises(ValueError):
            ra.run(url="", token="", hmac_secret="", dispatch_token="",
                   phone_number_id=PHONE_ID, known_lead_id=LEAD_ID,
                   known_lead_phone_last10=LEAD_PHONE_LAST10, poster=poster)
        assert poster.llamadas == []

    def test_sin_phone_number_id_no_ejecuta_nada(self, poster):
        with pytest.raises(ValueError):
            ra.run(url="https://radar.test", token="t", hmac_secret="s", dispatch_token="",
                   phone_number_id="", known_lead_id=LEAD_ID,
                   known_lead_phone_last10=LEAD_PHONE_LAST10, poster=poster)
        assert poster.llamadas == []

    def test_sin_telefono_del_lead_conocido_no_ejecuta_nada(self, poster):
        """El hallazgo del 2026-09-10: sin esto, los seis checks fallan por
        formato de telefono en vez de por lo que cada uno prueba. Ahora es
        un requisito duro antes de disparar ninguna peticion."""
        with pytest.raises(ValueError, match="known_lead_phone_last10"):
            ra.run(url="https://radar.test", token="t", hmac_secret="s", dispatch_token="",
                   phone_number_id=PHONE_ID, known_lead_id=LEAD_ID,
                   known_lead_phone_last10="", poster=poster)
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
