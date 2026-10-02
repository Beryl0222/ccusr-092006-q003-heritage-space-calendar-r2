"""领域模型与事件回放。

事件是事实，本模块的类型是把事实回放后得到的"当前视图"。所有判断
（合规复核、可用性解释）都只读这些视图，不直接改数据；要改变状态只能
通过 service 层往事件日志追加新事件。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Optional

from .events import Event

# ---------------------------------------------------------------------------
# 时间工具：统一使用带时区的 ISO 字符串；周历窗口以"星期 + HH:MM"表示。
# ---------------------------------------------------------------------------

def parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError(f"时间必须带时区: {value}")
    return dt


def _hm(value: str) -> int:
    h, m = value.split(":")
    return int(h) * 60 + int(m)


def interval_overlap(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    return a_start < b_end and b_start < a_end


def weekly_windows_overlap(start: datetime, end: datetime, schedule: list[dict]) -> bool:
    """绝对时间区间 [start, end) 是否与周历窗口（安静时段/噪声窗口等）相交。

    窗口形如 {"days": [0..6], "start": "HH:MM", "end": "HH:MM"}，
    end <= start 时表示跨夜（如 22:00-08:00）。
    """
    for day, seg_start, seg_end in _split_by_day(start, end):
        for win in schedule:
            w_start, w_end = _hm(win["start"]), _hm(win["end"])
            days = list(win["days"])
            if w_end <= w_start:  # 跨夜窗口：拆给当天与次日
                ranges = [(day, days, w_start, 24 * 60), (day + 1, days, 0, w_end)]
            else:
                ranges = [(day, days, w_start, w_end)]
            for w_day, w_days, ws, we in ranges:
                if w_day % 7 in w_days and seg_start < we and ws < seg_end:
                    return True
    return False


def weekly_windows_cover(start: datetime, end: datetime, schedule: list[dict]) -> bool:
    """绝对时间区间是否被周历窗口的并集完整覆盖（用于设备进出/噪声许可）。"""
    for day, seg_start, seg_end in _split_by_day(start, end):
        covered: list[tuple[int, int]] = []
        for win in schedule:
            w_start, w_end = _hm(win["start"]), _hm(win["end"])
            days = list(win["days"])
            if w_end <= w_start:
                pieces = [(day, w_start, 24 * 60), (day + 1, 0, w_end)]
            else:
                pieces = [(day, w_start, w_end)]
            for w_day, ws, we in pieces:
                if w_day % 7 in days:
                    covered.append((max(seg_start, ws), min(seg_end, we)))
        # 合并区间并检查全覆盖
        covered.sort()
        cursor = seg_start
        for cs, ce in covered:
            if cs > cursor:
                return False
            cursor = max(cursor, ce)
        if cursor < seg_end:
            return False
    return True


def _split_by_day(start: datetime, end: datetime):
    """把绝对区间按本地午夜切成 (weekday, minute_start, minute_end) 段。"""
    segments = []
    cur = start
    while cur < end:
        day_start = cur.replace(hour=0, minute=0, second=0, microsecond=0)
        next_day = day_start + timedelta(days=1)
        seg_end_dt = min(end, next_day)
        segments.append(
            (cur.weekday(), _hm(cur.strftime("%H:%M")), _hm(seg_end_dt.strftime("%H:%M")))
        )
        cur = seg_end_dt
    return segments


# ---------------------------------------------------------------------------
# 空间（按版本维护）与共用设备
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SpaceVersion:
    space_id: str
    version: int
    name: str
    zone: str                      # 院落 / 广场 / 街巷集合点
    protection_level: str          # 全国重点文物保护单位 / 市级 / 历史建筑 / 无
    capacity: int
    accessible_route: bool
    accessible_note: str
    fire_aisles: tuple[str, ...]   # 必须全程保持畅通的消防通道
    noise_windows: tuple[dict, ...] = ()    # 允许噪声活动的周历窗口
    quiet_periods: tuple[dict, ...] = ()    # 居民安静时段
    load_in_windows: tuple[dict, ...] = ()  # 允许设备进出的周历窗口
    construction: tuple[dict, ...] = ()     # 文保施工等绝对时间段限制
    resident_terms: tuple[str, ...] = ()    # 周边居民约定（文本条款）
    notes: str = ""
    status: str = "active"
    effective_from: str = ""

    def construction_active(self, start: datetime, end: datetime) -> list[dict]:
        hits = []
        for c in self.construction:
            if interval_overlap(start, end, parse_dt(c["start"]), parse_dt(c["end"])):
                hits.append(c)
        return hits


@dataclass(frozen=True)
class Equipment:
    equipment_id: str
    name: str
    shared: bool                   # 共用设备需按时间独占锁定
    requires_load_in_window: bool  # 是否只能在设备进出窗口搬运
    usable_spaces: tuple[str, ...] = ()  # 空表示任意空间可用


# ---------------------------------------------------------------------------
# 活动申请与意见
# ---------------------------------------------------------------------------

REVIEW_PARTIES = ("heritage", "fire", "jurisdiction")  # 文保 / 消防 / 属地管理
PARTY_LABELS = {"heritage": "文保部门", "fire": "消防部门", "jurisdiction": "属地管理方"}

REQUEST_STATUSES = ("HELD", "CONFIRMED", "RELEASED", "CANCELLED")


@dataclass(frozen=True)
class BookingRequest:
    request_id: str
    organization: str
    title: str
    space_id: str
    setup_start: str       # 搭建开始（设备进入）
    event_start: str
    event_end: str
    teardown_end: str      # 清场完成（设备撤出）
    expected_attendance: int
    equipment_ids: tuple[str, ...]
    amplification: bool    # 是否使用扩声（户外放映/音乐会）
    ticketed: bool         # 是否已售票（取消需触发退款建议）
    accessibility_required: bool
    activity_features: tuple[str, ...] = ()        # 明火、舞台地锚锚固等特殊做法
    setup_encroaches_aisles: tuple[str, ...] = ()  # 搭台/设备会占用的通道
    submitted_at: str = ""
    hold_expires_at: str = ""

    @property
    def span(self) -> tuple[datetime, datetime]:
        return parse_dt(self.setup_start), parse_dt(self.teardown_end)

    @property
    def event_span(self) -> tuple[datetime, datetime]:
        return parse_dt(self.event_start), parse_dt(self.event_end)


@dataclass(frozen=True)
class Review:
    party: str
    decision: str           # approved / conditional / rejected
    conditions: tuple[str, ...]
    basis: str
    signed_by: str
    signed_at: str


@dataclass
class Booking:
    """一个活动申请在事件回放后的完整状态。"""

    request: BookingRequest
    status: str = "HELD"
    reviews: dict[str, Review] = field(default_factory=dict)
    confirmed_at: Optional[str] = None
    approval_basis: Optional[dict] = None   # 确认时的依据快照
    # 扰动下的现行效果：relocated_to / capacity_limit / suspended_until
    effect: Optional[dict] = None
    decision_ids: list[str] = field(default_factory=list)
    restored_at: Optional[str] = None
    cancel_reason: str = ""


@dataclass
class Disruption:
    disruption_id: str
    kind: str               # rainstorm / repair / resident_access / organizer_cancel
    severity: str           # high / medium / low
    start: str
    end: str
    space_ids: tuple[str, ...]
    block_aisles: tuple[str, ...]
    description: str
    declared_at: str
    block_accessible: bool = False


@dataclass
class Complaint:
    complaint_id: str
    request_id: str
    received_at: str
    summary: str
    resolution: Optional[dict] = None


@dataclass
class State:
    spaces: dict[str, list[SpaceVersion]] = field(default_factory=dict)
    equipment: dict[str, Equipment] = field(default_factory=dict)
    bookings: dict[str, Booking] = field(default_factory=dict)
    disruptions: dict[str, Disruption] = field(default_factory=dict)
    complaints: dict[str, Complaint] = field(default_factory=dict)

    # ---- 空间读取 -------------------------------------------------------
    def space_ids(self) -> list[str]:
        return sorted(self.spaces)

    def current_space(self, space_id: str, at: datetime | None = None) -> Optional[SpaceVersion]:
        """取某空间当前（或指定时刻）生效的版本。"""
        versions = self.spaces.get(space_id)
        if not versions:
            return None
        if at is None:
            active = [v for v in versions if v.status == "active"]
            return active[-1] if active else versions[-1]
        effective = [
            v for v in versions
            if v.effective_from and parse_dt(v.effective_from) <= at
        ]
        return effective[-1] if effective else None

    def space_name(self, space_id: str) -> str:
        v = self.current_space(space_id)
        return v.name if v else space_id

    # ---- 档期读取 -------------------------------------------------------
    def active_bookings(self) -> list[Booking]:
        return [b for b in self.bookings.values() if b.status in ("HELD", "CONFIRMED")]

    def bookings_using_space(self, space_id: str, start: datetime, end: datetime) -> list[Booking]:
        out = []
        for b in self.active_bookings():
            if b.effect and b.effect.get("relocated_to"):
                sid = b.effect["relocated_to"]
            else:
                sid = b.request.space_id
            if sid == space_id:
                bs, be = b.request.span
                if interval_overlap(start, end, bs, be):
                    out.append(b)
        return out

    def bookings_using_equipment(self, equipment_id: str, start: datetime, end: datetime) -> list[Booking]:
        out = []
        for b in self.active_bookings():
            if equipment_id in b.request.equipment_ids:
                bs, be = b.request.span
                if interval_overlap(start, end, bs, be):
                    out.append(b)
        return out

    def active_disruptions(self, at: datetime) -> list[Disruption]:
        return [
            d for d in self.disruptions.values()
            if interval_overlap(at, at + timedelta(seconds=1), parse_dt(d.start), parse_dt(d.end))
        ]


# ---------------------------------------------------------------------------
# 回放：事件流 -> State
# ---------------------------------------------------------------------------

def _as_space_version(payload: dict) -> SpaceVersion:
    return SpaceVersion(
        space_id=payload["space_id"],
        version=payload["version"],
        name=payload["name"],
        zone=payload["zone"],
        protection_level=payload["protection_level"],
        capacity=payload["capacity"],
        accessible_route=payload["accessible_route"],
        accessible_note=payload.get("accessible_note", ""),
        fire_aisles=tuple(payload.get("fire_aisles", [])),
        noise_windows=tuple(payload.get("noise_windows", [])),
        quiet_periods=tuple(payload.get("quiet_periods", [])),
        load_in_windows=tuple(payload.get("load_in_windows", [])),
        construction=tuple(payload.get("construction", [])),
        resident_terms=tuple(payload.get("resident_terms", [])),
        notes=payload.get("notes", ""),
        status=payload.get("status", "active"),
        effective_from=payload.get("effective_from", ""),
    )


def apply_event(state: State, event: Event) -> State:
    p = event.payload
    kind = event.kind

    if kind == "SPACE_VERSIONED":
        sv = _as_space_version(p)
        prior = state.spaces.setdefault(sv.space_id, [])
        for i, old in enumerate(prior):
            if old.version == sv.version:
                prior[i] = sv
                break
        else:
            if prior:
                prior[-1] = replace(prior[-1], status="superseded")
            prior.append(sv)
        prior.sort(key=lambda v: v.version)

    elif kind == "EQUIPMENT_REGISTERED":
        state.equipment[p["equipment_id"]] = Equipment(
            equipment_id=p["equipment_id"],
            name=p["name"],
            shared=p.get("shared", True),
            requires_load_in_window=p.get("requires_load_in_window", True),
            usable_spaces=tuple(p.get("usable_spaces", [])),
        )

    elif kind == "HOLD_CREATED":
        req = BookingRequest(
            request_id=event.subject_id,
            organization=p["organization"],
            title=p["title"],
            space_id=p["space_id"],
            setup_start=p["setup_start"],
            event_start=p["event_start"],
            event_end=p["event_end"],
            teardown_end=p["teardown_end"],
            expected_attendance=p["expected_attendance"],
            equipment_ids=tuple(p.get("equipment_ids", [])),
            amplification=p.get("amplification", False),
            ticketed=p.get("ticketed", False),
            accessibility_required=p.get("accessibility_required", False),
            activity_features=tuple(p.get("activity_features", [])),
            setup_encroaches_aisles=tuple(p.get("setup_encroaches_aisles", [])),
            submitted_at=event.occurred_at,
            hold_expires_at=p["hold_expires_at"],
        )
        state.bookings[req.request_id] = Booking(request=req)

    elif kind == "REVIEW_SIGNED":
        b = state.bookings[event.subject_id]
        b.reviews[p["party"]] = Review(
            party=p["party"],
            decision=p["decision"],
            conditions=tuple(p.get("conditions", [])),
            basis=p.get("basis", ""),
            signed_by=p.get("signed_by", ""),
            signed_at=event.occurred_at,
        )

    elif kind == "BOOKING_CONFIRMED":
        b = state.bookings[event.subject_id]
        b.status = "CONFIRMED"
        b.confirmed_at = event.occurred_at
        b.approval_basis = p.get("basis_snapshot")

    elif kind == "HOLD_RELEASED":
        b = state.bookings[event.subject_id]
        b.status = "RELEASED"
        b.cancel_reason = p.get("reason", "")

    elif kind == "DISRUPTION_DECLARED":
        d = Disruption(
            disruption_id=event.subject_id,
            kind=p["kind"],
            severity=p["severity"],
            start=p["start"],
            end=p["end"],
            space_ids=tuple(p.get("space_ids", [])),
            block_aisles=tuple(p.get("block_aisles", [])),
            description=p.get("description", ""),
            declared_at=event.occurred_at,
            block_accessible=p.get("block_accessible", False),
        )
        state.disruptions[d.disruption_id] = d

    elif kind == "RECOMMENDATION_LOGGED":
        # 建议本身不改变状态；受影响对象在 payload 中留痕，供追溯读取。
        pass

    elif kind == "MANUAL_DECISION":
        for rid in p.get("affected_requests", [event.subject_id]):
            b = state.bookings.get(rid)
            if b is None:
                continue
            b.decision_ids.append(event.event_id)
            action = p.get("action")
            if action == "relocate":
                b.effect = {**(b.effect or {}), "relocated_to": p["new_space_id"],
                            "capacity_limit": p.get("capacity_limit")}
            elif action == "reduce_capacity":
                b.effect = {**(b.effect or {}), "capacity_limit": p["capacity_limit"]}
            elif action == "cancel":
                b.status = "CANCELLED"
                b.cancel_reason = p.get("reason", "")
            elif action == "proceed":
                b.effect = b.effect  # 负责人决定按原计划（带条件），仅记录
            elif action == "continue_with_conditions":
                b.effect = {**(b.effect or {}), "conditions": list(p.get("conditions", []))}

    elif kind == "BOOKING_RESTORED":
        b = state.bookings[event.subject_id]
        b.effect = None
        b.restored_at = event.occurred_at

    elif kind == "COMPLAINT_RECORDED":
        state.complaints[event.subject_id] = Complaint(
            complaint_id=event.subject_id,
            request_id=p["request_id"],
            received_at=event.occurred_at,
            summary=p["summary"],
        )

    elif kind == "COMPLAINT_RESOLVED":
        c = state.complaints[event.subject_id]
        c.resolution = {"resolved_at": event.occurred_at, **p}

    return state


def replay(events: list[Event]) -> State:
    """按顺序把事件流折成当前状态。"""
    state = State()
    for ev in events:
        apply_event(state, ev)
    return state
