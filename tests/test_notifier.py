import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from heirdrop import config, notifier
from heirdrop.config import TelegramConfig
from heirdrop.notifier import EventsLogNotifier, NotifyError, queue
from heirdrop.notifier.telegram import MAX_ANSWER_BYTES, TelegramNotifier, http_post
from heirdrop.statefiles import StateStore
from tests.faketelegram import TOKEN, FakeTelegram
from tests.helpers import day, owned_by_non_root

OWNER, WIFE = 111, 222
EXAMPLE = Path(__file__).resolve().parent.parent / "doc" / "init" / "heirdrop.example.toml"


def tg_config(token=TOKEN, api_url="https://api.telegram.org"):
    return TelegramConfig(Path("/nonexistent"), OWNER, WIFE, 5.0, api_url, token)


def alert(to="owner", text="hello"):
    return {"id": f"x:{to}", "to": to, "kind": "warning", "text": text}


class TelegramClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name))
        self.store.prepare()
        self.fake = FakeTelegram()
        self.n = TelegramNotifier(tg_config(), self.store, post=self.fake)

    def fails(self, a=None) -> NotifyError:
        with self.assertRaises(NotifyError) as cm:
            self.n.deliver(a or alert(), day(1))
        e = cm.exception
        self.assertNotIn(TOKEN, str(e))
        self.assertNotIn(TOKEN.split(":")[1], e.message)
        return e

    def test_success_returns_receipt_and_logs(self):
        r = self.n.deliver(alert("wife", "hi"), day(1))
        self.assertEqual(r, {"message_id": 101})
        self.assertEqual(self.fake.accepted, [{"chat_id": WIFE, "text": "hi", "message_id": 101}])
        [ev] = self.store.read_events()
        self.assertEqual((ev["kind"], ev["backend"], ev["message_id"], ev["to"]), ("alert", "telegram", 101, "wife"))
        self.assertNotIn(TOKEN, json.dumps(ev))

    def test_request_shape(self):
        seen = []
        self.n.post = lambda url, body, timeout: (seen.append((url, json.loads(body), timeout)),
                                                  (200, b'{"ok": true, "result": {"message_id": 7}}'))[1]
        self.assertEqual(self.n.send("owner", "t"), {"message_id": 7})
        url, body, timeout = seen[0]
        self.assertEqual(url, f"https://api.telegram.org/bot{TOKEN}/sendMessage")
        self.assertEqual(body, {"chat_id": OWNER, "text": "t", "disable_web_page_preview": True})
        self.assertEqual(timeout, 5.0)

    def test_error_classes(self):
        cases = [
            ("down", dict(permanent=False, transport=True)),
            ("timeout", dict(permanent=False, transport=True)),
            ("status_5xx", dict(permanent=False, transport=True)),
            ("rate_limited", dict(permanent=False, transport=True)),
            ("garbage", dict(permanent=False, transport=False)),
        ]
        for switch, expect in cases:
            with self.subTest(switch):
                setattr(self.fake, switch, True)
                e = self.fails()
                self.assertEqual((e.permanent, e.transport), (expect["permanent"], expect["transport"]))
                setattr(self.fake, switch, False)
        self.fake.blocked.add(WIFE)
        e = self.fails(alert("wife"))
        self.assertEqual((e.permanent, e.transport), (True, False))
        self.assertIn("blocked", e.message)
        self.assertEqual(self.n.deliver(alert("owner"), day(1)), {"message_id": 101})  # owner unaffected
        for status in (400, 401, 404):
            self.fake.answer = (status, json.dumps({"ok": False, "error_code": status, "description": "x"}).encode())
            self.assertTrue(self.fails().permanent, status)
        self.assertEqual(self.store.read_events()[-1]["message_id"], 101)  # failures write no events

    def test_wrong_token_is_permanent(self):
        n = TelegramNotifier(tg_config(token="999:other"), self.store, post=self.fake)
        with self.assertRaises(NotifyError) as cm:
            n.send("owner", "x")
        self.assertTrue(cm.exception.permanent)

    def test_malformed_answers_are_temporary_failures(self):
        answers = [
            (200, b"[]"), (200, b"null"), (200, b'"ok"'), (200, b'{"ok": true}'),
            (200, b'{"ok": true, "result": []}'), (200, b'{"ok": true, "result": {"message_id": "7"}}'),
            (200, b'{"ok": true, "result": {"message_id": true}}'), (200, b'{"ok": false}'),
            (200, b'{"ok": "true", "result": {"message_id": 7}}'), (200, b""),
            (500, b""), (418, b"teapot"), (403, b'{"error_code": "403"}'), (None, b""),
            (200, b'{"ok": false, "error_code": true, "description": 5}'),
        ]
        for answer in answers:
            with self.subTest(answer):
                self.fake.answer = answer
                e = self.fails()
                self.assertFalse(e.permanent and answer[0] not in (403,), answer)

    def test_token_never_in_error_text(self):
        def leaky(url, body, timeout):
            raise OSError(f"cannot reach {url}")
        self.n.post = leaky
        e = self.fails()
        self.assertIn("<token>", e.message)
        self.fake.answer = (400, json.dumps({"ok": False, "error_code": 400,
                                             "description": f"bad request {TOKEN}"}).encode())
        self.n.post = self.fake
        self.assertIn("<token>", self.fails().message)

    def test_token_not_in_repr(self):
        self.assertNotIn(TOKEN, repr(tg_config()))


def qa(id, kind, created, to="owner", accepted=None):
    return {"id": id, "to": to, "kind": kind, "created_at": f"2026-01-{created:02d}T12:00:00Z",
            "accepted_at": accepted}


class QueueTests(unittest.TestCase):
    def ids(self, alerts):
        return [a["id"] for a in alerts]

    def test_only_newest_of_a_series_is_sendable(self):
        alerts = [
            qa("owner-warning:D1:0", "warning", 1), qa("owner-warning:D1:1", "warning", 2),
            qa("owner-warning:D2:0", "warning", 3),                 # moved deadline: same series
            qa("fault:node_unreachable:2026-01-01", "fault", 1), qa("fault:node_unreachable:2026-01-02", "fault", 2),
            qa("fault:tx_checksum:W2:2026-01-01", "fault", 1),      # other reason: own series
            qa("hold:H1", "hold", 1), qa("hold-cleared:H1", "hold_cleared", 2),  # one-offs: all sent
            qa("grace-wife:D:0", "grace", 2, to="wife"), qa("grace-owner:D:0", "grace", 2),
        ]
        to_send, missed = queue.sendable(alerts)
        self.assertEqual(self.ids(to_send), ["fault:tx_checksum:W2:2026-01-01", "hold:H1",
                                             "fault:node_unreachable:2026-01-02", "grace-owner:D:0",
                                             "grace-wife:D:0", "hold-cleared:H1", "owner-warning:D2:0"])
        self.assertEqual(self.ids(missed), ["fault:node_unreachable:2026-01-01", "owner-warning:D1:0",
                                            "owner-warning:D1:1"])

    def test_after_a_decision_only_allowed_kinds_are_sendable(self):
        alerts = [qa("grace-wife:D:46", "grace", 1, to="wife"), qa("fault:node_unreachable:X", "fault", 1),
                  qa("decision:T:wife", "decision", 2, to="wife"), qa("broadcast-failed:F1:X", "broadcast_failed", 2)]
        to_send, missed = queue.sendable(alerts, frozenset({"decision", "broadcast_failed"}))
        self.assertEqual(self.ids(to_send), ["broadcast-failed:F1:X", "decision:T:wife"])
        self.assertEqual(self.ids(missed), ["fault:node_unreachable:X", "grace-wife:D:46"])
        self.assertEqual(queue.summary(alerts, frozenset({"decision"}))["owner"]["pending"], 0)

    def test_an_accepted_newer_alert_keeps_older_ones_missed(self):
        alerts = [qa("w:0", "warning", 1), qa("w:1", "warning", 2), qa("w:2", "warning", 3, accepted="x")]
        self.assertEqual(queue.sendable(alerts), ([], [alerts[0], alerts[1]]))

    def test_legacy_delivered_at_counts(self):
        a = qa("hold:H", "hold", 1)
        del a["accepted_at"]
        a["delivered_at"] = "2026-01-01T12:00:05Z"
        self.assertEqual(queue.sendable([a]), ([], []))
        self.assertEqual(queue.summary([a])["owner"]["last_notified_on"], "2026-01-01T12:00:05Z")

    def test_interleave_takes_turns(self):
        alerts = [qa(f"o{n}", "hold", n) for n in range(1, 4)] + [qa(f"w{n}", "grace", 5 + n, to="wife") for n in range(2)]
        self.assertEqual(self.ids(queue.interleave(alerts)), ["w0", "o1", "w1", "o2", "o3"])

    def test_summary(self):
        alerts = [qa("a", "hold", 1, accepted="2026-01-01T12:00:01Z"), qa("b", "armed", 2, accepted="2026-01-02T12:00:01Z"),
                  qa("c", "warning", 3), qa("d", "warning", 4), qa("g", "grace", 4, to="wife")]
        self.assertEqual(queue.summary(alerts), {
            "owner": {"last_notified_on": "2026-01-02T12:00:01Z", "last_kind": "armed", "last_id": "b", "pending": 1},
            "wife": {"last_notified_on": None, "last_kind": None, "last_id": None, "pending": 1},
        })


SERVER = r"""
import socket, sys, time
mode = sys.argv[1]
s = socket.socket(); s.bind(("127.0.0.1", 0)); s.listen(1)
print(s.getsockname()[1], flush=True)
c, _ = s.accept()
c.recv(65536)
head = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
if mode == "big":
    c.sendall(head + b"Content-Length: 200000\r\n\r\n" + b"x" * 200000)
elif mode == "slow":
    c.sendall(head + b"Content-Length: 100000\r\n\r\n")
    for _ in range(100):
        c.sendall(b"x" * 1000); time.sleep(0.05)
else:
    body = b'{"ok": true, "result": {"message_id": 5}}'
    c.sendall(head + b"Content-Length: %d\r\n\r\n" % len(body) + body)
time.sleep(0.5)
c.close()
"""


class HttpPostTests(unittest.TestCase):
    """http_post against a real socket in a child process (no threads in tests)."""

    def server(self, mode):
        p = subprocess.Popen([sys.executable, "-c", SERVER, mode], stdout=subprocess.PIPE, text=True)
        self.addCleanup(p.wait)
        self.addCleanup(p.kill)
        return f"http://127.0.0.1:{int(p.stdout.readline())}/bot{TOKEN}/sendMessage"

    def test_ok(self):
        status, data = http_post(self.server("ok"), b"{}", 5)
        self.assertEqual((status, json.loads(data)["result"]["message_id"]), (200, 5))

    def test_size_bound(self):
        with self.assertRaisesRegex(NotifyError, str(MAX_ANSWER_BYTES)):
            http_post(self.server("big"), b"{}", 5)

    def test_time_bound(self):
        start = time.monotonic()
        with self.assertRaisesRegex(NotifyError, "longer than"):
            http_post(self.server("slow"), b"{}", 1)
        self.assertLess(time.monotonic() - start, 3)

    def test_refused_connection_is_a_transport_failure(self):
        url = self.server("ok")
        n = TelegramNotifier(tg_config(api_url=url.split("/bot")[0]), None)
        n.send("owner", "first")              # the server takes one connection, then exits
        time.sleep(0.8)
        with self.assertRaises(NotifyError) as cm:
            n.send("owner", "second")
        self.assertTrue(cm.exception.transport)
        self.assertNotIn(TOKEN, cm.exception.message)


class TelegramConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.token_file = self.dir / "telegram.token"
        self.token_file.write_text(TOKEN + "\n")
        os.chmod(self.token_file, 0o600)
        self.path = self.dir / "heirdrop.toml"

    def write(self, section: str):
        self.path.write_text(EXAMPLE.read_text().replace("[safety]", section + "\n\n[safety]"))
        os.chmod(self.path, 0o600)

    def section(self, **over):
        values = {"bot_token_file": f'"{self.token_file}"', "owner_chat_id": "111", "wife_chat_id": "222", **over}
        return "[telegram]\n" + "".join(f"{k} = {v}\n" for k, v in values.items() if v is not None)

    def test_without_section_the_events_log_is_used(self):
        self.write("")
        cfg = config.load(self.path)
        self.assertIsNone(cfg.telegram)
        self.assertIsInstance(notifier.build(cfg, StateStore(self.dir)), EventsLogNotifier)

    def test_section_loads_token_and_builds_telegram(self):
        self.write(self.section())
        cfg = config.load(self.path)
        tg = cfg.telegram
        self.assertEqual((tg.owner_chat_id, tg.wife_chat_id, tg.timeout, tg.api_url, tg.token),
                         (111, 222, 10.0, "https://api.telegram.org", TOKEN))
        self.assertNotIn(TOKEN, repr(cfg))
        self.assertIsInstance(notifier.build(cfg, StateStore(self.dir)), TelegramNotifier)

    def test_bad_sections(self):
        cases = [
            (dict(owner_chat_id=None), "owner_chat_id is required"),
            (dict(wife_chat_id='"222"'), "wife_chat_id must be int"),
            (dict(wife_chat_id="111"), "must differ"),
            (dict(timeout_seconds="0"), "must be positive"),
            (dict(api_url='"ftp://x"'), "api_url"),
            (dict(bot_token_file=f'"{self.dir / "missing"}"'), "not found"),
        ]
        for over, msg in cases:
            with self.subTest(msg):
                self.write(self.section(**over))
                with self.assertRaisesRegex(config.ConfigError, msg):
                    config.load(self.path)

    def test_token_file_checks(self):
        self.write(self.section())
        os.chmod(self.token_file, 0o640)
        owned_by_non_root(self.token_file)
        with self.assertRaisesRegex(config.ConfigError, "readable by group"):
            config.load(self.path)
        config.load(self.path, check_perms=False)
        os.chmod(self.token_file, 0o600)
        for bad in ("", "not a token", f"{TOKEN}\nsecond line", "abc:def"):
            self.token_file.write_text(bad)
            with self.assertRaises(config.ConfigError) as cm:
                config.load(self.path)
            if bad:
                self.assertNotIn(bad, str(cm.exception))

    def test_build_refuses_a_config_without_token(self):
        self.write(self.section())
        cfg = config.load(self.path)
        with self.assertRaises(ValueError):
            notifier.build(replace(cfg, telegram=replace(cfg.telegram, token=None)), StateStore(self.dir))


if __name__ == "__main__":
    unittest.main()
