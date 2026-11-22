"""Client for the heirdrop service's Unix socket: one JSON request line, one
JSON answer line per connection (doc/2-heirdrop-service.md, "Local
interface"). The service knows the web app by its uid; nothing in the request
claims an identity.

Every failure is one of two exceptions, and the web app shows neither text
to the visitor: `Unavailable` (socket down, timeout, garbage, too large) and
`Refused` (the service answered `ok: false`).
"""
from __future__ import annotations

import json
import socket
import time
from pathlib import Path

TIMEOUT = 30.0               # whole request, connect to last byte
MAX_ANSWER = 20_000_000      # a 10 MB document is about 13.4 MB in base64


class Unavailable(Exception):
    pass


class Refused(Exception):
    pass


def ask(socket_path: Path, body: dict, timeout: float = TIMEOUT, max_bytes: int = MAX_ANSWER) -> dict:
    deadline = time.monotonic() + timeout
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(str(socket_path))
            s.sendall(json.dumps(body).encode() + b"\n")
            buf = bytearray()
            while not buf.endswith(b"\n"):
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError("answer took too long")
                s.settimeout(left)
                chunk = s.recv(1 << 16)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > max_bytes:
                    raise Unavailable(f"answer larger than {max_bytes} bytes")
    except OSError as e:
        raise Unavailable(f"socket {socket_path}: {e!r}") from None
    try:
        answer = json.loads(buf.decode())
    except (UnicodeDecodeError, ValueError):
        raise Unavailable("answer is not JSON") from None
    if not isinstance(answer, dict) or not isinstance(answer.get("ok"), bool):
        raise Unavailable("answer is not an object with ok")
    if not answer["ok"]:
        raise Refused(str(answer.get("error")))
    return answer
