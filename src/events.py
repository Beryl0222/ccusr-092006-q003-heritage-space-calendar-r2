"""事件日志：排期服务的唯一事实来源。

所有状态变更（空间版本发布、暂占、意见签署、确认、扰动与人工决定）
都以不可变事件的形式追加到日志；当前状态由回放得到。这样"投诉反查
当时批准依据、现场调整与后续恢复"就是一次针对活动流的时间线读取。

并发安全通过对事件流的 compare-and-append 实现：两个并发申请必须基于
同一个版本号竞写，后到者收到 :class:`ConcurrentAppendError`，由上层重试
或告知申请人——两个申请不会同时锁定同一空间或共用设备。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

# ---------------------------------------------------------------------------
# 事件种类：领域内全部"已经发生的事实"。新增事实时在此登记。
# ---------------------------------------------------------------------------
EVENT_KINDS = (
    "SPACE_VERSIONED",        # 一块公共空间发布新版本（分区/文保/容量/无障碍…）
    "EQUIPMENT_REGISTERED",   # 共用设备登记（投影、音响、舞台…）
    "HOLD_CREATED",           # 文化机构提交活动并暂占
    "REVIEW_SIGNED",          # 文保 / 消防 / 属地管理 一方意见签署
    "BOOKING_CONFIRMED",      # 三方意见齐全且复核通过，转为确定档期
    "HOLD_RELEASED",          # 暂占主动撤回 / 过期释放
    "DISRUPTION_DECLARED",    # 暴雨、临时修缮、居民紧急通行、已售活动取消
    "RECOMMENDATION_LOGGED",  # 系统给出的迁场/缩容/退款建议（仅建议）
    "MANUAL_DECISION",        # 负责人对建议的人工决定（系统不自动批准例外）
    "BOOKING_RESTORED",       # 扰动解除，档期恢复
    "COMPLAINT_RECORDED",     # 登记一条投诉
    "COMPLAINT_RESOLVED",     # 投诉处置结论
)

REQUIRED_FIELDS = ("event_id", "kind", "occurred_at", "subject_id", "payload")


class ConcurrentAppendError(RuntimeError):
    """追加时事件流版本已变化，乐观锁失败（并发申请冲突）。"""


def validate_event(record: dict) -> list[str]:
    """检查事件是否具备可交换的最小字段，返回问题字段名列表。"""
    problems = [name for name in REQUIRED_FIELDS if name not in record]
    if record.get("kind") not in EVENT_KINDS:
        problems.append("kind")
    return problems


@dataclass(frozen=True)
class Event:
    """一条不可变事件。seq 为追加成功后由日志分配的流水号。"""

    event_id: str
    kind: str
    occurred_at: str
    subject_id: str
    payload: dict
    seq: int = -1

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "kind": self.kind,
            "occurred_at": self.occurred_at,
            "subject_id": self.subject_id,
            "payload": self.payload,
            "seq": self.seq,
        }


class EventStore:
    """内存事件日志，进程内线程安全，可选 JSONL 持久化。

    对持久化文件的写入采用"先全量写临时文件再原子替换 + fsync"的方式，
    避免演示脚本中途崩溃留下半截日志。
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self._events: list[Event] = EventStore._load(path) if path else []
        self._path = Path(path) if path else None
        self._lock = threading.RLock()

    @staticmethod
    def _load(path: str | Path) -> list[Event]:
        p = Path(path)
        if not p.exists():
            return []
        events: list[Event] = []
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            raw = json.loads(line)
            events.append(
                Event(
                    event_id=raw["event_id"],
                    kind=raw["kind"],
                    occurred_at=raw["occurred_at"],
                    subject_id=raw["subject_id"],
                    payload=raw.get("payload", {}),
                    seq=raw.get("seq", -1),
                )
            )
        for i, ev in enumerate(events):
            if ev.seq != i:
                object.__setattr__(ev, "seq", i)
        return events

    # ---- 读取 -----------------------------------------------------------
    @property
    def version(self) -> int:
        """当前事件流版本（= 事件条数）。作为乐观锁的期望值。"""
        with self._lock:
            return len(self._events)

    def read_all(self) -> list[Event]:
        with self._lock:
            return list(self._events)

    def read_stream(self, subject_id: str) -> list[Event]:
        """读取某一主体（如一个活动申请）的完整事件时间线。"""
        with self._lock:
            return [e for e in self._events if e.subject_id == subject_id]

    def scan(self, predicate: Callable[[Event], bool]) -> list[Event]:
        with self._lock:
            return [e for e in self._events if predicate(e)]

    # ---- 写入 -----------------------------------------------------------
    def append(
        self,
        event: Event,
        *,
        expected_version: int | None = None,
        also_subjects: Iterable[str] = (),
    ) -> Event:
        """追加事件。

        expected_version 给定时执行 compare-and-append：事件流版本不符则
        抛 :class:`ConcurrentAppendError`。also_subjects 用于一次决定牵涉
        多个活动（如一场扰动影响多场已售活动）时把事件同步挂到这些时间线，
        方法是在 payload 中写入 ``_also_subjects``（不影响领域读取）。
        """
        with self._lock:
            if expected_version is not None and len(self._events) != expected_version:
                raise ConcurrentAppendError(
                    f"事件流版本 {len(self._events)} 与期望 {expected_version} 不一致"
                )
            if any(e.event_id == event.event_id for e in self._events):
                raise ValueError(f"事件 ID 重复: {event.event_id}")
            stored = Event(
                event_id=event.event_id,
                kind=event.kind,
                occurred_at=event.occurred_at,
                subject_id=event.subject_id,
                payload={**event.payload, "_also_subjects": list(also_subjects)},
                seq=len(self._events),
            )
            self._events.append(stored)
            if self._path is not None:
                self._persist()
            return stored

    def _persist(self) -> None:
        assert self._path is not None
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for ev in self._events:
                fh.write(json.dumps(ev.to_dict(), ensure_ascii=False) + "\n")
            fh.flush()
        tmp.replace(self._path)

    # ---- 回放 -----------------------------------------------------------
    def fold(self, initial, reducer: Callable[[object, Event], object]):
        """从空状态回放全部事件（事件溯源的标准入口）。"""
        state = initial
        for ev in self.read_all():
            state = reducer(state, ev)
        return state
