"""事件存储与状态归约。

排期服务采用事件溯源：所有命令结果都是不可变事件，状态通过对事件流
依次 fold 得到。存储在单个进程内线程安全；命令侧通过“版本号 + 存储锁”
做乐观并发控制，保证并发申请不会同时锁定同一空间或共用设备。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .models import (
    ActivityRequest,
    Adjustment,
    Assignment,
    Complaint,
    Decision,
    Disruption,
    EquipmentVersion,
    EventKind,
    Opinion,
    OpinionKind,
    SpaceVersion,
    parse_dt,
)


class ConcurrencyError(Exception):
    """事件流版本号已变化，调用方看到的是过期状态。"""


class UnknownEventError(Exception):
    """遇到无法归约的事件种类。"""


@dataclass(frozen=True)
class Event:
    """事件信封：与外部交换的最小结构。"""

    event_id: str
    kind: EventKind
    occurred_at: datetime
    subject_id: str
    payload: dict
    revision: int = 0          # 在全局事件流中的序号，由存储分配

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "kind": self.kind.value,
            "occurred_at": self.occurred_at.isoformat(timespec="minutes"),
            "subject_id": self.subject_id,
            "revision": self.revision,
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Event:
        return cls(
            event_id=data["event_id"],
            kind=EventKind(data["kind"]),
            occurred_at=parse_dt(data["occurred_at"]),
            subject_id=data["subject_id"],
            payload=data.get("payload", {}),
            revision=data.get("revision", 0),
        )


# ---------------------------------------------------------------- 归约视图

@dataclass
class BookingView:
    """一次申请在归约后的当前状态（含多代场地分配）。"""

    booking_id: str
    request: ActivityRequest
    status: str = "HELD"
    assignments: list[Assignment] = field(default_factory=list)
    opinions: dict[OpinionKind, Opinion] = field(default_factory=dict)
    confirmed_at: datetime | None = None
    confirmation_basis: dict | None = None
    cancelled_at: datetime | None = None
    cancel_reason: str = ""
    triggered_disruption_id: str | None = None
    disruption_ids: list[str] = field(default_factory=list)
    adjustment_ids: list[str] = field(default_factory=list)
    complaint_ids: list[str] = field(default_factory=list)

    @property
    def active_assignment(self) -> Assignment | None:
        for asm in reversed(self.assignments):
            if asm.effective_to is None:
                return asm
        return None

    @property
    def current_capacity_cap(self) -> int | None:
        asm = self.active_assignment
        return asm.capacity_cap if asm else None


@dataclass
class SchedulingState:
    """事件流归约出的完整排期状态。"""

    spaces: dict[str, list[SpaceVersion]] = field(default_factory=dict)
    equipment: dict[str, list[EquipmentVersion]] = field(default_factory=dict)
    bookings: dict[str, BookingView] = field(default_factory=dict)
    requests: dict[str, ActivityRequest] = field(default_factory=dict)
    disruptions: dict[str, Disruption] = field(default_factory=dict)
    adjustments: dict[str, Adjustment] = field(default_factory=dict)
    complaints: dict[str, Complaint] = field(default_factory=dict)

    # ---- 版本查询
    def space_version(self, space_id: str, at: datetime) -> SpaceVersion | None:
        return _version_at(self.spaces.get(space_id, []), at)

    def equipment_version(self, equipment_id: str, at: datetime) -> EquipmentVersion | None:
        return _version_at(self.equipment.get(equipment_id, []), at)

    def known_space_ids(self) -> list[str]:
        return sorted(self.spaces)

    def known_equipment_ids(self) -> list[str]:
        return sorted(self.equipment)

    # ---- 占用查询：某空间/设备在某时刻的有效预约
    def space_occupants(self, space_id: str) -> list[BookingView]:
        out: list[BookingView] = []
        for b in self.bookings.values():
            if b.status == "CANCELLED":
                continue
            asm = b.active_assignment
            if asm and asm.space_id == space_id:
                out.append(b)
        return out

    def equipment_occupants(self, equipment_id: str) -> list[BookingView]:
        out: list[BookingView] = []
        for b in self.bookings.values():
            if b.status == "CANCELLED":
                continue
            asm = b.active_assignment
            if asm and equipment_id in asm.equipment_ids:
                out.append(b)
        return out

    def booking(self, booking_id: str) -> BookingView:
        return self.bookings[booking_id]


def _version_at(versions: list, at: datetime):
    """取 ``at`` 时刻有效的版本（valid_from <= at < valid_to）。"""
    chosen = None
    for ver in sorted(versions, key=lambda v: v.version):
        if ver.valid_from <= at and (ver.valid_to is None or at < ver.valid_to):
            chosen = ver
    return chosen


# ---------------------------------------------------------------- 归约

def fold(events: list[Event]) -> SchedulingState:
    state = SchedulingState()
    for ev in events:
        _apply(state, ev)
    return state


def _apply(state: SchedulingState, ev: Event) -> None:
    p = ev.payload
    kind = ev.kind

    if kind is EventKind.SPACE_VERSIONED:
        ver = SpaceVersion.from_dict(p["version"])
        state.spaces.setdefault(p["space_id"], []).append(ver)

    elif kind is EventKind.EQUIPMENT_VERSIONED:
        ver = EquipmentVersion.from_dict(p["version"])
        state.equipment.setdefault(p["equipment_id"], []).append(ver)

    elif kind is EventKind.SPACE_SUPERSEDED:
        cur = state.space_version(p["space_id"], ev.occurred_at)
        if cur is not None and cur.valid_to is None:
            _close_version(state.spaces[p["space_id"]], cur.version, parse_dt(p["valid_to"]))

    elif kind is EventKind.EQUIPMENT_SUPERSEDED:
        cur = state.equipment_version(p["equipment_id"], ev.occurred_at)
        if cur is not None and cur.valid_to is None:
            _close_version(state.equipment[p["equipment_id"]], cur.version, parse_dt(p["valid_to"]))

    elif kind is EventKind.HOLD_CREATED:
        req = ActivityRequest.from_dict(p["request"])
        asm = Assignment.from_dict(p["assignment"])
        view = BookingView(
            booking_id=p["booking_id"],
            request=req,
            status="HELD",
            assignments=[asm],
        )
        state.bookings[p["booking_id"]] = view
        state.requests[req.request_id] = req

    elif kind is EventKind.REVIEW_SIGNED:
        view = state.bookings[p["booking_id"]]
        opinion = Opinion.from_dict(p["opinion"])
        view.opinions[opinion.kind] = opinion

    elif kind is EventKind.BOOKING_CONFIRMED:
        view = state.bookings[p["booking_id"]]
        view.status = "CONFIRMED"
        view.confirmed_at = parse_dt(p["confirmed_at"])
        view.confirmation_basis = p["basis"]

    elif kind is EventKind.BOOKING_CANCELLED:
        view = state.bookings[p["booking_id"]]
        view.status = "CANCELLED"
        view.cancelled_at = parse_dt(p["cancelled_at"])
        view.cancel_reason = p.get("reason", "")
        if p.get("trigger_disruption_id"):
            view.triggered_disruption_id = p["trigger_disruption_id"]
        asm = view.active_assignment
        if asm is not None:
            view.assignments[-1] = _with_effective_to(asm, parse_dt(p["cancelled_at"]))

    elif kind is EventKind.DISRUPTION_LOGGED:
        d = Disruption.from_dict(p["disruption"])
        state.disruptions[d.disruption_id] = d
        if d.booking_id:
            state.bookings[d.booking_id].disruption_ids.append(d.disruption_id)

    elif kind is EventKind.DISRUPTION_RESOLVED:
        d = state.disruptions[p["disruption_id"]]
        state.disruptions[d.disruption_id] = _replace(
            d, resolved_at=parse_dt(p["resolved_at"]), resolution_note=p.get("note", "")
        )

    elif kind is EventKind.ADJUSTMENT_PROPOSED:
        adj = Adjustment.from_dict(p["adjustment"])
        state.adjustments[adj.adjustment_id] = adj
        state.bookings[adj.booking_id].adjustment_ids.append(adj.adjustment_id)

    elif kind is EventKind.ADJUSTMENT_DECIDED:
        adj = Adjustment.from_dict(p["adjustment"])
        state.adjustments[adj.adjustment_id] = adj

    elif kind is EventKind.CAPACITY_REDUCED:
        view = state.bookings[p["booking_id"]]
        asm = view.active_assignment
        if asm is None:
            raise UnknownEventError("缩容事件缺少有效分配")
        new_asm = Assignment(
            space_id=asm.space_id,
            space_version=asm.space_version,
            equipment_ids=asm.equipment_ids,
            window=asm.window,
            effective_from=parse_dt(p["at"]),
            effective_to=None,
            capacity_cap=int(p["capacity_cap"]),
        )
        view.assignments[-1] = _with_effective_to(asm, parse_dt(p["at"]))
        view.assignments.append(new_asm)

    elif kind is EventKind.RELOCATION_APPLIED:
        view = state.bookings[p["booking_id"]]
        asm = view.active_assignment
        if asm is None:
            raise UnknownEventError("迁场事件缺少有效分配")
        view.assignments[-1] = _with_effective_to(asm, parse_dt(p["at"]))
        view.assignments.append(Assignment.from_dict(p["assignment"]))

    elif kind is EventKind.COMPLAINT_FILED:
        c = Complaint.from_dict(p["complaint"])
        state.complaints[c.complaint_id] = c
        state.bookings[c.booking_id].complaint_ids.append(c.complaint_id)

    elif kind is EventKind.COMPLAINT_RESOLVED:
        c = state.complaints[p["complaint_id"]]
        state.complaints[c.complaint_id] = _replace(
            c, resolved_at=parse_dt(p["resolved_at"]), resolution=p.get("resolution", "")
        )

    else:
        raise UnknownEventError(f"未知事件种类: {kind}")


def _close_version(versions: list, version: int, valid_to: datetime) -> None:
    for i, ver in enumerate(versions):
        if ver.version == version:
            versions[i] = _replace(ver, valid_to=valid_to)
            return


def _with_effective_to(asm: Assignment, moment: datetime) -> Assignment:
    return Assignment(
        space_id=asm.space_id,
        space_version=asm.space_version,
        equipment_ids=asm.equipment_ids,
        window=asm.window,
        effective_from=asm.effective_from,
        effective_to=moment,
        capacity_cap=asm.capacity_cap,
    )


def _replace(obj, **changes):
    import dataclasses

    return dataclasses.replace(obj, **changes)


# ---------------------------------------------------------------- 存储

class EventStore:
    """进程内线程安全事件存储，可落盘为 JSON Lines。"""

    def __init__(self) -> None:
        self._events: list[Event] = []
        self._seen_ids: set[str] = set()
        self._lock = threading.RLock()
        self._seq = 0

    @property
    def lock(self) -> threading.RLock:
        """命令侧在同一把锁内完成“读状态—校验—追加”。"""
        return self._lock

    def revision(self) -> int:
        return len(self._events)

    def append(self, event: Event, expected_revision: int | None = None) -> Event:
        with self._lock:
            if expected_revision is not None and expected_revision != len(self._events):
                raise ConcurrencyError(
                    f"期望版本 {expected_revision}，当前版本 {len(self._events)}"
                )
            if event.event_id in self._seen_ids:
                raise ConcurrencyError(f"事件编号重复: {event.event_id}")
            self._seq += 1
            stored = Event(
                event_id=event.event_id,
                kind=event.kind,
                occurred_at=event.occurred_at,
                subject_id=event.subject_id,
                payload=event.payload,
                revision=len(self._events) + 1,
            )
            self._events.append(stored)
            self._seen_ids.add(stored.event_id)
            return stored

    def events(self) -> list[Event]:
        with self._lock:
            return list(self._events)

    def state(self) -> SchedulingState:
        with self._lock:
            return fold(list(self._events))

    # ---- 落盘 / 回放
    def save(self, path: str | Path) -> None:
        with self._lock:
            lines = [json.dumps(e.to_dict(), ensure_ascii=False) for e in self._events]
        Path(path).write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> EventStore:
        store = cls()
        text = Path(path).read_text(encoding="utf-8")
        for line in text.splitlines():
            line = line.strip()
            if line:
                ev = Event.from_dict(json.loads(line))
                # 回放时不做重复/版本校验，按文件顺序重建
                store._events.append(ev)
                store._seen_ids.add(ev.event_id)
        return store
