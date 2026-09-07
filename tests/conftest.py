"""Shared Home Assistant test fixtures."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from custom_components.hkte_smart_school.models import (
    AccountSnapshot,
    ChildSnapshot,
    Homework,
    Message,
    Notice,
)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading this custom integration in Home Assistant tests."""
    yield


@pytest.fixture
def snapshot() -> AccountSnapshot:
    """Return representative display-safe provider data."""
    timezone = ZoneInfo("Asia/Hong_Kong")
    return AccountSnapshot(
        account_id="account-1",
        retrieved_at=datetime(2026, 9, 8, 8, 0, tzinfo=timezone),
        children=(
            ChildSnapshot(
                id="child-1",
                name="Test child",
                school="Test school",
                notices=(
                    Notice(
                        id="notice-1",
                        title="School notice",
                        issued_at=datetime(2026, 9, 7, 9, 0, tzinfo=timezone),
                        deadline=date(2026, 9, 15),
                        unread=True,
                        replied=False,
                        content="Private fixture notice body.\nSecond paragraph.",
                    ),
                ),
                messages=(
                    Message(
                        id="message-1",
                        title="School message",
                        created_at=datetime(2026, 9, 7, 10, 0, tzinfo=timezone),
                        unread=True,
                    ),
                ),
                homeworks=(
                    Homework(
                        id="homework-1",
                        title="Reading",
                        subject="English",
                        deadline=date(2026, 9, 20),
                        submitted=False,
                        overdue=False,
                        urgent=True,
                    ),
                    Homework(
                        id="homework-2",
                        title="Old work",
                        subject="Maths",
                        deadline=date(2026, 9, 1),
                        submitted=False,
                        overdue=True,
                        urgent=False,
                    ),
                ),
            ),
        ),
    )
