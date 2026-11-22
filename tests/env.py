"""Builds a complete test environment: config, state dir, tx files, fake node."""
from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

from heirdrop import config
from heirdrop.notifier import EventsLogNotifier
from heirdrop.clock import FakeClock
from heirdrop.service import Service
from heirdrop.statefiles import StateStore
from tests.fakenode import FakeNode
from tests.helpers import T0

WHW = "bc1qwife"
FMHW = "bc1qfamily"
INPUTS = {"W1": [("funding", 0)], "F1": [("funding", 0)], "W2": [("funding", 1)], "F2": [("funding", 1)]}
PAYS = {"W1": WHW, "W2": FMHW, "F1": FMHW, "F2": FMHW}


class Env:
    def __init__(self, dry_run=True, timing=None, pay=None):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.txdir = root / "txs"
        self.txdir.mkdir()
        self.hex = {name: f"hex-{name}" for name in INPUTS}
        tx_cfg = {}
        for name, hexstr in self.hex.items():
            path = self.txdir / f"{name.lower()}.hex"
            path.write_text(hexstr + "\n")
            tx_cfg[name] = {"file": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        t = {
            "tick_interval_seconds": 86400,       # tests tick once a day
            "cold_start_gap_threshold_hours": 72,
            "cold_start_hold_hours": 24,
        }
        t.update(timing or {})
        data = {
            "timing": t,
            "bitcoind": {"cookie_file": str(root / "cookie")},
            "transactions": tx_cfg,
            "addresses": {"whw": WHW, "fmhw": FMHW},
            "paths": {"state_dir": str(root / "state"), "socket": str(root / "sock"),
                      "documents_wife": str(root / "docs-wife"), "documents_family": str(root / "docs-family")},
            "interface": {"owner_user": "dmowner", "webapp_user": "dmweb"},
            "safety": {"dry_run": dry_run},
        }
        self.cfg = config.parse(data)
        self.clock = FakeClock(T0)
        self.node = FakeNode(self.clock)
        self.node.add_utxo("funding", 0, 1.0)
        self.node.add_utxo("funding", 1, 1.0)
        pay = {**PAYS, **(pay or {})}
        for name, hexstr in self.hex.items():
            self.node.register_tx(hexstr, f"txid-{name}", INPUTS[name], [(pay[name], 0.9999)])
        self.store = StateStore(self.cfg.state_dir)
        self.store.prepare()
        self.logs: list[str] = []
        self.service = self.new_service()

    def new_service(self, notifier=None) -> Service:
        """A fresh Service on the same state dir, i.e. a restart."""
        self.service = Service(self.cfg, self.clock, self.store, self.node,
                               notifier or EventsLogNotifier(self.store), log=self.logs.append)
        return self.service

    def tick_at(self, d: float):
        self.clock.set(T0.__class__.fromtimestamp(T0.timestamp() + d * 86400, tz=T0.tzinfo))
        return self.service.tick()

    def run_days(self, start: int, end: int):
        """Tick once per day from start to end inclusive."""
        s = None
        for d in range(start, end + 1):
            s = self.tick_at(d)
        return s

    def facts(self):
        return self.store.read_facts()

    def events(self, kind=None):
        ev = self.store.read_events(100000)
        return [e for e in ev if kind is None or e["kind"] == kind]

    def alert_ids(self):
        return sorted(p.stem for p in self.store.alerts_dir.glob("*.json"))

    def corrupt_tx_file(self, name):
        (self.txdir / f"{name.lower()}.hex").write_text("tampered\n")

    def close(self):
        self.tmp.cleanup()
