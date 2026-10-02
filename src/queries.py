"""查询侧：某天空间可用性解释、投诉反查。

查询只读事件流归约结果与原始事件，不产生任何新决定——所有“例外”
的解释都同时给出其决定人与批准事件，保证可追溯。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone

from . import rules
from .models import (
    Decision,
    DisruptionType,
    EventKind,
    iso,
)


DAY_START = time(0, 0)


# ---------------------------------------------------------------- 可用性解释

@dataclass(frozen=True)
class Segment:
    start: datetime
    end: datetime
    available: bool
    reasons: list[dict]                 # 不可用原因（rules.Reason 序列化）
    booking_id: str | None = None
    booking_status: str | None = None
    title: str | None = None

    def to_dict(self) -> dict:
        return {
            "start": iso(self.start),
            "end": iso(self.end),
            "available": self.available,
            "reasons": self.reasons,
            "booking_id": self.booking_id,
            "booking_status": self.booking_status,
            "title": self.title,
        }


@dataclass(frozen=True)
class SpaceDay:
    space_id: str
    version: int | None
    name: str
    zone: str
    capacity: int | None
    standing: dict
    segments: list[Segment]
    bookings: list[dict]
    disruptions: list[dict]
    available_note: str

    def to_dict(self) -> dict:
        return {
            "space_id": self.space_id,
            "version": self.version,
            "name": self.name,
            "zone": self.zone,
            "capacity": self.capacity,
            "standing": self.standing,
            "available_note": self.available_note,
            "segments": [s.to_dict() for s in self.segments],
            "bookings": self.bookings,
            "disruptions": self.disruptions,
        }


@dataclass(frozen=True)
class DayExplanation:
    day: str
    spaces: list[SpaceDay]
    equipment_locks: list[dict]

    def to_dict(self) -> dict:
        return {
            "day": self.day,
            "spaces": [s.to_dict() for s in self.spaces],
            "equipment_locks": self.equipment_locks,
        }


def explain_day(store, day, tz: timezone | None = None) -> DayExplanation:
    """解释某一天每块空间为什么可用或不可用。

    把当天按“封闭窗口、安静时段、暂占/确定预约、活跃扰动”的边界切成
    连续时段；不可用时段给出与命令侧拦截完全一致的原因码与证据。
    """
    tz = tz or timezone(timedelta(hours=8))
    if isinstance(day, str):
        from datetime import date as _date

        day = _date.fromisoformat(day)
    day_start = datetime.combine(day, DAY_START, tzinfo=tz)
    day_end = day_start + timedelta(days=1)
    state = store.state()

    space_days: list[SpaceDay] = []
    for space_id in state.known_space_ids():
        ver = state.space_version(space_id, day_start + timedelta(minutes=1))
        standing = _standing_constraints(ver, day_start, day_end)

        # 收集切片边界。
        breaks = {day_start, day_end}
        for win in standing["quiet_windows"] + standing["blocked_windows"]:
            breaks.add(max(day_start, min(day_end, win["_start"])))
            breaks.add(max(day_start, min(day_end, win["_end"])))

        day_bookings: list[dict] = []
        for view in state.space_occupants(space_id):
            asm = view.active_assignment
            if asm is None or asm.window.end <= day_start or asm.window.start >= day_end:
                continue
            seg_start = max(day_start, asm.window.start)
            seg_end = min(day_end, asm.window.end)
            breaks.add(seg_start)
            breaks.add(seg_end)
            day_bookings.append({
                "booking_id": view.booking_id,
                "title": view.request.title,
                "organizer": view.request.organizer,
                "status": view.status,
                "window": asm.window.to_dict(),
                "equipment_ids": list(asm.equipment_ids),
                "space_version": asm.space_version,
                "confirmed_at": iso(view.confirmed_at) if view.confirmed_at else None,
            })

        day_disruptions: list[dict] = []
        for d in state.disruptions.values():
            if d.window.end <= day_start or d.window.start >= day_end:
                continue
            if d.space_id not in (None, space_id):
                continue
            seg_start = max(day_start, d.window.start)
            seg_end = min(day_end, d.window.end)
            breaks.add(seg_start)
            breaks.add(seg_end)
            day_disruptions.append(d.to_dict())

        ordered = sorted(breaks)
        segments: list[Segment] = []
        for left, right in zip(ordered, ordered[1:]):
            if left == right:
                continue
            probe = _probe_window(left, right)

            seg_reasons: list[dict] = []
            if ver is None:
                seg_reasons.append({
                    "code": rules.Check.NO_VALID_VERSION,
                    "message": "当天无有效空间版本",
                    "evidence": {"space_id": space_id},
                })
            else:
                quiet = ver.quiet_blockers(probe)
                for q in quiet:
                    seg_reasons.append({
                        "code": rules.Check.QUIET_HOURS,
                        "message": f"居民安静时段 {iso(q.start)}–{iso(q.end)}",
                        "evidence": {"window": q.to_dict(), "resident_pact": ver.resident_pact},
                    })
                for b in ver.blocked_overlaps(probe):
                    seg_reasons.append({
                        "code": rules.Check.BLOCKED_WINDOW,
                        "message": f"封闭窗口 {iso(b.start)}–{iso(b.end)}（文保施工/消防通道等）",
                        "evidence": {"window": b.to_dict(), "version": ver.version},
                    })

            booking_here = None
            for view in state.space_occupants(space_id):
                asm = view.active_assignment
                if asm is not None and probe.overlaps(asm.window):
                    booking_here = (view, asm)
                    seg_reasons.append({
                        "code": rules.Check.SPACE_OCCUPIED,
                        "message": (
                            f"已被《{view.request.title}》占用（{_status_cn(view.status)}）"
                        ),
                        "evidence": {
                            "booking_id": view.booking_id, "status": view.status,
                            "window": asm.window.to_dict(),
                            "space_version": asm.space_version,
                        },
                    })
                    break

            # 活跃扰动本身作为现场封闭原因。
            for d in state.disruptions.values():
                if d.active and d.space_id in (None, space_id) and probe.overlaps(d.window):
                    seg_reasons.append({
                        "code": f"DISRUPTION_{d.kind.value}",
                        "message": f"扰动 {d.disruption_id}：{d.summary}",
                        "evidence": {"disruption_id": d.disruption_id,
                                     "kind": d.kind.value, "window": d.window.to_dict()},
                    })

            if booking_here is not None:
                view, asm = booking_here
                segments.append(Segment(
                    start=left, end=right, available=False,
                    reasons=seg_reasons,
                    booking_id=view.booking_id,
                    booking_status=view.status,
                    title=view.request.title,
                ))
            else:
                segments.append(Segment(
                    start=left, end=right,
                    available=ver is not None and not seg_reasons,
                    reasons=seg_reasons,
                ))

        available_minutes = sum(
            (s.end - s.start).total_seconds() // 60 for s in segments if s.available
        )
        note = (
            f"当天可排期 {int(available_minutes)} 分钟；可新办活动需满足容量"
            f" {ver.capacity} 人以内、避开安静/封闭时段并完成文保、消防、属地三方意见"
            if ver else "当天无有效版本，不能安排活动"
        )

        space_days.append(SpaceDay(
            space_id=space_id,
            version=ver.version if ver else None,
            name=ver.name if ver else space_id,
            zone=ver.zone if ver else "",
            capacity=ver.capacity if ver else None,
            standing=standing["public"],
            segments=segments,
            bookings=day_bookings,
            disruptions=day_disruptions,
            available_note=note,
        ))

    # 共用设备当天锁定情况。
    locks: list[dict] = []
    for eid in state.known_equipment_ids():
        for view in state.equipment_occupants(eid):
            asm = view.active_assignment
            if asm is None or asm.window.end <= day_start or asm.window.start >= day_end:
                continue
            ev = state.equipment_version(eid, day_start + timedelta(minutes=1))
            locks.append({
                "equipment_id": eid,
                "name": ev.name if ev else eid,
                "booking_id": view.booking_id,
                "title": view.request.title,
                "status": view.status,
                "window": asm.window.to_dict(),
            })

    return DayExplanation(day=day.isoformat(), spaces=space_days, equipment_locks=locks)


def _probe_window(left: datetime, right: datetime):
    from .models import Window

    # 用时段中点的极短窗口探测，避免跨到相邻时段的约束。
    mid = left + (right - left) / 2
    return Window(mid - timedelta(seconds=1), min(right, mid + timedelta(seconds=1)))


def _standing_constraints(ver, day_start, day_end) -> dict:
    if ver is None:
        return {"public": {}, "quiet_windows": [], "blocked_windows": []}
    quiet = []
    for q in ver.quiet_blockers(_day_window(day_start, day_end)):
        quiet.append({"_start": q.start, "_end": q.end, **q.to_dict()})
    blocked = []
    for b in ver.blocked_windows:
        if b.end > day_start and b.start < day_end:
            blocked.append({"_start": b.start, "_end": b.end, **b.to_dict()})
    public = {
        "capacity": ver.capacity,
        "heritage_conditions": list(ver.heritage_conditions),
        "accessible_route": ver.accessible_route,
        "equipment_route": ver.equipment_route,
        "fire_access": ver.fire_access,
        "resident_pact": ver.resident_pact,
        "quiet_windows": [{"start": q["start"], "end": q["end"]} for q in quiet],
        "blocked_windows": [{"start": b["start"], "end": b["end"]} for b in blocked],
    }
    return {"public": public, "quiet_windows": quiet, "blocked_windows": blocked}


def _day_window(day_start, day_end):
    from .models import Window

    # quiet_blockers 会覆盖跨午夜时段，用整日探测即可。
    return Window(day_start, day_end)


def _status_cn(status: str) -> str:
    return {"HELD": "暂占", "CONFIRMED": "确定档期", "CANCELLED": "已取消"}.get(status, status)


# ---------------------------------------------------------------- 投诉反查

@dataclass(frozen=True)
class TimelineEntry:
    at: datetime
    kind: str
    label: str
    detail: dict

    def to_dict(self) -> dict:
        return {"at": iso(self.at), "kind": self.kind, "label": self.label, "detail": self.detail}


@dataclass(frozen=True)
class ComplaintTrace:
    complaint: dict
    booking_id: str
    booking_snapshot: dict
    approval_basis: dict | None
    timeline: list[TimelineEntry]
    on_site_adjustments: list[dict]
    recovery: list[dict]

    def to_dict(self) -> dict:
        return {
            "complaint": self.complaint,
            "booking_id": self.booking_id,
            "booking_snapshot": self.booking_snapshot,
            "approval_basis": self.approval_basis,
            "timeline": [t.to_dict() for t in self.timeline],
            "on_site_adjustments": self.on_site_adjustments,
            "recovery": self.recovery,
        }


def trace_complaint(store, complaint_id: str) -> ComplaintTrace:
    """从一次投诉反查：当时批准依据、现场调整、后续恢复。"""
    state = store.state()
    complaint = state.complaints.get(complaint_id)
    if complaint is None:
        raise KeyError(f"投诉不存在: {complaint_id}")
    booking_id = complaint.booking_id
    view = state.bookings[booking_id]

    basis = None
    if view.confirmation_basis:
        basis = {
            "confirmed_at": iso(view.confirmed_at),
            "basis": view.confirmation_basis,
            "note": "确认时使用的空间/设备版本与三方意见即为当时批准依据",
        }

    related = {did for did in view.disruption_ids}
    for adj_id in view.adjustment_ids:
        related.add(state.adjustments[adj_id].disruption_id)
    pairs = [(ev, _timeline_entry(ev, booking_id, complaint_id))
             for ev in store.events()
             if _concerns(ev, booking_id, complaint_id, related)]
    pairs = [(ev, e) for ev, e in pairs if e is not None]
    pairs.sort(key=lambda pair: pair[0].revision)
    entries = [e for _, e in pairs]

    on_site: list[dict] = []
    for i, asm in enumerate(view.assignments):
        on_site.append({
            "generation": i + 1,
            "space_id": asm.space_id,
            "space_version": asm.space_version,
            "window": asm.window.to_dict(),
            "effective_from": iso(asm.effective_from),
            "effective_to": iso(asm.effective_to) if asm.effective_to else None,
            "capacity_cap": asm.capacity_cap,
        })

    recovery: list[dict] = []
    for adj_id in view.adjustment_ids:
        adj = state.adjustments[adj_id]
        recovery.append({
            "adjustment_id": adj.adjustment_id,
            "disruption_id": adj.disruption_id,
            "kind": adj.kind.value,
            "risk": adj.risk.value,
            "rationale": adj.rationale,
            "decision": adj.decision.value,
            "decided_by": adj.decided_by,
            "decided_at": iso(adj.decided_at) if adj.decided_at else None,
            "decision_note": adj.decision_note,
            "target_space_id": adj.target_space_id,
            "capacity_cap": adj.capacity_cap,
            "affected": [a.to_dict() for a in adj.affected],
            "auto_approved": False,  # 系统从不自动批准任何例外
        })
    for d_id in view.disruption_ids:
        d = state.disruptions[d_id]
        recovery.append({
            "type": "disruption",
            "disruption_id": d.disruption_id,
            "kind": d.kind.value,
            "summary": d.summary,
            "logged_at": iso(d.logged_at),
            "resolved_at": iso(d.resolved_at) if d.resolved_at else None,
            "resolution_note": d.resolution_note,
        })

    snapshot = {
        "request": view.request.to_dict(),
        "status": view.status,
        "cancelled_at": iso(view.cancelled_at) if view.cancelled_at else None,
        "cancel_reason": view.cancel_reason,
        "opinions": {
            k.value: op.to_dict() for k, op in sorted(view.opinions.items(), key=lambda kv: kv[0].value)
        },
    }

    return ComplaintTrace(
        complaint=complaint.to_dict(),
        booking_id=booking_id,
        booking_snapshot=snapshot,
        approval_basis=basis,
        timeline=entries,
        on_site_adjustments=on_site,
        recovery=recovery,
    )


def _concerns(ev, booking_id: str, complaint_id: str, related_disruptions: set[str]) -> bool:
    p = ev.payload
    if p.get("booking_id") == booking_id:
        return True
    complaint = p.get("complaint")
    if isinstance(complaint, dict) and complaint.get("complaint_id") == complaint_id:
        return True
    if p.get("complaint_id") == complaint_id:
        return True
    disruption = p.get("disruption")
    if isinstance(disruption, dict):
        if disruption.get("booking_id") == booking_id:
            return True
        if disruption.get("disruption_id") in related_disruptions:
            return True
    if p.get("disruption_id") in related_disruptions:
        return True
    adjustment = p.get("adjustment")
    if isinstance(adjustment, dict) and adjustment.get("booking_id") == booking_id:
        return True
    return False


def _timeline_entry(ev, booking_id: str, complaint_id: str):
    p = ev.payload
    kind = ev.kind

    if kind is EventKind.HOLD_CREATED:
        return TimelineEntry(ev.occurred_at, kind.value, "活动暂占",
                             {"space_id": p["space_id"], "request": p["request"]})
    if kind is EventKind.REVIEW_SIGNED:
        return TimelineEntry(ev.occurred_at, kind.value,
                             f"{p['opinion']['kind']} 意见签署", p["opinion"])
    if kind is EventKind.BOOKING_CONFIRMED:
        return TimelineEntry(ev.occurred_at, kind.value, "暂占转为确定档期", p["basis"])
    if kind is EventKind.BOOKING_CANCELLED:
        return TimelineEntry(ev.occurred_at, kind.value, "预约取消",
                             {"reason": p.get("reason"), "ticketed": p.get("ticketed")})
    if kind is EventKind.DISRUPTION_LOGGED:
        d = p["disruption"]
        return TimelineEntry(ev.occurred_at, kind.value,
                             f"扰动登记：{DisruptionType(d['kind']).name}", d)
    if kind is EventKind.DISRUPTION_RESOLVED:
        return TimelineEntry(ev.occurred_at, kind.value, "扰动结束/现场恢复",
                             {"note": p.get("note"), "resolved_at": p["resolved_at"]})
    if kind is EventKind.ADJUSTMENT_PROPOSED:
        a = p["adjustment"]
        return TimelineEntry(
            ev.occurred_at, kind.value,
            f"系统提出{a['kind']}建议（风险 {a['risk']}，待人工决定）",
            {"rationale": a["rationale"], "affected": a["affected"],
             "target_space_id": a.get("target_space_id"),
             "capacity_cap": a.get("capacity_cap")},
        )
    if kind is EventKind.ADJUSTMENT_DECIDED:
        a = p["adjustment"]
        label = "负责人批准高风险例外" if (
            a["risk"] == "HIGH" and a["decision"] == Decision.APPROVED.value
        ) else f"方案决定：{a['decision']}"
        return TimelineEntry(ev.occurred_at, kind.value, label,
                             {"decided_by": a["decided_by"], "note": a.get("decision_note"),
                              "risk": a["risk"]})
    if kind is EventKind.CAPACITY_REDUCED:
        return TimelineEntry(ev.occurred_at, kind.value, "现场缩容执行",
                             {"capacity_cap": p["capacity_cap"], "at": p["at"]})
    if kind is EventKind.RELOCATION_APPLIED:
        return TimelineEntry(ev.occurred_at, kind.value, "现场迁场执行", p["assignment"])
    if kind is EventKind.COMPLAINT_FILED:
        return TimelineEntry(ev.occurred_at, kind.value, "收到投诉", p["complaint"])
    if kind is EventKind.COMPLAINT_RESOLVED:
        return TimelineEntry(ev.occurred_at, kind.value, "投诉办结",
                             {"resolution": p.get("resolution")})
    return None
