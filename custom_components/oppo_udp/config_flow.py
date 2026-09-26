"""Config flow for Oppo/Magnetar integration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, override
from urllib.parse import urlsplit

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_MAC, CONF_NAME, CONF_PORT
from homeassistant.helpers.service_info.ssdp import ATTR_UPNP_FRIENDLY_NAME, ATTR_UPNP_MODEL_NAME

from .const import (
    CONF_MODEL,
    DEFAULT_PORT,
    DOMAIN,
    MAGNETAR_MODELS,
    MAGNETAR_PORT,
    MODEL_BDP10X,
    MODEL_DEFAULT_PORTS,
    MODEL_MAGNETAR,
    MODEL_UDP203,
    MODEL_UDP205,
    MODELS,
    OPPO_MODELS,
    PORT_MODEL_CANDIDATES,
    PRE_20X_MODELS,
)
from .magnetar_client import MagnetarClient, parse_mac
from .oppo_client import OppoClient, PowerState

if TYPE_CHECKING:
    from homeassistant.helpers.service_info.ssdp import SsdpServiceInfo

# UPnP modelName -> our model constant, for players discovered via SSDP (see
# async_step_ssdp).
_SSDP_MODEL_MAP = {
    "OPPO BDP-103": MODEL_BDP10X,
    "OPPO BDP-103D": MODEL_BDP10X,
    "OPPO BDP-105": MODEL_BDP10X,
    "OPPO BDP-105D": MODEL_BDP10X,
    "OPPO UDP-203": MODEL_UDP203,
    "OPPO UDP-205": MODEL_UDP205,
}

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


def _guess_udp20x_model(discovered_name: str) -> str:
    """Guess UDP-203 vs UDP-205 from the discovered server name.

    The discovery broadcast carries the player's configured name (e.g. "OPPO
    UDP-205"), not a model code, so this is a best-effort default for the
    confirmation form - the user can still change it before submitting.
    """
    return MODEL_UDP205 if "205" in discovered_name else MODEL_UDP203


def _model_candidates_for_port(port: int) -> list[str]:
    """Narrow the discovery-confirm model choices by the discovered control port.

    Both the UDP-20X broadcast and the legacy OREMOTE probe (see
    ``discovery.py``) resolve to a specific control port, which maps back to
    one or two candidate models (BDP-93/95 and BDP-103/105 share a port, as
    do UDP-203/UDP-205). A port outside that map means an unrecognized or
    non-standard setup, so fall back to every non-Magnetar model.
    """
    return PORT_MODEL_CANDIDATES.get(port, OPPO_MODELS)


def _default_model_for_discovery(discovered_name: str, port: int) -> str:
    """Best-effort default model for the discovery-confirm form."""
    if port == DEFAULT_PORT:
        return _guess_udp20x_model(discovered_name)
    return _model_candidates_for_port(port)[0]


class OppoUDPConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Blu-ray player."""

    VERSION = 1

    _discovered_host: str = ""
    _discovered_port: int = DEFAULT_PORT
    _discovered_name: str = ""
    # Set only when the discovery mechanism identifies the exact model (SSDP
    # does; the UDP-20X broadcast and legacy OREMOTE probe only narrow it
    # down, see _model_candidates_for_port).
    _discovered_model: str | None = None

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

    @override
    async def async_step_integration_discovery(self, discovery_info: dict[str, Any]) -> ConfigFlowResult:
        """Handle a player found via auto-discovery.

        ``discovery_info`` comes from one of three mechanisms (see
        ``discovery.py``): the UDP-20X broadcast, the legacy OREMOTE probe
        (both share the same shape: host/port/name), or the Magnetar SSDP
        probe (host only, flagged by ``CONF_MODEL``).
        """
        host = _normalize_host(str(discovery_info[CONF_HOST]))
        await self.async_set_unique_id(host.lower())
        self._abort_if_unique_id_configured()
        self._discovered_host = host

        if discovery_info.get(CONF_MODEL) == MODEL_MAGNETAR:
            self._discovered_port = MAGNETAR_PORT
            self._discovered_name = "Magnetar"
            self.context["title_placeholders"] = {"name": self._discovered_name}
            return await self.async_step_magnetar_confirm()

        self._discovered_port = int(discovery_info[CONF_PORT])
        self._discovered_name = str(discovery_info[CONF_NAME])
        self.context["title_placeholders"] = {"name": self._discovered_name}
        return await self.async_step_discovery_confirm()

    @override
    async def async_step_ssdp(self, discovery_info: SsdpServiceInfo) -> ConfigFlowResult:
        """Handle a player found via UPnP/SSDP.

        BDP-103/103D/105/105D and UDP-203/205 expose a standard UPnP device
        (for DLNA) that Home Assistant's core ``ssdp`` component already
        discovers on its own, matched here by manufacturer + modelName (see
        manifest.json).
        """
        model = _SSDP_MODEL_MAP.get(str(discovery_info.upnp.get(ATTR_UPNP_MODEL_NAME)))
        host = urlsplit(discovery_info.ssdp_location or "").hostname
        if model is None or not host:
            return self.async_abort(reason="not_oppo_player")

        await self.async_set_unique_id(host.lower())
        self._abort_if_unique_id_configured()

        self._discovered_host = host
        self._discovered_port = MODEL_DEFAULT_PORTS[model]
        self._discovered_name = str(discovery_info.upnp.get(ATTR_UPNP_FRIENDLY_NAME) or f"Oppo {model}")
        self._discovered_model = model
        self.context["title_placeholders"] = {"name": self._discovered_name}
        return await self.async_step_discovery_confirm()

    async def async_step_discovery_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Confirm setup of an Oppo player found via SSDP, the UDP-20X broadcast, or the legacy OREMOTE probe.

        All three mechanisms yield a host, a control port and a name (see
        ``discovery.py`` and ``async_step_ssdp``), so they share this one
        confirmation step; only the model choices offered differ -- fixed to
        a single option when SSDP already named the exact model, otherwise
        narrowed by the discovered port.
        """
        errors: dict[str, str] = {}

        if self._discovered_model is not None:
            model_candidates = [self._discovered_model]
            default_model = self._discovered_model
        else:
            model_candidates = _model_candidates_for_port(self._discovered_port)
            default_model = _default_model_for_discovery(self._discovered_name, self._discovered_port)

        if user_input is not None:
            data: dict[str, Any] = {
                CONF_HOST: self._discovered_host,
                CONF_PORT: self._discovered_port,
                CONF_NAME: user_input[CONF_NAME],
                CONF_MODEL: user_input[CONF_MODEL],
                CONF_MAC: "",
            }
            errors = await self._async_validate_oppo(data)
            if not errors:
                return self.async_create_entry(title=data[CONF_NAME], data=data)

        return self.async_show_form(
            step_id="discovery_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME, default=self._discovered_name): str,
                    vol.Required(CONF_MODEL, default=default_model): vol.In(model_candidates),
                }
            ),
            description_placeholders={"host": self._discovered_host, "port": str(self._discovered_port)},
            errors=errors,
        )

    async def async_step_magnetar_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Confirm setup of a Magnetar player found via the SSDP probe.

        The probe only yields an IP (see ``discovery.py``): the model, name
        and port are fixed, but the MAC address needed for Wake-on-LAN isn't
        in the reply, so it's still asked for here.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            data: dict[str, Any] = {
                CONF_HOST: self._discovered_host,
                CONF_PORT: self._discovered_port,
                CONF_NAME: user_input[CONF_NAME],
                CONF_MODEL: MODEL_MAGNETAR,
                CONF_MAC: user_input[CONF_MAC],
            }
            errors = await self._async_validate_magnetar(data)
            if not errors:
                return self.async_create_entry(title=data[CONF_NAME], data=data)

        return self.async_show_form(
            step_id="magnetar_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME, default=self._discovered_name): str,
                    vol.Required(CONF_MAC, default=""): str,
                }
            ),
            description_placeholders={"host": self._discovered_host},
            errors=errors,
        )

    @staticmethod
    async def _async_validate_oppo(user_input: dict[str, Any]) -> dict[str, str]:
        """Validate an Oppo entry by querying power status.

        Pre-20X models (BDP-83/93/95/103/105) frame commands with the IP
        ``REMOTE`` protocol and listen on model-specific ports. When the user
        leaves the port at the UDP-20X default, substitute the model's port;
        an explicit value is respected so non-standard setups can override it.
        """
        model = user_input[CONF_MODEL]
        use_remote = model in PRE_20X_MODELS
        if use_remote and user_input[CONF_PORT] == DEFAULT_PORT:
            user_input[CONF_PORT] = MODEL_DEFAULT_PORTS[model]
        client = OppoClient(user_input[CONF_HOST], port=user_input[CONF_PORT], use_remote_framing=use_remote)
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
            # Always tear down - connect() may have left a partial transport
            # open (e.g. if setsockopt failed after the writer was created).
            await client.disconnect()

    @staticmethod
    async def _async_validate_magnetar(user_input: dict[str, Any]) -> dict[str, str]:
        """Validate a Magnetar entry: require a MAC and confirm the port opens.

        Magnetar players answer commands with ``ack`` only - there is no query
        to confirm identity, so a successful TCP connection is the strongest
        check available. The port defaults to the Magnetar control port (8102)
        when the user leaves the Oppo default untouched, but any explicit value
        is respected so non-standard setups can override it.
        """
        if parse_mac(user_input.get(CONF_MAC, "")) is None:
            return {CONF_MAC: "invalid_mac"}

        if user_input[CONF_PORT] == DEFAULT_PORT:
            user_input[CONF_PORT] = MAGNETAR_PORT
        port = user_input[CONF_PORT]
        client = MagnetarClient(user_input[CONF_HOST], user_input[CONF_MAC], port=port)
        try:
            if await client.connect():
                return {}
            return {"base": "cannot_connect"}
        except Exception:
            _LOGGER.exception("Unexpected exception during connection test")
            return {"base": "cannot_connect"}
        finally:
            await client.disconnect()
