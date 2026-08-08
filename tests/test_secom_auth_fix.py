"""SECOM-AUTH-FIX-1 (DOC-0043, hydra-source-of-truth).

Cubre el flag SECOM_LOCAL_FALLBACK_ENABLED: modo legacy (default, sin
cambios de comportamiento) vs modo fix (Boardroom clasificado por
`status`, funnels activos continuan localmente sin bloquear en Boardroom
por turno). No prueba nueva logica comercial -- solo que las piezas ya
existentes (_route_command, _handle_media, _handle_awaiting_template_response)
vuelven a ser alcanzables segun las reglas de DOC-0043.
"""

from unittest.mock import Mock, patch

import pytest
import requests

import app as vicky


PHONE = "5216681234567"


def _payload(text, mtype="text", msg_id="wamid.test", button_text=None):
    if mtype == "button":
        message = {"from": PHONE, "id": msg_id, "type": "button", "button": {"text": button_text or text}}
    elif mtype in {"image", "document", "audio", "video"}:
        message = {"from": PHONE, "id": msg_id, "type": mtype, mtype: {"id": "media-1"}}
    else:
        message = {"from": PHONE, "id": msg_id, "type": "text", "text": {"body": text}}
    return {"entry": [{"changes": [{"value": {"messages": [message]}}]}]}


def _ok_response(message="Respuesta Boardroom", instruction_type="send_message", **extra_instruction):
    resp = Mock(status_code=200, text='{"status": "ok"}')
    instruction = {"type": instruction_type}
    if message is not None:
        instruction["message"] = message
    instruction.update(extra_instruction)
    resp.json.return_value = {
        "status": "ok",
        "instruction_id": "instr-1",
        "event_id": "evt-1",
        "instruction": instruction,
        "advisor_notification": {"required": False},
    }
    return resp


def _fallback_response():
    resp = Mock(status_code=200, text='{"status": "fallback"}')
    resp.json.return_value = {
        "status": "fallback",
        "instruction_id": "instr-2",
        "event_id": "evt-2",
        "instruction": {"type": "send_message", "message": vicky.NEUTRAL_FALLBACK_MESSAGE},
        "advisor_notification": {"required": False},
        "audit": {"decision_reason": "phase_1_safe_response_no_commercial_decision", "confidence": 0.0},
    }
    return resp


def _error_response():
    resp = Mock(status_code=200, text='{"status": "error"}')
    resp.json.return_value = {"status": "error", "instruction_id": "instr-3", "event_id": "evt-3"}
    return resp


@pytest.fixture(autouse=True)
def clean_state():
    vicky.user_state.clear()
    vicky.user_data.clear()
    yield
    vicky.user_state.clear()
    vicky.user_data.clear()


@pytest.fixture
def no_external_io():
    with patch.object(vicky, "send_message", return_value=True) as send_message, \
         patch.object(vicky, "_notify_advisor") as notify, \
         patch.object(vicky, "match_client_in_sheets", return_value=None), \
         patch.object(vicky, "append_respuesta_cliente"):
        yield send_message, notify


@pytest.fixture
def bus_configured():
    with patch.object(vicky, "BUS_URL", "https://boardroom.example.com"), \
         patch.object(vicky, "BUS_INTERNAL_TOKEN", "super-secret-token"), \
         patch.object(vicky, "_BUS_ACTIVE", True):
        yield


# ==========================
# 1-2. Feature flag: legacy vs fix
# ==========================

def test_missing_flag_env_defaults_to_false(monkeypatch):
    monkeypatch.delenv("SECOM_LOCAL_FALLBACK_ENABLED", raising=False)
    import importlib
    reloaded = importlib.reload(vicky)
    assert reloaded.SECOM_LOCAL_FALLBACK_ENABLED is False
    importlib.reload(vicky)  # restaurar estado de modulo para el resto de la suite


@pytest.mark.parametrize("raw_value", ["", "nope", "2", "  ", "TrueFalse"])
def test_invalid_flag_value_treated_as_false(monkeypatch, raw_value):
    monkeypatch.setenv("SECOM_LOCAL_FALLBACK_ENABLED", raw_value)
    import importlib
    reloaded = importlib.reload(vicky)
    assert reloaded.SECOM_LOCAL_FALLBACK_ENABLED is False
    monkeypatch.delenv("SECOM_LOCAL_FALLBACK_ENABLED", raising=False)
    importlib.reload(vicky)


def test_legacy_mode_status_fallback_still_executes_as_handled(no_external_io, bus_configured):
    """Documenta el bug original: en modo legacy (flag=false), un
    status:fallback se ejecuta igual que un status:ok -- exactamente lo
    que SECOM-AUTH-FIX-1 corrige cuando el flag esta en true (ver test
    siguiente). No es un comportamiento deseado; es el contrato legacy
    que este fix debe poder desactivar via rollback sin sorpresas."""
    send_message, _ = no_external_io
    with patch.object(vicky.requests, "post", return_value=_fallback_response()):
        rv = vicky.app.test_client().post("/webhook", json=_payload("hola"))

    assert rv.status_code == 200
    send_message.assert_called_once_with(PHONE, vicky.NEUTRAL_FALLBACK_MESSAGE)
    assert PHONE not in vicky.user_state


def test_fix_mode_status_fallback_triggers_local_technical_fallback(no_external_io, bus_configured):
    send_message, _ = no_external_io
    with patch.object(vicky, "SECOM_LOCAL_FALLBACK_ENABLED", True), \
         patch.object(vicky.requests, "post", return_value=_fallback_response()):
        rv = vicky.app.test_client().post("/webhook", json=_payload("imss"))

    assert rv.status_code == 200
    assert vicky.user_state[PHONE] == "imss_beneficios"


def test_fix_mode_status_error_triggers_local_technical_fallback(no_external_io, bus_configured):
    with patch.object(vicky, "SECOM_LOCAL_FALLBACK_ENABLED", True), \
         patch.object(vicky.requests, "post", return_value=_error_response()):
        rv = vicky.app.test_client().post("/webhook", json=_payload("vida"))

    assert rv.status_code == 200
    assert vicky.user_state[PHONE] == "vida_edad"


def test_fix_mode_malformed_json_triggers_local_technical_fallback(no_external_io, bus_configured):
    resp = Mock(status_code=200, text="not json")
    resp.json.side_effect = ValueError("invalid json")
    with patch.object(vicky, "SECOM_LOCAL_FALLBACK_ENABLED", True), \
         patch.object(vicky.requests, "post", return_value=resp):
        rv = vicky.app.test_client().post("/webhook", json=_payload("imss"))

    assert rv.status_code == 200
    assert vicky.user_state[PHONE] == "imss_beneficios"


def test_fix_mode_invalid_status_value_triggers_local_technical_fallback(no_external_io, bus_configured):
    resp = Mock(status_code=200, text='{"status": "weird"}')
    resp.json.return_value = {"status": "weird", "instruction": {"type": "send_message", "message": "x"}}
    with patch.object(vicky, "SECOM_LOCAL_FALLBACK_ENABLED", True), \
         patch.object(vicky.requests, "post", return_value=resp):
        rv = vicky.app.test_client().post("/webhook", json=_payload("imss"))

    assert rv.status_code == 200
    assert vicky.user_state[PHONE] == "imss_beneficios"


def test_fix_mode_invalid_instruction_type_triggers_local_technical_fallback(no_external_io, bus_configured):
    resp = Mock(status_code=200, text='{"status": "ok"}')
    resp.json.return_value = {"status": "ok", "instruction": {"type": "not_a_real_type", "message": "x"}}
    with patch.object(vicky, "SECOM_LOCAL_FALLBACK_ENABLED", True), \
         patch.object(vicky.requests, "post", return_value=resp):
        rv = vicky.app.test_client().post("/webhook", json=_payload("imss"))

    assert rv.status_code == 200
    assert vicky.user_state[PHONE] == "imss_beneficios"


def test_fix_mode_status_ok_takes_precedence_over_local_routing(no_external_io, bus_configured):
    """DOC-0043 regla 4: una decision de Boardroom con status:ok tiene
    precedencia sobre cualquier fallback local para ese turno."""
    send_message, _ = no_external_io
    with patch.object(vicky, "SECOM_LOCAL_FALLBACK_ENABLED", True), \
         patch.object(vicky.requests, "post", return_value=_ok_response("Decision real de Boardroom")):
        rv = vicky.app.test_client().post("/webhook", json=_payload("imss"))

    assert rv.status_code == 200
    send_message.assert_called_once_with(PHONE, "Decision real de Boardroom")
    assert PHONE not in vicky.user_state  # imss_start jamas se ejecuto


# ==========================
# no_action semantics (SAF.6)
# ==========================

def test_no_action_with_status_ok_is_handled_silently(no_external_io, bus_configured):
    """status:ok + no_action = HANDLED: Boardroom decidio deliberadamente
    no responder. No debe disparar fallback ni enviar ningun mensaje."""
    send_message, _ = no_external_io
    resp = Mock(status_code=200, text='{"status": "ok"}')
    resp.json.return_value = {
        "status": "ok",
        "instruction_id": "i1",
        "instruction": {"type": "no_action"},
        "advisor_notification": {"required": False},
    }
    with patch.object(vicky, "SECOM_LOCAL_FALLBACK_ENABLED", True), \
         patch.object(vicky.requests, "post", return_value=resp):
        rv = vicky.app.test_client().post("/webhook", json=_payload("hola"))

    assert rv.status_code == 200
    send_message.assert_not_called()


def test_no_action_with_status_fallback_is_not_handled(no_external_io, bus_configured):
    """status:fallback + no_action: el status manda, no el instruction
    type -- NOT_HANDLED, debe caer a fallback tecnico local."""
    resp = Mock(status_code=200, text='{"status": "fallback"}')
    resp.json.return_value = {
        "status": "fallback",
        "instruction_id": "i2",
        "instruction": {"type": "no_action"},
        "advisor_notification": {"required": False},
    }
    with patch.object(vicky, "SECOM_LOCAL_FALLBACK_ENABLED", True), \
         patch.object(vicky.requests, "post", return_value=resp):
        rv = vicky.app.test_client().post("/webhook", json=_payload("imss"))

    assert rv.status_code == 200
    assert vicky.user_state[PHONE] == "imss_beneficios"


# ==========================
# Active funnel: no bloquea en Boardroom por turno
# ==========================

def test_active_funnel_turn_does_not_depend_on_boardroom(no_external_io, bus_configured):
    # La observacion a /bus/event (_emit_boardroom_observation) es
    # fire-and-forget en un thread aparte -- si el turno del funnel
    # dependiera sincronamente de Boardroom, esta llamada fallida
    # rompería el request. Al fallar y el funnel avanzar igual, se
    # demuestra que no hay dependencia sincrona.
    vicky.user_state[PHONE] = "vida_edad"
    with patch.object(vicky, "SECOM_LOCAL_FALLBACK_ENABLED", True), \
         patch.object(vicky.requests, "post", side_effect=RuntimeError("bus caido")):
        rv = vicky.app.test_client().post("/webhook", json=_payload("45"))

    assert rv.status_code == 200
    assert vicky.user_state[PHONE] == "vida_fuma"


def test_legacy_mode_active_funnel_still_blocked(no_external_io, bus_configured):
    """Con el flag en false, el comportamiento historico se conserva: el
    gate intercepta incluso con funnel activo."""
    send_message, _ = no_external_io
    vicky.user_state[PHONE] = "vida_edad"
    with patch.object(vicky.requests, "post", return_value=_fallback_response()):
        rv = vicky.app.test_client().post("/webhook", json=_payload("45"))

    assert rv.status_code == 200
    assert vicky.user_state[PHONE] == "vida_edad"  # no avanzo
    send_message.assert_called_once_with(PHONE, vicky.NEUTRAL_FALLBACK_MESSAGE)


# ==========================
# Cancel / reset / menu (SAF.17)
# ==========================

@pytest.mark.parametrize("escape_word", ["menu", "menú", "cancelar", "salir", "inicio"])
def test_global_escape_command_exits_active_funnel(no_external_io, bus_configured, escape_word):
    vicky.user_state[PHONE] = "imss_pension"
    with patch.object(vicky, "SECOM_LOCAL_FALLBACK_ENABLED", True), \
         patch.object(vicky.requests, "post", side_effect=RuntimeError("bus caido")):
        rv = vicky.app.test_client().post("/webhook", json=_payload(escape_word))

    assert rv.status_code == 200
    assert vicky.user_state[PHONE] == "__greeted__"


def test_advisor_handoff_explicit_request_notifies_once(no_external_io, bus_configured):
    # Estado ya "__greeted__" (contacto no nuevo) para aislar la notificacion
    # de asesor del mensaje de bienvenida que _greet_and_match dispara para
    # un contacto sin match en Sheets -- ese es un flujo aparte, no una
    # doble notificacion introducida por este fix.
    vicky.user_state[PHONE] = "__greeted__"
    send_message, notify = no_external_io
    with patch.object(vicky, "SECOM_LOCAL_FALLBACK_ENABLED", True), \
         patch.object(vicky.requests, "post", return_value=_fallback_response()):
        rv = vicky.app.test_client().post("/webhook", json=_payload("asesor"))

    assert rv.status_code == 200
    # _notify_advisor exactamente una vez (INV-11) -- _route_command SI
    # envia dos mensajes al cliente por diseno ya existente (confirmacion
    # + menu), eso no es una doble notificacion al asesor.
    notify.assert_called_once()
    assert send_message.call_count == 2


# ==========================
# No duplicidad (INV-09, INV-11)
# ==========================

def test_handled_path_never_calls_local_route_command(no_external_io, bus_configured):
    with patch.object(vicky, "SECOM_LOCAL_FALLBACK_ENABLED", True), \
         patch.object(vicky, "_route_command") as route, \
         patch.object(vicky.requests, "post", return_value=_ok_response("Decision Boardroom")):
        rv = vicky.app.test_client().post("/webhook", json=_payload("imss"))

    assert rv.status_code == 200
    route.assert_not_called()


def test_not_handled_path_never_double_sends(no_external_io, bus_configured):
    # Estado ya "__greeted__" para aislar el envio del fallback tecnico
    # (arranque de imss_start) del mensaje de bienvenida de _greet_and_match.
    vicky.user_state[PHONE] = "__greeted__"
    send_message, _ = no_external_io
    with patch.object(vicky, "SECOM_LOCAL_FALLBACK_ENABLED", True), \
         patch.object(vicky.requests, "post", return_value=_fallback_response()):
        rv = vicky.app.test_client().post("/webhook", json=_payload("imss"))

    assert rv.status_code == 200
    assert send_message.call_count == 1


# ==========================
# Ambiguedad no selecciona producto en silencio (INV-04)
# ==========================

def test_ambiguous_text_does_not_auto_select_product(no_external_io, bus_configured):
    with patch.object(vicky, "SECOM_LOCAL_FALLBACK_ENABLED", True), \
         patch.object(vicky.requests, "post", return_value=_fallback_response()):
        rv = vicky.app.test_client().post("/webhook", json=_payload("necesito ayuda con algo"))

    assert rv.status_code == 200
    # Ningun funnel de producto arranco -- solo el catch-all generico de
    # _route_command (o __greeted__ si nunca existio estado).
    assert not vicky.user_state.get(PHONE, "").startswith(vicky.ACTIVE_FUNNEL_PREFIXES)


# ==========================
# Media / button branches
# ==========================

def test_media_during_active_funnel_forwards_without_boardroom_call(no_external_io, bus_configured):
    with patch.object(vicky, "_download_media", return_value=(None, None, None)):
        vicky.user_state[PHONE] = "auto_intro"
        with patch.object(vicky, "SECOM_LOCAL_FALLBACK_ENABLED", True), \
             patch.object(vicky.requests, "post", side_effect=RuntimeError("bus caido")):
            rv = vicky.app.test_client().post("/webhook", json=_payload("", mtype="image"))

    assert rv.status_code == 200
    assert vicky.user_state[PHONE] == "auto_intro"  # media no altera el estado del funnel


def test_button_stateless_not_handled_falls_back_to_route_command(no_external_io, bus_configured):
    with patch.object(vicky, "SECOM_LOCAL_FALLBACK_ENABLED", True), \
         patch.object(vicky.requests, "post", return_value=_fallback_response()):
        rv = vicky.app.test_client().post(
            "/webhook", json=_payload("imss", mtype="button", button_text="imss")
        )

    assert rv.status_code == 200
    assert vicky.user_state[PHONE] == "imss_beneficios"


# ==========================
# SAF.23 -- regresion de funnels bajo active-funnel bypass
# ==========================

@pytest.mark.parametrize(
    "state,answer,expected_next_state",
    [
        ("vida_edad", "45", "vida_fuma"),
        ("imss_beneficios", "sí", "imss_pension"),
        ("auto_intro", "no", "auto_vencimiento_fecha"),
        ("tpv_giro", "restaurante", "tpv_horario"),
        ("emp_confirma", "sí", "emp_giro"),
        ("fp_q1", "respuesta libre", "fp_q2"),
    ],
)
def test_funnel_continues_one_deterministic_step_without_boardroom(
    no_external_io, bus_configured, state, answer, expected_next_state
):
    vicky.user_state[PHONE] = state
    with patch.object(vicky, "SECOM_LOCAL_FALLBACK_ENABLED", True), \
         patch.object(vicky.requests, "post", side_effect=RuntimeError("bus caido")):
        rv = vicky.app.test_client().post("/webhook", json=_payload(answer))

    assert rv.status_code == 200
    assert vicky.user_state[PHONE] == expected_next_state
