# 历史街区公共文化空间排期

把有限的院落、广场、街巷集合点与室内空间排给公益音乐会、非遗体验、城市漫游
集合、线上剧场户外放映等活动，并在文保施工、居民安静时段、消防通道与天气
扰动之间留痕、给依据的一项公共文化空间排期服务。全部资料与数据均为虚构，
不含真实个人信息、生产连接或外部账号。

## 设计要点

- **事实只增不改（事件溯源）**：空间版本、暂占、三方意见、确认、扰动、系统
  建议、人工决定、恢复、投诉全部是不可变事件（`src/events.py`），当前状态由
  回放得到（`src/model.py`）。
- **场地条件按版本维护**：分区、文保级别与施工、噪声窗口、居民安静时段、
  容量、无障碍路线、消防通道、设备进出窗口、居民约定都在 `SPACE_VERSIONED`
  中版本化；暂占期间发布新版本，确认时按新版本重新复核。
- **暂占即加锁，但确认需三方意见**：文化机构提交活动即暂占空间与共用设备；
  只有文保、消防、属地管理三方意见齐全且无硬性违规才能转为确定档期
  （`src/service.py`）。
- **并发不双锁**：事件流 compare-and-append 乐观锁 + 规则复核，两个并发申请
  不会同时锁定同一空间或共用设备（见 `tests/test_events.py` 的 8 线程竞态）。
- **扰动只建议、不代批**：暴雨、临时修缮、居民紧急通行、已售活动取消会生成
  迁场 / 缩容 / 退款建议、可行替代空间与受影响对象清单（`src/compliance.py`）；
  高风险例外（如顶雨照常、迁到仍被扰动覆盖的空间）必须由负责人显式签收风险，
  系统只复核与留痕，从不自动批准。
- **可解释、可追溯**：`src/queries.py` 支持逐时段查询某一天每块空间"为什么
  可用 / 不可用"（每条结论附依据），以及从一次投诉反查批准依据快照、系统
  当时建议、负责人现场调整与后续恢复的完整时间线。

## 目录

- `src/events.py`：事件种类、不可变事件、线程安全事件日志（含 JSONL 持久化）。
- `src/model.py`：空间版本、共用设备、活动申请、意见、扰动等模型与事件回放。
- `src/compliance.py`：只读合规规则（容量/文保/消防/噪声/安静时段/无障碍/
  设备进出）、争用判定、扰动影响与替代空间枚举。
- `src/service.py`：排期服务——暂占、意见签署、确认、扰动登记、人工决定、恢复。
- `src/queries.py`：日可用性解释与投诉反查读模型。
- `examples/walkthrough.py`：端到端虚构场景走查。
- `data/sample.json`：符合事件契约的虚构样例。
- `tests/`：45 个用例，覆盖生命周期、规则、并发竞态、扰动应对、查询与持久化。

## 本地核对

```bash
python3 -m unittest discover -s tests
# 或
python3 -m pytest tests/

# 端到端走查（暂占→三方确认→并发被拒→暴雨建议→人工迁场→日可用性→投诉反查→恢复）
python3 -m examples.walkthrough
```

## 事件种类

`SPACE_VERSIONED` · `EQUIPMENT_REGISTERED` · `HOLD_CREATED` · `REVIEW_SIGNED` ·
`BOOKING_CONFIRMED` · `HOLD_RELEASED` · `DISRUPTION_DECLARED` ·
`RECOMMENDATION_LOGGED` · `MANUAL_DECISION` · `BOOKING_RESTORED` ·
`COMPLAINT_RECORDED` · `COMPLAINT_RESOLVED`
