"""Punto 2 (el aviso al asesor dice la verdad) y punto 4 (el acuse de Meta manda
sobre la hoja) del plan del 17-sep, con sus banderas de reversion."""
from unittest.mock import patch

import pytest

import app as vicky
import radar_events

PHONE = "5216681234567"
TOKEN = "brain-secret"


# ── Punto 2: el aviso al asesor ──────────────────────────────────────────────

def test_el_aviso_devuelve_lo_que_realmente_paso():
    with patch.object(vicky, "ADVISOR_NUMBER", "5216682478005"), \
         patch.object(vicky, "send_message", return_value={"ok": True, "wamid": "wamid.7"}), \
         patch.object(vicky, "_log_conversacion"):
        resultado = vicky._notify_advisor("🧠 Lead de Vida para ti")

    assert resultado["ok"] is True
    assert resultado["wamid"] == "wamid.7"
    assert resultado["request_id"]


def test_un_aviso_rechazado_por_meta_no_se_da_por_bueno():
    with patch.object(vicky, "ADVISOR_NUMBER", "5216682478005"), \
         patch.object(vicky, "send_message", return_value={"ok": False, "motivo": "131047"}), \
         patch.object(vicky, "_log_conversacion"), \
         patch.object(vicky, "_registrar_alerta_pendiente") as alerta:
        resultado = vicky._notify_advisor("🧠 Lead de Vida para ti")

    assert resultado["ok"] is False
    assert resultado["motivo"] == "131047"
    alerta.assert_called_once()


def test_si_hay_plantilla_aprobada_se_usa_cuando_el_texto_libre_no_pasa():
    with patch.object(vicky, "ADVISOR_NUMBER", "5216682478005"), \
         patch.object(vicky, "ADVISOR_ALERT_TEMPLATE", "aviso_asesor"), \
         patch.object(vicky, "send_message", return_value={"ok": False, "motivo": "131047"}), \
         patch.object(vicky, "send_template_message", return_value={"ok": True, "wamid": "wamid.tpl"}) as tpl, \
         patch.object(vicky, "_log_conversacion"):
        resultado = vicky._notify_advisor("🧠 Lead de Vida para ti")

    assert resultado["ok"] is True and resultado["wamid"] == "wamid.tpl"
    assert tpl.call_args.args[1] == "aviso_asesor"


def _instruction_request(body, token=TOKEN):
    return vicky.app.test_client().post(
        "/ext/boardroom/instruction", json=body, headers={"Authorization": f"Bearer {token}"}
    )


def _brain_on():
    vicky._brain_done.clear()
    vicky._brain_waiting.clear()
    return (patch.object(vicky, "BRAIN_CALLBACK_TOKEN", TOKEN),
            patch.object(vicky, "BRAIN_PHONES", {PHONE[-10:]}))


def _forward_body(**extra):
    body = {"phone": PHONE, "status": "ok", "instruction_id": "iid-a", "event_id": "evt-a",
            "decision": "forward_to_advisor",
            "instruction": {"type": "notify_advisor", "message": "📩 Yomero volvió a escribir: menu"},
            "advisor_notification": {"required": False}}
    body.update(extra)
    return body


def test_no_se_confirma_ejecucion_de_un_aviso_que_meta_rechazo():
    a, b = _brain_on()
    with a, b, patch.object(vicky, "_notify_advisor", return_value={"ok": False, "motivo": "131047", "request_id": "r1", "wamid": ""}), \
         patch.object(vicky, "_record_brain_radar_event"), \
         patch.object(vicky, "send_message", return_value=True):
        respuesta = _instruction_request(_forward_body())

    cuerpo = respuesta.get_json()
    assert cuerpo["executed"] is False
    assert cuerpo["error"] == "advisor_not_delivered"


def test_con_la_bandera_apagada_vuelve_el_comportamiento_anterior(monkeypatch):
    monkeypatch.setenv("ADVISOR_ALERT_STRICT", "0")
    a, b = _brain_on()
    with a, b, patch.object(vicky, "_notify_advisor", return_value={"ok": False, "motivo": "131047", "request_id": "r1", "wamid": ""}), \
         patch.object(vicky, "_record_brain_radar_event"), \
         patch.object(vicky, "send_message", return_value=True):
        respuesta = _instruction_request(_forward_body())

    assert respuesta.get_json()["executed"] is True


def test_el_evento_advisor_notified_ya_se_puede_construir():
    # Antes iba sin wamid ni request_id: la llave del contrato quedaba vacia y
    # event_id_for reventaba, asi que el aviso nunca llegaba a Radar.
    with pytest.raises(ValueError):
        radar_events.build_event("advisor_notified", lead_id="LEAD-1")

    evento = radar_events.build_event("advisor_notified", lead_id="LEAD-1",
                                      request_id="7f0f6f6e-2f7a-4a6a-9c1e-2c9a1f0b1a11")
    assert evento["event_id"]


def test_radar_recibe_el_aviso_con_su_request_id_y_su_resultado():
    a, b = _brain_on()
    with a, b, patch.object(vicky, "_notify_advisor", return_value={"ok": False, "motivo": "131047", "request_id": "r-9", "wamid": ""}), \
         patch.object(vicky, "_lead_identity_for_phone",
                      return_value={"lead_id": "LEAD-1", "nombre": "Ana", "phone_last10": PHONE[-10:]}), \
         patch.object(vicky, "record_radar_event") as radar, \
         patch.object(vicky, "send_message", return_value=True):
        _instruction_request(_forward_body())
        vicky._brain_radar_flush()

    kwargs = radar.call_args.kwargs
    assert kwargs["event_type"] == "advisor_notified"
    assert kwargs["request_id"] == "r-9"
    assert kwargs["delivery_status"] == "failed"
    assert kwargs["error_title"] == "131047"


def test_una_respuesta_entregada_al_prospecto_no_se_marca_como_fallida():
    a, b = _brain_on()
    body = {"phone": PHONE, "status": "ok", "instruction_id": "iid-b", "event_id": "evt-b",
            "instruction": {"type": "ask_question", "message": "¿Qué edad tienes?"},
            "advisor_notification": {"required": True, "message": "🧠 Lead de Vida"}}
    with a, b, patch.object(vicky, "_notify_advisor", return_value={"ok": False, "motivo": "131047", "request_id": "r2", "wamid": ""}), \
         patch.object(vicky, "_record_brain_radar_event"), \
         patch.object(vicky, "send_message", return_value={"ok": True, "wamid": "w"}):
        cuerpo = _instruction_request(body).get_json()

    assert cuerpo["executed"] is True
    assert cuerpo["error"] == "advisor_not_delivered"


# ── Punto 4: el acuse de Meta manda sobre la hoja ────────────────────────────

CABECERAS = ["Nombre", "WhatsApp", "ESTATUS", "LAST_MESSAGE_AT", "NEXT_ACTION", "retry_at"]


def _status(codigo):
    return {"recipient": "5216681234567", "status": "failed", "error_code": codigo,
            "error_title": "x", "wamid": "wamid.1", "occurred_at": "2026-09-17T19:00:00.000Z"}


def _hoja(estatus="ENVIADO_VIDA_TEMPORAL", next_action="", filas=None):
    fila = ["Yomero", "6681234567", estatus, "2026-09-11 19:25:22", next_action, ""]
    return (patch.object(vicky, "match_client_in_sheets",
                         return_value={"row": 2, "nombre": "Yomero", "estatus": estatus}),
            patch.object(vicky, "_sheet_get_rows", return_value=(CABECERAS, filas if filas is not None else [fila])))


def test_un_fallo_permanente_saca_al_prospecto_de_la_cola():
    h1, h2 = _hoja()
    with h1, h2, patch.object(vicky, "_update_row_cells") as update:
        resultado = vicky._reconciliar_fila_por_fallo(_status(131026))

    assert resultado == "NO_ENTREGABLE_131026"
    assert update.call_args.args[1]["ESTATUS"] == "NO_ENTREGABLE_131026"


def test_un_fallo_reintentable_devuelve_el_prospecto_a_la_cola_con_espera():
    h1, h2 = _hoja()
    with h1, h2, patch.object(vicky, "_update_row_cells") as update:
        resultado = vicky._reconciliar_fila_por_fallo(_status(131049))

    updates = update.call_args.args[1]
    assert resultado == "PENDIENTE"
    assert updates["ESTATUS"] == "PENDIENTE"
    assert updates["LAST_MESSAGE_AT"] == ""
    assert updates["NEXT_ACTION"] == "reintento_1"
    assert updates["retry_at"]


def test_el_segundo_fallo_ya_no_reintenta():
    h1, h2 = _hoja(next_action="reintento_1")
    with h1, h2, patch.object(vicky, "_update_row_cells") as update:
        resultado = vicky._reconciliar_fila_por_fallo(_status(131049))

    assert resultado == "NO_ENTREGABLE_TRAS_REINTENTO"
    assert update.call_count == 1


def test_no_se_tocan_filas_que_nosotros_no_dimos_por_enviadas():
    h1, h2 = _hoja(estatus="DUDA_TEMPLATE")
    with h1, h2, patch.object(vicky, "_update_row_cells") as update:
        assert vicky._reconciliar_fila_por_fallo(_status(131026)) is None
    update.assert_not_called()


def test_la_bandera_apaga_la_reconciliacion(monkeypatch):
    monkeypatch.setenv("SHEET_STATUS_RECONCILE", "0")
    assert vicky._reconcile_enabled() is False
    monkeypatch.delenv("SHEET_STATUS_RECONCILE")
    assert vicky._reconcile_enabled() is True


def test_un_fallo_de_hoja_no_tumba_el_webhook():
    with patch.object(vicky, "match_client_in_sheets", side_effect=RuntimeError("hoja caida")):
        assert vicky._reconciliar_fila_por_fallo(_status(131049)) is None


def test_el_cron_respeta_la_espera_del_reintento():
    fila_espera = ["Yomero", "6681234567", "PENDIENTE", "", "reintento_1", "2099-01-01 00:00:00"]
    fila_lista = ["Otro", "6681111111", "PENDIENTE", "", "reintento_1", "2020-01-01 00:00:00"]

    elegido = vicky._pick_next_pending(CABECERAS, [fila_espera, fila_lista])

    assert elegido["whatsapp"] == "6681111111"
