import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from webapp import auth, files, passwords
from webapp.config import WebConfig

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
WIFE_PW = "7305918264017352"
FAMILY_PW = "4819027365910284"
HASHES = {"wife": passwords.hash_password(WIFE_PW), "family": passwords.hash_password(FAMILY_PW)}
KEY = bytes(range(32))


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


def make_cfg(state_dir: Path, key=KEY, hours=8) -> WebConfig:
    return WebConfig(host="127.0.0.1", port=8090, socket_path=Path("/nonexistent.sock"), state_dir=state_dir,
                     session_key_file=Path("/nonexistent.key"), session_hours=hours, title="Family", contact="",
                     picture=Path("/nonexistent.jpg"), picture_type="image/jpeg", login_area=(70, 75, 20, 15),
                     password_hashes=dict(HASHES), session_key=key)


class AuthCase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.clock = Clock()
        self.auth = auth.Auth(make_cfg(self.dir), self.clock)

    def tearDown(self):
        os.chmod(self.dir, 0o700)
        shutil.rmtree(self.dir)


class SessionTests(AuthCase):
    def test_round_trip(self):
        s = self.auth.new_session("wife")
        self.assertEqual(s.expires, int(T0.timestamp()) + 8 * 3600)
        got = self.auth.session(s.value)
        self.assertEqual((got.account, got.expires), ("wife", s.expires))
        self.assertNotEqual(self.auth.new_session("wife").value, s.value)  # a nonce per login
        with self.assertRaises(ValueError):
            self.auth.new_session("owner")

    def test_expiry(self):
        s = self.auth.new_session("family")
        self.clock.advance(hours=7, minutes=59)
        self.assertIsNotNone(self.auth.session(s.value))
        self.clock.advance(minutes=1)
        self.assertIsNone(self.auth.session(s.value))

    def test_tampering(self):
        s = self.auth.new_session("family")
        account, expires, nonce, mac = s.value.split(".")
        later = str(int(expires) + 3600)
        flipped = mac[:-1] + ("A" if mac[-1] != "A" else "B")
        for bad in (f"wife.{expires}.{nonce}.{mac}", f"{account}.{later}.{nonce}.{mac}",
                    f"{account}.{expires}.{nonce}x.{mac}", f"{account}.{expires}.{nonce}.{flipped}",
                    f"{account}.{expires}.{nonce}", s.value + ".x", "", None, "x" * 500, "a.b.c.d"):
            self.assertIsNone(self.auth.session(bad), bad)

    def test_other_key(self):
        s = self.auth.new_session("wife")
        other = auth.Auth(make_cfg(self.dir, key=bytes(32)), self.clock)
        self.assertIsNone(other.session(s.value))

    def test_lowered_session_hours_shorten_existing_sessions(self):
        s = self.auth.new_session("wife")
        shorter = auth.Auth(make_cfg(self.dir, hours=2), self.clock)
        self.assertIsNone(shorter.session(s.value))
        self.clock.advance(hours=6, minutes=1)
        self.assertIsNotNone(shorter.session(s.value))

    def test_survives_a_restart(self):
        s = self.auth.new_session("wife")
        self.assertIsNotNone(auth.Auth(make_cfg(self.dir), self.clock).session(s.value))

    def test_csrf(self):
        s = self.auth.new_session("wife")
        token = self.auth.csrf_token(s)
        self.assertTrue(self.auth.csrf_ok(s, token))
        self.assertFalse(self.auth.csrf_ok(s, token[:-1] + ("A" if token[-1] != "A" else "B")))
        self.assertFalse(self.auth.csrf_ok(s, ""))
        self.assertFalse(self.auth.csrf_ok(s, None))
        self.assertFalse(self.auth.csrf_ok(self.auth.new_session("wife"), token))  # another session


class LockoutTests(AuthCase):
    def wrong(self, name="WFE", n=1):
        return [self.auth.login(name, "0000111122223333") for _ in range(n)][-1]

    def test_lock_schedule(self):
        self.assertEqual([auth.lock_after(n) for n in range(1, 16)],
                         [None, None, auth.SHORT_LOCK, None, None, auth.SHORT_LOCK, None, None, auth.LONG_LOCK,
                          None, None, auth.LONG_LOCK, None, None, auth.LONG_LOCK])

    def test_right_password_and_the_dropdown_values(self):
        r = self.auth.login("WFE", "7305 9182 6401 7352")
        self.assertEqual((r.outcome, r.account, r.bucket), ("ok", "wife", "wife"))
        self.assertEqual(self.auth.login("FML", FAMILY_PW).account, "family")
        self.assertEqual(self.auth.login("FML", WIFE_PW).outcome, "wrong")  # each account its own
        self.assertEqual(list(self.dir.iterdir()), [self.dir / "lockout-family.json"])

    def test_ten_minutes_after_three_then_an_hour_after_nine(self):
        self.assertEqual(self.wrong(n=2).locked_until, None)
        r = self.wrong()
        self.assertEqual((r.outcome, r.failures, r.locked_until), ("wrong", 3, T0 + timedelta(minutes=10)))
        r = self.auth.login("WFE", WIFE_PW)               # locked: even the right one is refused
        self.assertEqual((r.outcome, r.failures), ("locked", 3))
        self.assertEqual(self.wrong().outcome, "locked")  # not counted while locked
        self.clock.advance(minutes=10)
        r = self.wrong(n=3)
        self.assertEqual((r.failures, r.locked_until), (6, self.clock.t + timedelta(minutes=10)))
        self.clock.advance(minutes=10)
        r = self.wrong(n=3)
        self.assertEqual((r.failures, r.locked_until), (9, self.clock.t + timedelta(hours=1)))
        self.clock.advance(minutes=59)
        self.assertEqual(self.auth.login("WFE", WIFE_PW).outcome, "locked")
        self.clock.advance(minutes=1)
        r = self.wrong(n=3)
        self.assertEqual((r.failures, r.locked_until), (12, self.clock.t + timedelta(hours=1)))
        self.clock.advance(hours=1)
        self.assertEqual(self.auth.login("WFE", WIFE_PW).outcome, "ok")
        self.assertEqual(self.wrong(n=3).locked_until, self.clock.t + timedelta(minutes=10))  # reset by success

    def test_accounts_and_unknown_names_are_counted_apart(self):
        self.wrong("WFE", 3)
        self.assertEqual(self.auth.login("FML", FAMILY_PW).outcome, "ok")
        r = self.wrong("nobody", 2)
        self.assertEqual((r.bucket, r.failures), ("unknown", 2))
        r = self.wrong("admin")
        self.assertEqual((r.bucket, r.failures, r.locked_until), ("unknown", 3, T0 + timedelta(minutes=10)))
        self.assertEqual(self.auth.login("owner", WIFE_PW).outcome, "locked")
        self.assertEqual(self.auth.login("unknown", WIFE_PW).bucket, "unknown")
        self.assertEqual(self.auth.login("", "").outcome, "locked")

    def test_an_unknown_name_never_logs_in(self):
        for name in ("owner", "root", "unknown", "wife", "family", "wfe", " WFE ", "WFE2"):
            self.clock.advance(hours=2)
            self.assertNotEqual(self.auth.login(name, WIFE_PW).outcome, "ok")

    def test_lockout_survives_a_restart(self):
        self.wrong(n=3)
        again = auth.Auth(make_cfg(self.dir), self.clock)
        self.assertEqual(again.login("WFE", WIFE_PW).outcome, "locked")
        data = json.loads((self.dir / "lockout-wife.json").read_text())
        self.assertEqual(data, {"failures": 3, "locked_until": "2026-01-01T12:10:00Z",
                                "updated_at": "2026-01-01T12:00:00Z"})
        self.assertNotIn(WIFE_PW, (self.dir / "lockout-wife.json").read_text())

    def test_unreadable_state_refuses(self):
        for content in ("{", "[]", '{"failures": "3"}', '{"failures": -1}', '{"failures": true}',
                        '{"failures": 1, "locked_until": 5}', '{"failures": 1, "locked_until": "tomorrow"}'):
            (self.dir / "lockout-wife.json").write_text(content)
            r = self.auth.login("WFE", WIFE_PW)
            self.assertEqual(r.outcome, "state_error", content)

    @unittest.skipIf(os.geteuid() == 0, "root writes into a read-only directory")
    def test_unwritable_state_refuses(self):
        os.chmod(self.dir, 0o500)
        self.assertEqual(self.wrong().outcome, "state_error")
        self.assertEqual(self.auth.login("WFE", WIFE_PW).outcome, "ok")  # nothing to reset: no write needed
        os.chmod(self.dir, 0o700)
        self.wrong()
        os.chmod(self.dir, 0o500)
        self.assertEqual(self.auth.login("WFE", WIFE_PW).outcome, "state_error")  # reset cannot be written

    def test_odd_input(self):
        self.assertEqual(self.auth.login(None, None).bucket, "unknown")
        self.assertEqual(self.auth.login("WFE", None).outcome, "wrong")


class FilesTests(unittest.TestCase):
    def test_timestamps(self):
        self.assertEqual(files.fmt_ts(T0), "2026-01-01T12:00:00Z")
        self.assertEqual(files.parse_ts("2026-01-01T12:00:00Z"), T0)
        for bad in ("2026-01-01T12:00:00", 5, None, "2026-01-01 12:00:00Z"):
            with self.assertRaises((ValueError, TypeError)):
                files.parse_ts(bad)

    def test_write_atomic_leaves_no_temp_files(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d)
        files.write_atomic(d / "x.json", {"a": 1})
        files.write_atomic(d / "x.json", {"a": 2})
        self.assertEqual(sorted(p.name for p in d.iterdir()), ["x.json"])
        self.assertEqual(files.read_json(d / "x.json"), {"a": 2})
        self.assertIsNone(files.read_json(d / "missing.json"))


if __name__ == "__main__":
    unittest.main()
