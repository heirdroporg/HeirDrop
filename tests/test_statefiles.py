import json
import os
from datetime import timedelta
import tempfile
import unittest
from pathlib import Path

from heirdrop.logic import Broadcast
from heirdrop.statefiles import StateError, StateStore, create_exclusive, lock_exclusive, write_atomic
from tests.helpers import T0, day


class PrimitiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_write_atomic_replaces_and_leaves_no_temp(self):
        p = self.dir / "a.json"
        write_atomic(p, b"1")
        write_atomic(p, b"2")
        self.assertEqual(p.read_bytes(), b"2")
        self.assertEqual(os.listdir(self.dir), ["a.json"])

    def test_lock_exclusive_is_held_by_one(self):
        path = self.dir / "lock"
        fd = lock_exclusive(path)
        self.assertIsNotNone(fd)
        self.assertIsNone(lock_exclusive(path))   # flock is per open file, so this works in one process
        os.close(fd)
        fd = lock_exclusive(path)
        self.assertIsNotNone(fd)
        os.close(fd)

    def test_create_exclusive_once(self):
        p = self.dir / "m.json"
        self.assertTrue(create_exclusive(p, b"first"))
        self.assertFalse(create_exclusive(p, b"second"))
        self.assertEqual(p.read_bytes(), b"first")
        self.assertEqual(os.listdir(self.dir), ["m.json"])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.tmp.name) / "state")
        self.store.prepare()

    def tearDown(self):
        self.tmp.cleanup()

    def test_empty_store(self):
        f = self.store.read_facts()
        self.assertIsNone(f.armed_at)
        self.assertEqual(f.pauses, [])
        self.assertEqual(f.broadcasts, {})

    def test_owner_checkin_arms_once(self):
        self.store.write_checkin("owner", day(0))
        self.store.write_checkin("owner", day(5))
        f = self.store.read_facts()
        self.assertEqual(f.armed_at, day(0))
        self.assertEqual(f.owner_checkin, day(5))

    def test_round_trip(self):
        self.store.write_checkin("owner", day(0))
        self.store.write_checkin("wife", day(1))
        self.store.write_last_tick(day(2))
        self.store.record_pause(None, day(0), day(1))
        self.store.record_pause(day(1), day(1.5), None)
        self.assertTrue(self.store.record_decision("W", day(90)))
        self.store.record_broadcast(Broadcast("W1", "ab" * 32, day(90), True))
        self.store.record_broadcast(Broadcast("W2", "cd" * 32, day(90), True, "00" * 32, 900000))
        self.assertTrue(self.store.record_final(day(91)))
        self.store.create_alert("x:1", "owner", "test", "hello", day(0))
        f = self.store.read_facts()
        self.assertEqual((f.armed_at, f.owner_checkin, f.wife_checkin, f.last_tick), (day(0), day(0), day(1), day(2)))
        self.assertEqual(len(f.pauses), 2)
        self.assertIsNone(f.pauses[0].start)
        self.assertEqual(f.pauses[1].end, day(1.5))
        self.assertEqual(f.decision.choice, "W")
        self.assertEqual(f.broadcasts["W2"].blockhash, "00" * 32)
        self.assertEqual(f.broadcasts["W2"].block_height, 900000)
        self.assertEqual(f.final_at, day(91))
        self.assertEqual(f.alert_accepted, {"x:1": None})

    def test_decision_recorded_once(self):
        self.store.write_checkin("owner", day(0))
        self.assertTrue(self.store.record_decision("F", day(120)))
        self.assertFalse(self.store.record_decision("W", day(121)))
        self.assertEqual(self.store.read_facts().decision.choice, "F")

    def test_alert_delivery_round_trip(self):
        self.assertTrue(self.store.create_alert("a:1", "wife", "grace", "t", day(74)))
        self.assertFalse(self.store.create_alert("a:1", "wife", "grace", "t", day(75)))
        self.assertEqual(len(self.store.pending_alerts()), 1)
        a = self.store.read_alert("a:1")
        self.assertEqual(set(a), {"id", "to", "kind", "text", "created_at", "accepted_at", "attempts",
                                  "last_attempt_at", "last_error", "permanent", "receipt"})
        a["accepted_at"] = "2026-03-16T12:00:05Z"
        self.store.update_alert(a)
        self.assertEqual(self.store.pending_alerts(), [])
        self.assertEqual(self.store.read_facts().alert_accepted["a:1"], day(74) + timedelta(seconds=5))

    def test_alert_file_from_before_the_notifier(self):
        old = {"id": "a:1", "to": "wife", "kind": "grace", "text": "t", "created_at": "2026-03-16T12:00:00Z",
               "delivered_at": "2026-03-16T12:00:05Z", "attempts": 1, "last_error": None}
        self.store.update_alert(old)
        self.store.update_alert({**old, "id": "a:2", "delivered_at": None})
        self.assertEqual([a["id"] for a in self.store.pending_alerts()], ["a:2"])
        accepted = self.store.read_facts().alert_accepted
        self.assertEqual(accepted, {"a:1": day(74) + timedelta(seconds=5), "a:2": None})

    def test_events_log(self):
        self.store.log_event(day(0), "start", version="x")
        self.store.log_event(day(1), "tick")
        ev = self.store.read_events()
        self.assertEqual([e["kind"] for e in ev], ["start", "tick"])
        self.assertEqual(ev[0]["version"], "x")

    # ---- contradictions ------------------------------------------------
    def _write(self, name, obj):
        (self.store.dir / name).write_text(json.dumps(obj))

    def test_broadcast_without_decision(self):
        self.store.write_checkin("owner", day(0))
        self.store.record_broadcast(Broadcast("W1", "ab", day(1), True))
        with self.assertRaises(StateError):
            self.store.read_facts()

    def test_broadcast_from_wrong_pair(self):
        self.store.write_checkin("owner", day(0))
        self.store.record_decision("W", day(90))
        self.store.record_broadcast(Broadcast("F1", "ab", day(90), True))
        with self.assertRaises(StateError):
            self.store.read_facts()

    def test_final_without_full_pair(self):
        self.store.write_checkin("owner", day(0))
        self.store.record_decision("W", day(90))
        self.store.record_broadcast(Broadcast("W1", "ab", day(90), True))
        self.store.record_final(day(91))
        with self.assertRaises(StateError):
            self.store.read_facts()

    def test_decision_without_arming(self):
        self.store.record_decision("W", day(90))
        with self.assertRaises(StateError):
            self.store.read_facts()

    def test_corrupt_json(self):
        (self.store.dir / "checkin_owner.json").write_text("{not json")
        with self.assertRaises(StateError):
            self.store.read_facts()

    def test_wrong_types_are_state_errors(self):
        self.store.write_checkin("owner", day(0))
        cases = [("checkin_owner.json", '{"at": 5}'), ("checkin_owner.json", '{"at": null}'),
                 ("last_tick.json", '{"at": ["2026"]}'), ("decision.json", '{"choice": "F", "at": 1.5}'),
                 ("pauses.jsonl", "[]\n"), ("pauses.jsonl", "5\n"),
                 ("pauses.jsonl", '{"from": 1, "to": "2026-01-02T00:00:00Z"}\n'),
                 ("alerts/a.json", "[]"), ("alerts/a.json", '{"id": "a", "delivered_at": 5}')]
        for name, content in cases:
            with self.subTest(name=name, content=content):
                path = self.store.dir / name
                saved = path.read_text() if path.exists() else None
                path.write_text(content)
                with self.assertRaises(StateError):
                    self.store.read_facts()
                if saved is None:
                    path.unlink()
                else:
                    path.write_text(saved)
        self.store.read_facts()

    def test_bad_decision_choice(self):
        self.store.write_checkin("owner", day(0))
        self._write("decision.json", {"choice": "X", "at": "2026-01-01T00:00:00Z"})
        with self.assertRaises(StateError):
            self.store.read_facts()

    def test_checkin_without_armed(self):
        self._write("checkin_owner.json", {"at": "2026-01-01T00:00:00Z"})
        with self.assertRaises(StateError):
            self.store.read_facts()


if __name__ == "__main__":
    unittest.main()
