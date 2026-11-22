"""16-digit passwords and their scrypt hashes.

A password is 16 decimal digits, made by `new_password()` and shown in groups
of four. Its hash is stored in the config as

    scrypt$<n>$<r>$<p>$<salt, base64>$<hash, base64>

The parameters are in the string, so they can be raised later without
breaking existing hashes.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets

DIGITS = 16
N, R, P = 2 ** 15, 8, 1          # about 32 MiB and some 0.1 s per hash
MAXMEM = 64 * 1024 * 1024
HASH_RE = re.compile(r"scrypt\$(\d+)\$(\d+)\$(\d+)\$([A-Za-z0-9+/=]+)\$([A-Za-z0-9+/=]+)")
_SEPARATORS = re.compile(r"[\s-]+")


def new_password() -> str:
    """16 random decimal digits that pass `weakness()`."""
    while True:
        password = f"{secrets.randbelow(10 ** DIGITS):0{DIGITS}d}"
        if weakness(password) is None:
            return password


def grouped(password: str) -> str:
    return " ".join(password[i:i + 4] for i in range(0, len(password), 4))


def normalize(typed: str) -> str | None:
    """The digits of what was typed, with spaces and dashes removed, or None
    if that is not exactly 16 digits."""
    digits = _SEPARATORS.sub("", typed)
    if len(digits) != DIGITS or not digits.isascii() or not digits.isdigit():
        return None
    return digits


MIN_DISTINCT = 4


def weakness(digits: str) -> str | None:
    """Why a chosen 16-digit password is refused, or None. Only the obvious
    cases: the lockout makes guessing hopeless, not guessing by someone who
    knows the person."""
    if len(set(digits)) < MIN_DISTINCT:
        return f"uses fewer than {MIN_DISTINCT} different digits"
    steps = {(int(b) - int(a)) % 10 for a, b in zip(digits, digits[1:])}
    if len(steps) == 1:
        return "is a simple sequence"
    return None


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=MAXMEM, dklen=32)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = _scrypt(password, salt, N, R, P)
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    return f"scrypt${N}${R}${P}${b64(salt)}${b64(digest)}"


def check_hash_format(stored: str) -> str | None:
    """Why a stored hash is unusable, or None."""
    m = HASH_RE.fullmatch(stored)
    if not m:
        return "is not a scrypt hash from `python3 -m webapp new-password`"
    n, r, p = (int(x) for x in m.group(1, 2, 3))
    if n < 2 ** 14 or n & (n - 1) or not 1 <= r <= 32 or not 1 <= p <= 16 or 128 * r * n > MAXMEM:
        return "has scrypt parameters out of range"
    try:
        salt, digest = (base64.b64decode(x, validate=True) for x in m.group(4, 5))
    except ValueError:
        return "has a salt or hash that is not base64"
    if len(salt) < 16 or len(digest) != 32:
        return "has a salt or hash of the wrong length"
    return None


def verify(typed: str, stored: str) -> bool:
    """True if `typed` (spaces and dashes allowed) matches `stored`.

    A malformed entry still costs one scrypt run, so the answer time does not
    tell a wrong format from a wrong password.
    """
    m = HASH_RE.fullmatch(stored)
    if not m or check_hash_format(stored):
        return False
    n, r, p = (int(x) for x in m.group(1, 2, 3))
    salt, digest = (base64.b64decode(x) for x in m.group(4, 5))
    digits = normalize(typed)
    candidate = _scrypt(digits if digits is not None else "x" * DIGITS, salt, n, r, p)
    return digits is not None and hmac.compare_digest(candidate, digest)
