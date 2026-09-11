from unittest.mock import patch
import app as vicky


def test_inbound_is_recorded_with_original_message_identity():
    msg = {"id": "wamid.inbound", "timestamp": "1789084800", "text": {"body": "Quiero información"}}
    with patch.object(vicky, "record_radar_event") as record:
        vicky._record_inbound_radar(msg, {"lead_id": "SC-existing", "nombre": "Prueba"}, "5216680000000")
    payload = record.call_args.kwargs
    assert payload["event_type"] == "message_inbound"
    assert payload["lead_id"] == "SC-existing"
    assert payload["wamid"] == msg["id"]
    assert payload["direction"] == "inbound"


def test_inbound_does_not_invent_lead_identity():
    with patch.object(vicky, "record_radar_event") as record:
        vicky._record_inbound_radar({"id": "wamid.unknown"}, None, "5216680000000")
    record.assert_not_called()


def test_no_delivery_is_scheduled_without_durable_receipt():
    with patch.object(vicky._event_log, "record", return_value=None), \
         patch.object(vicky._radar_outbox, "start") as start, \
         patch.object(vicky._radar_client, "send") as send:
        vicky.record_radar_event(event_type="message_sent", lead_id="SC-existing", wamid="wamid.A")
    start.assert_not_called()
    send.assert_not_called()
