"""Tests for config, reauthentication and options flows."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hkte_smart_school.api import HkteConnectionError, HkteResponseError
from custom_components.hkte_smart_school.const import (
    CONF_LOGIN_NAME,
    CONF_UPDATE_INTERVAL,
    DOMAIN,
)


@pytest.mark.parametrize(
    "error,expected",
    [
        (HkteConnectionError, "cannot_connect"),
        (HkteResponseError, "invalid_response"),
    ],
)
async def test_connection_and_response_errors(hass, error, expected):
    with patch(
        "custom_components.hkte_smart_school.config_flow._async_validate",
        AsyncMock(side_effect=error),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER},
            data={CONF_LOGIN_NAME: "parent", CONF_PASSWORD: "fixture-password"},
        )
    assert result["errors"] == {"base": expected}


async def test_options_reject_out_of_range(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    for value in (0, 4, 61):
        with pytest.raises(vol.Invalid):
            result["data_schema"]({CONF_UPDATE_INTERVAL: value})


async def test_reauth_success_with_update_listener(hass, snapshot):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="account-1",
        data={CONF_LOGIN_NAME: "parent", CONF_PASSWORD: "fixture-old"},
    )
    entry.add_to_hass(hass)
    with (
        patch(
            "custom_components.hkte_smart_school.HkteClient.async_fetch",
            AsyncMock(return_value=snapshot),
        ),
        patch(
            "custom_components.hkte_smart_school.config_flow._async_validate",
            AsyncMock(return_value="account-1"),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": config_entries.SOURCE_REAUTH,
                "entry_id": entry.entry_id,
                "unique_id": entry.unique_id,
            },
            data=dict(entry.data),
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_LOGIN_NAME: "parent", CONF_PASSWORD: "fixture-new"}
        )
        await hass.async_block_till_done()
        assert result["reason"] == "reauth_successful"
        assert entry.data[CONF_PASSWORD] == "fixture-new"
        assert entry.state is config_entries.ConfigEntryState.LOADED
        await hass.config_entries.async_unload(entry.entry_id)


async def test_user_flow_creates_single_entry(hass):
    with patch(
        "custom_components.hkte_smart_school.config_flow._async_validate",
        AsyncMock(return_value="account-1"),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        assert result["type"] is FlowResultType.FORM

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_LOGIN_NAME: "parent", CONF_PASSWORD: "secret"},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "HKTE Smart School"
    assert result["data"] == {
        CONF_LOGIN_NAME: "parent",
        CONF_PASSWORD: "secret",
    }

    second = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert second["type"] is FlowResultType.ABORT
    assert second["reason"] == "single_instance_allowed"


async def test_user_flow_maps_validation_errors(hass):
    from custom_components.hkte_smart_school.api import HkteInvalidAuthError

    with patch(
        "custom_components.hkte_smart_school.config_flow._async_validate",
        AsyncMock(side_effect=HkteInvalidAuthError),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER},
            data={CONF_LOGIN_NAME: "parent", CONF_PASSWORD: "wrong"},
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_options_flow_sets_update_interval(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="account-1",
        data={CONF_LOGIN_NAME: "parent", CONF_PASSWORD: "secret"},
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_UPDATE_INTERVAL: 30}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_UPDATE_INTERVAL: 30, "ai_enabled": False}


async def test_reauth_rejects_different_account(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="account-1",
        data={CONF_LOGIN_NAME: "parent", CONF_PASSWORD: "old"},
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.hkte_smart_school.config_flow._async_validate",
        AsyncMock(return_value="account-2"),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": config_entries.SOURCE_REAUTH,
                "entry_id": entry.entry_id,
                "unique_id": entry.unique_id,
            },
            data=dict(entry.data),
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_LOGIN_NAME: "other", CONF_PASSWORD: "new"},
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"


@pytest.mark.parametrize(
    "fields,error",
    [
        ({"ai_enabled": True}, "ai_required"),
        (
            {
                "ai_enabled": True,
                "ai_api_key": "fixture",
                "ai_model": "vision",
                "ai_base_url": "ftp://example.test",
            },
            "invalid_ai_url",
        ),
    ],
)
async def test_ai_options_validation(hass, fields, error):
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_UPDATE_INTERVAL: 15, **fields}
    )
    assert result["errors"] == {"base": error}


async def test_ai_options_retain_secret_without_prefilling(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        options={
            "ai_enabled": True,
            "ai_api_key": "fixture-key",
            "ai_model": "vision",
            "ai_base_url": "https://example.test/v1",
        },
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert "fixture-key" not in str(result["data_schema"])
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_UPDATE_INTERVAL: 15, "ai_api_key": ""}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["ai_api_key"] == "fixture-key"
