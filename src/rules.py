"""排期硬规则（纯函数）。

命令侧暂占/确认/执行迁场前调用，查询侧解释“为什么不可用”时也调用
同一份规则，保证给出的理由与真正的拦截依据一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import iso


class Check:
    UNKNOWN_SPACE = "UNKNOWN_SPACE"
    NO_VALID_VERSION = "NO_VALID_VERSION"
    CAPACITY = "CAPACITY"
    QUIET_HOURS = "QUIET_HOURS"
    BLOCKED_WINDOW = "BLOCKED_WINDOW"
    ACCESSIBLE_ROUTE = "ACCESSIBLE_ROUTE"
    SPACE_OCCUPIED = "SPACE_OCCUPIED"
    UNKNOWN_EQUIPMENT = "UNKNOWN_EQUIPMENT"
    EQUIPMENT_NO_VERSION = "EQUIPMENT_NO_VERSION"
    EQUIPMENT_OCCUPIED = "EQUIPMENT_OCCUPIED"


@dataclass(frozen=True)
class Reason:
    """一条不可行/受限原因，code 稳定、message 可读、evidence 可溯源。"""

    code: str
    message: str
    evidence: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "evidence": self.evidence}


def evaluate(
    state,
    space_id: str,
    win,
    attendance: int,
    needs_access: bool,
    equipment_ids: tuple[str, ...],
    exclude_booking_id: str | None,
) -> list[Reason]:
    """按当前状态评估一次具体占用的可行性，返回原因列表（空=可行）。"""
    reasons: list[Reason] = []

    ver = state.space_version(space_id, win.start)
    if ver is None:
        if space_id not in state.spaces:
            reasons.append(Reason(
                Check.UNKNOWN_SPACE,
                f"空间 {space_id} 未登记",
                {"space_id": space_id},
            ))
        else:
            reasons.append(Reason(
                Check.NO_VALID_VERSION,
                f"空间 {space_id} 在 {iso(win.start)} 无有效版本",
                {"space_id": space_id, "at": iso(win.start)},
            ))
    else:
        if ver.valid_to is not None and ver.valid_to < win.end:
            reasons.append(Reason(
                Check.NO_VALID_VERSION,
                f"空间版本 v{ver.version} 不能覆盖整场（{iso(ver.valid_to)} 失效）",
                {"space_id": space_id, "version": ver.version, "valid_to": iso(ver.valid_to)},
            ))
        if attendance > ver.capacity:
            reasons.append(Reason(
                Check.CAPACITY,
                f"预计 {attendance} 人超过容量 {ver.capacity} 人（{ver.name}）",
                {"space_id": space_id, "version": ver.version,
                 "attendance": attendance, "capacity": ver.capacity},
            ))
        quiet = ver.quiet_blockers(win)
        if quiet:
            span = "、".join(f"{iso(q.start)}–{iso(q.end)}" for q in quiet)
            reasons.append(Reason(
                Check.QUIET_HOURS,
                f"与居民安静时段冲突 {span}",
                {"space_id": space_id, "windows": [q.to_dict() for q in quiet],
                 "resident_pact": ver.resident_pact},
            ))
        blocked = ver.blocked_overlaps(win)
        if blocked:
            span = "、".join(f"{iso(b.start)}–{iso(b.end)}" for b in blocked)
            reasons.append(Reason(
                Check.BLOCKED_WINDOW,
                f"空间处于封闭窗口 {span}（文保施工/消防通道等）",
                {"space_id": space_id, "version": ver.version,
                 "windows": [b.to_dict() for b in blocked]},
            ))
        if needs_access and not ver.accessible_route.strip():
            reasons.append(Reason(
                Check.ACCESSIBLE_ROUTE,
                f"{ver.name} 未登记无障碍路线",
                {"space_id": space_id, "version": ver.version},
            ))

        for other in state.space_occupants(space_id):
            if other.booking_id == exclude_booking_id:
                continue
            asm = other.active_assignment
            if asm is not None and win.overlaps(asm.window):
                reasons.append(Reason(
                    Check.SPACE_OCCUPIED,
                    f"与 {other.booking_id}（{other.request.title}）时段 "
                    f"{iso(asm.window.start)}–{iso(asm.window.end)} 冲突",
                    {"space_id": space_id, "booking_id": other.booking_id,
                     "status": other.status, "window": asm.window.to_dict(),
                     "space_version": asm.space_version},
                ))

    for eid in dict.fromkeys(equipment_ids):
        ev = state.equipment_version(eid, win.start)
        if ev is None:
            code = Check.UNKNOWN_EQUIPMENT if eid not in state.equipment else Check.EQUIPMENT_NO_VERSION
            reasons.append(Reason(
                code,
                f"共用设备 {eid} 未登记" if code == Check.UNKNOWN_EQUIPMENT
                else f"设备 {eid} 在该时段无有效版本",
                {"equipment_id": eid, "at": iso(win.start)},
            ))
            continue
        if not ev.shared:
            continue
        for other in state.equipment_occupants(eid):
            if other.booking_id == exclude_booking_id:
                continue
            asm = other.active_assignment
            if asm is not None and win.overlaps(asm.window):
                reasons.append(Reason(
                    Check.EQUIPMENT_OCCUPIED,
                    f"设备 {ev.name} 已被 {other.booking_id}（{other.request.title}）在 "
                    f"{iso(asm.window.start)}–{iso(asm.window.end)} 锁定",
                    {"equipment_id": eid, "equipment_version": ev.version,
                     "booking_id": other.booking_id, "status": other.status,
                     "window": asm.window.to_dict()},
                ))

    return reasons


def render(reasons: list[Reason]) -> list[str]:
    return [f"{r.code}: {r.message}" for r in reasons]
