"""扰动应对：暴雨/修缮/居民紧急通行/已售取消 -> 建议、替代、人工决定门、恢复。"""

import unittest

from src import SchedulingError, HighRiskOverrideRequired
from helpers import build_world, screening_request, approve_all, iso


def confirm_screening(svc, request_id="rq-screening"):
    svc.submit_request(screening_request(request_id=request_id))
    approve_all(svc, request_id)
    svc.confirm(request_id)


class DisruptionTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_world()
        confirm_screening(self.svc)

    def _rainstorm(self):
        return self.svc.declare_disruption({
            "kind": "rainstorm", "severity": "high",
            "start": iso(2026, 10, 16, 17), "end": iso(2026, 10, 16, 23),
            "space_ids": ["yard-03", "plaza-east"],
            "description": "暴雨橙色预警，户外场地关停",
        })

    def test_rainstorm_suggests_relocate_with_indoor_alternative(self):
        result = self._rainstorm()
        self.assertEqual(len(result["impacts"]), 1)
        impact = result["impacts"][0]
        self.assertEqual(impact.request_id, "rq-screening")
        self.assertIn("relocate", impact.suggested_actions)
        self.assertIn("refund_if_not_relocated", impact.suggested_actions)
        alt_ids = {a.space_id for a in impact.alternatives}
        self.assertEqual(alt_ids, {"hall-01"})

    def test_recommendation_does_not_change_booking(self):
        self._rainstorm()
        b = self.svc._state().bookings["rq-screening"]
        self.assertIsNone(b.effect)
        self.assertEqual(b.status, "CONFIRMED")

    def test_high_risk_proceed_requires_explicit_acknowledgement(self):
        result = self._rainstorm()
        with self.assertRaises(HighRiskOverrideRequired):
            self.svc.manual_decision(
                "rq-screening", "proceed", decided_by="值班员",
                justification="观众已到场",
                recommendation_id=result["recommendation_id"])
        # 显式签收后允许记录（系统不批准，只留痕负责人的决定与风险）
        decision_id = self.svc.manual_decision(
            "rq-screening", "proceed", decided_by="值班负责人",
            justification="观众已到场，已加搭防雨棚并加密疏导",
            recommendation_id=result["recommendation_id"],
            risk_acknowledged=True)
        self.assertTrue(decision_id)
        b = self.svc._state().bookings["rq-screening"]
        self.assertEqual(b.decision_ids, [decision_id])

    def test_relocate_to_blocked_space_is_high_risk(self):
        result = self._rainstorm()
        # 想迁至同样被暴雨覆盖的东广场 -> 硬风险，未签收被拒
        with self.assertRaises(HighRiskOverrideRequired) as ctx:
            self.svc.manual_decision(
                "rq-screening", "relocate", decided_by="赵岚",
                justification="广场有棚", new_space_id="plaza-east",
                recommendation_id=result["recommendation_id"])
        self.assertTrue(any("RAINSTORM" in r or "扰动" in r for r in ctx.exception.reasons))

    def test_manual_relocate_to_hall_succeeds_and_is_visible(self):
        result = self._rainstorm()
        self.svc.manual_decision(
            "rq-screening", "relocate", decided_by="赵岚",
            justification="室内厅可容纳全部已售票观众",
            new_space_id="hall-01",
            recommendation_id=result["recommendation_id"])
        state = self.svc._state()
        b = state.bookings["rq-screening"]
        self.assertEqual(b.effect["relocated_to"], "hall-01")
        # 三号院此时段已释放：新活动可以占三号院（若没有暴雨覆盖的话）
        # 这里暴雨仍覆盖三号院，所以改验证设备争用随活动迁到 hall-01
        self.assertEqual(
            [x.request.request_id for x in state.bookings_using_space(
                "hall-01", *b.request.span)], ["rq-screening"])

    def test_restore_after_disruption(self):
        result = self._rainstorm()
        self.svc.manual_decision(
            "rq-screening", "relocate", decided_by="赵岚",
            justification="雨备", new_space_id="hall-01",
            recommendation_id=result["recommendation_id"])
        self.svc.restore_booking("rq-screening", note="预警解除，恢复原场地安排")
        b = self.svc._state().bookings["rq-screening"]
        self.assertIsNone(b.effect)
        self.assertIsNotNone(b.restored_at)

    def test_resident_urgent_access_suggests_capacity_reduction(self):
        result = self.svc.declare_disruption({
            "kind": "resident_access", "severity": "medium",
            "start": iso(2026, 10, 16, 18), "end": iso(2026, 10, 16, 20),
            "space_ids": ["yard-03"], "block_aisles": ["东门主通道"],
            "description": "居民突发就医需保持东门主通道畅通",
        })
        impact = result["impacts"][0]
        self.assertIn("reduce_capacity", impact.suggested_actions)
        # 负责人据建议缩容；已售 110 张而容量下调 -> 标记需部分退款
        self.svc.manual_decision(
            "rq-screening", "reduce_capacity", decided_by="赵岚",
            justification="让行紧急通道，单侧观演区关闭",
            capacity_limit=90, recommendation_id=result["recommendation_id"])
        events = [e for e in self.svc.store.read_all() if e.kind == "MANUAL_DECISION"]
        self.assertTrue(events[-1].payload.get("partial_refund_required"))

    def test_organizer_cancel_ticketed_triggers_refund(self):
        result = self.svc.declare_disruption({
            "kind": "organizer_cancel", "severity": "high",
            "start": iso(2026, 10, 16, 18), "end": iso(2026, 10, 16, 22),
            "space_ids": ["yard-03"],
            "description": "主办方因演员突发状况取消",
        })
        impact = result["impacts"][0]
        self.assertEqual(impact.suggested_actions, ("cancel", "refund"))
        self.svc.manual_decision(
            "rq-screening", "cancel", decided_by="赵岚",
            justification="主办方取消，启动退票",
            recommendation_id=result["recommendation_id"])
        b = self.svc._state().bookings["rq-screening"]
        self.assertEqual(b.status, "CANCELLED")
        events = [e for e in self.svc.store.read_all() if e.kind == "MANUAL_DECISION"]
        self.assertTrue(events[-1].payload.get("refund_required"))

    def test_repair_closure_lists_alternatives(self):
        result = self.svc.declare_disruption({
            "kind": "repair", "severity": "high",
            "start": iso(2026, 10, 16, 12), "end": iso(2026, 10, 17, 12),
            "space_ids": ["yard-03"],
            "description": "三号院地面突发沉降抢修",
        })
        impact = result["impacts"][0]
        self.assertIn("relocate", impact.suggested_actions)
        # 室内厅与不受本次修缮影响的东广场都属客观可行
        self.assertEqual({a.space_id for a in impact.alternatives},
                         {"hall-01", "plaza-east"})

    def test_decision_requires_decider_and_justification(self):
        self._rainstorm()
        with self.assertRaisesRegex(SchedulingError, "决定人与理由"):
            self.svc.manual_decision(
                "rq-screening", "reduce_capacity", decided_by="",
                justification="", capacity_limit=100)


if __name__ == "__main__":
    unittest.main()
