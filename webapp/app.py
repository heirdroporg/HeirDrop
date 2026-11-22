"""Request handling: a plain function from `Request` to `Response`, so it is
tested without HTTP. The server (step 5) parses HTTP into a Request.

Routes:
  GET  /             front page without a session, the person's own view with one
  GET  /p            the picture
  GET  /login        login form;  POST /login  check, set the cookie, back to /
  POST /logout       clear the cookie
  GET  /checkin      wife: the confirm page; POST /checkin  check in
  GET  /tx/<n>.txt   a transaction of the person's package as text
  GET  /doc/<n>      a document of the person's package
Anything else is an empty 404. A page that needs a session, asked without
one, answers with the front page (GET) or a redirect to it (POST).

The service is asked again on every request; nothing about its state is
kept. Any doubt about an answer shows the "not reachable" page.
"""
from __future__ import annotations

import base64
import binascii
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from http.cookies import CookieError, SimpleCookie
from typing import Callable
from urllib.parse import quote

from . import client, files, pages
from .auth import CHOICES, COOKIE, Auth, Session
from .config import WebConfig

WIFE_STATES = {"normal", "grace", "wife_window", "inheritance", "closed", "false_triggered"}
FAMILY_STATES = {"standby", "alert", "inheritance"}
CHECKIN_STATES = {"grace", "wife_window", "closed"}  # where she has a button
LABELS = tuple(label for label, _ in CHOICES)          # the login dropdown
# The browser tab after the login names the account (the owner's choice);
# before it, the neutral `title` from the config.
TITLES = {"wife": "WIFE", "family": "FAMILY"}
TX_PATH = re.compile(r"/tx/([1-9][0-9]?)\.txt")
DOC_PATH = re.compile(r"/doc/([1-9][0-9]{0,3})")


@dataclass
class Request:
    method: str
    path: str
    headers: dict = field(default_factory=dict)  # lower-case names
    form: dict = field(default_factory=dict)     # POST fields, one value each
    client: str = "-"


@dataclass
class Response:
    status: int
    body: bytes = b""
    content_type: str = "text/html; charset=utf-8"
    headers: list = field(default_factory=list)


class Doubt(Exception):
    """The service's answer has an unexpected shape."""


def _journal(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


class App:
    def __init__(self, cfg: WebConfig, auth: Auth, ask: Callable[[dict], dict] | None = None,
                 now: Callable[[], datetime] = files.utc_now, log: Callable[[str], None] = _journal):
        self.cfg = cfg
        self.auth = auth
        self.ask = ask or (lambda body: client.ask(cfg.socket_path, body))
        self.now = now
        self.log = log
        self.css = pages.stylesheet(cfg.login_area)
        self.csp = pages.csp(self.css)
        self.picture = cfg.picture.read_bytes()

    # ---- responses -------------------------------------------------------------
    def _html(self, html: str, status: int = 200, headers: list | None = None) -> Response:
        return Response(status, html.encode(), headers=list(headers or []))

    def _redirect(self, to: str = "/", headers: list | None = None) -> Response:
        return Response(303, b"", "text/plain; charset=utf-8", [("Location", to), *(headers or [])])

    def _front(self) -> Response:
        return self._html(pages.front(self.cfg.title, self.css))

    def _title(self, session: Session | None) -> str:
        return TITLES[session.account] if session else self.cfg.title

    def _unavailable(self, why: str, session: Session | None) -> Response:
        self.log(f"service_unavailable {why}")
        return self._html(pages.unavailable(self._title(session), self.css, self.cfg.contact), 503)

    def _page(self, session: Session, content: str, dry_run: bool) -> Response:
        return self._html(pages.page(self._title(session), self.css, content, csrf=self.auth.csrf_token(session),
                                     dry_run=dry_run, contact=self.cfg.contact))

    def _cookie(self, value: str, max_age: int) -> tuple[str, str]:
        return ("Set-Cookie", f"{COOKIE}={value}; Max-Age={max_age}; Path=/; Secure; HttpOnly; SameSite=Strict")

    def security_headers(self) -> list[tuple[str, str]]:
        return [("Content-Security-Policy", self.csp), ("X-Content-Type-Options", "nosniff"),
                ("X-Frame-Options", "DENY"), ("Referrer-Policy", "no-referrer"), ("Cache-Control", "no-store")]

    # ---- the service -------------------------------------------------------------
    def _status(self, role: str) -> tuple[dict, bool]:
        """(the person's view, dry_run); raises client errors or Doubt."""
        answer = self.ask({"request": "status"})
        views, dry_run = answer.get("views"), answer.get("dry_run")
        view = views.get(role) if isinstance(views, dict) else None
        if not isinstance(view, dict) or not isinstance(dry_run, bool):
            raise Doubt("status without the view or dry_run")
        allowed = WIFE_STATES if role == "wife" else FAMILY_STATES
        if view.get("state") not in allowed:
            raise Doubt(f"unknown {role} state {view.get('state')!r}")
        for key in ("deadline", "checked_in_at"):
            if view.get(key) is not None:
                try:
                    files.parse_ts(view[key])
                except (ValueError, TypeError):
                    raise Doubt(f"{role} view {key} is not a timestamp") from None
        return view, dry_run

    def _package(self, role: str) -> dict:
        pkg = self.ask({"request": "package", "role": role})
        if not isinstance(pkg.get("transactions"), list) or not isinstance(pkg.get("documents"), list):
            raise Doubt("package without transactions or documents")
        for tx in pkg["transactions"]:
            if not isinstance(tx, dict) or not all(isinstance(tx.get(k), (str, type(None)))
                                                   for k in ("hex", "txid", "pays", "broadcast_at")):
                raise Doubt("package transaction of the wrong shape")
            if not isinstance(tx.get("confirmations"), (int, type(None))) or isinstance(tx.get("confirmations"), bool):
                raise Doubt("package confirmations of the wrong shape")
        for d in pkg["documents"]:
            if not isinstance(d, dict) or not isinstance(d.get("name"), str):
                raise Doubt("package document of the wrong shape")
        return pkg

    # ---- entry -------------------------------------------------------------------
    def handle(self, req: Request) -> Response:
        try:
            resp = self._route(req)
        except (client.Unavailable, client.Refused, Doubt) as e:
            resp = self._unavailable(f"{type(e).__name__}: {e}", self._session(req))
        resp.headers = self.security_headers() + resp.headers
        return resp

    def _session(self, req: Request) -> Session | None:
        raw = req.headers.get("cookie")
        if not raw:
            return None
        try:
            jar = SimpleCookie(raw)
        except CookieError:
            return None
        morsel = jar.get(COOKIE)
        return self.auth.session(morsel.value) if morsel else None

    def _route(self, req: Request) -> Response:
        path, method = req.path, req.method
        if method not in ("GET", "POST"):
            return Response(405, headers=[("Allow", "GET, POST")])
        if path == "/p" and method == "GET":
            return Response(200, self.picture, self.cfg.picture_type)
        if path == "/login":
            return self._login(req) if method == "POST" else self._html(pages.login(self.cfg.title, self.css, LABELS))
        known = path in ("/", "/logout", "/checkin") or TX_PATH.fullmatch(path) or DOC_PATH.fullmatch(path)
        if not known:
            return Response(404)
        session = self._session(req)
        if session is None:
            return self._redirect() if method == "POST" else self._front()
        if method == "POST" and not self.auth.csrf_ok(session, req.form.get("csrf")):
            self.log(f"csrf_refused {session.account} {req.client} {path}")
            return self._redirect()
        if path == "/logout":
            return self._redirect(headers=[self._cookie("", 0)]) if method == "POST" else self._redirect()
        if path == "/":
            return self._home(session) if method == "GET" else Response(405, headers=[("Allow", "GET")])
        if path == "/checkin":
            if session.account != "wife":
                return self._redirect()
            return self._checkin(session, req) if method == "POST" else self._checkin_confirm(session)
        if method != "GET":
            return Response(405, headers=[("Allow", "GET")])
        m = TX_PATH.fullmatch(path)
        if m:
            return self._tx(session, int(m.group(1)))
        return self._document(session, int(DOC_PATH.fullmatch(path).group(1)))

    # ---- handlers ------------------------------------------------------------------
    def _login(self, req: Request) -> Response:
        choice = req.form.get("account", "")
        result = self.auth.login(choice, req.form.get("password", ""))
        if result.outcome == "ok":
            session = self.auth.new_session(result.account)
            self.log(f"login_ok {result.account} {req.client}")
            return self._redirect(headers=[self._cookie(session.value, self.cfg.session_hours * 3600)])
        if result.outcome == "locked":
            self.log(f"login_locked {result.bucket} until {files.fmt_ts(result.locked_until)} {req.client}")
        elif result.outcome == "state_error":
            self.log(f"login_state_error {result.bucket} {req.client}: lockout file unreadable or unwritable")
        else:
            until = f" locked until {files.fmt_ts(result.locked_until)}" if result.locked_until else ""
            self.log(f"login_failed {result.bucket} failures={result.failures}{until} {req.client}")
        # A lock (this attempt started it, or it was already on) is shown with
        # its end; a state error is not a lock and gets the plain answer.
        locked_until = result.locked_until if result.outcome in ("wrong", "locked") else None
        return self._html(pages.login(self.cfg.title, self.css, LABELS, refused=True,
                                      chosen=choice if choice in LABELS else None,
                                      locked_until=locked_until, now=self.now()))

    def _home(self, session: Session) -> Response:
        view, dry_run = self._status(session.account)
        pkg_html = ""
        if view["state"] == "inheritance":
            pkg_html = pages.package(self._package(session.account))
        if session.account == "wife":
            content = pages.wife_home(view, self.now(), pkg_html)
        else:
            content = pages.family_home(view, pkg_html)
        return self._page(session, content, dry_run)

    def _may_check_in(self, view: dict) -> bool:
        return view["state"] in CHECKIN_STATES and not (view["state"] != "closed" and view.get("checked_in_at"))

    def _checkin_confirm(self, session: Session) -> Response:
        view, dry_run = self._status("wife")
        if not self._may_check_in(view):
            return self._redirect()
        return self._page(session, pages.checkin_confirm(view["state"], self.auth.csrf_token(session)), dry_run)

    def _checkin(self, session: Session, req: Request) -> Response:
        view, dry_run = self._status("wife")
        if not self._may_check_in(view):
            return self._redirect()
        if req.form.get("state") != view["state"]:
            # What the check-in does changed since she read it: show the new text.
            self.log(f"checkin_state_changed {req.form.get('state')!r} -> {view['state']} {req.client}")
            return self._page(session, pages.checkin_confirm(view["state"], self.auth.csrf_token(session)), dry_run)
        answer = self.ask({"request": "checkin", "role": "wife"})
        warning = " (service warning: tick failed after the check-in)" if answer.get("warning") else ""
        self.log(f"checkin wife at {answer.get('at')} state {answer.get('state')} {req.client}{warning}")
        return self._redirect()

    def _inheritance_package(self, session: Session) -> dict | None:
        view, _ = self._status(session.account)
        if view["state"] != "inheritance":
            return None
        return self._package(session.account)

    def _tx(self, session: Session, n: int) -> Response:
        pkg = self._inheritance_package(session)
        txs = pkg["transactions"] if pkg else []
        if not 1 <= n <= len(txs) or not txs[n - 1].get("hex"):
            return Response(404)
        self.log(f"download {session.account} transaction {n}")
        return Response(200, (txs[n - 1]["hex"] + "\n").encode(), "text/plain; charset=utf-8",
                        [("Content-Disposition", f'attachment; filename="transaction-{n}.txt"')])

    def _document(self, session: Session, n: int) -> Response:
        pkg = self._inheritance_package(session)
        docs = pkg["documents"] if pkg else []
        if not 1 <= n <= len(docs):
            return Response(404)
        name = docs[n - 1]["name"]
        answer = self.ask({"request": "document", "role": session.account, "name": name})
        try:
            data = base64.b64decode(answer.get("base64") or "", validate=True)
        except (binascii.Error, ValueError, TypeError):
            raise Doubt("document is not base64") from None
        self.log(f"download {session.account} document {n} ({len(data)} bytes)")
        return Response(200, data, "application/octet-stream",
                        [("Content-Disposition", _attachment(name, n))])


def _attachment(name: str, n: int) -> str:
    """A Content-Disposition that cannot break the header: a plain ASCII
    fallback name plus the real name percent-encoded (RFC 6266)."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)[:100] or f"document-{n}"
    return f"attachment; filename=\"{safe}\"; filename*=UTF-8''{quote(name, safe='')}"
