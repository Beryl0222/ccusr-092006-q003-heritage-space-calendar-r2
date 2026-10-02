"""排期服务命令侧。

所有写操作都在事件存储的同一把锁内完成“读状态—规则校验—追加事件”，
因此并发申请不可能同时锁定同一空间或共用设备。

护栏原则：系统可以给出迁场 / 缩容 / 退款建议并标明风险，但
* 高风险（触碰文保、消防、容量、安静时段红线）的例外一律保持 PENDING；
* 只有负责人显式决定后才能生效，系统绝不自动批准。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from . import rules
from .models import (
    ActivityRequest,
    Adjustment,
    AdjustmentType,
    AffectedParty,
    Assignment,
    Complaint,
    Decision,
    Disruption,
    DisruptionType,
    EquipmentVersion,
    EventKind,
    Opinion,
    OpinionKind,
    REQUIRED_OPINIONS,
    RiskLevel,
    SpaceVersion,
    iso,
)
from .store import Event, EventStore, SchedulingState


class SchedulingError(Exception):
    """违反排期领域规则。"""


class DecisionRequiredError(Exception):
    """高风险方案必须由负责人决定，工作人员或系统无权批准。"""


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


# 校验结论代码的稳定入口，与查询侧共用同一份规则。
Check = rules.Check


class SchedulingService:
    def __init__(self, store: EventStore) -> None:
        self.store = store

    # ================================================================ 版本维护

    def register_space(self, space_id: str, version: SpaceVersion) -> Event:
        """首次登记一块空间。"""
        with self.store.lock:
            state = self.store.state()
            if space_id in state.spaces:
                raise SchedulingError(f"空间已登记，更新请用新版本: {space_id}")
            return self._append(
                kind=EventKind.SPACE_VERSIONED,
                subject_id=space_id,
                at=version.valid_from,
                payload={"space_id": space_id, "version": version.to_dict()},
            )

    def publish_space_version(
        self, space_id: str, next_version: SpaceVersion, at: datetime
    ) -> list[Event]:
        """发布空间新版本：旧版本在 ``at`` 失效，新版本同时生效。

        已确认的历史档期仍按确认时的版本留痕；新申请与暂占转确认都按
        当前版本重新校验。
        """
        with self.store.lock:
            state = self.store.state()
            current = state.space_version(space_id, at)
            if current is None:
                raise SchedulingError(f"空间不存在或当前无有效版本: {space_id}")
            expected_no = current.version + 1
            if next_version.version != expected_no:
                raise SchedulingError(
                    f"新版本号应为 {expected_no}，收到 {next_version.version}"
                )
            events = [
                self._append(
                    kind=EventKind.SPACE_SUPERSEDED,
                    subject_id=space_id,
                    at=at,
                    payload={"space_id": space_id, "valid_to": iso(at)},
                ),
                self._append(
                    kind=EventKind.SPACE_VERSIONED,
                    subject_id=space_id,
                    at=at,
                    payload={"space_id": space_id, "version": next_version.to_dict()},
                ),
            ]
            return events

    def register_equipment(self, equipment_id: str, version: EquipmentVersion) -> Event:
        with self.store.lock:
            state = self.store.state()
            if equipment_id in state.equipment:
                raise SchedulingError(f"设备已登记，更新请用新版本: {equipment_id}")
            return self._append(
                kind=EventKind.EQUIPMENT_VERSIONED,
                subject_id=equipment_id,
                at=version.valid_from,
                payload={"equipment_id": equipment_id, "version": version.to_dict()},
            )

    def publish_equipment_version(
        self, equipment_id: str, next_version: EquipmentVersion, at: datetime
    ) -> list[Event]:
        with self.store.lock:
            state = self.store.state()
            current = state.equipment_version(equipment_id, at)
            if current is None:
                raise SchedulingError(f"设备不存在或当前无有效版本: {equipment_id}")
            if next_version.version != current.version + 1:
                raise SchedulingError(f"设备新版本号应为 {current.version + 1}")
            events = [
                self._append(
                    kind=EventKind.EQUIPMENT_SUPERSEDED,
                    subject_id=equipment_id,
                    at=at,
                    payload={"equipment_id": equipment_id, "valid_to": iso(at)},
                ),
                self._append(
                    kind=EventKind.EQUIPMENT_VERSIONED,
                    subject_id=equipment_id,
                    at=at,
                    payload={"equipment_id": equipment_id, "version": next_version.to_dict()},
                ),
            ]
            return events

    # ================================================================ 暂占与确认

    def place_hold(
        self,
        request: ActivityRequest,
        space_id: str,
        *,
        expected_revision: int | None = None,
    ) -> Event:
        """文化机构提交活动并暂占空间与共用设备。

        暂占同样做硬冲突校验（同一空间/共用设备不能并发锁定），但不要求
        文保、消防、属地意见已经齐备——意见齐备是转“确定档期”的闸门。
        """
        with self.store.lock:
            state = self.store.state()
            if expected_revision is not None and expected_revision != self.store.revision():
                from .store import ConcurrencyError

                raise ConcurrencyError("申请基于过期状态，请刷新后重试")
            problems = self.feasibility_problems(
                state, space_id, request.window, request.expected_attendance,
                request.requires_accessible_route, request.equipment_ids,
                exclude_booking_id=None,
            )
            if problems:
                raise SchedulingError(f"暂占被拒绝: {'; '.join(problems)}")
            space_ver = state.space_version(space_id, request.window.start)
            asm = Assignment(
                space_id=space_id,
                space_version=space_ver.version,
                equipment_ids=request.equipment_ids,
                window=request.window,
                effective_from=request.window.start,
            )
            booking_id = _uid("bk")
            return self._append(
                kind=EventKind.HOLD_CREATED,
                subject_id=booking_id,
                at=datetime.now(tz=request.window.start.tzinfo),
                payload={
                    "booking_id": booking_id,
                    "space_id": space_id,
                    "request": request.to_dict(),
                    "assignment": asm.to_dict(),
                },
                expected_revision=expected_revision,
            )

    def sign_review(
        self,
        booking_id: str,
        opinion: Opinion,
    ) -> Event:
        """登记文保 / 消防 / 属地管理之一的意见。不同意也留痕。"""
        with self.store.lock:
            state = self.store.state()
            view = self._booking(state, booking_id)
            if view.status == "CANCELLED":
                raise SchedulingError("预约已取消，不能再签署意见")
            if opinion.kind in view.opinions:
                raise SchedulingError(f"{opinion.kind.value} 意见已签署，结论以首次签署为准")
            return self._append(
                kind=EventKind.REVIEW_SIGNED,
                subject_id=booking_id,
                at=opinion.signed_at,
                payload={"booking_id": booking_id, "opinion": opinion.to_dict()},
            )

    def confirm_booking(self, booking_id: str, at: datetime) -> Event:
        """暂占转确定档期：所需意见齐全且全部同意，并按当前版本复核。"""
        with self.store.lock:
            state = self.store.state()
            view = self._booking(state, booking_id)
            if view.status != "HELD":
                raise SchedulingError(f"只有暂占可以转确定，当前状态 {view.status}")
            asm = view.active_assignment
            req = view.request

            missing = [k.value for k in REQUIRED_OPINIONS if k not in view.opinions]
            if missing:
                raise SchedulingError(f"意见不齐全，缺少: {', '.join(missing)}")
            rejected = [
                k.value for k, op in view.opinions.items() if not op.approved
            ]
            if rejected:
                raise SchedulingError(f"存在不同意的意见: {', '.join(rejected)}")

            # 暂占之后空间/设备可能已发布新版本，必须按当前版本重新校验。
            problems = self.feasibility_problems(
                state, asm.space_id, req.window, req.expected_attendance,
                req.requires_accessible_route, asm.equipment_ids,
                exclude_booking_id=booking_id,
            )
            if problems:
                raise SchedulingError(f"按当前版本复核未通过: {'; '.join(problems)}")

            space_ver = state.space_version(asm.space_id, req.window.start)
            eq_versions = {
                eid: state.equipment_version(eid, req.window.start).version
                for eid in asm.equipment_ids
            }
            basis = {
                "checked_at": iso(at),
                "space": {"space_id": asm.space_id, "version": space_ver.version, "name": space_ver.name},
                "equipment": eq_versions,
                "opinions": {
                    k.value: {
                        "reviewer": op.reviewer,
                        "approved": op.approved,
                        "conditions": list(op.conditions),
                        "signed_at": iso(op.signed_at),
                    }
                    for k, op in sorted(view.opinions.items(), key=lambda kv: kv[0].value)
                },
                "required_opinions": sorted(k.value for k in REQUIRED_OPINIONS),
                "checks_passed": [
                    "CAPACITY", "QUIET_HOURS", "BLOCKED_WINDOW", "ACCESSIBLE_ROUTE",
                    "SPACE_OCCUPIED", "EQUIPMENT_OCCUPIED", "VERSIONS_VALID",
                ],
            }
            return self._append(
                kind=EventKind.BOOKING_CONFIRMED,
                subject_id=booking_id,
                at=at,
                payload={"booking_id": booking_id, "confirmed_at": iso(at), "basis": basis},
            )

    def cancel_booking(self, booking_id: str, at: datetime, reason: str) -> list[Event]:
        """取消预约；已售票的取消同时形成扰动并给出退款建议。"""
        with self.store.lock:
            state = self.store.state()
            view = self._booking(state, booking_id)
            if view.status == "CANCELLED":
                raise SchedulingError("预约已取消")
            events = [
                self._append(
                    kind=EventKind.BOOKING_CANCELLED,
                    subject_id=booking_id,
                    at=at,
                    payload={
                        "booking_id": booking_id,
                        "cancelled_at": iso(at),
                        "reason": reason,
                        "ticketed": view.request.ticketed,
                    },
                )
            ]
            if view.request.ticketed:
                win = view.request.window
                d = Disruption(
                    disruption_id=_uid("dp"),
                    kind=DisruptionType.TICKETED_CANCELLATION,
                    window=win,
                    space_id=view.active_assignment.space_id if view.active_assignment else None,
                    booking_id=booking_id,
                    summary=f"已售活动取消：{view.request.title}（{reason}）",
                    logged_at=at,
                )
                events.append(self._log_disruption(state, d))
            return events

    # ================================================================ 扰动与处置

    def log_disruption(self, disruption: Disruption) -> list[Event]:
        """登记暴雨 / 临时修缮 / 居民紧急通行，并生成处置建议。"""
        with self.store.lock:
            state = self.store.state()
            return [self._log_disruption(state, disruption)]

    def _log_disruption(self, state: SchedulingState, d: Disruption) -> Event:
        event = self._append(
            kind=EventKind.DISRUPTION_LOGGED,
            subject_id=d.disruption_id,
            at=d.logged_at,
            payload={"disruption": d.to_dict()},
        )
        # 建议在同一把锁内、按追加扰动事件后的最新归约状态生成，保证依据一致。
        local = self.store.state()
        for adj in self.plan_for_disruption(local, d):
            self._append(
                kind=EventKind.ADJUSTMENT_PROPOSED,
                subject_id=adj.adjustment_id,
                at=adj.proposed_at,
                payload={"adjustment": adj.to_dict()},
            )
        return event

    def resolve_disruption(self, disruption_id: str, at: datetime, note: str) -> list[Event]:
        """扰动结束：记录恢复，并对曾迁场的活动给出回迁建议（同样不自动执行）。"""
        with self.store.lock:
            state = self.store.state()
            d = state.disruptions.get(disruption_id)
            if d is None:
                raise SchedulingError(f"扰动不存在: {disruption_id}")
            if not d.active:
                raise SchedulingError("扰动已结束")
            events = [
                self._append(
                    kind=EventKind.DISRUPTION_RESOLVED,
                    subject_id=disruption_id,
                    at=at,
                    payload={"disruption_id": disruption_id, "resolved_at": iso(at), "note": note},
                )
            ]
            state = self.store.state()
            for adj in self.plan_returns(state, disruption_id, at):
                events.append(
                    self._append(
                        kind=EventKind.ADJUSTMENT_PROPOSED,
                        subject_id=adj.adjustment_id,
                        at=at,
                        payload={"adjustment": adj.to_dict()},
                    )
                )
            return events

    def decide_adjustment(
        self,
        adjustment_id: str,
        decision: Decision,
        decided_by: str,
        at: datetime,
        note: str = "",
        *,
        responsible_person: bool = False,
    ) -> Event:
        """对处置建议作出决定。

        高风险方案必须 ``responsible_person=True``（即负责人本人操作），
        否则抛出 DecisionRequiredError——系统不会替任何人批准例外。
        """
        if not decided_by:
            raise SchedulingError("决定必须记录决定人")
        with self.store.lock:
            state = self.store.state()
            adj = state.adjustments.get(adjustment_id)
            if adj is None:
                raise SchedulingError(f"方案不存在: {adjustment_id}")
            if adj.decision is not Decision.PENDING:
                raise SchedulingError("方案已经有决定")
            if adj.risk is RiskLevel.HIGH and decision is Decision.APPROVED and not responsible_person:
                raise DecisionRequiredError(
                    f"方案 {adjustment_id} 属高风险例外（{adj.rationale}），须负责人批准"
                )
            new_adj = _replace_adj(
                adj,
                decision=decision,
                decided_by=decided_by,
                decided_at=at,
                decision_note=note,
            )
            return self._append(
                kind=EventKind.ADJUSTMENT_DECIDED,
                subject_id=adjustment_id,
                at=at,
                payload={"adjustment": new_adj.to_dict()},
            )

    def apply_adjustment(self, adjustment_id: str, at: datetime) -> Event | None:
        """执行已批准的方案；执行前按当前状态再次复核。

        RELOCATE 产生迁场事件，DOWNSIZE 产生缩容事件，REFUND 仅留下已批准
        的退款建议记录（由票务/财务系统执行，本服务不代替退款）。
        """
        with self.store.lock:
            state = self.store.state()
            adj = state.adjustments.get(adjustment_id)
            if adj is None:
                raise SchedulingError(f"方案不存在: {adjustment_id}")
            if adj.decision is not Decision.APPROVED:
                raise SchedulingError("只能执行已批准的方案")
            view = self._booking(state, adj.booking_id)
            if view.status == "CANCELLED":
                raise SchedulingError("预约已取消，无可执行的现场安排")
            asm = view.active_assignment
            req = view.request

            if adj.kind is AdjustmentType.REFUND:
                return None  # 决定记录本身即执行凭据

            if adj.kind is AdjustmentType.DOWNSIZE:
                cap = adj.capacity_cap
                if cap is None or cap <= 0:
                    raise SchedulingError("缩容方案缺少有效人数上限")
                if cap > state.space_version(asm.space_id, req.window.start).capacity:
                    raise SchedulingError("缩容上限不得高于空间容量")
                return self._append(
                    kind=EventKind.CAPACITY_REDUCED,
                    subject_id=view.booking_id,
                    at=at,
                    payload={
                        "booking_id": view.booking_id,
                        "adjustment_id": adjustment_id,
                        "at": iso(at),
                        "capacity_cap": cap,
                    },
                )

            if adj.kind is AdjustmentType.RELOCATE:
                target = adj.target_space_id
                win = adj.target_window or req.window
                cap = min(
                    adj.capacity_cap or req.expected_attendance,
                    req.expected_attendance,
                )
                problems = self.feasibility_problems(
                    state, target, win, cap,
                    req.requires_accessible_route, asm.equipment_ids,
                    exclude_booking_id=view.booking_id,
                )
                if adj.risk is RiskLevel.ROUTINE and problems:
                    raise SchedulingError(f"迁场目标已不再可行: {'; '.join(problems)}")
                target_ver = state.space_version(target, win.start)
                new_asm = Assignment(
                    space_id=target,
                    space_version=target_ver.version if target_ver else asm.space_version,
                    equipment_ids=asm.equipment_ids,
                    window=win,
                    effective_from=at,
                    capacity_cap=adj.capacity_cap,
                )
                return self._append(
                    kind=EventKind.RELOCATION_APPLIED,
                    subject_id=view.booking_id,
                    at=at,
                    payload={
                        "booking_id": view.booking_id,
                        "adjustment_id": adjustment_id,
                        "at": iso(at),
                        "assignment": new_asm.to_dict(),
                    },
                )

            raise SchedulingError(f"不可执行的方案类型: {adj.kind}")

    # ================================================================ 投诉

    def file_complaint(self, complaint: Complaint) -> Event:
        with self.store.lock:
            state = self.store.state()
            self._booking(state, complaint.booking_id)
            return self._append(
                kind=EventKind.COMPLAINT_FILED,
                subject_id=complaint.complaint_id,
                at=complaint.received_at,
                payload={"complaint": complaint.to_dict()},
            )

    def resolve_complaint(self, complaint_id: str, at: datetime, resolution: str) -> Event:
        with self.store.lock:
            state = self.store.state()
            c = state.complaints.get(complaint_id)
            if c is None:
                raise SchedulingError(f"投诉不存在: {complaint_id}")
            if c.resolved_at is not None:
                raise SchedulingError("投诉已办结")
            return self._append(
                kind=EventKind.COMPLAINT_RESOLVED,
                subject_id=complaint_id,
                at=at,
                payload={
                    "complaint_id": complaint_id,
                    "resolved_at": iso(at),
                    "resolution": resolution,
                },
            )

    # ================================================================ 规则校验

    def feasibility_problems(
        self,
        state: SchedulingState,
        space_id: str,
        win,
        attendance: int,
        needs_access: bool,
        equipment_ids: tuple[str, ...],
        exclude_booking_id: str | None,
    ) -> list[str]:
        """返回人类可读的冲突原因列表；空列表表示可行。"""
        return rules.render(
            rules.evaluate(
                state, space_id, win, attendance, needs_access,
                equipment_ids, exclude_booking_id,
            )
        )

    # ================================================================ 方案生成

    def plan_for_disruption(
        self, state: SchedulingState, d: Disruption
    ) -> list[Adjustment]:
        """为受扰动影响的有效预约列明可行替代与受影响对象。"""
        if d.kind is DisruptionType.TICKETED_CANCELLATION:
            if not d.booking_id:
                return []
            view = state.bookings.get(d.booking_id)
            if view is None:
                return []
            return [
                self._refund_proposal(state, view, d, d.logged_at),
            ]

        plans: list[Adjustment] = []
        for view in self._impacted_bookings(state, d):
            plans.extend(self._plans_for_booking(state, view, d))
        return plans

    def _plans_for_booking(
        self, state: SchedulingState, view, d: Disruption
    ) -> list[Adjustment]:
        req = view.request
        cur = view.active_assignment
        at = d.logged_at
        plans: list[Adjustment] = []

        routine_target = self._find_alternative(
            state, d, view, cur.space_id, req.expected_attendance
        )
        if routine_target is not None:
            target_id, target_ver = routine_target
            plans.append(
                Adjustment(
                    adjustment_id=_uid("aj"),
                    disruption_id=d.disruption_id,
                    booking_id=view.booking_id,
                    kind=AdjustmentType.RELOCATE,
                    risk=RiskLevel.ROUTINE,
                    rationale=f"迁至 {target_ver.name}，容量/噪声/消防/无障碍均满足",
                    affected=self._affected(state, view, cur.space_id, target_id),
                    proposed_at=at,
                    target_space_id=target_id,
                    target_window=req.window,
                )
            )
        else:
            # 没有完全合规的替代场院：找“最接近”的场院作为高风险备选，
            # 列明触碰的红线，交负责人决定，系统绝不自行批准。
            best = self._best_exception(state, d, view)
            if best is not None:
                target_id, reasons = best
                tver = state.space_version(target_id, req.window.start)
                plans.append(
                    Adjustment(
                        adjustment_id=_uid("aj"),
                        disruption_id=d.disruption_id,
                        booking_id=view.booking_id,
                        kind=AdjustmentType.RELOCATE,
                        risk=RiskLevel.HIGH,
                        rationale="替代场院存在红线冲突：" + "；".join(reasons),
                        affected=self._affected(state, view, cur.space_id, target_id, high_risk=True),
                        proposed_at=at,
                        target_space_id=target_id,
                        target_window=req.window,
                    )
                )

        # 居民紧急通行优先尝试“原地缩容、让出通道”；其它扰动下列为补充选项。
        if d.kind is DisruptionType.RESIDENT_EMERGENCY:
            cap = max(0, int(req.expected_attendance * 0.5))
            plans.append(
                Adjustment(
                    adjustment_id=_uid("aj"),
                    disruption_id=d.disruption_id,
                    booking_id=view.booking_id,
                    kind=AdjustmentType.DOWNSIZE,
                    risk=RiskLevel.ROUTINE,
                    rationale="原地缩容约一半，预留居民紧急通行与消防通道宽度",
                    affected=self._affected(state, view, cur.space_id, cur.space_id),
                    proposed_at=at,
                    capacity_cap=cap,
                )
            )

        # 已售票且不能给出任何合规安置时，列明退款建议。
        if req.ticketed and not any(p.risk is RiskLevel.ROUTINE for p in plans):
            plans.append(self._refund_proposal(state, view, d, at))

        return plans

    def _refund_proposal(self, state, view, d: Disruption, at: datetime) -> Adjustment:
        cur = view.active_assignment
        return Adjustment(
            adjustment_id=_uid("aj"),
            disruption_id=d.disruption_id,
            booking_id=view.booking_id,
            kind=AdjustmentType.REFUND,
            risk=RiskLevel.ROUTINE,
            rationale="活动取消且无合规替代档期，建议按购票渠道全额退款",
            affected=self._affected(state, view, cur.space_id if cur else None, None),
            proposed_at=at,
        )

    def plan_returns(self, state: SchedulingState, disruption_id: str, at: datetime) -> list[Adjustment]:
        """扰动恢复后，为已迁场的活动生成回迁建议。"""
        out: list[Adjustment] = []
        d = state.disruptions[disruption_id]
        for adj in state.adjustments.values():
            if adj.disruption_id != disruption_id:
                continue
            if adj.kind is not AdjustmentType.RELOCATE or adj.decision is not Decision.APPROVED:
                continue
            view = state.bookings.get(adj.booking_id)
            if view is None or view.status == "CANCELLED":
                continue
            asm = view.active_assignment
            if asm is None:
                continue
            # 原场地 = 该迁场方案生效前的上一代分配。
            idx = next(
                (i for i in reversed(range(len(view.assignments)))
                 if view.assignments[i].effective_to is not None),
                None,
            )
            if idx is None:
                continue
            origin = view.assignments[idx].space_id
            problems = self.feasibility_problems(
                state, origin, view.request.window, view.request.expected_attendance,
                view.request.requires_accessible_route, asm.equipment_ids,
                exclude_booking_id=view.booking_id,
            )
            out.append(
                Adjustment(
                    adjustment_id=_uid("aj"),
                    disruption_id=disruption_id,
                    booking_id=view.booking_id,
                    kind=AdjustmentType.RELOCATE,
                    risk=RiskLevel.ROUTINE if not problems else RiskLevel.HIGH,
                    rationale=(
                        f"扰动已结束（{d.resolution_note or '现场恢复'}），建议回迁 {origin}"
                        if not problems else
                        "回迁原场地仍有限制：" + "；".join(problems)
                    ),
                    affected=self._affected(state, view, asm.space_id, origin,
                                            high_risk=bool(problems)),
                    proposed_at=at,
                    target_space_id=origin,
                    target_window=view.request.window,
                )
            )
        return out

    # ------------------------------------------------------------ 方案辅助

    def _impacted_bookings(self, state: SchedulingState, d: Disruption):
        for view in state.bookings.values():
            if view.status == "CANCELLED":
                continue
            asm = view.active_assignment
            if asm is None or not asm.window.overlaps(d.window):
                continue
            if d.space_id is None or asm.space_id == d.space_id:
                yield view

    def _find_alternative(self, state, d, view, origin_id: str, attendance: int):
        """找一块完全合规的替代空间；返回 (space_id, 版本)。"""
        req = view.request
        best = None
        for sid in state.known_space_ids():
            if sid == origin_id:
                continue
            ver = state.space_version(sid, req.window.start)
            if ver is None:
                continue
            problems = self.feasibility_problems(
                state, sid, req.window, attendance,
                req.requires_accessible_route,
                view.active_assignment.equipment_ids,
                exclude_booking_id=view.booking_id,
            )
            if not problems:
                # 优先同分区、容量最接近的院落/广场。
                origin_ver = state.space_version(origin_id, req.window.start)
                score = (
                    0 if origin_ver and ver.zone == origin_ver.zone else 1,
                    abs(ver.capacity - attendance),
                )
                if best is None or score < best[0]:
                    best = (score, sid, ver)
        return (best[1], best[2]) if best else None

    def _best_exception(self, state, d, view):
        """无可合规替代时，选冲突最少的场院作为高风险备选并附原因。"""
        req = view.request
        cur = view.active_assignment
        candidates = []
        for sid in state.known_space_ids():
            if sid == cur.space_id:
                continue
            if state.space_version(sid, req.window.start) is None:
                continue
            problems = self.feasibility_problems(
                state, sid, req.window, req.expected_attendance,
                req.requires_accessible_route, cur.equipment_ids,
                exclude_booking_id=view.booking_id,
            )
            if not problems:
                continue  # 理论上不会发生（已先找合规替代）
            candidates.append((len(problems), sid, problems))
        if not candidates:
            return None
        candidates.sort(key=lambda c: c[0])
        _, sid, problems = candidates[0]
        return sid, problems

    def _affected(self, state, view, from_space, to_space, *, high_risk: bool=False):
        parties: list[AffectedParty] = [
            AffectedParty("ORGANIZER", view.request.organizer, "通过机构登记联系人通知（脱敏工单）")
        ]
        if view.request.ticketed:
            parties.append(
                AffectedParty(
                    "TICKET_HOLDERS",
                    f"已购票观众约 {view.request.expected_attendance} 人",
                    "由票务平台按订单推送，不导出个人信息",
                )
            )
        for sid in {s for s in (from_space, to_space) if s}:
            ver = state.space_version(sid, view.request.window.start)
            if ver and ver.resident_pact:
                parties.append(
                    AffectedParty("RESIDENT", f"{ver.name}周边居民", "依居民约定公示栏/代表转告")
                )
        if high_risk:
            parties.append(AffectedParty("DEPARTMENT", "文保与消防属地联络人", "高风险例外须会签"))
        return tuple(parties)

    # ------------------------------------------------------------ 基础设施

    def _booking(self, state: SchedulingState, booking_id: str):
        view = state.bookings.get(booking_id)
        if view is None:
            raise SchedulingError(f"预约不存在: {booking_id}")
        return view

    def _append(
        self,
        *,
        kind: EventKind,
        subject_id: str,
        at: datetime,
        payload: dict,
        expected_revision: int | None = None,
    ) -> Event:
        event = Event(
            event_id=_uid("ev"),
            kind=kind,
            occurred_at=at,
            subject_id=subject_id,
            payload=payload,
        )
        return self.store.append(event, expected_revision=expected_revision)


def _replace_adj(adj: Adjustment, **changes) -> Adjustment:
    import dataclasses

    return dataclasses.replace(adj, **changes)
