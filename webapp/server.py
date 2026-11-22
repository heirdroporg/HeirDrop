"""HTTP adapter: `http.server.HTTPServer` (one thread) turning HTTP into an
`app.Request` and the `app.Response` back into HTTP.

It sits behind nginx, which terminates TLS, buffers slow clients and
passes the visitor's address in `X-Forwarded-For`. That header is believed
only from a loopback peer. Bounds: request bodies of at most MAX_BODY bytes
(forms only), POST only as `application/x-www-form-urlencoded`, and a socket
timeout per connection. A failure inside the app answers a bare 500 and is
logged; the server keeps running.
"""
from __future__ import annotations

import ipaddress
import signal
import socket
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

from .app import App, Request, Response

MAX_BODY = 4096
MAX_FIELDS = 10
TIMEOUT = 15  # seconds per connection; nginx has the whole request already


def client_address(peer: str, forwarded: str | None) -> str:
    """The visitor's address: nginx's X-Forwarded-For (its last entry) when
    the peer is loopback, else the peer itself."""
    try:
        loopback = ipaddress.ip_address(peer).is_loopback
    except ValueError:
        loopback = False
    if loopback and forwarded:
        last = forwarded.split(",")[-1].strip()
        try:
            return str(ipaddress.ip_address(last))
        except ValueError:
            return f"{peer}(bad-forwarded)"
    return peer


def parse_form(body: bytes) -> dict | None:
    """One value per field, or None if the body is not a small urlencoded form."""
    try:
        text = body.decode("ascii")
        fields = parse_qs(text, keep_blank_values=True, strict_parsing=bool(text),
                          max_num_fields=MAX_FIELDS, encoding="utf-8", errors="strict")
    except (UnicodeDecodeError, ValueError):
        return None
    return {k: v[0] for k, v in fields.items()}


class Handler(BaseHTTPRequestHandler):
    server_version = "web"
    sys_version = ""
    # One request per connection: with one thread, a kept-alive connection
    # would block everyone else until it timed out.
    protocol_version = "HTTP/1.0"
    timeout = TIMEOUT
    app: App  # set on the class by make_server

    def version_string(self) -> str:
        return self.server_version

    def log_message(self, fmt, *args):  # the app logs what matters; keep http.server quiet
        pass

    def _client(self) -> str:
        return client_address(self.client_address[0], self.headers.get("X-Forwarded-For"))

    def _send(self, resp: Response, head_only: bool = False) -> None:
        self.send_response(resp.status)
        for name, value in resp.headers:
            self.send_header(name, value)
        self.send_header("Content-Type", resp.content_type)
        self.send_header("Content-Length", str(len(resp.body)))
        self.end_headers()
        if not head_only:
            self.wfile.write(resp.body)
        print(f"request {self.command} {urlsplit(self.path).path[:200]} {resp.status} {self._client()}",
              file=sys.stderr, flush=True)

    def _plain(self, status: int, text: str) -> None:
        self._send(Response(status, text.encode(), "text/plain; charset=utf-8",
                            [("Cache-Control", "no-store")]))

    def _dispatch(self, method: str, form: dict) -> None:
        path = urlsplit(self.path).path
        headers = {k.lower(): v for k, v in self.headers.items()}
        req = Request(method, path, headers, form, self._client())
        try:
            resp = self.app.handle(req)
        except Exception as e:  # noqa: BLE001 - a bug answers 500, the server stays up
            print(f"internal_error {method} {path[:200]} {e!r}", file=sys.stderr, flush=True)
            self._plain(500, "internal error")
            return
        self._send(resp)

    def do_GET(self):
        if self.headers.get("Content-Length") not in (None, "0"):
            self._plain(400, "bad request")
            return
        self._dispatch("GET", {})

    def do_POST(self):
        length = self.headers.get("Content-Length")
        if length is None or not length.isascii() or not length.isdigit():
            self._plain(411, "length required")
            return
        if int(length) > MAX_BODY:
            self._plain(413, "too large")
            return
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/x-www-form-urlencoded":
            self._plain(415, "unsupported")
            return
        form = parse_form(self.rfile.read(int(length)))
        if form is None:
            self._plain(400, "bad request")
            return
        self._dispatch("POST", form)

    def _not_allowed(self):
        self._plain(405, "not allowed")

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = _not_allowed


def make_server(app: App, host: str, port: int) -> HTTPServer:
    handler = type("BoundHandler", (Handler,), {"app": app})
    cls = HTTPServer
    if ":" in host:  # IPv6 loopback
        cls = type("HTTPServer6", (HTTPServer,), {"address_family": socket.AF_INET6})
    return cls((host, port), handler)


def serve(app: App, host: str, port: int) -> None:
    httpd = make_server(app, host, port)

    def stop(signum, _frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    print(f"listening on {host}:{port}", file=sys.stderr, flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        print("stopped", file=sys.stderr, flush=True)
