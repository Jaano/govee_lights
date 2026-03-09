from __future__ import annotations

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.const import (CONF_API_KEY, CONF_MODEL, MAJOR_VERSION, MINOR_VERSION)
from homeassistant.helpers.storage import Store

from .govee_api import GoveeAPI
from .govee_ble import GoveeBLE

from .const import DOMAIN
import logging

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[str] = ["light"]

class Hub:
    def __init__(self, api: GoveeAPI | None, address: str = None, devices: list = None) -> None:
        """Init Govee dummy hub."""
        self.api = api
        self.devices = devices
        self.address = address

async def async_setup_api(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Set up Govee API"""
    assert config_entry.data.get(CONF_API_KEY) is not None
    await internal_api_setup(hass, config_entry)

async def internal_api_setup(hass: HomeAssistant, entry: ConfigEntry):
    api_key = entry.data.get(CONF_API_KEY)
    api = GoveeAPI(api_key)

    try:
        devices = await api.list_devices()
    except PermissionError as err:
        raise ConfigEntryAuthFailed("Invalid Govee API key") from err
    except Exception as err:
        raise ConfigEntryNotReady("Could not reach Govee API") from err

    _LOGGER.debug("Govee devices: %s", devices)

    store = Store(hass, 1, f"{DOMAIN}/{api_key}.json")
    await store.async_save(devices)
    await internal_cache_setup(hass, api, entry, devices)

UNIQUE_DEVICES = {}

async def internal_cache_setup(
        hass: HomeAssistant, api: GoveeAPI, entry: ConfigEntry, devices: list = None
):
    if devices is None:
        store = Store(hass, 1, f"{DOMAIN}/{entry.data.get(CONF_API_KEY)}.json")
        devices = await store.async_load()
        if devices:
            _LOGGER.debug(f"{len(devices)} devices loaded from cache!")
    entry.runtime_data = Hub(api, devices=devices)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)


def internal_unique_devices(uid: str, devices: list) -> list:
    """For support multiple integrations - bind each device to one integraion.
    To avoid duplicates.
    """
    return [
        device
        for device in devices
        if UNIQUE_DEVICES.setdefault(device["device"], uid) == uid
    ]


async def async_setup_ble(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Govee BLE"""
    address = entry.unique_id
    assert address is not None
    ble_device = bluetooth.async_ble_device_from_address(hass, address.upper(), True)
    if not ble_device:
        raise ConfigEntryNotReady(
            f"Could not find Govee BLE device with address {address}"
        )

    try:
        client = await GoveeBLE.connect_to(ble_device, address)
        await client.disconnect()
    except Exception as err:
        raise ConfigEntryNotReady(
            f"Could not connect to Govee BLE device {address}"
        ) from err

    entry.runtime_data = Hub(None, address=address)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Govee BLE device from a config entry."""
    if entry.data.get(CONF_API_KEY):
        await async_setup_api(hass, entry)
    if entry.data.get(CONF_MODEL):
        await async_setup_ble(hass, entry)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    if (MAJOR_VERSION, MINOR_VERSION) < (2025, 7):
        raise Exception("unsupported hass version, need at least 2025.7")
    return True
