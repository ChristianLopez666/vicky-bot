"""Transformacion del historico de agosto al sobre 1.1.

Los seis fallos de entrega de la campana de agosto no existen en ningun otro
sitio: la hoja los marca como ENVIADO_VIDA_TEMPORAL y Meta los reporto por
webhook, donde el codigo de entonces solo escribia un warning. Estas pruebas
fijan que la transformacion no inventa nada y que se niega antes que rellenar
un hueco.
"""

import pytest

import backfill_historico as bf
import radar_events


PHONE_ID = "1045543821971905"

EXTRACCION = {
    "_meta": {"extraido_el": "2026-09-05T00:30:00Z"},
    "fallos_a_prospecto": [
        {
            "wamid": "wamid.UNO",
            "recipient_id": "5216682492932",
            "phone_last10": "6682492932",
            "meta_timestamp_unix": "1788194410",
            "occurred_at": "2026-08-31T16:40:10.000Z",
            "error_code": 131026,
            "error_title": "Message undeliverable",
        },
        {
            "wamid": "wamid.DOS",
            "recipient_id": "5216682427220",
            "phone_last10": "6682427220",
            "meta_timestamp_unix": "1788200124",
            "occurred_at": "2026-08-31T18:15:24.000Z",
            "error_code": 131049,
            "error_title": "This message was not delivered...",
        },
    ],
    "fallos_a_asesor": [
        {
            "wamid": "wamid.ASESOR",
            "recipient_id": "5216682478005",
            "phone_last10": "6682478005",
            "meta_timestamp_unix": "1788389291",
            "error_code": 131047,
            "error_title": "Re-engagement message",
        },
    ],
}

MAPA = {"6682492932": "SC-uno", "6682427220": "SC-dos"}


def _construir(extraccion=None, mapa=None):
    return bf.construir_fallos_de_prospecto(
        extraccion if extraccion is not None else EXTRACCION,
        mapa if mapa is not None else MAPA,
        phone_number_id=PHONE_ID,
    )


class TestAlcance:
    def test_solo_se_transforman_los_fallos_a_prospecto(self):
        """Decision cerrada: los dos 131047 del asesor no se precargan."""
        eventos = _construir()
        assert len(eventos) == 2
        wamids = {e["message"]["wamid"] for e in eventos}
        assert "wamid.ASESOR" not in wamids

    def test_el_asesor_no_se_cuela_por_ampliar_el_backfill(self):
        """advisor_notified no admite backfill, y el sobre lo rechaza."""
        with pytest.raises(ValueError):
            radar_events.build_event(
                "advisor_notified", lead_id="SC-x", wamid="wamid.ASESOR", backfill=True,
            )


class TestSobre:
    def test_cada_fallo_produce_un_message_failed_de_backfill(self):
        eventos = _construir()
        for e in eventos:
            assert e["event_type"] == "message_failed"
            assert e["backfill"] is True
            assert e["delivery"]["status"] == "failed"
            assert e["contract_version"] == "1.1"
            assert e["source"] == "vicky_secom"
            assert e["channel"]["phone_number_id"] == PHONE_ID
            assert e["message"]["direction"] == "outbound"

    def test_conserva_el_codigo_y_el_titulo_reales_del_log(self):
        uno = _construir()[0]
        assert uno["delivery"]["error_code"] == 131026
        assert uno["delivery"]["error_title"] == "Message undeliverable"

    def test_la_identidad_sale_de_la_hoja_no_se_genera(self):
        eventos = _construir()
        assert [e["lead"]["lead_id"] for e in eventos] == ["SC-uno", "SC-dos"]

    def test_no_rellena_plantilla_ni_campana(self):
        """El log de envio no imprime el wamid: atarlos seria inferir."""
        uno = _construir()[0]
        assert uno["message"]["template"] is None
        assert uno["message"]["campaign"] is None

    def test_la_fecha_es_la_de_meta_y_termina_en_000Z(self):
        eventos = _construir()
        assert eventos[0]["occurred_at"] == "2026-08-31T16:40:10.000Z"
        assert eventos[1]["occurred_at"] == "2026-08-31T18:15:24.000Z"
        assert all(e["occurred_at"].endswith(".000Z") for e in eventos)

    def test_la_fecha_coincide_con_la_que_traia_la_extraccion(self):
        """Se recalcula del unix, no se copia: si difieren, algo esta mal."""
        for original, evento in zip(EXTRACCION["fallos_a_prospecto"], _construir()):
            assert evento["occurred_at"] == original["occurred_at"]


class TestIdentidadDeterminista:
    def test_el_event_id_se_llavea_por_wamid(self):
        uno = _construir()[0]
        assert uno["event_id"] == radar_events.event_id_for("message_failed", "wamid.UNO")

    def test_dos_transformaciones_producen_los_mismos_event_id(self):
        """Recargar el historico no puede duplicarlo en Radar."""
        primeros = [e["event_id"] for e in _construir()]
        segundos = [e["event_id"] for e in _construir()]
        assert primeros == segundos

    def test_los_event_id_no_colisionan_entre_si(self):
        eventos = _construir()
        assert len({e["event_id"] for e in eventos}) == len(eventos)


class TestSeNiegaAntesDeInventar:
    def test_un_telefono_sin_lead_id_conocido_aborta(self):
        with pytest.raises(bf.BackfillIncompleto, match="LEAD_ID"):
            _construir(mapa={"6682492932": "SC-uno"})

    def test_no_genera_un_lead_id_nuevo_para_tapar_el_hueco(self):
        try:
            _construir(mapa={})
        except bf.BackfillIncompleto as exc:
            assert "no se genera uno nuevo" in str(exc)
        else:
            pytest.fail("debio abortar en vez de inventar identidades")

    def test_un_fallo_sin_wamid_aborta(self):
        roto = {"_meta": {}, "fallos_a_prospecto": [
            {"phone_last10": "6682492932", "meta_timestamp_unix": "1788194410", "error_code": 131026},
        ]}
        with pytest.raises(bf.BackfillIncompleto, match="wamid"):
            _construir(extraccion=roto)

    def test_un_fallo_sin_codigo_real_aborta(self):
        roto = {"_meta": {}, "fallos_a_prospecto": [
            {"wamid": "wamid.UNO", "phone_last10": "6682492932",
             "meta_timestamp_unix": "1788194410", "error_code": None},
        ]}
        with pytest.raises(bf.BackfillIncompleto, match="error_code"):
            _construir(extraccion=roto)

    def test_una_extraccion_vacia_aborta(self):
        with pytest.raises(bf.BackfillIncompleto):
            _construir(extraccion={"_meta": {}, "fallos_a_prospecto": []})


class TestExtraccionReal:
    """Contra el archivo de verdad, si esta disponible en esta maquina."""

    RUTA = r"C:\Users\chris\Downloads\historico-secom-fallos-agosto-2026-extraido-2026-09-05.json"

    def test_la_extraccion_real_produce_seis_eventos(self):
        try:
            extraccion = bf.cargar_extraccion(self.RUTA)
        except OSError:
            pytest.skip("la extraccion real no esta en esta maquina")

        mapa = {
            "6682492932": "SC-11eb5e0d-cc84-4e41-852a-9b681d452a0b",
            "6681392074": "SC-aa1bf46b-f9bb-4110-b884-47c9f827c853",
            "6681385567": "SC-9a00700e-7cfb-4635-839a-7ac5ea245c0d",
            "6682427220": "SC-f54e1d3f-0f9c-4243-b8f7-53cd5844d08a",
            "6681965389": "SC-f07fb818-e2fd-4a8b-8227-f8c6805dcde4",
            "6681708810": "SC-d59075d5-7485-4a2d-bbf8-005c163e93d1",
        }
        eventos = bf.construir_fallos_de_prospecto(
            extraccion, mapa, phone_number_id=PHONE_ID
        )

        assert len(eventos) == 6
        codigos = sorted(e["delivery"]["error_code"] for e in eventos)
        assert codigos == [130472, 131026, 131026, 131026, 131049, 131049]
        assert all(e["backfill"] is True for e in eventos)
        assert len({e["event_id"] for e in eventos}) == 6


# ==========================================================================
# Historico de envios: la fuente es la hoja, no los logs
# ==========================================================================
FILAS = [
    {"LEAD_ID": "SC-a", "ESTATUS": "ENVIADO_VIDA_TEMPORAL",
     "LAST_MESSAGE_AT": "2026-08-28 23:10:28", "WhatsApp": "6611107889"},
    {"LEAD_ID": "SC-b", "ESTATUS": "ENVIADO_VIDA_TEMPORAL",
     "LAST_MESSAGE_AT": "2026-08-29 18:15:17", "WhatsApp": "6683224169"},
    {"LEAD_ID": "SC-c", "ESTATUS": "DUDA_TEMPLATE",
     "LAST_MESSAGE_AT": "2026-08-29 18:03:35", "WhatsApp": "6681032235"},
    {"LEAD_ID": "SC-d", "ESTATUS": "NO_INTERESADO_TEMPLATE",
     "LAST_MESSAGE_AT": "2026-08-29 19:39:56", "WhatsApp": "6681255067"},
    {"LEAD_ID": "SC-e", "ESTATUS": "", "LAST_MESSAGE_AT": "", "WhatsApp": ""},
]


def _envios(filas=None):
    return bf.construir_envios_de_hoja(
        filas if filas is not None else FILAS, phone_number_id=PHONE_ID
    )


class TestEnviosHistoricos:
    def test_solo_las_filas_realmente_enviadas_producen_evento(self):
        eventos = _envios()
        assert [e["lead"]["lead_id"] for e in eventos] == ["SC-a", "SC-b"]

    def test_una_fila_cuya_fecha_se_sobrescribio_al_responder_se_excluye(self):
        """DUDA_TEMPLATE y NO_INTERESADO_TEMPLATE llevan la fecha de la
        respuesta, no la del envio. Fecharlas mal seria peor que omitirlas."""
        eventos = _envios()
        ids = {e["lead"]["lead_id"] for e in eventos}
        assert "SC-c" not in ids and "SC-d" not in ids

    def test_el_sobre_es_un_message_sent_de_backfill_sin_wamid(self):
        uno = _envios()[0]
        assert uno["event_type"] == "message_sent"
        assert uno["backfill"] is True
        assert uno["delivery"]["status"] == "sent"
        assert uno["message"]["wamid"] is None
        assert uno["message"]["request_id"] is None
        assert uno["message"]["template"] is None

    def test_la_fecha_de_la_hoja_se_normaliza_a_000Z(self):
        eventos = _envios()
        assert eventos[0]["occurred_at"] == "2026-08-28T23:10:28.000Z"
        assert all(e["occurred_at"].endswith(".000Z") for e in eventos)

    def test_la_llave_es_lead_id_mas_fecha(self):
        uno = _envios()[0]
        esperado = radar_events.event_id_for(
            "message_sent", "SC-a:2026-08-28T23:10:28.000Z"
        )
        assert uno["event_id"] == esperado

    def test_recargar_el_historico_produce_los_mismos_event_id(self):
        assert [e["event_id"] for e in _envios()] == [e["event_id"] for e in _envios()]

    def test_una_fila_enviada_sin_lead_id_aborta(self):
        with pytest.raises(bf.BackfillIncompleto, match="LEAD_ID"):
            _envios([{"LEAD_ID": "", "ESTATUS": "ENVIADO_VIDA_TEMPORAL",
                      "LAST_MESSAGE_AT": "2026-08-28 23:10:28", "WhatsApp": "6611107889"}])

    def test_una_fecha_ilegible_aborta_en_vez_de_inventarse(self):
        with pytest.raises(bf.BackfillIncompleto):
            _envios([{"LEAD_ID": "SC-a", "ESTATUS": "ENVIADO_VIDA_TEMPORAL",
                      "LAST_MESSAGE_AT": "el jueves", "WhatsApp": "6611107889"}])

    def test_dos_filas_con_la_misma_llave_abortan(self):
        repetida = dict(FILAS[0])
        with pytest.raises(bf.BackfillIncompleto, match="repetida"):
            _envios([FILAS[0], repetida])

    def test_sin_filas_enviadas_aborta(self):
        with pytest.raises(bf.BackfillIncompleto, match="ninguna fila"):
            _envios([FILAS[2], FILAS[4]])


class TestLotes:
    def test_parte_en_lotes_de_como_mucho_cincuenta(self):
        eventos = [{"event_id": str(i)} for i in range(140)]
        lotes = bf.en_lotes(eventos)
        assert [len(l["events"]) for l in lotes] == [50, 50, 40]

    def test_no_pierde_ni_repite_ningun_evento(self):
        eventos = [{"event_id": str(i)} for i in range(140)]
        planos = [e for l in bf.en_lotes(eventos) for e in l["events"]]
        assert planos == eventos

    def test_rechaza_un_tamano_fuera_del_contrato(self):
        with pytest.raises(ValueError):
            bf.en_lotes([{"event_id": "1"}], tamano=51)
