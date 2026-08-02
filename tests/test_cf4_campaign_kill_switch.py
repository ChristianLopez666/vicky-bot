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
         patch.object(vicky, "SHEETS_TITLE_LEADS", "Prospectos SECOM Auto"):
        assert vicky._is_campaign_paused() is False


def test_is_campaign_paused_false_when_sheets_not_configured():
    with patch.object(vicky, "google_ready", False):
        assert vicky._is_campaign_paused() is False
