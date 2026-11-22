"""Command line: run the daemon, validate a config, or talk to a running
service over its socket (check-in, status, health, notify-test, package,
document).
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys

from . import config, txcheck
from . import notifier as notifiers
from .clock import SystemClock
from .node import RpcClient
from .server import Server, request, uid_roles_from_config
from .service import Service
from .statefiles import StateStore, lock_exclusive

DEFAULT_SOCKET = "/run/heirdrop/heirdrop.sock"
DEFAULT_CONFIG = "/etc/heirdrop/heirdrop.toml"


def cmd_run(args) -> int:
    if os.geteuid() == 0:
        print("do not run the service as root: use the heirdrop user (see the systemd unit)", file=sys.stderr)
        return 2
    cfg = config.load(args.config)
    store = StateStore(cfg.state_dir)
    store.prepare()
    # Held until the process exits: two services on one state directory would
    # take each other's socket and both write the ledger.
    if lock_exclusive(cfg.state_dir / "lock") is None:
        print(f"another heirdrop service is already running on {cfg.state_dir}", file=sys.stderr)
        return 1
    node = RpcClient(cfg.rpc_url, cfg.rpc_cookie_file, cfg.rpc_user, cfg.rpc_password)
    service = Service(cfg, SystemClock(), store, node, notifiers.build(cfg, store))
    Server(cfg, service).serve_forever()
    return 0


def cmd_validate_config(args) -> int:
    try:
        cfg = config.load(args.config, check_perms=not args.no_perms_check)
    except config.ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 1
    t = cfg.timing
    print(f"config ok: {args.config}")
    print(f"  dry_run={cfg.dry_run}  tick={t.tick_interval}  owner_period={t.owner_period}  "
          f"grace={t.owner_grace}  wife_window={t.wife_window}")
    print(f"  state_dir={cfg.state_dir}  socket={cfg.socket_path}")
    print(f"  owner_user={cfg.owner_user}  webapp_user={cfg.webapp_user}")
    problems = 0
    try:
        uid_roles_from_config(cfg)  # the service refuses to start without both users
    except RuntimeError as e:
        print(f"  {e}")
        problems += 1
    for name, spec in cfg.transactions.items():
        if not spec.file.is_file():
            print(f"  {name}: MISSING {spec.file}")
            problems += 1
            continue
        actual = hashlib.sha256(spec.file.read_bytes()).hexdigest()
        if actual != spec.sha256:
            print(f"  {name}: CHECKSUM MISMATCH {spec.file} (actual {actual})")
            problems += 1
        else:
            print(f"  {name}: ok {spec.file} -> pays {txcheck.expected_address(cfg, name)}")
    for who, d in cfg.documents.items():
        print(f"  documents_{who}={d} {'(exists)' if d.is_dir() else '(MISSING)'}")
    tg = cfg.telegram
    if tg is None:
        print("  telegram: not configured, alerts go to the events log only")
        if not cfg.dry_run:
            print("  WARNING: dry_run is off and there is no [telegram] section: holds and deadlines are silent")
    else:  # no network call here; use notify-test against the running service
        print(f"  telegram: token file ok {tg.bot_token_file}  owner_chat_id={tg.owner_chat_id}  "
              f"wife_chat_id={tg.wife_chat_id}  api={tg.api_url}")
    return 1 if problems else 0


def _client(args, body: dict) -> dict:
    if os.geteuid() == 0:
        print("run this as your own user, not as root: the service identifies you by your uid",
              file=sys.stderr)
        sys.exit(2)
    try:
        return request(args.socket, body)
    except (OSError, ValueError) as e:
        print(f"cannot talk to the service at {args.socket}: {e}", file=sys.stderr)
        sys.exit(2)


def _print(resp: dict, as_json: bool) -> int:
    if as_json or not resp.get("ok"):
        print(json.dumps(resp, indent=2))
    else:
        _pretty(resp)
    return 0 if resp.get("ok") else 1


def _pretty(resp: dict, indent: int = 0) -> None:
    pad = "  " * indent
    for k, v in resp.items():
        if k == "ok":
            continue
        if isinstance(v, dict):
            print(f"{pad}{k}:")
            _pretty(v, indent + 1)
        elif isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
            print(f"{pad}{k}:")
            for x in v:
                print(f"{pad}  -")
                _pretty(x, indent + 2)
        elif isinstance(v, list):
            print(f"{pad}{k}: {json.dumps(v)}")
        else:
            print(f"{pad}{k}: {v}")


def cmd_checkin(args) -> int:
    return _print(_client(args, {"request": "checkin", "role": args.role}), args.json)


def cmd_status(args) -> int:
    return _print(_client(args, {"request": "status"}), args.json)


def cmd_health(args) -> int:
    return _print(_client(args, {"request": "health"}), args.json)


def cmd_notify_test(args) -> int:
    return _print(_client(args, {"request": "notify_test", "to": args.to}), args.json)


def cmd_package(args) -> int:
    return _print(_client(args, {"request": "package", "role": args.role}), args.json)


def cmd_document(args) -> int:
    resp = _client(args, {"request": "document", "role": args.role, "name": args.name})
    if not resp.get("ok"):
        return _print(resp, True)
    data = base64.b64decode(resp["base64"])
    out = args.out or args.name
    with open(out, "xb") as f:
        f.write(data)
    print(f"wrote {len(data)} bytes to {out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="heirdrop", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="run the daemon (used by the systemd unit)")
    r.add_argument("--config", default=DEFAULT_CONFIG)
    r.set_defaults(func=cmd_run)

    v = sub.add_parser("validate-config", help="load the config and check the transaction files")
    v.add_argument("--config", default=DEFAULT_CONFIG)
    v.add_argument("--no-perms-check", action="store_true", help="skip the owner and mode check (dev only)")
    v.set_defaults(func=cmd_validate_config)

    def client_parser(name, help_text):
        c = sub.add_parser(name, help=help_text)
        c.add_argument("--socket", default=os.environ.get("HEIRDROP_SOCKET", DEFAULT_SOCKET))
        c.add_argument("--json", action="store_true", help="print the raw JSON response")
        return c

    c = client_parser("checkin", "check in (owner by default; the web app user checks in as wife)")
    c.add_argument("role", nargs="?", choices=["owner", "wife"], default=None)
    c.set_defaults(func=cmd_checkin)

    client_parser("status", "show the current state").set_defaults(func=cmd_status)
    client_parser("health", "node, transaction and alert health (owner only)").set_defaults(func=cmd_health)

    c = client_parser("notify-test", "send a test message through the service's notifier (owner only)")
    c.add_argument("to", choices=["owner", "wife"])
    c.set_defaults(func=cmd_notify_test)

    c = client_parser("package", "transactions and document list for a beneficiary")
    c.add_argument("role", choices=["wife", "family"])
    c.set_defaults(func=cmd_package)

    c = client_parser("document", "fetch one document for a beneficiary")
    c.add_argument("role", choices=["wife", "family"])
    c.add_argument("name")
    c.add_argument("--out", help="output path (default: the document name in the current directory)")
    c.set_defaults(func=cmd_document)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "role", "x") is None and args.command == "checkin":
        del args.role  # let the server pick the default for the caller's role
        return _print(_client(args, {"request": "checkin"}), args.json)
    return args.func(args)
