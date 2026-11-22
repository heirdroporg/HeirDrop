"""Local interface: a Unix domain socket, one JSON request and one JSON
response per connection, each a single line. Callers are identified by the
kernel's peer credentials (uid), never by anything they send.

Roles:
  owner  (config owner_user)  check in as owner, read everything, send a test message
  webapp (config webapp_user) check in as wife, read wife and family views,
                              fetch the wife's or the family's package
root and unknown users are refused.
"""
from __future__ import annotations

import base64
import json
import os
import pwd
import selectors
import signal
import socket
import struct
import sys
import time
from pathlib import Path

from . import txcheck
from .clock import fmt_ts
from .config import PAIRS, Config
from .service import Service

MAX_REQUEST_BYTES = 65536
RECV_TIMEOUT = 5.0  # for the whole request line, not per recv()
SEND_TIMEOUT = 60.0


class RequestError(Exception):
    pass


# ---- peer credentials ------------------------------------------------------

def peer_uid(conn: socket.socket) -> int:
    """The uid of the process at the other end of a Unix socket."""
    if sys.platform.startswith("linux"):
        data = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", data)
        return uid
    if sys.platform == "darwin":  # struct xucred: version, uid, ngroups, groups[16]
        sol_local = getattr(socket, "SOL_LOCAL", 0)
        local_peercred = getattr(socket, "LOCAL_PEERCRED", 1)
        data = conn.getsockopt(sol_local, local_peercred, struct.calcsize("IIh16I"))
        _version, uid = struct.unpack_from("II", data)
        return uid
    raise OSError(f"peer credentials are not supported on {sys.platform}")


def uid_roles_from_config(cfg: Config) -> dict[int, str]:
    roles = {}
    for user, role in ((cfg.owner_user, "owner"), (cfg.webapp_user, "webapp")):
        try:
            uid = pwd.getpwnam(user).pw_uid
        except KeyError:
            raise RuntimeError(f"[interface] user {user!r} does not exist on this system") from None
        if uid == 0:
            raise RuntimeError(f"[interface] {role} user must not be root")
        roles[uid] = role
    return roles


# ---- the server -------------------------------------------------------------

class Server:
    def __init__(self, cfg: Config, service: Service, uid_roles: dict[int, str] | None = None,
                 peer_uid_fn=peer_uid):
        self.cfg = cfg
        self.service = service
        self.uid_roles = uid_roles if uid_roles is not None else uid_roles_from_config(cfg)
        self.peer_uid = peer_uid_fn
        self.sock: socket.socket | None = None
        self.stopping = False

    # -- lifecycle --
    def listen(self) -> None:
        path = self.cfg.socket_path
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.unlink()  # stale socket from a previous run
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(str(path))
        os.chmod(path, 0o666)  # anyone may connect; peer credentials decide what they get
        self.sock.listen(8)
        self.sock.setblocking(False)

    def close(self) -> None:
        if self.sock is not None:
            self.sock.close()
            self.sock = None
        try:
            self.cfg.socket_path.unlink()
        except OSError:
            pass

    def serve_forever(self) -> None:
        """Start the service, then alternate between ticks and requests."""
        interval = self.cfg.timing.tick_interval.total_seconds()
        self.service.start()
        self.listen()
        sel = selectors.DefaultSelector()
        sel.register(self.sock, selectors.EVENT_READ, "accept")
        wake_r, wake_w = socket.socketpair()
        wake_r.setblocking(False)
        wake_w.setblocking(False)
        signal.set_wakeup_fd(wake_w.fileno())
        sel.register(wake_r, selectors.EVENT_READ, "wakeup")
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, self._on_signal)
        next_tick = time.monotonic() + interval
        try:
            while not self.stopping:
                for key, _ in sel.select(max(0.0, next_tick - time.monotonic())):
                    if key.data == "wakeup":
                        wake_r.recv(1024)
                        continue
                    try:
                        conn, _ = self.sock.accept()
                    except OSError:
                        continue
                    with conn:
                        self.handle_connection(conn)
                if time.monotonic() >= next_tick:
                    self.service.safe_tick()
                    next_tick = time.monotonic() + interval
        finally:
            self.service.log(self.service.clock.now(), "stop")
            self.close()

    def _on_signal(self, signum, _frame) -> None:
        self.stopping = True

    # -- one connection --
    def handle_connection(self, conn: socket.socket) -> None:
        try:
            uid = self.peer_uid(conn)
            role = self.uid_roles.get(uid)
            if role is None:
                # Refused before anything is read: a stranger gets no time on the loop.
                self.service.note(f"refused uid {uid}")
                raise RequestError("forbidden: unknown user")
            line = _read_line(conn, time.monotonic() + RECV_TIMEOUT)
            try:
                request = json.loads(line)
            except ValueError:
                raise RequestError("request is not valid JSON") from None
            if not isinstance(request, dict):
                raise RequestError("request must be a JSON object")
            response = self.dispatch(role, request)
            response["ok"] = True
        except RequestError as e:
            response = {"ok": False, "error": str(e)}
        except Exception as e:  # noqa: BLE001 - never let one request kill the daemon
            self.service.log(self.service.clock.now(), "request_error", error=repr(e))
            response = {"ok": False, "error": "internal error"}  # details are in the events log only
        conn.settimeout(SEND_TIMEOUT)
        try:
            conn.sendall(json.dumps(response).encode() + b"\n")
        except OSError:
            pass

    # -- requests --
    def dispatch(self, role: str, request: dict) -> dict:
        kind = request.get("request")
        if not isinstance(kind, str):
            raise RequestError("request must name what is asked, e.g. {\"request\": \"status\"}")
        handler = {"status": self.req_status, "checkin": self.req_checkin, "package": self.req_package,
                   "document": self.req_document, "health": self.req_health,
                   "notify_test": self.req_notify_test}.get(kind)
        if handler is None:
            raise RequestError(f"unknown request {kind!r}")
        return handler(role, request)

    def req_status(self, role: str, request: dict) -> dict:
        views = self.service.views()
        if role != "owner":
            views = {k: v for k, v in views.items() if k in ("wife", "family")}
        return {"now": fmt_ts(self.service.clock.now()), "dry_run": self.cfg.dry_run, "views": views}

    def req_checkin(self, role: str, request: dict) -> dict:
        who = request.get("role", "owner" if role == "owner" else "wife")
        allowed = {"owner": "owner", "webapp": "wife"}[role]
        if who != allowed:
            raise RequestError(f"forbidden: {role} may only check in as {allowed}")
        result = self.service.checkin(who)
        if who == "wife":
            # The web app gets her own state, never the owner's phase.
            result["state"] = self.service.views().get("wife", {}).get("state")
        result["dry_run"] = self.cfg.dry_run
        return result

    def req_health(self, role: str, request: dict) -> dict:
        if role != "owner":
            raise RequestError("forbidden: health is owner only")
        return self.service.health()

    def req_notify_test(self, role: str, request: dict) -> dict:
        if role != "owner":
            raise RequestError("forbidden: notify_test is owner only")
        to = request.get("to")
        if to not in ("owner", "wife"):
            raise RequestError("to must be 'owner' or 'wife'")
        result = self.service.notify_test(to)
        if not result["accepted"]:
            raise RequestError(f"not accepted: {result['error']}")
        del result["accepted"]
        return result

    def _beneficiary(self, role: str, request: dict) -> str:
        who = request.get("role")
        if who not in ("wife", "family"):
            raise RequestError("role must be 'wife' or 'family'")
        if role == "owner":
            return who  # the owner may inspect either package at any time
        decision = self.service.facts.decision
        if decision is None or (who == "wife" and decision.choice != "W"):
            raise RequestError(f"no package: {who} is not in the inheritance state")
        return who

    def _documents(self, who: str) -> list[dict]:
        d = self.cfg.documents[who]
        if not d.is_dir():
            return []
        out = []
        for p in sorted(d.iterdir()):
            if p.name.startswith(".") or not p.is_file():
                continue
            out.append({"name": p.name, "size": p.stat().st_size})
        return out

    def req_package(self, role: str, request: dict) -> dict:
        who = self._beneficiary(role, request)
        facts = self.service.facts
        decision = facts.decision
        names: tuple[str, ...] = ()
        if decision is not None:
            names = ("W1",) if who == "wife" else tuple(n for n in PAIRS[decision.choice] if n != "W1")
        txs = []
        for name in names:
            # tx_hexes is only filled by a validation run, which needs a healthy
            # node; an heir must get the hex even if there never was one.
            hexstr = self.service.tx_hexes.get(name) or txcheck.read_tx_file(self.cfg.transactions[name])
            report = self.service.tx_reports.get(name)
            bc = facts.broadcasts.get(name)
            track = self.service.tracking.get(name, {})
            txs.append({
                "name": name,
                "hex": hexstr,
                "txid": bc.txid if bc else (report.txid if report else None),
                "broadcast_at": fmt_ts(bc.at) if bc else None,
                "dry_run": bc.dry_run if bc else None,
                "confirmations": track.get("confirmations"),
                "pays": self.cfg.whw_address if name == "W1" else self.cfg.fmhw_address,
            })
        views = self.service.views()
        return {
            "role": who,
            "state": views.get(who, {}).get("state"),
            "decision": {"choice": decision.choice, "at": fmt_ts(decision.at)} if decision else None,
            "final_at": fmt_ts(facts.final_at) if facts.final_at else None,
            "transactions": txs,
            "documents": self._documents(who),
        }

    def req_document(self, role: str, request: dict) -> dict:
        who = self._beneficiary(role, request)
        name = request.get("name")
        if not isinstance(name, str) or not name or name != os.path.basename(name) or name.startswith("."):
            raise RequestError("name must be a plain file name")
        path = self.cfg.documents[who] / name
        if not path.is_file():
            raise RequestError("no such document")
        size = path.stat().st_size
        if size > self.cfg.max_document_bytes:
            raise RequestError(f"document larger than max_document_bytes ({size} > {self.cfg.max_document_bytes})")
        data = path.read_bytes()
        return {"role": who, "name": name, "size": len(data), "base64": base64.b64encode(data).decode()}


def _read_line(conn: socket.socket, deadline: float) -> bytes:
    """Read one line. `deadline` (time.monotonic) bounds the whole read, so a
    client sending a byte at a time cannot keep the loop from ticking."""
    buf = bytearray()
    while b"\n" not in buf:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RequestError("timed out waiting for the request line")
        conn.settimeout(remaining)
        try:
            chunk = conn.recv(4096)
        except socket.timeout:
            raise RequestError("timed out waiting for the request line") from None
        if not chunk:
            break
        buf += chunk
        if len(buf) > MAX_REQUEST_BYTES:
            raise RequestError("request too long")
    if not buf.strip():
        raise RequestError("empty request")
    return bytes(buf.split(b"\n", 1)[0])


# ---- client side -------------------------------------------------------------

def request(socket_path: str | os.PathLike, body: dict, timeout: float = 120.0) -> dict:
    """Send one request to a running service and return its response dict."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(str(Path(socket_path)))
        s.sendall(json.dumps(body).encode() + b"\n")
        buf = bytearray()
        while True:
            chunk = s.recv(1 << 16)
            if not chunk:
                break
            buf += chunk
            if buf.endswith(b"\n"):
                break
    if not buf:
        raise ConnectionError("service closed the connection without a response")
    return json.loads(buf.decode())
