"""来源状态机（见《项目介绍.md》5.2）。"""

import pytest

from app.domain.state import (
    ALLOWED_TRANSITIONS,
    InvalidStatusTransition,
    SourceStatus,
    allows_direct_delete,
    can_transition,
    ensure_transition,
)


def test_documented_transitions_are_allowed() -> None:
    assert can_transition(SourceStatus.PENDING, SourceStatus.PROCESSING)
    assert can_transition(SourceStatus.PROCESSING, SourceStatus.COMPLETED)
    assert can_transition(SourceStatus.PROCESSING, SourceStatus.FAILED)
    assert can_transition(SourceStatus.FAILED, SourceStatus.PROCESSING)
    assert can_transition(SourceStatus.COMPLETED, SourceStatus.DELETING)
    assert can_transition(SourceStatus.DELETING, SourceStatus.DELETING)


def test_undocumented_transitions_are_rejected() -> None:
    assert not can_transition(SourceStatus.COMPLETED, SourceStatus.PROCESSING)
    assert not can_transition(SourceStatus.PENDING, SourceStatus.COMPLETED)
    assert not can_transition(SourceStatus.DELETING, SourceStatus.COMPLETED)


def test_ensure_transition_raises_with_context() -> None:
    with pytest.raises(InvalidStatusTransition) as excinfo:
        ensure_transition(SourceStatus.COMPLETED, SourceStatus.PROCESSING)

    message = str(excinfo.value)
    assert "completed" in message
    assert "processing" in message


def test_every_status_has_a_transition_entry() -> None:
    assert set(ALLOWED_TRANSITIONS) == set(SourceStatus)


def test_only_pending_allows_direct_record_delete() -> None:
    assert allows_direct_delete(SourceStatus.PENDING)
    assert not allows_direct_delete(SourceStatus.PROCESSING)
    assert not allows_direct_delete(SourceStatus.COMPLETED)
    assert not allows_direct_delete(SourceStatus.FAILED)
    assert not allows_direct_delete(SourceStatus.DELETING)
