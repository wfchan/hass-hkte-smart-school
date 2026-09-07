"""Tests for persistent event deduplication."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from homeassistant.const import CONF_PASSWORD
from homeassistant.exceptions import ConfigEntryAuthFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hkte_smart_school.api import HkteConnectionError, HkteInvalidAuthError
from custom_components.hkte_smart_school.const import CONF_LOGIN_NAME, DOMAIN
from custom_components.hkte_smart_school.coordinator import HkteDataUpdateCoordinator
from custom_components.hkte_smart_school.models import AccountSnapshot, ChildSnapshot, Notice


async def test_failed_update_retains_snapshot(hass, snapshot):
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_fetch.return_value = snapshot
    coordinator = HkteDataUpdateCoordinator(hass, entry, client, timedelta(minutes=15))
    await coordinator.async_refresh()
    client.async_fetch.side_effect = HkteConnectionError
    await coordinator.async_refresh()
    assert coordinator.data == snapshot
    assert not coordinator.last_update_success
    await coordinator.async_shutdown()


async def test_auth_failure_maps_to_reauth(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_fetch.side_effect = HkteInvalidAuthError
    coordinator = HkteDataUpdateCoordinator(hass, entry, client, timedelta(minutes=15))
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()
    await coordinator.async_shutdown()


async def test_seen_ids_persist_and_only_new_ids_emit(hass, snapshot):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="account-1",
        data={CONF_LOGIN_NAME: "parent", CONF_PASSWORD: "secret"},
    )
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_fetch.return_value = snapshot
    coordinator = HkteDataUpdateCoordinator(hass, entry, client, timedelta(minutes=15))
    await coordinator.async_initialize()
    await coordinator.async_refresh()
    assert coordinator.new_items == ()

    child = snapshot.children[0]
    changed = AccountSnapshot(
        account_id=snapshot.account_id,
        retrieved_at=snapshot.retrieved_at + timedelta(minutes=15),
        children=(
            ChildSnapshot(
                id=child.id,
                name=child.name,
                school=child.school,
                notices=(
                    Notice(
                        id="notice-2",
                        title="New notice",
                        issued_at=snapshot.retrieved_at,
                        deadline=None,
                        unread=True,
                        replied=False,
                    ),
                    *child.notices,
                ),
                messages=child.messages,
                homeworks=child.homeworks,
            ),
        ),
    )
    client.async_fetch.return_value = changed
    await coordinator.async_refresh()
    assert [(item.kind, item.item_id) for item in coordinator.new_items] == [("notice", "notice-2")]
    await coordinator.async_shutdown()

    restarted_client = AsyncMock()
    restarted_client.async_fetch.return_value = changed
    restarted = HkteDataUpdateCoordinator(hass, entry, restarted_client, timedelta(minutes=15))
    await restarted.async_initialize()
    await restarted.async_refresh()
    assert restarted.new_items == ()
    await restarted.async_shutdown()
