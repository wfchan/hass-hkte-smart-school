"""Live HA HTTP routing and entity authorization with synthetic provider records."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import CONF_PASSWORD
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hkte_smart_school.attachments import DownloadedFile
from custom_components.hkte_smart_school.const import CONF_LOGIN_NAME, DOMAIN


@pytest.fixture
async def setup_notice(hass, snapshot):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="account-1",
        data={CONF_LOGIN_NAME: "parent", CONF_PASSWORD: "fixture"},
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.hkte_smart_school.HkteClient.async_fetch",
        AsyncMock(return_value=snapshot),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    entity = next(
        e.entity_id
        for e in er.async_get(hass).entities.values()
        if e.unique_id.endswith("notice_content")
    )
    yield entry, f"/api/hkte_smart_school/notice/{entity}/notice-1"
    await hass.config_entries.async_unload(entry.entry_id)


async def test_download_authorization_and_membership(hass, hass_client, setup_notice):
    _, path = setup_notice
    client = await hass_client()
    with patch(
        "custom_components.hkte_smart_school.http.async_download",
        AsyncMock(return_value=DownloadedFile(b"%PDF-valid", "notice.pdf", "application/pdf")),
    ) as download:
        response = await client.get(path + "/attachment/attachment-1")
        assert response.status == 200
        assert await response.read() == b"%PDF-valid"
        assert response.headers["Cache-Control"] == "no-store"
        assert "attachment;" in response.headers["Content-Disposition"]
        response = await client.get(path + "/attachment/not-owned")
        assert response.status == 404
        assert download.await_count == 1
        with patch(
            "homeassistant.auth.permissions.PolicyPermissions.check_entity", return_value=False
        ):
            assert (await client.get(path + "/analysis")).status == 403
            assert (await client.get(path + "/attachment/attachment-1")).status == 403
    anonymous = await hass_client(None)
    assert (await anonymous.get(path + "/attachment/attachment-1")).status == 401
    assert (await anonymous.post(path + "/analysis", json={})).status == 401
    assert (await anonymous.get(path + "/analysis")).status == 401


async def test_analysis_requires_configuration_and_valid_input(hass_client, setup_notice):
    _, path = setup_notice
    client = await hass_client()
    response = await client.get(path + "/analysis")
    assert await response.json() == {"enabled": False, "status": "idle"}
    response = await client.post(path + "/analysis", json={})
    assert response.status == 409
    assert (await response.json())["error"] == "ai_not_configured"
    assert (await client.post(path + "/analysis", json={"force": "yes"})).status == 400
    assert (await client.post(path + "/analysis", json={"url": "https://bad.test"})).status == 400


async def test_unavailable_and_wrong_notice(hass_client, setup_notice):
    entry, path = setup_notice
    client = await hass_client()
    assert (await client.get(path.replace("notice-1", "other") + "/analysis")).status == 404
    entry.runtime_data.coordinator.last_update_success = False
    assert (await client.get(path + "/analysis")).status == 503
    assert (await client.get(path + "/attachment/attachment-1")).status == 503
