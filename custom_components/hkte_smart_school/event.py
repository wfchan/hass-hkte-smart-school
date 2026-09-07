"""New-item events for HKTE Smart School."""

from __future__ import annotations

from datetime import date, datetime

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import HkteConfigEntry
from .coordinator import HkteDataUpdateCoordinator
from .entity import HkteChildEntity
from .models import ChildSnapshot, NewItemEvent

EVENT_TYPES = ["notice", "message", "homework"]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HkteConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up event entities and add children discovered later."""
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
        async_add_entities(HkteNewItemEvent(coordinator, child) for child in new_children)

    async_add_new_children()
    entry.async_on_unload(coordinator.async_add_listener(async_add_new_children))


class HkteNewItemEvent(HkteChildEntity, EventEntity):
    """Emit one event for every newly observed provider item."""

    _attr_event_types = EVENT_TYPES
    _attr_translation_key = "new_item"

    def __init__(self, coordinator: HkteDataUpdateCoordinator, child: ChildSnapshot) -> None:
        super().__init__(coordinator, child, "new_item")
        self._last_generation: datetime | None = None

    async def async_added_to_hass(self) -> None:
        """Attach to coordinator and emit items found while HA was offline."""
        await super().async_added_to_hass()
        self._emit_new_items()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Emit current refresh events before writing normal state."""
        self._emit_new_items()
        super()._handle_coordinator_update()

    @callback
    def _emit_new_items(self) -> None:
        generation = self.coordinator.data.retrieved_at
        if generation == self._last_generation:
            return
        self._last_generation = generation
        for item in self.coordinator.new_items:
            if item.child_id != self._child_id:
                continue
            self._trigger_event(item.kind, _event_attributes(item))
            self.async_write_ha_state()


def _event_attributes(item: NewItemEvent) -> dict[str, str]:
    attributes = {
        "child_id": item.child_id,
        "item_id": item.item_id,
        "title": item.title,
    }
    if item.occurred_at is not None:
        attributes["occurred_at"] = item.occurred_at.isoformat()
    if item.deadline is not None:
        attributes["deadline"] = _date_to_iso(item.deadline)
    return attributes


def _date_to_iso(value: date | datetime) -> str:
    return value.isoformat()
