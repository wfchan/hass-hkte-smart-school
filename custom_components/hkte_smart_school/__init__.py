"""HKTE Smart School integration setup."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .analysis import AnalysisManager
from .api import HkteClient
from .const import (
    CONF_LOGIN_NAME,
    CONF_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL_MINUTES,
    PLATFORMS,
)
from .coordinator import HkteDataUpdateCoordinator
from .http import AnalysisView, AttachmentView


@dataclass(slots=True)
class HkteRuntimeData:
    """Runtime data attached to a config entry."""

    coordinator: HkteDataUpdateCoordinator
    client: HkteClient
    analysis: AnalysisManager


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
    analysis = AnalysisManager(hass, entry.entry_id, client, entry.options)
    await analysis.async_initialize()
    entry.runtime_data = HkteRuntimeData(coordinator, client, analysis)
    if not hass.data.get("hkte_smart_school_http"):
        hass.http.register_view(AttachmentView(hass))
        hass.http.register_view(AnalysisView(hass))
        hass.data["hkte_smart_school_http"] = True
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    await hass.config_entries.async_forward_entry_setups(
        entry, [Platform(platform) for platform in PLATFORMS]
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: HkteConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(
        entry, [Platform(platform) for platform in PLATFORMS]
    )
    if unloaded:
        await entry.runtime_data.analysis.async_close()
    return unloaded


async def _async_reload_entry(hass: HomeAssistant, entry: HkteConfigEntry) -> None:
    """Reload after an option changes."""
    await hass.config_entries.async_reload(entry.entry_id)
