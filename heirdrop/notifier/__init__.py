"""Notifier package. Alerts are queued on disk by the StateStore; a backend
sends them and the service records the acceptance time in the queue file.

Which alerts exist is decided in `logic._alerts()`; this package only
renders, selects and sends them.
"""
from __future__ import annotations

from datetime import datetime
from typing import Protocol

from ..statefiles import StateStore
from .eventslog import EventsLogNotifier


class NotifyError(Exception):
    """A backend did not accept a message. `permanent` is True when retrying
    will not help until someone changes something (blocked bot, bad chat id).
    `transport` is True when the network or the service behind it failed, so
    further sends in the same tick would most likely fail (and block) too."""

    def __init__(self, message: str, permanent: bool = False, transport: bool = False):
        super().__init__(message)
        self.message = message
        self.permanent = permanent
        self.transport = transport


class Notifier(Protocol):
    name: str

    def deliver(self, alert: dict, now: datetime) -> dict | None:
        """Send one alert. Returns a receipt (or None) or raises on failure."""


def build(cfg, store: StateStore) -> Notifier:
    """The backend the config asks for: Telegram if configured, else the events log."""
    if cfg.telegram is None:
        return EventsLogNotifier(store)
    from .telegram import TelegramNotifier  # imports NotifyError from this module
    return TelegramNotifier(cfg.telegram, store)
