"""Notice feed and native dashboard template coverage."""

from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import yaml
from homeassistant.const import CONF_PASSWORD
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.template import Template
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hkte_smart_school.api import HkteConnectionError
from custom_components.hkte_smart_school.const import CONF_LOGIN_NAME, DOMAIN


async def test_notice_feed_and_dashboard(hass, snapshot):
    notice = snapshot.children[0].notices[0]
    notices = tuple(
        replace(
            notice,
            id=str(index),
            title=f"Notice {index}",
            issued_at=notice.issued_at + timedelta(days=index),
            content='<img src="https://example.test/private">\n![image](https://example.test)',
        )
        for index in range(25)
    )
    snapshot = replace(
        snapshot,
        children=(replace(snapshot.children[0], notices=(*notices, notices[-1])),),
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="account-1",
        data={CONF_LOGIN_NAME: "parent", CONF_PASSWORD: "fixture-password"},
    )
    entry.add_to_hass(hass)
    fetch = AsyncMock(return_value=snapshot)
    with patch("custom_components.hkte_smart_school.HkteClient.async_fetch", fetch):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        entity_id = next(
            entity.entity_id
            for entity in er.async_get(hass).entities.values()
            if entity.unique_id.endswith("notice_content")
        )
        state = hass.states.get(entity_id)
        assert state.attributes["displayed_count"] == 20
        assert state.attributes["has_more"] is True
        assert [item["id"] for item in state.attributes["notices"]] == [
            str(index) for index in range(24, 4, -1)
        ]
        assert "notices" in state.state_info["unrecorded_attributes"]

        card = yaml.safe_load(
            (Path(__file__).parents[1] / "examples/notices-card.yaml").read_text()
        )
        template = Template(card["content"], hass)
        rendered = template.async_render()
        assert "Notice 24" in rendered
        assert "&lt;img" in rendered
        assert '<img src="https://example.test/private">' not in rendered
        assert "<details open>" in rendered

        fetch.side_effect = HkteConnectionError()
        await entry.runtime_data.coordinator.async_refresh()
        await hass.async_block_till_done()
        failed_state = hass.states.get(entity_id)
        assert failed_state.state == "unavailable"
        assert "notices" not in failed_state.attributes
        assert entry.runtime_data.coordinator.data == snapshot
        assert "尚未載入通告內容或暫時無法更新" in template.async_render()

        fetch.side_effect = None
        fetch.return_value = replace(
            snapshot, children=(replace(snapshot.children[0], notices=()),)
        )
        await entry.runtime_data.coordinator.async_refresh()
        await hass.async_block_till_done()
        empty_state = hass.states.get(entity_id)
        assert empty_state.state == "0"
        assert empty_state.attributes["notices"] == []
        assert empty_state.attributes["has_more"] is False
    await hass.config_entries.async_unload(entry.entry_id)
