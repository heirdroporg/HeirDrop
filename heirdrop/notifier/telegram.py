"""Telegram backend: one Bot API `sendMessage` per alert, outbound only.

"Accepted" means Telegram answered HTTP 200 with `ok: true` and a
`message_id`. Telegram gives no delivery or read report.

Failures raise NotifyError:
- permanent (400, 401, 403, 404): wrong token or chat id, blocked bot, the
  recipient never pressed Start. Retrying helps only after someone acts.
- temporary: network errors, timeouts, 429, 5xx, a malformed answer.
  `transport` is set when the network or Telegram itself is the cause
  (network, timeout, 429, 5xx): the service stops sending for that tick.

The answer is untrusted in shape. The token is part of the URL and must never
reach a log line, an alert file or the socket: error texts are built from the
status and Telegram's `description`, and anything taken from an exception
has the token replaced.
"""
from __future__ import annotations

import http.client
import json
import time
from datetime import datetime
from urllib.parse import urlsplit

from ..config import TelegramConfig
from ..statefiles import StateStore
from . import NotifyError

MAX_ANSWER_BYTES = 64_000
PERMANENT = (400, 401, 403, 404)


def http_post(url: str, body: bytes, timeout: float) -> tuple[int, bytes]:
    """POST a JSON body; returns (status, answer). Bounded in total time and size."""
    parts = urlsplit(url)
    conn_cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    conn = conn_cls(parts.hostname, parts.port, timeout=timeout)
    deadline = time.monotonic() + timeout
    try:
        conn.request("POST", parts.path, body=body, headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = b""
        while chunk := resp.read(16384):
            data += chunk
            if len(data) > MAX_ANSWER_BYTES:
                raise NotifyError(f"answer larger than {MAX_ANSWER_BYTES} bytes", transport=True)
            if time.monotonic() > deadline:
                raise NotifyError(f"answer took longer than {timeout} s", transport=True)
        return resp.status, data
    finally:
        conn.close()


class TelegramNotifier:
    name = "telegram"

    def __init__(self, tg: TelegramConfig, store: StateStore, post=http_post):
        if not tg.token:
            raise ValueError("telegram token not loaded")
        self.tg = tg
        self.store = store
        self.post = post
        self._token = tg.token
        self._url = f"{tg.api_url}/bot{tg.token}/sendMessage"

    def deliver(self, alert: dict, now: datetime) -> dict:
        receipt = self.send(alert["to"], alert["text"])
        self.store.log_event(now, "alert", id=alert["id"], to=alert["to"], alert_kind=alert["kind"],
                             text=alert["text"], backend=self.name, message_id=receipt["message_id"])
        return receipt

    def send(self, to: str, text: str) -> dict:
        body = json.dumps({"chat_id": self.tg.chat_id(to), "text": text,
                           "disable_web_page_preview": True}).encode()
        try:
            status, data = self.post(self._url, body, self.tg.timeout)
        except NotifyError as e:
            raise NotifyError(self._clean(e.message), e.permanent, e.transport) from None
        except Exception as e:  # noqa: BLE001 - network failure of any kind; never raise anything else
            raise NotifyError(self._clean(f"network: {type(e).__name__}: {e}"), transport=True) from None
        return self._answer(status, data)

    def _answer(self, status, data) -> dict:
        obj = None
        try:
            obj = json.loads(data)
        except (ValueError, TypeError):
            pass
        description = ""
        code = status
        if isinstance(obj, dict):
            if isinstance(obj.get("description"), str):
                description = obj["description"][:200]
            if isinstance(obj.get("error_code"), int) and not isinstance(obj.get("error_code"), bool):
                code = obj["error_code"]
            if status == 200 and obj.get("ok") is True:
                result = obj.get("result")
                mid = result.get("message_id") if isinstance(result, dict) else None
                if isinstance(mid, int) and not isinstance(mid, bool):
                    return {"message_id": mid}
                raise NotifyError("malformed answer: no message_id")
        if status == 200 or not isinstance(status, int):
            raise NotifyError(f"malformed answer (HTTP {status})")
        text = self._clean(f"HTTP {code}{': ' + description if description else ''}")
        if code in PERMANENT or status in PERMANENT:
            raise NotifyError(text, permanent=True)
        raise NotifyError(text, transport=code == 429 or code >= 500 or status >= 500)

    def _clean(self, text: str) -> str:
        return text.replace(self._token, "<token>")
