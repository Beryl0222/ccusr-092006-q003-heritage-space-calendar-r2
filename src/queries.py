"""读模型：只回答"发生了什么/为什么这样"，不改变任何状态。

两个主要入口：

* :meth:`ReadModel.day_availability` —— 某一天每块空间逐时段的可用性与
  每条结论的依据（空间版本、施工、扰动、暂占/确定档期、噪声窗口、安静时段）；
* :meth:`ReadModel.complaint_trace` —— 从一次投诉反查：批准依据快照、
  系统当时给的建议、负责人现场调整决定、后续恢复。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, date

from .events import EventStore
from .model import (
    State, replay, parse_dt, interval_overlap,
)


@dataclass(frozen=True)
class Segment:
    start: str
    end: str
    state: str                 # BLOCKED / OCCUPIED / HELD / AVAILABLE
    reasons: tuple[dict, ...]

    def to_dict(self) -> dict:
        return {"start": self.start, "end": self.end, "state": self.state,
                "reasons": list(self.reasons)}


# 状态严重度，用于同一时段多条事实时给出主状态
_STATE_RANK = {"AVAILABLE": 0, "HELD": 1, "OCCUPIED": 2, "BLOCKED": 3}
_STATE_LABEL = {"AVAILABLE": "可用", "HELD": "已暂占", "OCCUPIED": "已确定档期", "BLOCKED": "不可用"}


class ReadModel:
    def __init__(self, store: EventStore) -> None:
        self.store = store

    def state(self) -> State:
        return replay(self.store.read_all())

    # ------------------------------------------------------------------
    # 某日某空间为什么可用 / 不可用
    # ------------------------------------------------------------------

    def day_availability(self, day: str | date, space_ids: list[str] | None = None) -> list[dict]:
        state = self.state()
        if isinstance(day, str):
            day = date.fromisoformat(day)
        day_start = datetime(day.year, day.month, day.day, tzinfo=self._tz_of_data(state))
        day_end = day_start + timedelta(days=1)

        out = []
        for sid in space_ids or state.space_ids():
            versions = state.spaces.get(sid, [])
            space = state.current_space(sid, day_end - timedelta(seconds=1))
            if not versions:
                out.append({"space_id": sid, "published": False,
                            "headline": "从未发布空间版本，不可安排活动", "segments": []})
                continue
            if space is None:
                out.append({"space_id": sid, "published": True,
                            "headline": "当天尚无生效版本", "segments": []})
                continue

            # 静态条件：解释"这块空间整体上是什么条件"
            static = {
                "name": space.name,
                "zone": space.zone,
                "version": space.version,
                "effective_from": space.effective_from,
                "protection_level": space.protection_level,
                "capacity": space.capacity,
                "accessible_route": space.accessible_route,
                "accessible_note": space.accessible_note,
                "fire_aisles": list(space.fire_aisles),
                "resident_terms": list(space.resident_terms),
                "noise_windows": list(space.noise_windows),
                "quiet_periods": list(space.quiet_periods),
                "load_in_windows": list(space.load_in_windows),
            }

            # 收集该天的事实区间：(start,end,state,reason)
            facts: list[tuple[datetime, datetime, str, dict]] = []

            for c in space.construction:
                cs, ce = parse_dt(c["start"]), parse_dt(c["end"])
                if interval_overlap(day_start, day_end, cs, ce):
                    facts.append((max(cs, day_start), min(ce, day_end), "BLOCKED", {
                        "type": "construction",
                        "text": f"文保施工《{c.get('name', c.get('reason', '施工'))}》占用",
                        "basis": c.get("permit", f"空间 v{space.version} 施工安排")}))

            for d in state.disruptions.values():
                if sid not in d.space_ids and d.space_ids:
                    continue
                ds, de = parse_dt(d.start), parse_dt(d.end)
                if interval_overlap(day_start, day_end, ds, de):
                    facts.append((max(ds, day_start), min(de, day_end), "BLOCKED", {
                        "type": "disruption", "text": f"扰动：{d.description}",
                        "basis": f"扰动事件 {d.disruption_id}（{d.severity}）"}))

            for b in state.active_bookings():
                cur = b.effect.get("relocated_to") if b.effect else None
                bs_id = cur or b.request.space_id
                if bs_id != sid:
                    continue
                bs, be = b.request.span
                if not interval_overlap(day_start, day_end, bs, be):
                    continue
                seg_state = "OCCUPIED" if b.status == "CONFIRMED" else "HELD"
                extra = ""
                if cur:
                    extra = f"（由 {b.request.space_id} 人工迁场至此）"
                facts.append((max(bs, day_start), min(be, day_end), seg_state, {
                    "type": "booking",
                    "request_id": b.request.request_id,
                    "title": b.request.title,
                    "organization": b.request.organization,
                    "status": b.status,
                    "relocated_from": b.request.space_id if cur else None,
                    "text": f"{'确定档期' if b.status == 'CONFIRMED' else '暂占'}："
                            f"《{b.request.title}》（{b.request.organization}）{extra}",
                    "basis": (f"确认事件 {b.confirmed_at}" if b.status == "CONFIRMED"
                              else f"暂占有效期至 {b.request.hold_expires_at}")}))

            # 已从本空间迁出的活动：时段本身释放，但要能看到"为什么空了"
            for b in state.active_bookings():
                if not (b.effect and b.effect.get("relocated_to")):
                    continue
                if b.request.space_id != sid or b.effect["relocated_to"] == sid:
                    continue
                bs, be = b.request.span
                if not interval_overlap(day_start, day_end, bs, be):
                    continue
                target = state.space_name(b.effect["relocated_to"])
                facts.append((max(bs, day_start), min(be, day_end), "AVAILABLE", {
                    "type": "relocated_away",
                    "request_id": b.request.request_id,
                    "text": f"《{b.request.title}》原定于此，已由负责人决定迁至{target}，"
                            f"本时段在本空间已释放",
                    "basis": f"人工决定事件（{', '.join(b.decision_ids) or '见活动时间线'}）"}))

            # 用所有事实边界切分当天
            boundaries = sorted({day_start, day_end,
                                 *(t for f in facts for t in (f[0], f[1]))})
            segments: list[Segment] = []
            for x0, x1 in zip(boundaries, boundaries[1:]):
                mid = x0 + (x1 - x0) / 2
                active = [f for f in facts if f[0] <= mid < f[1]]
                if not active:
                    state_name = "AVAILABLE"
                    reasons = ({"type": "free", "text": "无施工、扰动或在档活动",
                                "basis": f"空间 v{space.version} 当日排期"},)
                else:
                    state_name = max((f[2] for f in active), key=lambda s: _STATE_RANK[s])
                    reasons = tuple(f[3] for f in active)
                segments.append(Segment(x0.isoformat(timespec="minutes"),
                                        x1.isoformat(timespec="minutes"),
                                        state_name, reasons))

            # 周历窗口：标注安静时段/噪声窗口/进出窗口（约束信息，非整体占用）
            constraints = self._day_weekly(day, space, day_start)

            headline_parts = []
            if space.status != "active":
                headline_parts.append(f"空间版本 v{space.version} 状态为 {space.status}")
            blocked_min = sum(self._minutes(s) for s in segments if s.state == "BLOCKED")
            occupied_min = sum(self._minutes(s) for s in segments
                               if s.state in ("OCCUPIED", "HELD"))
            headline_parts.append(
                f"{space.name}（{space.zone}/{space.protection_level}，容量 {space.capacity} 人）："
                f"不可用 {blocked_min // 60}h{blocked_min % 60:02d}m，"
                f"在档 {occupied_min // 60}h{occupied_min % 60:02d}m")

            out.append({
                "space_id": sid,
                "published": True,
                "headline": "；".join(headline_parts),
                "space": static,
                "constraints": constraints,
                "segments": [s.to_dict() for s in segments],
            })
        return out

    @staticmethod
    def _minutes(seg: Segment) -> int:
        return int((parse_dt(seg.end) - parse_dt(seg.start)).total_seconds() // 60)

    @staticmethod
    def _tz_of_data(state: State):
        for versions in state.spaces.values():
            for v in versions:
                if v.effective_from:
                    return parse_dt(v.effective_from).tzinfo
        from datetime import timezone, timedelta
        return timezone(timedelta(hours=8))

    def _day_weekly(self, day, space, day_start) -> dict:
        """把周历窗口折算成当天的绝对时段，并标注跨夜部分。"""
        def windows(schedule):
            res = []
            for win in schedule:
                for start_min, end_min, wday in _day_pieces(win, day.weekday()):
                    res.append({
                        "start": (day_start + timedelta(minutes=start_min)).isoformat(timespec="minutes"),
                        "end": (day_start + timedelta(minutes=end_min)).isoformat(timespec="minutes"),
                        "weekday": wday,
                    })
            return res

        return {
            "noise_windows": windows(space.noise_windows),
            "quiet_periods": windows(space.quiet_periods),
            "load_in_windows": windows(space.load_in_windows),
        }

    # ------------------------------------------------------------------
    # 投诉反查
    # ------------------------------------------------------------------

    def complaint_trace(self, complaint_id: str) -> dict:
        state = self.state()
        complaint = state.complaints.get(complaint_id)
        if complaint is None:
            raise KeyError(f"投诉 {complaint_id} 不存在")
        rid = complaint.request_id
        booking = state.bookings.get(rid)

        # 该活动的全部直接事件 + 因"受影响对象"而挂接的建议事件
        direct = self.store.read_stream(rid)
        linked = self.store.scan(
            lambda e: rid in e.payload.get("_also_subjects", [])
            and e.subject_id != rid)
        disruption_ids = {e.subject_id for e in linked if e.kind == "RECOMMENDATION_LOGGED"}
        disruption_events = self.store.scan(
            lambda e: e.kind == "DISRUPTION_DECLARED" and e.subject_id in disruption_ids)

        timeline = sorted(direct + linked + disruption_events,
                          key=lambda e: (e.occurred_at, e.seq))

        approval = None
        adjustments = []
        restoration = None
        recommendations = []
        for ev in timeline:
            if ev.kind == "BOOKING_CONFIRMED":
                approval = ev.payload.get("basis_snapshot")
            elif ev.kind == "RECOMMENDATION_LOGGED":
                for impact in ev.payload.get("impacts", []):
                    if impact.get("request_id") == rid:
                        recommendations.append({
                            "recommendation_id": ev.event_id,
                            "at": ev.occurred_at,
                            "suggested_actions": impact.get("suggested_actions"),
                            "findings": impact.get("findings"),
                            "alternatives": impact.get("alternatives"),
                        })
            elif ev.kind == "MANUAL_DECISION":
                adjustments.append({
                    "decision_id": ev.event_id,
                    "at": ev.occurred_at,
                    "action": ev.payload.get("action"),
                    "decided_by": ev.payload.get("decided_by"),
                    "justification": ev.payload.get("justification"),
                    "new_space_id": ev.payload.get("new_space_id"),
                    "capacity_limit": ev.payload.get("capacity_limit"),
                    "conditions": ev.payload.get("conditions"),
                    "refund_required": ev.payload.get("refund_required"),
                    "risk_acknowledged": ev.payload.get("risk_acknowledged"),
                    "risk_reasons": ev.payload.get("risk_reasons"),
                    "recommendation_id": ev.payload.get("recommendation_id"),
                })
            elif ev.kind == "BOOKING_RESTORED":
                restoration = {"at": ev.occurred_at, "note": ev.payload.get("note")}

        return {
            "complaint": {
                "complaint_id": complaint_id,
                "received_at": complaint.received_at,
                "summary": complaint.summary,
                "resolution": complaint.resolution,
            },
            "request": {
                "request_id": rid,
                "title": booking.request.title if booking else None,
                "organization": booking.request.organization if booking else None,
                "space_id": booking.request.space_id if booking else None,
                "current_status": booking.status if booking else None,
            },
            "approval_basis": approval,
            "system_recommendations": recommendations,
            "on_site_adjustments": adjustments,
            "restoration": restoration,
            "event_timeline": [
                {"seq": e.seq, "at": e.occurred_at, "kind": e.kind,
                 "subject_id": e.subject_id, "event_id": e.event_id}
                for e in timeline
            ],
        }


def _day_pieces(win: dict, weekday: int) -> list[tuple[int, int, int]]:
    """窗口在指定星期当天贡献的 (起始分, 结束分, 所属星期) 段，含跨夜。"""
    def hm(s):
        h, m = s.split(":")
        return int(h) * 60 + int(m)

    s, e = hm(win["start"]), hm(win["end"])
    days = list(win["days"])
    pieces = []
    if e <= s:  # 跨夜
        if weekday in days:
            pieces.append((s, 24 * 60, weekday))
        if (weekday - 1) % 7 in days:
            pieces.append((0, e, (weekday - 1) % 7))
    elif weekday in days:
        pieces.append((s, e, weekday))
    return pieces
