"""Data coordinator and persistent new-item tracking."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    HkteClient,
    HkteConnectionError,
    HkteInvalidAuthError,
    HkteResponseError,
)
from .const import DOMAIN, SEEN_IDS_PER_KIND
from .models import AccountSnapshot, ChildSnapshot, NewItemEvent

_LOGGER = logging.getLogger(__name__)
_STORE_VERSION = 1

type SeenMap = dict[str, list[str]]


class HkteDataUpdateCoordinator(DataUpdateCoordinator[AccountSnapshot]):
    """Coordinate snapshots while keeping provider details out of entities."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: HkteClient,
        update_interval: timedelta,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=update_interval,
            always_update=True,
        )
        self._client = client
        self.entry_id = entry.entry_id
        self._store = Store[SeenMap](
            hass,
            _STORE_VERSION,
            f"{DOMAIN}.{entry.entry_id}.seen",
            private=True,
            atomic_writes=True,
        )
        self._seen: SeenMap = {}
        self._has_persisted_baseline = False
        self.new_items: tuple[NewItemEvent, ...] = ()

    async def async_initialize(self) -> None:
        """Load event-deduplication state before the first refresh."""
        stored = await self._store.async_load()
        if not isinstance(stored, dict):
            return
        self._seen = {
            str(key): [str(value) for value in values if isinstance(value, str)]
            for key, values in stored.items()
            if isinstance(values, list)
        }
        self._has_persisted_baseline = True

    async def _async_update_data(self) -> AccountSnapshot:
        try:
            snapshot = await self._client.async_fetch()
        except HkteInvalidAuthError as err:
            raise ConfigEntryAuthFailed from err
        except (HkteConnectionError, HkteResponseError) as err:
            raise UpdateFailed("Unable to update HKTE Smart School") from err

        self.new_items = self._find_new_items(snapshot)
        await self._store.async_save(self._seen)
        return snapshot

    def _find_new_items(self, snapshot: AccountSnapshot) -> tuple[NewItemEvent, ...]:
        events: list[NewItemEvent] = []
        updated_seen: SeenMap = {}

        for child in snapshot.children:
            for kind, records in _records_by_kind(child).items():
                key = f"{child.id}:{kind}"
                unique_records = {record.id: record for record in records}
                current_ids = list(unique_records)[:SEEN_IDS_PER_KIND]
                previous_ids = set(self._seen.get(key, []))
                if self._has_persisted_baseline and key in self._seen:
                    events.extend(
                        _new_item_event(child.id, kind, record)
                        for record in reversed(list(unique_records.values())[:SEEN_IDS_PER_KIND])
                        if record.id not in previous_ids
                    )
                updated_seen[key] = list(dict.fromkeys([*current_ids, *self._seen.get(key, [])]))[
                    :SEEN_IDS_PER_KIND
                ]

        self._seen = updated_seen
        self._has_persisted_baseline = True
        return tuple(events)


def _records_by_kind(child: ChildSnapshot) -> dict[str, tuple[Any, ...]]:
    return {
        "notice": child.notices,
        "message": child.messages,
        "homework": child.homeworks,
    }


def _new_item_event(child_id: str, kind: str, record: Any) -> NewItemEvent:
    occurred_at = getattr(record, "issued_at", None) or getattr(record, "created_at", None)
    return NewItemEvent(
        child_id=child_id,
        kind=kind,
        item_id=record.id,
        title=record.title,
        occurred_at=occurred_at,
        deadline=getattr(record, "deadline", None),
    )
