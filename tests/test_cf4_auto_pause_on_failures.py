from unittest.mock import Mock, patch

import pytest

import app as vicky


@pytest.fixture(autouse=True)
def reset_failure_counter():
    vicky._consecutive_send_failures = 0
    yield
    vicky._consecutive_send_failures = 0


@pytest.fixture
def auto_send_token():
    with patch.object(vicky, "AUTO_SEND_TOKEN", "auto-secret"):
        yield


@pytest.fixture
def client():
    vicky.app.config["TESTING"] = True
    with vicky.app.test_client() as c:
        yield c


def test_success_resets_counter():
    vicky._consecutive_send_failures = 2
    triggered = vicky._register_send_result(True)
    assert triggered is False
    assert vicky._consecutive_send_failures == 0


def test_failures_below_threshold_do_not_pause():
    with patch.object(vicky, "_set_campaign_paused") as set_paused, \
         patch.object(vicky, "CAMPAIGN_FAILURE_THRESHOLD", 3):
        assert vicky._register_send_result(False) is False
        assert vicky._register_send_result(False) is False
    set_paused.assert_not_called()
    assert vicky._consecutive_send_failures == 2


def test_reaching_threshold_pauses_and_notifies_boardroom():
    with patch.object(vicky, "_set_campaign_paused") as set_paused, \
         patch.object(vicky, "_emit_bus_event") as emit_event, \
         patch.object(vicky, "CAMPAIGN_FAILURE_THRESHOLD", 3):
        assert vicky._register_send_result(False) is False
        assert vicky._register_send_result(False) is False
        triggered = vicky._register_send_result(False)

    assert triggered is True
    set_paused.assert_called_once_with(True)
    emit_event.assert_called_once()
    _, kwargs = emit_event.call_args
    assert kwargs["event_type"] == "campaign_auto_paused"
    assert vicky._consecutive_send_failures == 0


def test_counter_does_not_reset_between_calls_until_threshold_or_success():
    with patch.object(vicky, "_set_campaign_paused"), \
         patch.object(vicky, "_emit_bus_event"), \
         patch.object(vicky, "CAMPAIGN_FAILURE_THRESHOLD", 5):
        vicky._register_send_result(False)
        vicky._register_send_result(False)
        vicky._register_send_result(True)
    assert vicky._consecutive_send_failures == 0


def test_set_campaign_paused_error_does_not_crash_and_does_not_report_triggered():
    with patch.object(vicky, "_set_campaign_paused", side_effect=RuntimeError("sheets down")), \
         patch.object(vicky, "_emit_bus_event") as emit_event, \
         patch.object(vicky, "CAMPAIGN_FAILURE_THRESHOLD", 1):
        triggered = vicky._register_send_result(False)
    assert triggered is False
    emit_event.assert_not_called()


def test_auto_send_one_reports_auto_paused_in_response(client, auto_send_token):
    with patch.object(vicky, "_is_campaign_paused", return_value=False), \
         patch.object(vicky, "_sheet_get_rows", return_value=(["Nombre", "WhatsApp", "ESTATUS"], [["Juan", "5216681234567", "PENDIENTE"]])), \
         patch.object(vicky, "_pick_next_pending", return_value={"whatsapp": "5216681234567", "nombre": "Juan", "row_number": 2}), \
         patch.object(vicky, "_normalize_to_e164_mx", return_value="5216681234567"), \
         patch.object(vicky, "send_template_message", return_value=False), \
         patch.object(vicky, "append_envio_status"), \
         patch.object(vicky, "_update_row_cells"), \
         patch.object(vicky, "_status_for_template", return_value="ENVIADO"), \
         patch.object(vicky, "_set_campaign_paused") as set_paused, \
         patch.object(vicky, "_emit_bus_event"), \
         patch.object(vicky, "CAMPAIGN_FAILURE_THRESHOLD", 1):
        resp = client.post(
            "/ext/auto-send-one",
            json={"template": "promo_vrim"},
            headers={"X-AUTO-TOKEN": "auto-secret"},
        )

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["sent"] is False
    assert body.get("auto_paused") is True
    set_paused.assert_called_once_with(True)
