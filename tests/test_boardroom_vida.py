import logging
from unittest.mock import Mock, patch

import pytest
import requests

import app as vicky


PHONE = "5216681234567"


def _payload(text, msg_id="wamid.test"):
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "from": PHONE,
                                    "id": msg_id,
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }


@pytest.fixture(autouse=True)
def clean_state():
    vicky.user_state.clear()
    vicky.user_data.clear()
    yield
    vicky.user_state.clear()
    vicky.user_data.clear()


@pytest.fixture
def no_boardroom():
    with patch.object(vicky, "BOARDROOM_ENABLED", False):
        yield


@pytest.fixture
def no_external_io(no_boardroom):
    with patch.object(vicky, "send_message", return_value=True) as send_message, \
         patch.object(vicky, "_notify_advisor") as notify, \
         patch.object(vicky, "match_client_in_sheets", return_value=None), \
         patch.object(vicky, "append_respuesta_cliente"):
        yield send_message, notify


def test_option_3_starts_vida_and_does_not_send_menu(no_external_io):
    send_message, _ = no_external_io

    vicky._route_command(PHONE, "3", {"row": 2, "nombre": "Ana"})

    assert vicky.user_state[PHONE] == "vida_edad"
    assert any("¿Cuál es tu edad?" in call.args[1] for call in send_message.call_args_list)
    assert not any(call.args[1] == vicky.MAIN_MENU for call in send_message.call_args_list)


def test_vida_temporal_does_not_send_main_menu(no_external_io):
    send_message, _ = no_external_io

    vicky._route_command(PHONE, "vida temporal", None)

    assert vicky.user_state[PHONE] == "vida_edad"
    assert not any(call.args[1] == vicky.MAIN_MENU for call in send_message.call_args_list)


def test_full_vida_flow_notifies_advisor_with_required_fields(no_external_io):
    send_message, notify = no_external_io
    match = {"row": 2, "nombre": "Ana"}

    vicky.vida_start(PHONE, match)
    vicky._vida_next(PHONE, "45", match)
    vicky._vida_next(PHONE, "no", match)
    vicky._vida_next(PHONE, "Sinaloa", match)
    vicky._vida_next(PHONE, "1 millón", match)
    vicky._vida_next(PHONE, "1", match)

    assert vicky.user_state[PHONE] == "__greeted__"
    advisor_text = notify.call_args.args[0]
    for expected in (
        f"WhatsApp: {PHONE}",
        "Nombre: Ana",
        "Edad: 45",
        "Fuma: no",
        "Estado: Sinaloa",
        "Suma asegurada: 1 millón",
        "Objetivo: Familia",
    ):
        assert expected in advisor_text
    assert any("Ya tengo los datos iniciales" in call.args[1] for call in send_message.call_args_list)
    assert not any(call.args[1] == vicky.MAIN_MENU for call in send_message.call_args_list)


def test_vida_start_attempts_sheet_update_with_product(no_external_io):
    with patch.object(vicky, "_safe_update_row_cells") as update:
        vicky.vida_start(PHONE, {"row": 5, "nombre": "Ana"})

    updates = update.call_args.args[1]
    assert updates["PRODUCTO"] == "vida_temporal"


def test_boardroom_reply_handled_before_local_router():
    response = Mock(status_code=200, text='{"ok": true}')
    response.json.return_value = {"ok": True, "handled": True, "reply": "Respuesta Boardroom"}
    with patch.object(vicky, "BOARDROOM_ENABLED", True), \
         patch.object(vicky, "BOARDROOM_DECISION_URL", "https://boardroom.example.com"), \
         patch.object(vicky, "BOARDROOM_AUTH_TOKEN", "super-secret-token"), \
         patch.object(vicky.requests, "post", return_value=response), \
         patch.object(vicky, "send_message", return_value=True) as send_message, \
         patch.object(vicky, "_route_command") as route, \
         patch.object(vicky, "match_client_in_sheets", return_value=None), \
         patch.object(vicky, "append_respuesta_cliente"):
        rv = vicky.app.test_client().post("/webhook", json=_payload("hola"))

    assert rv.status_code == 200
    send_message.assert_called_once_with(PHONE, "Respuesta Boardroom")
    route.assert_not_called()


@pytest.mark.parametrize("side_effect", [requests.exceptions.Timeout(), Exception("boom")])
def test_boardroom_failure_falls_back_local_and_webhook_200(side_effect):
    with patch.object(vicky, "BOARDROOM_ENABLED", True), \
         patch.object(vicky, "BOARDROOM_DECISION_URL", "https://boardroom.example.com"), \
         patch.object(vicky, "BOARDROOM_AUTH_TOKEN", "super-secret-token"), \
         patch.object(vicky.requests, "post", side_effect=side_effect), \
         patch.object(vicky, "send_message", return_value=True) as send_message, \
         patch.object(vicky, "match_client_in_sheets", return_value=None), \
         patch.object(vicky, "append_respuesta_cliente"):
        rv = vicky.app.test_client().post("/webhook", json=_payload("3"))

    assert rv.status_code == 200
    assert vicky.user_state[PHONE] == "vida_edad"
    assert any("¿Cuál es tu edad?" in call.args[1] for call in send_message.call_args_list)


def test_basic_routes_imss_auto_tpv_empresarial_are_preserved(no_external_io):
    send_message, _ = no_external_io

    vicky._route_command(PHONE, "imss", None)
    assert vicky.user_state[PHONE] == "imss_beneficios"

    vicky.user_state[PHONE] = "__greeted__"
    vicky._route_command(PHONE, "auto", None)
    assert vicky.user_state[PHONE] == "auto_intro"

    vicky.user_state[PHONE] = "__greeted__"
    rv = vicky.app.test_client().post("/webhook", json=_payload("tpv"))
    assert rv.status_code == 200
    assert vicky.user_state[PHONE] == "tpv_giro"

    vicky.user_state[PHONE] = "__greeted__"
    vicky._route_command(PHONE, "empresarial", None)
    assert vicky.user_state[PHONE] == "emp_confirma"
    assert send_message.call_count >= 4


def test_boardroom_auth_token_is_not_logged(caplog):
    token = "super-secret-token"
    caplog.set_level(logging.INFO, logger="vicky-secom")
    with patch.object(vicky, "BOARDROOM_ENABLED", True), \
         patch.object(vicky, "BOARDROOM_DECISION_URL", "https://boardroom.example.com"), \
         patch.object(vicky, "BOARDROOM_AUTH_TOKEN", token), \
         patch.object(vicky.requests, "post", side_effect=requests.exceptions.Timeout()):
        result = vicky.send_to_boardroom(PHONE, "3")

    assert result["reason"] == "timeout"
    assert token not in caplog.text


def test_reply_and_action_present_only_executes_reply(no_external_io):
    send_message, _ = no_external_io
    with patch.object(vicky, "vida_start") as vida_start:
        handled = vicky.execute_boardroom_decision(
            PHONE,
            {
                "ok": True,
                "handled": True,
                "reply": "Solo reply",
                "action": "start_vida_temporal_flow",
                "product": "vida_temporal",
            },
            match=None,
        )

    assert handled is True
    send_message.assert_called_once_with(PHONE, "Solo reply")
    vida_start.assert_not_called()


def test_boardroom_url_normalization_replaces_dead_paths_and_base_host():
    expected = "https://boardroom-engine.onrender.com/api/boardroom/orchestrate"

    assert vicky._normalize_boardroom_url(
        "https://boardroom-engine.onrender.com/boardroom/decision/process"
    ) == expected
    assert vicky._normalize_boardroom_url(
        "https://boardroom-engine.onrender.com/api/decision/process"
    ) == expected
    assert vicky._normalize_boardroom_url(
        "https://boardroom-engine.onrender.com"
    ) == expected


def test_boardroom_reply_executes_even_when_handled_false(no_external_io):
    send_message, _ = no_external_io

    handled = vicky.execute_boardroom_decision(
        PHONE,
        {"ok": True, "handled": False, "reply": "Reply aunque handled false"},
        match=None,
    )

    assert handled is True
    send_message.assert_called_once_with(PHONE, "Reply aunque handled false")


def test_empty_handled_true_does_not_block_fallback(no_external_io):
    handled = vicky.execute_boardroom_decision(PHONE, {"ok": True, "handled": True}, match=None)

    assert handled is False


def test_sheet_update_without_reply_or_action_executes_and_returns_true(no_external_io):
    with patch.object(vicky, "_safe_update_row_cells") as update:
        handled = vicky.execute_boardroom_decision(
            PHONE,
            {"ok": True, "handled": True, "sheet_update": {"ESTATUS": "interesado"}},
            match={"row": 7, "nombre": "Ana"},
        )

    assert handled is True
    update.assert_called_once_with(7, {"ESTATUS": "interesado"}, vicky.VIDA_SHEET_FIELDS)


def test_product_vida_temporal_only_does_not_block_fallback(no_external_io):
    with patch.object(vicky, "_safe_update_row_cells") as update:
        handled = vicky.execute_boardroom_decision(
            PHONE,
            {"ok": True, "handled": True, "product": "vida_temporal"},
            match={"row": 8, "nombre": "Ana"},
        )

    assert handled is False
    update.assert_called_once_with(8, {"PRODUCTO": "vida_temporal"}, vicky.VIDA_SHEET_FIELDS)


def test_vida_objetivo_1_does_not_start_imss(no_external_io):
    send_message, notify = no_external_io
    match = {"row": 2, "nombre": "Ana"}

    vicky.vida_start(PHONE, match)
    vicky._vida_next(PHONE, "45", match)
    vicky._vida_next(PHONE, "no", match)
    vicky._vida_next(PHONE, "Sinaloa", match)
    vicky._vida_next(PHONE, "1000000", match)

    with patch.object(vicky, "imss_start") as imss_start:
        vicky._route_command(PHONE, "1", match)

    imss_start.assert_not_called()
    assert vicky.user_state[PHONE] == "__greeted__"
    assert not any("Préstamo IMSS" in call.args[1] for call in send_message.call_args_list)
    assert not any("Ley 73" in call.args[1] for call in send_message.call_args_list)
    advisor_text = notify.call_args.args[0]
    assert "Objetivo: Familia" in advisor_text


def test_vida_fuma_no_advances_not_global_reject(no_external_io):
    send_message, _ = no_external_io

    vicky.vida_start(PHONE, None)
    vicky._vida_next(PHONE, "35", None)

    vicky._route_command(PHONE, "no", None)

    assert vicky.user_state[PHONE] == "vida_estado"
    assert any("estado" in call.args[1].lower() for call in send_message.call_args_list)


def test_vida_suma_number_advances_to_objetivo(no_external_io):
    send_message, _ = no_external_io

    vicky.vida_start(PHONE, None)
    vicky._vida_next(PHONE, "40", None)
    vicky._vida_next(PHONE, "no", None)
    vicky._vida_next(PHONE, "Sonora", None)

    vicky._route_command(PHONE, "1000000", None)

    assert vicky.user_state[PHONE] == "vida_objetivo"
    assert any("proteger principalmente" in call.args[1].lower() for call in send_message.call_args_list)


def test_imss_active_number_does_not_restart_global(no_external_io):
    send_message, _ = no_external_io

    vicky.user_state[PHONE] = "imss_monto"
    vicky.user_data[PHONE] = {"imss_pension": 8500}

    with patch.object(vicky, "imss_start") as imss_start:
        vicky._route_command(PHONE, "100000", None)

    imss_start.assert_not_called()
    assert vicky.user_state[PHONE] == "imss_nombre"
    assert any("nombre completo" in call.args[1].lower() for call in send_message.call_args_list)


def test_webhook_active_vida_state_skips_boardroom():
    vicky.user_state[PHONE] = "vida_objetivo"
    vicky.user_data[PHONE] = {
        "edad": 45,
        "fuma": "no",
        "estado": "Sinaloa",
        "suma": "1 millón",
        "last_message": "",
    }

    with patch.object(vicky, "send_to_boardroom") as boardroom, \
         patch.object(vicky, "send_message", return_value=True), \
         patch.object(vicky, "_notify_advisor"), \
         patch.object(vicky, "match_client_in_sheets", return_value=None), \
         patch.object(vicky, "append_respuesta_cliente"):
        rv = vicky.app.test_client().post("/webhook", json=_payload("1"))

    assert rv.status_code == 200
    boardroom.assert_not_called()
    assert vicky.user_state[PHONE] == "__greeted__"


def test_webhook_no_active_state_calls_boardroom():
    vicky.user_state[PHONE] = "__greeted__"

    response = Mock(status_code=200, text='{}')
    response.json.return_value = {"ok": False, "handled": False}

    with patch.object(vicky, "BOARDROOM_ENABLED", True), \
         patch.object(vicky, "BOARDROOM_DECISION_URL", "https://boardroom.example.com"), \
         patch.object(vicky, "BOARDROOM_AUTH_TOKEN", "token"), \
         patch.object(vicky.requests, "post", return_value=response) as post_mock, \
         patch.object(vicky, "send_message", return_value=True), \
         patch.object(vicky, "match_client_in_sheets", return_value=None), \
         patch.object(vicky, "append_respuesta_cliente"):
        rv = vicky.app.test_client().post("/webhook", json=_payload("hola"))

    assert rv.status_code == 200
    post_mock.assert_called_once()


def test_bulk_send_worker_rejects_text_without_template():
    item = {
        "to": PHONE,
        "text": "Mensaje libre proactivo",
        "template": "",
        "params": [],
    }

    with patch.object(vicky, "send_message") as send_message, \
         patch.object(vicky, "send_template_message") as send_template, \
         patch.object(vicky, "ADVISOR_NUMBER", ""):
        vicky._bulk_send_worker([item])

    send_message.assert_not_called()
    send_template.assert_not_called()


def test_bulk_send_worker_template_uses_send_template_message():
    item = {
        "to": PHONE,
        "text": "",
        "template": "vida_temporal_apertura_01",
        "params": ["Ana"],
    }

    with patch.object(vicky, "send_message") as send_message, \
         patch.object(vicky, "send_template_message", return_value=True) as send_template, \
         patch.object(vicky, "ADVISOR_NUMBER", ""):
        vicky._bulk_send_worker([item])

    send_message.assert_not_called()
    send_template.assert_called_once_with(PHONE, "vida_temporal_apertura_01", ["Ana"])
