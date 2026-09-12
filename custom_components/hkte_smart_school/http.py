"""HA-authenticated notice actions; IDs are always resolved through the entity registry."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from aiohttp import web
from homeassistant.auth.permissions.const import POLICY_CONTROL, POLICY_READ
from homeassistant.components.http.const import KEY_HASS_USER
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.http import HomeAssistantView

from .api import HkteError
from .attachments import AttachmentError, async_download
from .const import DOMAIN
from .models import ChildSnapshot, Notice
from .signing import SigningError

if TYPE_CHECKING:
    from . import HkteRuntimeData


def resolve(
    hass: HomeAssistant, request: web.Request, entity_id: str, notice_id: str
) -> tuple[HkteRuntimeData, ChildSnapshot, Notice]:
    user = request[KEY_HASS_USER]
    if not user.permissions.check_entity(entity_id, POLICY_READ):
        raise web.HTTPForbidden
    entity = er.async_get(hass).async_get(entity_id)
    if entity is None or entity.platform != DOMAIN or not entity.config_entry_id:
        raise web.HTTPNotFound
    entry = hass.config_entries.async_get_entry(entity.config_entry_id)
    if entry is None or entry.state is not ConfigEntryState.LOADED:
        raise web.HTTPServiceUnavailable
    runtime: HkteRuntimeData = entry.runtime_data
    if not runtime.coordinator.last_update_success:
        raise web.HTTPServiceUnavailable
    snapshot = runtime.coordinator.data
    for child in snapshot.children:
        if entity.unique_id != f"{snapshot.account_id}_{child.id}_notice_content":
            continue
        for notice in child.notices:
            if notice.id == notice_id:
                return runtime, child, notice
    raise web.HTTPNotFound


class AttachmentView(HomeAssistantView):
    """Return bytes to an authenticated frontend without exposing uHubSid."""

    url = "/api/hkte_smart_school/notice/{entity_id}/{notice_id}/attachment/{attachment_id}"
    name = "api:hkte_smart_school:attachment"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(
        self, request: web.Request, entity_id: str, notice_id: str, attachment_id: str
    ) -> web.Response:
        runtime, child, notice = resolve(self.hass, request, entity_id, notice_id)
        if not any(a.id == attachment_id for a in notice.attachments):
            raise web.HTTPNotFound
        try:
            file = await async_download(runtime.client, child.id, notice.id, attachment_id)
        except AttachmentError as err:
            return self.json({"error": str(err)}, status_code=502)
        except HkteError:
            return self.json({"error": "download_failed"}, status_code=502)
        return web.Response(
            body=file.content,
            content_type=file.mime_type,
            headers={
                "Content-Disposition": "attachment; filename*=UTF-8''"
                + quote(file.filename, safe=""),
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )


class AnalysisView(HomeAssistantView):
    """Start and inspect a notice's analysis with the same authorization as downloads."""

    url = "/api/hkte_smart_school/notice/{entity_id}/{notice_id}/analysis"
    name = "api:hkte_smart_school:analysis"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request, entity_id: str, notice_id: str) -> web.Response:
        runtime, child, notice = resolve(self.hass, request, entity_id, notice_id)
        return self.json(
            runtime.analysis.status(child.id, notice), headers={"Cache-Control": "no-store"}
        )

    async def post(self, request: web.Request, entity_id: str, notice_id: str) -> web.Response:
        runtime, child, notice = resolve(self.hass, request, entity_id, notice_id)
        try:
            body: Any = await request.json()
        except ValueError:
            raise web.HTTPBadRequest from None
        if (
            not isinstance(body, dict)
            or set(body) - {"force"}
            or type(body.get("force", False)) is not bool
        ):
            raise web.HTTPBadRequest
        try:
            result = runtime.analysis.start(child.id, notice, body.get("force", False))
        except AttachmentError as err:
            return self.json({"error": str(err)}, status_code=409)
        return self.json(result, headers={"Cache-Control": "no-store"})


class ReplyFormView(HomeAssistantView):
    """Opening the form never marks read or signs the notice."""

    url = "/api/hkte_smart_school/notice/{entity_id}/{notice_id}/reply-form"
    name = "api:hkte_smart_school:reply-form"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request, entity_id: str, notice_id: str) -> web.Response:
        runtime, child, notice = resolve(self.hass, request, entity_id, notice_id)
        if not request[KEY_HASS_USER].permissions.check_entity(entity_id, POLICY_CONTROL):
            raise web.HTTPForbidden
        try:
            result = await runtime.signing.form(child.id, notice.id)
        except SigningError as err:
            return self.json({"error": str(err)}, status_code=409)
        return self.json(result, headers={"Cache-Control": "no-store"})


class SignView(ReplyFormView):
    """Submit only user-reviewed answers with entity control permission."""

    url = "/api/hkte_smart_school/notice/{entity_id}/{notice_id}/sign"
    name = "api:hkte_smart_school:sign"

    async def post(self, request: web.Request, entity_id: str, notice_id: str) -> web.Response:
        runtime, child, notice = resolve(self.hass, request, entity_id, notice_id)
        if not request[KEY_HASS_USER].permissions.check_entity(entity_id, POLICY_CONTROL):
            raise web.HTTPForbidden
        try:
            body = await request.json()
        except ValueError:
            raise web.HTTPBadRequest from None
        try:
            result = await runtime.signing.submit(child.id, notice.id, body)
        except SigningError as err:
            return self.json({"error": str(err)}, status_code=409)
        if result["status"] == "succeeded":
            self.hass.async_create_task(runtime.coordinator.async_request_refresh())
        return self.json(result, headers={"Cache-Control": "no-store"})


class SignStatusView(ReplyFormView):
    """Explicit reconciliation of an uncertain operation, with the original UUID."""

    url = "/api/hkte_smart_school/notice/{entity_id}/{notice_id}/sign-status"
    name = "api:hkte_smart_school:sign-status"

    async def post(self, request: web.Request, entity_id: str, notice_id: str) -> web.Response:
        runtime, child, notice = resolve(self.hass, request, entity_id, notice_id)
        if not request[KEY_HASS_USER].permissions.check_entity(entity_id, POLICY_CONTROL):
            raise web.HTTPForbidden
        try:
            result = await runtime.signing.reconcile(child.id, notice.id)
        except SigningError as err:
            return self.json({"error": str(err)}, status_code=409)
        if result["status"] == "succeeded":
            self.hass.async_create_task(runtime.coordinator.async_request_refresh())
        return self.json(result, headers={"Cache-Control": "no-store"})
