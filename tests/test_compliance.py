"""合规规则：容量、文保、消防通道、噪声/安静时段、无障碍、设备进出、争用。"""

import unittest

from src import SchedulingError, ConflictError
from src.compliance import evaluate_request, evaluate_conflicts
from helpers import build_world, screening_request, iso


class ComplianceTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_world()

    @property
    def state(self):
        return self.svc._state()

    def _codes(self, spec):
        from src.model import BookingRequest
        req = BookingRequest(
            request_id=spec.get("request_id", "probe"),
            organization=spec["organization"], title=spec["title"],
            space_id=spec["space_id"], setup_start=spec["setup_start"],
            event_start=spec["event_start"], event_end=spec["event_end"],
            teardown_end=spec["teardown_end"],
            expected_attendance=spec["expected_attendance"],
            equipment_ids=tuple(spec.get("equipment_ids", [])),
            amplification=spec.get("amplification", False),
            ticketed=spec.get("ticketed", False),
            accessibility_required=spec.get("accessibility_required", False),
            activity_features=tuple(spec.get("activity_features", [])),
            setup_encroaches_aisles=tuple(spec.get("setup_encroaches_aisles", [])),
        )
        return {f.code for f in evaluate_request(self.state, req)}, req

    def test_clean_request_has_no_errors(self):
        codes, _ = self._codes(screening_request())
        self.assertNotIn("CAPACITY_EXCEEDED", codes)
        self.assertNotIn("NOISE_OUTSIDE_WINDOW", codes)
        self.assertNotIn("QUIET_PERIOD_OVERLAP", codes)

    def test_capacity(self):
        codes, _ = self._codes(screening_request(expected_attendance=121))
        self.assertIn("CAPACITY_EXCEEDED", codes)

    def test_amplification_outside_noise_window(self):
        # 周日（weekday=6）三号院没有噪声窗口
        codes, _ = self._codes(screening_request(
            setup_start=iso(2026, 10, 18, 13),
            event_start=iso(2026, 10, 18, 18, 30),
            event_end=iso(2026, 10, 18, 20, 45),
            teardown_end=iso(2026, 10, 18, 22, 0)))
        self.assertIn("NOISE_OUTSIDE_WINDOW", codes)

    def test_event_running_into_quiet_period_fails_window_cover(self):
        # 演出到 21:20，超出 21:00 噪声窗口（安静时段 21:30 起，但窗口先失败）
        codes, _ = self._codes(screening_request(
            event_end=iso(2026, 10, 16, 21, 20)))
        self.assertIn("NOISE_OUTSIDE_WINDOW", codes)

    def test_loadin_window_enforced(self):
        codes, _ = self._codes(screening_request(
            setup_start=iso(2026, 10, 16, 7)))  # 早于 09:00 进出窗口
        self.assertIn("LOADIN_WINDOW", codes)

    def test_open_flame_forbidden_at_protected_site(self):
        codes, _ = self._codes(screening_request(
            activity_features=["明火"], amplification=False))
        self.assertIn("HERITAGE_FEATURE_FORBIDDEN", codes)

    def test_setup_must_not_block_fire_aisle(self):
        codes, _ = self._codes(screening_request(
            setup_encroaches_aisles=["东门主通道"]))
        self.assertIn("FIRE_AISLE_BLOCKED", codes)

    def test_accessibility_requirement(self):
        # 发布一块无无障碍路线的空间
        self.svc.define_space({
            "space_id": "lane-09", "name": "九曲巷", "zone": "街巷集合点",
            "protection_level": "历史建筑", "capacity": 60,
            "accessible_route": False, "accessible_note": "台阶密集",
            "fire_aisles": ["南口"],
        })
        codes, _ = self._codes(screening_request(
            space_id="lane-09", accessibility_required=True))
        self.assertIn("ACCESSIBLE_ROUTE_MISSING", codes)

    def test_equipment_space_mismatch(self):
        self.svc.register_equipment({"equipment_id": "rig-yard-only",
                                     "name": "院落专用灯架", "shared": True,
                                     "requires_load_in_window": False,
                                     "usable_spaces": ["yard-03"]})
        codes, _ = self._codes(screening_request(
            space_id="plaza-east", equipment_ids=["rig-yard-only"],
            setup_start=iso(2026, 10, 16, 12),
            event_start=iso(2026, 10, 16, 18),
            event_end=iso(2026, 10, 16, 21),
            teardown_end=iso(2026, 10, 16, 21, 30)))
        self.assertIn("EQUIPMENT_SPACE_MISMATCH", codes)

        codes2, _ = self._codes(screening_request(equipment_ids=["no-such-device"]))
        self.assertIn("EQUIPMENT_UNKNOWN", codes2)

    def test_submit_rejects_hard_violations(self):
        with self.assertRaises(SchedulingError):
            self.svc.submit_request(screening_request(expected_attendance=500))


class ConflictTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_world()

    def test_same_space_and_shared_equipment_cannot_double_lock(self):
        self.svc.submit_request(screening_request())
        with self.assertRaises(ConflictError) as ctx:
            self.svc.submit_request(screening_request(
                request_id="rq-rival", title="公益音乐会",
                setup_start=iso(2026, 10, 16, 16),
                event_start=iso(2026, 10, 16, 19),
                event_end=iso(2026, 10, 16, 20, 30),
                teardown_end=iso(2026, 10, 16, 21, 30),
                expected_attendance=80))
        codes = {f.code for f in ctx.exception.findings}
        self.assertEqual(codes, {"SPACE_LOCKED", "EQUIPMENT_LOCKED"})

    def test_non_overlapping_time_is_fine(self):
        self.svc.submit_request(screening_request())
        hold = self.svc.submit_request(screening_request(
            request_id="rq-daytime", title="城市漫游集合",
            setup_start=iso(2026, 10, 16, 9),
            event_start=iso(2026, 10, 16, 9, 30),
            event_end=iso(2026, 10, 16, 11, 30),
            teardown_end=iso(2026, 10, 16, 12),
            expected_attendance=40, amplification=False,
            equipment_ids=[], accessibility_required=False))
        self.assertEqual(hold.request_id, "rq-daytime")

    def test_shared_equipment_conflict_even_at_different_spaces(self):
        self.svc.submit_request(screening_request())  # yard-03 用机组
        with self.assertRaises(ConflictError) as ctx:
            self.svc.submit_request(screening_request(
                request_id="rq-plaza", space_id="plaza-east",
                title="广场带妆彩排", setup_start=iso(2026, 10, 16, 12),
                event_start=iso(2026, 10, 16, 14),
                event_end=iso(2026, 10, 16, 16),
                teardown_end=iso(2026, 10, 16, 17, 30),
                expected_attendance=200, amplification=False))
        codes = {f.code for f in ctx.exception.findings}
        self.assertIn("EQUIPMENT_LOCKED", codes)
        self.assertNotIn("SPACE_LOCKED", codes)

    def test_adjacent_intervals_touch_but_do_not_overlap(self):
        # 第二场恰在第一场撤场完成的 22:30 开始（设备时间首尾相接，不重叠）
        self.svc.submit_request(screening_request())
        hold = self.svc.submit_request(screening_request(
            request_id="rq-after", title="室内厅紧接放映", space_id="hall-01",
            setup_start=iso(2026, 10, 16, 22, 30),
            event_start=iso(2026, 10, 16, 22, 35),
            event_end=iso(2026, 10, 16, 22, 50),
            teardown_end=iso(2026, 10, 16, 23, 0),
            expected_attendance=30, amplification=False))
        self.assertEqual(hold.request_id, "rq-after")


if __name__ == "__main__":
    unittest.main()
