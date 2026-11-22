"""In-memory stand-in for the Telegram Bot API, injected as the HTTP transport
of TelegramNotifier: a callable post(url, body, timeout) -> (status, bytes).
No network, no threads."""
from __future__ import annotations

import json

TOKEN = "123456:SECRET-token_abc"


class FakeTelegram:
    def __init__(self, token: str = TOKEN):
        self.token = token
        self.down = False             # connection refused
        self.timeout = False          # socket timeout
        self.status_5xx = False       # HTTP 502
        self.rate_limited = False     # HTTP 429
        self.garbage = False          # HTTP 200, not JSON
        self.blocked: set[int] = set()  # chat ids answering 403 (bot blocked / never started)
        self.answer = None            # (status, bytes) override for one-off shapes
        self.accepted: list[dict] = []  # {"chat_id", "text", "message_id"}
        self.calls = 0
        self.next_id = 100

    def __call__(self, url: str, body: bytes, timeout: float):
        self.calls += 1
        if self.down:
            raise ConnectionRefusedError(f"[Errno 61] Connection refused: {url}")
        if self.timeout:
            raise TimeoutError("timed out")
        if self.answer is not None:
            return self.answer
        if not url.endswith(f"/bot{self.token}/sendMessage"):
            return 404, json.dumps({"ok": False, "error_code": 404, "description": "Not Found"}).encode()
        if self.status_5xx:
            return 502, b"<html>Bad Gateway</html>"
        if self.rate_limited:
            return 429, json.dumps({"ok": False, "error_code": 429,
                                    "description": "Too Many Requests: retry after 5"}).encode()
        if self.garbage:
            return 200, b"\x00not json"
        msg = json.loads(body)
        if msg["chat_id"] in self.blocked:
            return 403, json.dumps({"ok": False, "error_code": 403,
                                    "description": "Forbidden: bot was blocked by the user"}).encode()
        self.next_id += 1
        self.accepted.append({"chat_id": msg["chat_id"], "text": msg["text"], "message_id": self.next_id})
        return 200, json.dumps({"ok": True, "result": {"message_id": self.next_id, "chat": {"id": msg["chat_id"]}}}).encode()

    def texts(self, chat_id: int) -> list[str]:
        return [m["text"] for m in self.accepted if m["chat_id"] == chat_id]
