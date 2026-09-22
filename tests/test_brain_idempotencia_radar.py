"""Lo que pidio la auditoria de Work del 15-sep en el lado de SECOM: que una
instruccion reintentada no se ejecute dos veces, que Radar vea lo que contesta
el cerebro, y que nadie se quede esperando en silencio."""
from unittest.mock import patch

import app as vicky

PHONE = "5216681234567"
TOKEN = "brain-secret"


def _instruction_request(body, token=TOKEN):
    return vicky.app.test_client().post(
        "/ext/boardroom/instruction", json=body, headers={"Authorization": f"Bearer {token}"}
    )


def _brain_on():
    vicky._brain_done.clear()
    vicky._brain_waiting.clear()
    return (
        patch.object(vicky, "BRAIN_CALLBACK_TOKEN", TOKEN),
        patch.object(vicky, "BRAIN_PHONES", {PHONE[-10:]}),
    )


def _reply_body(**extra):
    body = {
        "phone": PHONE,
        "status": "ok",
        "instruction": {"type": "ask_question", "message": "¿Qué edad tienes?"},
        "advisor_notification": {"required": False},
        "instruction_id": "iid-1",
        "event_id": "evt-1",
        "decision": "reply",
        "confidence": 0.9,
        "fields": {"vida.edad": 48},
    }
    body.update(extra)
    return body


# ── Idempotencia ──────────────────────────────────────────────────────────────

def test_la_misma_instruccion_no_se_envia_dos_veces():
    a, b = _brain_on()
    with a, b, patch.object(vicky, "send_message", return_value=True) as send, \
         patch.object(vicky, "_record_brain_radar_event"):
        primera = _instruction_request(_reply_body())
        segunda = _instruction_request(_reply_body())

    assert send.call_count == 1
    assert primera.get_json()["executed"] is True
    assert segunda.status_code == 200
    assert segunda.get_json()["duplicate"] is True
    assert segunda.get_json()["executed"] is True


def test_dos_turnos_distintos_si_se_envian():
    a, b = _brain_on()
    with a, b, patch.object(vicky, "send_message", return_value=True) as send, \
         patch.object(vicky, "_record_brain_radar_event"):
        _instruction_request(_reply_body())
        _instruction_request(_reply_body(instruction_id="iid-2", event_id="evt-2"))

    assert send.call_count == 2


def test_sin_instruction_id_se_deduplica_por_event_id():
    a, b = _brain_on()
    body = _reply_body()
    body.pop("instruction_id")
    with a, b, patch.object(vicky, "send_message", return_value=True) as send, \
         patch.object(vicky, "_record_brain_radar_event"):
        _instruction_request(body)
        repetida = _instruction_request(body)

    assert send.call_count == 1
    assert repetida.get_json()["duplicate"] is True


def test_un_fallback_repetido_tampoco_avisa_dos_veces():
    a, b = _brain_on()
    body = {"phone": PHONE, "status": "fallback", "text": "¿cuánto cuesta?",
            "instruction_id": "iid-f", "event_id": "evt-f"}
    with a, b, patch.object(vicky, "send_message", return_value=True) as send, \
         patch.object(vicky, "_notify_advisor") as notify:
        _instruction_request(body)
        repetida = _instruction_request(body)

    assert notify.call_count == 1 and send.call_count == 1
    assert repetida.get_json()["duplicate"] is True


# ── Radar ────────────────────────────────────────────────────────────────────

def test_radar_recibe_la_respuesta_del_cerebro_con_su_decision():
    a, b = _brain_on()
    with a, b, patch.object(vicky, "send_message", return_value={"ok": True, "wamid": "wamid.99"}), \
         patch.object(vicky, "_lead_identity_for_phone",
                      return_value={"lead_id": "LEAD-1", "nombre": "Ana", "phone_last10": PHONE[-10:]}), \
         patch.object(vicky, "record_radar_event") as radar:
        _instruction_request(_reply_body())
        vicky._brain_radar_flush()

    kwargs = radar.call_args.kwargs
    assert kwargs["event_type"] == "message_sent"
    assert kwargs["lead_id"] == "LEAD-1"
    assert kwargs["wamid"] == "wamid.99"
    assert kwargs["delivery_status"] == "sent"
    assert kwargs["text"] == "¿Qué edad tienes?"
    assert kwargs["trace"]["origen"] == "cerebro_boardroom"
    assert kwargs["trace"]["decision"] == "reply"
    assert kwargs["trace"]["datos"] == {"vida.edad": 48}


def test_radar_registra_el_envio_fallido_del_cerebro():
    a, b = _brain_on()
    with a, b, patch.object(vicky, "send_message", return_value={"ok": False, "motivo": "131049"}), \
         patch.object(vicky, "_lead_identity_for_phone",
                      return_value={"lead_id": "LEAD-1", "nombre": "Ana", "phone_last10": PHONE[-10:]}), \
         patch.object(vicky, "record_radar_event") as radar:
        rv = _instruction_request(_reply_body())
        vicky._brain_radar_flush()

    tipos = [call.kwargs["event_type"] for call in radar.call_args_list]
    assert "message_failed" in tipos
    assert rv.get_json()["executed"] is False


def test_el_aviso_a_christian_tambien_queda_en_radar():
    a, b = _brain_on()
    body = _reply_body(instruction={"type": "handoff", "message": "Te escribe Christian."},
                       advisor_notification={"required": True, "message": "🧠 Lead de Vida", "to": "christian"},
                       decision="escalate",
                       authority={"escalation_reason": "money_or_client_impact"})
    with a, b, patch.object(vicky, "send_message", return_value={"ok": True, "wamid": "wamid.1"}), \
         patch.object(vicky, "_notify_advisor", return_value={"ok": True, "motivo": "", "request_id": "r-advisor", "wamid": "wamid.advisor"}), \
         patch.object(vicky, "_lead_identity_for_phone",
                      return_value={"lead_id": "LEAD-1", "nombre": "Ana", "phone_last10": PHONE[-10:]}), \
         patch.object(vicky, "record_radar_event") as radar:
        _instruction_request(body)
        vicky._brain_radar_flush()

    tipos = [call.kwargs["event_type"] for call in radar.call_args_list]
    assert tipos == ["advisor_notified", "message_sent"]
    aviso = radar.call_args_list[0].kwargs
    assert aviso["trace"]["motivo"] == "money_or_client_impact"
    assert aviso["advisor_notification"] == {
        "advisor_phone_e164": "5216682478005",
        "result": "sent",
        "wamid": "wamid.advisor",
        "error": None,
    }


def test_si_radar_falla_la_respuesta_al_prospecto_sigue_saliendo():
    a, b = _brain_on()
    with a, b, patch.object(vicky, "send_message", return_value={"ok": True, "wamid": "w"}) as send, \
         patch.object(vicky, "_lead_identity_for_phone", side_effect=RuntimeError("hoja caida")):
        rv = _instruction_request(_reply_body())
        vicky._brain_radar_flush()

    assert send.call_count == 1
    assert rv.get_json()["executed"] is True


# ── Nadie se queda esperando en silencio ─────────────────────────────────────

def test_si_el_cerebro_no_contesta_christian_se_entera():
    vicky._brain_waiting.clear()
    with patch.object(vicky, "_notify_advisor") as notify:
        vicky._brain_waiting["evt-9"] = {"phone": PHONE, "text": "¿me cubre?"}
        vicky._brain_wait_expired("evt-9")

    assert "¿me cubre?" in notify.call_args.args[0]
    assert vicky._brain_waiting == {}


def test_si_la_respuesta_llega_a_tiempo_no_se_avisa_nada():
    vicky._brain_waiting.clear()
    vicky._brain_waiting["evt-10"] = {"phone": PHONE, "text": "hola"}
    vicky._brain_wait_done("evt-10")

    with patch.object(vicky, "_notify_advisor") as notify:
        vicky._brain_wait_expired("evt-10")

    notify.assert_not_called()


def test_la_instruccion_cierra_la_vigilancia_del_turno():
    a, b = _brain_on()
    vicky._brain_waiting["evt-1"] = {"phone": PHONE, "text": "hola"}
    with a, b, patch.object(vicky, "send_message", return_value=True), \
         patch.object(vicky, "_record_brain_radar_event"):
        _instruction_request(_reply_body())

    assert "evt-1" not in vicky._brain_waiting
