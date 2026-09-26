from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.const import CONF_MODEL, Platform
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import CONF_LAN_DEVICE_ID, CONF_LAN_IP, CONF_LAN_SKU, DOMAIN
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


def _lan_unique_id(entry: ConfigEntry) -> str:
    """The LAN device's own ID: the entry's unique ID"""
    if entry.unique_id:
        return entry.unique_id
    device_id: str = entry.data.get(CONF_LAN_DEVICE_ID) or ""
    if device_id:
        return device_id.replace(":", "").upper()
    return f"lan_{entry.data[CONF_LAN_IP].replace('.', '_')}"


async def _async_migrate_lan_unique_ids(
    hass: HomeAssistant, entry: ConfigEntry, new_id: str
) -> None:
    """Re-key a LAN device from its SKU to *new_id*."""
    sku: str = entry.data[CONF_LAN_SKU]
    if sku == new_id:
        return
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_device(identifiers={(DOMAIN, sku)})
    if (
        device is not None
        and entry.entry_id in device.config_entries
        and dev_reg.async_get_device(identifiers={(DOMAIN, new_id)}) is None
    ):
        dev_reg.async_update_device(device.id, new_identifiers={(DOMAIN, new_id)})

    @callback
    def _rekey(entity_entry: er.RegistryEntry) -> dict[str, Any] | None:
        unique_id = entity_entry.unique_id
        if unique_id == sku or unique_id.startswith(f"{sku}_"):
            return {"new_unique_id": new_id + unique_id[len(sku):]}
        return None

    await er.async_migrate_entries(hass, entry.entry_id, _rekey)


async def async_setup_lan(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a Govee LAN (Wi-Fi) device using govee-local-api."""
    ip = entry.data[CONF_LAN_IP]
    sku = entry.data[CONF_LAN_SKU]
    unique_id = _lan_unique_id(entry)
    await _async_migrate_lan_unique_ids(hass, entry, unique_id)
    entry.runtime_data = await GoveeLANCoordinator.async_create(hass, ip, sku, unique_id)
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


