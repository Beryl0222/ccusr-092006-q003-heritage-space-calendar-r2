"""公共文化空间排期领域的等值模型。

只依赖标准库，时间一律使用带时区的 ``datetime``；对外序列化时由
``to_dict`` / ``from_dict`` 负责，领域内部只处理结构化值对象。

空间与共用设备都是“版本化”的：文保条件、噪声窗口、容量、无障碍路线、
设备进出通道、周边居民约定等属性按版本维护，历史预约只受其确认时有效的
版本约束，排期校验始终使用当前版本。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from enum import Enum


# ---------------------------------------------------------------- 枚举

class BookingStatus(str, Enum):
    HELD = "HELD"                  # 暂占：意见未齐前的候选状态
    CONFIRMED = "CONFIRMED"        # 确定档期：所需意见全部签署
    CANCELLED = "CANCELLED"        # 取消（如已售票，触发退款建议）


class OpinionKind(str, Enum):
    HERITAGE = "HERITAGE"          # 文保意见
    FIRE = "FIRE"                  # 消防意见
    LOCAL = "LOCAL"                # 属地管理意见（含居民安静时段等）


# 一场活动转为确定档期所需的意见集合；规则变化时只改这里。
REQUIRED_OPINIONS = frozenset(OpinionKind)


class EventKind(str, Enum):
    SPACE_VERSIONED = "SPACE_VERSIONED"
    EQUIPMENT_VERSIONED = "EQUIPMENT_VERSIONED"
    SPACE_SUPERSEDED = "SPACE_SUPERSEDED"
    EQUIPMENT_SUPERSEDED = "EQUIPMENT_SUPERSEDED"
    HOLD_CREATED = "HOLD_CREATED"
    REVIEW_SIGNED = "REVIEW_SIGNED"
    BOOKING_CONFIRMED = "BOOKING_CONFIRMED"
    BOOKING_CANCELLED = "BOOKING_CANCELLED"
    DISRUPTION_LOGGED = "DISRUPTION_LOGGED"
    DISRUPTION_RESOLVED = "DISRUPTION_RESOLVED"
    ADJUSTMENT_PROPOSED = "ADJUSTMENT_PROPOSED"
    ADJUSTMENT_DECIDED = "ADJUSTMENT_DECIDED"
    CAPACITY_REDUCED = "CAPACITY_REDUCED"
    RELOCATION_APPLIED = "RELOCATION_APPLIED"
    COMPLAINT_FILED = "COMPLAINT_FILED"
    COMPLAINT_RESOLVED = "COMPLAINT_RESOLVED"


class DisruptionType(str, Enum):
    STORM = "STORM"                          # 暴雨
    REPAIR = "REPAIR"                        # 临时修缮（含文保施工）
    RESIDENT_EMERGENCY = "RESIDENT_EMERGENCY"  # 居民紧急通行
    TICKETED_CANCELLATION = "TICKETED_CANCELLATION"  # 已售活动取消


class AdjustmentType(str, Enum):
    RELOCATE = "RELOCATE"          # 迁场
    DOWNSIZE = "DOWNSIZE"          # 缩容
    REFUND = "REFUND"              # 退款
    KEEP = "KEEP"                  # 维持原档（负责人决策后的结论之一）


class RiskLevel(str, Enum):
    ROUTINE = "ROUTINE"            # 常规替代：在既有约束内，办公室可执行
    HIGH = "HIGH"                  # 高风险：触碰文保/消防/容量红线，须负责人决定


class Decision(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


# ---------------------------------------------------------------- 时间

@dataclass(frozen=True)
class Window:
    """半开时间区间 [start, end)。"""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise ValueError("时间窗结束时间必须晚于开始时间")

    def overlaps(self, other: Window) -> bool:
        return self.start < other.end and other.start < self.end

    def contains(self, moment: datetime) -> bool:
        return self.start <= moment < self.end

    @property
    def duration_minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)

    def to_dict(self) -> dict:
        return {"start": iso(self.start), "end": iso(self.end)}

    @classmethod
    def from_dict(cls, data: dict) -> Window:
        return cls(parse_dt(data["start"]), parse_dt(data["end"]))


def parse_dt(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError("时间必须带时区偏移")
    return dt


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="minutes")


def parse_clock(value: str) -> time:
    return time.fromisoformat(value)


# ---------------------------------------------------------------- 空间与设备版本

@dataclass(frozen=True)
class SpaceVersion:
    """一块空间在某个版本下的全部排期约束。

    quiet_hours 以“星期几 + 时分”表达周边居民约定的安静时段（0=周一）；
    blocked_windows 是具体日期上的硬封闭（文保施工、消防通道占用等）。
    """

    version: int
    valid_from: datetime
    valid_to: datetime | None
    name: str
    zone: str                        # 场地分区，例如“一期院落群”
    capacity: int
    heritage_conditions: tuple[str, ...]          # 文物保护条件
    quiet_hours: tuple[tuple[int, str, str], ...]  # (weekday, HH:MM, HH:MM)
    blocked_windows: tuple[Window, ...]
    accessible_route: str            # 无障碍路线说明
    equipment_route: str             # 设备/装台进出通道
    fire_access: str                 # 消防通道要求
    resident_pact: str               # 周边居民约定（文本摘要）

    def __post_init__(self) -> None:
        if self.capacity <= 0:
            raise ValueError("容量必须为正")
        if not self.name:
            raise ValueError("空间名称不能为空")

    def quiet_blockers(self, win: Window) -> tuple[Window, ...]:
        """返回 ``win`` 与当天居民安静时段相交的时间窗（含跨午夜时段）。"""
        blockers: list[Window] = []
        day = win.start.date()
        tz = win.start.tzinfo
        # 前一日跨午夜的安静时段也可能盖住当天凌晨。
        for offset in (-1, 0):
            cur_day = day + timedelta(days=offset)
            for weekday, start_hm, end_hm in quiet_for_date(self.quiet_hours, cur_day):
                qs = datetime.combine(cur_day, parse_clock(start_hm), tzinfo=tz)
                qe = datetime.combine(cur_day, parse_clock(end_hm), tzinfo=tz)
                if qe <= qs:
                    qe += timedelta(days=1)
                qw = Window(qs, qe)
                if win.overlaps(qw):
                    blockers.append(qw)
        return tuple(blockers)

    def blocked_overlaps(self, win: Window) -> tuple[Window, ...]:
        return tuple(b for b in self.blocked_windows if win.overlaps(b))

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "valid_from": iso(self.valid_from),
            "valid_to": iso(self.valid_to) if self.valid_to else None,
            "name": self.name,
            "zone": self.zone,
            "capacity": self.capacity,
            "heritage_conditions": list(self.heritage_conditions),
            "quiet_hours": [list(q) for q in self.quiet_hours],
            "blocked_windows": [w.to_dict() for w in self.blocked_windows],
            "accessible_route": self.accessible_route,
            "equipment_route": self.equipment_route,
            "fire_access": self.fire_access,
            "resident_pact": self.resident_pact,
        }

    @classmethod
    def from_dict(cls, data: dict) -> SpaceVersion:
        return cls(
            version=data["version"],
            valid_from=parse_dt(data["valid_from"]),
            valid_to=parse_dt(data["valid_to"]) if data.get("valid_to") else None,
            name=data["name"],
            zone=data["zone"],
            capacity=data["capacity"],
            heritage_conditions=tuple(data.get("heritage_conditions", [])),
            quiet_hours=tuple((q[0], q[1], q[2]) for q in data.get("quiet_hours", [])),
            blocked_windows=tuple(Window.from_dict(w) for w in data.get("blocked_windows", [])),
            accessible_route=data.get("accessible_route", ""),
            equipment_route=data.get("equipment_route", ""),
            fire_access=data.get("fire_access", ""),
            resident_pact=data.get("resident_pact", ""),
        )


def quiet_for_date(
    quiet_hours: tuple[tuple[int, str, str], ...], day
) -> tuple[tuple[int, str, str], ...]:
    return tuple(q for q in quiet_hours if q[0] == day.weekday())


@dataclass(frozen=True)
class EquipmentVersion:
    """共用设备（流动音响、投影、无障碍坡道箱等）的版本登记。"""

    version: int
    valid_from: datetime
    valid_to: datetime | None
    name: str
    shared: bool                     # 是否为跨空间共用设备
    note: str

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "valid_from": iso(self.valid_from),
            "valid_to": iso(self.valid_to) if self.valid_to else None,
            "name": self.name,
            "shared": self.shared,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict) -> EquipmentVersion:
        return cls(
            version=data["version"],
            valid_from=parse_dt(data["valid_from"]),
            valid_to=parse_dt(data["valid_to"]) if data.get("valid_to") else None,
            name=data["name"],
            shared=data.get("shared", True),
            note=data.get("note", ""),
        )


# ---------------------------------------------------------------- 申请与意见

@dataclass(frozen=True)
class ActivityRequest:
    """文化机构提交的活动申请。"""

    request_id: str
    organizer: str                   # 文化机构名称
    title: str
    window: Window                  # 含搭建/撤场的完整占用时段
    expected_attendance: int
    requires_accessible_route: bool
    equipment_ids: tuple[str, ...]  # 需要锁定的共用设备
    ticketed: bool                  # 是否已售票
    notes: str = ""

    def __post_init__(self) -> None:
        if self.expected_attendance <= 0:
            raise ValueError("预计参与人数必须为正")
        if not self.organizer or not self.title:
            raise ValueError("机构与活动名称不能为空")

    def to_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "organizer": self.organizer,
            "title": self.title,
            "window": self.window.to_dict(),
            "expected_attendance": self.expected_attendance,
            "requires_accessible_route": self.requires_accessible_route,
            "equipment_ids": list(self.equipment_ids),
            "ticketed": self.ticketed,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ActivityRequest:
        return cls(
            request_id=data["request_id"],
            organizer=data["organizer"],
            title=data["title"],
            window=Window.from_dict(data["window"]),
            expected_attendance=data["expected_attendance"],
            requires_accessible_route=data.get("requires_accessible_route", False),
            equipment_ids=tuple(data.get("equipment_ids", [])),
            ticketed=data.get("ticketed", False),
            notes=data.get("notes", ""),
        )


@dataclass(frozen=True)
class Opinion:
    """文保 / 消防 / 属地管理三方之一出具的意见。"""

    kind: OpinionKind
    reviewer: str
    signed_at: datetime
    conclusion: str                  # 同意 / 附条件同意 / 不同意的摘要
    approved: bool
    conditions: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "kind": self.kind.value,
            "reviewer": self.reviewer,
            "signed_at": iso(self.signed_at),
            "conclusion": self.conclusion,
            "approved": self.approved,
            "conditions": list(self.conditions),
        }

    @classmethod
    def from_dict(cls, data: dict) -> Opinion:
        return cls(
            kind=OpinionKind(data["kind"]),
            reviewer=data["reviewer"],
            signed_at=parse_dt(data["signed_at"]),
            conclusion=data.get("conclusion", ""),
            approved=data["approved"],
            conditions=tuple(data.get("conditions", [])),
        )


# ---------------------------------------------------------------- 扰动、方案、投诉

@dataclass(frozen=True)
class AffectedParty:
    """方案影响对象：机构、已购票观众（估算）、居民或需协同的部门。"""

    party_type: str                 # ORGANIZER / TICKET_HOLDERS / RESIDENT / DEPARTMENT
    name: str
    contact_hint: str = ""          # 脱敏联系方式说明，禁止存放真实个人信息

    def to_dict(self) -> dict:
        return {"party_type": self.party_type, "name": self.name, "contact_hint": self.contact_hint}

    @classmethod
    def from_dict(cls, data: dict) -> AffectedParty:
        return cls(party_type=data["party_type"], name=data["name"], contact_hint=data.get("contact_hint", ""))


@dataclass(frozen=True)
class Disruption:
    """暴雨、临时修缮、居民紧急通行、已售活动取消等突发事件。"""

    disruption_id: str
    kind: DisruptionType
    window: Window                  # 预计影响时段
    logged_at: datetime
    space_id: str | None = None     # 影响的空间（居民紧急通行可能只压通道）
    booking_id: str | None = None   # 已售活动取消时指向被取消的预约
    summary: str = ""
    resolved_at: datetime | None = None
    resolution_note: str = ""

    @property
    def active(self) -> bool:
        return self.resolved_at is None

    def to_dict(self) -> dict:
        return {
            "disruption_id": self.disruption_id,
            "kind": self.kind.value,
            "window": self.window.to_dict(),
            "space_id": self.space_id,
            "booking_id": self.booking_id,
            "summary": self.summary,
            "logged_at": iso(self.logged_at),
            "resolved_at": iso(self.resolved_at) if self.resolved_at else None,
            "resolution_note": self.resolution_note,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Disruption:
        return cls(
            disruption_id=data["disruption_id"],
            kind=DisruptionType(data["kind"]),
            window=Window.from_dict(data["window"]),
            space_id=data.get("space_id"),
            booking_id=data.get("booking_id"),
            summary=data.get("summary", ""),
            logged_at=parse_dt(data["logged_at"]),
            resolved_at=parse_dt(data["resolved_at"]) if data.get("resolved_at") else None,
            resolution_note=data.get("resolution_note", ""),
        )


@dataclass(frozen=True)
class Assignment:
    """一次场地/设备分配，迁场后旧分配结束、新分配开始，形成多代记录。"""

    space_id: str
    space_version: int
    equipment_ids: tuple[str, ...]
    window: Window
    effective_from: datetime
    effective_to: datetime | None = None   # 迁场/取消时封口
    capacity_cap: int | None = None        # 缩容后的人数上限；None 表示用空间容量

    def active_at(self, moment: datetime) -> bool:
        return self.effective_from <= moment and (self.effective_to is None or moment < self.effective_to)

    def to_dict(self) -> dict:
        return {
            "space_id": self.space_id,
            "space_version": self.space_version,
            "equipment_ids": list(self.equipment_ids),
            "window": self.window.to_dict(),
            "effective_from": iso(self.effective_from),
            "effective_to": iso(self.effective_to) if self.effective_to else None,
            "capacity_cap": self.capacity_cap,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Assignment:
        return cls(
            space_id=data["space_id"],
            space_version=data["space_version"],
            equipment_ids=tuple(data.get("equipment_ids", [])),
            window=Window.from_dict(data["window"]),
            effective_from=parse_dt(data["effective_from"]),
            effective_to=parse_dt(data["effective_to"]) if data.get("effective_to") else None,
            capacity_cap=data.get("capacity_cap"),
        )


@dataclass(frozen=True)
class Adjustment:
    """针对扰动的处置方案（迁场 / 缩容 / 退款 / 维持）。

    系统只负责列明可行性、风险与受影响对象；高风险方案的
    decision 必须保持 PENDING，直到负责人显式决定，不得自动批准。
    """

    adjustment_id: str
    disruption_id: str
    booking_id: str
    kind: AdjustmentType
    risk: RiskLevel
    rationale: str
    affected: tuple[AffectedParty, ...]
    proposed_at: datetime
    decision: Decision = Decision.PENDING
    decided_by: str | None = None
    decided_at: datetime | None = None
    decision_note: str = ""
    # 迁场/缩容方案的具体落点
    target_space_id: str | None = None
    target_window: Window | None = None
    capacity_cap: int | None = None

    def to_dict(self) -> dict:
        return {
            "adjustment_id": self.adjustment_id,
            "disruption_id": self.disruption_id,
            "booking_id": self.booking_id,
            "kind": self.kind.value,
            "risk": self.risk.value,
            "rationale": self.rationale,
            "affected": [a.to_dict() for a in self.affected],
            "proposed_at": iso(self.proposed_at),
            "decision": self.decision.value,
            "decided_by": self.decided_by,
            "decided_at": iso(self.decided_at) if self.decided_at else None,
            "decision_note": self.decision_note,
            "target_space_id": self.target_space_id,
            "target_window": self.target_window.to_dict() if self.target_window else None,
            "capacity_cap": self.capacity_cap,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Adjustment:
        return cls(
            adjustment_id=data["adjustment_id"],
            disruption_id=data["disruption_id"],
            booking_id=data["booking_id"],
            kind=AdjustmentType(data["kind"]),
            risk=RiskLevel(data["risk"]),
            rationale=data.get("rationale", ""),
            affected=tuple(AffectedParty.from_dict(a) for a in data.get("affected", [])),
            proposed_at=parse_dt(data["proposed_at"]),
            decision=Decision(data.get("decision", Decision.PENDING.value)),
            decided_by=data.get("decided_by"),
            decided_at=parse_dt(data["decided_at"]) if data.get("decided_at") else None,
            decision_note=data.get("decision_note", ""),
            target_space_id=data.get("target_space_id"),
            target_window=Window.from_dict(data["target_window"]) if data.get("target_window") else None,
            capacity_cap=data.get("capacity_cap"),
        )


@dataclass(frozen=True)
class Complaint:
    """一次居民/观众投诉，用于反查当时的批准依据与处置链条。"""

    complaint_id: str
    booking_id: str
    received_at: datetime
    source: str                      # 投诉来源（脱敏：居民代表/12345 工单等）
    content: str
    resolution: str = ""
    resolved_at: datetime | None = None

    def to_dict(self) -> dict:
        return {
            "complaint_id": self.complaint_id,
            "booking_id": self.booking_id,
            "received_at": iso(self.received_at),
            "source": self.source,
            "content": self.content,
            "resolution": self.resolution,
            "resolved_at": iso(self.resolved_at) if self.resolved_at else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Complaint:
        return cls(
            complaint_id=data["complaint_id"],
            booking_id=data["booking_id"],
            received_at=parse_dt(data["received_at"]),
            source=data.get("source", ""),
            content=data.get("content", ""),
            resolution=data.get("resolution", ""),
            resolved_at=parse_dt(data["resolved_at"]) if data.get("resolved_at") else None,
        )
