"""Command line of the web app: run, new-password, new-key, validate-config.

Nothing here prints a password hash or the session key, apart from the one
hash line `new-password` prints for the config. A chosen password is never
echoed.
"""
from __future__ import annotations

import argparse
import getpass
import os
import secrets
import sys
import tomllib
from pathlib import Path

from . import config, passwords
from .app import App
from .auth import Auth
from .server import serve

DEFAULT_CONFIG = "/etc/heirdrop-web/web.toml"


def _other_hash(path: Path, account: str) -> str | None:
    """The other account's stored hash from the config, None if there is no
    config yet or no such entry. Read with tomllib only: the picture and the
    key need not exist yet. Raises OSError or ValueError if it cannot be read."""
    if not path.exists():
        return None
    data = tomllib.loads(path.read_text())
    other = "family" if account == "wife" else "wife"
    accounts = data.get("accounts")
    table = accounts.get(other) if isinstance(accounts, dict) else None
    stored = table.get("password") if isinstance(table, dict) else None
    return stored if isinstance(stored, str) and passwords.check_hash_format(stored) is None else None


def _chosen(prompt) -> tuple[str | None, str | None]:
    """(digits, None) or (None, why not) for a password typed twice."""
    first = prompt("password (16 digits, spaces or dashes allowed): ")
    digits = passwords.normalize(first)
    if digits is None:
        return None, "the password must be exactly 16 digits"
    why = passwords.weakness(digits)
    if why:
        return None, f"the password {why}"
    if passwords.normalize(prompt("again: ")) != digits:
        return None, "the two entries differ"
    return digits, None


def cmd_new_password(args, prompt=getpass.getpass) -> int:
    if args.choose:
        try:
            password, problem = _chosen(prompt)
        except (EOFError, KeyboardInterrupt):
            password, problem = None, "no password entered"
        if problem:
            print(f"refused: {problem}; nothing written", file=sys.stderr)
            return 1
    else:
        password = passwords.new_password()
    # The two accounts must not share a password: check against the other one.
    try:
        other = _other_hash(Path(args.config), args.account)
    except (OSError, ValueError) as e:
        print(f"cannot read {args.config} to compare with the other account: {e}", file=sys.stderr)
        print("run as root, or pass --config with the right path", file=sys.stderr)
        return 1
    if other is not None and passwords.verify(password, other):
        print(f"refused: this is the other account's password; {args.account} needs its own", file=sys.stderr)
        return 1
    if other is None:
        print(f"note: no other account in {args.config} yet to compare with")
    if not args.choose:
        print(f"password: {passwords.grouped(password)}")
        print("Give it to the person on paper; it is shown only now.")
    print(f"Config lines for {args.config}:")
    print(f"[accounts.{args.account}]")
    print(f'password = "{passwords.hash_password(password)}"')
    return 0


def cmd_new_key(args) -> int:
    path = Path(args.path)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        print(f"{path} exists; remove it first (a new key logs everyone out)", file=sys.stderr)
        return 1
    with os.fdopen(fd, "w") as f:
        f.write(secrets.token_hex(config.KEY_HEX_LEN // 2) + "\n")
    print(f"session key written to {path} (mode 0600)")
    print(f"as root: chown root:dmweb {path} && chmod 640 {path}")
    return 0


def cmd_validate_config(args) -> int:
    try:
        cfg = config.load(args.config, check_perms=not args.no_perms_check)
    except (config.ConfigError, OSError) as e:
        print(f"config error: {e}", file=sys.stderr)
        return 1
    x, y, w, h = cfg.login_area
    print(f"config ok: {args.config}")
    print(f"  listen={cfg.host}:{cfg.port}  session_hours={cfg.session_hours}  title={cfg.title!r}")
    print(f"  picture={cfg.picture} ({cfg.picture_type}, {cfg.picture.stat().st_size} bytes)  "
          f"login_area x={x:g}% y={y:g}% w={w:g}% h={h:g}%")
    print(f"  accounts: {', '.join(sorted(cfg.password_hashes))}  session key: ok")
    print(f"  contact: {'set' if cfg.contact else 'none'}")
    # systemd creates the state directory at the unit's first start (`run`
    # refuses to start without it), so a missing one is only a note here.
    state = "(exists)" if cfg.state_dir.is_dir() else "(missing: systemd creates it when the unit starts)"
    print(f"  state_dir={cfg.state_dir} {state}")
    # Only whether the socket is there; nothing is sent to the service.
    is_socket = cfg.socket_path.exists() and cfg.socket_path.is_socket()
    print(f"  socket={cfg.socket_path} {'(present)' if is_socket else '(not present: is heirdrop running?)'}")
    return 0


def cmd_run(args) -> int:
    if os.geteuid() == 0:
        print("do not run the web app as root: use the dmweb user (see the systemd unit)", file=sys.stderr)
        return 2
    try:
        cfg = config.load(args.config)
    except (config.ConfigError, OSError) as e:
        print(f"config error: {e}", file=sys.stderr)
        return 1
    if not cfg.state_dir.is_dir() or not os.access(cfg.state_dir, os.W_OK):
        print(f"state_dir {cfg.state_dir} is missing or not writable", file=sys.stderr)
        return 1
    serve(App(cfg, Auth(cfg)), cfg.host, cfg.port)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python3 -m webapp")
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("new-password", help="a 16-digit password for one account and its config lines")
    s.add_argument("account", choices=("wife", "family"))
    s.add_argument("--choose", action="store_true", help="type the password yourself (asked twice, not shown)")
    s.add_argument("--config", default=DEFAULT_CONFIG, help="compared with the other account's password here")
    s.set_defaults(func=cmd_new_password)
    s = sub.add_parser("new-key", help="write a new session key file")
    s.add_argument("path", nargs="?", default="/etc/heirdrop-web/session.key")
    s.set_defaults(func=cmd_new_key)
    s = sub.add_parser("run", help="serve the web app (as the dmweb user, behind nginx)")
    s.add_argument("--config", default=DEFAULT_CONFIG)
    s.set_defaults(func=cmd_run)
    s = sub.add_parser("validate-config", help="check the config; no network, no socket request")
    s.add_argument("--config", default=DEFAULT_CONFIG)
    s.add_argument("--no-perms-check", action="store_true", help=argparse.SUPPRESS)
    s.set_defaults(func=cmd_validate_config)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
