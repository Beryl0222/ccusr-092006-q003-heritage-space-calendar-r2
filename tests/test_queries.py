"""读模型：日可用性逐时段依据，与投诉反查时间线。"""

import unittest

from src import ReadModel
from helpers import build_world, screening_request, approve_all, iso


def _segments_by_state(day_rows, space_id):
    row = next(r for r in day_rows if r["space_id"] == space_id)
    out = {}
    for seg in row["segments"]:
        out.setdefault(seg["state"], []).append(
            (seg["start"][11:16], seg["end"][11:16]))
    return row, out


class DayAvailabilityTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_world()
        self.read = ReadModel(self.svc.store)

    def test_empty_day_explains_availability(self):
        rows = self.read.day_availability("2026-10-17")
        yard = next(r for r in rows if r["space_id"] == "yard-03")
        self.assertTrue(yard["published"])
        self.assertTrue(all(s["state"] == "AVAILABLE" for s in yard["segments"]))
        # 周历约束仍然展示（解释这块空间"在什么条件下可用"）
        self.assertIn("quiet_periods", yard["constraints"])

    def test_confirmed_booking_blocks_its_window(self):
        self.svc.submit_request(screening_request())
        approve_all(self.svc, "rq-screening")
        self.svc.confirm("rq-screening")
        rows = self.read.day_availability("2026-10-16")
        _, by_state = _segments_by_state(rows, "yard-03")
        occupied = by_state.get("OCCUPIED", [])
        # 13:00 进场 ~ 22:30 撤场 为确定档期
        self.assertEqual(occupied, [("13:00", "22:30")])
        row, _ = _segments_by_state(rows, "yard-03")
        occ_seg = next(s for s in row["segments"] if s["state"] == "OCCUPIED")
        self.assertEqual(occ_seg["reasons"][0]["request_id"], "rq-screening")

    def test_disruption_renders_blocked_with_basis(self):
        self.svc.declare_disruption({
            "kind": "repair", "severity": "high",
            "start": iso(2026, 10, 17, 8), "end": iso(2026, 10, 17, 18),
            "space_ids": ["yard-03"], "description": "沉降抢修",
        })
        rows = self.read.day_availability("2026-10-17")
        _, by_state = _segments_by_state(rows, "yard-03")
        self.assertEqual(by_state["BLOCKED"], [("08:00", "18:00")])
        row, _ = _segments_by_state(rows, "yard-03")
        blocked = next(s for s in row["segments"] if s["state"] == "BLOCKED")
        self.assertIn("沉降抢修", blocked["reasons"][0]["text"])
        self.assertTrue(blocked["reasons"][0]["basis"].startswith("扰动事件"))

    def test_relocation_shown_on_both_spaces(self):
        self.svc.submit_request(screening_request())
        approve_all(self.svc, "rq-screening")
        self.svc.confirm("rq-screening")
        result = self.svc.declare_disruption({
            "kind": "rainstorm", "severity": "high",
            "start": iso(2026, 10, 16, 17), "end": iso(2026, 10, 16, 23),
            "space_ids": ["yard-03", "plaza-east"],
            "description": "暴雨预警",
        })
        self.svc.manual_decision(
            "rq-screening", "relocate", decided_by="赵岚",
            justification="雨备室内", new_space_id="hall-01",
            recommendation_id=result["recommendation_id"])

        rows = self.read.day_availability("2026-10-16")
        # 目标空间显示迁入
        hall_row, hall_states = _segments_by_state(rows, "hall-01")
        occ = next(s for s in hall_row["segments"] if s["state"] == "OCCUPIED")
        self.assertEqual(occ["reasons"][0]["relocated_from"], "yard-03")
        # 原空间的活动时段标注"已迁出、时段释放"（17 点后与暴雨 BLOCKED 段叠加）
        yard_row, _ = _segments_by_state(rows, "yard-03")
        away = [r for s in yard_row["segments"] for r in s["reasons"]
                if r["type"] == "relocated_away"]
        self.assertTrue(away)
        self.assertIn("迁至室内展演厅", away[0]["text"])

    def test_unpublished_space_is_reported_not_crashed(self):
        rows = self.read.day_availability("2026-10-16", space_ids=["ghost"])
        self.assertFalse(rows[0]["published"])


class ComplaintTraceTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_world()
        self.read = ReadModel(self.svc.store)
        self.svc.submit_request(screening_request())
        self.svc.sign_review("rq-screening", "heritage", "conditional",
                             conditions=["拉索不得接触木构"], basis="文保巡视记录",
                             signed_by="王苹")
        self.svc.sign_review("rq-screening", "fire", "approved",
                             basis="通道净宽达标", signed_by="李澈")
        self.svc.sign_review("rq-screening", "jurisdiction", "conditional",
                             conditions=["21:30 前静场"], basis="居民公约",
                             signed_by="赵岚")
        self.svc.confirm("rq-screening")

    def test_trace_covers_approval_adjustment_and_restoration(self):
        result = self.svc.declare_disruption({
            "kind": "rainstorm", "severity": "high",
            "start": iso(2026, 10, 16, 17), "end": iso(2026, 10, 16, 23),
            "space_ids": ["yard-03", "plaza-east"], "description": "暴雨预警",
        })
        self.svc.manual_decision(
            "rq-screening", "relocate", decided_by="赵岚",
            justification="改室内", new_space_id="hall-01",
            recommendation_id=result["recommendation_id"])
        cid = self.svc.record_complaint("rq-screening", "居民反映散场噪音")
        self.svc.resolve_complaint(cid, conclusion="动线调整所致",
                                   followups=["更新引导标识"], resolved_by="赵岚")
        self.svc.restore_booking("rq-screening", note="预警解除")

        trace = self.read.complaint_trace(cid)
        # 批准依据：三方意见与空间版本
        self.assertEqual(set(trace["approval_basis"]["reviews"]),
                         {"heritage", "fire", "jurisdiction"})
        self.assertEqual(trace["approval_basis"]["space_id"], "yard-03")
        # 系统建议
        self.assertTrue(trace["system_recommendations"])
        self.assertIn("relocate",
                      trace["system_recommendations"][0]["suggested_actions"])
        # 现场调整
        self.assertEqual(trace["on_site_adjustments"][0]["action"], "relocate")
        self.assertEqual(trace["on_site_adjustments"][0]["new_space_id"], "hall-01")
        # 后续恢复
        self.assertIsNotNone(trace["restoration"])
        # 投诉结论
        self.assertEqual(trace["complaint"]["resolution"]["conclusion"], "动线调整所致")
        # 时间线包含全部关键事件且按序
        kinds = [e["kind"] for e in trace["event_timeline"]]
        self.assertLess(kinds.index("BOOKING_CONFIRMED"),
                        kinds.index("RECOMMENDATION_LOGGED"))
        self.assertLess(kinds.index("MANUAL_DECISION"),
                        kinds.index("BOOKING_RESTORED"))

    def test_trace_missing_complaint_raises(self):
        with self.assertRaises(KeyError):
            self.read.complaint_trace("nope")


if __name__ == "__main__":
    unittest.main()
