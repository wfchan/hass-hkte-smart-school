"""End-to-end entity registration tests inside Home Assistant."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from homeassistant.const import CONF_PASSWORD
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hkte_smart_school.const import CONF_LOGIN_NAME, DOMAIN
from custom_components.hkte_smart_school.models import Notice


async def test_setup_creates_expected_entities(hass, snapshot):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="account-1",
        data={CONF_LOGIN_NAME: "parent", CONF_PASSWORD: "secret"},
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.hkte_smart_school.HkteClient.async_fetch",
        AsyncMock(return_value=snapshot),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    registry = er.async_get(hass)
    entities = [
        item for item in registry.entities.values() if item.config_entry_id == entry.entry_id
    ]
    assert len(entities) == 11
    assert sum(item.domain == "sensor" for item in entities) == 8
    assert sum(item.domain == "calendar" for item in entities) == 2
    assert sum(item.domain == "event" for item in entities) == 1

    values = [
        hass.states.get(item.entity_id).state
        for item in entities
        if item.domain == "sensor"
        and not item.unique_id.endswith(("deadline", "notice_content"))
    ]
    assert sorted(values) == ["1", "1", "1", "1", "1", "2"]

    content_entity = next(item for item in entities if item.unique_id.endswith("notice_content"))
    content_state = hass.states.get(content_entity.entity_id)
    assert content_state.state == "1"
    assert (
        content_state.attributes["notices"][0]["content"]
        == snapshot.children[0].notices[0].content
    )
    assert content_state.attributes["has_more"] is False

    calendar_entity = next(
        item.entity_id
        for item in entities
        if item.domain == "calendar" and item.unique_id.endswith("homework_deadlines")
    )
    response = await hass.services.async_call(
        "calendar",
        "get_events",
        {
            "entity_id": calendar_entity,
            "start_date_time": "2026-09-01T00:00:00+08:00",
            "end_date_time": "2026-09-30T00:00:00+08:00",
        },
        blocking=True,
        return_response=True,
    )
    assert response is not None
    assert len(response[calendar_entity]["events"]) == 2

    event_entity = next(item for item in entities if item.domain == "event")
    assert hass.states.get(event_entity.entity_id).state == "unknown"

    await hass.config_entries.async_unload(entry.entry_id)


async def test_diagnostics_exclude_sensitive_fields(hass, snapshot):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="account-1",
        data={CONF_LOGIN_NAME: "private-login", CONF_PASSWORD: "private-password"},
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.hkte_smart_school.HkteClient.async_fetch",
        AsyncMock(return_value=snapshot),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    from custom_components.hkte_smart_school.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    rendered = str(diagnostics)
    assert "private-login" not in rendered
    assert "private-password" not in rendered
    assert "Test child" not in rendered
    assert snapshot.children[0].notices[0].content not in rendered
    assert diagnostics["child_count"] == 1
    assert diagnostics["record_counts"] == [{"notices": 1, "messages": 1, "homeworks": 2}]

    await hass.config_entries.async_unload(entry.entry_id)


async def test_refresh_emits_only_the_new_item(hass, snapshot):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="account-1",
        data={CONF_LOGIN_NAME: "parent", CONF_PASSWORD: "secret"},
    )
    entry.add_to_hass(hass)
    mock_fetch = AsyncMock(return_value=snapshot)
    with patch("custom_components.hkte_smart_school.HkteClient.async_fetch", mock_fetch):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        registry = er.async_get(hass)
        event_entity = next(
            item
            for item in registry.entities.values()
            if item.config_entry_id == entry.entry_id and item.domain == "event"
        )
        assert hass.states.get(event_entity.entity_id).state == "unknown"

        child = snapshot.children[0]
        changed = replace(
            snapshot,
            retrieved_at=snapshot.retrieved_at + timedelta(minutes=15),
            children=(
                replace(
                    child,
                    notices=(
                        Notice(
                            id="notice-2",
                            title="New notice",
                            issued_at=snapshot.retrieved_at,
                            deadline=None,
                            unread=True,
                            replied=False,
                            content="Private new notice body",
                        ),
                        *child.notices,
                    ),
                ),
            ),
        )
        mock_fetch.return_value = changed
        await entry.runtime_data.coordinator.async_refresh()
        await hass.async_block_till_done()

        state = hass.states.get(event_entity.entity_id)
        assert state.state != "unknown"
        assert state.attributes["event_type"] == "notice"
        assert state.attributes["item_id"] == "notice-2"
        assert state.attributes["title"] == "New notice"
        assert "Private new notice body" not in str(state.attributes)

    await hass.config_entries.async_unload(entry.entry_id)
