"""Typed data model for normalized HKTE Smart School records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

DateValue = date | datetime


@dataclass(frozen=True, slots=True)
class AccountIdentity:
    """Stable, non-display identity returned after authentication."""

    account_id: str


@dataclass(frozen=True, slots=True)
class Notice:
    """A normalized read-only notice."""

    id: str
    title: str
    issued_at: datetime | None
    deadline: DateValue | None
    unread: bool | None
    replied: bool | None


@dataclass(frozen=True, slots=True)
class Message:
    """A normalized message-centre prompt."""

    id: str
    title: str
    created_at: datetime | None
    unread: bool | None


@dataclass(frozen=True, slots=True)
class Homework:
    """A normalized homework item."""

    id: str
    title: str
    subject: str
    deadline: DateValue | None
    submitted: bool | None
    overdue: bool | None
    urgent: bool | None


@dataclass(frozen=True, slots=True)
class ChildSnapshot:
    """All display-safe records belonging to one child."""

    id: str
    name: str
    school: str
    notices: tuple[Notice, ...]
    messages: tuple[Message, ...]
    homeworks: tuple[Homework, ...]


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    """Latest normalized account snapshot."""

    account_id: str
    retrieved_at: datetime
    children: tuple[ChildSnapshot, ...]

    def child(self, child_id: str) -> ChildSnapshot | None:
        """Return a child by provider identifier."""
        return next((child for child in self.children if child.id == child_id), None)


@dataclass(frozen=True, slots=True)
class NewItemEvent:
    """A new item safe to expose through a Home Assistant event entity."""

    child_id: str
    kind: str
    item_id: str
    title: str
    occurred_at: datetime | None
    deadline: DateValue | None
