"""State directory: atomic file primitives and the StateStore.

Plain files are the only source of truth. Every file carries UTC timestamps
inside it; modification times are never used. Files are replaced atomically
(temp file, fsync, rename) and the decision marker is created exclusively.
"""
from __future__ import annotations

import fcntl
import json
import os
from datetime import datetime
from pathlib import Path

from .clock import fmt_ts, parse_ts
from .config import PAIRS, TX_NAMES
from .logic import Broadcast, Decision, Facts, Pause


class StateError(Exception):
    """State files are unreadable or contradict each other."""


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_temp(path: Path, data: bytes) -> Path:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    return tmp


def write_atomic(path: Path, data: bytes) -> None:
    tmp = _write_temp(path, data)
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def create_exclusive(path: Path, data: bytes) -> bool:
    """Create path with data; return False if it already exists.

    The content is written and fsynced to a temp file first, then hard-linked
    into place, so the marker is either absent or complete, never empty.
    """
    tmp = _write_temp(path, data)
    try:
        os.link(tmp, path)
    except FileExistsError:
        return False
    finally:
        os.unlink(tmp)
    _fsync_dir(path.parent)
    return True


def append_line(path: Path, line: str) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(line.rstrip("\n") + "\n")
        f.flush()
        os.fsync(f.fileno())


def drop_torn_tail(path: Path) -> bool:
    """Remove an incomplete last line (no newline), left by a crash in the
    middle of an append. Returns True if something was removed."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return False
    if not data or data.endswith(b"\n"):
        return False
    with open(path, "r+b") as f:
        f.truncate(data.rfind(b"\n") + 1)
        f.flush()
        os.fsync(f.fileno())
    return True


def lock_exclusive(path: Path) -> int | None:
    """Take an exclusive lock on path for the life of the process. Returns
    the open descriptor (keep it), or None if another process holds it."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def _dumps(obj) -> bytes:
    return (json.dumps(obj, sort_keys=True) + "\n").encode()


class StateStore:
    def __init__(self, state_dir: Path):
        self.dir = Path(state_dir)
        self.alerts_dir = self.dir / "alerts"
        self.events_path = self.dir / "events.jsonl"

    def prepare(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.alerts_dir.mkdir(exist_ok=True, mode=0o700)

    # ---- low level -----------------------------------------------------
    def _read_json(self, name: str) -> dict | None:
        path = self.dir / name
        if not path.exists():
            return None
        try:
            obj = json.loads(path.read_text())
        except (OSError, ValueError) as e:
            raise StateError(f"{name}: unreadable: {e}") from e
        if not isinstance(obj, dict):
            raise StateError(f"{name}: not a JSON object")
        return obj

    def _read_ts(self, name: str) -> datetime | None:
        obj = self._read_json(name)
        if obj is None:
            return None
        try:
            return parse_ts(obj["at"])
        except (KeyError, ValueError, TypeError) as e:
            raise StateError(f"{name}: bad timestamp: {e}") from e

    def _write_ts(self, name: str, at: datetime) -> None:
        write_atomic(self.dir / name, _dumps({"at": fmt_ts(at)}))

    # ---- facts ---------------------------------------------------------
    def read_facts(self) -> Facts:
        f = Facts()
        f.armed_at = self._read_ts("armed.json")
        f.owner_checkin = self._read_ts("checkin_owner.json")
        f.wife_checkin = self._read_ts("checkin_wife.json")
        f.last_tick = self._read_ts("last_tick.json")
        f.pauses = self._read_pauses()
        f.decision = self._read_decision()
        f.broadcasts = {}
        for name in TX_NAMES:
            bc = self._read_broadcast(name)
            if bc is not None:
                f.broadcasts[name] = bc
        f.final_at = self._read_ts("final.json")
        f.alert_accepted = self._read_alert_accepted()
        self._check_consistency(f)
        return f

    def _read_pauses(self) -> list[Pause]:
        path = self.dir / "pauses.jsonl"
        if not path.exists():
            return []
        pauses = []
        text = path.read_text()
        lines = text.splitlines()
        if text and not text.endswith("\n"):
            # A crash in the middle of an append. That tick never finished, so
            # last_tick did not move and the same gap is recorded again.
            lines.pop()
        for n, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
                if not isinstance(obj, dict):
                    raise ValueError("not a JSON object")
                start = parse_ts(obj["from"]) if obj.get("from") else None
                end = parse_ts(obj["to"])
                hold = parse_ts(obj["hold_until"]) if obj.get("hold_until") else None
            except (KeyError, ValueError, TypeError) as e:
                raise StateError(f"pauses.jsonl line {n}: {e}") from e
            if start is not None and end < start:
                raise StateError(f"pauses.jsonl line {n}: ends before it starts")
            pauses.append(Pause(start=start, end=end, hold_until=hold))
        return pauses

    def _read_decision(self) -> Decision | None:
        obj = self._read_json("decision.json")
        if obj is None:
            return None
        try:
            choice, at = obj["choice"], parse_ts(obj["at"])
        except (KeyError, ValueError, TypeError) as e:
            raise StateError(f"decision.json: {e}") from e
        if choice not in PAIRS:
            raise StateError(f"decision.json: unknown choice {choice!r}")
        return Decision(choice=choice, at=at)

    def _read_broadcast(self, name: str) -> Broadcast | None:
        fname = f"broadcast_{name}.json"
        obj = self._read_json(fname)
        if obj is None:
            return None
        try:
            return Broadcast(
                name=name,
                txid=str(obj["txid"]),
                at=parse_ts(obj["at"]),
                dry_run=bool(obj["dry_run"]),
                blockhash=obj.get("blockhash"),
                block_height=obj.get("block_height"),
            )
        except (KeyError, ValueError, TypeError) as e:
            raise StateError(f"{fname}: {e}") from e

    def _read_alert_accepted(self) -> dict[str, datetime | None]:
        """Alert id -> time the notifier backend accepted it, or None."""
        out = {}
        if not self.alerts_dir.exists():
            return out
        for path in sorted(self.alerts_dir.glob("*.json")):  # sorted: same error on every filesystem
            try:
                obj = json.loads(path.read_text())
                if not isinstance(obj, dict):
                    raise ValueError("not a JSON object")
                at = _accepted_at(obj)
                out[obj["id"]] = parse_ts(at) if at else None
            except (OSError, ValueError, KeyError, TypeError) as e:
                raise StateError(f"alerts/{path.name}: {e}") from e
        return out

    def _check_consistency(self, f: Facts) -> None:
        if f.owner_checkin is not None and f.armed_at is None:
            raise StateError("checkin_owner.json exists without armed.json")
        if f.armed_at is not None and f.owner_checkin is None:
            raise StateError("armed.json exists without checkin_owner.json")
        if f.decision is None:
            if f.broadcasts:
                raise StateError("broadcast markers exist without decision.json")
            if f.final_at is not None:
                raise StateError("final.json exists without decision.json")
            return
        if f.armed_at is None:
            raise StateError("decision.json exists but the service was never armed")
        pair = set(PAIRS[f.decision.choice])
        extra = set(f.broadcasts) - pair
        if extra:
            raise StateError(f"broadcast markers {sorted(extra)} do not belong to pair {f.decision.choice}")
        if f.final_at is not None and set(f.broadcasts) != pair:
            raise StateError("final.json exists but the pair was not fully broadcast")

    # ---- writes --------------------------------------------------------
    def write_checkin(self, role: str, at: datetime) -> None:
        if role == "owner":
            if not (self.dir / "armed.json").exists():
                # arming happens once; checkin follows so a crash in between
                # leaves "armed without checkin", which read_facts reports.
                create_exclusive(self.dir / "armed.json", _dumps({"at": fmt_ts(at)}))
            self._write_ts("checkin_owner.json", at)
        elif role == "wife":
            self._write_ts("checkin_wife.json", at)
        else:
            raise ValueError(role)

    def write_last_tick(self, at: datetime) -> None:
        self._write_ts("last_tick.json", at)

    def record_pause(self, start: datetime | None, end: datetime, hold_until: datetime | None) -> bool:
        """Append a pause. Returns True if an incomplete last line was dropped first."""
        path = self.dir / "pauses.jsonl"
        repaired = drop_torn_tail(path)
        obj = {
            "from": fmt_ts(start) if start else None,
            "to": fmt_ts(end),
            "hold_until": fmt_ts(hold_until) if hold_until else None,
        }
        append_line(path, json.dumps(obj, sort_keys=True))
        return repaired

    def record_decision(self, choice: str, at: datetime) -> bool:
        """Record the decision exactly once. Returns False if one existed."""
        if choice not in PAIRS:
            raise ValueError(choice)
        return create_exclusive(self.dir / "decision.json", _dumps({"choice": choice, "at": fmt_ts(at)}))

    def record_broadcast(self, bc: Broadcast) -> None:
        obj = {
            "txid": bc.txid,
            "at": fmt_ts(bc.at),
            "dry_run": bc.dry_run,
            "blockhash": bc.blockhash,
            "block_height": bc.block_height,
        }
        write_atomic(self.dir / f"broadcast_{bc.name}.json", _dumps(obj))

    def record_final(self, at: datetime) -> bool:
        return create_exclusive(self.dir / "final.json", _dumps({"at": fmt_ts(at)}))

    # ---- events and alerts --------------------------------------------
    def log_event(self, at: datetime, kind: str, **data) -> None:
        obj = {"at": fmt_ts(at), "kind": kind, **data}
        append_line(self.events_path, json.dumps(obj, sort_keys=True))

    def read_events(self, limit: int = 100) -> list[dict]:
        if not self.events_path.exists():
            return []
        lines = self.events_path.read_text().splitlines()
        out = []
        for line in lines[-limit:]:
            try:
                out.append(json.loads(line))
            except ValueError:
                out.append({"raw": line})
        return out

    def alert_path(self, alert_id: str) -> Path:
        return self.alerts_dir / f"{alert_id}.json"

    def create_alert(self, alert_id: str, to: str, kind: str, text: str, at: datetime) -> bool:
        if self.alert_path(alert_id).exists():
            return False  # the common case each tick; skips writing and fsyncing a temp file
        obj = {"id": alert_id, "to": to, "kind": kind, "text": text, "created_at": fmt_ts(at),
               "accepted_at": None, "attempts": 0, "last_attempt_at": None, "last_error": None,
               "permanent": False, "receipt": None}
        return create_exclusive(self.alert_path(alert_id), _dumps(obj))

    def read_alert(self, alert_id: str) -> dict | None:
        path = self.alert_path(alert_id)
        if not path.exists():
            return None
        return json.loads(path.read_text())

    def update_alert(self, alert: dict) -> None:
        write_atomic(self.alert_path(alert["id"]), _dumps(alert))

    def read_alerts(self) -> list[dict]:
        """All readable alert files. Unreadable ones are skipped: read_facts
        reports them as a state error, and this must not raise on top."""
        out = []
        if not self.alerts_dir.exists():
            return out
        for path in sorted(self.alerts_dir.glob("*.json")):
            try:
                obj = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if isinstance(obj, dict) and isinstance(obj.get("id"), str):
                out.append(obj)
        return out

    def pending_alerts(self) -> list[dict]:
        return [a for a in self.read_alerts() if not _accepted_at(a)]


def _accepted_at(alert: dict):
    """`accepted_at`, or `delivered_at` in alert files written before the notifier."""
    return alert["accepted_at"] if "accepted_at" in alert else alert.get("delivered_at")
