"""事件日志：契约校验、乐观并发、线程竞态、JSONL 持久化与重放。"""

import json
import tempfile
import threading
import unittest
from pathlib import Path

from src import EventStore, SchedulingService, ConflictError, replay
from src.events import Event, ConcurrentAppendError
from helpers import build_world, screening_request, iso


class EventContractTest(unittest.TestCase):
    def test_sample_matches_domain_contract(self):
        record = json.loads(
            (Path(__file__).parents[1] / "data" / "sample.json").read_text(encoding="utf-8"))
        from src import validate_event
        self.assertEqual(validate_event(record), [])

    def test_validate_rejects_unknown_kind_and_missing_fields(self):
        from src import validate_event
        self.assertIn("kind", validate_event({"kind": "NOPE"}))
        problems = validate_event({"event_id": "x"})
        self.assertIn("kind", problems)
        self.assertIn("occurred_at", problems)


class OptimisticConcurrencyTest(unittest.TestCase):
    def test_compare_and_append(self):
        store = EventStore()
        ev = Event("e1", "SPACE_VERSIONED", "2026-09-01T09:00:00+08:00", "s1", {})
        store.append(ev, expected_version=0)
        with self.assertRaises(ConcurrentAppendError):
            store.append(Event("e2", "SPACE_VERSIONED", "2026-09-01T09:01:00+08:00", "s2", {}),
                         expected_version=0)
        store.append(Event("e2", "SPACE_VERSIONED", "2026-09-01T09:01:00+08:00", "s2", {}),
                     expected_version=1)
        self.assertEqual(store.version, 2)

    def test_duplicate_event_id_rejected(self):
        store = EventStore()
        store.append(Event("dup", "SPACE_VERSIONED", "2026-09-01T09:00:00+08:00", "s1", {}))
        with self.assertRaises(ValueError):
            store.append(Event("dup", "SPACE_VERSIONED", "2026-09-01T09:00:00+08:00", "s2", {}))

    def test_concurrent_submissions_only_one_locks_space(self):
        svc = build_world()
        n = 8
        barrier = threading.Barrier(n)
        results = {"ok": [], "conflict": [], "other": []}

        def worker(i):
            barrier.wait()
            try:
                svc.submit_request(screening_request(request_id=f"rq-race-{i}"))
                results["ok"].append(i)
            except ConflictError:
                results["conflict"].append(i)
            except Exception as exc:  # noqa: BLE001
                results["other"].append(repr(exc))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(results["other"], [])
        self.assertEqual(len(results["ok"]), 1, results)
        self.assertEqual(len(results["conflict"]), n - 1)
        held = [b for b in replay(svc.store.read_all()).bookings.values()
                if b.status == "HELD"]
        self.assertEqual(len(held), 1)


class PersistenceTest(unittest.TestCase):
    def test_jsonl_roundtrip_restores_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.jsonl"
            svc = SchedulingService(EventStore(path))
            build_world(svc=svc)
            svc.submit_request(screening_request())
            svc.sign_review("rq-screening", "heritage", "approved", signed_by="王苹")
            seq_before = svc.store.version

            # 重新打开：事件流与回放状态一致
            store2 = EventStore(path)
            self.assertEqual(store2.version, seq_before)
            state = replay(store2.read_all())
            self.assertEqual(sorted(state.space_ids()),
                             ["hall-01", "plaza-east", "yard-03"])
            self.assertIn("rq-screening", state.bookings)
            self.assertEqual(state.equipment["proj-4k-01"].name, "4K户外放映机组")
            # 每行都是合法 JSON，序号连续
            lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([row["seq"] for row in lines], list(range(len(lines))))


if __name__ == "__main__":
    unittest.main()
