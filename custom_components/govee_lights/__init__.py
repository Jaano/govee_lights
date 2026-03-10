from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.components import bluetooth
from homeassistant.const import CONF_MODEL, Platform
from homeassistant.exceptions import ConfigEntryNotReady

from .const import CONF_LAN_IP
from .govee_ble import GoveeBLECoordinator
from .govee_lan import GoveeLANCoordinator

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .coordinator import GoveeCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.LIGHT]

async def async_setup_ble(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Govee BLE"""
    address = entry.unique_id
    assert address is not None
    ble_device = bluetooth.async_ble_device_from_address(hass, address.upper(), True)
    if not ble_device:
        raise ConfigEntryNotReady(
            f"Could not find Govee BLE device with address {address}"
        )

    entry.runtime_data = GoveeBLECoordinator(hass, address, entry.data["model"])
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_setup_lan(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a Govee LAN (Wi-Fi) device using govee-local-api."""
    ip = entry.data[CONF_LAN_IP]
    entry.runtime_data = await GoveeLANCoordinator.async_create(hass, ip)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Govee device from a config entry."""
    if entry.data.get(CONF_MODEL):
        await async_setup_ble(hass, entry)
    elif entry.data.get(CONF_LAN_IP):
        await async_setup_lan(hass, entry)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    hub: GoveeCoordinator = entry.runtime_data
    hub.cleanup()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


