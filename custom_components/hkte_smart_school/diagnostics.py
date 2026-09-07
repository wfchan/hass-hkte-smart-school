"""Privacy-preserving diagnostics for HKTE Smart School."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from . import HkteConfigEntry
from .const import CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL_MINUTES


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: HkteConfigEntry
) -> dict[str, Any]:
    """Return counts only; never expose provider records or credentials."""
    coordinator = entry.runtime_data.coordinator
    return {
        "update_interval_minutes": entry.options.get(
            CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL_MINUTES
        ),
        "last_update_success": coordinator.last_update_success,
        "retrieved_at": coordinator.data.retrieved_at.isoformat(),
        "child_count": len(coordinator.data.children),
        "record_counts": [
            {
                "notices": len(child.notices),
                "messages": len(child.messages),
                "homeworks": len(child.homeworks),
            }
            for child in coordinator.data.children
        ],
    }
