"""Summary sensors for HKTE Smart School."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import HkteConfigEntry
from .coordinator import HkteDataUpdateCoordinator
from .entity import HkteChildEntity
from .models import ChildSnapshot

_HK_TIMEZONE = ZoneInfo("Asia/Hong_Kong")


@dataclass(frozen=True, kw_only=True)
class HkteSensorDescription(SensorEntityDescription):
    """Describe an HKTE summary sensor."""

    value_fn: Callable[[ChildSnapshot], Any]


SENSOR_DESCRIPTIONS = (
    HkteSensorDescription(
        key="unread_notices",
        translation_key="unread_notices",
        icon="mdi:email-alert-outline",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="notices",
        value_fn=lambda child: sum(item.unread is True for item in child.notices),
    ),
    HkteSensorDescription(
        key="unreplied_notices",
        translation_key="unreplied_notices",
        icon="mdi:email-edit-outline",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="notices",
        value_fn=lambda child: sum(item.replied is False for item in child.notices),
    ),
    HkteSensorDescription(
        key="unread_messages",
        translation_key="unread_messages",
        icon="mdi:message-badge-outline",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="messages",
        value_fn=lambda child: sum(item.unread is True for item in child.messages),
    ),
    HkteSensorDescription(
        key="unsubmitted_homeworks",
        translation_key="unsubmitted_homeworks",
        icon="mdi:book-open-variant-outline",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="homeworks",
        value_fn=lambda child: sum(item.submitted is False for item in child.homeworks),
    ),
    HkteSensorDescription(
        key="overdue_homeworks",
        translation_key="overdue_homeworks",
        icon="mdi:calendar-alert",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="homeworks",
        value_fn=lambda child: sum(item.overdue is True for item in child.homeworks),
    ),
    HkteSensorDescription(
        key="urgent_homeworks",
        translation_key="urgent_homeworks",
        icon="mdi:alert-circle-outline",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="homeworks",
        value_fn=lambda child: sum(item.urgent is True for item in child.homeworks),
    ),
    HkteSensorDescription(
        key="next_homework_deadline",
        translation_key="next_homework_deadline",
        icon="mdi:calendar-clock-outline",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda child: _next_homework_deadline(child),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HkteConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up sensors and add children discovered later."""
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
            HkteSensor(coordinator, child, description)
            for child in new_children
            for description in SENSOR_DESCRIPTIONS
        )

    async_add_new_children()
    entry.async_on_unload(coordinator.async_add_listener(async_add_new_children))


class HkteSensor(HkteChildEntity, SensorEntity):
    """A calculated sensor from one child snapshot."""

    entity_description: HkteSensorDescription

    def __init__(
        self,
        coordinator: HkteDataUpdateCoordinator,
        child: ChildSnapshot,
        description: HkteSensorDescription,
    ) -> None:
        super().__init__(coordinator, child, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        """Return the calculated sensor value."""
        child = self.child
        return self.entity_description.value_fn(child) if child else None


def _next_homework_deadline(child: ChildSnapshot) -> datetime | None:
    now = datetime.now(tz=_HK_TIMEZONE)
    deadlines = [
        _as_datetime(item.deadline)
        for item in child.homeworks
        if item.submitted is not True and item.deadline is not None
    ]
    return min((deadline for deadline in deadlines if deadline >= now), default=None)


def _as_datetime(value: date | datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.combine(value, time.max, tzinfo=_HK_TIMEZONE)
