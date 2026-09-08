"""HKTE Smart School integration setup."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import quote

from aiohttp import web
from homeassistant.components.http import HomeAssistantView  # type: ignore[attr-defined]
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import HkteClient, HkteConnectionError, HkteResponseError
from .const import (
    CONF_LOGIN_NAME,
    CONF_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL_MINUTES,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import HkteDataUpdateCoordinator


@dataclass(slots=True)
class HkteRuntimeData:
    """Runtime data attached to a config entry."""

    coordinator: HkteDataUpdateCoordinator
    client: HkteClient


type HkteConfigEntry = ConfigEntry[HkteRuntimeData]


class HkteAttachmentView(HomeAssistantView):
    """Proxy authenticated attachment downloads without exposing HKTE sid."""

    url = "/api/hkte_smart_school/attachments/{entry_id}/{child_id}/{notice_id}/{attachment_id}"
    name = "api:hkte_smart_school:attachment"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        """Download an attachment after checking it belongs to the snapshot."""
        hass = request.app["hass"]
        entry = hass.config_entries.async_get_entry(request.match_info["entry_id"])
        if entry is None or entry.domain != "hkte_smart_school" or not entry.state.recoverable:
                raise web.HTTPNotFound
        runtime = entry.runtime_data
        child = runtime.coordinator.data.child(request.match_info["child_id"])
        if child is None:
            raise web.HTTPNotFound
        notice = next(
            (item for item in child.notices if item.id == request.match_info["notice_id"]),
            None,
        )
        if notice is None:
            raise web.HTTPNotFound
        attachment = next(
            (item for item in notice.attachments if item.id == request.match_info["attachment_id"]),
            None,
        )
        if attachment is None:
            raise web.HTTPNotFound
        try:
            downloaded = await runtime.client.async_download_attachment(
                child.id,
                attachment.id,
                attachment.filename,
                attachment.mime_type,
                attachment.source_url,
            )
        except (HkteConnectionError, HkteResponseError) as err:
            raise web.HTTPBadGateway from err
        safe_filename = quote(downloaded.filename.replace("\r", "").replace("\n", ""))
        return web.Response(
            body=downloaded.content,
            content_type=downloaded.mime_type,
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{safe_filename}"},
        )


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
    if hass.http is not None and not hass.data.get(f"{DOMAIN}.attachment_view"):
        hass.http.register_view(HkteAttachmentView())
        hass.data[f"{DOMAIN}.attachment_view"] = True
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
