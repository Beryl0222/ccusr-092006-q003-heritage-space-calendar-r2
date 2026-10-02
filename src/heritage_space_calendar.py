"""事件信封的最小字段校验（对外交换契约）。

领域事件种类以 :class:`src.models.EventKind` 为准；这里保留早期资料模块的
``validate_event`` 入口，供外部核对 JSON 事件信封使用。
"""

from __future__ import annotations

from .models import EventKind

EVENT_KINDS = [kind.value for kind in EventKind]
REQUIRED_FIELDS = ("event_id", "kind", "occurred_at", "subject_id", "payload")


def validate_event(record: dict) -> list[str]:
    """检查事件是否具备可交换的最小字段且种类已知。"""
    problems = [name for name in REQUIRED_FIELDS if name not in record]
    if record.get("kind") not in EVENT_KINDS:
        problems.append("kind")
    return problems
