"""Config and options flows for HKTE Smart School."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import (
    HkteClient,
    HkteConnectionError,
    HkteInvalidAuthError,
    HkteResponseError,
)
from .const import (
    CONF_LOGIN_NAME,
    CONF_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL_MINUTES,
    DOMAIN,
    MAX_UPDATE_INTERVAL_MINUTES,
    MIN_UPDATE_INTERVAL_MINUTES,
)


async def _async_validate(hass: HomeAssistant, login_name: str, password: str) -> str:
    if not login_name.strip() or not password:
        raise HkteInvalidAuthError
    session = async_create_clientsession(hass, auto_cleanup=False)
    try:
        client = HkteClient(session, login_name.strip(), password)
        identity = await client.async_validate()
        return identity.account_id
    finally:
        session.detach()


class HkteConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle HKTE Smart School configuration."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Create the single supported account entry."""
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                account_id = await _async_validate(
                    self.hass,
                    user_input[CONF_LOGIN_NAME],
                    user_input[CONF_PASSWORD],
                )
            except HkteInvalidAuthError:
                errors["base"] = "invalid_auth"
            except HkteConnectionError:
                errors["base"] = "cannot_connect"
            except HkteResponseError:
                errors["base"] = "invalid_response"
            else:
                await self.async_set_unique_id(account_id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title="HKTE Smart School", data=user_input)
        return self.async_show_form(
            step_id="user", data_schema=_credentials_schema(user_input), errors=errors
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> config_entries.ConfigFlowResult:
        """Start credential renewal."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Validate and replace credentials."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            try:
                account_id = await _async_validate(
                    self.hass,
                    user_input[CONF_LOGIN_NAME],
                    user_input[CONF_PASSWORD],
                )
            except HkteInvalidAuthError:
                errors["base"] = "invalid_auth"
            except HkteConnectionError:
                errors["base"] = "cannot_connect"
            except HkteResponseError:
                errors["base"] = "invalid_response"
            else:
                await self.async_set_unique_id(account_id)
                self._abort_if_unique_id_mismatch(reason="wrong_account")
                return self.async_update_and_abort(entry, data=user_input)
        suggested = {
            CONF_LOGIN_NAME: entry.data[CONF_LOGIN_NAME],
        }
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=_credentials_schema(user_input or suggested),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Return the options flow."""
        return HkteOptionsFlow()


class HkteOptionsFlow(config_entries.OptionsFlow):
    """Manage polling options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Edit the update interval."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        current = self.config_entry.options.get(
            CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL_MINUTES
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_UPDATE_INTERVAL, default=current): NumberSelector(
                        NumberSelectorConfig(
                            min=MIN_UPDATE_INTERVAL_MINUTES,
                            max=MAX_UPDATE_INTERVAL_MINUTES,
                            step=1,
                            mode=NumberSelectorMode.BOX,
                            unit_of_measurement="minutes",
                        )
                    )
                }
            ),
        )


def _credentials_schema(values: dict[str, Any] | None) -> vol.Schema:
    values = values or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_LOGIN_NAME,
                default=values.get(CONF_LOGIN_NAME, ""),
            ): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
            vol.Required(CONF_PASSWORD): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD)
            ),
        }
    )
