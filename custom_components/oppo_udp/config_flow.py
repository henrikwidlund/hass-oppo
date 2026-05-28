"""Config flow for Oppo UDP-20X integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT

from .const import CONF_MODEL, DEFAULT_PORT, DOMAIN, MODEL_UDP203, MODELS
from .oppo_client import OppoClient

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
        vol.Required(CONF_NAME, default="Oppo UDP-203"): str,
        vol.Required(CONF_MODEL, default=MODEL_UDP203): vol.In(MODELS),
    }
)


class OppoUDPConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Oppo UDP-20X."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST]
            port = user_input[CONF_PORT]

            # Test the connection
            client = OppoClient(host, port=port)
            try:
                if await client.connect():
                    await client.query_power_status()
                    await client.disconnect()

                    # Use host as unique ID
                    await self.async_set_unique_id(host)
                    self._abort_if_unique_id_configured()

                    return self.async_create_entry(
                        title=user_input[CONF_NAME],
                        data=user_input,
                    )
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected exception during connection test")
                errors["base"] = "cannot_connect"
            finally:
                await client.disconnect()

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )
