"""Tests for stable snapshot lookup."""

from datetime import UTC, datetime

from custom_components.hkte_smart_school.models import AccountSnapshot, ChildSnapshot


def test_snapshot_child_lookup():
    child = ChildSnapshot(
        id="child-1",
        name="Child",
        school="School",
        notices=(),
        messages=(),
        homeworks=(),
    )
    snapshot = AccountSnapshot(
        account_id="account",
        retrieved_at=datetime.now(tz=UTC),
        children=(child,),
    )
    assert snapshot.child("child-1") is child
    assert snapshot.child("missing") is None
