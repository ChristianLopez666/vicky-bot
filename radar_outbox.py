"""Replay the existing durable Sheets log; never send WhatsApp messages.

Runs inside the existing web process, only with RADAR_EMIT_ENABLED. Each
thread owns its Google client. No new service, dependency or paid queue.
"""
from __future__ import annotations

import copy
import json
import logging
import threading
import time
import uuid
from datetime import datetime, timezone

from radar_events import EVENTS_HEADER, EVENTS_TAB, PENDIENTE, ENVIADO, RECHAZADO, canonical_ts

log = logging.getLogger("vicky-secom.radar.outbox")
OUTBOUND = {"message_requested", "message_sent", "message_delivered", "message_read", "message_failed", "advisor_notified"}


def request_key(event):
    return (event.get("source"), (event.get("channel") or {}).get("phone_number_id"),
            (event.get("lead") or {}).get("lead_id"), (event.get("message") or {}).get("wamid"))


def correlation_index(rows):
    result = {}
    for row in rows:
        if len(row) < 9 or not row[8] or not row[7]:
            continue
        try:
            if uuid.UUID(row[7]).version != 4:
                continue
        except (ValueError, TypeError, AttributeError):
            continue
        result.setdefault((row[3], row[4], row[5], row[8]), set()).add(row[7])
    return result


def correlated_event(event, index):
    event = copy.deepcopy(event)
    message = event.setdefault("message", {})
    if event.get("event_type") in OUTBOUND and not event.get("backfill") and not message.get("request_id"):
        candidates = index.get(request_key(event), set())
        if len(candidates) != 1:
            return None
        message["request_id"] = next(iter(candidates))
    return event


def retry_due(row, now):
    if row.get("radar_state") != PENDIENTE:
        return False
    try:
        attempts = max(0, int(row.get("radar_attempts") or 0))
        last = row.get("radar_last_try")
        if not last:
            return True
        elapsed = now - datetime.fromisoformat(last.replace("Z", "+00:00")).timestamp()
        return elapsed >= min(900, 60 * 2 ** min(attempts, 4))
    except (ValueError, TypeError, OverflowError):
        return True


class SheetsOutbox:
    def __init__(self, service, spreadsheet_id):
        self.values = service.spreadsheets().values()
        self.spreadsheet_id = spreadsheet_id
        self.index = None

    def read(self, range_name):
        return self.values.get(spreadsheetId=self.spreadsheet_id, range=range_name).execute().get("values", [])

    def page(self, start, size):
        self.index = None
        header = self.read(f"{EVENTS_TAB}!A1:R1")
        if not header or header[0] != EVENTS_HEADER:
            raise ValueError("La cabecera de EVENTOS_RADAR no coincide; no se modifica.")
        return self.read(f"{EVENTS_TAB}!A{start}:R{start + size - 1}")

    def correlations(self):
        if self.index is None:
            self.index = correlation_index(self.read(f"{EVENTS_TAB}!A2:I"))
        return self.index

    def mark(self, number, event_id, state, attempts, now):
        # Do not write to a different event if a human moved/deleted a row.
        latest = self.read(f"{EVENTS_TAB}!A{number}:R{number}")
        if not latest or latest[0][0] != event_id:
            raise ValueError("La fila cambio de identidad; se releera en el siguiente ciclo.")
        row = dict(zip(EVENTS_HEADER, latest[0]))
        if row.get("radar_state") in (ENVIADO, RECHAZADO):
            return
        attempts = max(attempts, int(row.get("radar_attempts") or 0))
        self.values.update(spreadsheetId=self.spreadsheet_id,
                           range=f"{EVENTS_TAB}!O{number}:Q{number}", valueInputOption="RAW",
                           body={"values": [[state, str(attempts), canonical_ts(now)]]}).execute()


class OutboxPump:
    def __init__(self, repository, client, page_size=200, limit=10, clock=time.time):
        self.repository, self.client = repository, client
        self.page_size, self.limit, self.clock = page_size, limit, clock
        self.cursor = 2

    def run_once(self):
        if not self.client.configured():
            return 0
        start = self.cursor
        rows = self.repository.page(start, self.page_size)
        attempts = 0
        for offset, values in enumerate(rows):
            if attempts >= self.limit or not self.client.configured():
                self.cursor = start + offset
                return attempts
            row = dict(zip(EVENTS_HEADER, values))
            now = self.clock()
            if not retry_due(row, now):
                continue
            try:
                event = json.loads(row.get("payload_json") or "")
                if not isinstance(event, dict) or event.get("event_id") != row.get("event_id") or event.get("source") != "vicky_secom":
                    raise ValueError("Identidad del payload inconsistente")
            except (ValueError, TypeError, AttributeError):
                state = RECHAZADO
                log.error("Payload de bitacora invalido en fila %s", start + offset)
            else:
                needs_link = (event.get("event_type") in OUTBOUND and not event.get("backfill")
                              and not (event.get("message") or {}).get("request_id"))
                # Storage lookup failures propagate to the next cycle; they
                # must never turn a valid event into a terminal rejection.
                event = correlated_event(event, self.repository.correlations() if needs_link else {})
                state = self.client.send(event) if event is not None else PENDIENTE
            attempts += 1
            try:
                previous_attempts = max(0, int(row.get("radar_attempts") or 0))
            except (ValueError, TypeError):
                previous_attempts = 0
            self.repository.mark(start + offset, row.get("event_id"), state, previous_attempts + 1, now)
        self.cursor = 2 if len(rows) < self.page_size else start + len(rows)
        return attempts


class OutboxWorker:
    def __init__(self, factory, client, interval=60):
        self.factory, self.client, self.interval = factory, client, interval
        self._lock = threading.Lock()
        self._thread = None
        self._stop = threading.Event()

    def start(self):
        if not self.client.configured():
            return
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._run, name="RadarOutbox", daemon=True)
            self._thread.start()

    def _run(self):
        pump = None
        while not self._stop.is_set():
            try:
                if self.client.configured():
                    if pump is None:
                        pump = OutboxPump(self.factory(), self.client)
                    pump.run_once()
            except Exception as exc:
                log.warning("Barrido Radar pendiente (%s); se reintentara", type(exc).__name__)
                pump = None
            self._stop.wait(self.interval)
