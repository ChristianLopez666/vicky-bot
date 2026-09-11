import copy
import json
import unittest
from unittest.mock import Mock

from radar_events import EventLog, RadarClient, EVENTS_HEADER, PENDIENTE, ENVIADO, RECHAZADO, build_event
from radar_outbox import OutboxPump, OutboxWorker, SheetsOutbox, correlated_event, correlation_index

REQUEST_ID = "b3b82a80-59a0-4cb5-b8dc-138a830098ef"


def event(suffix="A", **extra):
    return build_event("message_sent", lead_id="SC-test", wamid="wamid." + suffix,
                       phone_number_id="123", request_id=REQUEST_ID, **extra)


class Repository:
    def __init__(self, events):
        self.rows = []
        self.index = {}
        self.fail_mark = False
        ledger = EventLog(lambda tab, row: (self.rows.append(row), len(self.rows) + 1)[1])
        for item in events:
            ledger.record(item)

    def page(self, start, size):
        return copy.deepcopy(self.rows[start-2:start-2+size])

    def correlations(self):
        return self.index

    def mark(self, number, event_id, state, attempts, now):
        if self.fail_mark:
            raise OSError("Sheets unavailable")
        row = self.rows[number-2]
        row[14:17] = [state, str(attempts), "2026-09-11T00:00:00.000Z"]


class OutboxTests(unittest.TestCase):
    def client(self, states=None):
        c = Mock()
        c.configured.return_value = True
        c.send.side_effect = states or [ENVIADO] * 20
        return c

    def test_restart_recovers_pending_and_skips_terminal(self):
        r = Repository([event("A"), event("B"), event("C")])
        r.rows[1][14] = ENVIADO
        r.rows[2][14] = RECHAZADO
        c = self.client()
        self.assertEqual(OutboxPump(r, c).run_once(), 1)
        self.assertEqual(c.send.call_args.args[0]["event_id"], event("A")["event_id"])
        self.assertEqual(r.rows[0][14:16], [ENVIADO, "1"])
        self.assertEqual(OutboxPump(r, c).run_once(), 0)

    def test_lost_sheet_ack_repeats_same_event_after_restart(self):
        r = Repository([event()]); c = self.client(); r.fail_mark = True
        with self.assertRaises(OSError):
            OutboxPump(r, c).run_once()
        r.fail_mark = False
        OutboxPump(r, c).run_once()
        self.assertEqual(c.send.call_args_list[0].args[0]["event_id"], c.send.call_args_list[1].args[0]["event_id"])

    def test_backoff_and_rate_limit_do_not_starve_next_rows(self):
        r = Repository([event(str(i)) for i in range(4)])
        c = self.client([PENDIENTE] * 10)
        clock = lambda: 1789084800.0
        pump = OutboxPump(r, c, limit=2, clock=clock)
        self.assertEqual(pump.run_once(), 2)
        self.assertEqual(pump.cursor, 4)
        self.assertEqual(pump.run_once(), 2)
        self.assertEqual(pump.run_once(), 0)
        self.assertEqual(c.send.call_count, 4)

    def test_status_uses_original_request_and_missing_link_waits(self):
        item = event(); item["message"]["request_id"] = None
        r = Repository([item]); c = self.client()
        OutboxPump(r, c).run_once()
        c.send.assert_not_called()
        self.assertEqual(r.rows[0][14], PENDIENTE)
        original = Repository([event()])
        r.index = correlation_index(original.rows)
        r.rows[0][16] = ""
        OutboxPump(r, c).run_once()
        self.assertEqual(c.send.call_args.args[0]["message"]["request_id"], REQUEST_ID)
        self.assertIsNone(item["message"]["request_id"])

    def test_ambiguous_or_different_source_cannot_supply_request_id(self):
        item = event(); item["message"]["request_id"] = None
        original = Repository([event()]).rows
        wrong = copy.deepcopy(original); wrong[0][3] = "vicky_redes"
        self.assertIsNone(correlated_event(item, correlation_index(wrong)))
        duplicate = copy.deepcopy(original[0]); duplicate[7] = "2c56cbe5-7fd9-44d3-bbec-8fca7e4d47c8"
        self.assertIsNone(correlated_event(item, correlation_index(original + [duplicate])))

    def test_incomplete_receipt_does_not_hide_later_correlation(self):
        item = event(); item["message"]["request_id"] = None
        r = Repository([item, item, event(), event()])
        self.assertEqual(len(r.rows), 2)
        self.assertEqual(r.rows[1][7], REQUEST_ID)

    def test_disabled_client_never_reads_or_starts(self):
        r = Mock(); c = self.client(); c.configured.return_value = False
        self.assertEqual(OutboxPump(r, c).run_once(), 0)
        r.page.assert_not_called()
        worker = OutboxWorker(Mock(), c)
        worker.start()
        self.assertIsNone(worker._thread)

    def test_corrupt_row_is_rejected_but_next_valid_event_runs(self):
        r = Repository([event("A"), event("B")]); c = self.client()
        r.rows[0][17] = "{corrupt"
        self.assertEqual(OutboxPump(r, c).run_once(), 2)
        self.assertEqual(r.rows[0][14], RECHAZADO)
        self.assertEqual(r.rows[1][14], ENVIADO)

    def test_moved_sheet_row_is_never_overwritten(self):
        svc = Mock()
        values = svc.spreadsheets.return_value.values.return_value
        values.get.return_value.execute.return_value = {"values": [["other-event"]]}
        with self.assertRaises(ValueError):
            SheetsOutbox(svc, "sheet").mark(2, "original-event", ENVIADO, 1, 1789084800)
        values.update.assert_not_called()

    def test_transport_requires_exact_ack_even_after_http_200(self):
        for payload, expected in [({}, PENDIENTE), ({"ok": False, "event_id": "x"}, PENDIENTE),
                                  ({"ok": True, "event_id": "other"}, PENDIENTE),
                                  ({"ok": True, "event_id": "x", "duplicate": True}, ENVIADO)]:
            response = Mock(status_code=200); response.json.return_value = payload
            c = RadarClient(url="https://radar.example", token="t", hmac_secret="s", enabled=True,
                            poster=lambda *a, **k: response)
            self.assertEqual(c.send({"event_id": "x"}), expected)


if __name__ == "__main__":
    unittest.main()
