"""Explicit signing bridge: ownership, confirmation, durable replay and privacy."""

import time
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web

from custom_components.hkte_smart_school.signing import (
    SigningError,
    SigningManager,
    hub_origin,
    validate_submission,
)

OPTIONS = {
    "signing_enabled": True,
    "message_hub_url": "https://hub.example",
    "message_hub_api_key": "fixture-hub-key",
}
BODY = {"form_version": "a" * 64, "answers": {"main": [1]}, "comment": "", "confirmed": True}


def form():
    return {
        "child_id": "child-1",
        "notice_id": "notice-1",
        "title": "Fixture",
        "introduction": "Choose an answer",
        "deadline": None,
        "unread": True,
        "replied": False,
        "form_version": "a" * 64,
        "can_sign": True,
        "supported": True,
        "blocked_reasons": [],
        "questions": [],
    }


def operation(body, status="succeeded"):
    return {
        "request_id": body["request_id"],
        "child_id": "child-1",
        "notice_id": "notice-1",
        "action": "sign",
        "status": status,
        "replied": status == "succeeded",
        "verified_at": "2026-09-12T00:00:00Z" if status == "succeeded" else None,
    }


@pytest.mark.parametrize(
    "url",
    [
        "http://hub.example",
        "https://key@hub.example",
        "https://hub.example/path",
        "https://hub.example?key=x",
        "https://hub.example/#x",
    ],
)
def test_reject_unsafe_origins(url):
    with pytest.raises(ValueError):
        hub_origin(url)


@pytest.mark.parametrize(
    "change",
    [
        {"confirmed": False},
        {"confirmed": 1},
        {"confirmed": "true"},
        {"request_id": "browser-id"},
        {"answers": {"main": [True]}},
        {"answers": {"main": [-1]}},
        {"comment": "x" * 10001},
        {"form_version": "bad"},
    ],
)
def test_explicit_bounded_confirmation(change):
    with pytest.raises(SigningError):
        validate_submission(BODY | change)


async def test_read_form_never_signs_and_projects_response(hass):
    manager = SigningManager(hass, "read-form", OPTIONS)
    with patch.object(
        manager, "request", AsyncMock(return_value=form() | {"secret": "private"})
    ) as req:
        result = await manager.form("child-1", "notice-1")
        assert "secret" not in result
        assert req.call_args.args == ("GET", "/children/child-1/notices/notice-1/reply-form")
    with (
        patch.object(manager, "request", AsyncMock(return_value=form() | {"child_id": "other"})),
        pytest.raises(SigningError),
    ):
        await manager.form("child-1", "notice-1")


async def test_persist_before_send_and_no_duplicate_submission(hass):
    manager = SigningManager(hass, "sign-success", OPTIONS)

    async def request(method, path, body=None):
        if method == "GET":
            return form()
        stored = await manager.store.async_load()
        assert next(iter(stored.values()))["body"] == body
        assert body["answers"] == {"main": [1]}
        return operation(body)

    with patch.object(manager, "request", AsyncMock(side_effect=request)) as req:
        assert (await manager.submit("child-1", "notice-1", BODY))["status"] == "succeeded"
        assert (await manager.submit("child-1", "notice-1", BODY))["status"] == "succeeded"
        assert req.await_count == 2


async def test_timeout_restore_and_reconcile_original_body_only(hass):
    manager = SigningManager(hass, "sign-unknown", OPTIONS)
    with patch.object(
        manager, "request", AsyncMock(side_effect=[form(), SigningError("hub_unavailable")])
    ):
        assert (await manager.submit("child-1", "notice-1", BODY))["status"] == "unknown"
    original = next(iter(manager.records.values()))["body"]
    restored = SigningManager(hass, "sign-unknown", OPTIONS)
    await restored.async_initialize()
    with patch.object(restored, "request", AsyncMock()) as req:
        assert (await restored.reconcile("child-1", "notice-1"))["status"] == "unknown"
        assert (await restored.submit("child-1", "notice-1", BODY))["status"] == "unknown"
        req.assert_not_called()
    next(iter(restored.records.values()))["checked_at"] = time.time() - 301
    with patch.object(restored, "request", AsyncMock(return_value=operation(original))) as req:
        assert (await restored.reconcile("child-1", "notice-1"))["status"] == "succeeded"
        assert req.call_args.args == ("POST", "/children/child-1/notices/notice-1/sign", original)


async def test_unresolved_retry_error_never_allows_new_uuid(hass):
    manager = SigningManager(hass, "sign-retry", OPTIONS)
    with patch.object(
        manager, "request", AsyncMock(side_effect=[form(), SigningError("hub_unavailable")])
    ):
        await manager.submit("child-1", "notice-1", BODY)
    record = next(iter(manager.records.values()))
    original = record["body"]["request_id"]
    record["checked_at"] = time.time() - 301
    with patch.object(manager, "request", AsyncMock(side_effect=SigningError("form_conflict"))):
        assert (await manager.reconcile("child-1", "notice-1"))["status"] == "unknown"
    assert record["body"]["request_id"] == original


@pytest.mark.parametrize(
    "change", [{"can_sign": False}, {"supported": False}, {"form_version": "b" * 64}]
)
async def test_preflight_blocks_changed_or_unsupported_form(hass, change):
    manager = SigningManager(hass, "sign-preflight", OPTIONS)
    with patch.object(manager, "request", AsyncMock(return_value=form() | change)) as req:
        with pytest.raises(SigningError):
            await manager.submit("child-1", "notice-1", BODY)
        assert req.await_count == 1
        assert not manager.records


async def test_changed_form_response_requires_review(hass):
    manager = SigningManager(hass, "sign-changed", OPTIONS)
    with patch.object(
        manager, "request", AsyncMock(side_effect=[form(), SigningError("form_conflict")])
    ):
        result = await manager.submit("child-1", "notice-1", BODY)
    assert result["status"] == "not_sent"


async def test_concurrent_requests_do_not_queue_second_write(hass):
    manager = SigningManager(hass, "sign-concurrent", OPTIONS)
    async with manager.lock:
        with pytest.raises(SigningError, match="signing_busy"):
            await manager.submit("child-1", "notice-1", BODY)


async def test_success_requires_verified_target_and_reply(hass):
    manager = SigningManager(hass, "sign-unverified", OPTIONS)

    async def request(method, path, body=None):
        return form() if method == "GET" else operation(body) | {"replied": False}

    with patch.object(manager, "request", AsyncMock(side_effect=request)):
        assert (await manager.submit("child-1", "notice-1", BODY))["status"] == "unknown"


async def test_network_headers_no_redirects_or_provider_error_leak(
    hass, aiohttp_server, socket_enabled
):
    calls = []

    async def handler(request):
        calls.append(request.path)
        assert request.headers["Authorization"] == "Bearer fixture-hub-key"
        assert request.headers["User-Agent"] == "HKTE-HomeAssistant/1.0"
        assert not request.cookies
        return web.Response(
            status=302, headers={"Location": "/secret"}, text="private provider body"
        )

    app = web.Application()
    app.router.add_get("/{tail:.*}", handler)
    server = await aiohttp_server(app)
    manager = SigningManager(hass, "wire", OPTIONS)
    with (
        patch(
            "custom_components.hkte_smart_school.signing.hub_origin",
            return_value=str(server.make_url("/")).rstrip("/"),
        ),
        pytest.raises(SigningError, match="^hub_unavailable$"),
    ):
        await manager.request("GET", "/children")
    assert len(calls) == 1
