"""Direct HKTE signing: confirmation, persistence and readback recovery."""

import time
from unittest.mock import AsyncMock

import pytest

from custom_components.hkte_smart_school.forms import FormError, build_form, validate_reply
from custom_components.hkte_smart_school.signing import (
    SigningError,
    SigningManager,
    validate_submission,
)

RAW = {
    "nid": "notice-1",
    "title": "Fixture",
    "introduction": "Choose an answer",
    "body": "Fixture body",
    "qtype": "Choice",
    "options": ["Yes", "No"],
    "optional": False,
    "sectionsData": [],
    "sub_notices": [],
    "deadline": "2099-09-17T23:59:00+08:00",
    "unread": True,
    "replied": False,
}


def body() -> dict:
    return {
        "form_version": build_form(RAW)["form_version"],
        "answers": {"main": [1]},
        "comment": "",
        "confirmed": True,
    }


BODY = body()


def client() -> AsyncMock:
    value = AsyncMock()
    value.async_notice_form = AsyncMock(return_value=RAW)
    value.async_notice_reply = AsyncMock(return_value={"reply": [1]})
    value.async_sign_notice = AsyncMock(return_value={"success": True})
    return value


@pytest.mark.parametrize(
    "change", [{"confirmed": False}, {"confirmed": 1}, {"answers": []}, {"comment": "x" * 10001}]
)
def test_confirmation_is_required_and_bounded(change):
    with pytest.raises(SigningError):
        validate_submission(body() | change)


async def test_form_reads_direct_hkte_and_never_writes(hass):
    api = client()
    manager = SigningManager(hass, "direct-form", {"signing_enabled": True}, api)
    result = await manager.form("child-1", "notice-1")
    assert result["enabled"] is True
    assert result["questions"][0]["options"][1]["label"] == "No"
    api.async_sign_notice.assert_not_called()


async def test_submit_persists_before_direct_sign_and_verifies(hass):
    api = client()
    api.async_notice_form.side_effect = [RAW, RAW | {"replied": True}]
    seen = False

    async def sign(child, notice, payload):
        nonlocal seen
        seen = bool(await manager.store.async_load())

    api.async_sign_notice.side_effect = sign
    manager = SigningManager(hass, "direct-success", {"signing_enabled": True}, api)
    result = await manager.submit("child-1", "notice-1", body())
    assert result["status"] == "pending"
    assert seen
    api.async_sign_notice.assert_awaited_once()
    api.async_notice_reply.assert_not_awaited()
    assert api.async_sign_notice.await_args.args[2] == {
        "reply": [1],
        "amount": 0,
        "amount_without_extra_subsidy": 0,
    }


async def test_successful_write_waits_before_readback(hass):
    api = client()
    manager = SigningManager(hass, "direct-pending", {"signing_enabled": True}, api)
    result = await manager.submit("child-1", "notice-1", body())
    assert result["status"] == "pending"
    api.async_notice_reply.assert_not_awaited()
    manager.records[manager._key("child-1", "notice-1")]["checked_at"] = time.time() - 301
    api.async_notice_form.return_value = RAW | {"replied": True}
    result = await manager.reconcile("child-1", "notice-1")
    assert result["status"] == "succeeded"
    api.async_notice_reply.assert_awaited_once_with("child-1", "notice-1")


async def test_connection_failure_is_unknown_and_reconcile_only_reads(hass):
    api = client()
    api.async_sign_notice.side_effect = TimeoutError
    manager = SigningManager(hass, "direct-unknown", {"signing_enabled": True}, api)
    assert (await manager.submit("child-1", "notice-1", body()))["status"] == "unknown"
    api.async_sign_notice.assert_awaited_once()
    record = manager.records[manager._key("child-1", "notice-1")]
    record["checked_at"] = time.time() - 301
    api.async_notice_form.side_effect = [RAW | {"replied": True}]
    result = await manager.reconcile("child-1", "notice-1")
    assert result["status"] == "succeeded"
    api.async_sign_notice.assert_awaited_once()


async def test_existing_unknown_is_restored_without_new_write(hass):
    api = client()
    api.async_sign_notice.side_effect = TimeoutError
    manager = SigningManager(hass, "direct-restore", {"signing_enabled": True}, api)
    await manager.submit("child-1", "notice-1", body())
    restored = SigningManager(hass, "direct-restore", {"signing_enabled": True}, api)
    await restored.async_initialize()
    assert (await restored.submit("child-1", "notice-1", body()))["status"] == "unknown"
    api.async_sign_notice.assert_awaited_once()


@pytest.mark.parametrize(
    "change",
    [
        {"qtype": "Payment", "options": ["Pay"]},
        {"payment": True},
        {"qtype": "Unknown", "options": ["x"]},
        {"deadline": "2000-01-01"},
        {"replied": True},
    ],
)
def test_unsupported_or_unavailable_forms_are_blocked(change):
    raw = RAW | change
    form = build_form(raw)
    assert form["can_sign"] is False
    with pytest.raises(FormError):
        validate_reply(raw, form["form_version"], {"main": [0]}, "")
