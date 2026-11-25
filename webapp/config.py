"""TOML configuration of the web app: loading, defaults and validation.

Stricter than the service's loader: an unknown section or key is an error,
because a misspelt key in a security setting must not pass silently.

File permissions follow the service's rule (copied, not imported, since the
web app imports nothing from `heirdrop`): owned by root and at most 0640, or
owned by someone else and 0600. It applies to the config, the session key
and the picture.
"""
from __future__ import annotations

import ipaddress
import os
import stat
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import passwords

ACCOUNTS = ("wife", "family")
MAX_PICTURE_BYTES = 5_000_000
MAX_TITLE = 60
MAX_CONTACT = 300
KEY_HEX_LEN = 64  # 32 bytes
PICTURE_TYPES = {  # extension -> (content type, magic check)
    ".jpg": ("image/jpeg", lambda b: b[:3] == b"\xff\xd8\xff"),
    ".jpeg": ("image/jpeg", lambda b: b[:3] == b"\xff\xd8\xff"),
    ".png": ("image/png", lambda b: b[:8] == b"\x89PNG\r\n\x1a\n"),
    ".webp": ("image/webp", lambda b: b[:4] == b"RIFF" and b[8:12] == b"WEBP"),
}
KEYS = {
    "web": {"listen", "socket", "state_dir", "session_key_file", "session_hours", "title",
            "contact", "picture", "login_area"},
    "accounts": set(ACCOUNTS),
    "account": {"password"},
}


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class WebConfig:
    host: str
    port: int
    socket_path: Path
    state_dir: Path
    session_key_file: Path
    session_hours: int
    title: str
    contact: str
    picture: Path
    picture_type: str
    login_area: tuple[float, float, float, float]  # x, y, width, height in % of the picture
    password_hashes: dict[str, str] = field(repr=False)
    session_key: bytes | None = field(default=None, repr=False)  # read by load(); never printed


def permission_problem(mode: int, owner_uid: int) -> str | None:
    """Why a file with this mode and owner is refused, or None (the rule of
    heirdrop.config.permission_problem)."""
    mode = stat.S_IMODE(mode)
    if owner_uid != 0:
        return "is readable by group or others" if mode & 0o077 else None
    if mode & 0o007:
        return "is accessible by others"
    if mode & 0o030:
        return "is writable or executable by its group"
    return None


def check_permissions(path: Path) -> None:
    st = path.stat()
    problem = permission_problem(st.st_mode, st.st_uid)
    if problem:
        raise ConfigError(f"{path} {problem}; run: chown root:dmweb {path} && chmod 640 {path} "
                          f"(or chmod 600 if the web app user owns it)")


def _unknown(table: dict, allowed: set, where: str) -> None:
    extra = sorted(set(table) - allowed)
    if extra:
        raise ConfigError(f"unknown key(s) in {where}: {', '.join(extra)}")


def _get(table: dict, key: str, kind, default=None, required=False):
    if key not in table:
        if required:
            raise ConfigError(f"[web] {key} is required")
        return default
    value = table[key]
    if kind is int and isinstance(value, bool) or not isinstance(value, kind):
        raise ConfigError(f"[web] {key} must be {kind.__name__}")
    return value


def _listen(value: str) -> tuple[str, int]:
    host, sep, port = value.rpartition(":")
    host = host.strip("[]")
    if not sep or not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ConfigError(f"[web] listen must be host:port, got {value!r}")
    try:
        loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if not loopback:
        raise ConfigError(f"[web] listen must be a loopback address (nginx terminates TLS), got {host!r}")
    return host, int(port)


def _login_area(value) -> tuple[float, float, float, float]:
    if (not isinstance(value, list) or len(value) != 4
            or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value)):
        raise ConfigError("[web] login_area must be [x, y, width, height] in percent")
    x, y, w, h = (float(v) for v in value)
    if not (x >= 0 and y >= 0 and w > 0 and h > 0 and x + w <= 100 and y + h <= 100):
        raise ConfigError("[web] login_area must lie inside the picture (0 to 100 percent)")
    return x, y, w, h


def picture_problem(path: Path) -> tuple[str | None, str | None]:
    """(content type, None) for a usable picture, else (None, why not)."""
    kind = PICTURE_TYPES.get(path.suffix.lower())
    if kind is None:
        return None, f"{path} must be .jpg, .jpeg, .png or .webp"
    if not path.is_file():
        return None, f"picture not found: {path}"
    size = path.stat().st_size
    if size > MAX_PICTURE_BYTES:
        return None, f"{path} is {size} bytes, at most {MAX_PICTURE_BYTES}"
    with path.open("rb") as f:
        head = f.read(12)
    if not kind[1](head):
        return None, f"{path} does not look like {kind[0]}"
    return kind[0], None


def read_key(path: Path, check_perms: bool = True) -> bytes:
    if not path.is_file():
        raise ConfigError(f"session key not found: {path} (make one with `python3 -m webapp new-key`)")
    if check_perms:
        check_permissions(path)
    try:
        text = path.read_text().strip()
    except (OSError, UnicodeDecodeError) as e:  # the key is a secret: no bytes in the message
        raise ConfigError(f"cannot read session key {path}: {type(e).__name__}") from None
    try:
        key = bytes.fromhex(text)
    except ValueError:
        key = b""
    if len(text) != KEY_HEX_LEN or len(key) != KEY_HEX_LEN // 2:
        raise ConfigError(f"session key {path} must be {KEY_HEX_LEN} hex digits on one line")
    return key


def parse(data: dict) -> WebConfig:
    _unknown(data, {"web", "accounts"}, "the config")
    web = data.get("web")
    if not isinstance(web, dict):
        raise ConfigError("[web] section is required")
    _unknown(web, KEYS["web"], "[web]")
    accounts = data.get("accounts")
    if not isinstance(accounts, dict):
        raise ConfigError("[accounts.wife] and [accounts.family] are required")
    _unknown(accounts, KEYS["accounts"], "[accounts]")
    hashes = {}
    for name in ACCOUNTS:
        table = accounts.get(name)
        if not isinstance(table, dict) or "password" not in table:
            raise ConfigError(f"[accounts.{name}] password is required")
        _unknown(table, KEYS["account"], f"[accounts.{name}]")
        stored = table["password"]
        problem = passwords.check_hash_format(stored) if isinstance(stored, str) else "must be a string"
        if problem:
            raise ConfigError(f"[accounts.{name}] password {problem}")
        hashes[name] = stored
    if hashes["wife"] == hashes["family"]:
        raise ConfigError("[accounts.wife] and [accounts.family] have the same password line; "
                          "each needs its own password from `python3 -m webapp new-password`")

    host, port = _listen(_get(web, "listen", str, "127.0.0.1:8090"))
    hours = _get(web, "session_hours", int, 8)
    if not 1 <= hours <= 24 * 7:
        raise ConfigError("[web] session_hours must be between 1 and 168")
    title = _get(web, "title", str, "Family").strip()
    if not title or len(title) > MAX_TITLE:
        raise ConfigError(f"[web] title must be 1 to {MAX_TITLE} characters")
    contact = _get(web, "contact", str, "").strip()
    if len(contact) > MAX_CONTACT:
        raise ConfigError(f"[web] contact must be at most {MAX_CONTACT} characters")
    picture = Path(_get(web, "picture", str, required=True))
    picture_type, problem = picture_problem(picture)
    if problem:
        raise ConfigError(f"[web] picture: {problem}")
    return WebConfig(
        host=host, port=port,
        socket_path=Path(_get(web, "socket", str, "/run/heirdrop/heirdrop.sock")),
        state_dir=Path(_get(web, "state_dir", str, "/var/lib/heirdrop-web")),
        session_key_file=Path(_get(web, "session_key_file", str, "/etc/heirdrop-web/session.key")),
        session_hours=hours, title=title, contact=contact,
        picture=picture, picture_type=picture_type,
        login_area=_login_area(_get(web, "login_area", list, [70, 75, 20, 15])),
        password_hashes=hashes,
    )


def load(path: str | os.PathLike, check_perms: bool = True) -> WebConfig:
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    if check_perms:
        check_permissions(path)
    try:
        data = tomllib.loads(path.read_text())
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise ConfigError(f"{path}: {e}") from None
    cfg = parse(data)
    if check_perms:
        check_permissions(cfg.picture)
    key = read_key(cfg.session_key_file, check_perms)
    return WebConfig(**{**cfg.__dict__, "session_key": key})
