"""HKTE Smart School integration setup."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import HkteClient
from .const import (
    CONF_LOGIN_NAME,
    CONF_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL_MINUTES,
    PLATFORMS,
)
from .coordinator import HkteDataUpdateCoordinator


@dataclass(slots=True)
class HkteRuntimeData:
    """Runtime data attached to a config entry."""

    coordinator: HkteDataUpdateCoordinator
    client: HkteClient


type HkteConfigEntry = ConfigEntry[HkteRuntimeData]


async def async_setup_entry(hass: HomeAssistant, entry: HkteConfigEntry) -> bool:
    """Set up HKTE Smart School from a config entry."""
    client = HkteClient(
        async_create_clientsession(hass),
        entry.data[CONF_LOGIN_NAME],
        entry.data[CONF_PASSWORD],
    )
    interval_minutes = entry.options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL_MINUTES)
    coordinator = HkteDataUpdateCoordinator(
        hass,
        entry,
        client,
        timedelta(minutes=interval_minutes),
    )
    await coordinator.async_initialize()
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = HkteRuntimeData(coordinator, client)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    await hass.config_entries.async_forward_entry_setups(
        entry, [Platform(platform) for platform in PLATFORMS]
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: HkteConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(
        entry, [Platform(platform) for platform in PLATFORMS]
    )


async def _async_reload_entry(hass: HomeAssistant, entry: HkteConfigEntry) -> None:
    """Reload after an option changes."""
    await hass.config_entries.async_reload(entry.entry_id)
