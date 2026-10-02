"""测试共用夹具：一套自洽的虚构街区数据。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src import EventStore, SchedulingService, ReadModel

CST = timezone(timedelta(hours=8))


def dt(y, m, d, hh=0, mm=0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=CST)


def iso(y, m, d, hh=0, mm=0) -> str:
    return dt(y, m, d, hh, mm).isoformat(timespec="seconds")


def build_world(clock=None, svc: "SchedulingService | None" = None):
    """三块空间 + 两台设备：院落（市级文保/室外）、广场（室外）、室内厅。"""
    if svc is None:
        svc = SchedulingService(EventStore(), clock=clock or (lambda: dt(2026, 9, 20, 10)))
    else:
        svc._clock = clock or svc._clock
    svc.define_space({
        "space_id": "yard-03", "name": "三号院", "zone": "院落",
        "protection_level": "市级文物保护单位", "capacity": 120,
        "accessible_route": True, "accessible_note": "东侧无障碍坡道",
        "fire_aisles": ["东门主通道", "北侧备弄"],
        "noise_windows": [{"days": [4, 5], "start": "18:30", "end": "21:00"}],
        "quiet_periods": [{"days": list(range(7)), "start": "21:30", "end": "08:00"}],
        "load_in_windows": [{"days": [4, 5], "start": "09:00", "end": "22:30"}],
        "resident_terms": ["21:30 后院落静场"],
    })
    svc.define_space({
        "space_id": "plaza-east", "name": "东广场", "zone": "广场",
        "protection_level": "历史建筑", "capacity": 300,
        "accessible_route": True, "accessible_note": "全线平坡",
        "fire_aisles": ["广场中线救援通道"],
        "noise_windows": [{"days": [4, 5], "start": "18:00", "end": "21:30"}],
        "quiet_periods": [{"days": list(range(7)), "start": "22:00", "end": "07:00"}],
        "load_in_windows": [{"days": [4, 5], "start": "08:00", "end": "18:00"}],
        "resident_terms": ["活动结束后30分钟内清场"],
    })
    svc.define_space({
        "space_id": "hall-01", "name": "室内展演厅", "zone": "室内场馆",
        "protection_level": "无", "capacity": 150,
        "accessible_route": True, "accessible_note": "无障碍电梯",
        "fire_aisles": ["前厅疏散通道"],
        "noise_windows": [{"days": list(range(7)), "start": "09:00", "end": "21:30"}],
        "load_in_windows": [{"days": list(range(7)), "start": "08:00", "end": "23:00"}],
        "resident_terms": [],
    })
    svc.register_equipment({"equipment_id": "proj-4k-01", "name": "4K户外放映机组",
                            "shared": True, "requires_load_in_window": True,
                            "usable_spaces": ["yard-03", "plaza-east", "hall-01"]})
    svc.register_equipment({"equipment_id": "speaker-02", "name": "流动音响组",
                            "shared": True, "requires_load_in_window": True,
                            "usable_spaces": ["yard-03", "plaza-east", "hall-01"]})
    return svc


def screening_request(**overrides) -> dict:
    """10/16（周五）三号院晚间户外放映，默认对夹具空间完全合规。"""
    spec = {
        "request_id": "rq-screening", "organization": "云上剧场",
        "title": "老街区影像夜", "space_id": "yard-03",
        "setup_start": iso(2026, 10, 16, 13),
        "event_start": iso(2026, 10, 16, 18, 30),
        "event_end": iso(2026, 10, 16, 20, 45),
        "teardown_end": iso(2026, 10, 16, 22, 30),
        "expected_attendance": 110, "equipment_ids": ["proj-4k-01"],
        "amplification": True, "ticketed": True, "accessibility_required": True,
    }
    spec.update(overrides)
    return spec


def approve_all(svc, request_id: str) -> None:
    svc.sign_review(request_id, "heritage", "approved", signed_by="文保员")
    svc.sign_review(request_id, "fire", "approved", signed_by="消防员")
    svc.sign_review(request_id, "jurisdiction", "approved", signed_by="街区办")
