import contextlib
import http.client
import io
import threading
import unittest
from unittest import mock

from tests.test_webapp_app import AppCase
from tests.test_webapp_auth import WIFE_PW
from webapp import cli, server
from webapp.app import Response


class HttpCase(AppCase):
    def setUp(self):
        super().setUp()
        self.httpd = server.make_server(self.app, "127.0.0.1", 0)
        self.port = self.httpd.server_address[1]
        t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        t.start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.stderr = io.StringIO()
        patcher = contextlib.redirect_stderr(self.stderr)
        patcher.__enter__()
        self.addCleanup(patcher.__exit__, None, None, None)

    def http(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            return resp.status, dict(resp.getheaders()), resp.read()
        finally:
            conn.close()

    def post(self, path, body, cookie=None, extra=None):
        headers = {"Content-Type": "application/x-www-form-urlencoded", **(extra or {})}
        if cookie:
            headers["Cookie"] = f"s={cookie}"
        return self.http("POST", path, body, headers)


class ServerTests(HttpCase):
    def test_front_page_over_http(self):
        status, headers, body = self.http("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b'<img src="/p"', body)
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        self.assertEqual(headers["Server"], "web")
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertEqual(int(headers["Content-Length"]), len(body))

    def test_query_string_is_ignored(self):
        self.assertEqual(self.http("GET", "/?x=1")[0], 200)
        self.assertEqual(self.http("GET", "/p?v=2")[2][:3], b"\xff\xd8\xff")

    def test_login_and_view(self):
        status, headers, _ = self.post("/login", f"account=WFE&password={WIFE_PW}")
        self.assertEqual((status, headers["Location"]), (303, "/"))
        cookie = headers["Set-Cookie"].split(";")[0].split("=", 1)[1]
        status, _, body = self.http("GET", "/", headers={"Cookie": f"s={cookie}"})
        self.assertIn(b"Nothing to do.", body)

    def test_password_with_spaces_in_the_form(self):
        typed = " ".join(WIFE_PW[i:i + 4] for i in range(0, 16, 4)).replace(" ", "+")
        self.assertEqual(self.post("/login", f"account=WFE&password={typed}")[0], 303)

    def test_bad_posts(self):
        import socket
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as s:
            s.sendall(b"POST /login HTTP/1.0\r\nContent-Type: application/x-www-form-urlencoded\r\n\r\n")
            self.assertTrue(s.makefile("rb").readline().startswith(b"HTTP/1.0 411"))
        self.assertEqual(self.post("/login", "a=" + "x" * 5000)[0], 413)
        self.assertEqual(self.http("POST", "/login", "{}", {"Content-Type": "application/json"})[0], 415)
        self.assertEqual(self.post("/login", "\xff=1".encode("latin-1"))[0], 400)
        self.assertEqual(self.post("/login", "&".join(f"f{i}=1" for i in range(20)))[0], 400)
        self.assertEqual(self.http("GET", "/", "x", {"Content-Length": "1"})[0], 400)

    def test_other_methods(self):
        for m in ("PUT", "DELETE", "HEAD", "OPTIONS", "PATCH"):
            self.assertEqual(self.http(m, "/")[0], 405, m)

    def test_forwarded_address_from_loopback_only(self):
        self.post("/login", "account=WFE&password=0000111122223333", extra={"X-Forwarded-For": "198.51.100.7"})
        self.assertIn("login_failed wife failures=1 198.51.100.7", self.lines)
        self.assertEqual(server.client_address("203.0.113.1", "198.51.100.7"), "203.0.113.1")
        self.assertEqual(server.client_address("127.0.0.1", "10.0.0.1, 198.51.100.7"), "198.51.100.7")
        self.assertEqual(server.client_address("::1", "2001:db8::1"), "2001:db8::1")
        self.assertEqual(server.client_address("127.0.0.1", "<script>"), "127.0.0.1(bad-forwarded)")
        self.assertEqual(server.client_address("127.0.0.1", None), "127.0.0.1")

    def test_a_crash_in_the_app_answers_500_and_the_server_stays_up(self):
        with mock.patch.object(self.app, "handle", side_effect=RuntimeError("boom")):
            status, _, body = self.http("GET", "/")
        self.assertEqual((status, body), (500, b"internal error"))
        self.assertIn("internal_error GET / RuntimeError('boom')", self.stderr.getvalue())
        self.assertEqual(self.http("GET", "/")[0], 200)

    def test_request_line_logged_without_query_or_form(self):
        self.post("/login", f"account=WFE&password={WIFE_PW}")
        self.http("GET", "/?secret=1")
        log = self.stderr.getvalue()
        self.assertIn("request POST /login 303 127.0.0.1", log)
        self.assertNotIn(WIFE_PW, log)
        self.assertNotIn("secret", log)

    def test_one_request_per_connection(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/")
        resp = conn.getresponse()
        resp.read()
        self.assertTrue(resp.will_close)
        conn.close()

    def test_handler_response_headers_pass_through(self):
        with mock.patch.object(self.app, "handle",
                               return_value=Response(200, b"x", "text/plain", [("X-Test", "1")])):
            _, headers, _ = self.http("GET", "/")
        self.assertEqual(headers["X-Test"], "1")


class RunCommandTests(unittest.TestCase):
    def test_refuses_root(self):
        err = io.StringIO()
        with mock.patch("os.geteuid", return_value=0), contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["run", "--config", "/nonexistent"]), 2)
        self.assertIn("not run the web app as root", err.getvalue())

    def test_bad_config(self):
        err = io.StringIO()
        # not root, or `run` refuses before it reads the config (tests may run as root)
        with mock.patch("os.geteuid", return_value=1000), contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["run", "--config", "/nonexistent"]), 1)
        self.assertIn("config error", err.getvalue())


if __name__ == "__main__":
    unittest.main()
