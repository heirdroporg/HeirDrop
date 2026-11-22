"""Minimal JSON-RPC client for bitcoind (standard library only).

Only these calls are used by the service, none of which need txindex or a
wallet: getblockchaininfo, getblockheader, getblockhash, decoderawtransaction,
gettxout, testmempoolaccept, getmempoolinfo, getmempoolentry,
sendrawtransaction.
"""
from __future__ import annotations

import base64
import http.client
import json
import time
from pathlib import Path
from urllib.parse import urlsplit


MAX_RESPONSE_BYTES = 8_000_000  # far above any answer to the calls listed above


class NodeError(Exception):
    pass


class NodeUnreachable(NodeError):
    """Connection, authentication or protocol failure: the node cannot be used."""


class RpcError(NodeError):
    """The node answered with a JSON-RPC error."""

    def __init__(self, code: int, message: str):
        super().__init__(f"RPC error {code}: {message}")
        self.code = code
        self.message = message


class RpcClient:
    def __init__(self, url: str, cookie_file: Path | None = None,
                 user: str | None = None, password: str | None = None, timeout: float = 20.0):
        parts = urlsplit(url)
        self.scheme = parts.scheme
        self.host = parts.hostname or "127.0.0.1"
        self.port = parts.port or (443 if parts.scheme == "https" else 8332)
        self.path = parts.path or "/"
        self.cookie_file = cookie_file
        self.user = user
        self.password = password
        self.timeout = timeout

    def _credentials(self) -> str:
        if self.cookie_file is not None:
            # The cookie changes every time bitcoind starts, so read it each call.
            try:
                return self.cookie_file.read_text().strip()
            except OSError as e:
                raise NodeUnreachable(f"cannot read cookie file {self.cookie_file}: {e}") from e
        return f"{self.user}:{self.password}"

    def call(self, method: str, *params):
        body = json.dumps({"jsonrpc": "2.0", "id": "heirdrop", "method": method, "params": list(params)})
        headers = {
            "Content-Type": "application/json",
            "Authorization": "Basic " + base64.b64encode(self._credentials().encode()).decode(),
        }
        conn_cls = http.client.HTTPSConnection if self.scheme == "https" else http.client.HTTPConnection
        conn = conn_cls(self.host, self.port, timeout=self.timeout)
        try:
            conn.request("POST", self.path, body=body, headers=headers)
            resp = conn.getresponse()
            # The socket timeout is per read; bound the whole answer in time and size.
            deadline = time.monotonic() + self.timeout
            data = b""
            while chunk := resp.read(65536):
                data += chunk
                if len(data) > MAX_RESPONSE_BYTES:
                    raise NodeUnreachable(f"{method}: response larger than {MAX_RESPONSE_BYTES} bytes")
                if time.monotonic() > deadline:
                    raise NodeUnreachable(f"{method}: response took longer than {self.timeout} s")
        except (OSError, http.client.HTTPException) as e:
            raise NodeUnreachable(f"{method}: {e}") from e
        finally:
            conn.close()
        if resp.status == 401:
            raise NodeUnreachable("authentication rejected by bitcoind")
        try:
            obj = json.loads(data)
        except ValueError as e:
            raise NodeUnreachable(f"{method}: non-JSON response (HTTP {resp.status})") from e
        if not isinstance(obj, dict):
            raise NodeUnreachable(f"{method}: unexpected response")
        if obj.get("error"):
            err = obj["error"]
            try:
                code, message = int(err.get("code", 0)), str(err.get("message", ""))
            except (AttributeError, TypeError, ValueError):
                raise NodeUnreachable(f"{method}: malformed error {err!r}") from None
            raise RpcError(code, message)
        return obj.get("result")
