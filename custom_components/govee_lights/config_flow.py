from __future__ import annotations

import asyncio
from typing import Any

import voluptuous as vol
from homeassistant import config_entries

from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigFlow
from homeassistant.const import (CONF_ADDRESS, CONF_MODEL, CONF_TYPE)
from homeassistant.data_entry_flow import FlowResult

from govee_local_api import GoveeController, GoveeDevice

from .const import DOMAIN, CONF_TYPE_BLE, CONF_TYPE_LAN, CONF_LAN_IP, CONF_LAN_DEVICE_ID, CONF_LAN_SKU
from .govee import GoveeBLE


def _model_from_ble_name(name: str) -> str:
    """Extract the model number from a Govee BLE device name.

    Device names follow the pattern '<prefix>_<MODEL>_<suffix>',
    e.g. 'Govee_H617C_5E1B', 'GBK_H6076B_AA01', 'ihoment_H617C_3C2D'.
    Returns the middle segment, or an empty string if the pattern doesn't match.
    """
    parts = name.split("_")
    if len(parts) >= 3:
        return parts[1]
    return ""


class GoveeConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._config_type: str = ''
        self._discovery_info: None = None
        self._discovered_device: None = None
        self._discovered_devices: dict[str, str] = {}
        self._discovered_service_infos: dict[str, BluetoothServiceInfoBleak] = {}
        self._available_config_types: dict[str, str] = {
            CONF_TYPE_BLE: 'BLE',
            CONF_TYPE_LAN: 'LAN (Wi-Fi)',
        }
        self._lan_discovered: dict[str, Any] = {}  # display-name → LanDevice

    async def async_step_bluetooth(
            self, discovery_info: BluetoothServiceInfoBleak
    ) -> FlowResult:
        """Handle the bluetooth discovery step."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self._discovery_info = discovery_info
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
            self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirm discovery."""
        assert self._discovery_info is not None
        discovery_info = self._discovery_info
        title = discovery_info.name
        inferred_model = _model_from_ble_name(title)
        placeholders = {"name": title, "model": inferred_model or "Device model"}
        self.context["title_placeholders"] = placeholders
        errors: dict[str, str] = {}

        if user_input is not None:
            model = user_input[CONF_MODEL]
            try:
                client = await GoveeBLE.connect_to(discovery_info.device, title)
                await client.disconnect()
            except Exception:
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(title=title, data={CONF_MODEL: model})

        self._set_confirm_only()
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders=placeholders,
            data_schema=vol.Schema({
                vol.Required(CONF_MODEL, default=inferred_model): str
            }),
            errors=errors,
        )

    async def async_step_ble(
            self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors = {}
        current_addresses = self._async_current_ids()
        for discovery_info in async_discovered_service_info(self.hass, False):
            address = discovery_info.address
            if address in current_addresses or address in self._discovered_devices:
                continue
            self._discovered_devices[address] = discovery_info.name
            self._discovered_service_infos[address] = discovery_info

        if (user_input is not None and CONF_ADDRESS in user_input and user_input[CONF_ADDRESS] is not None
                and CONF_MODEL in user_input and user_input[CONF_MODEL] is not None):
            address = user_input[CONF_ADDRESS]
            model = user_input[CONF_MODEL]
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            disc = self._discovered_service_infos.get(address)
            if disc is not None:
                try:
                    client = await GoveeBLE.connect_to(disc.device, disc.name)
                    await client.disconnect()
                except Exception:
                    errors["base"] = "cannot_connect"
            if not errors:
                return self.async_create_entry(
                    title=self._discovered_devices[address], data={CONF_MODEL: model}
                )

        # Pre-fill model from the selected address (on error re-show) or first device.
        preselected_address = (user_input or {}).get(CONF_ADDRESS, "")
        if not preselected_address and self._discovered_devices:
            preselected_address = next(iter(self._discovered_devices))
        inferred_model = _model_from_ble_name(
            self._discovered_devices.get(preselected_address, "")
        )

        return self.async_show_form(
            step_id="ble",
            data_schema=vol.Schema({
                vol.Required(CONF_ADDRESS): vol.In(self._discovered_devices),
                vol.Required(CONF_MODEL, default=inferred_model): str
            }),
            errors=errors
        )

    async def async_step_user(
            self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None and user_input[CONF_TYPE] == CONF_TYPE_BLE:
            return await self.async_step_ble(user_input)
        if user_input is not None and user_input[CONF_TYPE] == CONF_TYPE_LAN:
            return await self.async_step_lan()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_TYPE): vol.In(self._available_config_types),
            }),
        )

    async def async_step_lan(
            self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle LAN device setup.

        On the first invocation (user_input=None) a multicast discovery scan is
        performed using govee-local-api.  The form shows discovered devices in a
        dropdown plus optional manual IP / model fields for devices that were not
        auto-discovered.
        """
        errors: dict[str, str] = {}

        # Run discovery exactly once per flow (cache results in self._lan_discovered).
        if not self._lan_discovered:
            found = await _discover_lan_devices()
            self._lan_discovered = {
                f"{d.sku} ({d.ip})": d for d in found
            }

        if user_input is not None:
            choice = user_input.get("lan_device_key", "")
            manual_ip = (user_input.get(CONF_LAN_IP) or "").strip()
            manual_sku = (user_input.get(CONF_LAN_SKU) or "").strip()

            if choice and choice in self._lan_discovered:
                dev: GoveeDevice = self._lan_discovered[choice]
                ip: str = dev.ip
                device_id: str = dev.fingerprint
                sku: str = dev.sku
            elif manual_ip and manual_sku:
                ip = manual_ip
                device_id = ""
                sku = manual_sku
            else:
                errors["base"] = "no_device_selected"

            if not errors:
                # Verify we can reach the device before creating the entry.
                reachable = await _test_lan_connectivity(ip)
                if not reachable:
                    errors["base"] = "cannot_connect"
                else:
                    uid = (
                        device_id.replace(":", "").upper()
                        if device_id
                        else f"lan_{ip.replace('.', '_')}"
                    )
                    await self.async_set_unique_id(uid, raise_on_progress=False)
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title=f"{sku} (LAN)",
                        data={
                            CONF_LAN_IP: ip,
                            CONF_LAN_DEVICE_ID: device_id,
                            CONF_LAN_SKU: sku,
                        },
                    )

        schema_fields: dict = {}
        if self._lan_discovered:
            schema_fields[vol.Optional("lan_device_key")] = vol.In(
                list(self._lan_discovered.keys())
            )
        schema_fields[vol.Optional(CONF_LAN_IP)] = str
        schema_fields[vol.Optional(CONF_LAN_SKU)] = str

        return self.async_show_form(
            step_id="lan",
            data_schema=vol.Schema(schema_fields),
            description_placeholders={"discovered": str(len(self._lan_discovered))},
            errors=errors,
        )


async def _discover_lan_devices(timeout: float = 5.0) -> list[GoveeDevice]:
    """Discover Govee LAN devices on the local network via multicast."""
    found: list[GoveeDevice] = []

    def _on_discovered(device: GoveeDevice, is_new: bool) -> bool:
        found.append(device)
        return True

    controller = GoveeController(
        discovered_callback=_on_discovered,
        discovery_enabled=True,
        discovery_interval=60,
        update_enabled=False,
    )
    try:
        await controller.start()
        await asyncio.sleep(timeout)
    except OSError:
        pass
    finally:
        controller.cleanup()

    return found


async def _test_lan_connectivity(ip: str, timeout: float = 5.0) -> bool:
    """Return True if a Govee LAN device at *ip* responds within *timeout* seconds."""
    device_found: asyncio.Event = asyncio.Event()

    def _on_discovered(device: GoveeDevice, is_new: bool) -> bool:
        if device.ip == ip:
            device_found.set()
        return True

    controller = GoveeController(
        discovered_callback=_on_discovered,
        discovery_enabled=False,
        update_enabled=False,
    )
    controller.add_device_to_discovery_queue(ip)
    try:
        await controller.start()
        await asyncio.wait_for(device_found.wait(), timeout=timeout)
        return True
    except (asyncio.TimeoutError, OSError):
        return False
    finally:
        controller.cleanup()
