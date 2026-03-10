from __future__ import annotations

import asyncio

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.const import CONF_MODEL, MAJOR_VERSION, MINOR_VERSION

from govee_local_api import GoveeController, GoveeDevice

from .govee import GoveeBLE, GoveeBLECoordinator
from .const import DOMAIN, CONF_LAN_IP, CONF_LAN_DEVICE_ID, CONF_LAN_SKU
import logging

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[str] = ["light"]

class Hub:
    def __init__(
        self,
        address: str = None,
        ble_coordinator: GoveeBLECoordinator | None = None,
        lan_device: GoveeDevice | None = None,
        lan_controller: GoveeController | None = None,
    ) -> None:
        self.address = address
        self.ble_coordinator = ble_coordinator
        self.lan_device = lan_device
        self.lan_controller = lan_controller


async def async_setup_ble(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Govee BLE"""
    address = entry.unique_id
    assert address is not None
    ble_device = bluetooth.async_ble_device_from_address(hass, address.upper(), True)
    if not ble_device:
        raise ConfigEntryNotReady(
            f"Could not find Govee BLE device with address {address}"
        )

    coordinator = GoveeBLECoordinator(hass, address, entry.data["model"])
    entry.runtime_data = Hub(address=address, ble_coordinator=coordinator)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_setup_lan(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a Govee LAN (Wi-Fi) device using govee-local-api."""
    ip = entry.data[CONF_LAN_IP]

    device_found: asyncio.Event = asyncio.Event()

    def _on_discovered(device: GoveeDevice, is_new: bool) -> bool:
        if device.ip == ip:
            device_found.set()
        return True

    controller = GoveeController(
        discovered_callback=_on_discovered,
        discovery_enabled=False,
        update_enabled=True,
        update_interval=10,
    )
    controller.add_device_to_discovery_queue(ip)

    try:
        await controller.start()
    except OSError as err:
        controller.cleanup()
        raise ConfigEntryNotReady(f"Could not bind LAN API socket: {err}") from err

    try:
        await asyncio.wait_for(device_found.wait(), timeout=5.0)
    except asyncio.TimeoutError:
        controller.cleanup()
        raise ConfigEntryNotReady(
            f"Could not reach Govee LAN device at {ip} — check IP address and that the LAN API is enabled in the Govee app."
        )

    lan_device = controller.get_device_by_ip(ip)
    if lan_device is None:
        controller.cleanup()
        raise ConfigEntryNotReady(f"Device at {ip} responded but could not be registered")

    entry.runtime_data = Hub(lan_device=lan_device, lan_controller=controller)
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
    hub: Hub = entry.runtime_data
    if hub is not None and hub.lan_controller is not None:
        hub.lan_controller.cleanup()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    if (MAJOR_VERSION, MINOR_VERSION) < (2025, 7):
        raise Exception("unsupported hass version, need at least 2025.7")
    return True
