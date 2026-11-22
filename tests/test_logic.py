import unittest
from dataclasses import replace
from datetime import timedelta

from heirdrop.logic import (Decision, Facts, Pause, advance, derive, family_view,
                           grace_alert_id, owner_view, wife_view)
from tests.helpers import DEFAULT_TIMING as T, T0, armed_facts, day


def at(n, facts, timing=T, faults=()):
    return derive(timing, facts, day(n), faults)


class AdvanceTests(unittest.TestCase):
    def test_no_pauses(self):
        self.assertEqual(advance(T0, timedelta(days=74), []), day(74))

    def test_pause_inside_interval_pushes_out(self):
        pauses = [Pause(day(50), day(60), None)]
        self.assertEqual(advance(T0, timedelta(days=74), pauses), day(84))

    def test_pause_before_start_ignored(self):
        pauses = [Pause(day(-5), day(-1), None)]
        self.assertEqual(advance(T0, timedelta(days=74), pauses), day(74))

    def test_pause_straddling_start_counts_only_after(self):
        pauses = [Pause(day(-2), day(3), None)]
        self.assertEqual(advance(T0, timedelta(days=74), pauses), day(77))

    def test_pause_after_deadline_ignored(self):
        pauses = [Pause(day(80), day(90), None)]
        self.assertEqual(advance(T0, timedelta(days=74), pauses), day(74))

    def test_pause_pulled_in_by_earlier_pause(self):
        # second pause starts after the naive deadline but before the shifted one
        pauses = [Pause(day(70), day(75), None), Pause(day(76), day(78), None)]
        self.assertEqual(advance(T0, timedelta(days=74), pauses), day(81))

    def test_overlapping_pauses_count_once(self):
        # the ledger after ticks that died before writing last_tick: same start, growing end
        pauses = [Pause(day(50), day(53), None), Pause(day(50), day(54), None), Pause(day(50), day(55), None)]
        self.assertEqual(advance(T0, timedelta(days=74), pauses), day(79))
        # partial overlap and a pause contained in another
        pauses = [Pause(day(10), day(20), None), Pause(day(15), day(25), None), Pause(day(11), day(12), None)]
        self.assertEqual(advance(T0, timedelta(days=74), pauses), day(89))

    def test_first_start_pause_has_no_effect(self):
        self.assertEqual(advance(T0, timedelta(days=74), [Pause(None, day(1), day(2))]), day(74))


class PhaseTests(unittest.TestCase):
    def test_unarmed(self):
        s = at(0, armed_facts().__class__())
        self.assertEqual(s.phase, "unarmed")
        self.assertIn("unarmed", s.holds)
        self.assertEqual(wife_view(s, s and armed_facts().__class__())["state"], "normal")

    def test_timeline_without_wife(self):
        f = armed_facts()
        self.assertEqual(at(10, f).phase, "normal")
        self.assertEqual(at(59.9, f).phase, "normal")
        self.assertEqual(at(60, f).phase, "warning")
        self.assertEqual(at(73.9, f).phase, "warning")
        s = at(74, f)
        self.assertEqual(s.phase, "grace")
        self.assertEqual(s.period, "owner_grace")
        # grace clock without delivery starts after the max wait (1 day)
        self.assertEqual(s.grace_end, day(91))
        self.assertEqual(s.wife_window_end, day(121))
        s = at(91, f)
        self.assertEqual(s.phase, "wife_window")
        self.assertIsNone(s.decision_due)
        self.assertEqual(at(120.9, f).decision_due, None)
        self.assertEqual(at(121, f).decision_due, "F")

    def test_grace_clock_starts_at_alert_delivery(self):
        f = armed_facts()
        f.alert_accepted[grace_alert_id(day(74))] = day(74) + timedelta(minutes=1)
        s = at(75, f)
        self.assertEqual(s.grace_clock_start, day(74) + timedelta(minutes=1))
        self.assertEqual(s.grace_end, day(90) + timedelta(minutes=1))
        self.assertEqual(s.wife_window_end, day(120) + timedelta(minutes=1))

    def test_late_delivery_capped_by_max_wait(self):
        f = armed_facts()
        f.alert_accepted[grace_alert_id(day(74))] = day(77)
        self.assertEqual(at(78, f).grace_clock_start, day(75))

    def test_time_left_in_period(self):
        f = armed_facts()
        self.assertEqual(at(80, f).time_left(), 11 * 86400)


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.f = armed_facts()
        self.f.alert_accepted[grace_alert_id(day(74))] = day(74)

    def test_wife_checkin_during_grace_decides_w_at_grace_end(self):
        self.f.wife_checkin = day(80)
        self.assertEqual(at(80, self.f).w_selected_at, day(80))
        self.assertIsNone(at(89.9, self.f).decision_due)
        self.assertEqual(at(90, self.f).decision_due, "W")

    def test_wife_checkin_during_window_decides_w_immediately(self):
        self.assertIsNone(at(99, self.f).decision_due)
        self.f.wife_checkin = day(100)
        self.assertEqual(at(100, self.f).decision_due, "W")

    def test_no_wife_checkin_decides_f_at_window_end(self):
        self.assertIsNone(at(119.9, self.f).decision_due)
        self.assertEqual(at(120, self.f).decision_due, "F")

    def test_wife_checkin_before_owner_deadline_does_not_select_w(self):
        self.f.wife_checkin = day(73.9)
        self.assertIsNone(at(100, self.f).w_selected_at)
        self.assertEqual(at(120, self.f).decision_due, "F")

    def test_late_wife_checkin_while_held_still_selects_w(self):
        # decision F was due at day 120 but a hold blocked it; she checks in on 121
        self.f.wife_checkin = day(121)
        self.assertEqual(at(122, self.f).decision_due, "W")

    def test_owner_checkin_during_wife_window_cancels(self):
        self.f.wife_checkin = day(95)
        self.f.owner_checkin = day(100)
        s = at(100, self.f)
        self.assertEqual(s.phase, "normal")
        self.assertIsNone(s.decision_due)
        self.assertIsNone(s.w_selected_at)  # old selection does not carry over
        self.assertEqual(s.owner_deadline, day(174))

    def test_zero_wife_window_decides_at_grace_end(self):
        timing = replace(T, wife_window=timedelta(0))
        self.assertIsNone(at(89.9, self.f, timing).decision_due)
        self.assertEqual(at(90, self.f, timing).decision_due, "F")
        self.f.wife_checkin = day(80)
        self.assertEqual(at(90, self.f, timing).decision_due, "W")

    def test_recorded_decision_is_fixed(self):
        self.f.decision = Decision("F", day(120))
        self.f.owner_checkin = day(121)  # too late
        s = at(121, self.f)
        self.assertEqual(s.phase, "decided")
        self.assertIsNone(s.decision_due)
        self.assertEqual(s.decision.choice, "F")

    def test_hold_blocks_but_does_not_change_due_decision(self):
        self.f.pauses.append(Pause(day(100), day(119), day(120.5)))
        s = at(120, self.f)
        self.assertIn("cold_start_hold", s.holds)
        self.assertFalse(s.may_act)
        # the pause pushed everything by 19 days, so no decision is due yet
        self.assertIsNone(s.decision_due)
        self.assertEqual(s.wife_window_end, day(139))
        s = at(139, self.f)
        self.assertTrue(s.may_act)
        self.assertEqual(s.decision_due, "F")


class PauseAndHoldTests(unittest.TestCase):
    def test_downtime_pushes_owner_deadline(self):
        f = armed_facts(wife_day=10)
        f.pauses.append(Pause(day(50), day(60), None))
        s = at(61, f)
        self.assertEqual(s.owner_deadline, day(84))

    def test_downtime_during_grace_extends_grace(self):
        f = armed_facts()
        f.alert_accepted[grace_alert_id(day(74))] = day(74)
        f.pauses.append(Pause(day(80), day(85), None))
        s = at(86, f)
        self.assertEqual(s.owner_deadline, day(74))
        self.assertEqual(s.grace_end, day(95))
        self.assertEqual(s.wife_window_end, day(125))

    def test_hold_until_from_latest_pause(self):
        f = armed_facts()
        f.pauses += [Pause(day(1), day(2), day(3)), Pause(day(5), day(6), day(7))]
        s = at(6.5, f)
        self.assertEqual(s.hold_until, day(7))
        self.assertEqual(s.hold_remaining(), 43200)
        self.assertEqual(at(7, f).hold_until, None)

    def test_clock_backwards_holds(self):
        f = armed_facts()
        f.last_tick = day(10)
        self.assertIn("clock_went_backwards", at(9, f).holds)
        self.assertTrue(at(10, f).may_act)

    def test_checkin_in_future_holds(self):
        f = armed_facts(owner_day=1)
        self.assertIn("owner_checkin_in_future", at(0, f).holds)

    def test_external_faults_hold(self):
        s = at(10, armed_facts(), faults=["node_unreachable"])
        self.assertEqual(s.holds, ["node_unreachable"])
        self.assertEqual(s.phase, "normal")


class ViewTests(unittest.TestCase):
    def test_wife_states(self):
        f = armed_facts()
        f.alert_accepted[grace_alert_id(day(74))] = day(74)
        self.assertEqual(wife_view(at(10, f), f)["state"], "normal")
        v = wife_view(at(73.9, f), f)  # owner warning does not involve her
        self.assertEqual(v["state"], "normal")
        self.assertIsNone(v["deadline"])
        self.assertNotIn("overdue", v)
        v = wife_view(at(75, f), f)
        self.assertEqual(v["state"], "grace")
        self.assertEqual(v["deadline"], "2026-05-01T12:00:00Z")  # day 120
        self.assertEqual(v["period"], "owner_grace")
        self.assertEqual(wife_view(at(89.9, f), f)["state"], "grace")
        self.assertEqual(wife_view(at(90, f), f)["state"], "wife_window")
        v = wife_view(at(100, f), f)
        self.assertEqual(v["state"], "wife_window")
        self.assertEqual(v["period"], "wife_window")
        self.assertEqual(v["deadline"], "2026-05-01T12:00:00Z")
        self.assertIsNone(v["checked_in_at"])
        f.decision = Decision("F", day(120))
        v = wife_view(at(121, f), f)
        self.assertEqual(v["state"], "closed")
        self.assertIsNone(v["deadline"])
        f.wife_checkin = day(125)
        self.assertEqual(wife_view(at(126, f), f)["state"], "false_triggered")
        f.decision = Decision("W", day(120))
        self.assertEqual(wife_view(at(126, f), f)["state"], "inheritance")

    def test_wife_checked_in_at(self):
        f = armed_facts(wife_day=50)               # before the owner deadline: ignored, not shown
        f.alert_accepted[grace_alert_id(day(74))] = day(74)
        self.assertIsNone(wife_view(at(60, f), f)["checked_in_at"])
        self.assertIsNone(wife_view(at(80, f), f)["checked_in_at"])
        f.wife_checkin = day(80)                   # during grace: counts
        v = wife_view(at(81, f), f)
        self.assertEqual((v["state"], v["checked_in_at"]), ("grace", "2026-03-22T12:00:00Z"))
        f.decision = Decision("W", day(90))
        self.assertEqual(wife_view(at(91, f), f)["checked_in_at"], "2026-03-22T12:00:00Z")
        unarmed = Facts(wife_checkin=day(5))
        self.assertIsNone(wife_view(at(10, unarmed), unarmed)["checked_in_at"])

    def test_wife_checkin_before_owner_deadline_has_no_effect(self):
        early, none = armed_facts(wife_day=50), armed_facts()
        for n in (70, 75, 95, 121):
            a, b = at(n, early), at(n, none)
            # only the owner is told about her check-in; nothing else differs
            a_alerts = [x for x in a.alerts if x.kind != "checkin_wife"]
            self.assertEqual((a.phase, a.w_selected_at, a.decision_due, a.wife_window_end, a_alerts),
                             (b.phase, b.w_selected_at, b.decision_due, b.wife_window_end, b.alerts))
            self.assertEqual([x.params["effect"] for x in a.alerts if x.kind == "checkin_wife"],
                             ["before_deadline"])
            self.assertEqual(wife_view(a, early), wife_view(b, none))
        self.assertEqual(at(121, early).decision_due, "F")

    def test_family_states(self):
        f = armed_facts()
        self.assertEqual(family_view(at(10, f), f)["state"], "standby")
        self.assertEqual(family_view(at(70, f), f)["state"], "standby")
        self.assertEqual(family_view(at(75, f), f)["state"], "alert")
        self.assertEqual(family_view(at(100, f), f)["state"], "alert")
        f.decision = Decision("W", day(100))
        self.assertEqual(family_view(at(101, f), f)["state"], "inheritance")

    def test_owner_view_fields(self):
        f = armed_facts()
        # before the first wife grace alert is delivered: projected at the max wait
        v = owner_view(at(70, f), f)
        self.assertEqual(v["grace_clock_start"], "2026-03-17T12:00:00Z")  # day 75
        self.assertEqual(v["grace_end"], "2026-04-02T12:00:00Z")  # day 91
        f.alert_accepted[grace_alert_id(day(74))] = day(74)
        v = owner_view(at(80, f), f)
        self.assertEqual(v["grace_clock_start"], "2026-03-16T12:00:00Z")  # day 74
        self.assertEqual(v["grace_end"], "2026-04-01T12:00:00Z")  # day 90
        self.assertNotIn("wife_deadline", v)
        self.assertEqual(v["state"], "grace")
        self.assertEqual(v["owner_deadline"], "2026-03-16T12:00:00Z")
        self.assertEqual(v["period"], "owner_grace")
        self.assertIsNone(v["decision"])


class AlertTests(unittest.TestCase):
    def ids(self, s, skip=("armed", "weekly")):
        return sorted(a.id for a in s.alerts if a.kind not in skip)

    def test_normal_has_only_armed_and_weekly(self):
        self.assertEqual(self.ids(at(10, armed_facts()), skip=()),
                         ["armed:2026-01-01T12:00:00Z", "owner-weekly:2026-01-01T12:00:00Z:10"])

    def test_warning_alerts_daily_and_stable_within_day(self):
        f = armed_facts()
        a = self.ids(at(60.1, f))
        b = self.ids(at(60.9, f))
        c = self.ids(at(61.1, f))
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertEqual(a, ["owner-warning:2026-03-16T12:00:00Z:0"])

    def test_no_wife_alerts_before_owner_deadline(self):
        f = armed_facts(owner_day=30)  # owner deadline day 104
        for n in (60, 75, 90, 103.9):
            self.assertEqual([a for a in at(n, f).alerts if a.to == "wife" or "wife" in a.id], [])
        self.assertIn("wife", [a.to for a in at(104, f).alerts])

    def test_grace_alerts_both_daily(self):
        f = armed_facts()
        s = at(74, f)
        self.assertEqual(self.ids(s), ["grace-owner:2026-03-16T12:00:00Z:0", grace_alert_id(day(74))])
        s = at(100.5, f)
        self.assertEqual(self.ids(s), ["grace-owner:2026-03-16T12:00:00Z:26", "grace-wife:2026-03-16T12:00:00Z:26",
                                       "wife-window:2026-01-01T12:00:00Z"])

    def test_hold_and_unarmed_alerts(self):
        f = armed_facts().__class__(pauses=[Pause(None, day(0), day(1))])
        kinds = {a.kind for a in at(0.5, f).alerts}
        self.assertEqual(kinds, {"hold", "unarmed"})

    def test_decision_final_and_false_triggered(self):
        f = armed_facts()
        f.decision = Decision("F", day(120))
        f.wife_checkin = day(130)
        f.final_at = day(121)
        kinds = {a.kind for a in at(131, f).alerts}
        self.assertEqual(kinds, {"decision", "final", "false_triggered"})

    def test_quiet_after_decision(self):
        f = armed_facts()
        f.alert_accepted[grace_alert_id(day(74))] = day(74)
        f.decision = Decision("F", day(120))
        f.owner_checkin = day(122)                    # after the decision: changes nothing
        f.armed_at = day(0)
        faults = ["node_unreachable"]
        f.alert_accepted["fault:clock_skew:2026-05-01"] = day(120)  # would clear
        kinds = {a.kind for a in at(123, f, faults=faults).alerts}
        self.assertEqual(kinds, {"decision"})
        self.assertEqual(at(123, f, faults=faults).holds, ["node_unreachable"])  # still held, silently
        f.final_at = day(124)
        self.assertEqual({a.kind for a in at(125, f, faults=faults).alerts}, {"decision", "final"})

    def test_weekly_days_on_default_timeline(self):
        f = armed_facts()
        first = {}  # the alert exists for a week; it is created on the first day
        for n in range(0, 74):
            for a in at(n, f).alerts:
                if a.kind == "weekly":
                    first.setdefault(a.id, n)
        self.assertEqual(sorted(first.values()), [4, 11, 18, 25, 32, 39, 46, 53])
        self.assertEqual([a.params["days_left"] for a in at(4, f).alerts if a.kind == "weekly"], [70])
        self.assertEqual([a.kind for a in at(60, f).alerts if a.kind in ("weekly", "warning")], ["warning"])

    def test_no_weekly_right_after_checkin(self):
        f = armed_facts(owner_day=30.5)  # deadline 104.5; boundaries 34.5, 41.5, ...
        self.assertEqual([a for a in at(30.6, f).alerts if a.kind == "weekly"], [])
        self.assertEqual([a for a in at(34.4, f).alerts if a.kind == "weekly"], [])
        self.assertEqual([a.id for a in at(34.5, f).alerts if a.kind == "weekly"],
                         ["owner-weekly:2026-02-01T00:00:00Z:10"])

    def test_checkin_alerts(self):
        f = armed_facts(owner_day=0)
        self.assertEqual([a for a in at(1, f).alerts if a.kind == "checkin_owner"], [])  # arming is "armed"
        f.owner_checkin = day(20)
        [a] = [a for a in at(21, f).alerts if a.kind == "checkin_owner"]
        self.assertEqual((a.id, a.params["deadline"]), ("checkin-owner:2026-01-21T12:00:00Z", day(94)))
        f = armed_facts()
        f.alert_accepted[grace_alert_id(day(74))] = day(74)
        f.wife_checkin = day(80)
        [a] = [a for a in at(81, f).alerts if a.kind == "checkin_wife"]
        self.assertEqual((a.params["effect"], a.params["decision_at"]), ("selects_w", day(90)))
        f.decision = Decision("F", day(120))
        f.wife_checkin = day(125)
        self.assertEqual([a for a in at(126, f).alerts if a.kind == "checkin_wife"], [])  # changes nothing

    def test_no_grace_alerts_in_the_deciding_tick(self):
        f = armed_facts()
        f.alert_accepted[grace_alert_id(day(74))] = day(74)
        self.assertEqual([a for a in at(120, f).alerts if a.kind == "grace"], [])
        held = at(120, f, faults=["node_unreachable"])       # held: she can still check in, keep telling her
        self.assertEqual(sorted(a.to for a in held.alerts if a.kind == "grace"), ["owner", "wife"])

    def test_wife_window_alert_once_per_cycle(self):
        f = armed_facts()
        self.assertEqual([a for a in at(90, f).alerts if a.kind == "wife_window"], [])
        ids = {a.id for n in (91, 95, 120) for a in at(n, f).alerts if a.kind == "wife_window"}
        self.assertEqual(ids, {"wife-window:2026-01-01T12:00:00Z"})


class ClearedAlertTests(unittest.TestCase):
    def cleared(self, s):
        return [a.id for a in s.alerts if a.kind in ("hold_cleared", "fault_cleared")]

    def test_cold_start_hold_cleared(self):
        f = armed_facts()
        f.pauses.append(Pause(day(3), day(4), day(5)))
        self.assertEqual(self.cleared(at(4.5, f)), [])      # still holding
        self.assertEqual(self.cleared(at(5, f)), [])        # no hold alert was ever raised
        f.alert_accepted["hold:2026-01-06T12:00:00Z"] = None
        self.assertEqual(self.cleared(at(4.5, f)), [])
        self.assertEqual(self.cleared(at(5, f)), ["hold-cleared:2026-01-06T12:00:00Z"])
        f.pauses.append(Pause(day(6), day(7), day(8)))      # a newer hold replaces it
        self.assertEqual(self.cleared(at(7.5, f)), [])

    def test_fault_cleared_uses_newest_day_per_reason(self):
        f = armed_facts()
        for alert_id in ("fault:node_unreachable:2026-01-05", "fault:node_unreachable:2026-01-06",
                         "fault:tx_checksum:W2:2026-01-02"):
            f.alert_accepted[alert_id] = day(5)
        s = at(5.5, f, faults=["node_unreachable"])
        self.assertEqual(self.cleared(s), ["fault-cleared:tx_checksum:W2:2026-01-02"])
        self.assertEqual(self.cleared(at(6.5, f)), ["fault-cleared:node_unreachable:2026-01-06",
                                                    "fault-cleared:tx_checksum:W2:2026-01-02"])
        [a] = [a for a in at(6.5, f).alerts if a.id.startswith("fault-cleared:tx")]
        self.assertEqual(a.params, {"reason": "tx_checksum:W2"})

    def test_unarmed_alerts_cleared_too(self):
        f = Facts(pauses=[Pause(None, day(0), day(1))])
        f.alert_accepted["hold:2026-01-02T12:00:00Z"] = None
        f.alert_accepted["fault:clock_skew:2026-01-01"] = None
        self.assertEqual(self.cleared(at(1, f)), ["hold-cleared:2026-01-02T12:00:00Z",
                                                  "fault-cleared:clock_skew:2026-01-01"])


if __name__ == "__main__":
    unittest.main()
