"""排期服务业务规则测试。"""

import json
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path

from src import (
    Complaint,
    ConcurrencyError,
    Decision,
    DecisionRequiredError,
    Disruption,
    DisruptionType,
    EventKind,
    OpinionKind,
    SchedulingError,
    explain_day,
    trace_complaint,
)

from fixtures import (
    TZ,
    all_opinions,
    dt,
    hold_and_confirm,
    new_service,
    register_courtyard,
    register_equipment,
    make_request,
    make_space,
)


class VersionAndFeasibilityTest(unittest.TestCase):
    def setUp(self):
        self.store, self.service = new_service()

    def test_space_must_be_registered_before_hold(self):
        _, service = new_service()
        with self.assertRaises(SchedulingError) as ctx:
            service.place_hold(make_request("r1"), "sp-unknown")
        self.assertIn("UNKNOWN_SPACE", str(ctx.exception))

    def test_capacity_quiet_and_blocked_windows_are_enforced(self):
        service = self.service
        register_courtyard(service)
        register_equipment(service)

        too_big = make_request("r-big", attendance=240)
        with self.assertRaises(SchedulingError) as ctx:
            service.place_hold(too_big, "sp-courtyard-3")
        self.assertIn("CAPACITY", str(ctx.exception))

        # 周五 20:00 之后是居民安静时段。
        quiet = make_request("r-quiet", start="2026-10-09T19:30:00+08:00", minutes=90)
        with self.assertRaises(SchedulingError) as ctx:
            service.place_hold(quiet, "sp-courtyard-3")
        self.assertIn("QUIET_HOURS", str(ctx.exception))

    def test_construction_blocked_window_rejects_hold(self):
        from src.models import Window

        service = self.service
        register_courtyard(service, blocked=(
            Window(dt("2026-10-10T08:00:00+08:00"), dt("2026-10-10T18:00:00+08:00")),
        ))
        register_equipment(service)
        with self.assertRaises(SchedulingError) as ctx:
            service.place_hold(make_request("r-block"), "sp-courtyard-3")
        self.assertIn("BLOCKED_WINDOW", str(ctx.exception))

    def test_version_chain_must_be_sequential(self):
        service = self.service
        register_courtyard(service)
        bad = make_space(version=3, valid_from=dt("2026-09-01T00:00:00+08:00"))
        with self.assertRaises(SchedulingError):
            service.publish_space_version("sp-courtyard-3", bad, dt("2026-09-01T00:00:00+08:00"))

    def test_new_version_rechecks_unconfirmed_holds(self):
        """暂占后空间收紧容量，转确定必须按新版本复核。"""
        service = self.service
        register_courtyard(service, capacity=200)
        register_equipment(service)
        hold = service.place_hold(make_request("r1", attendance=180), "sp-courtyard-3")
        booking_id = hold.payload["booking_id"]
        for op in all_opinions().values():
            service.sign_review(booking_id, op)

        # 新版本：院落容量因文保勘查降为 120。
        service.publish_space_version(
            "sp-courtyard-3",
            make_space(version=2, capacity=120, valid_from=dt("2026-10-01T00:00:00+08:00")),
            dt("2026-10-01T00:00:00+08:00"),
        )
        with self.assertRaises(SchedulingError) as ctx:
            service.confirm_booking(booking_id, dt("2026-10-02T09:00:00+08:00"))
        self.assertIn("CAPACITY", str(ctx.exception))


class OpinionGateTest(unittest.TestCase):
    def setUp(self):
        self.store, self.service = new_service()
        register_courtyard(self.service)
        register_equipment(self.service)

    def _hold(self):
        ev = self.service.place_hold(make_request("r1"), "sp-courtyard-3")
        return ev.payload["booking_id"]

    def test_hold_cannot_confirm_without_all_three_opinions(self):
        booking_id = self._hold()
        self.service.sign_review(booking_id, all_opinions()[OpinionKind.HERITAGE])
        self.service.sign_review(booking_id, all_opinions()[OpinionKind.FIRE])
        with self.assertRaises(SchedulingError) as ctx:
            self.service.confirm_booking(booking_id, dt("2026-10-02T09:00:00+08:00"))
        self.assertIn("LOCAL", str(ctx.exception))

    def test_rejected_opinion_blocks_confirmation(self):
        from fixtures import opinion

        booking_id = self._hold()
        ops = all_opinions()
        ops[OpinionKind.FIRE] = opinion(OpinionKind.FIRE, approved=False,
                                        reviewer="消防大队李参谋",
                                        conclusion="消防通道被搭台占压，不同意")
        for op in ops.values():
            self.service.sign_review(booking_id, op)
        with self.assertRaises(SchedulingError) as ctx:
            self.service.confirm_booking(booking_id, dt("2026-10-02T09:00:00+08:00"))
        self.assertIn("FIRE", str(ctx.exception))

    def test_full_pipeline_confirms_and_records_basis(self):
        booking_id = hold_and_confirm(self.service, make_request("r1"), "sp-courtyard-3")
        state = self.store.state()
        view = state.booking(booking_id)
        self.assertEqual(view.status, "CONFIRMED")
        self.assertEqual(set(view.confirmation_basis["opinions"]),
                         {"HERITAGE", "FIRE", "LOCAL"})
        self.assertEqual(view.confirmation_basis["space"]["version"], 1)


class ConcurrencyLockTest(unittest.TestCase):
    def test_concurrent_holds_on_same_space_one_wins(self):
        store, service = new_service()
        register_courtyard(service)
        register_equipment(service)
        results: list[Exception | str] = []
        barrier = threading.Barrier(4)

        def worker(i):
            barrier.wait()
            req = make_request(
                f"r{i}", organizer=f"机构{i}", title=f"活动{i}",
                start="2026-10-10T14:00:00+08:00", minutes=120,
            )
            try:
                ev = service.place_hold(req, "sp-courtyard-3")
                results.append(ev.payload["booking_id"])
            except SchedulingError as exc:
                results.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        successes = [r for r in results if isinstance(r, str)]
        failures = [r for r in results if isinstance(r, Exception)]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 3)
        self.assertTrue(all("SPACE_OCCUPIED" in str(f) or "EQUIPMENT_OCCUPIED" in str(f)
                            for f in failures))

    def test_shared_equipment_cannot_be_double_booked_across_spaces(self):
        _, service = new_service()
        register_courtyard(service, space_id="sp-a", name="东院")
        register_courtyard(service, space_id="sp-b", name="西院")
        register_equipment(service)
        service.place_hold(make_request("r1", title="非遗体验A"), "sp-a")
        with self.assertRaises(SchedulingError) as ctx:
            service.place_hold(make_request("r2", title="城市漫游集合B"), "sp-b")
        self.assertIn("EQUIPMENT_OCCUPIED", str(ctx.exception))

    def test_stale_revision_is_rejected(self):
        _, service = new_service()
        register_courtyard(service)
        register_equipment(service)
        revision_before = service.store.revision()
        # 其它事务先写入。
        service.place_hold(make_request("r-earlier", title="更早的暂占",
                                        start="2026-10-11T14:00:00+08:00"),
                           "sp-courtyard-3")
        with self.assertRaises(ConcurrencyError):
            service.place_hold(
                make_request("r-late"), "sp-courtyard-3",
                expected_revision=revision_before,
            )


class DisruptionPlanTest(unittest.TestCase):
    def setUp(self):
        self.store, self.service = new_service()
        register_courtyard(self.service, space_id="sp-a", name="东院", capacity=200)
        register_courtyard(self.service, space_id="sp-b", name="西院", capacity=260)
        register_equipment(self.service)

    def test_storm_proposes_routine_relocation(self):
        booking_id = hold_and_confirm(
            self.service, make_request("r1", title="户外放映"), "sp-a"
        )
        storm = Disruption(
            disruption_id="dp-storm",
            kind=DisruptionType.STORM,
            window=make_request("r1").window,
            space_id="sp-a",
            booking_id=None,
            summary="暴雨橙色预警，东院露天区域停用",
            logged_at=dt("2026-10-10T10:00:00+08:00"),
        )
        self.service.log_disruption(storm)

        state = self.store.state()
        plans = [state.adjustments[i] for i in state.booking(booking_id).adjustment_ids]
        routine = [p for p in plans if p.risk.value == "ROUTINE" and p.kind.value == "RELOCATE"]
        self.assertEqual(len(routine), 1)
        self.assertEqual(routine[0].target_space_id, "sp-b")
        affected_types = {a.party_type for a in routine[0].affected}
        self.assertIn("ORGANIZER", affected_types)
        # 建议默认待决定，未执行前原场地仍占用。
        self.assertEqual(state.booking(booking_id).active_assignment.space_id, "sp-a")

    def test_resident_emergency_proposes_downsize(self):
        booking_id = hold_and_confirm(
            self.service, make_request("r1", title="城市漫游集合"), "sp-a"
        )
        d = Disruption(
            disruption_id="dp-em",
            kind=DisruptionType.RESIDENT_EMERGENCY,
            window=make_request("r1").window,
            space_id="sp-a",
            booking_id=None,
            summary="居民突发就医需保留通道",
            logged_at=dt("2026-10-10T13:00:00+08:00"),
        )
        self.service.log_disruption(d)
        state = self.store.state()
        kinds = sorted(
            (state.adjustments[i].kind.value, state.adjustments[i].risk.value)
            for i in state.booking(booking_id).adjustment_ids
        )
        self.assertIn(("DOWNSIZE", "ROUTINE"), kinds)

    def test_high_risk_exception_requires_responsible_person(self):
        # 只有一块空间且容量不足——找不到合规替代，只能产生高风险备选/退款。
        store2, service2 = new_service()
        register_courtyard(service2, space_id="sp-a", name="东院", capacity=200)
        register_equipment(service2)
        booking_id = hold_and_confirm(
            service2, make_request("r1", title="已售票音乐会", attendance=180, ticketed=True),
            "sp-a",
        )
        repair = Disruption(
            disruption_id="dp-repair",
            kind=DisruptionType.REPAIR,
            window=make_request("r1").window,
            space_id="sp-a",
            booking_id=None,
            summary="文保单位紧急修缮，东院封闭",
            logged_at=dt("2026-10-10T09:00:00+08:00"),
        )
        service2.log_disruption(repair)
        state = store2.state()
        plans = [state.adjustments[i] for i in state.booking(booking_id).adjustment_ids]
        # 无替代场地 -> 至少给出退款建议，且没有自动批准的方案。
        self.assertTrue(any(p.kind.value == "REFUND" for p in plans))
        self.assertTrue(all(p.decision is Decision.PENDING for p in plans))

        # 若存在高风险迁场备选，普通工作人员不能批准。
        high = [p for p in plans if p.risk.value == "HIGH"]
        for p in high:
            with self.assertRaises(DecisionRequiredError):
                service2.decide_adjustment(
                    p.adjustment_id, Decision.APPROVED, "值班员小赵",
                    dt("2026-10-10T09:30:00+08:00"),
                )
            # 负责人显式批准才放行。
            service2.decide_adjustment(
                p.adjustment_id, Decision.APPROVED, "街区负责人钱主任",
                dt("2026-10-10T09:40:00+08:00"),
                note="已会签文保/消防，限时限容执行",
                responsible_person=True,
            )

    def test_ticketed_cancellation_emits_refund_plan(self):
        booking_id = hold_and_confirm(
            self.service, make_request("r1", ticketed=True, attendance=100), "sp-a"
        )
        self.service.cancel_booking(
            booking_id, dt("2026-10-09T12:00:00+08:00"), "机构演员档期变动"
        )
        state = self.store.state()
        self.assertEqual(state.booking(booking_id).status, "CANCELLED")
        plans = [state.adjustments[i] for i in state.booking(booking_id).adjustment_ids]
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].kind.value, "REFUND")
        parties = {a.party_type for a in plans[0].affected}
        self.assertIn("TICKET_HOLDERS", parties)

    def test_approved_relocation_applies_and_recovery_plan_is_generated(self):
        booking_id = hold_and_confirm(
            self.service, make_request("r1", title="户外放映"), "sp-a"
        )
        storm = Disruption(
            disruption_id="dp-storm", kind=DisruptionType.STORM,
            window=make_request("r1").window, space_id="sp-a",
            summary="暴雨预警", logged_at=dt("2026-10-10T10:00:00+08:00"),
        )
        self.service.log_disruption(storm)
        state = self.store.state()
        plan = next(state.adjustments[i] for i in state.booking(booking_id).adjustment_ids
                    if state.adjustments[i].kind.value == "RELOCATE")
        self.service.decide_adjustment(
            plan.adjustment_id, Decision.APPROVED, "运营办孙主任",
            dt("2026-10-10T10:15:00+08:00"), responsible_person=True,
        )
        self.service.apply_adjustment(plan.adjustment_id, dt("2026-10-10T10:20:00+08:00"))
        state = self.store.state()
        self.assertEqual(state.booking(booking_id).active_assignment.space_id, "sp-b")

        # 雨停恢复 -> 生成回迁建议。
        self.service.resolve_disruption(
            "dp-storm", dt("2026-10-10T12:30:00+08:00"), "预警解除，东院场地复检无积水"
        )
        state = self.store.state()
        returns = [
            state.adjustments[i] for i in state.booking(booking_id).adjustment_ids
            if state.adjustments[i].target_space_id == "sp-a"
        ]
        self.assertTrue(returns)
        self.assertEqual(returns[-1].decision, Decision.PENDING)


class ExplainDayTest(unittest.TestCase):
    def test_explain_day_lists_reasons_and_occupancy(self):
        _, service = new_service()
        register_courtyard(service)
        register_equipment(service)
        booking_id = hold_and_confirm(service, make_request("r1"), "sp-courtyard-3")

        day = explain_day(service.store, "2026-10-10")
        data = day.to_dict()
        self.assertEqual(data["day"], "2026-10-10")
        space = next(s for s in data["spaces"] if s["space_id"] == "sp-courtyard-3")
        self.assertEqual(space["version"], 1)

        occupied = [s for s in space["segments"] if not s["available"] and s["booking_id"]]
        self.assertTrue(occupied)
        seg = occupied[0]
        self.assertEqual(seg["booking_status"], "CONFIRMED")
        self.assertIn("公益音乐会", seg["title"])

        free = [s for s in space["segments"] if s["available"]]
        self.assertTrue(free)

        # 安静时段在周五当天出现（周五 20:00 起）。
        fri = explain_day(service.store, "2026-10-09").to_dict()
        fri_space = fri["spaces"][0]
        quiet_unavail = [
            s for s in fri_space["segments"]
            if not s["available"] and any(r["code"] == "QUIET_HOURS" for r in s["reasons"])
        ]
        self.assertTrue(quiet_unavail)

        # 设备锁定清单指向同场活动。
        lock = next(l for l in data["equipment_locks"] if l["equipment_id"] == "eq-audio")
        self.assertEqual(lock["booking_id"], booking_id)

    def test_explain_empty_day_is_available(self):
        _, service = new_service()
        register_courtyard(service)
        data = explain_day(service.store, "2026-10-14").to_dict()
        space = data["spaces"][0]
        self.assertTrue(all(s["available"] for s in space["segments"]))


class ComplaintTraceTest(unittest.TestCase):
    def test_trace_links_approval_basis_adjustments_and_recovery(self):
        _, service = new_service()
        register_courtyard(service, space_id="sp-a", name="东院", capacity=200)
        register_courtyard(service, space_id="sp-b", name="西院", capacity=260)
        register_equipment(service)
        booking_id = hold_and_confirm(
            service, make_request("r1", title="户外放映"), "sp-a"
        )

        storm = Disruption(
            disruption_id="dp-storm", kind=DisruptionType.STORM,
            window=make_request("r1").window, space_id="sp-a",
            summary="暴雨预警", logged_at=dt("2026-10-10T10:00:00+08:00"),
        )
        service.log_disruption(storm)
        state = service.store.state()
        plan = next(state.adjustments[i] for i in state.booking(booking_id).adjustment_ids
                    if state.adjustments[i].kind.value == "RELOCATE")
        service.decide_adjustment(
            plan.adjustment_id, Decision.APPROVED, "运营办孙主任",
            dt("2026-10-10T10:15:00+08:00"), responsible_person=True,
        )
        service.apply_adjustment(plan.adjustment_id, dt("2026-10-10T10:20:00+08:00"))
        service.resolve_disruption(
            "dp-storm", dt("2026-10-10T13:00:00+08:00"), "现场恢复"
        )

        complaint = Complaint(
            complaint_id="cp-1",
            booking_id=booking_id,
            received_at=dt("2026-10-11T09:30:00+08:00"),
            source="12345 工单（脱敏）",
            content="居民反映放映当晚扩声超约定时段",
        )
        service.file_complaint(complaint)
        service.resolve_complaint(
            "cp-1", dt("2026-10-12T15:00:00+08:00"),
            "核对确认依据与现场迁场记录，向居民代表反馈并补充音量值守约定",
        )

        trace = trace_complaint(service.store, "cp-1").to_dict()

        # 批准依据：确认时的空间版本与三方意见。
        self.assertIsNotNone(trace["approval_basis"])
        self.assertEqual(set(trace["approval_basis"]["basis"]["opinions"]),
                         {"HERITAGE", "FIRE", "LOCAL"})

        # 现场调整：东院 -> 西院两代分配。
        spaces = [g["space_id"] for g in trace["on_site_adjustments"]]
        self.assertEqual(spaces, ["sp-a", "sp-b"])

        # 时间线包含关键事件，且顺序与事件流一致。
        labels = [t["label"] for t in trace["timeline"]]
        self.assertIn("活动暂占", labels)
        self.assertIn("暂占转为确定档期", labels)
        self.assertIn("现场迁场执行", labels)
        self.assertIn("扰动结束/现场恢复", labels)
        self.assertIn("收到投诉", labels)
        self.assertIn("投诉办结", labels)

        # 所有方案都标明非系统自动批准。
        decisions = [r for r in trace["recovery"] if "decision" in r]
        self.assertTrue(decisions)
        self.assertTrue(all(r["auto_approved"] is False for r in decisions))
        approved = next(r for r in decisions if r["decision"] == "APPROVED")
        self.assertEqual(approved["decided_by"], "运营办孙主任")


class EventStorePersistenceTest(unittest.TestCase):
    def test_save_and_load_roundtrip(self):
        store, service = new_service()
        register_courtyard(service)
        register_equipment(service)
        hold_and_confirm(service, make_request("r1"), "sp-courtyard-3")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            store.save(path)
            reloaded = store.load(path)
            self.assertEqual(reloaded.revision(), store.revision())
            state = reloaded.state()
            self.assertEqual(len(state.bookings), 1)
            self.assertEqual(next(iter(state.bookings.values())).status, "CONFIRMED")
            # 落盘内容为合法 JSON 信封。
            for line in path.read_text(encoding="utf-8").splitlines():
                record = json.loads(line)
                self.assertIn("event_id", record)
                self.assertIn("kind", record)


if __name__ == "__main__":
    unittest.main()
