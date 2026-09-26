"""The Oppo/Magnetar integration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.const import Platform

from .discovery import async_start_oppo_discovery, start_magnetar_discovery

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.typing import ConfigType

PLATFORMS: list[Platform] = [Platform.MEDIA_PLAYER]


async def async_setup(hass: HomeAssistant, _config: ConfigType) -> bool:
    """Set up the Oppo/Magnetar integration.

    Starts discovery for the whole domain, rather than per config entry, so
    it also surfaces players before the first one is configured.
    """
    await async_start_oppo_discovery(hass)
    start_magnetar_discovery(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Blu-ray player from a config entry."""
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
