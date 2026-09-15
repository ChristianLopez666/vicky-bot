from unittest.mock import Mock, patch

import app as vicky

PHONE = "5216681234567"
TOKEN = "brain-secret"


def _response(status_code, body):
    resp = Mock(status_code=status_code, text="x")
    resp.json.return_value = body
    return resp


def _bus():
    return (
        patch.object(vicky, "_BUS_ACTIVE", True),
        patch.object(vicky, "BUS_URL", "https://boardroom.test"),
        patch.object(vicky, "BUS_INTERNAL_TOKEN", "bus-token"),
    )


def test_el_turno_se_entrega_al_cerebro_sin_esperar_su_respuesta():
    vicky._brain_seen_ids.clear()
    a, b, c = _bus()
    with a, b, c, patch.object(vicky.requests, "post", return_value=_response(202, {"status": "accepted"})) as post:
        handed = vicky._handoff_turn_to_brain(PHONE, {"id": "wamid.1"}, None, "text", "vida")

    assert handed is True
    payload = post.call_args.kwargs["json"]
    assert payload["brain"] == {"mode": "async"}
    assert payload["channel"] == "vicky_secom"
    assert payload["text"] == "vida"
    assert post.call_args.kwargs["timeout"] == 3


def test_un_reenvio_de_meta_no_se_entrega_dos_veces():
    vicky._brain_seen_ids.clear()
    a, b, c = _bus()
    with a, b, c, patch.object(vicky.requests, "post", return_value=_response(202, {"status": "accepted"})) as post:
        assert vicky._handoff_turn_to_brain(PHONE, {"id": "wamid.2"}, None, "text", "vida")
        assert vicky._handoff_turn_to_brain(PHONE, {"id": "wamid.2"}, None, "text", "vida")

    assert post.call_count == 1


def test_si_boardroom_no_acepta_atiende_el_guion():
    vicky._brain_seen_ids.clear()
    a, b, c = _bus()
    with a, b, c, patch.object(vicky.requests, "post", return_value=_response(200, {"status": "fallback"})):
        assert vicky._handoff_turn_to_brain(PHONE, {"id": "wamid.3"}, None, "text", "vida") is False

    with a, b, c, patch.object(vicky.requests, "post", side_effect=vicky.requests.exceptions.Timeout()):
        assert vicky._handoff_turn_to_brain(PHONE, {"id": "wamid.4"}, None, "text", "vida") is False


def _webhook_payload(text):
    message = {"from": PHONE, "id": "wamid.hook", "type": "text", "text": {"body": text}}
    return {"entry": [{"changes": [{"value": {"messages": [message]}}]}]}


def test_numero_autorizado_va_al_cerebro_y_no_al_guion():
    with patch.object(vicky, "BRAIN_PHONES", {PHONE[-10:]}), \
         patch.object(vicky, "BRAIN_CALLBACK_TOKEN", TOKEN), \
         patch.object(vicky, "_handoff_turn_to_brain", return_value=True) as handoff, \
         patch.object(vicky, "_consult_boardroom") as consult, \
         patch.object(vicky, "_route_command") as route:
        rv = vicky.app.test_client().post("/webhook", json=_webhook_payload("vida"))

    assert rv.status_code == 200
    handoff.assert_called_once()
    consult.assert_not_called()
    route.assert_not_called()


def test_numero_no_autorizado_sigue_exactamente_igual():
    with patch.object(vicky, "BRAIN_PHONES", set()), \
         patch.object(vicky, "BRAIN_CALLBACK_TOKEN", TOKEN), \
         patch.object(vicky, "_handoff_turn_to_brain") as handoff:
        vicky.app.test_client().post("/webhook", json=_webhook_payload("vida"))

    handoff.assert_not_called()


def _instruction_request(body, token=TOKEN):
    return vicky.app.test_client().post(
        "/ext/boardroom/instruction", json=body, headers={"Authorization": f"Bearer {token}"}
    )


def test_la_instruccion_exige_el_secreto_propio():
    with patch.object(vicky, "BRAIN_CALLBACK_TOKEN", ""):
        assert _instruction_request({"phone": PHONE}).status_code == 401
    with patch.object(vicky, "BRAIN_CALLBACK_TOKEN", TOKEN):
        assert _instruction_request({"phone": PHONE}, token="otro").status_code == 401


def test_la_instruccion_solo_aplica_a_numeros_autorizados():
    with patch.object(vicky, "BRAIN_CALLBACK_TOKEN", TOKEN), patch.object(vicky, "BRAIN_PHONES", set()):
        assert _instruction_request({"phone": PHONE, "status": "ok"}).status_code == 403


def test_una_decision_del_cerebro_se_ejecuta_y_avisa_a_christian():
    body = {
        "phone": PHONE,
        "status": "ok",
        "instruction": {"type": "handoff", "message": "Te escribe Christian en breve."},
        "advisor_notification": {"required": True, "message": "🧠 Lead de Vida para ti"},
    }
    with patch.object(vicky, "BRAIN_CALLBACK_TOKEN", TOKEN), \
         patch.object(vicky, "BRAIN_PHONES", {PHONE[-10:]}), \
         patch.object(vicky, "send_message", return_value=True) as send, \
         patch.object(vicky, "_notify_advisor") as notify:
        rv = _instruction_request(body)

    assert rv.status_code == 200 and rv.get_json()["executed"] is True
    notify.assert_called_once_with("🧠 Lead de Vida para ti")
    send.assert_called_once_with(PHONE, "Te escribe Christian en breve.")


def test_si_el_cerebro_falla_christian_atiende_y_el_cliente_no_queda_sin_respuesta():
    body = {"phone": PHONE, "status": "fallback", "text": "¿cuánto cuesta?", "error": "TimeoutError"}
    with patch.object(vicky, "BRAIN_CALLBACK_TOKEN", TOKEN), \
         patch.object(vicky, "BRAIN_PHONES", {PHONE[-10:]}), \
         patch.object(vicky, "send_message", return_value=True) as send, \
         patch.object(vicky, "_notify_advisor") as notify:
        rv = _instruction_request(body)

    assert rv.get_json()["fallback"] is True
    assert "¿cuánto cuesta?" in notify.call_args.args[0]
    send.assert_called_once_with(PHONE, vicky.NEUTRAL_FALLBACK_MESSAGE)
