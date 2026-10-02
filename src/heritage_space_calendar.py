"""向后兼容的领域资料入口；实现已拆分到 src 包各模块。"""

from .events import EVENT_KINDS, REQUIRED_FIELDS, validate_event

__all__ = ["EVENT_KINDS", "REQUIRED_FIELDS", "validate_event"]
