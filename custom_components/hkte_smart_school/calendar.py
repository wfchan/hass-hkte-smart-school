"""Read-only homework and notice deadline calendars."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import HkteConfigEntry
from .coordinator import HkteDataUpdateCoordinator
from .entity import HkteChildEntity
from .models import ChildSnapshot, Homework, Notice

_HK_TIMEZONE = ZoneInfo("Asia/Hong_Kong")


@dataclass(frozen=True, slots=True)
class CalendarDefinition:
    """Describe one child calendar."""

    key: str
    translation_key: str
    records_fn: Callable[[ChildSnapshot], Iterable[Homework | Notice]]


CALENDAR_DEFINITIONS = (
    CalendarDefinition(
        key="homework_deadlines",
        translation_key="homework_deadlines",
        records_fn=lambda child: child.homeworks,
    ),
    CalendarDefinition(
        key="notice_deadlines",
        translation_key="notice_deadlines",
        records_fn=lambda child: child.notices,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HkteConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up calendars and add children discovered later."""
    coordinator = entry.runtime_data.coordinator
    known_children: set[str] = set()

    @callback
    def async_add_new_children() -> None:
        new_children = [
            child for child in coordinator.data.children if child.id not in known_children
        ]
        if not new_children:
            return
        known_children.update(child.id for child in new_children)
        async_add_entities(
            HkteCalendar(coordinator, child, definition)
            for child in new_children
            for definition in CALENDAR_DEFINITIONS
        )

    async_add_new_children()
    entry.async_on_unload(coordinator.async_add_listener(async_add_new_children))


class HkteCalendar(HkteChildEntity, CalendarEntity):
    """A read-only calendar derived from one collection."""

    def __init__(
        self,
        coordinator: HkteDataUpdateCoordinator,
        child: ChildSnapshot,
        definition: CalendarDefinition,
    ) -> None:
        super().__init__(coordinator, child, definition.key)
        self._definition = definition
        self._attr_translation_key = definition.translation_key

    @property
    def event(self) -> CalendarEvent | None:
        """Return the next current or future event."""
        now = datetime.now(tz=_HK_TIMEZONE)
        events = sorted(self._events(), key=_event_start_datetime)
        return next((event for event in events if _event_end_datetime(event) >= now), None)

    async def async_get_events(
        self,
        hass: HomeAssistant,
        start_date: datetime,
        end_date: datetime,
    ) -> list[CalendarEvent]:
        """Return events intersecting the requested time window."""
        return [
            event
            for event in self._events()
            if _event_start_datetime(event) < end_date and _event_end_datetime(event) > start_date
        ]

    def _events(self) -> list[CalendarEvent]:
        child = self.child
        if child is None:
            return []
        events: list[CalendarEvent] = []
        for record in self._definition.records_fn(child):
            if record.deadline is None:
                continue
            description = _calendar_description(record)
            if isinstance(record.deadline, datetime):
                events.append(
                    CalendarEvent(
                        start=record.deadline,
                        end=record.deadline + timedelta(hours=1),
                        summary=_calendar_summary(record),
                        description=description,
                    )
                )
            else:
                events.append(
                    CalendarEvent(
                        start=record.deadline,
                        end=record.deadline + timedelta(days=1),
                        summary=_calendar_summary(record),
                        description=description,
                    )
                )
        return events


def _calendar_summary(record: Homework | Notice) -> str:
    if isinstance(record, Homework) and record.subject:
        return f"{record.subject}: {record.title}"
    return record.title


def _calendar_description(record: Homework | Notice) -> str:
    states: list[str] = []
    if isinstance(record, Homework):
        if record.submitted is True:
            states.append("submitted")
        if record.overdue is True:
            states.append("overdue")
        if record.urgent is True:
            states.append("urgent")
    else:
        if record.unread is True:
            states.append("unread")
        if record.replied is False:
            states.append("unreplied")
    return ", ".join(states)


def _event_start_datetime(event: CalendarEvent) -> datetime:
    return _date_to_datetime(event.start)


def _event_end_datetime(event: CalendarEvent) -> datetime:
    return _date_to_datetime(event.end)


def _date_to_datetime(value: date | datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.combine(value, time.min, tzinfo=_HK_TIMEZONE)
