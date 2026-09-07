"""Shared entity model for HKTE child entities."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import HkteDataUpdateCoordinator
from .models import ChildSnapshot


class HkteChildEntity(CoordinatorEntity[HkteDataUpdateCoordinator]):
    """Base an entity on a single normalized child snapshot."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: HkteDataUpdateCoordinator,
        child: ChildSnapshot,
        suffix: str,
    ) -> None:
        super().__init__(coordinator)
        self._child_id = child.id
        self._attr_unique_id = f"{coordinator.data.account_id}_{child.id}_{suffix}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{coordinator.data.account_id}:{child.id}")},
            name=child.name,
            manufacturer="HKTE",
            model="Smart School child account",
            configuration_url="https://cls.hkteducation.com",
        )

    @property
    def child(self) -> ChildSnapshot | None:
        """Return current child data."""
        return self.coordinator.data.child(self._child_id)

    @property
    def available(self) -> bool:
        """Report unavailable if either the update or child lookup failed."""
        return super().available and self.child is not None
