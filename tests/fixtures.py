"""测试共用构造工具：虚构院落/广场、设备、申请，不含真实信息。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src import (
    ActivityRequest,
    EquipmentVersion,
    EventStore,
    Opinion,
    OpinionKind,
    SchedulingService,
    SpaceVersion,
    Window,
)

TZ = timezone(timedelta(hours=8))


def dt(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=TZ) if "+" not in text else datetime.fromisoformat(text)


def make_space(
    version: int = 1,
    *,
    name: str = "三号院",
    zone: str = "一期院落群",
    capacity: int = 200,
    heritage=("木构建筑周边禁明火", "搭建限重 200kg/m²"),
    quiet=((4, "20:00", "08:00"), (5, "21:00", "09:00")),
    blocked=(),
    accessible: str = "南厢房无障碍坡道，宽 1.5m",
    equipment_route: str = "北侧后勤巷直入院落，货车限时 08:00 前",
    fire_access: str = "东侧 3m 消防通道全程不得占用",
    resident_pact: str = "周五20点后、周六21点后停止户外扩声",
    valid_from: datetime | None = None,
):
    return SpaceVersion(
        version=version,
        valid_from=valid_from or dt("2026-01-01T00:00:00+08:00"),
        valid_to=None,
        name=name,
        zone=zone,
        capacity=capacity,
        heritage_conditions=tuple(heritage),
        quiet_hours=tuple(quiet),
        blocked_windows=tuple(blocked),
        accessible_route=accessible,
        equipment_route=equipment_route,
        fire_access=fire_access,
        resident_pact=resident_pact,
    )


def make_equipment(version=1, *, name="流动音响A", shared=True, note="", valid_from=None):
    return EquipmentVersion(
        version=version,
        valid_from=valid_from or dt("2026-01-01T00:00:00+08:00"),
        valid_to=None,
        name=name,
        shared=shared,
        note=note,
    )


def make_request(
    request_id: str,
    *,
    organizer: str = "区文化馆",
    title: str = "公益音乐会",
    start: str = "2026-10-10T14:00:00+08:00",
    minutes: int = 120,
    attendance: int = 150,
    access: bool = True,
    equipment=("eq-audio",),
    ticketed: bool = False,
):
    s = dt(start)
    return ActivityRequest(
        request_id=request_id,
        organizer=organizer,
        title=title,
        window=Window(s, s + timedelta(minutes=minutes)),
        expected_attendance=attendance,
        requires_accessible_route=access,
        equipment_ids=tuple(equipment),
        ticketed=ticketed,
    )


def opinion(kind: OpinionKind, *, reviewer: str = "值班审查人", approved=True,
            at="2026-10-01T10:00:00+08:00", conditions=(), conclusion=None):
    return Opinion(
        kind=kind,
        reviewer=reviewer,
        signed_at=dt(at),
        conclusion=conclusion if conclusion is not None else ("同意" if approved else "不同意"),
        approved=approved,
        conditions=tuple(conditions),
    )


def all_opinions(**kw):
    base = {
        OpinionKind.HERITAGE: opinion(OpinionKind.HERITAGE, reviewer="文保所王工", **kw),
        OpinionKind.FIRE: opinion(OpinionKind.FIRE, reviewer="消防大队李参谋", **kw),
        OpinionKind.LOCAL: opinion(OpinionKind.LOCAL, reviewer="街道办张主任", **kw),
    }
    return base


def new_service() -> tuple[EventStore, SchedulingService]:
    store = EventStore()
    return store, SchedulingService(store)


def register_courtyard(
    service: SchedulingService,
    space_id: str = "sp-courtyard-3",
    *,
    capacity: int = 200,
    name: str = "三号院",
    zone: str = "一期院落群",
    blocked=(),
    quiet=((4, "20:00", "08:00"), (5, "21:00", "09:00")),
    accessible: str = "南厢房无障碍坡道，宽 1.5m",
):
    service.register_space(space_id, make_space(
        capacity=capacity, name=name, zone=zone, blocked=blocked,
        quiet=quiet, accessible=accessible,
    ))
    return space_id


def register_equipment(service: SchedulingService, equipment_id: str = "eq-audio",
                       *, name="流动音响A", shared=True):
    service.register_equipment(equipment_id, make_equipment(name=name, shared=shared))
    return equipment_id


def hold_and_confirm(service, request, space_id):
    """暂占 + 三方意见 + 确认，返回 booking_id。"""
    hold = service.place_hold(request, space_id)
    booking_id = hold.payload["booking_id"]
    for op in all_opinions().values():
        service.sign_review(booking_id, op)
    service.confirm_booking(booking_id, dt("2026-10-02T09:00:00+08:00"))
    return booking_id
