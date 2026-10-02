"""合规规则引擎：只读判断，不产生副作用。

规则来源是街区与场地的版本化约定（文保条件、居民安静时段、消防通道、
容量、无障碍路线、设备进出窗口）。引擎把判断分成三层：

* :func:`evaluate_request` —— 一个申请在其目标空间上本身是否合规；
* :func:`evaluate_conflicts` —— 与既存暂占/确定档期是否争用同一空间或共用设备；
* :func:`evaluate_disruption` —— 扰动对每场在档活动意味着什么、有哪些可行替代。

引擎只给结论与依据（finding 列表），"是否批准/是否例外"由负责人决定。
"""

from __future__ import annotations

from dataclasses import dataclass

from .model import (
    State, BookingRequest, SpaceVersion, Equipment, Booking, Disruption,
    parse_dt, interval_overlap, weekly_windows_overlap, weekly_windows_cover,
)

# ---------------------------------------------------------------------------
# 结论结构
# ---------------------------------------------------------------------------

ERROR = "error"        # 硬性违反：不得确认、不得自动例外
WARNING = "warning"    # 需要属地协调/附加条件
INFO = "info"

# 受保护级别禁止的活动做法（明火、地锚锚固会损伤文物本体）
PROTECTION_FORBIDDEN_FEATURES = {
    "全国重点文物保护单位": ("明火", "地锚锚固", "舞台地锚"),
    "市级文物保护单位": ("明火", "地锚锚固", "舞台地锚"),
    "历史建筑": ("明火",),
}

OUTDOOR_ZONES = {"广场", "院落", "街巷集合点"}


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    message: str
    basis: str = ""          # 依据的是哪一条版本化约定/意见
    party: str = ""          # 归属审查方 heritage/fire/jurisdiction

    @property
    def blocks(self) -> bool:
        return self.severity == ERROR

    def to_dict(self) -> dict:
        return {"code": self.code, "severity": self.severity,
                "message": self.message, "basis": self.basis, "party": self.party}


@dataclass(frozen=True)
class Alternative:
    space_id: str
    space_name: str
    capacity: int
    reasons: tuple[str, ...]
    limitations: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {"space_id": self.space_id, "space_name": self.space_name,
                "capacity": self.capacity, "reasons": list(self.reasons),
                "limitations": list(self.limitations)}


# ---------------------------------------------------------------------------
# 申请本身的合规性
# ---------------------------------------------------------------------------

def evaluate_request(state: State, req: BookingRequest,
                     space: SpaceVersion | None = None,
                     at: str | None = None) -> list[Finding]:
    findings: list[Finding] = []
    probe = parse_dt(at) if at else parse_dt(req.event_start)
    space = space or state.current_space(req.space_id, probe)

    if space is None:
        return [Finding("SPACE_UNKNOWN", ERROR, f"空间 {req.space_id} 不存在或未发布版本",
                        party="jurisdiction")]
    if space.status != "active":
        findings.append(Finding("SPACE_INACTIVE", ERROR,
                                f"{space.name} 的 v{space.version} 非现行有效版本",
                                basis=f"空间版本状态 {space.status}", party="jurisdiction"))

    # 容量
    cap = space.capacity
    if req.expected_attendance > cap:
        findings.append(Finding("CAPACITY_EXCEEDED", ERROR,
                                f"预计 {req.expected_attendance} 人超过 {space.name} 容量 {cap} 人",
                                basis=f"空间 v{space.version} 容量 {cap}", party="fire"))

    # 文保施工（绝对时间互斥）
    setup_s, teardown_e = req.span
    for c in space.construction_active(setup_s, teardown_e):
        findings.append(Finding("CONSTRUCTION_CONFLICT", ERROR,
                                f"与文保施工《{c.get('name', c.get('reason', '施工'))}》"
                                f"（{c['start']} ~ {c['end']}）时间冲突",
                                basis=c.get("permit", "文保施工安排"), party="heritage"))

    # 文保级别禁止的活动做法
    for feat in req.activity_features:
        if feat in PROTECTION_FORBIDDEN_FEATURES.get(space.protection_level, ()):
            findings.append(Finding("HERITAGE_FEATURE_FORBIDDEN", ERROR,
                                    f"{space.protection_level}内禁止{feat}",
                                    basis=f"空间 v{space.version} 文保条件：{space.protection_level}",
                                    party="heritage"))

    # 消防通道：搭台/设备布置不得占用消防通道
    for aisle in req.setup_encroaches_aisles:
        if aisle in space.fire_aisles:
            findings.append(Finding("FIRE_AISLE_BLOCKED", ERROR,
                                    f"搭台布置将占用消防通道 {aisle}，须全程保持畅通",
                                    basis=f"空间 v{space.version} 消防通道清单：{'、'.join(space.fire_aisles)}",
                                    party="fire"))

    # 噪声：扩声活动必须落在噪声窗口内，且不得与居民安静时段相交
    ev_s, ev_e = req.event_span
    if req.amplification:
        if not space.noise_windows:
            findings.append(Finding("NOISE_WINDOW_NONE", ERROR,
                                    f"{space.name} 未公布任何允许扩声的噪声窗口",
                                    basis=f"空间 v{space.version} 噪声窗口", party="jurisdiction"))
        elif not weekly_windows_cover(ev_s, ev_e, list(space.noise_windows)):
            findings.append(Finding("NOISE_OUTSIDE_WINDOW", ERROR,
                                    f"扩声时段 {req.event_start[11:16]}~{req.event_end[11:16]} "
                                    f"未完全包含在公布的噪声窗口内",
                                    basis=f"空间 v{space.version} 噪声窗口", party="jurisdiction"))
        if space.quiet_periods and weekly_windows_overlap(ev_s, ev_e, list(space.quiet_periods)):
            findings.append(Finding("QUIET_PERIOD_OVERLAP", ERROR,
                                    "活动（扩声）与周边居民安静时段冲突",
                                    basis=f"居民约定安静时段（空间 v{space.version}）",
                                    party="jurisdiction"))

    # 设备进出窗口：搭建与拆撤段都须被进出窗口覆盖
    if space.load_in_windows:
        load_s, load_e = parse_dt(req.setup_start), parse_dt(req.event_start)
        out_s, out_e = parse_dt(req.event_end), parse_dt(req.teardown_end)
        if not weekly_windows_cover(load_s, load_e, list(space.load_in_windows)):
            findings.append(Finding("LOADIN_WINDOW", ERROR,
                                    "设备进场时段不在公布的设备进出窗口内",
                                    basis=f"空间 v{space.version} 设备进出窗口", party="jurisdiction"))
        if not weekly_windows_cover(out_s, out_e, list(space.load_in_windows)):
            findings.append(Finding("LOADOUT_WINDOW", ERROR,
                                    "设备撤场时段不在公布的设备进出窗口内",
                                    basis=f"空间 v{space.version} 设备进出窗口", party="jurisdiction"))

    # 无障碍
    if req.accessibility_required and not space.accessible_route:
        findings.append(Finding("ACCESSIBLE_ROUTE_MISSING", ERROR,
                                f"{space.name} 不具备无障碍路线，无法满足申请要求",
                                basis=f"空间 v{space.version} 无障碍信息：{space.accessible_note or '无'}",
                                party="jurisdiction"))

    # 设备与空间的匹配（共用设备的时间独占在 evaluate_conflicts 中判定）
    for eid in req.equipment_ids:
        eq = state.equipment.get(eid)
        if eq is None:
            findings.append(Finding("EQUIPMENT_UNKNOWN", ERROR, f"共用设备 {eid} 未登记",
                                    party="jurisdiction"))
            continue
        if eq.usable_spaces and req.space_id not in eq.usable_spaces:
            findings.append(Finding("EQUIPMENT_SPACE_MISMATCH", ERROR,
                                    f"{eq.name}不可在{space.name}使用",
                                    basis=f"设备 {eq.equipment_id} 适用空间清单", party="jurisdiction"))
        if eq.requires_load_in_window and not space.load_in_windows:
            findings.append(Finding("EQUIPMENT_LOADIN_MISSING", WARNING,
                                    f"{eq.name}只能在设备进出窗口搬运，但{space.name}未公布窗口",
                                    party="jurisdiction"))

    # 居民约定的提示性条款（需要属地意见中明确）
    if space.resident_terms:
        findings.append(Finding("RESIDENT_TERMS_NOTICE", INFO,
                                "须遵守周边居民约定：" + "；".join(space.resident_terms),
                                basis=f"空间 v{space.version} 居民约定", party="jurisdiction"))
    return findings


# ---------------------------------------------------------------------------
# 并发争用：同一空间 / 共用设备的时间独占
# ---------------------------------------------------------------------------

def evaluate_conflicts(state: State, req: BookingRequest,
                       ignore_request_id: str | None = None) -> list[Finding]:
    findings: list[Finding] = []
    span_s, span_e = req.span

    clash_space = [
        b for b in state.bookings_using_space(req.space_id, span_s, span_e)
        if b.request.request_id != ignore_request_id
    ]
    for b in clash_space:
        findings.append(Finding("SPACE_LOCKED", ERROR,
                                f"空间已被《{b.request.title}》（{b.request.request_id}，"
                                f"{'已确认' if b.status == 'CONFIRMED' else '暂占'}）锁定",
                                basis="同一空间时间互斥", party="jurisdiction"))

    for eid in req.equipment_ids:
        eq = state.equipment.get(eid)
        if eq is None or not eq.shared:
            continue
        clash_eq = [
            b for b in state.bookings_using_equipment(eid, span_s, span_e)
            if b.request.request_id != ignore_request_id
        ]
        for b in clash_eq:
            findings.append(Finding("EQUIPMENT_LOCKED", ERROR,
                                    f"共用设备{eq.name}已被《{b.request.title}》"
                                    f"（{b.request.request_id}）占用",
                                    basis=f"共用设备 {eid} 时间独占", party="jurisdiction"))
    return findings


# ---------------------------------------------------------------------------
# 扰动评估与替代方案
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DisruptionImpact:
    request_id: str
    title: str
    ticketed: bool
    findings: tuple[Finding, ...]
    alternatives: tuple[Alternative, ...]
    suggested_actions: tuple[str, ...]   # relocate / reduce_capacity / cancel_refund / proceed_with_caution

    def to_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "title": self.title,
            "ticketed": self.ticketed,
            "findings": [f.to_dict() for f in self.findings],
            "alternatives": [a.to_dict() for a in self.alternatives],
            "suggested_actions": list(self.suggested_actions),
        }


def evaluate_disruption(state: State, disruption: Disruption) -> list[DisruptionImpact]:
    """评估一场扰动对全部时间相交、在档（含暂占）活动的影响与建议。"""
    d_s, d_e = parse_dt(disruption.start), parse_dt(disruption.end)
    impacts: list[DisruptionImpact] = []

    for b in list(state.bookings.values()):
        if b.status not in ("HELD", "CONFIRMED"):
            continue
        b_s, b_e = b.request.span
        if not interval_overlap(b_s, b_e, d_s, d_e):
            continue
        cur_sid = b.effect.get("relocated_to") if b.effect else None
        cur_sid = cur_sid or b.request.space_id
        space = state.current_space(cur_sid, b_s)
        if space is None:
            continue

        findings: list[Finding] = []
        actions: list[str] = []

        if disruption.kind == "organizer_cancel":
            findings.append(Finding("ORGANIZER_CANCEL", ERROR,
                                    "主办方已取消该活动", basis=disruption.description))
            actions.append("cancel")
            if b.request.ticketed:
                actions.append("refund")
            impacts.append(DisruptionImpact(
                b.request.request_id, b.request.title, b.request.ticketed,
                tuple(findings), (), tuple(actions)))
            continue

        space_hit = cur_sid in disruption.space_ids or not disruption.space_ids
        aisle_hit = bool(set(disruption.block_aisles) & set(space.fire_aisles))
        # 居民紧急通行即便未点名通道，也可能压缩可用集散面
        route_hit = disruption.block_accessible

        if disruption.kind == "rainstorm" and space_hit and space.zone in OUTDOOR_ZONES:
            findings.append(Finding("RAINSTORM_OUTDOOR", ERROR,
                                    f"暴雨预警（{disruption.severity}）下{space.zone}活动不具备安全条件",
                                    basis=disruption.description))
            actions.append("relocate")
            if b.request.ticketed:
                actions.append("refund_if_not_relocated")
        elif disruption.kind == "repair" and space_hit:
            findings.append(Finding("REPAIR_CLOSURE", ERROR,
                                    f"临时修缮导致{space.name}不可用：{disruption.description}",
                                    basis=disruption.description))
            actions.append("relocate")
            if b.request.ticketed:
                actions.append("refund_if_not_relocated")
        elif disruption.kind == "resident_access" and (aisle_hit or space_hit):
            # 紧急通行须让行：缩容或调整布置，保留通道
            reduced = _capacity_after_aisle(space, disruption.block_aisles)
            findings.append(Finding("RESIDENT_URGENT_ACCESS", WARNING,
                                    "居民紧急通行需让行，须调整布置并压缩占用面",
                                    basis=disruption.description))
            actions.append("reduce_capacity")
            findings.append(Finding("REDUCED_CAPACITY_HINT", INFO,
                                    f"建议容量上限 {reduced} 人（原容量 {space.capacity} 人）",
                                    party="fire"))
        else:
            findings.append(Finding("DISRUPTION_NEAR_MISS", INFO,
                                    "扰动与活动时间相交，但未命中该活动所用空间/通道，需现场确认",
                                    basis=disruption.description))
            actions.append("proceed_with_caution")

        if aisle_hit and disruption.kind != "resident_access":
            findings.append(Finding("FIRE_AISLE_DISRUPTION", ERROR,
                                    f"扰动阻断消防通道：{'、'.join(sorted(set(disruption.block_aisles) & set(space.fire_aisles)))}",
                                    party="fire"))
            if "relocate" not in actions:
                actions.append("relocate")
        if route_hit and b.request.accessibility_required:
            findings.append(Finding("ACCESSIBLE_ROUTE_BLOCKED", ERROR,
                                    "扰动阻断无障碍路线，须提供替代路线或迁场",
                                    party="jurisdiction"))
            if "relocate" not in actions:
                actions.append("relocate")

        alts: tuple[Alternative, ...] = ()
        if "relocate" in actions:
            alts = tuple(find_alternative_spaces(state, b, (d_s, d_e)))
        impacts.append(DisruptionImpact(
            b.request.request_id, b.request.title, b.request.ticketed,
            tuple(findings), alts, tuple(dict.fromkeys(actions))))
    return impacts


def _capacity_after_aisle(space: SpaceVersion, blocked_aisles: tuple[str, ...]) -> int:
    """通道让行后的经验容量：每让出一条主通道扣减 15%，下限不少于 40%。"""
    hit = len(set(blocked_aisles) & set(space.fire_aisles))
    factor = max(0.4, 1.0 - 0.15 * max(hit, 1))
    return int(space.capacity * factor)


def find_alternative_spaces(state: State, booking: Booking,
                            blocked_window: tuple | None = None) -> list[Alternative]:
    """为受扰动活动枚举可行替代空间，给出每个方案可行的原因与限制。

    只列"客观可行"项：版本有效、容量足够、文保允许其做法、无障碍满足、
    时间不与施工/既存档期/扰动冲突。是否采用仍由负责人决定。
    """
    req = booking.request
    span_s, span_e = req.span
    win = blocked_window or (span_s, span_e)
    current = booking.effect.get("relocated_to") if booking.effect else req.space_id
    options: list[Alternative] = []

    for sid in state.space_ids():
        if sid == current:
            continue
        space = state.current_space(sid, span_s)
        if space is None or space.status != "active":
            continue
        reasons: list[str] = []
        limits: list[str] = []

        if space.capacity < req.expected_attendance:
            continue
        reasons.append(f"容量 {space.capacity} 人 ≥ 预计 {req.expected_attendance} 人")

        forbidden = PROTECTION_FORBIDDEN_FEATURES.get(space.protection_level, ())
        if any(f in forbidden for f in req.activity_features):
            continue
        reasons.append(f"{space.protection_level}条件允许该活动形式")

        if req.accessibility_required:
            if not space.accessible_route:
                continue
            reasons.append("具备无障碍路线")

        if space.construction_active(span_s, span_e):
            continue
        reasons.append("时段内无文保施工")

        if state.bookings_using_space(sid, span_s, span_e):
            continue
        reasons.append("时段内无其他档期")

        disrupted = any(
            sid in d.space_ids and interval_overlap(span_s, span_e, parse_dt(d.start), parse_dt(d.end))
            for d in state.disruptions.values()
        )
        if disrupted:
            continue
        reasons.append("不受当前扰动影响")

        # 设备能否在新空间使用与搬运
        for eid in req.equipment_ids:
            eq = state.equipment.get(eid)
            if eq is None:
                continue
            if eq.usable_spaces and sid not in eq.usable_spaces:
                limits.append(f"{eq.name}不适用于本空间，需另调设备")
            elif eq.requires_load_in_window:
                limits.append("设备搬运须预约新空间的进出窗口")

        if req.amplification:
            if not space.noise_windows:
                continue
            ev_s, ev_e = req.event_span
            if not weekly_windows_cover(ev_s, ev_e, list(space.noise_windows)):
                limits.append("原活动时段不在本空间噪声窗口，需调整演出时间")
            else:
                reasons.append("原时段在噪声窗口内")
        if space.zone in OUTDOOR_ZONES:
            limits.append("仍为室外空间，若天气持续需准备室内二次方案")

        options.append(Alternative(sid, space.name, space.capacity,
                                    tuple(reasons), tuple(limits)))
    return options
