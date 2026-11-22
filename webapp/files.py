"""UTC time and the web app's own small state files (lockout counters).

Timestamps inside files are UTC strings; file mtimes are never used. Files
are replaced atomically: write a unique temp file, fsync, rename, fsync the
directory, so a crash leaves the old or the new content, never a mix.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

UTC = timezone.utc


def utc_now() -> datetime:
    return datetime.now(UTC)


def fmt_ts(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(s: str) -> datetime:
    if not isinstance(s, str) or not s.endswith("Z"):
        raise ValueError(f"not a UTC timestamp: {s!r}")
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def write_atomic(path: Path, data: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(data, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    dfd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def read_json(path: Path) -> dict | None:
    """The object in `path`, None if there is no file. Raises ValueError if
    the content is not a JSON object, OSError if it cannot be read."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not hold a JSON object")
    return data
