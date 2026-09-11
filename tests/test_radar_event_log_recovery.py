"""Storage failures must not poison deduplication. No network or credentials."""
import concurrent.futures
import threading
import unittest

from radar_events import EventLog, build_event


class EventLogRecoveryTests(unittest.TestCase):
    def event(self, suffix="A"):
        return build_event("message_sent", lead_id="SC-test", wamid="wamid.test." + suffix)

    def test_retry_after_exception_reaches_storage_then_deduplicates(self):
        calls = []

        def append(tab, row):
            calls.append(row)
            if len(calls) == 1:
                raise OSError("simulated unavailable storage")
            return 42

        ledger = EventLog(append)
        event = self.event()
        with self.assertLogs("vicky-secom.radar", level="ERROR"):
            self.assertIsNone(ledger.record(event))
        self.assertEqual(ledger.record(event), 42)
        self.assertIsNone(ledger.record(event))
        self.assertEqual(len(calls), 2)

    def test_missing_write_receipt_does_not_suppress_retry(self):
        calls = []

        def append(tab, row):
            calls.append(row)
            return None if len(calls) == 1 else 43

        ledger = EventLog(append)
        self.assertIsNone(ledger.record(self.event()))
        self.assertEqual(ledger.record(self.event()), 43)
        self.assertEqual(len(calls), 2)

    def test_concurrent_duplicate_waits_for_successful_write(self):
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def append(tab, row):
            calls.append(row)
            entered.set()
            self.assertTrue(release.wait(2))
            return 44

        ledger = EventLog(append)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(ledger.record, self.event())
            self.assertTrue(entered.wait(2))
            second = pool.submit(ledger.record, self.event())
            release.set()
            self.assertEqual(first.result(timeout=2), 44)
            self.assertIsNone(second.result(timeout=2))
        self.assertEqual(len(calls), 1)

    def test_concurrent_retry_can_write_after_first_attempt_fails(self):
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def append(tab, row):
            calls.append(row)
            if len(calls) == 1:
                entered.set()
                self.assertTrue(release.wait(2))
                raise OSError("simulated first-write failure")
            return 45

        ledger = EventLog(append)
        with self.assertLogs("vicky-secom.radar", level="ERROR"):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(ledger.record, self.event())
                self.assertTrue(entered.wait(2))
                second = pool.submit(ledger.record, self.event())
                release.set()
                self.assertIsNone(first.result(timeout=2))
                self.assertEqual(second.result(timeout=2), 45)
        self.assertEqual(len(calls), 2)

    def test_cache_eviction_keeps_the_window_bounded(self):
        calls = []
        ledger = EventLog(lambda tab, row: (calls.append(row), len(calls))[1], dedupe_size=2)
        for suffix in ["A", "B", "C", "C", "A"]:
            ledger.record(self.event(suffix))
        self.assertEqual(len(calls), 4)

    def test_restart_preserves_deterministic_identity_for_radar(self):
        calls = []

        def append(tab, row):
            calls.append(row)
            return len(calls)

        EventLog(append).record(self.event())
        EventLog(append).record(self.event())
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0], calls[1][0])

    def test_empty_id_never_calls_storage(self):
        ledger = EventLog(lambda *_: self.fail("invalid event reached storage"))
        with self.assertLogs("vicky-secom.radar", level="WARNING"):
            self.assertIsNone(ledger.record({}))


if __name__ == "__main__":
    unittest.main()
