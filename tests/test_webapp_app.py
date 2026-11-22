import base64
import json
import os
import re
import shutil
import socket
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests.test_webapp_auth import FAMILY_PW, WIFE_PW, Clock, make_cfg
from webapp import auth, client, pages
from webapp.app import App, Request

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
JPEG = b"\xff\xd8\xff\xe0picture"
HEX_W1 = "0200000001" + "ab" * 40
HEX_F1, HEX_F2 = "0200000001" + "cd" * 40, "0200000001" + "ef" * 40


class FakeService:
    """Answers like the heirdrop socket does for the web app's uid."""

    def __init__(self):
        self.wife = {"state": "normal", "deadline": None, "checked_in_at": None}
        self.family = {"state": "standby"}
        self.dry_run = False
        self.down = False
        self.requests = []
        self.packages = {}
        self.documents = {}
        self.checkins = 0
        self.answer_override = None

    def __call__(self, body):
        self.requests.append(body)
        if self.down:
            raise client.Unavailable("socket down")
        if self.answer_override is not None:
            return self.answer_override
        kind = body["request"]
        if kind == "status":
            return {"ok": True, "now": "x", "dry_run": self.dry_run,
                    "views": {"wife": dict(self.wife), "family": dict(self.family)}}
        if kind == "checkin":
            assert body.get("role") == "wife"
            self.checkins += 1
            return {"ok": True, "role": "wife", "at": "2026-01-01T12:00:00Z", "state": self.wife["state"]}
        if kind == "package":
            if body["role"] not in self.packages:
                raise client.Refused("no package")
            return {"ok": True, **self.packages[body["role"]]}
        if kind == "document":
            data = self.documents[(body["role"], body["name"])]
            return {"ok": True, "name": body["name"], "size": len(data), "base64": base64.b64encode(data).decode()}
        raise AssertionError(kind)

    def decide_w(self):
        self.wife = {"state": "inheritance", "deadline": None, "checked_in_at": "2026-03-20T12:00:00Z"}
        self.family = {"state": "inheritance"}
        self.packages["wife"] = {"transactions": [
            {"name": "W1", "hex": HEX_W1, "txid": "aa" * 32, "broadcast_at": "2026-04-01T12:00:00Z",
             "dry_run": False, "confirmations": 3, "pays": "bc1qwife"}],
            "documents": [{"name": "letter to you.pdf", "size": 5}], "final_at": None}
        self.documents[("wife", "letter to you.pdf")] = b"%PDF1"

    def decide_f(self):
        self.wife = {"state": "closed", "deadline": None, "checked_in_at": None}
        self.family = {"state": "inheritance"}
        self.packages["family"] = {"transactions": [
            {"name": "F1", "hex": HEX_F1, "txid": "f1" * 32, "broadcast_at": None, "dry_run": None,
             "confirmations": None, "pays": "bc1qfam"},
            {"name": "F2", "hex": HEX_F2, "txid": "f2" * 32, "broadcast_at": None, "dry_run": None,
             "confirmations": None, "pays": "bc1qfam"}],
            "documents": [], "final_at": None}


class AppCase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        pic = self.dir / "picture.jpg"
        pic.write_bytes(JPEG)
        cfg = make_cfg(self.dir)
        cfg = cfg.__class__(**{**cfg.__dict__, "picture": pic, "contact": "Call Anna: 555 0100"})
        self.clock = Clock()
        self.svc = FakeService()
        self.lines = []
        self.app = App(cfg, auth.Auth(cfg, self.clock), ask=self.svc, now=self.clock, log=self.lines.append)
        self.cookie = None

    def req(self, method, path, form=None, cookie="session"):
        headers = {}
        c = self.cookie if cookie == "session" else cookie
        if c:
            headers["cookie"] = f"s={c}"
        return self.app.handle(Request(method, path, headers, form or {}, "203.0.113.9"))

    def html(self, resp):
        return resp.body.decode()

    def login(self, name="WFE", pw=WIFE_PW):
        resp = self.req("POST", "/login", {"account": name, "password": pw}, cookie=None)
        self.assertEqual(resp.status, 303, self.html(resp))
        cookie = dict(resp.headers)["Set-Cookie"]
        self.assertIn("Secure; HttpOnly; SameSite=Strict", cookie)
        self.cookie = cookie.split(";")[0].split("=", 1)[1]
        return resp

    def csrf(self):
        page = self.html(self.req("GET", "/"))
        return re.search(r'name="csrf" value="([^"]+)"', page).group(1)

    def text(self, resp):
        """Visible text of a page, roughly."""
        body = re.sub(r"<style>.*?</style>", "", self.html(resp), flags=re.S)
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body)).strip()


class FrontAndLoginTests(AppCase):
    def test_front_page_has_no_text(self):
        resp = self.req("GET", "/", cookie=None)
        self.assertEqual(resp.status, 200)
        page = self.html(resp)
        self.assertEqual(self.text(resp), "Family")      # only the <title>
        self.assertIn('<img src="/p" alt="">', page)
        self.assertIn('<a href="/login"', page)
        self.assertIn(".pic a{left:70%;top:75%;width:20%;height:15%}", page)
        for word in ("bitcoin", "inherit", "dead", "wallet", "transfer", "login", "password"):
            outside_css = re.sub(r"<style>.*?</style>", "", page, flags=re.S).lower()
            self.assertNotIn(word, outside_css.replace('href="/login"', ""), word)
        self.assertEqual(self.svc.requests, [])          # the service is not asked

    def test_picture(self):
        resp = self.req("GET", "/p", cookie=None)
        self.assertEqual((resp.status, resp.body, resp.content_type), (200, JPEG, "image/jpeg"))

    def test_security_headers_everywhere(self):
        for path in ("/", "/p", "/login", "/nothing"):
            headers = dict(self.req("GET", path, cookie=None).headers)
            csp = headers["Content-Security-Policy"]
            self.assertIn("default-src 'none'", csp)
            self.assertIn("frame-ancestors 'none'", csp)
            self.assertNotIn("script", csp)
            self.assertEqual(headers["X-Frame-Options"], "DENY")
            self.assertEqual(headers["Cache-Control"], "no-store")

    def test_csp_hash_matches_the_stylesheet(self):
        import hashlib
        page = self.html(self.req("GET", "/", cookie=None))
        css = re.search(r"<style>(.*?)</style>", page, re.S).group(1)
        digest = base64.b64encode(hashlib.sha256(css.encode()).digest()).decode()
        self.assertIn(f"'sha256-{digest}'", dict(self.req("GET", "/", cookie=None).headers)["Content-Security-Policy"])

    def test_unknown_paths_and_methods(self):
        for path in ("/admin", "/login/", "/p.jpg", "/tx/0.txt", "/doc/x", "/../etc/passwd", "/favicon.ico"):
            resp = self.req("GET", path, cookie=None)
            self.assertEqual((resp.status, resp.body), (404, b""), path)
        self.assertEqual(self.req("PUT", "/", cookie=None).status, 405)

    def test_pages_need_a_session(self):
        for path in ("/checkin", "/tx/1.txt", "/doc/1"):
            resp = self.req("GET", path, cookie=None)
            self.assertEqual(self.text(resp), "Family", path)   # the front page
        for path in ("/checkin", "/logout"):
            self.assertEqual(self.req("POST", path, {"csrf": "x"}, cookie=None).status, 303)
        self.assertEqual(self.svc.checkins, 0)
        self.assertEqual(self.text(self.req("GET", "/", cookie="garbage")), "Family")
        self.assertEqual(self.text(self.req("GET", "/", cookie='"unclosed')), "Family")

    def test_login_form(self):
        html = self.html(self.req("GET", "/login", cookie=None))
        self.assertIn('<select id="u" name="account" required><option value="">choose\u2026</option>'
                      '<option value="WFE">WFE</option><option value="FML">FML</option></select>', html)
        self.assertNotIn("wife", html.lower())
        self.assertNotIn("family", html.lower().replace("<title>family</title>", ""))
        self.assertNotIn(" disabled>", html)
        self.assertNotIn("refresh", html)

    def test_title_names_the_account_after_the_login_only(self):
        title = lambda resp: re.search(r"<title>(.*?)</title>", self.html(resp)).group(1)
        self.assertEqual(title(self.req("GET", "/", cookie=None)), "Family")      # neutral before the login
        self.assertEqual(title(self.req("GET", "/login", cookie=None)), "Family")
        self.login()
        self.assertEqual(title(self.req("GET", "/")), "WIFE")
        self.svc.wife = {"state": "grace", "deadline": "2026-01-05T12:00:00Z", "checked_in_at": None}
        self.assertEqual(title(self.req("GET", "/checkin")), "WIFE")
        self.svc.down = True
        self.assertEqual(title(self.req("GET", "/")), "WIFE")                     # "not reachable" too
        self.assertEqual(title(self.req("GET", "/", cookie=None)), "Family")
        self.svc.down = False
        self.login("FML", FAMILY_PW)
        self.assertEqual(title(self.req("GET", "/")), "FAMILY")

    def test_wrong_and_right_login(self):
        resp = self.req("POST", "/login", {"account": "WFE", "password": "0000111122223333"}, cookie=None)
        self.assertEqual(resp.status, 200)
        self.assertIn("Not accepted. Try again later.", self.text(resp))
        self.assertIn('<option value="WFE" selected>', self.html(resp))   # her choice is kept
        self.assertNotIn(" disabled>", self.html(resp))
        self.assertNotIn("Set-Cookie", dict(resp.headers))
        other = self.req("POST", "/login", {"account": "wife", "password": WIFE_PW}, cookie=None)
        self.assertEqual(other.status, 200)                    # not a dropdown value: never logs in
        self.assertNotIn(" selected", self.html(other))
        self.assertIn("login_failed wife failures=1 203.0.113.9", self.lines)
        self.assertIn("login_failed unknown failures=1 203.0.113.9", self.lines)
        self.login()
        self.assertIn("login_ok wife 203.0.113.9", self.lines)
        self.assertFalse(any(WIFE_PW in line for line in self.lines))
        self.login("FML", FAMILY_PW)
        self.assertIn("login_ok family 203.0.113.9", self.lines)

    def test_lock_is_shown_with_its_end_and_the_button_disabled(self):
        wrong = lambda: self.req("POST", "/login", {"account": "WFE", "password": "0000111122223333"}, cookie=None)
        wrong(), wrong()
        third = self.html(wrong())                              # this attempt starts the lock
        self.assertIn("Too many attempts. Try again after 12:10 UTC.", third)
        self.assertIn('<button type="submit" disabled>OK</button>', third)
        self.assertIn('<meta http-equiv="refresh" content="601;url=/login">', third)
        self.assertIn('<option value="WFE" selected>', third)
        self.assertNotIn("Not accepted", third)
        self.clock.advance(minutes=4, seconds=30)
        locked = self.html(self.req("POST", "/login", {"account": "WFE", "password": WIFE_PW}, cookie=None))
        self.assertIn("Try again after 12:10 UTC.", locked)    # even the right password, while locked
        self.assertIn('content="331;url=/login"', locked)
        self.assertTrue(any(line.startswith("login_locked wife until 2026-01-01T12:10:00Z") for line in self.lines))
        fml = self.html(self.req("POST", "/login", {"account": "FML", "password": "0000111122223333"}, cookie=None))
        self.assertIn("Not accepted. Try again later.", fml)    # the family member's own counter
        self.assertNotIn(" disabled>", fml)
        self.clock.advance(minutes=5, seconds=30)
        self.assertNotIn(" disabled>", self.html(self.req("GET", "/login", cookie=None)))
        self.login()                                            # lock over

    def test_lock_end_is_rounded_up_to_the_minute(self):
        html = pages.login("T", "", ("WFE",), refused=True, locked_until=T0 + timedelta(minutes=10, seconds=1),
                           now=T0)
        self.assertIn("Try again after 12:11 UTC.", html)
        self.assertIn('content="602;url=/login"', html)
        ended = pages.login("T", "", ("WFE",), refused=True, locked_until=T0, now=T0)
        self.assertIn("Not accepted. Try again later.", ended)
        self.assertNotIn(" disabled>", ended)

    def test_state_error_is_not_shown_as_a_lock(self):
        (self.dir / "lockout-wife.json").write_text("{")
        html = self.html(self.req("POST", "/login", {"account": "WFE", "password": WIFE_PW}, cookie=None))
        self.assertIn("Not accepted. Try again later.", html)
        self.assertNotIn(" disabled>", html)

    def test_logout(self):
        self.login()
        resp = self.req("POST", "/logout", {"csrf": self.csrf()})
        self.assertEqual(resp.status, 303)
        self.assertIn("s=; Max-Age=0", dict(resp.headers)["Set-Cookie"])

    def test_session_expires(self):
        self.login()
        self.clock.advance(hours=8)
        self.assertEqual(self.text(self.req("GET", "/")), "Family")


class WifeTests(AppCase):
    def setUp(self):
        super().setUp()
        self.login()

    def test_normal(self):
        resp = self.req("GET", "/")
        self.assertIn("Nothing to do.", self.text(resp))
        self.assertNotIn("/checkin", self.html(resp))
        self.assertIn("Call Anna: 555 0100", self.text(resp))
        self.assertIn("Log out", self.text(resp))
        self.assertNotIn("Rehearsal", self.text(resp))
        self.assertEqual(self.req("GET", "/checkin").status, 303)   # no confirm page in normal
        self.assertEqual(self.req("POST", "/checkin", {"csrf": self.csrf(), "state": "normal"}).status, 303)
        self.assertEqual(self.svc.checkins, 0)

    def test_rehearsal_banner(self):
        self.svc.dry_run = True
        self.assertIn("Rehearsal: nothing is real.", self.text(self.req("GET", "/")))

    def test_grace_check_in(self):
        self.svc.wife = {"state": "grace", "deadline": "2026-05-01T12:00:00Z", "checked_in_at": None}
        page = self.text(self.req("GET", "/"))
        self.assertIn("Please check in by 01 May 2026, 12:00 UTC.", page)
        self.assertNotIn("delayed", page)
        confirm = self.req("GET", "/checkin")
        self.assertIn("the transfer goes to you", self.text(confirm))
        token = re.search(r'name="csrf" value="([^"]+)"', self.html(confirm)).group(1)
        self.assertEqual(self.req("POST", "/checkin", {"csrf": token, "state": "grace"}).status, 303)
        self.assertEqual(self.svc.checkins, 1)
        self.assertTrue(any(line.startswith("checkin wife at") for line in self.lines))
        self.svc.wife["checked_in_at"] = "2026-03-20T12:00:00Z"
        page = self.text(self.req("GET", "/"))
        self.assertIn("Your check-in is recorded (20 March 2026, 12:00 UTC).", page)
        self.assertNotIn("Please check in", page)
        self.assertEqual(self.req("POST", "/checkin", {"csrf": token, "state": "grace"}).status, 303)
        self.assertEqual(self.svc.checkins, 1)                     # once is enough

    def test_check_in_needs_the_csrf_token(self):
        self.svc.wife = {"state": "grace", "deadline": "2026-05-01T12:00:00Z", "checked_in_at": None}
        for form in ({"state": "grace"}, {"csrf": "forged", "state": "grace"}):
            self.assertEqual(self.req("POST", "/checkin", form).status, 303)
        self.assertEqual(self.svc.checkins, 0)
        self.assertTrue(any(line.startswith("csrf_refused wife") for line in self.lines))

    def test_changed_state_shows_the_new_effect_first(self):
        self.svc.wife = {"state": "wife_window", "deadline": "2026-05-01T12:00:00Z", "checked_in_at": None}
        resp = self.req("POST", "/checkin", {"csrf": self.csrf(), "state": "grace"})  # she read the grace text
        self.assertEqual(resp.status, 200)
        self.assertIn("decided at once. This cannot be undone.", self.text(resp))
        self.assertEqual(self.svc.checkins, 0)
        self.assertEqual(self.req("POST", "/checkin", {"csrf": self.csrf(), "state": "wife_window"}).status, 303)
        self.assertEqual(self.svc.checkins, 1)

    def test_delayed(self):
        self.svc.wife = {"state": "wife_window", "deadline": "2026-01-01T11:00:00Z", "checked_in_at": None}
        self.assertIn("may be delayed", self.text(self.req("GET", "/")))

    def test_inheritance_package_and_downloads(self):
        self.svc.decide_w()
        page = self.text(self.req("GET", "/"))
        self.assertIn("The transfer to you has started.", page)
        self.assertIn("Sent, 3 confirmations.", page)
        self.assertIn("bc1qwife", page)
        self.assertIn(HEX_W1, self.html(self.req("GET", "/")))
        tx = self.req("GET", "/tx/1.txt")
        self.assertEqual((tx.status, tx.body), (200, (HEX_W1 + "\n").encode()))
        self.assertIn('filename="transaction-1.txt"', dict(tx.headers)["Content-Disposition"])
        self.assertEqual(self.req("GET", "/tx/2.txt").status, 404)
        doc = self.req("GET", "/doc/1")
        self.assertEqual((doc.status, doc.body), (200, b"%PDF1"))
        self.assertEqual(dict(doc.headers)["Content-Disposition"],
                         "attachment; filename=\"letter_to_you.pdf\"; filename*=UTF-8''letter%20to%20you.pdf")
        self.assertEqual(self.req("GET", "/doc/2").status, 404)
        self.svc.packages["wife"]["final_at"] = "2026-04-02T12:00:00Z"
        self.assertIn("Confirmed. Final.", self.text(self.req("GET", "/")))

    def test_no_downloads_before_inheritance(self):
        self.svc.packages["wife"] = {"transactions": [{"hex": HEX_W1}], "documents": [{"name": "x"}]}
        self.assertEqual(self.req("GET", "/tx/1.txt").status, 404)
        self.assertEqual(self.req("GET", "/doc/1").status, 404)
        self.assertFalse([r for r in self.svc.requests if r["request"] != "status"])

    def test_closed_then_false_triggered(self):
        self.svc.decide_f()
        page = self.text(self.req("GET", "/"))
        self.assertIn("The family transfer has started.", page)
        self.assertIn("Check in anyway", page)
        self.assertIn("changes nothing", self.text(self.req("GET", "/checkin")))
        self.assertEqual(self.req("POST", "/checkin", {"csrf": self.csrf(), "state": "closed"}).status, 303)
        self.assertEqual(self.svc.checkins, 1)
        self.svc.wife = {"state": "false_triggered", "deadline": None, "checked_in_at": "2026-05-03T12:00:00Z"}
        page = self.text(self.req("GET", "/"))
        self.assertIn("Please contact the family member.", page)
        self.assertNotIn("Check in", page)
        self.assertEqual(self.req("GET", "/tx/1.txt").status, 404)   # the family's package is not hers

    def test_service_down_or_odd(self):
        self.svc.down = True
        resp = self.req("GET", "/")
        self.assertEqual(resp.status, 503)
        self.assertIn("Not reachable right now. Try again later.", self.text(resp))
        self.assertIn("Call Anna", self.text(resp))
        self.assertNotIn("socket", self.text(resp))
        self.assertTrue(any(line.startswith("service_unavailable Unavailable") for line in self.lines))
        self.svc.down = False
        for odd in ({"ok": True}, {"ok": True, "dry_run": False, "views": {"wife": {"state": "weird"}}},
                    {"ok": True, "dry_run": "no", "views": {"wife": {"state": "normal"}}},
                    {"ok": True, "dry_run": False, "views": {"wife": {"state": "grace", "deadline": 5}}},
                    {"ok": True, "dry_run": False, "views": {"wife": {"state": "grace", "deadline": "soon"}}}):
            self.svc.answer_override = odd
            self.assertEqual(self.req("GET", "/").status, 503, odd)
        self.svc.answer_override = None
        self.svc.decide_w()
        self.svc.packages["wife"]["transactions"][0]["confirmations"] = "3"
        self.assertEqual(self.req("GET", "/").status, 503)

    def test_escaping(self):
        self.svc.decide_w()
        self.svc.packages["wife"]["documents"] = [{"name": "<script>x</script>.pdf", "size": 1}]
        page = self.html(self.req("GET", "/"))
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;script&gt;", page)


class FamilyTests(AppCase):
    def setUp(self):
        super().setUp()
        self.login("FML", FAMILY_PW)

    def test_as_little_as_possible_before_a_decision(self):
        pages = set()
        for state in ("standby", "alert"):
            self.svc.family = {"state": state}
            pages.add(self.text(self.req("GET", "/")))
        self.assertEqual(len(pages), 1)                 # standby and alert look the same
        self.assertIn("Nothing to do.", pages.pop())

    def test_no_check_in(self):
        self.svc.wife = {"state": "grace", "deadline": "2026-05-01T12:00:00Z", "checked_in_at": None}
        self.assertEqual(self.req("GET", "/checkin").status, 303)
        self.assertEqual(self.req("POST", "/checkin", {"csrf": self.csrf(), "state": "grace"}).status, 303)
        self.assertEqual(self.svc.checkins, 0)

    def test_inheritance(self):
        self.svc.decide_f()
        page = self.text(self.req("GET", "/"))
        self.assertIn("The transfer has started.", page)
        self.assertIn("Transaction 1 of 2", page)
        self.assertIn("Not sent yet.", page)
        self.assertEqual(self.req("GET", "/tx/2.txt").body, (HEX_F2 + "\n").encode())
        self.assertEqual([r["role"] for r in self.svc.requests if r["request"] == "package"], ["family", "family"])

    def test_never_asks_for_the_wifes_package(self):
        self.svc.decide_w()
        self.svc.packages["family"] = {"transactions": [], "documents": [], "final_at": None}
        self.req("GET", "/")
        self.req("GET", "/tx/1.txt")
        self.assertNotIn("wife", [r.get("role") for r in self.svc.requests])


class ClientTests(unittest.TestCase):
    def serve(self, answer: bytes):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        path = Path(d) / "s.sock"
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(path))
        srv.listen(1)
        self.addCleanup(srv.close)
        got = []

        def run():
            conn, _ = srv.accept()
            with conn:
                got.append(conn.makefile("rb").readline())
                if answer:
                    conn.sendall(answer)
        threading.Thread(target=run, daemon=True).start()
        return path, got

    def test_round_trip(self):
        path, got = self.serve(b'{"ok": true, "views": {}}\n')
        self.assertEqual(client.ask(path, {"request": "status"}), {"ok": True, "views": {}})
        self.assertEqual(json.loads(got[0]), {"request": "status"})

    def test_refused(self):
        path, _ = self.serve(b'{"ok": false, "error": "forbidden"}\n')
        with self.assertRaises(client.Refused):
            client.ask(path, {"request": "health"})

    def test_garbage_and_silence(self):
        for answer in (b"not json\n", b"[1]\n", b'{"views": 1}\n', b""):
            path, _ = self.serve(answer)
            with self.assertRaises(client.Unavailable, msg=answer):
                client.ask(path, {"request": "status"}, timeout=2)

    def test_too_large(self):
        path, _ = self.serve(b'{"ok": true, "x": "' + b"a" * 5000 + b'"}\n')
        with self.assertRaises(client.Unavailable):
            client.ask(path, {"request": "status"}, max_bytes=1000)

    def test_no_socket(self):
        with self.assertRaises(client.Unavailable):
            client.ask(Path("/nonexistent/heirdrop.sock"), {"request": "status"})


if __name__ == "__main__":
    unittest.main()
