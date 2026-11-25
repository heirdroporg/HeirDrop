import json
import unittest

from heirdrop.node import NodeUnreachable
from pathlib import Path

from heirdrop.config import TelegramConfig
from heirdrop.notifier import messages, queue
from heirdrop.notifier.telegram import TelegramNotifier
from heirdrop.service import MAX_SENDS_PER_TICK
from heirdrop.statefiles import StateError
from tests.env import Env, WHW
from tests.faketelegram import TOKEN, FakeTelegram
from tests.helpers import day


class ServiceCase(unittest.TestCase):
    def setUp(self):
        self.env = Env(dry_run=False)
        self.addCleanup(self.env.close)

    def arm(self):
        """Start the service at day 0, check in as owner, clear the cold start hold."""
        self.env.service.start()
        self.env.service.checkin("owner")
        return self.env.run_days(1, 1)


class ColdStartTests(ServiceCase):
    def test_first_start_holds_and_alerts(self):
        s = self.env.tick_at(0)
        self.assertIn("cold_start_hold", s.holds)
        self.assertIn("unarmed", s.holds)
        self.assertEqual(s.hold_until, day(1))
        self.assertEqual(len(self.env.events("cold_start")), 1)
        self.assertEqual(self.env.alert_ids(), ["hold:2026-01-02T12:00:00Z", "unarmed:2026-01-01"])
        self.assertEqual(self.env.store.pending_alerts(), [])  # events log backend delivers at once
        self.env.service.checkin("owner")
        self.assertEqual(self.env.tick_at(0.5).holds, ["cold_start_hold"])
        self.assertEqual(self.env.tick_at(1).holds, [])

    def test_restart_during_hold_keeps_hold(self):
        self.env.tick_at(0)
        self.env.new_service()
        s = self.env.tick_at(0.5)
        self.assertEqual(s.hold_until, day(1))
        self.assertEqual(len(self.env.events("cold_start")), 1)

    def test_quick_restart_skips_hold_and_pause(self):
        self.arm()
        self.env.run_days(2, 10)
        self.env.new_service()
        s = self.env.tick_at(10 + 1 / 24)
        self.assertEqual(s.holds, [])
        self.assertEqual(len(self.env.facts().pauses), 1)  # only the very first start

    def test_long_downtime_is_a_pause_with_hold(self):
        self.arm()
        self.env.run_days(2, 50)
        self.env.new_service()
        s = self.env.tick_at(60)
        self.assertIn("cold_start_hold", s.holds)
        self.assertEqual(s.owner_deadline, day(84))
        self.assertEqual(self.env.tick_at(61).holds, [])
        self.assertEqual(self.env.run_days(62, 83).phase, "warning")
        self.assertEqual(self.env.tick_at(84).phase, "grace")

    def test_failing_ticks_do_not_multiply_the_pause(self):
        self.arm()
        self.env.run_days(2, 9)
        good = self.env.service._check_node

        def dies(now):
            raise RuntimeError("boom")
        self.env.service._check_node = dies
        for d in range(10, 16):              # every tick dies after the gap check, before last_tick
            self.env.clock.set(day(d))
            self.env.service.safe_tick()
        self.assertEqual(self.env.facts().last_tick, day(9))
        self.assertEqual(len(self.env.events("tick_error")), 6)
        self.env.service._check_node = good
        s = self.env.tick_at(16)
        self.assertEqual(s.owner_deadline, day(81))   # 7 days without a completed tick, counted once

    def test_torn_last_pause_line_is_ignored_then_dropped(self):
        self.arm()
        self.env.run_days(2, 9)
        ledger = self.env.store.dir / "pauses.jsonl"
        whole = ledger.read_text()
        with open(ledger, "a") as f:
            f.write('{"from": "2026-01-1')        # the crash came in the middle of an append
        s = self.env.tick_at(10)                   # read: the fragment is not a state error
        self.assertEqual(s.holds, [])
        self.assertEqual(s.owner_deadline, day(74))
        s = self.env.tick_at(20)                   # next pause: fragment dropped before the append
        self.assertEqual(s.owner_deadline, day(84))
        self.assertTrue(ledger.read_text().startswith(whole))
        self.assertEqual(len(ledger.read_text().splitlines()), len(whole.splitlines()) + 1)
        self.assertEqual(len(self.env.events("pauses_repaired")), 1)
        with open(ledger, "a") as f:
            f.write("garbage\n")                   # a complete bad line is still a state error
        self.assertEqual(self.env.tick_at(21).holds, ["state_files"])

    def test_decision_due_during_hold_waits(self):
        self.arm()
        self.env.run_days(2, 119)
        self.env.new_service()
        s = self.env.tick_at(123)        # 4 days down: pause pushes window end to 124, hold until 124
        self.assertEqual(s.wife_window_end, day(124))
        self.assertIsNone(self.env.facts().decision)
        self.env.tick_at(124)                # hold cleared at 124 and the window has ended
        self.assertEqual(self.env.facts().decision.choice, "F")


class TimelineTests(ServiceCase):
    def test_f_flow_dry_run(self):
        self.env.close()
        self.env = Env(dry_run=True)
        self.arm()
        s = self.env.run_days(2, 119)
        self.assertEqual(s.phase, "wife_window")
        self.assertIsNone(self.env.facts().decision)
        s = self.env.tick_at(120)
        f = self.env.facts()
        self.assertEqual(f.decision.choice, "F")
        self.assertEqual(f.decision.at, day(120))
        self.assertEqual(set(f.broadcasts), {"F1", "F2"})
        self.assertTrue(all(b.dry_run for b in f.broadcasts.values()))
        self.assertEqual(self.env.node.sent, [])
        views = self.env.service.views()
        self.assertEqual(views["wife"]["state"], "closed")
        self.assertEqual(views["family"]["state"], "inheritance")
        self.assertEqual(views["owner"]["state"], "decided")
        kinds = [e["kind"] for e in self.env.events()]
        self.assertIn("decision", kinds)
        self.assertEqual(kinds.count("dry_run_broadcast"), 2)
        grace = [a for a in self.env.alert_ids() if a.startswith("grace-wife")]
        self.assertEqual(len(grace), 46)  # day 74 .. 119, once a day; none on the decision tick

    def test_w_via_grace_checkin_then_confirmations(self):
        self.arm()
        self.env.run_days(2, 80)
        self.env.service.checkin("wife")
        s = self.env.run_days(81, 89)
        self.assertIsNone(self.env.facts().decision)
        self.assertEqual(s.w_selected_at, day(80))
        self.env.tick_at(90)
        f = self.env.facts()
        self.assertEqual(f.decision.choice, "W")
        self.assertEqual(self.env.node.sent, ["txid-W1", "txid-W2"])
        self.assertEqual(set(f.broadcasts), {"W1", "W2"})
        self.env.tick_at(91)
        self.assertEqual(self.env.node.sent, ["txid-W1", "txid-W2"])  # in mempool, no rebroadcast
        self.env.node.mine(1)
        self.env.tick_at(92)
        f = self.env.facts()
        self.assertEqual(f.broadcasts["W1"].blockhash, "hash800001")
        self.assertEqual(self.env.service.tracking["W1"]["confirmations"], 1)
        self.env.node.mine(4)
        self.env.tick_at(93)
        self.assertIsNone(self.env.facts().final_at)
        self.env.node.mine(1)
        s = self.env.tick_at(94)
        self.assertEqual(self.env.facts().final_at, day(94))
        self.assertEqual(s.phase, "final")
        self.assertEqual(self.env.service.views()["wife"]["state"], "inheritance")

    def test_w_via_wife_window_checkin_is_immediate(self):
        self.arm()
        self.env.run_days(2, 100)
        self.assertIsNone(self.env.facts().decision)
        self.env.service.checkin("wife")
        f = self.env.facts()
        self.assertEqual(f.decision.choice, "W")
        self.assertEqual(f.decision.at, day(100))
        self.assertEqual(self.env.node.sent, ["txid-W1", "txid-W2"])

    def test_owner_cancel_during_wife_window(self):
        self.arm()
        self.env.run_days(2, 100)
        self.assertEqual(self.env.service.situation.phase, "wife_window")
        self.env.service.checkin("owner")
        s = self.env.run_days(101, 130)
        self.assertIsNone(self.env.facts().decision)
        self.assertEqual(s.phase, "normal")
        self.assertEqual(s.owner_deadline, day(174))
        self.assertEqual(self.env.node.sent, [])
        self.assertEqual(self.env.run_days(131, 174).phase, "grace")

    def test_decide_once_and_false_triggered(self):
        self.arm()
        self.env.run_days(2, 120)
        self.assertEqual(self.env.facts().decision.choice, "F")
        self.env.tick_at(121)
        r = self.env.service.checkin("owner")
        self.assertTrue(r["after_decision"])
        self.assertEqual(self.env.facts().decision.choice, "F")
        self.assertEqual(self.env.service.views()["owner"]["state"], "decided")
        self.env.tick_at(125)
        self.env.service.checkin("wife")
        self.assertEqual(self.env.facts().decision.choice, "F")
        self.assertEqual(self.env.service.views()["wife"]["state"], "false_triggered")
        self.assertTrue(any(a.startswith("false-triggered") for a in self.env.alert_ids()))

    def test_zero_wife_window(self):
        self.env.close()
        self.env = Env(dry_run=False, timing={"wife_window_days": 0})
        self.arm()
        self.env.run_days(2, 89)
        self.assertIsNone(self.env.facts().decision)
        self.env.tick_at(90)
        self.assertEqual(self.env.facts().decision.choice, "F")


class BroadcastTests(ServiceCase):
    def test_crash_between_decision_and_broadcast(self):
        self.arm()
        self.env.run_days(2, 119)
        self.env.node.fail_send = NodeUnreachable("boom")
        self.env.clock.set(day(120))
        self.env.service.safe_tick()
        f = self.env.facts()
        self.assertEqual(f.decision.choice, "F")
        self.assertEqual(f.broadcasts, {})
        self.env.node.fail_send = None
        self.env.new_service()
        self.env.tick_at(120 + 1 / 24)
        f = self.env.facts()
        self.assertEqual(set(f.broadcasts), {"F1", "F2"})
        self.assertEqual(self.env.node.sent[-2:], ["txid-F1", "txid-F2"])

    def test_malformed_answer_while_tracking_does_not_stop_the_tick(self):
        self.arm()
        self.env.run_days(2, 120)
        good = self.env.node.rpc_gettxout
        self.env.node.rpc_gettxout = lambda *a: "not a dict"
        s = self.env.tick_at(121)
        self.assertEqual(s.phase, "decided")
        self.assertEqual(self.env.facts().last_tick, day(121))
        self.assertEqual(self.env.events("node_error")[-1]["during"], "broadcast_and_track")
        self.assertEqual(self.env.events("tick_error"), [])
        self.env.node.rpc_gettxout = good
        self.env.node.mine(6)
        self.env.tick_at(122)
        self.assertIsNotNone(self.env.facts().final_at)

    def test_block_arriving_mid_tick_does_not_shift_the_recorded_block(self):
        self.arm()
        self.env.run_days(2, 120)
        self.env.node.mine(1)
        mined_at = self.env.node.chain["txid-F1"]
        good, fired = self.env.node.rpc_gettxout, []

        def racing(*a):
            if not fired:
                fired.append(1)
                self.env.node.mine(1)        # after getblockchaininfo, before this answer
            return good(*a)
        self.env.node.rpc_gettxout = racing
        self.env.tick_at(121)
        bc = self.env.facts().broadcasts["F1"]
        self.assertEqual((bc.block_height, bc.blockhash), (mined_at, f"hash{mined_at}"))
        self.env.node.rpc_gettxout = good
        self.env.tick_at(121.01)
        self.assertEqual(self.env.service.tracking["F1"]["confirmations"], 2)

    def test_failed_first_transaction_does_not_block_the_second(self):
        self.arm()
        self.env.run_days(2, 119)
        self.env.node.reject["txid-F1"] = "min relay fee not met"
        self.env.tick_at(120)
        f = self.env.facts()
        self.assertEqual(f.decision.choice, "F")
        self.assertEqual(set(f.broadcasts), {"F2"})
        self.assertEqual(self.env.node.sent, ["txid-F1", "txid-F2"])
        self.assertIn("broadcast-failed:F1:2026-05-01", self.env.alert_ids())
        self.env.node.mine(6)
        self.env.tick_at(121)
        self.assertEqual(self.env.service.tracking["F2"]["confirmations"], 6)
        self.assertNotIn("F1", self.env.service.tracking)
        self.assertIsNone(self.env.facts().final_at)  # final needs both
        self.assertIn("broadcast-failed:F1:2026-05-02", self.env.alert_ids())
        del self.env.node.reject["txid-F1"]
        self.env.tick_at(122)
        self.assertEqual(set(self.env.facts().broadcasts), {"F1", "F2"})
        self.env.node.mine(6)
        self.env.tick_at(123)
        self.assertEqual(self.env.facts().final_at, day(123))

    def test_rebroadcast_when_evicted(self):
        self.arm()
        self.env.run_days(2, 120)
        self.assertEqual(self.env.node.sent, ["txid-F1", "txid-F2"])
        self.env.node.evict("txid-F1")
        self.env.tick_at(121)
        self.assertEqual(self.env.node.sent, ["txid-F1", "txid-F2", "txid-F1"])
        self.assertIn("rebroadcast", [e["kind"] for e in self.env.events()])
        self.assertIn("txid-F1", self.env.node.mempool)

    def test_rebroadcast_failure_alerts(self):
        self.arm()
        self.env.run_days(2, 120)
        self.env.node.evict("txid-F1")
        self.env.node.reject["txid-F1"] = "min relay fee not met"
        self.env.tick_at(121)
        self.assertIn("rebroadcast-failed:F1:2026-05-02", self.env.alert_ids())

    def test_dry_run_markers_are_resent_when_dry_run_is_turned_off(self):
        self.env.close()
        self.env = Env(dry_run=True)
        self.arm()
        self.env.run_days(2, 120)
        self.assertEqual(self.env.node.sent, [])
        self.env.cfg = self.env.cfg.__class__(**{**self.env.cfg.__dict__, "dry_run": False})
        self.env.new_service()
        self.env.tick_at(121)
        self.assertEqual(self.env.node.sent, ["txid-F1", "txid-F2"])
        self.assertFalse(any(b.dry_run for b in self.env.facts().broadcasts.values()))

    def test_already_in_chain_counts_as_broadcast(self):
        self.arm()
        self.env.run_days(2, 119)
        # someone else broadcast F1 and it confirmed deeply; our output was already spent
        self.env.node.rpc_sendrawtransaction(self.env.hex["F1"])
        self.env.node.mine(3)
        self.env.node.spend_externally(("txid-F1", 0))
        self.env.tick_at(120)
        f = self.env.facts()
        self.assertEqual(set(f.broadcasts), {"F1", "F2"})


class HoldConditionTests(ServiceCase):
    def run_to_decision_moment(self):
        self.arm()
        self.env.run_days(2, 119)

    def assert_blocked_then_released(self, reason, release):
        s = self.env.tick_at(120)
        self.assertIn(reason, s.holds)
        self.assertIsNone(self.env.facts().decision)
        self.assertEqual(self.env.node.sent, [])
        self.assertTrue(any(a.startswith(f"fault:{reason}") for a in self.env.alert_ids()), self.env.alert_ids())
        release()
        s = self.env.tick_at(121)
        self.assertEqual(s.holds, [])
        self.assertEqual(self.env.facts().decision.choice, "F")

    def test_node_unreachable(self):
        self.run_to_decision_moment()
        self.env.node.reachable = False
        self.assert_blocked_then_released("node_unreachable", lambda: setattr(self.env.node, "reachable", True))

    def test_malformed_node_answers_hold_instead_of_crashing(self):
        self.run_to_decision_moment()
        good = self.env.node.rpc_getblockchaininfo
        now = lambda: self.env.clock.now().timestamp()
        answers = [
            lambda: None,
            lambda: [],
            lambda: {"blocks": 1, "initialblockdownload": False, "time": now()},              # no headers
            lambda: {"blocks": "x", "headers": 1, "initialblockdownload": False, "time": now()},
            lambda: {"blocks": 1, "headers": 1, "initialblockdownload": False, "time": "soon"},
            lambda: {"blocks": 1, "headers": 1, "initialblockdownload": False, "time": 1e300},
        ]
        for i, answer in enumerate(answers):
            self.env.node.rpc_getblockchaininfo = answer
            s = self.env.tick_at(120 + i)
            self.assertEqual(s.holds, ["node_unreachable"], i)
            self.assertIn("malformed", self.env.service.node_status["error"])
            self.assertEqual(self.env.facts().last_tick, day(120 + i))   # the tick completed
        self.assertIsNone(self.env.facts().decision)
        self.assertEqual(self.env.events("tick_error"), [])
        self.env.node.rpc_getblockchaininfo = good
        self.env.tick_at(120 + len(answers))
        self.assertEqual(self.env.facts().decision.choice, "F")

    def test_aborted_validation_holds(self):
        self.run_to_decision_moment()
        good = self.env.node.rpc_decoderawtransaction
        self.env.node.rpc_decoderawtransaction = lambda hexstr: "not a dict"
        self.env.service.last_validation = None
        s = self.env.tick_at(120)
        self.assertEqual(s.holds, ["node_unreachable"])
        self.assertIsNone(self.env.facts().decision)
        self.assertEqual(len(self.env.events("validation_aborted")), 1)
        self.env.node.rpc_decoderawtransaction = good
        self.env.tick_at(121)
        self.assertEqual(self.env.facts().decision.choice, "F")

    def test_malformed_alert_file_holds_even_at_start(self):
        self.arm()
        # each alone, so the result does not depend on the order the directory is listed in
        for n, content in enumerate(("{", "[]", '"text"', "{}", '{"id": "x", "delivered_at": 5}')):
            with self.subTest(content):
                bad = self.env.store.alerts_dir / "bad.json"
                bad.write_text(content)
                service = self.env.new_service()
                self.env.clock.set(day(2 + n))
                service.start()                          # must not raise
                self.assertIsNotNone(service.situation)
                self.assertEqual(service.situation.holds, ["state_files"])
                self.assertIn("alerts/bad.json", service.state_error)
                self.assertEqual(self.env.events("tick_error"), [])
                self.assertIn("notifications", service.health())
                bad.unlink()
        self.assertTrue(any(a.startswith("fault:state_files") for a in self.env.alert_ids()))

    def test_node_in_initial_block_download(self):
        self.run_to_decision_moment()
        self.env.node.ibd = True
        self.assert_blocked_then_released("node_not_synced", lambda: setattr(self.env.node, "ibd", False))

    def test_node_behind_headers(self):
        self.run_to_decision_moment()
        self.env.node.headers_ahead = 5
        self.assert_blocked_then_released("node_not_synced", lambda: setattr(self.env.node, "headers_ahead", 0))

    def test_clock_skew(self):
        self.run_to_decision_moment()
        self.env.node.tip_time_fixed = day(119.5)  # tip 12 hours behind the clock
        self.assert_blocked_then_released("clock_skew", lambda: setattr(self.env.node, "tip_time_fixed", None))

    def test_tx_checksum(self):
        self.run_to_decision_moment()
        self.env.corrupt_tx_file("W2")
        self.env.service.last_validation = None  # force the daily validation now
        good = (self.env.txdir / "w2.hex").write_text

        def release():
            good(self.env.hex["W2"] + "\n")
            self.env.service.last_validation = None
        self.assert_blocked_then_released("tx_checksum:W2", release)

    def test_tx_pays_wrong_address(self):
        self.env.close()
        self.env = Env(dry_run=False, pay={"F1": WHW})  # F1 must pay FMHW
        self.run_to_decision_moment()
        self.env.service.last_validation = None

        def release():
            self.env.node.txs[self.env.hex["F1"]]["vout"][0]["scriptPubKey"]["address"] = "bc1qfamily"
            self.env.service.last_validation = None
        self.assert_blocked_then_released("tx_address:F1", release)

    def test_state_files_unreadable(self):
        self.run_to_decision_moment()
        path = self.env.store.dir / "checkin_owner.json"
        saved = path.read_text()
        path.write_text("{garbage")
        s = self.env.tick_at(120)
        self.assertEqual(s.holds, ["state_files"])
        self.assertIsNotNone(self.env.service.state_error)
        self.assertTrue(any(a.startswith("fault:state_files") for a in self.env.alert_ids()))
        with self.assertRaises(StateError):
            self.env.facts()
        path.write_text(saved)
        s = self.env.tick_at(121)
        self.assertEqual(s.holds, [])
        self.assertEqual(self.env.facts().decision.choice, "F")

    def test_unreadable_pause_ledger_holds_and_alerts(self):
        self.run_to_decision_moment()
        ledger = self.env.store.dir / "pauses.jsonl"
        ledger.write_bytes(b'{"from": null, "to": "2026-01-01T00:00:00Z", "hold_until": null}\n\xff\n')
        s = self.env.tick_at(120)
        self.assertEqual(s.holds, ["state_files"])
        self.assertIn("pauses.jsonl", self.env.service.state_error)
        self.assertTrue(any(a.startswith("fault:state_files") for a in self.env.alert_ids()))
        self.assertEqual(self.env.events("tick_error"), [])
        self.assertFalse((self.env.store.dir / "decision.json").exists())   # held: no decision
        self.assertEqual(self.env.service.views()["owner"]["holds"], ["state_files"])
        ledger.unlink()
        s = self.env.tick_at(121)
        self.assertEqual(s.holds, [])
        self.assertEqual(self.env.facts().decision.choice, "F")

    def test_clock_went_backwards(self):
        self.run_to_decision_moment()
        self.env.clock.set(day(118))
        s = self.env.service.tick()
        self.assertIn("clock_went_backwards", s.holds)
        self.assertEqual(self.env.facts().last_tick, day(119))


class ValidationTests(ServiceCase):
    def test_spent_input_alerts_without_hold(self):
        self.arm()
        self.env.node.spend_externally(("funding", 0))
        self.env.service.last_validation = None
        s = self.env.tick_at(2)
        self.assertEqual(s.holds, [])
        ids = self.env.alert_ids()
        self.assertIn("tx-problem:W1:2026-01-03", ids)
        self.assertIn("tx-problem:F1:2026-01-03", ids)
        self.assertNotIn("tx-problem:W2:2026-01-03", ids)
        self.assertFalse(self.env.service.tx_reports["W1"].inputs_unspent)
        self.assertEqual(self.env.service.tx_reports["W2"].fee_rate_sat_vb, 50.0)

    def test_validation_only_covers_chosen_pair_after_decision(self):
        self.arm()
        self.env.run_days(2, 120)
        self.env.service.last_validation = None
        self.env.tick_at(121)
        self.assertEqual(set(self.env.service.tx_reports), {"F1", "F2"})
        self.assertEqual(self.env.service.tx_reports["F1"].inputs_unspent, None)  # skipped: already sent
        self.assertTrue(self.env.service.tx_reports["F1"].address_ok)

    def test_health_report(self):
        self.arm()
        h = self.env.service.health()
        self.assertTrue(h["node"]["reachable"])
        self.assertEqual(set(h["transactions"]), {"W1", "W2", "F1", "F2"})
        self.assertEqual(h["pending_alerts"], [])
        self.assertEqual(h["missed_alerts"], 0)
        self.assertEqual(h["notifications"]["backend"], "events_log")
        self.assertEqual(h["notifications"]["owner"]["last_notified_on"], "2026-01-02T12:00:00Z")
        self.assertIsNone(h["notifications"]["wife"]["last_notified_on"])
        self.assertFalse(h["dry_run"])


class FailingNotifier:
    name = "failing"

    def __init__(self):
        self.fail = True

    def deliver(self, alert, now):
        if self.fail:
            raise RuntimeError("telegram down")


class AlertDeliveryTests(ServiceCase):
    def test_grace_clock_starts_at_delivery_or_max_wait(self):
        n = FailingNotifier()
        self.env.new_service(n)
        self.arm()
        self.env.run_days(2, 74)
        self.assertTrue(self.env.store.pending_alerts())
        s = self.env.tick_at(74.5)
        self.assertEqual(s.grace_end, day(91))        # not delivered: max wait of one day
        n.fail = False
        s = self.env.tick_at(74.75)                    # delivered now
        self.assertEqual(s.grace_clock_start, day(74.75))
        self.assertEqual(s.grace_end, day(90.75))
        a = self.env.store.read_alert("grace-wife:2026-03-16T12:00:00Z:0")
        self.assertEqual((a["accepted_at"], a["attempts"]), ("2026-03-17T06:00:00Z", 3))
        # the owner's backlog of 74 days does not hold her alert back: they take turns

    def test_undelivered_alert_caps_grace_at_max_wait(self):
        n = FailingNotifier()
        self.env.new_service(n)
        self.arm()
        self.env.run_days(2, 76)
        n.fail = False
        s = self.env.tick_at(77)
        self.assertEqual(s.grace_clock_start, day(75))
        self.assertEqual(s.grace_end, day(91))


class TelegramDeliveryTests(ServiceCase):
    OWNER, WIFE = 111, 222

    def setUp(self):
        super().setUp()
        self.tg = FakeTelegram()
        cfg = TelegramConfig(Path("/nonexistent"), self.OWNER, self.WIFE, 5.0, "https://api.example", TOKEN)
        self.env.new_service(TelegramNotifier(cfg, self.env.store, post=self.tg))

    def restart(self):
        self.env.new_service(self.env.service.notifier)

    def test_outage_then_recovery_sends_one_per_series(self):
        self.arm()
        self.env.run_days(2, 59)
        self.tg.down = True
        self.env.run_days(60, 69)                       # ten daily warnings, none accepted
        self.tg.down = False
        self.env.run_days(70, 72)
        near = [t for t in self.tg.texts(self.OWNER) if "is near" in t]
        self.assertEqual(len(near), 3, near)            # days 70, 71, 72; 60..69 are missed
        self.assertIn("(4 days left)", near[0])
        h = queue.sendable(self.env.store.read_alerts())
        self.assertEqual((len(h[0]), len(h[1])), (0, 10))
        accepted = [a for a in all_alerts(self.env) if a["accepted_at"]]
        self.assertEqual(len(accepted), len(self.tg.accepted))       # one send per accepted alert
        self.assertEqual(len({a["receipt"]["message_id"] for a in accepted}), len(accepted))

    def test_missed_first_wife_alert_starts_grace_at_max_wait(self):
        self.arm()
        self.env.run_days(2, 73)
        self.tg.down = True
        self.env.run_days(74, 76)
        self.tg.down = False
        s = self.env.run_days(77, 77)
        self.assertEqual(s.grace_clock_start, day(75))
        self.assertIsNone(self.env.store.read_alert("grace-wife:2026-03-16T12:00:00Z:0")["accepted_at"])
        self.assertEqual(len(self.tg.texts(self.WIFE)), 1)          # only the newest, day 77

    def test_blocked_wife_alerts_owner_and_keeps_retrying(self):
        self.arm()
        self.env.run_days(2, 73)
        self.tg.blocked.add(self.WIFE)
        self.env.run_days(74, 76)
        broken = [a for a in self.env.alert_ids() if a.startswith("channel-broken:")]
        self.assertEqual(broken, ["channel-broken:wife:2026-03-16", "channel-broken:wife:2026-03-17",
                                  "channel-broken:wife:2026-03-18"])
        texts = self.tg.texts(self.OWNER)
        self.assertTrue(any("does not accept messages to your wife" in t and "blocked" in t for t in texts))
        newest = self.env.store.read_alert("grace-wife:2026-03-16T12:00:00Z:2")
        self.assertTrue(newest["permanent"])
        self.assertIn("403", newest["last_error"])
        self.tg.blocked.clear()                          # she pressed Start
        self.env.run_days(77, 77)
        self.assertEqual(len(self.tg.texts(self.WIFE)), 1)
        self.assertNotIn("channel-broken:wife:2026-03-19", self.env.alert_ids())

    def test_nothing_stale_after_the_decision(self):
        self.arm()
        self.env.run_days(2, 73)
        self.tg.blocked.add(self.WIFE)                   # her grace alerts stay pending
        n = 74
        while self.env.facts().decision is None:
            self.env.tick_at(n)
            n += 1
        decided = n - 1
        self.env.node.reachable = False                  # a hold after the decision: silent
        self.env.run_days(n, n + 1)
        self.env.node.reachable = True
        before = len(self.tg.texts(self.OWNER))
        self.tg.blocked.clear()
        self.env.run_days(n + 2, n + 2)
        self.assertEqual(self.tg.texts(self.WIFE),
                         ["The waiting time ended without your check-in. The family transfer has been started."])
        ids = self.env.alert_ids()
        self.assertFalse([a for a in ids if a.startswith(("fault:", "fault-cleared:"))
                          and a.rsplit(":", 1)[1] > day(decided).date().isoformat()], ids)
        self.assertFalse([a for a in ids if a.startswith("channel-broken:")
                          and a.rsplit(":", 1)[1] > day(decided).date().isoformat()], ids)
        new = self.tg.texts(self.OWNER)[before:]
        self.assertFalse([t for t in new if "check in" in t.lower() or "holding" in t], new)
        self.assertEqual(self.env.service.health()["notifications"]["wife"]["pending"], 0)
        self.assertGreater(self.env.service.health()["missed_alerts"], 0)
        self.env.service.checkin("owner")
        self.env.service.checkin("wife")
        self.assertFalse([a for a in self.env.alert_ids() if a.startswith(("checkin-owner:", "checkin-wife:"))])

    def test_transport_failure_stops_sending_and_the_tick_completes(self):
        self.arm()
        self.env.run_days(2, 73)
        for switch in ("timeout", "down", "status_5xx", "rate_limited"):
            with self.subTest(switch):
                setattr(self.tg, switch, True)
                calls = self.tg.calls
                d = 74 + ["timeout", "down", "status_5xx", "rate_limited"].index(switch)
                self.env.tick_at(d)                     # owner and wife alerts are both due
                self.assertEqual(self.tg.calls - calls, 1)
                self.assertEqual(self.env.facts().last_tick, day(d))
                setattr(self.tg, switch, False)

    def test_per_tick_limit_and_store_failures(self):
        self.arm()
        for n in range(10):
            self.env.store.create_alert(f"x:{n}", "owner", "armed", f"backlog {n}", day(1))
        calls = self.tg.calls
        self.env.tick_at(2)
        self.assertEqual(self.tg.calls - calls, MAX_SENDS_PER_TICK)
        self.env.store.update_alert = lambda alert: (_ for _ in ()).throw(OSError("disk full"))
        self.env.tick_at(3)
        self.assertEqual(self.env.facts().last_tick, day(3))
        self.assertTrue(any("alert_error" in line for line in self.env.logs))

    def test_events_log_failure_after_telegram_accepted_does_not_resend(self):
        self.arm()
        self.env.service.notifier._log = self.env.logs.append
        good = self.env.store.log_event
        self.env.store.log_event = lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
        self.env.run_days(60, 60)                       # the first owner warning
        self.env.store.log_event = good
        sent = len(self.tg.accepted)
        self.assertGreater(sent, 0)
        accepted = [a for a in all_alerts(self.env) if a["accepted_at"]]
        self.assertEqual(len(accepted), sent)           # every send counts as accepted
        self.assertTrue(any("events_log_error" in line for line in self.env.logs))
        self.env.tick_at(60.5)
        self.assertEqual(len(self.tg.accepted), sent)   # nothing sent twice

    def test_raising_notifier_never_stops_the_tick(self):
        self.arm()
        n = FailingNotifier()
        self.env.new_service(n)
        self.env.run_days(2, 80)
        self.assertEqual(self.env.facts().last_tick, day(80))
        self.assertEqual(self.env.events("tick_error"), [])
        a = self.env.store.read_alert("grace-wife:2026-03-16T12:00:00Z:6")
        self.assertIn("telegram down", a["last_error"])
        self.assertFalse(a["permanent"])

    def test_restart_does_not_resend(self):
        self.arm()
        self.env.run_days(2, 70)
        self.restart()
        self.env.tick_at(70.5)
        self.restart()
        self.env.run_days(71, 76)
        accepted = [a for a in all_alerts(self.env) if a["accepted_at"]]
        self.assertEqual(len(accepted), len(self.tg.accepted))

    def test_health_shows_notifications_pending_and_missed(self):
        self.arm()
        self.env.run_days(2, 73)
        self.tg.blocked.add(self.WIFE)
        self.env.run_days(74, 76)
        h = self.env.service.health()
        n = h["notifications"]
        self.assertEqual(n["backend"], "telegram")
        self.assertEqual(n["owner"]["last_notified_on"], "2026-03-18T12:00:00Z")
        self.assertIsNone(n["wife"]["last_notified_on"])
        self.assertEqual(n["wife"]["pending"], 1)
        [p] = [p for p in h["pending_alerts"] if p["to"] == "wife"]
        self.assertEqual(p, {"id": "grace-wife:2026-03-16T12:00:00Z:2", "to": "wife", "kind": "grace",
                             "created_at": "2026-03-18T12:00:00Z", "attempts": 1,
                             "last_attempt_at": "2026-03-18T12:00:00Z",
                             "last_error": "HTTP 403: Forbidden: bot was blocked by the user", "permanent": True})
        self.assertEqual(h["missed_alerts"], 2)          # grace-wife :0 and :1
        self.tg.blocked.clear()
        self.env.tick_at(77)
        n = self.env.service.health()["notifications"]
        self.assertEqual(n["wife"], {"last_notified_on": "2026-03-19T12:00:00Z", "last_kind": "grace",
                                     "last_id": "grace-wife:2026-03-16T12:00:00Z:3", "pending": 0})

    def test_notify_test(self):
        self.arm()
        before = len(self.env.alert_ids())
        r = self.env.service.notify_test("wife")
        self.assertEqual(r, {"accepted": True, "to": "wife", "accepted_at": "2026-01-02T12:00:00Z",
                             "receipt": {"message_id": self.tg.accepted[-1]["message_id"]}})
        self.assertIn("Test message", self.tg.texts(self.WIFE)[0])
        self.assertEqual(len(self.env.alert_ids()), before)       # no alert file
        self.assertIsNone(self.env.service.health()["notifications"]["wife"]["last_notified_on"])
        self.tg.blocked.add(self.OWNER)
        r = self.env.service.notify_test("owner")
        self.assertEqual(r["accepted"], False)
        self.assertIn("403", r["error"])
        self.assertEqual([e["accepted"] for e in self.env.events("notify_test")], [True, False])

    def test_old_alert_file_is_sent_and_rewritten(self):
        self.arm()
        old = {"id": "fault:clock_skew:2026-01-02", "to": "owner", "kind": "fault", "text": "old text",
               "created_at": "2026-01-02T12:00:00Z", "delivered_at": None, "attempts": 2, "last_error": "x"}
        self.env.store.update_alert(old)
        self.env.tick_at(2)
        a = self.env.store.read_alert(old["id"])
        self.assertNotIn("delivered_at", a)
        self.assertEqual((a["accepted_at"], a["attempts"]), ("2026-01-03T12:00:00Z", 3))
        self.assertIn("old text", self.tg.texts(self.OWNER))


def all_alerts(env):
    return [json.loads(p.read_text()) for p in sorted(env.store.alerts_dir.glob("*.json"))]


class ScheduleTests(ServiceCase):
    def test_cold_start_hold_enforced_then_cleared(self):
        self.arm()
        ids = self.env.alert_ids()
        self.assertIn("hold:2026-01-02T12:00:00Z", ids)
        self.assertIn("hold-cleared:2026-01-02T12:00:00Z", ids)
        self.assertIn("armed:2026-01-01T12:00:00Z", ids)    # the end of the unarmed hold
        self.env.run_days(2, 50)
        self.env.new_service()
        self.env.tick_at(60)                                # long downtime: new hold until 61
        self.assertIn("hold:2026-03-03T12:00:00Z", self.env.alert_ids())
        self.assertNotIn("hold-cleared:2026-03-03T12:00:00Z", self.env.alert_ids())
        self.env.tick_at(61)
        self.assertIn("hold-cleared:2026-03-03T12:00:00Z", self.env.alert_ids())

    def test_fault_enforced_then_cleared_once_per_day(self):
        self.arm()
        self.env.run_days(2, 29)
        for i, up in enumerate((False, True, False, True, False)):   # a flapping node on day 30
            self.env.node.reachable = up
            self.env.tick_at(30 + i / 10)
        pair = ["fault-cleared:node_unreachable:2026-01-31", "fault:node_unreachable:2026-01-31"]
        ids = [a for a in self.env.alert_ids() if "node_unreachable" in a]
        self.assertEqual(ids, pair)                         # one pair per reason and day
        self.env.tick_at(31)                                # still down on a new day: enforced again
        self.env.node.reachable = True
        self.env.tick_at(31.5)
        ids = [a for a in self.env.alert_ids() if "node_unreachable" in a]
        self.assertEqual(ids, sorted(pair + ["fault-cleared:node_unreachable:2026-02-01", "fault:node_unreachable:2026-02-01"]))

    def test_state_files_hold_cleared_on_first_good_tick(self):
        self.arm()
        path = self.env.store.dir / "checkin_owner.json"
        saved = path.read_text()
        path.write_text("{garbage")
        self.env.tick_at(2)
        path.write_text(saved)
        self.env.tick_at(3)
        self.assertIn("fault-cleared:state_files:2026-01-03", self.env.alert_ids())

    def test_wife_gets_nothing_before_the_deadline(self):
        self.arm()
        self.env.run_days(2, 73)
        self.env.service.checkin("wife")                    # early: stored and ignored
        self.env.run_days(74, 74)
        wife = sorted(a["id"] for a in all_alerts(self.env) if a["to"] == "wife")
        self.assertEqual(wife, ["grace-wife:2026-03-16T12:00:00Z:0"])
        [a] = [a for a in all_alerts(self.env) if a["kind"] == "checkin_wife"]
        self.assertIn("before your deadline", a["text"])

    def test_restarts_change_no_alert(self):
        def timeline(env, restart):
            env.service.start()
            env.service.checkin("owner")
            for d in range(1, 125):
                if restart:
                    env.new_service()
                env.tick_at(d)
                if d == 30:
                    env.service.checkin("owner")
                if d == 110:
                    env.service.checkin("wife")
            return env.alert_ids()
        plain = timeline(self.env, False)
        other = Env(dry_run=False)
        self.addCleanup(other.close)
        self.assertEqual(timeline(other, True), plain)
        self.assertEqual(len([a for a in plain if a.startswith("checkin-owner:")]), 1)
        self.assertEqual(len([a for a in plain if a.startswith("decision:")]), 2)

    def test_every_kind_renders_without_txids_or_addresses(self):
        texts = {}

        def collect(env):
            self.assertEqual(env.events("tick_error"), [])
            for a in all_alerts(env):
                texts.setdefault((a["kind"], a["to"]), a["text"])
                for secret in ("txid-", "hash8", "bc1q", "funding", "0.9999"):
                    self.assertNotIn(secret, a["text"], a["id"])

        # F: weekly, warning, grace, wife window, fault, decision, broadcast, final, late wife
        self.arm()
        self.env.run_days(2, 29)
        self.env.node.reachable = False
        self.env.tick_at(30)
        self.env.node.reachable = True
        self.env.run_days(31, 120)
        self.env.node.mine(6)
        self.env.tick_at(121)
        self.env.service.checkin("owner")
        self.env.tick_at(125)
        self.env.service.checkin("wife")
        collect(self.env)

        # W with a failing and then a rejected rebroadcast, and a spent input
        env = Env(dry_run=False)
        self.addCleanup(env.close)
        env.service.start()
        env.service.checkin("owner")
        env.run_days(1, 80)
        env.service.checkin("wife")
        env.node.reject["txid-W1"] = "min relay fee not met"
        env.run_days(81, 91)
        del env.node.reject["txid-W1"]
        env.run_days(92, 92)
        env.node.evict("txid-W1")
        env.node.reject["txid-W1"] = "min relay fee not met"
        env.run_days(93, 93)
        collect(env)

        env = Env(dry_run=True)
        self.addCleanup(env.close)
        env.service.start()
        env.service.checkin("owner")
        env.node.spend_externally(("funding", 0))
        env.run_days(1, 120)
        collect(env)

        expected = set(messages.TEXTS) - {("test", "owner"), ("test", "wife")}  # notify-test only
        env = Env(dry_run=True)                                     # a broken wife channel
        self.addCleanup(env.close)
        tg = FakeTelegram()
        tg.blocked.add(222)
        cfg = TelegramConfig(Path("/nonexistent"), 111, 222, 5.0, "https://api.example", TOKEN)
        env.new_service(TelegramNotifier(cfg, env.store, post=tg))
        env.service.start()
        env.service.checkin("owner")
        env.run_days(1, 10)
        env.service.checkin("owner")                                # checkin_owner (before a decision)
        env.run_days(11, 85)
        collect(env)
        self.assertEqual(set(texts), expected, sorted(expected - set(texts)))


if __name__ == "__main__":
    unittest.main()
