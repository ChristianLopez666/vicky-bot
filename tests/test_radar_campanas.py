"""Campanas lanzadas desde Radar Comercial.

Radar encola personas con su campana; el cron de siempre las envia, cada una
con la plantilla de SU campana. Lo que ya funcionaba (plantilla fija en el
comando del cron) sigue igual para las filas sin campana.

Sin red, sin Google Sheets, sin Meta: todo mockeado. Datos inventados.
"""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

import app as vicky
import radar_campanas as rc

LEAD_A = "SC-11111111-1111-4111-8111-111111111111"
LEAD_B = "SC-22222222-2222-4222-8222-222222222222"
LEAD_C = "SC-33333333-3333-4333-8333-333333333333"
AHORA = datetime(2026, 10, 16, 18, 0, tzinfo=timezone.utc)

H = ["Nombre", "WhatsApp", "ESTATUS", "LAST_MESSAGE_AT", "LEAD_ID", "retry_at", "CAMPANA_RADAR"]

CAMPANA_VIDA = {
    "id": "vida-oct", "nombre": "Vida octubre", "template": "vida_temporal", "language": "es_MX",
    "image_url": "https://example.test/vida.jpg", "params_from_row": {"nombre": "Nombre"},
    "success_status": "ENVIADO_VIDA_TEMPORAL", "activa": True,
}


# --------------------------------------------------------------------------
# Plan de encolado (puro)
# --------------------------------------------------------------------------

def test_persona_nueva_se_agrega_con_su_campana_y_lead():
    res, upd, nuevas = rc.planear_encolado(H, [], "vida-oct", [{"telefono": "6680000001", "nombre": "ANA", "lead_id": LEAD_A}], AHORA)
    assert res == [{"telefono": "6680000001", "lead_id": LEAD_A, "accion": "nuevo", "motivo": ""}]
    assert upd == {}
    assert nuevas == [{"Nombre": "ANA", "WhatsApp": "6680000001", "ESTATUS": "PENDIENTE", "LEAD_ID": LEAD_A, "CAMPANA_RADAR": "vida-oct"}]


def test_persona_existente_reusa_su_fila_y_respeta_su_lead_id():
    viejo = (AHORA - timedelta(days=40)).replace(tzinfo=None).isoformat()
    rows = [["ANA", "5216680000001", "ENVIADO_SEGURO_AUTO", viejo, LEAD_B, "", ""]]
    res, upd, nuevas = rc.planear_encolado(H, rows, "vida-oct", [{"telefono": "668 000 0001", "lead_id": LEAD_A}], AHORA)
    assert nuevas == []
    assert upd == {2: {"CAMPANA_RADAR": "vida-oct", "ESTATUS": "PENDIENTE", "LAST_MESSAGE_AT": "", "retry_at": ""}}
    assert res[0] == {"telefono": "6680000001", "lead_id": LEAD_B, "accion": "reencolado", "motivo": ""}


def test_fila_existente_sin_lead_id_recibe_el_de_radar():
    rows = [["ANA", "6680000001", "", "", "", "", ""]]
    res, upd, _ = rc.planear_encolado(H, rows, "vida-oct", [{"telefono": "6680000001", "lead_id": LEAD_A}], AHORA)
    assert upd[2]["LEAD_ID"] == LEAD_A
    assert res[0]["lead_id"] == LEAD_A


def test_no_se_toca_a_quien_recibio_mensaje_hace_poco_ni_a_quien_se_esta_enviando():
    reciente = (AHORA - timedelta(hours=10)).replace(tzinfo=None).isoformat()
    rows = [
        ["A", "6680000001", "ENVIADO_SEGURO_AUTO", reciente, LEAD_A, "", ""],
        ["B", "6680000002", "ENVIANDO", "x", LEAD_B, "", ""],
        ["C", "6680000003", "PENDIENTE", "", LEAD_C, "", "otra-campana"],
    ]
    items = [{"telefono": t, "lead_id": LEAD_A} for t in ("6680000001", "6680000002", "6680000003")]
    res, upd, nuevas = rc.planear_encolado(H, rows, "vida-oct", items, AHORA)
    assert [r["motivo"] for r in res] == ["contactado_recientemente", "enviandose", "ya_en_cola"]
    assert upd == {} and nuevas == []


def test_pendiente_manual_sin_campana_pasa_a_la_campana_de_radar():
    rows = [["A", "6680000001", "PENDIENTE", "", "", "", ""]]
    res, upd, _ = rc.planear_encolado(H, rows, "vida-oct", [{"telefono": "6680000001", "lead_id": LEAD_A}], AHORA)
    assert res[0]["accion"] == "reencolado"
    assert upd[2]["CAMPANA_RADAR"] == "vida-oct"


def test_datos_invalidos_y_repetidos_se_omiten():
    items = [
        {"telefono": "123", "lead_id": LEAD_A},
        {"telefono": "6680000009", "lead_id": "no-es-lead"},
        {"telefono": "6680000008", "lead_id": LEAD_A},
        {"telefono": "6680000008", "lead_id": LEAD_B},
    ]
    res, _, nuevas = rc.planear_encolado(H, [], "vida-oct", items, AHORA)
    assert [r["motivo"] for r in res] == ["telefono_invalido", "lead_id_invalido", "", "repetido_en_lote"]
    assert len(nuevas) == 1


def test_validar_campana():
    ok = rc.validar_campana({**CAMPANA_VIDA})
    assert ok["template"] == "vida_temporal" and ok["activa"] is True
    for malo in ({"id": "Mayus"}, {"template": "Con Espacio"}, {"success_status": "minus"}, {"image_url": "http://x"}, {"params_from_row": {}}):
        with pytest.raises(rc.CampanaInvalida):
            rc.validar_campana({**CAMPANA_VIDA, **malo})


def test_leer_campanas_ignora_filas_rotas():
    filas = [
        rc.fila_de_campana(rc.validar_campana(CAMPANA_VIDA), "t"),
        ["rota", "x", "tpl", "es_MX", "", "{no es json", "ST", "SI"],
        ["", "sin id"],
    ]
    campanas = rc.leer_campanas(filas)
    assert list(campanas) == ["vida-oct"]
    assert campanas["vida-oct"]["params_from_row"] == {"nombre": "Nombre"}


# --------------------------------------------------------------------------
# Cron: /ext/auto-send-one
# --------------------------------------------------------------------------

class FakeResp:
    status_code = 200
    text = json.dumps({"messages": [{"id": "wamid.TEST"}]})

    def json(self):
        return {"messages": [{"id": "wamid.TEST"}]}


@pytest.fixture
def entorno():
    posts, updates, eventos = [], [], []
    estado = {"rows": [], "campanas": {"vida-oct": dict(CAMPANA_VIDA)}}

    def fake_post(url, headers=None, json=None, timeout=None):
        posts.append(json)
        return FakeResp()

    vicky.app.config["TESTING"] = True
    with patch.object(vicky, "AUTO_SEND_TOKEN", "auto-secret"), \
            patch.object(vicky, "META_TOKEN", "t"), \
            patch.object(vicky, "WPP_API_URL", "https://graph.test/v20.0/1/messages"), \
            patch.object(vicky, "_is_campaign_paused", return_value=False), \
            patch.object(vicky, "_sheet_get_rows", side_effect=lambda: (list(H), [list(r) for r in estado["rows"]])), \
            patch.object(vicky, "_campanas_leer", side_effect=lambda: estado["campanas"]), \
            patch.object(vicky, "_update_row_cells", side_effect=lambda rn, u, h: updates.append((rn, u))), \
            patch.object(vicky, "_seal_lead_id", side_effect=lambda rn, h, row: row[4] or "SC-nuevo"), \
            patch.object(vicky, "record_radar_event", side_effect=lambda **kw: eventos.append(kw)), \
            patch.object(vicky, "append_envio_status"), \
            patch.object(vicky, "_register_send_result", return_value=False), \
            patch.object(vicky.requests, "post", side_effect=fake_post):
        with vicky.app.test_client() as c:
            yield {"c": c, "posts": posts, "updates": updates, "eventos": eventos, "estado": estado}


LEGACY = {"template": "seguro_auto_70", "language": "es_MX", "image_url": "https://example.test/auto.jpg",
          "params_from_row": {"nombre": "Nombre"}, "success_status": "ENVIADO_SEGURO_AUTO"}


def _cron(c, body):
    return c.post("/ext/auto-send-one", json=body, headers={"X-AUTO-TOKEN": "auto-secret"})


def test_modo_radar_envia_la_plantilla_de_la_campana_de_la_fila(entorno):
    entorno["estado"]["rows"] = [
        ["MANUAL", "6680000010", "", "", "", "", ""],
        ["ANA", "6680000011", "PENDIENTE", "", LEAD_A, "", "vida-oct"],
    ]
    r = _cron(entorno["c"], {"modo": "campanas_radar", **LEGACY})
    d = r.get_json()
    assert r.status_code == 200 and d["sent"] is True
    assert d["campana"] == "vida-oct" and d["row"] == 3 and d["lead_id"] == LEAD_A
    assert entorno["posts"][0]["template"]["name"] == "vida_temporal"
    assert entorno["updates"][-1] == (3, {"ESTATUS": "ENVIADO_VIDA_TEMPORAL", "LAST_MESSAGE_AT": entorno["updates"][-1][1]["LAST_MESSAGE_AT"]})
    assert all(e["campaign"] == "vida-oct" for e in entorno["eventos"])


def test_sin_filas_de_radar_sigue_la_campana_de_siempre(entorno):
    entorno["estado"]["rows"] = [["MANUAL", "6680000010", "", "", "", "", ""]]
    d = _cron(entorno["c"], {"modo": "campanas_radar", **LEGACY}).get_json()
    assert d["sent"] is True and "campana" not in d and d["row"] == 2
    assert entorno["posts"][0]["template"]["name"] == "seguro_auto_70"


def test_campana_pausada_no_se_envia_y_su_fila_no_la_toma_el_camino_de_siempre(entorno):
    entorno["estado"]["campanas"]["vida-oct"]["activa"] = False
    entorno["estado"]["rows"] = [["ANA", "6680000011", "PENDIENTE", "", LEAD_A, "", "vida-oct"]]
    d = _cron(entorno["c"], {"modo": "campanas_radar", **LEGACY}).get_json()
    assert d == {"ok": True, "sent": False, "reason": "no_pending"}
    assert entorno["posts"] == []


def test_modo_radar_sin_plantilla_de_respaldo_y_sin_filas(entorno):
    d = _cron(entorno["c"], {"modo": "campanas_radar"}).get_json()
    assert d == {"ok": True, "sent": False, "reason": "no_pending"}


def test_comando_de_siempre_no_toca_filas_de_radar(entorno):
    entorno["estado"]["rows"] = [
        ["ANA", "6680000011", "PENDIENTE", "", LEAD_A, "", "vida-oct"],
        ["MANUAL", "6680000010", "", "", "", "", ""],
    ]
    d = _cron(entorno["c"], LEGACY).get_json()
    assert d["row"] == 3 and entorno["posts"][0]["template"]["name"] == "seguro_auto_70"


def test_fila_con_reintento_futuro_espera_su_turno(entorno):
    futuro = (datetime.utcnow() + timedelta(hours=2)).isoformat()
    entorno["estado"]["rows"] = [["ANA", "6680000011", "PENDIENTE", "", LEAD_A, futuro, "vida-oct"]]
    d = _cron(entorno["c"], {"modo": "campanas_radar"}).get_json()
    assert d["reason"] == "no_pending"


# --------------------------------------------------------------------------
# Rutas nuevas: /ext/radar/campana y /ext/radar/encolar
# --------------------------------------------------------------------------

@pytest.fixture
def rutas():
    llamadas = MagicMock()
    estado = {"campanas": {"vida-oct": dict(CAMPANA_VIDA)}, "headers": list(H), "rows": []}
    vicky.app.config["TESTING"] = True
    with patch.object(vicky, "RADAR_QUEUE_TOKEN", "queue-secret"), \
            patch.object(vicky, "_campanas_leer", side_effect=lambda: estado["campanas"]), \
            patch.object(vicky, "_campana_guardar") as guardar, \
            patch.object(vicky, "_sheet_get_rows", side_effect=lambda: (estado["headers"], estado["rows"])), \
            patch.object(vicky, "_sheets_values", return_value=llamadas), \
            patch.object(vicky, "_sheet_rows_invalidate"):
        with vicky.app.test_client() as c:
            yield {"c": c, "llamadas": llamadas, "guardar": guardar, "estado": estado}


def _post(c, ruta, body, token="queue-secret"):
    return c.post(ruta, json=body, headers={"X-RADAR-QUEUE-TOKEN": token})


def test_rutas_exigen_su_token(rutas):
    assert _post(rutas["c"], "/ext/radar/encolar", {}, token="otro").status_code == 401
    assert _post(rutas["c"], "/ext/radar/campana", {}, token="").status_code == 401
    with patch.object(vicky, "RADAR_QUEUE_TOKEN", ""):
        assert _post(rutas["c"], "/ext/radar/encolar", {}, token="").status_code == 401


def test_alta_de_campana_valida_y_guarda(rutas):
    assert _post(rutas["c"], "/ext/radar/campana", {**CAMPANA_VIDA, "template": "Mala"}).status_code == 400
    r = _post(rutas["c"], "/ext/radar/campana", CAMPANA_VIDA)
    assert r.status_code == 200
    assert rutas["guardar"].call_args[0][0]["template"] == "vida_temporal"


def test_encolar_campana_desconocida_o_pausada(rutas):
    item = [{"telefono": "6680000001", "lead_id": LEAD_A}]
    assert _post(rutas["c"], "/ext/radar/encolar", {"campana_id": "nada", "items": item}).status_code == 404
    rutas["estado"]["campanas"]["vida-oct"]["activa"] = False
    assert _post(rutas["c"], "/ext/radar/encolar", {"campana_id": "vida-oct", "items": item}).status_code == 409


def test_encolar_escribe_filas_nuevas_y_actualiza_existentes(rutas):
    rutas["estado"]["rows"] = [["VIEJA", "6680000002", "ENVIADO_SEGURO_AUTO", "2026-08-01T10:00:00", LEAD_B, "", ""]]
    r = _post(rutas["c"], "/ext/radar/encolar", {"campana_id": "vida-oct", "items": [
        {"telefono": "6680000001", "nombre": "NUEVA", "lead_id": LEAD_A},
        {"telefono": "6680000002", "nombre": "VIEJA", "lead_id": LEAD_C},
    ]})
    d = r.get_json()
    assert r.status_code == 200 and d["encolados"] == 2 and d["omitidos"] == 0
    assert [x["lead_id"] for x in d["resultados"]] == [LEAD_A, LEAD_B]
    append = rutas["llamadas"].append.call_args.kwargs
    assert append["body"]["values"] == [["NUEVA", "6680000001", "PENDIENTE", "", LEAD_A, "", "vida-oct"]]
    rangos = {x["range"]: x["values"][0][0] for x in rutas["llamadas"].batchUpdate.call_args.kwargs["body"]["data"]}
    assert rangos[f"{vicky.SHEETS_TITLE_LEADS}!G2"] == "vida-oct"
    assert rangos[f"{vicky.SHEETS_TITLE_LEADS}!C2"] == "PENDIENTE"


def test_encolar_agrega_la_columna_si_falta_y_frena_si_no_cabe(rutas):
    rutas["estado"]["headers"] = H[:-1]
    r = _post(rutas["c"], "/ext/radar/encolar", {"campana_id": "vida-oct", "items": [{"telefono": "6680000001", "lead_id": LEAD_A}]})
    assert r.status_code == 200
    rango = rutas["llamadas"].update.call_args.kwargs["range"]
    assert rango == f"{vicky.SHEETS_TITLE_LEADS}!G1"
    rutas["estado"]["headers"] = [f"C{i}" for i in range(24)] + ["WhatsApp", "ESTATUS"]
    r = _post(rutas["c"], "/ext/radar/encolar", {"campana_id": "vida-oct", "items": [{"telefono": "6680000001", "lead_id": LEAD_A}]})
    assert r.status_code == 503 and "26 columnas" in r.get_json()["error"]
