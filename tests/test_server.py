"""Socket interface: peer credentials, authorization, request handlers, CLI."""
import contextlib
import io
import json
import os
import pwd
import signal
import socket
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from heirdrop import cli, server as server_module
from heirdrop.statefiles import lock_exclusive
from heirdrop.server import Server, peer_uid, request
from tests.env import Env
from tests.helpers import owned_by_non_root

OWNER_UID, WEB_UID, STRANGER_UID = 1000, 1001, 1002
ROLES = {OWNER_UID: "owner", WEB_UID: "webapp"}


def ask(server: Server, uid: int, body: dict) -> dict:
    """Run one request through handle_connection with a fake peer uid."""
    a, b = socket.socketpair()
    server.peer_uid = lambda conn: uid
    with a, b:
        a.sendall(json.dumps(body).encode() + b"\n")
        server.handle_connection(b)
        a.settimeout(2)
        data = b""
        while not data.endswith(b"\n"):
            data += a.recv(65536)
    return json.loads(data)


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.server = Server(self.env.cfg, self.env.service, uid_roles=ROLES)
        for who in ("wife", "family"):
            d = self.env.cfg.documents[who]
            d.mkdir()
            (d / f"letter-{who}.txt").write_bytes(b"dear " + who.encode())
        (self.env.cfg.documents["family"] / ".hidden").write_bytes(b"x")

    def tearDown(self):
        self.env.close()

    def test_real_socket_and_peer_credentials(self):
        """Exercises SO_PEERCRED / LOCAL_PEERCRED with the test process' own uid."""
        self.env.tick_at(0)
        server = Server(self.env.cfg, self.env.service, uid_roles={os.getuid(): "owner"})
        server.listen()
        server.sock.setblocking(True)
        result = {}
        t = threading.Thread(target=lambda: result.update(request(self.env.cfg.socket_path, {"request": "status"})))
        t.start()
        conn, _ = server.sock.accept()
        with conn:
            self.assertEqual(peer_uid(conn), os.getuid())
            server.handle_connection(conn)
        t.join(5)
        server.close()
        self.assertTrue(result["ok"], result)
        self.assertEqual(set(result["views"]), {"owner", "wife", "family"})
        self.assertFalse(self.env.cfg.socket_path.exists())

    def test_serve_forever_handles_requests_and_sigterm(self):
        server = Server(self.env.cfg, self.env.service, uid_roles={os.getuid(): "owner"})
        result = {}

        def client():
            for _ in range(100):  # wait for the socket to appear
                if self.env.cfg.socket_path.exists():
                    break
                threading.Event().wait(0.05)
            result.update(request(self.env.cfg.socket_path, {"request": "checkin"}))
            os.kill(os.getpid(), signal.SIGTERM)

        t = threading.Thread(target=client)
        t.start()
        server.serve_forever()  # returns after SIGTERM
        t.join(5)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["role"], "owner")
        self.assertEqual(self.env.events("start")[0]["dry_run"], True)
        self.assertEqual(len(self.env.events("stop")), 1)
        self.assertFalse(self.env.cfg.socket_path.exists())

    def test_status_views_per_role(self):
        self.env.tick_at(0)
        owner = ask(self.server, OWNER_UID, {"request": "status"})
        web = ask(self.server, WEB_UID, {"request": "status"})
        self.assertEqual(set(owner["views"]), {"owner", "wife", "family"})
        self.assertEqual(set(web["views"]), {"wife", "family"})
        self.assertEqual(web["views"]["family"]["state"], "standby")
        self.assertTrue(web["dry_run"])

    def test_unknown_and_root_refused(self):
        for uid in (STRANGER_UID, 0):
            r = ask(self.server, uid, {"request": "status"})
            self.assertFalse(r["ok"])
            self.assertIn("forbidden", r["error"])

    def test_stranger_is_refused_without_being_read(self):
        a, b = socket.socketpair()
        self.server.peer_uid = lambda conn: STRANGER_UID
        with a, b:
            started = time.monotonic()
            self.server.handle_connection(b)       # nothing was sent: it must not wait for a request
            self.assertLess(time.monotonic() - started, 1)
            self.assertIn(b"forbidden", a.recv(65536))
        self.assertTrue(any(f"refused uid {STRANGER_UID}" in line for line in self.env.logs))
        self.assertEqual(self.env.events("request_error"), [])

    def test_slow_request_gets_one_budget_for_the_whole_line(self):
        saved = server_module.RECV_TIMEOUT
        server_module.RECV_TIMEOUT = 0.5
        self.addCleanup(setattr, server_module, "RECV_TIMEOUT", saved)
        a, b = socket.socketpair()
        self.server.peer_uid = lambda conn: OWNER_UID
        stop = threading.Event()

        def drip():                                 # always well inside the per-recv timeout
            while not stop.is_set():
                try:
                    a.sendall(b" ")
                except OSError:
                    return
                stop.wait(0.05)
        t = threading.Thread(target=drip)
        with a, b:
            t.start()
            started = time.monotonic()
            self.server.handle_connection(b)
            elapsed = time.monotonic() - started
            stop.set()
            t.join(5)
            a.settimeout(2)
            self.assertIn(b"timed out", a.recv(65536))
        self.assertLess(elapsed, 2)

    def test_bad_requests(self):
        a, b = socket.socketpair()
        self.server.peer_uid = lambda conn: OWNER_UID
        with a, b:
            a.sendall(b"not json\n")
            self.server.handle_connection(b)
            self.assertIn(b"not valid JSON", a.recv(65536))
        r = ask(self.server, OWNER_UID, {"request": "launch"})
        self.assertFalse(r["ok"])
        self.assertIn("unknown request", r["error"])
        for kind in ([], {}, 5, None):
            r = ask(self.server, OWNER_UID, {"request": kind})
            self.assertIn("request must name", r["error"])
        self.assertEqual(self.env.events("request_error"), [])
        self.server.req_status = lambda role, request: 1 / 0
        r = ask(self.server, OWNER_UID, {"request": "status"})
        self.assertEqual(r, {"ok": False, "error": "internal error"})       # no exception text to the client
        self.assertIn("ZeroDivisionError", self.env.events("request_error")[0]["error"])
        del self.server.req_status
        r = ask(self.server, OWNER_UID, {"request": "health"})
        self.assertTrue(r["ok"])
        r = ask(self.server, WEB_UID, {"request": "health"})
        self.assertFalse(r["ok"])

    def test_notify_test(self):
        self.env.tick_at(0)
        r = ask(self.server, OWNER_UID, {"request": "notify_test", "to": "wife"})
        self.assertEqual((r["ok"], r["to"], r["receipt"]), (True, "wife", None))   # events log backend
        self.assertEqual(self.env.events("alert")[-1]["alert_kind"], "test")
        for body, error in (({"request": "notify_test"}, "to must be"),
                            ({"request": "notify_test", "to": "family"}, "to must be")):
            r = ask(self.server, OWNER_UID, body)
            self.assertIn(error, r["error"])
        r = ask(self.server, WEB_UID, {"request": "notify_test", "to": "wife"})
        self.assertEqual(r, {"ok": False, "error": "forbidden: notify_test is owner only"})
        self.env.service.notifier.deliver = lambda alert, now: 1 / 0
        r = ask(self.server, OWNER_UID, {"request": "notify_test", "to": "owner"})
        self.assertFalse(r["ok"])
        self.assertIn("not accepted: ZeroDivisionError", r["error"])

    def test_checkin_roles(self):
        self.env.tick_at(0)
        r = ask(self.server, OWNER_UID, {"request": "checkin"})
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["role"], "owner")
        self.assertIsNotNone(self.env.facts().owner_checkin)
        self.assertFalse(ask(self.server, OWNER_UID, {"request": "checkin", "role": "wife"})["ok"])
        self.assertFalse(ask(self.server, WEB_UID, {"request": "checkin", "role": "owner"})["ok"])
        r = ask(self.server, WEB_UID, {"request": "checkin"})
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["role"], "wife")
        self.assertEqual(r["state"], "normal")     # her view's state, not the owner's phase
        self.assertIsNotNone(self.env.facts().wife_checkin)
        self.assertEqual(self.env.service.views()["wife"]["state"], "normal")

    def test_wife_checkin_answers_her_state_not_the_owners(self):
        self.env.tick_at(0)
        ask(self.server, OWNER_UID, {"request": "checkin"})
        self.env.run_days(1, 61)                   # owner in warning; she is not involved yet
        self.assertEqual(self.env.service.views()["owner"]["state"], "warning")
        r = ask(self.server, WEB_UID, {"request": "checkin"})
        self.assertEqual(r["state"], "normal")
        self.assertEqual(ask(self.server, OWNER_UID, {"request": "checkin"})["state"], "normal")  # owner: phase

    def test_checkin_is_acknowledged_when_the_tick_after_it_fails(self):
        self.env.tick_at(0)
        ask(self.server, OWNER_UID, {"request": "checkin"})
        self.env.clock.set(self.env.clock.now().replace(hour=13))

        def dies(now):
            raise RuntimeError("boom")
        self.env.service._check_node = dies
        r = ask(self.server, OWNER_UID, {"request": "checkin"})
        self.assertTrue(r["ok"], r)
        self.assertIn("check-in recorded", r["warning"])
        self.assertEqual(r["state"], "normal")
        self.assertEqual(self.env.store._read_ts("checkin_owner.json"), self.env.clock.now())
        self.assertEqual(self.env.events("tick_error")[0]["during"], "checkin")
        del self.env.service._check_node
        self.assertNotIn("warning", ask(self.server, OWNER_UID, {"request": "checkin"}))

    def _arm_and_decide(self, choice: str):
        self.env.tick_at(0)
        self.env.service.checkin("owner")
        if choice == "W":
            self.env.run_days(1, 80)
            self.env.service.checkin("wife")
            self.env.run_days(81, 91)
        else:
            self.env.run_days(1, 121)
        self.assertEqual(self.env.facts().decision.choice, choice)

    def test_package_before_decision(self):
        self.env.tick_at(0)
        self.env.service.checkin("owner")
        for who in ("wife", "family"):
            r = ask(self.server, WEB_UID, {"request": "package", "role": who})
            self.assertFalse(r["ok"])
            self.assertIn("not in the inheritance state", r["error"])
        r = ask(self.server, OWNER_UID, {"request": "package", "role": "family"})
        self.assertTrue(r["ok"])  # the owner may always look
        self.assertEqual(r["transactions"], [])
        self.assertEqual([d["name"] for d in r["documents"]], ["letter-family.txt"])

    def test_package_after_w(self):
        self._arm_and_decide("W")
        wife = ask(self.server, WEB_UID, {"request": "package", "role": "wife"})
        fam = ask(self.server, WEB_UID, {"request": "package", "role": "family"})
        self.assertTrue(wife["ok"] and fam["ok"], (wife, fam))
        self.assertEqual(wife["state"], "inheritance")
        self.assertEqual([t["name"] for t in wife["transactions"]], ["W1"])
        self.assertEqual(wife["transactions"][0]["hex"], "hex-W1")
        self.assertEqual(wife["transactions"][0]["txid"], "txid-W1")
        self.assertTrue(wife["transactions"][0]["dry_run"])
        self.assertEqual(wife["transactions"][0]["pays"], "bc1qwife")
        self.assertEqual([t["name"] for t in fam["transactions"]], ["W2"])
        self.assertEqual([d["name"] for d in fam["documents"]], ["letter-family.txt"])

    def test_package_after_f(self):
        self._arm_and_decide("F")
        wife = ask(self.server, WEB_UID, {"request": "package", "role": "wife"})
        self.assertFalse(wife["ok"])
        fam = ask(self.server, WEB_UID, {"request": "package", "role": "family"})
        self.assertEqual([t["name"] for t in fam["transactions"]], ["F1", "F2"])
        self.assertEqual(fam["decision"]["choice"], "F")

    def test_package_has_hex_after_restart_without_a_healthy_node(self):
        self._arm_and_decide("F")
        for attr, value in (("reachable", False), ("ibd", True)):
            with self.subTest(attr):
                setattr(self.env.node, attr, value)
                service = self.env.new_service()   # restart: nothing validated in this process
                self.assertTrue(self.env.tick_at(122).holds)
                fam = ask(Server(self.env.cfg, service, uid_roles=ROLES), WEB_UID,
                          {"request": "package", "role": "family"})
                self.assertEqual([t["hex"] for t in fam["transactions"]], ["hex-F1", "hex-F2"])
                setattr(self.env.node, attr, not value)
        self.env.corrupt_tx_file("F1")             # a tampered file is still never handed out
        self.env.node.reachable = False
        service = self.env.new_service()
        self.env.tick_at(123)
        fam = ask(Server(self.env.cfg, service, uid_roles=ROLES), WEB_UID,
                  {"request": "package", "role": "family"})
        self.assertEqual([t["hex"] for t in fam["transactions"]], [None, "hex-F2"])

    def test_document(self):
        self._arm_and_decide("F")
        r = ask(self.server, WEB_UID, {"request": "document", "role": "family", "name": "letter-family.txt"})
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["size"], len(b"dear family"))
        import base64
        self.assertEqual(base64.b64decode(r["base64"]), b"dear family")
        for bad in ("../letter-family.txt", ".hidden", "", "sub/x", None, "missing.txt"):
            r = ask(self.server, WEB_UID, {"request": "document", "role": "family", "name": bad})
            self.assertFalse(r["ok"], bad)
        r = ask(self.server, WEB_UID, {"request": "document", "role": "wife", "name": "letter-wife.txt"})
        self.assertFalse(r["ok"])  # wife is not in inheritance after F

    def test_document_size_limit(self):
        from dataclasses import replace
        self.server.cfg = replace(self.env.cfg, max_document_bytes=4)
        r = ask(self.server, OWNER_UID, {"request": "document", "role": "wife", "name": "letter-wife.txt"})
        self.assertFalse(r["ok"])
        self.assertIn("max_document_bytes", r["error"])


class CliTests(unittest.TestCase):
    def test_validate_config(self):
        env = Env()
        try:
            cfg_path = Path(env.tmp.name) / "heirdrop.toml"
            tx = "\n".join(f'{n} = {{ file = "{s.file}", sha256 = "{s.sha256}" }}'
                           for n, s in env.cfg.transactions.items())
            cfg_path.write_text(f"""
[timing]
[bitcoind]
cookie_file = "{env.tmp.name}/cookie"
[transactions]
{tx}
[addresses]
whw = "bc1qwife"
fmhw = "bc1qfamily"
[paths]
state_dir = "{env.cfg.state_dir}"
socket = "{env.cfg.socket_path}"
documents_wife = "{env.cfg.documents['wife']}"
documents_family = "{env.cfg.documents['family']}"
[interface]
owner_user = "{"nobody" if os.geteuid() == 0 else pwd.getpwuid(os.getuid()).pw_name}"
webapp_user = "daemon"
""")
            os.chmod(cfg_path, 0o644)
            owned_by_non_root(cfg_path)
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(cli.main(["validate-config", "--config", str(cfg_path), "--no-perms-check"]), 0)
                self.assertEqual(cli.main(["validate-config", "--config", str(cfg_path)]), 1)  # mode 0644 refused
                env.corrupt_tx_file("W1")
                self.assertEqual(cli.main(["validate-config", "--config", str(cfg_path), "--no-perms-check"]), 1)
            self.assertIn("W1: ok", out.getvalue())
            self.assertIn("CHECKSUM MISMATCH", out.getvalue())
            self.assertIn("readable by group or others", err.getvalue())
            # an interface user that does not exist is a problem here, not only at start
            good = cfg_path.read_text()
            cfg_path.write_text(good.replace('webapp_user = "daemon"', 'webapp_user = "no-such-user-x"'))
            out = io.StringIO()
            (env.txdir / "w1.hex").write_text(env.hex["W1"] + "\n")   # undo corrupt_tx_file
            with contextlib.redirect_stdout(out):
                self.assertEqual(cli.main(["validate-config", "--config", str(cfg_path), "--no-perms-check"]), 1)
            self.assertIn("'no-such-user-x' does not exist", out.getvalue())
            self.assertNotIn("MISMATCH", out.getvalue())
            cfg_path.write_text(good)
            # a second service on the same state directory is refused before it touches anything
            os.chmod(cfg_path, 0o600)
            fd = lock_exclusive(env.cfg.state_dir / "lock")
            events = len(env.events())
            err = io.StringIO()
            # not root, or `run` refuses before it reaches the lock (tests may run as root)
            with mock.patch("os.geteuid", return_value=1000), contextlib.redirect_stderr(err):
                self.assertEqual(cli.main(["run", "--config", str(cfg_path)]), 1)
            self.assertIn("already running", err.getvalue())
            self.assertEqual(len(env.events()), events)
            self.assertFalse(env.cfg.socket_path.exists())
            os.close(fd)
        finally:
            env.close()


if __name__ == "__main__":
    unittest.main()
