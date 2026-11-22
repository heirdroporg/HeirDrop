"""Events-log backend: writes the alert to the events log and reports it as
accepted. Used when no Telegram section is configured, and in tests."""
from __future__ import annotations

from datetime import datetime

from ..statefiles import StateStore


class EventsLogNotifier:
    name = "events_log"

    def __init__(self, store: StateStore):
        self.store = store

    def deliver(self, alert: dict, now: datetime) -> None:
        self.store.log_event(now, "alert", id=alert["id"], to=alert["to"],
                             alert_kind=alert["kind"], text=alert["text"])
