"""Config flow for Oppo UDP-20X integration."""

from __future__ import annotations

import logging
from typing import Any, override
from urllib.parse import urlsplit

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_MAC, CONF_NAME, CONF_PORT

from .const import CONF_MODEL, DEFAULT_PORT, DOMAIN, MAGNETAR_MODELS, MAGNETAR_PORT, MODEL_UDP203, MODELS
from .magnetar_client import MagnetarClient, parse_mac
from .oppo_client import OppoClient, PowerState

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
        vol.Required(CONF_NAME, default="Oppo UDP-203"): str,
        vol.Required(CONF_MODEL, default=MODEL_UDP203): vol.In(MODELS),
        # Required for Magnetar players (used for Wake-on-LAN); ignored for Oppo.
        vol.Optional(CONF_MAC, default=""): str,
    }
)


def _normalize_host(host: str) -> str:
    """Normalize a user-supplied host (port and brackets handled separately).

    Strips whitespace, removes IPv6 bracket framing, and discards a trailing
    ``:port`` for plain hostnames or IPv4 literals (the port is configured
    separately). Bare IPv6 literals are returned unchanged because they cannot
    carry a port without brackets.
    """
    normalized = host.strip()
    if normalized.startswith("["):
        end = normalized.find("]")
        if end > 0:
            return normalized[1:end]
    if normalized.count(":") >= 2:
        return normalized
    return urlsplit(f"//{normalized}").hostname or normalized


class OppoUDPConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Oppo UDP-20X."""

    VERSION = 1

    @override
    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Normalize host so the unique ID, connection test, and the stored
            # config entry all agree (handles bracketed IPv6, stray whitespace,
            # and case-folding for the unique ID lookup).
            normalized_host = _normalize_host(user_input[CONF_HOST])
            user_input[CONF_HOST] = normalized_host

            await self.async_set_unique_id(normalized_host.lower())
            self._abort_if_unique_id_configured()

            if user_input[CONF_MODEL] in MAGNETAR_MODELS:
                errors = await self._async_validate_magnetar(user_input)
            else:
                errors = await self._async_validate_oppo(user_input)

            if not errors:
                return self.async_create_entry(title=user_input[CONF_NAME], data=user_input)

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    @staticmethod
    async def _async_validate_oppo(user_input: dict[str, Any]) -> dict[str, str]:
        """Validate an Oppo UDP-20X entry by querying power status."""
        client = OppoClient(user_input[CONF_HOST], port=user_input[CONF_PORT])
        try:
            if await client.connect():
                power_state = await client.query_power_status()
                if power_state != PowerState.UNKNOWN:
                    return {}
            return {"base": "cannot_connect"}
        except Exception:
            _LOGGER.exception("Unexpected exception during connection test")
            return {"base": "cannot_connect"}
        finally:
            # Always tear down — connect() may have left a partial transport
            # open (e.g. if setsockopt failed after the writer was created).
            await client.disconnect()

    @staticmethod
    async def _async_validate_magnetar(user_input: dict[str, Any]) -> dict[str, str]:
        """Validate a Magnetar entry: require a MAC and confirm the port opens.

        Magnetar players listen on a fixed control port and answer commands
        with ``ack`` only — there is no query to confirm identity, so a
        successful TCP connection is the strongest check available. The port is
        forced to the Magnetar default regardless of the value in the form.
        """
        if parse_mac(user_input.get(CONF_MAC, "")) is None:
            return {CONF_MAC: "invalid_mac"}

        user_input[CONF_PORT] = MAGNETAR_PORT
        client = MagnetarClient(user_input[CONF_HOST], user_input[CONF_MAC], port=MAGNETAR_PORT)
        try:
            if await client.connect():
                return {}
            return {"base": "cannot_connect"}
        except Exception:
            _LOGGER.exception("Unexpected exception during connection test")
            return {"base": "cannot_connect"}
        finally:
            await client.disconnect()
