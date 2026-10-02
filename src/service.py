"""排期服务：唯一允许向事件日志追加事实的地方。

关键边界：

* 文化机构可以**暂占**（HOLD），暂占同样对空间与共用设备加时间独占锁；
* 只有文保、消防、属地管理三方意见齐全且没有硬性违规时，才能**确认**；
* 扰动发生时系统只产出**建议**（迁场/缩容/退款）与受影响对象清单；
* 高风险例外必须由负责人在知悉依据后人工确认，系统不会自行批准；
* 所有动作都落为不可变事件，现场调整与恢复均可被投诉追溯读取。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

from .events import EventStore, Event, ConcurrentAppendError
from .model import (
    State, Booking, BookingRequest, REVIEW_PARTIES, PARTY_LABELS,
    replay, parse_dt, interval_overlap,
)
from .compliance import (
    evaluate_request, evaluate_conflicts, evaluate_disruption, ERROR,
)
CST = timezone(timedelta(hours=8))


class SchedulingError(RuntimeError):
    """业务规则拒绝（空间未知、审查缺失、高风险例外未确认等）。"""


class ConflictError(SchedulingError):
    """并发争用：同一空间或共用设备已被锁定。"""

    def __init__(self, findings):
        self.findings = findings
        super().__init__("；".join(f.message for f in findings))


class HighRiskOverrideRequired(SchedulingError):
    """拟由人工批准的决定仍存在硬性风险，须显式签收风险后才能记录。"""

    def __init__(self, reasons):
        self.reasons = reasons
        super().__init__("高风险例外需负责人显式签收：" + "；".join(reasons))


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


@dataclass(frozen=True)
class HoldResult:
    request_id: str
    expires_at: str
    findings: list


@dataclass(frozen=True)
class ConfirmResult:
    request_id: str
    basis_snapshot: dict


class SchedulingService:
    def __init__(self, store: EventStore, clock=None) -> None:
        self.store = store
        self._clock = clock or (lambda: datetime.now(CST))

    def now(self) -> datetime:
        return self._clock()

    def now_iso(self) -> str:
        return self.now().isoformat(timespec="seconds")

    def _state(self) -> State:
        return replay(self.store.read_all())

    # =====================================================================
    # 空间与设备：按版本维护
    # =====================================================================

    def define_space(self, spec: dict) -> str:
        """发布一块空间的新版本。

        spec 必须含 space_id；version 省略时自动在上一版本号上 +1，
        并把上一版本标记为 superseded（由回放处理）。
        """
        state = self._state()
        sid = spec["space_id"]
        prev = state.spaces.get(sid, [])
        version = spec.get("version")
        if version is None:
            version = (prev[-1].version + 1) if prev else 1
        payload = {
            **spec,
            "version": version,
            "effective_from": spec.get("effective_from") or self.now_iso(),
            "status": spec.get("status", "active"),
        }
        ev = Event(_uid("sp"), "SPACE_VERSIONED", self.now_iso(), sid, payload)
        self.store.append(ev)
        return f"{sid}:v{version}"

    def register_equipment(self, spec: dict) -> str:
        eid = spec["equipment_id"]
        ev = Event(_uid("eq"), "EQUIPMENT_REGISTERED", self.now_iso(), eid, spec)
        self.store.append(ev)
        return eid

    # =====================================================================
    # 暂占
    # =====================================================================

    def sweep_expired_holds(self) -> list[str]:
        """把已过暂占有效期且仍未确认的申请释放掉。"""
        state = self._state()
        now = self.now()
        expired = [
            b for b in state.bookings.values()
            if b.status == "HELD" and parse_dt(b.request.hold_expires_at) <= now
        ]
        out = []
        for b in expired:
            self.store.append(Event(
                _uid("rl"), "HOLD_RELEASED", self.now_iso(), b.request.request_id,
                {"reason": "暂占到期未完成确认，自动释放"}))
            out.append(b.request.request_id)
        return out

    def submit_request(self, req_spec: dict, *, hold_hours: int = 72) -> HoldResult:
        """提交活动并暂占空间与共用设备。

        并发策略：基于事件流版本号的 compare-and-append。冲突时按最新状态
        重新判定有限次数；仍冲突则抛 :class:`ConflictError`，绝不双锁。
        """
        self.sweep_expired_holds()
        request_id = req_spec.get("request_id") or _uid("rq")
        now = self.now()
        expires = (now + timedelta(hours=hold_hours)).isoformat(timespec="seconds")

        for attempt in range(5):
            state = self._state()
            expected = self.store.version

            if req_spec["space_id"] not in state.spaces:
                raise SchedulingError(f"空间 {req_spec['space_id']} 尚未发布任何版本，无法暂占")

            draft = BookingRequest(
                request_id=request_id,
                organization=req_spec["organization"],
                title=req_spec["title"],
                space_id=req_spec["space_id"],
                setup_start=req_spec["setup_start"],
                event_start=req_spec["event_start"],
                event_end=req_spec["event_end"],
                teardown_end=req_spec["teardown_end"],
                expected_attendance=req_spec["expected_attendance"],
                equipment_ids=tuple(req_spec.get("equipment_ids", [])),
                amplification=req_spec.get("amplification", False),
                ticketed=req_spec.get("ticketed", False),
                accessibility_required=req_spec.get("accessibility_required", False),
                activity_features=tuple(req_spec.get("activity_features", [])),
                setup_encroaches_aisles=tuple(req_spec.get("setup_encroaches_aisles", [])),
                submitted_at=now.isoformat(timespec="seconds"),
                hold_expires_at=expires,
            )
            findings = evaluate_request(state, draft) + evaluate_conflicts(state, draft)
            hard = [f for f in findings if f.severity == ERROR]
            if hard:
                # 客观硬性违规（含空间/设备已锁定）不进入暂占队列，避免无意义占锁
                lock_codes = {"SPACE_LOCKED", "EQUIPMENT_LOCKED"}
                if all(f.code in lock_codes for f in hard):
                    raise ConflictError(hard)
                raise SchedulingError("暂占被拒绝：" + "；".join(f.message for f in hard))

            payload = {
                "organization": draft.organization,
                "title": draft.title,
                "space_id": draft.space_id,
                "setup_start": draft.setup_start,
                "event_start": draft.event_start,
                "event_end": draft.event_end,
                "teardown_end": draft.teardown_end,
                "expected_attendance": draft.expected_attendance,
                "equipment_ids": list(draft.equipment_ids),
                "amplification": draft.amplification,
                "ticketed": draft.ticketed,
                "accessibility_required": draft.accessibility_required,
                "activity_features": list(draft.activity_features),
                "setup_encroaches_aisles": list(draft.setup_encroaches_aisles),
                "hold_expires_at": expires,
                "preliminary_findings": [f.to_dict() for f in findings],
            }
            try:
                self.store.append(
                    Event(_uid("hd"), "HOLD_CREATED", draft.submitted_at, request_id, payload),
                    expected_version=expected,
                )
            except ConcurrentAppendError:
                continue  # 事件流被其他申请抢先，基于新状态重判空间/设备锁
            return HoldResult(request_id, expires, findings)
        raise ConflictError([type("F", (), {"message": "并发繁忙，暂占失败，请重试"})()])

    def release_hold(self, request_id: str, reason: str) -> None:
        state = self._state()
        b = state.bookings.get(request_id)
        if b is None:
            raise SchedulingError(f"申请 {request_id} 不存在")
        if b.status != "HELD":
            raise SchedulingError(f"申请 {request_id} 当前状态 {b.status}，不可释放暂占")
        self.store.append(Event(
            _uid("rl"), "HOLD_RELEASED", self.now_iso(), request_id, {"reason": reason}))

    # =====================================================================
    # 三方意见与确认
    # =====================================================================

    def sign_review(self, request_id: str, party: str, decision: str, *,
                    conditions: list[str] | None = None, basis: str = "",
                    signed_by: str = "") -> None:
        if party not in REVIEW_PARTIES:
            raise SchedulingError(f"审查方必须是 {REVIEW_PARTIES} 之一")
        if decision not in ("approved", "conditional", "rejected"):
            raise SchedulingError("意见只能是 approved / conditional / rejected")
        state = self._state()
        b = state.bookings.get(request_id)
        if b is None:
            raise SchedulingError(f"申请 {request_id} 不存在")
        if b.status != "HELD":
            raise SchedulingError(f"申请 {request_id} 状态为 {b.status}，不再受理意见")
        if decision == "conditional" and not conditions:
            raise SchedulingError("有条件同意必须列明条件")
        payload = {"party": party, "decision": decision,
                   "conditions": list(conditions or []), "basis": basis,
                   "signed_by": signed_by}
        self.store.append(Event(
            _uid("rv"), "REVIEW_SIGNED", self.now_iso(), request_id, payload))

    def confirm(self, request_id: str) -> ConfirmResult:
        """暂占 -> 确定档期。三方意见齐全且复核无硬性违规方可确认。"""
        self.sweep_expired_holds()
        state = self._state()
        b = state.bookings.get(request_id)
        if b is None:
            raise SchedulingError(f"申请 {request_id} 不存在")
        if b.status == "RELEASED":
            raise SchedulingError("暂占已过期或被撤回，请重新提交后再确认")
        if b.status != "HELD":
            raise SchedulingError(f"申请 {request_id} 状态为 {b.status}，无法确认")
        if parse_dt(b.request.hold_expires_at) <= self.now():
            raise SchedulingError("暂占已过期，请重新提交")

        missing = [p for p in REVIEW_PARTIES if p not in b.reviews]
        if missing:
            raise SchedulingError(
                "意见不齐全，缺少：" + "、".join(PARTY_LABELS[p] for p in missing))
        rejected = [p for p, r in b.reviews.items() if r.decision == "rejected"]
        if rejected:
            raise SchedulingError(
                "以下方已出具不同意意见：" + "、".join(PARTY_LABELS[p] for p in rejected))

        # 以空间*当前版本*重新复核（暂占期间可能发布了新施工/新约定）
        findings = (evaluate_request(state, b.request)
                    + evaluate_conflicts(state, b.request, ignore_request_id=request_id))
        hard = [f for f in findings if f.severity == ERROR]
        if hard:
            raise SchedulingError("复核未通过：" + "；".join(f.message for f in hard))

        snapshot = {
            "space_id": b.request.space_id,
            "space_version": state.current_space(b.request.space_id).version,
            "reviews": {
                p: {"decision": r.decision, "conditions": list(r.conditions),
                    "basis": r.basis, "signed_by": r.signed_by, "signed_at": r.signed_at}
                for p, r in sorted(b.reviews.items())
            },
            "rule_findings": [f.to_dict() for f in findings],
        }
        self.store.append(Event(
            _uid("cf"), "BOOKING_CONFIRMED", self.now_iso(), request_id,
            {"basis_snapshot": snapshot}))
        return ConfirmResult(request_id, snapshot)

    # =====================================================================
    # 扰动：只给建议，不自动执行
    # =====================================================================

    def declare_disruption(self, spec: dict) -> dict:
        """登记扰动并产出每场受影响活动的建议与替代方案。

        返回结构同时作为 RECOMMENDATION_LOGGED 事件落库，任何后续人工
        决定都引用该建议编号——投诉追溯时能还原"系统当时建议了什么"。
        """
        disruption_id = spec.get("disruption_id") or _uid("ds")
        payload = {
            "kind": spec["kind"],
            "severity": spec.get("severity", "medium"),
            "start": spec["start"],
            "end": spec["end"],
            "space_ids": spec.get("space_ids", []),
            "block_aisles": spec.get("block_aisles", []),
            "block_accessible": spec.get("block_accessible", False),
            "description": spec.get("description", ""),
        }
        self.store.append(Event(
            _uid("dp"), "DISRUPTION_DECLARED", self.now_iso(), disruption_id, payload))

        state = self._state()
        from .model import Disruption
        d = Disruption(disruption_id=disruption_id, declared_at=self.now_iso(), **payload)
        impacts = evaluate_disruption(state, d)
        affected = [i.request_id for i in impacts]

        recommendation_id = _uid("rc")
        self.store.append(Event(
            recommendation_id, "RECOMMENDATION_LOGGED", self.now_iso(), disruption_id,
            {"impacts": [i.to_dict() for i in impacts],
             "affected_requests": affected,
             "note": "以上均为系统建议，须由负责人逐项人工决定"}),
            also_subjects=affected,
        )
        return {"disruption_id": disruption_id,
                "recommendation_id": recommendation_id,
                "impacts": impacts}

    # =====================================================================
    # 负责人的人工决定（系统不代批高风险例外）
    # =====================================================================

    def manual_decision(self, request_id: str, action: str, *, decided_by: str,
                        justification: str, recommendation_id: str = "",
                        new_space_id: str | None = None,
                        capacity_limit: int | None = None,
                        conditions: list[str] | None = None,
                        risk_acknowledged: bool = False) -> str:
        state = self._state()
        b = state.bookings.get(request_id)
        if b is None:
            raise SchedulingError(f"申请 {request_id} 不存在")
        if b.status not in ("HELD", "CONFIRMED"):
            raise SchedulingError(f"申请 {request_id} 状态为 {b.status}，无可执行的现场决定")
        if not decided_by or not justification:
            raise SchedulingError("人工决定必须记录决定人与理由")

        risk_reasons: list[str] = []
        payload_extra: dict = {}

        if action == "relocate":
            if not new_space_id:
                raise SchedulingError("迁场必须指定目标空间")
            moved = replace(b.request, space_id=new_space_id)
            target_findings = (evaluate_request(state, moved)
                               + evaluate_conflicts(state, moved, ignore_request_id=request_id)
                               + self._disruption_findings(state, moved))
            hard = [f for f in target_findings if f.severity == ERROR]
            if hard:
                risk_reasons.extend(f"[{f.code}] {f.message}" for f in hard)
            payload_extra["new_space_id"] = new_space_id
            payload_extra["target_findings"] = [f.to_dict() for f in target_findings]
            if capacity_limit is not None:
                payload_extra["capacity_limit"] = capacity_limit

        elif action == "reduce_capacity":
            if capacity_limit is None or capacity_limit <= 0:
                raise SchedulingError("缩容必须给出正整数容量上限")
            cur_space = state.current_space(
                b.effect.get("relocated_to", b.request.space_id) if b.effect else b.request.space_id)
            if cur_space and capacity_limit > cur_space.capacity:
                raise SchedulingError(
                    f"容量上限 {capacity_limit} 超过空间容量 {cur_space.capacity}")
            payload_extra["capacity_limit"] = capacity_limit
            if b.request.ticketed and capacity_limit < b.request.expected_attendance:
                payload_extra["partial_refund_required"] = True

        elif action == "cancel":
            payload_extra["reason"] = justification
            if b.request.ticketed:
                payload_extra["refund_required"] = True

        elif action == "proceed":
            # 顶着扰动/未消隐患照常举行：高风险
            open_risks = self._open_risk_findings(state, b)
            if open_risks:
                risk_reasons.extend(open_risks)

        elif action == "continue_with_conditions":
            if not conditions:
                raise SchedulingError("带条件继续必须列明条件")
            payload_extra["conditions"] = list(conditions)

        else:
            raise SchedulingError(f"未知决定类型 {action}")

        if risk_reasons and not risk_acknowledged:
            raise HighRiskOverrideRequired(risk_reasons)

        decision_id = _uid("md")
        self.store.append(Event(
            decision_id, "MANUAL_DECISION", self.now_iso(), request_id,
            {"action": action, "decided_by": decided_by,
             "justification": justification,
             "recommendation_id": recommendation_id,
             "risk_acknowledged": bool(risk_reasons) and risk_acknowledged,
             "risk_reasons": risk_reasons,
             "affected_requests": [request_id],
             **payload_extra},
        ))
        return decision_id

    def _open_risk_findings(self, state: State, b: Booking) -> list[str]:
        """找出该活动当前仍暴露在未解除扰动下的硬性风险。"""
        now = self.now()
        reasons: list[str] = []
        cur_sid = b.effect.get("relocated_to", b.request.space_id) if b.effect else b.request.space_id
        b_s, b_e = b.request.span
        for d in state.disruptions.values():
            d_s, d_e = parse_dt(d.start), parse_dt(d.end)
            if b_s >= d_e or b_e <= d_s:
                continue
            if cur_sid in d.space_ids and d.severity == "high":
                reasons.append(f"高风险扰动《{d.description}》仍覆盖 {cur_sid}（{d.start}~{d.end}）")
            if set(d.block_aisles) & set(state.current_space(cur_sid).fire_aisles):
                reasons.append(f"扰动《{d.description}》阻断消防通道")
        return reasons

    def _disruption_findings(self, state: State, req: BookingRequest) -> list:
        """迁场目标空间上仍生效的扰动（硬风险），供人工决定时显式呈现。"""
        from .compliance import Finding
        out: list[Finding] = []
        span_s, span_e = req.span
        space = state.current_space(req.space_id, span_s)
        if space is None:
            return out
        for d in state.disruptions.values():
            d_s, d_e = parse_dt(d.start), parse_dt(d.end)
            if not interval_overlap(span_s, span_e, d_s, d_e):
                continue
            if req.space_id in d.space_ids:
                out.append(Finding("TARGET_SPACE_DISRUPTED", ERROR,
                                   f"目标空间{space.name}仍受扰动《{d.description}》覆盖",
                                   basis=f"扰动事件 {d.disruption_id}（{d.severity}）"))
            if set(d.block_aisles) & set(space.fire_aisles):
                out.append(Finding("TARGET_AISLE_BLOCKED", ERROR,
                                   f"扰动《{d.description}》阻断目标空间消防通道", party="fire"))
            if d.block_accessible and req.accessibility_required:
                out.append(Finding("TARGET_ACCESSIBLE_BLOCKED", ERROR,
                                   f"扰动《{d.description}》阻断目标空间无障碍路线",
                                   party="jurisdiction"))
        return out

    def restore_booking(self, request_id: str, *, note: str) -> None:
        """扰动解除、现场恢复后，清除迁场/缩容效果并记录恢复。"""
        state = self._state()
        b = state.bookings.get(request_id)
        if b is None:
            raise SchedulingError(f"申请 {request_id} 不存在")
        if b.status not in ("HELD", "CONFIRMED"):
            raise SchedulingError(f"申请 {request_id} 状态为 {b.status}，无需恢复")
        if not b.effect:
            raise SchedulingError(f"申请 {request_id} 没有待恢复的现场调整")
        self.store.append(Event(
            _uid("rs"), "BOOKING_RESTORED", self.now_iso(), request_id, {"note": note}))

    # =====================================================================
    # 投诉
    # =====================================================================

    def record_complaint(self, request_id: str, summary: str) -> str:
        state = self._state()
        if request_id not in state.bookings:
            raise SchedulingError(f"申请 {request_id} 不存在")
        cid = _uid("cp")
        self.store.append(Event(
            cid, "COMPLAINT_RECORDED", self.now_iso(), cid,
            {"request_id": request_id, "summary": summary}))
        return cid

    def resolve_complaint(self, complaint_id: str, *, conclusion: str,
                          followups: list[str] | None = None, resolved_by: str = "") -> None:
        self.store.append(Event(
            _uid("cpr"), "COMPLAINT_RESOLVED", self.now_iso(), complaint_id,
            {"conclusion": conclusion, "followups": list(followups or []),
             "resolved_by": resolved_by}))
