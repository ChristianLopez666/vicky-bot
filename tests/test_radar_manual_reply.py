from datetime import datetime, timedelta
from unittest.mock import patch
import uuid

import app as vicky


PHONE = "5216682478005"
LEAD_ID = "SC-00000000-0000-4000-8000-000000000001"


def _headers():
    return {"X-RADAR-REPLY-TOKEN": "radar-secret"}


def _match():
    return {"lead_id": LEAD_ID, "nombre": "Prospecto de prueba"}


def _future_window():
    return (datetime.utcnow() + timedelta(hours=2)).isoformat()


def test_manual_reply_requires_its_own_secret():
    with patch.object(vicky, "RADAR_REPLY_TOKEN", "radar-secret"):
        response = vicky.app.test_client().post("/ext/radar/reply", json={})
    assert response.status_code == 401


def test_manual_reply_rejects_closed_window_without_sending():
    body = {
        "action": "reply", "to": PHONE, "lead_id": LEAD_ID, "text": "Hola",
        "window_expires_at": (datetime.utcnow() - timedelta(minutes=1)).isoformat(),
        "request_id": str(uuid.uuid4()),
    }
    with patch.object(vicky, "RADAR_REPLY_TOKEN", "radar-secret"), \
         patch.object(vicky, "match_client_in_sheets", return_value=_match()), \
         patch.object(vicky, "send_message") as send:
        response = vicky.app.test_client().post("/ext/radar/reply", headers=_headers(), json=body)
    assert response.status_code == 409
    assert response.get_json()["error"] == "ventana_24h_cerrada"
    send.assert_not_called()


def test_manual_reply_activates_handoff_and_records_meta_identity():
    events = []
    body = {
        "action": "reply", "to": PHONE, "lead_id": LEAD_ID, "text": "Mensaje de prueba",
        "window_expires_at": _future_window(), "request_id": str(uuid.uuid4()),
        "actor": "Christian López",
    }
    handoff = {"active": True, "expires_at": body["window_expires_at"], "actor": body["actor"]}
    with patch.object(vicky, "RADAR_REPLY_TOKEN", "radar-secret"), \
         patch.object(vicky, "match_client_in_sheets", return_value=_match()), \
         patch.object(vicky, "_set_human_handoff", return_value=handoff) as set_handoff, \
         patch.object(vicky, "record_radar_event", side_effect=lambda **event: events.append(event)), \
         patch.object(vicky, "send_message", return_value={"ok": True, "wamid": "wamid.test", "motivo": ""}) as send:
        response = vicky.app.test_client().post("/ext/radar/reply", headers=_headers(), json=body)
    assert response.status_code == 200
    assert response.get_json()["wamid"] == "wamid.test"
    set_handoff.assert_called_once_with(PHONE, True, body["window_expires_at"], body["actor"])
    send.assert_called_once_with(PHONE, body["text"], return_detail=True, retry_on_timeout=False)
    assert [event["event_type"] for event in events] == ["message_requested", "message_sent"]
    assert events[1]["wamid"] == "wamid.test"
    assert events[1]["text"] == body["text"]


def test_inbound_text_is_logged_but_vicky_does_not_answer_in_human_mode():
    message = {"id": "wamid.inbound", "from": PHONE, "timestamp": "1789430400", "type": "text", "text": {"body": "Sí me interesa"}}
    with patch.object(vicky, "match_client_in_sheets", return_value=_match()), \
         patch.object(vicky, "_record_inbound_radar") as record, \
         patch.object(vicky, "append_respuesta_cliente") as append, \
         patch.object(vicky, "_human_handoff_active", return_value=True), \
         patch.object(vicky, "_extend_human_handoff_from_inbound") as extend, \
         patch.object(vicky, "send_message") as send:
        vicky._handle_inbound_message(message)
    record.assert_called_once()
    append.assert_called_once()
    extend.assert_called_once_with(PHONE, message)
    send.assert_not_called()
