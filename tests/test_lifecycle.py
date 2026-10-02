"""生命周期：空间版本、暂占、三方意见门、确认、释放。"""

import unittest
from datetime import datetime

from src import SchedulingError
from helpers import build_world, screening_request, approve_all, iso, dt


class LifecycleTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_world()

    def test_hold_then_three_party_confirm(self):
        r = self.svc.submit_request(screening_request())
        self.assertEqual(r.request_id, "rq-screening")
        state = self.svc._state()
        self.assertEqual(state.bookings["rq-screening"].status, "HELD")

        # 缺任何一方意见都不能确认
        self.svc.sign_review("rq-screening", "heritage", "approved", signed_by="文保员")
        with self.assertRaisesRegex(SchedulingError, "消防部门、属地管理方"):
            self.svc.confirm("rq-screening")

        self.svc.sign_review("rq-screening", "fire", "approved", signed_by="消防员")
        self.svc.sign_review("rq-screening", "jurisdiction", "approved", signed_by="街区办")
        result = self.svc.confirm("rq-screening")
        self.assertEqual(result.basis_snapshot["space_id"], "yard-03")
        self.assertEqual(set(result.basis_snapshot["reviews"]),
                         {"heritage", "fire", "jurisdiction"})
        self.assertEqual(self.svc._state().bookings["rq-screening"].status, "CONFIRMED")

    def test_rejected_review_blocks_confirmation(self):
        self.svc.submit_request(screening_request())
        self.svc.sign_review("rq-screening", "heritage", "rejected",
                             basis="木构风险", signed_by="文保员")
        self.svc.sign_review("rq-screening", "fire", "approved", signed_by="消防员")
        self.svc.sign_review("rq-screening", "jurisdiction", "approved", signed_by="街区办")
        with self.assertRaisesRegex(SchedulingError, "文保部门"):
            self.svc.confirm("rq-screening")

    def test_conditional_review_requires_conditions(self):
        self.svc.submit_request(screening_request())
        with self.assertRaisesRegex(SchedulingError, "列明条件"):
            self.svc.sign_review("rq-screening", "jurisdiction", "conditional")

    def test_release_frees_space_and_equipment(self):
        self.svc.submit_request(screening_request())
        self.svc.release_hold("rq-screening", "机构改期")
        # 释放后同场地同设备可以被新申请锁定
        other = screening_request(request_id="rq-other", title="非遗市集")
        hold = self.svc.submit_request(other)
        self.assertEqual(hold.request_id, "rq-other")

    def test_expired_hold_is_swept_and_cannot_confirm(self):
        clock_val = [dt(2026, 10, 14, 9)]

        def clock():
            return clock_val[0]

        svc = build_world(clock=clock)
        svc.submit_request(screening_request(), hold_hours=48)
        clock_val[0] = dt(2026, 10, 16, 10)
        approve_all(svc, "rq-screening")
        with self.assertRaisesRegex(SchedulingError, "过期"):
            svc.confirm("rq-screening")
        # 过期暂占已被清扫，新申请可进入
        svc.submit_request(screening_request(request_id="rq-new"))
        self.assertEqual(svc._state().bookings["rq-new"].status, "HELD")

    def test_space_versioning_supersedes_previous(self):
        self.svc.define_space({
            "space_id": "yard-03", "name": "三号院（容量调整）", "zone": "院落",
            "protection_level": "市级文物保护单位", "capacity": 80,
            "accessible_route": True, "fire_aisles": ["东门主通道"],
        })
        state = self.svc._state()
        versions = state.spaces["yard-03"]
        self.assertEqual([v.version for v in versions], [1, 2])
        self.assertEqual(versions[0].status, "superseded")
        self.assertEqual(state.current_space("yard-03").capacity, 80)

    def test_new_version_with_stricter_rule_rechecks_at_confirmation(self):
        # 暂占之后空间发布新版本：施工覆盖活动日，确认时必须按新版本复核
        self.svc.submit_request(screening_request())
        self.svc.define_space({
            "space_id": "yard-03", "name": "三号院", "zone": "院落",
            "protection_level": "市级文物保护单位", "capacity": 120,
            "accessible_route": True, "fire_aisles": ["东门主通道", "北侧备弄"],
            "noise_windows": [{"days": [4, 5], "start": "18:30", "end": "21:00"}],
            "quiet_periods": [{"days": list(range(7)), "start": "21:30", "end": "08:00"}],
            "load_in_windows": [{"days": [4, 5], "start": "09:00", "end": "22:30"}],
            "construction": [{"name": "突发抢险修缮",
                              "start": iso(2026, 10, 16, 0),
                              "end": iso(2026, 10, 17, 0)}],
        })
        approve_all(self.svc, "rq-screening")
        with self.assertRaisesRegex(SchedulingError, "抢险修缮"):
            self.svc.confirm("rq-screening")


if __name__ == "__main__":
    unittest.main()
