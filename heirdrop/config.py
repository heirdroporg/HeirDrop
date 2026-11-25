"""TOML configuration: loading, defaults and validation."""
from __future__ import annotations

import os
import re
import stat
import tomllib
from dataclasses import dataclass, field, replace
from datetime import timedelta
from pathlib import Path

TX_NAMES = ("W1", "W2", "F1", "F2")
PAIRS = {"W": ("W1", "W2"), "F": ("F1", "F2")}
_REQUIRED = object()

# Every key the service reads. Anything else is a typo and is refused, so a
# misspelt setting cannot silently fall back to its default.
KEYS = {
    "": {"timing", "bitcoind", "transactions", "addresses", "paths", "interface", "safety", "telegram"},
    "timing": {"owner_period_days", "warning_window_days", "owner_grace_days", "wife_window_days",
               "cold_start_hold_hours", "cold_start_gap_threshold_hours", "tick_interval_seconds",
               "confirmations_final", "max_clock_skew_hours", "max_alert_wait_hours"},
    "bitcoind": {"rpc_url", "cookie_file", "rpc_user", "rpc_password"},
    "transactions": set(TX_NAMES),
    "transaction": {"file", "sha256"},
    "addresses": {"whw", "fmhw"},
    "paths": {"state_dir", "socket", "documents_wife", "documents_family"},
    "interface": {"owner_user", "webapp_user"},
    "safety": {"dry_run", "max_document_bytes", "max_fee_rate"},
    "telegram": {"bot_token_file", "owner_chat_id", "wife_chat_id", "timeout_seconds", "api_url"},
}



class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Timing:
    owner_period: timedelta
    warning_window: timedelta
    owner_grace: timedelta
    wife_window: timedelta
    cold_start_hold: timedelta
    cold_start_gap_threshold: timedelta
    tick_interval: timedelta
    confirmations_final: int
    max_clock_skew: timedelta
    max_alert_wait: timedelta


@dataclass(frozen=True)
class TxSpec:
    name: str
    file: Path
    sha256: str


TOKEN_RE = re.compile(r"\d+:[A-Za-z0-9_-]+")


@dataclass(frozen=True)
class TelegramConfig:
    bot_token_file: Path
    owner_chat_id: int
    wife_chat_id: int
    timeout: float
    api_url: str
    token: str | None = field(default=None, repr=False)  # read by load(); never printed

    def chat_id(self, to: str) -> int:
        return self.owner_chat_id if to == "owner" else self.wife_chat_id


@dataclass(frozen=True)
class Config:
    timing: Timing
    rpc_url: str
    rpc_cookie_file: Path | None
    rpc_user: str | None
    rpc_password: str | None
    transactions: dict[str, TxSpec]
    whw_address: str
    fmhw_address: str
    state_dir: Path
    socket_path: Path
    documents: dict[str, Path]  # "wife" and "family"
    owner_user: str
    webapp_user: str
    dry_run: bool
    max_document_bytes: int
    max_fee_rate: float  # BTC/kvB passed to sendrawtransaction; 0 disables the cap
    telegram: TelegramConfig | None = None  # None: alerts go to the events log only


def _unknown(table: dict, allowed: set, where: str) -> None:
    extra = sorted(set(table) - allowed)
    if extra:
        raise ConfigError(f"unknown key(s) in {where}: {', '.join(extra)}")


def _table(data: dict, name: str) -> dict:
    t = data.get(name)
    if t is None:
        raise ConfigError(f"missing [{name}] section")
    if not isinstance(t, dict):
        raise ConfigError(f"[{name}] must be a table")
    _unknown(t, KEYS[name], f"[{name}]")
    return t


def _get(table: dict, section: str, key: str, kind, default=_REQUIRED):
    if key not in table:
        if default is _REQUIRED:
            raise ConfigError(f"[{section}] {key} is required")
        return default
    value = table[key]
    if kind is float and isinstance(value, int) and not isinstance(value, bool):
        value = float(value)
    if kind is int and isinstance(value, bool):
        raise ConfigError(f"[{section}] {key} must be an integer")
    if not isinstance(value, kind):
        raise ConfigError(f"[{section}] {key} must be {kind.__name__}")
    return value


def _nonneg(table, section, key, default):
    v = _get(table, section, key, float, default)
    if v < 0:
        raise ConfigError(f"[{section}] {key} must not be negative")
    return v


def _positive(table, section, key, default):
    v = _nonneg(table, section, key, default)
    if v == 0:
        raise ConfigError(f"[{section}] {key} must be positive")
    return v


def permission_problem(mode: int, owner_uid: int) -> str | None:
    """Why a config file with this mode and owner is refused, or None.

    Two layouts are accepted. Owned by root, the group may read it (the
    service reads it through its group and cannot change its own policy):
    at most 0640. Owned by anyone else, only the owner may touch it: 0600.
    """
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
        raise ConfigError(
            f"{path} {problem}; run: chmod 600 {path} "
            f"(or chown root:<service group> and chmod 640)"
        )


def load(path: str | os.PathLike, check_perms: bool = True) -> Config:
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    if check_perms:
        check_permissions(path)
    try:
        data = tomllib.loads(path.read_text())
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise ConfigError(f"cannot parse {path}: {e}") from e
    cfg = parse(data)
    if cfg.telegram is not None:
        cfg = replace(cfg, telegram=replace(cfg.telegram, token=read_token(cfg.telegram.bot_token_file, check_perms)))
    return cfg


def read_token(path: Path, check_perms: bool = True) -> str:
    """The bot token from its own file. Error texts never contain the token."""
    if not path.is_file():
        raise ConfigError(f"[telegram] bot_token_file not found: {path}")
    if check_perms:
        check_permissions(path)
    try:
        token = path.read_text().strip()
    except (OSError, UnicodeDecodeError) as e:
        raise ConfigError(f"[telegram] cannot read bot_token_file {path}: {type(e).__name__}") from None
    if not TOKEN_RE.fullmatch(token):
        raise ConfigError(f"[telegram] {path} does not hold a bot token (expected digits:letters, one line)")
    return token


def _telegram(data: dict) -> TelegramConfig | None:
    if "telegram" not in data:
        return None
    t = _table(data, "telegram")
    owner = _get(t, "telegram", "owner_chat_id", int)
    wife = _get(t, "telegram", "wife_chat_id", int)
    if owner == wife:
        raise ConfigError("[telegram] owner_chat_id and wife_chat_id must differ")
    api_url = _get(t, "telegram", "api_url", str, "https://api.telegram.org")
    if not re.match(r"^https?://", api_url):
        raise ConfigError("[telegram] api_url must start with http:// or https://")
    return TelegramConfig(
        bot_token_file=Path(_get(t, "telegram", "bot_token_file", str)),
        owner_chat_id=owner,
        wife_chat_id=wife,
        timeout=_positive(t, "telegram", "timeout_seconds", 10),
        api_url=api_url.rstrip("/"),
    )


def parse(data: dict) -> Config:
    extra = sorted(set(data) - KEYS[""])
    if extra:
        raise ConfigError(f"unknown section(s): {', '.join(extra)}")
    t = _table(data, "timing")
    timing = Timing(
        owner_period=timedelta(days=_positive(t, "timing", "owner_period_days", 74)),
        warning_window=timedelta(days=_nonneg(t, "timing", "warning_window_days", 14)),
        owner_grace=timedelta(days=_positive(t, "timing", "owner_grace_days", 16)),
        wife_window=timedelta(days=_nonneg(t, "timing", "wife_window_days", 30)),
        cold_start_hold=timedelta(hours=_nonneg(t, "timing", "cold_start_hold_hours", 24)),
        cold_start_gap_threshold=timedelta(
            hours=_positive(t, "timing", "cold_start_gap_threshold_hours", 6)
        ),
        tick_interval=timedelta(seconds=_positive(t, "timing", "tick_interval_seconds", 60)),
        confirmations_final=_get(t, "timing", "confirmations_final", int, 6),
        max_clock_skew=timedelta(hours=_positive(t, "timing", "max_clock_skew_hours", 4)),
        max_alert_wait=timedelta(hours=_nonneg(t, "timing", "max_alert_wait_hours", 24)),
    )
    if timing.confirmations_final < 1:
        raise ConfigError("[timing] confirmations_final must be at least 1")
    if timing.warning_window > timing.owner_period:
        raise ConfigError("[timing] warning_window_days exceeds owner_period_days")

    b = _table(data, "bitcoind")
    rpc_url = _get(b, "bitcoind", "rpc_url", str, "http://127.0.0.1:8332")
    if not re.match(r"^https?://", rpc_url):
        raise ConfigError("[bitcoind] rpc_url must start with http:// or https://")
    cookie = _get(b, "bitcoind", "cookie_file", str, None)
    user = _get(b, "bitcoind", "rpc_user", str, None)
    password = _get(b, "bitcoind", "rpc_password", str, None)
    if cookie and (user or password):
        raise ConfigError("[bitcoind] give either cookie_file or rpc_user/rpc_password, not both")
    if not cookie and not (user and password):
        raise ConfigError("[bitcoind] needs cookie_file, or both rpc_user and rpc_password")

    tx_table = _table(data, "transactions")
    transactions = {}
    for name in TX_NAMES:
        entry = tx_table.get(name)
        if not isinstance(entry, dict):
            raise ConfigError(f"[transactions] {name} must be a table with file and sha256")
        _unknown(entry, KEYS["transaction"], f"[transactions] {name}")
        file = _get(entry, f"transactions.{name}", "file", str)
        digest = _get(entry, f"transactions.{name}", "sha256", str).lower()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ConfigError(f"[transactions] {name}.sha256 must be 64 hex characters")
        transactions[name] = TxSpec(name=name, file=Path(file), sha256=digest)

    a = _table(data, "addresses")
    whw = _get(a, "addresses", "whw", str).strip()
    fmhw = _get(a, "addresses", "fmhw", str).strip()
    if not whw or not fmhw:
        raise ConfigError("[addresses] whw and fmhw must not be empty")
    if whw == fmhw:
        raise ConfigError("[addresses] whw and fmhw must differ")

    p = _table(data, "paths")
    documents = {
        "wife": Path(_get(p, "paths", "documents_wife", str)),
        "family": Path(_get(p, "paths", "documents_family", str)),
    }

    i = _table(data, "interface")
    owner_user = _get(i, "interface", "owner_user", str)
    webapp_user = _get(i, "interface", "webapp_user", str)
    if owner_user == webapp_user:
        raise ConfigError("[interface] owner_user and webapp_user must differ")

    s = data.get("safety", {})
    if not isinstance(s, dict):
        raise ConfigError("[safety] must be a table")
    _unknown(s, KEYS["safety"], "[safety]")
    dry_run = _get(s, "safety", "dry_run", bool, True)
    max_doc = _get(s, "safety", "max_document_bytes", int, 10_000_000)
    max_fee_rate = _nonneg(s, "safety", "max_fee_rate", 0.0)

    return Config(
        timing=timing,
        rpc_url=rpc_url,
        rpc_cookie_file=Path(cookie) if cookie else None,
        rpc_user=user,
        rpc_password=password,
        transactions=transactions,
        whw_address=whw,
        fmhw_address=fmhw,
        state_dir=Path(_get(p, "paths", "state_dir", str)),
        socket_path=Path(_get(p, "paths", "socket", str)),
        documents=documents,
        owner_user=owner_user,
        webapp_user=webapp_user,
        dry_run=dry_run,
        max_document_bytes=max_doc,
        max_fee_rate=max_fee_rate,
        telegram=_telegram(data),
    )
