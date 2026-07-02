from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.const import CONF_MODEL, Platform

from .const import CONF_LAN_IP, CONF_LAN_SKU
from .govee_ble import GoveeBLECoordinator
from .govee_lan import GoveeLANCoordinator

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .coordinator import GoveeCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.LIGHT]

async def async_setup_ble(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Govee Lights BLE entry."""
    address = entry.unique_id
    assert address is not None
    entry.runtime_data = GoveeBLECoordinator(hass, address, entry.data["model"])
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_setup_lan(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a Govee LAN (Wi-Fi) device using govee-local-api."""
    ip = entry.data[CONF_LAN_IP]
    sku = entry.data[CONF_LAN_SKU]
    entry.runtime_data = await GoveeLANCoordinator.async_create(hass, ip, sku)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Govee device from a config entry."""
    if entry.data.get(CONF_MODEL):
        await async_setup_ble(hass, entry)
    elif entry.data.get(CONF_LAN_IP):
        await async_setup_lan(hass, entry)
    else:
        return False

    # Connect/query the device without blocking entry setup on it - an
    # unreachable device is a normal runtime state, not a setup failure.
    coordinator: GoveeCoordinator = entry.runtime_data
    if coordinator.setup_in_background:
        entry.async_create_background_task(
            hass,
            coordinator.async_setup(),
            f"govee_lights_initial_connect_{entry.entry_id}",
        )
    else:
        await coordinator.async_setup()

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    hub: GoveeCoordinator = entry.runtime_data
    hub.cleanup()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


