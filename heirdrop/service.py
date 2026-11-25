"""The daemon core: one tick reads the facts, derives the situation, raises
alerts, records a decision when one is due, broadcasts and tracks the chosen
pair. Everything it decides is recomputed from disk and the clock each tick.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta

from . import txcheck
from .clock import DAY, fmt_ts
from .config import PAIRS, Config
from .logic import (Broadcast, Decision, Facts, Pause, Situation, allowed_kinds, derive, family_view,
                    owner_view, wife_view)
from .node import NodeError, NodeUnreachable, RpcError
from .notifier import NotifyError, messages, queue
from .statefiles import StateError, StateStore

# What a missing field or a wrong type in a node answer raises. The node is
# only partly trusted: such an answer is a node failure, never a crashed tick.
BAD_ANSWER = (KeyError, TypeError, ValueError, AttributeError, OverflowError)

# Send attempts per tick. A tick waits for each attempt, up to the backend's
# timeout, so this bounds how long a failing notifier can delay a tick.
MAX_SENDS_PER_TICK = 4

RPC_VERIFY_ALREADY_IN_CHAIN = -27
RPC_INVALID_ADDRESS_OR_KEY = -5  # getmempoolentry: not in mempool


class Service:
    def __init__(self, cfg: Config, clock, store: StateStore, node, notifier, log=None):
        self.cfg = cfg
        self.clock = clock
        self.store = store
        self.node = node
        self.notifier = notifier
        self._log = log or (lambda msg: print(msg, file=sys.stdout, flush=True))
        self.tx_hexes: dict[str, str | None] = {}
        self.tx_reports: dict[str, txcheck.TxReport] = {}
        self.last_validation: datetime | None = None
        self.node_status: dict = {"reachable": False}
        self.tracking: dict[str, dict] = {}
        self.facts: Facts = Facts()
        self.situation: Situation | None = None
        self.state_error: str | None = None
        self._sends_left = MAX_SENDS_PER_TICK
        self._send_blocked: set[str] = set()  # recipients not tried again in this tick

    # ---- logging ---------------------------------------------------------
    def log(self, now: datetime, kind: str, **data) -> None:
        self._log(f"{fmt_ts(now)} {kind} {data if data else ''}".rstrip())
        try:
            self.store.log_event(now, kind, **data)
        except OSError as e:  # the events log must never crash the service
            self._log(f"{fmt_ts(now)} events_log_error {e}")

    def note(self, msg: str) -> None:
        """Journal only: for things anyone can cause, which must not grow the events log."""
        self._log(f"{fmt_ts(self.clock.now())} {msg}")

    # ---- lifecycle -------------------------------------------------------
    def start(self) -> None:
        self.store.prepare()
        self.log(self.clock.now(), "start", dry_run=self.cfg.dry_run)
        self.safe_tick()  # a failing first tick must not keep the socket from coming up

    def run_forever(self) -> None:
        self.start()
        while True:
            self.clock.sleep(self.cfg.timing.tick_interval.total_seconds())
            self.safe_tick()

    def safe_tick(self) -> None:
        try:
            self.tick()
        except NodeError as e:
            self._log(f"{fmt_ts(self.clock.now())} node_error {e}")
        except Exception as e:  # noqa: BLE001 - keep running, systemd restarts on exit anyway
            self.log(self.clock.now(), "tick_error", error=repr(e))

    # ---- the tick --------------------------------------------------------
    def tick(self) -> Situation:
        now = self.clock.now()
        self._sends_left, self._send_blocked = MAX_SENDS_PER_TICK, set()
        try:
            facts = self.store.read_facts()
            self.state_error = None
        except StateError as e:
            self.state_error = str(e)
            self.log(now, "state_error", error=str(e))
            s = derive(self.cfg.timing, Facts(), now, ["state_files"])
            s.holds = ["state_files"]  # unarmed is meaningless here; the files are unreadable
            s.alerts = [a for a in s.alerts if a.kind != "unarmed"]
            self._raise_alerts(s, now)
            self._deliver_pending(now, Facts())
            self.situation = s
            return s

        self._handle_gap(facts, now)

        faults = self._check_node(now)
        if not faults:
            faults.extend(self._maybe_validate(now, facts))
        for r in self.tx_reports.values():
            faults.extend(r.hold_reasons())

        s = derive(self.cfg.timing, facts, now, faults)
        self._raise_alerts(s, now)
        self._deliver_pending(now, facts)
        # Delivery of the first grace alert can move the grace clock, so derive again.
        s = derive(self.cfg.timing, facts, now, faults)

        if s.decision_due and s.may_act and facts.decision is None:
            if self.store.record_decision(s.decision_due, now):
                facts.decision = Decision(s.decision_due, now)
                self.log(now, "decision", choice=s.decision_due, w_selected_at=fmt_ts(s.w_selected_at) if s.w_selected_at else None)
            else:
                facts = self.store.read_facts()

        if facts.decision is not None and s.may_act and facts.final_at is None:
            try:
                self._broadcast_and_track(facts, now)
            except (NodeError, *BAD_ANSWER) as e:  # retried next tick; this one still completes
                self.log(now, "node_error", during="broadcast_and_track", error=repr(e))

        if facts.last_tick is None or now >= facts.last_tick:
            self.store.write_last_tick(now)

        s = derive(self.cfg.timing, facts, now, faults)
        self._raise_alerts(s, now)
        self._deliver_pending(now, facts)  # also sends what the broadcast step raised
        self.facts, self.situation = facts, s
        return s

    # ---- gaps, holds, pauses ----------------------------------------------
    def _handle_gap(self, facts: Facts, now: datetime) -> None:
        t = self.cfg.timing
        prev = facts.last_tick
        if prev is None:
            hold_until = now + t.cold_start_hold
            self._record_pause(None, now, hold_until)
            facts.pauses.append(Pause(None, now, hold_until))
            self.log(now, "cold_start", reason="no previous tick", hold_until=fmt_ts(hold_until))
            return
        gap = now - prev
        if gap < timedelta(0):
            self.log(now, "clock_went_backwards", last_tick=fmt_ts(prev))
            return
        if gap <= 2 * t.tick_interval:
            return
        hold_until = now + t.cold_start_hold if gap > t.cold_start_gap_threshold else None
        self._record_pause(prev, now, hold_until)
        facts.pauses.append(Pause(prev, now, hold_until))
        self.log(now, "pause", gap_seconds=int(gap.total_seconds()),
                 hold_until=fmt_ts(hold_until) if hold_until else None)

    def _record_pause(self, start: datetime | None, end: datetime, hold_until: datetime | None) -> None:
        if self.store.record_pause(start, end, hold_until):
            self.log(end, "pauses_repaired", reason="dropped an incomplete last line")

    # ---- node ------------------------------------------------------------
    def _check_node(self, now: datetime) -> list[str]:
        try:
            info = self.node.call("getblockchaininfo")
            tip_time = info.get("time")
            if tip_time is None:
                tip_time = self.node.call("getblockheader", info["bestblockhash"])["time"]
            tip_time = float(tip_time)
            blocks, headers = int(info["blocks"]), int(info["headers"])
            ibd = bool(info.get("initialblockdownload", True))
            tip_at = fmt_ts(datetime.fromtimestamp(tip_time, tz=now.tzinfo))
        except (NodeError, OSError, *BAD_ANSWER) as e:
            error = str(e) if isinstance(e, NodeError) else f"malformed answer: {e!r}"
            self.node_status = {"reachable": False, "error": error, "checked_at": fmt_ts(now)}
            return ["node_unreachable"]
        faults = []
        if ibd or headers - blocks > 1:
            faults.append("node_not_synced")
        skew = abs(now.timestamp() - tip_time)
        if skew > self.cfg.timing.max_clock_skew.total_seconds():
            faults.append("clock_skew")
        self.node_status = {
            "reachable": True, "blocks": blocks, "headers": headers,
            "initialblockdownload": ibd,
            "tip_time": tip_at,
            "clock_skew_seconds": int(skew), "checked_at": fmt_ts(now),
        }
        return faults

    def _maybe_validate(self, now: datetime, facts: Facts) -> list[str]:
        """Validate when due. Returns faults: an aborted run holds."""
        if self.last_validation is not None:
            had_problems = any(r.problems for r in self.tx_reports.values())
            interval = timedelta(hours=1) if had_problems else DAY
            if now - self.last_validation < interval:
                return []
        self.tx_hexes = txcheck.load_all(self.cfg)
        hexes = self.tx_hexes
        if facts.decision is not None:
            hexes = {n: hexes[n] for n in PAIRS[facts.decision.choice]}
        try:
            reports = txcheck.validate(self.cfg, self.node, hexes, set(facts.broadcasts))
        except (NodeError, *BAD_ANSWER) as e:
            self.log(now, "validation_aborted", error=str(e) if isinstance(e, NodeError) else repr(e))
            return ["node_unreachable"]
        self.tx_reports = reports
        self.last_validation = now
        summary = {n: ("ok" if not r.problems else "; ".join(r.problems)) for n, r in reports.items()}
        self.log(now, "validation", **summary)
        for name, r in reports.items():
            if r.problems and not r.hold_reasons() and facts.decision is None:
                self._alert(f"tx-problem:{name}:{now.date().isoformat()}", "owner", "tx_problem",
                            {"name": name}, now)
        return []

    # ---- alerts ----------------------------------------------------------
    def _alert(self, alert_id: str, to: str, kind: str, params: dict, now: datetime) -> dict | None:
        text = messages.render(kind, to, params)
        if self.store.create_alert(alert_id, to, kind, text, now):
            self._log(f"{fmt_ts(now)} alert_queued {alert_id}")
            return self.store.read_alert(alert_id)
        return None

    def _raise_alerts(self, s: Situation, now: datetime) -> list[dict]:
        created = [self._alert(a.id, a.to, a.kind, a.params, now) for a in s.alerts]
        return [c for c in created if c is not None]

    def _deliver_pending(self, now: datetime, facts: Facts) -> None:
        """Send what is worth sending, within this tick's limits. Retried every
        tick with no backoff, permanent failures too (the wife may press Start
        later). Nothing here may stop the tick."""
        try:
            to_send = queue.interleave(queue.sendable(self.store.read_alerts(), allowed_kinds(facts))[0])
        except Exception as e:  # noqa: BLE001 - e.g. a hand-edited alert file of the wrong shape
            self._log(f"{fmt_ts(now)} alert_queue_error {e!r}")
            return
        for alert in to_send:
            if self._sends_left <= 0:
                return
            if alert.get("to") in self._send_blocked:
                continue
            self._sends_left -= 1
            try:
                self._send(alert, now, facts)
            except Exception as e:  # noqa: BLE001 - e.g. the alert file cannot be written
                self._log(f"{fmt_ts(now)} alert_error {alert.get('id')} {e!r}")

    def _send(self, alert: dict, now: datetime, facts: Facts) -> None:
        alert["attempts"] = int(alert.get("attempts") or 0) + 1
        alert["last_attempt_at"] = fmt_ts(now)
        alert.pop("delivered_at", None)  # old name; accepted_at replaces it
        try:
            receipt = self.notifier.deliver(alert, now)
        except Exception as e:  # noqa: BLE001 - a send failure is data, not a crash
            if isinstance(e, NotifyError):
                error, permanent, transport = e.message, e.permanent, e.transport
            else:
                error, permanent, transport = repr(e), False, False
            alert.update(accepted_at=None, last_error=error, permanent=permanent)
            self.store.update_alert(alert)
            self._log(f"{fmt_ts(now)} alert_failed {alert['id']} {error}")
            # The backend refused this recipient: the others may still work.
            # The network or Telegram failed: the next attempt would wait too.
            self._send_blocked.add(alert.get("to"))
            if transport:
                self._sends_left = 0
            if permanent and alert.get("to") == "wife" and facts.decision is None:
                self._alert(f"channel-broken:wife:{now.date().isoformat()}", "owner", "channel_broken",
                            {"error": error}, now)
            return
        alert.update(accepted_at=fmt_ts(now), receipt=receipt if isinstance(receipt, dict) else None,
                     last_error=None, permanent=False)
        self.store.update_alert(alert)
        self._log(f"{fmt_ts(now)} alert_accepted {alert['id']}")
        facts.alert_accepted[alert["id"]] = now

    # ---- broadcast and tracking ------------------------------------------
    def _broadcast_and_track(self, facts: Facts, now: datetime) -> None:
        pair = PAIRS[facts.decision.choice]
        confirmations = {}
        for name in pair:
            bc = facts.broadcasts.get(name)
            if bc is None or (bc.dry_run and not self.cfg.dry_run):
                bc = self._broadcast(name, now)
                if bc is None:
                    # Alerted; retried next tick. The other transaction of
                    # the pair spends other coins, so it still goes out.
                    continue
                facts.broadcasts[name] = bc
            if bc.dry_run:
                self.tracking[name] = {"txid": bc.txid, "dry_run": True, "confirmations": None}
                continue
            confs = self._track(bc, now)
            confirmations[name] = confs
            self.tracking[name] = {"txid": bc.txid, "dry_run": False, "confirmations": confs,
                                   "blockhash": bc.blockhash}
        if len(confirmations) == len(pair) and all(c >= self.cfg.timing.confirmations_final for c in confirmations.values()):
            if self.store.record_final(now):
                facts.final_at = now
                self.log(now, "final", **{n: facts.broadcasts[n].txid for n in pair})

    def _hex_and_txid(self, name: str) -> tuple[str, str]:
        hexstr = self.tx_hexes.get(name) or txcheck.read_tx_file(self.cfg.transactions[name])
        if hexstr is None:
            raise RpcError(0, f"{name}: file missing or checksum mismatch")
        report = self.tx_reports.get(name)
        txid = report.txid if report and report.txid else self.node.call("decoderawtransaction", hexstr)["txid"]
        return hexstr, txid

    def _broadcast(self, name: str, now: datetime) -> Broadcast | None:
        try:
            hexstr, txid = self._hex_and_txid(name)
            if not isinstance(txid, str):
                raise TypeError(f"txid is not a string: {txid!r}")
        except (NodeError, *BAD_ANSWER) as e:
            self.log(now, "broadcast_failed", name=name, error=str(e))
            self._alert(f"broadcast-failed:{name}:{now.date().isoformat()}", "owner", "broadcast_failed",
                        {"name": name}, now)
            return None
        if self.cfg.dry_run:
            self.log(now, "dry_run_broadcast", name=name, txid=txid)
            bc = Broadcast(name, txid, now, dry_run=True)
        else:
            try:
                sent = self.node.call("sendrawtransaction", hexstr, self.cfg.max_fee_rate)
                if isinstance(sent, str) and sent and sent != txid:
                    txid = sent
            except RpcError as e:
                if e.code != RPC_VERIFY_ALREADY_IN_CHAIN:
                    self.log(now, "broadcast_failed", name=name, error=str(e))
                    self._alert(f"broadcast-failed:{name}:{now.date().isoformat()}", "owner", "broadcast_failed",
                                {"name": name}, now)
                    return None
            self.log(now, "broadcast", name=name, txid=txid)
            bc = Broadcast(name, txid, now, dry_run=False)
        self.store.record_broadcast(bc)
        self._alert(f"broadcast:{name}:{txid}", "owner", "broadcast", {"name": name, "dry_run": bc.dry_run}, now)
        return bc

    def _track(self, bc: Broadcast, now: datetime) -> int:
        """Return the confirmation count of bc, rebroadcasting if it vanished."""
        tip = self.node_status.get("blocks", 0)
        if bc.blockhash:
            confs = int(self.node.call("getblockheader", bc.blockhash)["confirmations"])
            if confs >= 0:
                return confs
            self.log(now, "reorg", name=bc.name, blockhash=bc.blockhash)
            bc = Broadcast(bc.name, bc.txid, bc.at, bc.dry_run)
            self.store.record_broadcast(bc)

        out = self.node.call("gettxout", bc.txid, 0, True)
        if out is not None:
            confs = int(out.get("confirmations", 0))
            if confs >= 1:
                # Count from the tip this answer belongs to: a block may have
                # arrived since getblockchaininfo at the start of the tick.
                if out.get("bestblock"):
                    tip = int(self.node.call("getblockheader", out["bestblock"])["height"])
                height = tip - confs + 1
                blockhash = self.node.call("getblockhash", height)
                self.store.record_broadcast(Broadcast(bc.name, bc.txid, bc.at, False, blockhash, height))
                self.log(now, "confirmed", name=bc.name, txid=bc.txid, height=height, blockhash=blockhash)
            return confs
        if bc.block_height is not None:
            # Known to be in the chain (sendrawtransaction said so) but our
            # output is already spent, so count from where we first saw it.
            return max(1, tip - bc.block_height + 1)
        try:
            self.node.call("getmempoolentry", bc.txid)
            return 0
        except RpcError as e:
            if e.code != RPC_INVALID_ADDRESS_OR_KEY:
                raise
        # Neither in the mempool nor in the UTXO set: rebroadcast.
        hexstr, _ = self._hex_and_txid(bc.name)
        try:
            self.node.call("sendrawtransaction", hexstr, self.cfg.max_fee_rate)
            self.log(now, "rebroadcast", name=bc.name, txid=bc.txid)
            return 0
        except RpcError as e:
            if e.code == RPC_VERIFY_ALREADY_IN_CHAIN:
                self.store.record_broadcast(Broadcast(bc.name, bc.txid, bc.at, False, None, tip))
                self.log(now, "in_chain_unlocated", name=bc.name, txid=bc.txid, height=tip)
                return 1
            self.log(now, "rebroadcast_failed", name=bc.name, error=str(e))
            self._alert(f"rebroadcast-failed:{bc.name}:{now.date().isoformat()}", "owner", "rebroadcast_failed",
                        {"name": bc.name}, now)
            return 0

    # ---- read side for the interface --------------------------------------
    def views(self) -> dict:
        s, f = self.situation, self.facts
        if s is None:
            return {}
        return {"owner": owner_view(s, f), "wife": wife_view(s, f), "family": family_view(s, f)}

    def health(self) -> dict:
        return {
            "state_error": self.state_error,
            "holds": list(self.situation.holds) if self.situation else [],
            "node": self.node_status,
            "transactions": {n: r.as_dict() for n, r in self.tx_reports.items()},
            "last_validation": fmt_ts(self.last_validation) if self.last_validation else None,
            "last_tick": fmt_ts(self.facts.last_tick) if self.facts.last_tick else None,
            "tracking": self.tracking,
            **self._notifications(),
            "dry_run": self.cfg.dry_run,
        }

    def _notifications(self) -> dict:
        """Computed from the alert files on each call; unreadable files are
        skipped, so this answers during a state_files hold too."""
        alerts = self.store.read_alerts()
        allowed = allowed_kinds(self.facts)
        to_send, missed = queue.sendable(alerts, allowed)
        keys = ("id", "to", "kind", "created_at", "attempts", "last_attempt_at", "last_error", "permanent")
        return {
            "notifications": {"backend": self.notifier.name, **queue.summary(alerts, allowed)},
            "pending_alerts": [{k: a.get(k) for k in keys} for a in to_send],
            "missed_alerts": len(missed),
        }

    def notify_test(self, to: str) -> dict:
        """Send one fixed test message now. Writes no alert file, so it does
        not count as a notification in health."""
        now = self.clock.now()
        alert = {"id": f"notify-test:{fmt_ts(now)}", "to": to, "kind": "test",
                 "text": messages.render("test", to, {"at": now})}
        try:
            receipt = self.notifier.deliver(alert, now)
        except Exception as e:  # noqa: BLE001 - the answer says what went wrong
            error = e.message if isinstance(e, NotifyError) else repr(e)
            self.log(now, "notify_test", to=to, accepted=False, error=error)
            return {"accepted": False, "error": error}
        self.log(now, "notify_test", to=to, accepted=True)
        return {"accepted": True, "to": to, "accepted_at": fmt_ts(now),
                "receipt": receipt if isinstance(receipt, dict) else None}

    # ---- check-ins -------------------------------------------------------
    def checkin(self, role: str) -> dict:
        now = self.clock.now()
        self.store.write_checkin(role, now)
        after_decision = self.facts.decision is not None
        self.log(now, "checkin", role=role, after_decision=after_decision)
        result = {"role": role, "at": fmt_ts(now), "after_decision": after_decision}
        try:
            s = self.tick()
        except Exception as e:  # noqa: BLE001 - the check-in is on disk; say so, the next tick picks it up
            self.log(now, "tick_error", during="checkin", error=repr(e))
            s = self.situation
            result["warning"] = "check-in recorded, but the service could not update its state; see the journal"
        result["state"] = s.phase if s else None
        return result

