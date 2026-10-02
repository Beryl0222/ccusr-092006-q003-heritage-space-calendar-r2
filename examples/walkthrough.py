"""端到端走查脚本（虚构数据）：把需求中的关键场景跑一遍并打印。

运行：python -m examples.walkthrough
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src import (
    EventStore, SchedulingService, ReadModel, ConflictError,
    HighRiskOverrideRequired,
)

CST = timezone(timedelta(hours=8))


class Clock:
    """可控时钟，演示按时间顺序推进。"""
    def __init__(self, t: datetime):
        self.t = t

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


def hr(title):
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def main():
    t0 = datetime(2026, 9, 25, 9, 0, tzinfo=CST)
    clock = Clock(t0)
    svc = SchedulingService(EventStore(), clock=clock)
    read = ReadModel(svc.store)

    # 1) 场地分区按版本维护 ------------------------------------------------
    hr("1. 场地办公室发布空间版本（文保条件/噪声窗口/安静时段/消防通道…）")
    svc.define_space({
        "space_id": "yard-03", "name": "三号院", "zone": "院落",
        "protection_level": "市级文物保护单位", "capacity": 120,
        "accessible_route": True, "accessible_note": "东侧无障碍坡道，宽1.5m",
        "fire_aisles": ["东门主通道", "北侧备弄"],
        "noise_windows": [{"days": [4, 5], "start": "18:30", "end": "21:00"}],
        "quiet_periods": [{"days": list(range(7)), "start": "21:30", "end": "08:00"}],
        "load_in_windows": [{"days": [4, 5], "start": "09:00", "end": "22:30"}],
        "construction": [{"name": "东厢木屋架修缮", "reason": "文保本体修缮",
                          "permit": "文物审〔2026〕31号",
                          "start": "2026-10-08T08:00:00+08:00",
                          "end": "2026-10-15T18:00:00+08:00"}],
        "resident_terms": ["21:30 后院落静场", "重型车辆一律走西侧后勤门"],
    })
    svc.define_space({
        "space_id": "plaza-east", "name": "东广场", "zone": "广场",
        "protection_level": "历史建筑", "capacity": 300,
        "accessible_route": True, "accessible_note": "全线平坡",
        "fire_aisles": ["广场中线救援通道"],
        "noise_windows": [{"days": [4, 5], "start": "18:00", "end": "21:30"}],
        "quiet_periods": [{"days": list(range(7)), "start": "22:00", "end": "07:00"}],
        "load_in_windows": [{"days": [4, 5], "start": "08:00", "end": "18:00"}],
        "resident_terms": ["活动结束后 30 分钟内完成清场"],
    })
    svc.define_space({
        "space_id": "hall-01", "name": "一期室内展演厅", "zone": "室内场馆",
        "protection_level": "无", "capacity": 150,
        "accessible_route": True, "accessible_note": "无障碍电梯直达",
        "fire_aisles": ["前厅疏散通道"],
        "noise_windows": [{"days": list(range(7)), "start": "09:00", "end": "21:30"}],
        "load_in_windows": [{"days": list(range(7)), "start": "08:00", "end": "23:00"}],
        "resident_terms": [],
    })
    svc.register_equipment({"equipment_id": "proj-4k-01", "name": "4K户外放映机组",
                            "shared": True, "requires_load_in_window": True,
                            "usable_spaces": ["yard-03", "plaza-east", "hall-01"]})
    print("已发布 3 块空间、1 台共用设备")

    # 2) 线上剧场户外放映：暂占 -------------------------------------------
    hr("2. 线上剧场提交 10/16（周五）三号院户外放映，先暂占")
    screening = {
        "request_id": "rq-screening-1016", "organization": "云上剧场",
        "title": "老街区影像夜·户外放映", "space_id": "yard-03",
        "setup_start": "2026-10-16T13:00:00+08:00",
        "event_start": "2026-10-16T18:30:00+08:00",
        "event_end": "2026-10-16T20:45:00+08:00",
        "teardown_end": "2026-10-16T22:30:00+08:00",
        "expected_attendance": 110, "equipment_ids": ["proj-4k-01"],
        "amplification": True, "ticketed": True, "accessibility_required": True,
    }
    hold = svc.submit_request(screening)
    print(f"暂占成功：{hold.request_id}，有效期至 {hold.expires_at}")

    # 3) 并发申请不能同时锁定同一空间/设备 --------------------------------
    hr("3. 公益音乐会在同一时段申请三号院并使用同一台放映机组（被拒）")
    concert = {
        "request_id": "rq-concert-1016", "organization": "邻里乐团",
        "title": "秋夜公益音乐会", "space_id": "yard-03",
        "setup_start": "2026-10-16T16:00:00+08:00",
        "event_start": "2026-10-16T19:00:00+08:00",
        "event_end": "2026-10-16T20:30:00+08:00",
        "teardown_end": "2026-10-16T21:30:00+08:00",
        "expected_attendance": 90, "equipment_ids": ["proj-4k-01"],
        "amplification": True, "ticketed": False,
    }
    try:
        svc.submit_request(concert)
    except ConflictError as exc:
        for f in exc.findings:
            print(f"  拒绝：{f.message}")

    # 4) 缺意见不能确认 ----------------------------------------------------
    hr("4. 文保、消防意见未齐时尝试转确定档期（被拒）")
    clock.advance(hours=2)
    svc.sign_review("rq-screening-1016", "heritage", "conditional",
                    conditions=["银幕拉索不得接触木构", "雨备方案须提前24小时确认"],
                    basis="文物审〔2026〕31号施工时段避让已核对",
                    signed_by="文保员 王苹")
    try:
        svc.confirm("rq-screening-1016")
    except Exception as exc:
        print(f"  拒绝：{exc}")

    # 5) 三方齐全后确认，留下批准依据快照 ---------------------------------
    hr("5. 消防与属地补齐意见，转为确定档期（依据随确认事件固化）")
    svc.sign_review("rq-screening-1016", "fire", "approved",
                    basis="东门主通道、北侧备弄全程净宽≥1.4m；容量110<120",
                    signed_by="监督员 李澈")
    svc.sign_review("rq-screening-1016", "jurisdiction", "conditional",
                    conditions=["21:30 前静场", "散场引导走东侧"],
                    basis="居民公约 + 噪声窗口周五 18:30-21:00",
                    signed_by="街区办 赵岚")
    svc.confirm("rq-screening-1016")
    print("  已确认，批准依据（空间版本、三方意见、规则复核）已写入 BOOKING_CONFIRMED")

    # 6) 施工期间硬冲突：不可暂占 -----------------------------------------
    hr("6. 非遗体验想落在文保施工高峰 10/15，三号院（硬性拒绝）")
    workshop = {
        "request_id": "rq-workshop-1015", "organization": "非遗传承社",
        "title": "木版拓印亲子体验", "space_id": "yard-03",
        "setup_start": "2026-10-15T13:00:00+08:00",
        "event_start": "2026-10-15T14:00:00+08:00",
        "event_end": "2026-10-15T16:30:00+08:00",
        "teardown_end": "2026-10-15T17:00:00+08:00",
        "expected_attendance": 40, "equipment_ids": [],
        "amplification": False, "ticketed": False,
    }
    try:
        svc.submit_request(workshop)
    except Exception as exc:
        print(f"  拒绝：{exc}")

    # 7) 暴雨扰动：系统只建议，不自动批 -----------------------------------
    hr("7. 10/16 16:00 发布暴雨橙色预警：系统给出迁场/退款建议与受影响对象")
    clock.t = datetime(2026, 10, 16, 16, 0, tzinfo=CST)
    result = svc.declare_disruption({
        "kind": "rainstorm", "severity": "high",
        "start": "2026-10-16T17:00:00+08:00",
        "end": "2026-10-16T23:00:00+08:00",
        "space_ids": ["yard-03", "plaza-east"],
        "description": "市气象台暴雨橙色预警，户外场地关停",
    })
    for impact in result["impacts"]:
        print(f"  受影响：《{impact.title}》({impact.request_id}) "
              f"建议动作={list(impact.suggested_actions)}")
        for alt in impact.alternatives:
            print(f"    可替代：{alt.space_name}（{alt.space_id}）——" + "；".join(alt.reasons))
            for lim in alt.limitations:
                print(f"      限制：{lim}")

    # 8) 负责人想"顶雨照常"——高风险例外不会被系统自动批准 ----------------
    hr("8. 有人主张照常举行：高风险例外必须负责人显式签收（首次被拦下）")
    try:
        svc.manual_decision(
            "rq-screening-1016", "proceed", decided_by="值班员 小陈",
            justification="观众已到场", recommendation_id=result["recommendation_id"])
    except HighRiskOverrideRequired as exc:
        print(f"  系统拦下：{exc}")

    hr("9. 负责人改采迁场至室内展演厅（系统复核新空间可行后记录人工决定）")
    decision_id = svc.manual_decision(
        "rq-screening-1016", "relocate", decided_by="街区办 赵岚",
        justification="室内厅 150 座可容纳 110 名已售票观众，设备窗口可进场",
        recommendation_id=result["recommendation_id"],
        new_space_id="hall-01")
    print(f"  人工迁场已记录：{decision_id}（系统未自行决定，仅复核与留痕）")

    # 9) 查询当天每块空间为什么可用/不可用 --------------------------------
    hr("10. 查询 2026-10-16 每块空间的逐时段可用性与依据")
    for row in read.day_availability("2026-10-16"):
        print(f"\n● {row['headline']}")
        for seg in row["segments"]:
            label = {"AVAILABLE": "可用", "HELD": "暂占",
                     "OCCUPIED": "确定档期", "BLOCKED": "不可用"}[seg["state"]]
            print(f"  {seg['start'][11:16]}–{seg['end'][11:16]} {label}")
            for r in seg["reasons"]:
                print(f"      · {r['text']}（依据：{r['basis']}）")

    # 10) 投诉反查 ---------------------------------------------------------
    hr("11. 居民投诉散场喧哗：从投诉反查批准依据、现场调整")
    cid = svc.record_complaint("rq-screening-1016",
                               "三号院邻居反映 21 点后仍有喧哗与设备搬运声")
    trace = read.complaint_trace(cid)
    print("  批准依据快照中的三方意见：")
    for party, r in trace["approval_basis"]["reviews"].items():
        print(f"    {party}: {r['decision']} / 条件 {r['conditions']} / 签署 {r['signed_by']}")
    print("  系统建议：", [s["suggested_actions"] for s in trace["system_recommendations"]])
    for adj in trace["on_site_adjustments"]:
        print(f"  现场调整：{adj['action']} -> {adj.get('new_space_id')}，"
              f"决定人 {adj['decided_by']}，理由：{adj['justification']}")

    hr("12. 处置投诉并记录后续恢复")
    svc.resolve_complaint(cid, conclusion="迁场后实际散场点为室内厅，噪音为西侧后勤门"
                                          "设备卸车所致；已通报后续活动 21:00 后只走室内通道",
                          followups=["更新 hall-01 设备撤场动线提示", "周五夜场加派一名引导员"],
                          resolved_by="街区办 赵岚")
    clock.t = datetime(2026, 10, 17, 10, 0, tzinfo=CST)
    svc.restore_booking("rq-screening-1016", note="暴雨预警解除，迁场为单次措施，后续场次按原空间执行")
    trace = read.complaint_trace(cid)
    print("  恢复记录：", trace["restoration"])
    print("  投诉结论：", trace["complaint"]["resolution"]["conclusion"])

    hr("事件流（全部事实，可用于审计/重放）")
    for e in svc.store.read_all():
        print(f"  #{e.seq:>2} {e.occurred_at} {e.kind:<22} {e.subject_id}")


if __name__ == "__main__":
    main()
