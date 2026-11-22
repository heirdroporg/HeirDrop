"""Login, sessions, CSRF tokens and the lockout.

Session: a cookie `account.expires.nonce.mac`, where `expires` is a unix
time and `mac` an HMAC-SHA256 with the session key. Nothing is kept on the
server, so a restart logs no one out; a new key logs everyone out. Logout
clears the cookie in the browser only (an accepted limit of a stateless
session: a copied cookie stays valid until it expires).

CSRF: every form carries an HMAC of the session cookie; a POST without the
matching token is refused. The login form needs none: there is no session
yet, and logging someone into an account the attacker cannot open gains
nothing.

Lockout per account, counted since the last successful login: the 3rd and
6th wrong guess lock for 10 minutes, the 9th and every 3rd after that for an
hour. While locked even the right password is refused and nothing is
counted. The login form picks the account from a dropdown (`CHOICES`: WFE
is the wife, FML the family member); any other value, only possible with a
hand-made request, can never log in and is counted in one shared counter
(`unknown`). The counters live
in `<state_dir>/lockout-<name>.json` and are written before the answer, so a
crash or restart cannot reset them. If they cannot be read or written the
login is refused (`state_error`), never let through.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from . import files, passwords
from .config import ACCOUNTS, WebConfig

LOCK_EVERY = 3
SHORT_LOCK = timedelta(minutes=10)
LONG_LOCK = timedelta(hours=1)
LONG_FROM = 9          # from this failure on, locks are LONG_LOCK
UNKNOWN = "unknown"    # the counter shared by all values not in CHOICES
# The dropdown on the login form: (value and label, account). The page shows
# only the labels, so its source does not name the accounts either.
CHOICES = (("WFE", "wife"), ("FML", "family"))
COOKIE = "s"


@dataclass(frozen=True)
class Session:
    account: str
    value: str  # the cookie value
    expires: int


@dataclass(frozen=True)
class LoginResult:
    outcome: str  # "ok", "wrong", "locked", "state_error"
    account: str | None = None     # the logged-in account if ok
    bucket: str | None = None      # the counter used: an account or "unknown"
    failures: int = 0              # failures counted after this attempt
    locked_until: datetime | None = None


def lock_after(failures: int) -> timedelta | None:
    """How long the failure numbered `failures` locks, or None."""
    if failures <= 0 or failures % LOCK_EVERY:
        return None
    return LONG_LOCK if failures >= LONG_FROM else SHORT_LOCK


class Auth:
    def __init__(self, cfg: WebConfig, now: Callable[[], datetime] = files.utc_now):
        if cfg.session_key is None:
            raise ValueError("config loaded without its session key")
        self.cfg = cfg
        self.now = now
        self.key = cfg.session_key
        # Checked against when the choice is not in CHOICES, so the answer takes
        # as long as for a known one.
        self._dummy_hash = passwords.hash_password(passwords.new_password())

    def _mac(self, purpose: str, text: str) -> str:
        digest = hmac.new(self.key, f"{purpose}|{text}".encode(), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).decode().rstrip("=")

    # ---- sessions ----------------------------------------------------------
    def new_session(self, account: str) -> Session:
        if account not in ACCOUNTS:
            raise ValueError(f"unknown account {account!r}")
        expires = int(self.now().timestamp()) + self.cfg.session_hours * 3600
        body = f"{account}.{expires}.{secrets.token_hex(16)}"
        return Session(account, f"{body}.{self._mac('session', body)}", expires)

    def session(self, value: str | None) -> Session | None:
        """The valid session in a cookie value, or None."""
        if not value or len(value) > 200:
            return None
        parts = value.split(".")
        if len(parts) != 4:
            return None
        account, expires, nonce, mac = parts
        if not hmac.compare_digest(mac, self._mac("session", f"{account}.{expires}.{nonce}")):
            return None
        if account not in ACCOUNTS or not expires.isascii() or not expires.isdigit():
            return None
        now = int(self.now().timestamp())
        # Also refuse a lifetime longer than session_hours allows now, so
        # lowering it in the config shortens existing sessions too.
        if not now < int(expires) <= now + self.cfg.session_hours * 3600:
            return None
        return Session(account, value, int(expires))

    def csrf_token(self, session: Session) -> str:
        return self._mac("csrf", session.value)

    def csrf_ok(self, session: Session, token: str | None) -> bool:
        return isinstance(token, str) and hmac.compare_digest(token, self.csrf_token(session))

    # ---- login and lockout ---------------------------------------------------
    def _path(self, bucket: str):
        return self.cfg.state_dir / f"lockout-{bucket}.json"

    def _read(self, bucket: str) -> tuple[int, datetime | None]:
        data = files.read_json(self._path(bucket))
        if data is None:
            return 0, None
        failures = data.get("failures")
        if isinstance(failures, bool) or not isinstance(failures, int) or failures < 0:
            raise ValueError(f"lockout-{bucket}.json: bad failures {failures!r}")
        until = data.get("locked_until")
        return failures, files.parse_ts(until) if until is not None else None

    def _write(self, bucket: str, failures: int, locked_until: datetime | None) -> None:
        files.write_atomic(self._path(bucket), {
            "failures": failures,
            "locked_until": files.fmt_ts(locked_until) if locked_until else None,
            "updated_at": files.fmt_ts(self.now()),
        })

    def login(self, choice: str, typed: str) -> LoginResult:
        bucket = dict(CHOICES).get(choice, UNKNOWN) if isinstance(choice, str) else UNKNOWN
        typed = typed if isinstance(typed, str) else ""
        now = self.now()
        try:
            failures, locked_until = self._read(bucket)
        except (OSError, ValueError, TypeError):
            passwords.verify(typed, self._dummy_hash)  # same time as a real attempt
            return LoginResult("state_error", bucket=bucket)
        stored = self.cfg.password_hashes.get(bucket, self._dummy_hash)
        if locked_until is not None and now < locked_until:
            passwords.verify(typed, stored)  # same time as a real attempt
            return LoginResult("locked", bucket=bucket, failures=failures, locked_until=locked_until)
        ok = passwords.verify(typed, stored) and bucket != UNKNOWN
        try:
            if ok:
                if failures or locked_until is not None:  # reset; nothing to write otherwise
                    self._write(bucket, 0, None)
                return LoginResult("ok", account=bucket, bucket=bucket)
            failures += 1
            lock = lock_after(failures)
            until = now + lock if lock else None
            self._write(bucket, failures, until)
        except OSError:
            return LoginResult("state_error", bucket=bucket, failures=failures)
        return LoginResult("wrong", bucket=bucket, failures=failures, locked_until=until)
