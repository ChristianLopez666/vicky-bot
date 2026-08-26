from unittest.mock import Mock, patch

import pytest

import app as vicky


@pytest.fixture
def client():
    vicky.app.config["TESTING"] = True
    with vicky.app.test_client() as c:
        yield c


@pytest.fixture
def auto_send_token():
    with patch.object(vicky, "AUTO_SEND_TOKEN", "auto-secret"):
        yield


@pytest.fixture
def bus_token():
    with patch.object(vicky, "BUS_INTERNAL_TOKEN", "bus-secret"):
        yield


def test_boardroom_instruct_requires_token(client, bus_token):
    resp = client.post("/ext/boardroom/instruct", json={"instruction": "pause_outbound"})
    assert resp.status_code == 401


def test_boardroom_instruct_rejects_wrong_token(client, bus_token):
    resp = client.post(
        "/ext/boardroom/instruct",
        json={"instruction": "pause_outbound"},
        headers={"X-Internal-Token": "wrong"},
    )
    assert resp.status_code == 401


def test_boardroom_instruct_pause_outbound_calls_set_paused(client, bus_token):
    with patch.object(vicky, "_set_campaign_paused") as set_paused:
        resp = client.post(
            "/ext/boardroom/instruct",
            json={"instruction": "pause_outbound"},
            headers={"X-Internal-Token": "bus-secret"},
        )
    assert resp.status_code == 200
    assert resp.get_json()["paused"] is True
    set_paused.assert_called_once_with(True)


def test_boardroom_instruct_resume_outbound_calls_set_paused(client, bus_token):
    with patch.object(vicky, "_set_campaign_paused") as set_paused:
        resp = client.post(
            "/ext/boardroom/instruct",
            json={"instruction": "resume_outbound"},
            headers={"X-Internal-Token": "bus-secret"},
        )
    assert resp.status_code == 200
    assert resp.get_json()["paused"] is False
    set_paused.assert_called_once_with(False)


def test_boardroom_instruct_unknown_instruction(client, bus_token):
    resp = client.post(
        "/ext/boardroom/instruct",
        json={"instruction": "not_a_real_instruction"},
        headers={"X-Internal-Token": "bus-secret"},
    )
    assert resp.status_code == 400


def test_boardroom_instruct_500_when_sheets_write_fails(client, bus_token):
    with patch.object(vicky, "_set_campaign_paused", side_effect=RuntimeError("boom")):
        resp = client.post(
            "/ext/boardroom/instruct",
            json={"instruction": "pause_outbound"},
            headers={"X-Internal-Token": "bus-secret"},
        )
    assert resp.status_code == 500


def test_auto_send_one_skips_when_paused(client, auto_send_token):
    with patch.object(vicky, "_is_campaign_paused", return_value=True), \
         patch.object(vicky, "_sheet_get_rows") as sheet_get_rows:
        resp = client.post(
            "/ext/auto-send-one",
            json={"template": "promo_vrim"},
            headers={"X-AUTO-TOKEN": "auto-secret"},
        )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["sent"] is False
    assert body["reason"] == "paused_by_boardroom"
    sheet_get_rows.assert_not_called()


def test_auto_send_one_proceeds_when_not_paused(client, auto_send_token):
    with patch.object(vicky, "_is_campaign_paused", return_value=False), \
         patch.object(vicky, "_sheet_get_rows", return_value=([], [])):
        resp = client.post(
            "/ext/auto-send-one",
            json={"template": "promo_vrim"},
            headers={"X-AUTO-TOKEN": "auto-secret"},
        )
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "Sheet vacío"


def test_is_campaign_paused_reads_pause_cell():
    fake_values_get = Mock()
    fake_values_get.return_value.execute.return_value = {"values": [["PAUSED"]]}
    fake_spreadsheets = Mock()
    fake_spreadsheets.return_value.values.return_value.get = fake_values_get
    with patch.object(vicky, "google_ready", True), \
         patch.object(vicky, "sheets_svc", Mock(spreadsheets=fake_spreadsheets)), \
         patch.object(vicky, "SHEETS_ID_LEADS", "sheet-id"), \
         patch.object(vicky, "_ensure_control_tab"), \
         patch.object(vicky, "SHEETS_TITLE_LEADS", "Prospectos SECOM Auto"):
        assert vicky._is_campaign_paused() is True


def test_is_campaign_paused_false_when_cell_empty():
    fake_values_get = Mock()
    fake_values_get.return_value.execute.return_value = {"values": []}
    fake_spreadsheets = Mock()
    fake_spreadsheets.return_value.values.return_value.get = fake_values_get
    with patch.object(vicky, "google_ready", True), \
         patch.object(vicky, "sheets_svc", Mock(spreadsheets=fake_spreadsheets)), \
         patch.object(vicky, "SHEETS_ID_LEADS", "sheet-id"), \
         patch.object(vicky, "_ensure_control_tab"), \
         patch.object(vicky, "SHEETS_TITLE_LEADS", "Prospectos SECOM Auto"):
        assert vicky._is_campaign_paused() is False


def test_is_campaign_paused_fails_open_on_sheets_error():
    fake_values_get = Mock()
    fake_values_get.return_value.execute.side_effect = RuntimeError("api down")
    fake_spreadsheets = Mock()
    fake_spreadsheets.return_value.values.return_value.get = fake_values_get
    with patch.object(vicky, "google_ready", True), \
         patch.object(vicky, "sheets_svc", Mock(spreadsheets=fake_spreadsheets)), \
         patch.object(vicky, "SHEETS_ID_LEADS", "sheet-id"), \
         patch.object(vicky, "_ensure_control_tab"), \
         patch.object(vicky, "SHEETS_TITLE_LEADS", "Prospectos SECOM Auto"):
        assert vicky._is_campaign_paused() is False


def test_is_campaign_paused_false_when_sheets_not_configured():
    with patch.object(vicky, "google_ready", False):
        assert vicky._is_campaign_paused() is False

# --- Regresion: el kill switch vivia en "<hoja de leads>!AA1"; esa celda no
# existe en una hoja de 16 columnas, asi que cada lectura daba HTTP 400
# ("exceeds grid limits") -> fail-open permanente y auto-pausa muerta.
# Ahora vive en su propia pestana, que no depende del ancho de los datos. ---


def test_pause_cell_is_not_outside_the_data_grid():
    assert vicky.CAMPAIGN_PAUSE_CELL == "A2"
    assert vicky.CAMPAIGN_CONTROL_TAB
    assert "AA" not in vicky.CAMPAIGN_PAUSE_CELL


def test_is_campaign_paused_reads_from_control_tab_not_leads_sheet():
    fake_values_get = Mock()
    fake_values_get.return_value.execute.return_value = {"values": [["PAUSED"]]}
    fake_spreadsheets = Mock()
    fake_spreadsheets.return_value.values.return_value.get = fake_values_get
    with (
        patch.object(vicky, "google_ready", True),
        patch.object(vicky, "sheets_svc", Mock(spreadsheets=fake_spreadsheets)),
        patch.object(vicky, "SHEETS_ID_LEADS", "sheet-id"),
        patch.object(vicky, "_ensure_control_tab"),
        patch.object(vicky, "CAMPAIGN_CONTROL_TAB", "CONTROL"),
        patch.object(vicky, "SHEETS_TITLE_LEADS", "Hoja1"),
    ):
        assert vicky._is_campaign_paused() is True

    rango = fake_values_get.call_args.kwargs["range"]
    assert rango == "CONTROL!A2"
    assert "Hoja1" not in rango


def test_ensure_control_tab_creates_it_when_missing():
    fake_ss = Mock()
    fake_ss.return_value.get.return_value.execute.return_value = {
        "sheets": [{"properties": {"title": "Hoja1"}}]
    }
    with (
        patch.object(vicky, "sheets_svc", Mock(spreadsheets=fake_ss)),
        patch.object(vicky, "SHEETS_ID_LEADS", "sheet-id"),
        patch.object(vicky, "CAMPAIGN_CONTROL_TAB", "CONTROL"),
        patch.object(vicky, "_control_tab_ready", False),
    ):
        vicky._ensure_control_tab()

    cuerpo = fake_ss.return_value.batchUpdate.call_args.kwargs["body"]
    assert cuerpo["requests"][0]["addSheet"]["properties"]["title"] == "CONTROL"


def test_ensure_control_tab_does_not_recreate_existing_tab():
    fake_ss = Mock()
    fake_ss.return_value.get.return_value.execute.return_value = {
        "sheets": [{"properties": {"title": "Hoja1"}},
                   {"properties": {"title": "CONTROL"}}]
    }
    with (
        patch.object(vicky, "sheets_svc", Mock(spreadsheets=fake_ss)),
        patch.object(vicky, "SHEETS_ID_LEADS", "sheet-id"),
        patch.object(vicky, "CAMPAIGN_CONTROL_TAB", "CONTROL"),
        patch.object(vicky, "_control_tab_ready", False),
    ):
        vicky._ensure_control_tab()

    fake_ss.return_value.batchUpdate.assert_not_called()


def test_set_campaign_paused_writes_to_control_tab():
    fake_update = Mock()
    fake_ss = Mock()
    fake_ss.return_value.values.return_value.update = fake_update
    with (
        patch.object(vicky, "google_ready", True),
        patch.object(vicky, "sheets_svc", Mock(spreadsheets=fake_ss)),
        patch.object(vicky, "SHEETS_ID_LEADS", "sheet-id"),
        patch.object(vicky, "_ensure_control_tab"),
        patch.object(vicky, "CAMPAIGN_CONTROL_TAB", "CONTROL"),
    ):
        vicky._set_campaign_paused(True)

    kwargs = fake_update.call_args.kwargs
    assert kwargs["range"] == "CONTROL!A2"
    assert kwargs["body"]["values"] == [["PAUSED"]]


def test_ensure_tab_pide_una_cuadricula_explicita_y_pequena():
    """Regresion: con el tamano por defecto (1000x26) addSheet daba HTTP 400
    porque el libro ya roza el tope de 10 millones de celdas de Google."""
    fake_ss = Mock()
    fake_ss.return_value.get.return_value.execute.return_value = {"sheets": []}
    with (
        patch.object(vicky, "sheets_svc", Mock(spreadsheets=fake_ss)),
        patch.object(vicky, "SHEETS_ID_LEADS", "sheet-id"),
    ):
        vicky._ensure_tab("CONTROL", ["A", "B"], filas=10)

    props = (fake_ss.return_value.batchUpdate.call_args.kwargs["body"]
             ["requests"][0]["addSheet"]["properties"])
    grid = props["gridProperties"]
    assert grid["rowCount"] == 10
    assert grid["columnCount"] == 2
    assert grid["rowCount"] * grid["columnCount"] < 26000
