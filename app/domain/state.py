"""来源状态机（见《项目开发说明_v1.md》5.2）。"""

from enum import StrEnum


class SourceStatus(StrEnum):
    """knowledge_sources.status 的全部取值。"""

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    DELETING = "deleting"


ALLOWED_TRANSITIONS: dict[SourceStatus, frozenset[SourceStatus]] = {
    SourceStatus.PENDING: frozenset({SourceStatus.PROCESSING, SourceStatus.FAILED}),
    SourceStatus.PROCESSING: frozenset({SourceStatus.COMPLETED, SourceStatus.FAILED}),
    SourceStatus.COMPLETED: frozenset({SourceStatus.DELETING}),
    SourceStatus.FAILED: frozenset({SourceStatus.PROCESSING}),
    SourceStatus.DELETING: frozenset({SourceStatus.DELETING}),
}

DIRECT_DELETE_ALLOWED = frozenset({SourceStatus.PENDING})


class InvalidStatusTransition(ValueError):
    """状态迁移不在允许集合内。"""


def can_transition(current: SourceStatus, target: SourceStatus) -> bool:
    return target in ALLOWED_TRANSITIONS[current]


def ensure_transition(current: SourceStatus, target: SourceStatus) -> None:
    if not can_transition(current, target):
        raise InvalidStatusTransition(
            f"非法状态迁移: {current.value} -> {target.value}"
        )


def allows_direct_delete(status: SourceStatus) -> bool:
    """pending 记录无派生索引，允许直接删除行。"""

    return status in DIRECT_DELETE_ALLOWED
