"""
Govee LAN (Wi-Fi) DataUpdateCoordinator.

GoveeLANCoordinator - manages a govee-local-api controller and device for a
                      single LAN device, pushing state updates to listeners.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import TYPE_CHECKING, Any

from govee_local_api import GoveeController, GoveeDevice
from govee_local_api.message import PtRealMessage
from homeassistant.components.light.const import ColorMode
from homeassistant.exceptions import ConfigEntryNotReady

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

from .coordinator import GoveeCoordinator
from .govee import GoveeHelper

_LOGGER = logging.getLogger(__name__)


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  GoveeLANCoordinator                                                     ║
# ╚══════════════════════════════════════════════════════════════════════════╝


class GoveeLANCoordinator(GoveeCoordinator):
    """Manages a govee-local-api controller and device for a single LAN (Wi-Fi) device."""

    def __init__(
        self,
        hass: HomeAssistant,
        device: GoveeDevice,
        controller: GoveeController,
    ) -> None:
        super().__init__(hass, _LOGGER, f"Govee {device.sku} ({device.ip})")
        self.device = device
        self.controller = controller

        # Last-known device state (updated from GoveeDevice callbacks)

    # ── Factory ───────────────────────────────────────────────────────────────

    @classmethod
    async def async_create(cls, hass: HomeAssistant, ip: str) -> GoveeLANCoordinator:
        """Discover the device at *ip* and return a coordinator, or raise ConfigEntryNotReady."""
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
        except TimeoutError:
            controller.cleanup()
            raise ConfigEntryNotReady(
                f"Could not reach Govee LAN device at {ip} - "
                "check IP address and that the LAN API is enabled in the Govee app."
            ) from None

        device = controller.get_device_by_ip(ip)
        if device is None:
            controller.cleanup()
            raise ConfigEntryNotReady(f"Device at {ip} responded but could not be registered")

        coord = cls(hass, device, controller)
        device.set_update_callback(coord._on_device_update)
        return coord

    # ── Public coordinator interface ─────────────────────────────────────────

    @property
    def unique_device_id(self) -> str:
        """Unique identifier for this device (device SKU)."""
        return self.device.sku

    @property
    def device_key(self) -> str:
        """SKU, used for effect list loading and device labelling."""
        return self.device.sku

    @property
    def current_rgb_color(self) -> tuple[int, int, int] | None:
        return self.rgb_color

    @property
    def brightness(self) -> int:
        """Brightness in HA scale (0-255)."""
        return round(self.brightness_raw * 255 / 100)

    @property
    def supported_color_modes(self) -> set[ColorMode]:
        return {ColorMode.RGB, ColorMode.COLOR_TEMP}

    @property
    def min_color_temp_kelvin(self) -> int | None:
        return 2000

    @property
    def max_color_temp_kelvin(self) -> int | None:
        return 9000

    @property
    def supports_music_modes(self) -> bool:
        return False

    @property
    def setup_in_background(self) -> bool:
        return False

    @property
    def inferred_effect(self) -> str | None:
        return None

    def cleanup(self) -> None:
        self.device.set_update_callback(None)
        self.controller.cleanup()

    async def async_setup(self) -> None:
        """Send an initial state-query to the device."""
        self.controller.send_update_message()

    async def async_load_effects(self, config_dir: str) -> None:
        """Load the scene/effect list in an executor thread and store results."""
        scenes, effect_map, effect_list = await self.hass.async_add_executor_job(
            GoveeHelper.build_model_effect_list, self.device_key, config_dir
        )
        self._scenes_data = scenes
        self._effect_map = effect_map
        self._effect_list = effect_list

    async def async_turn_on(self) -> None:
        await self.device.turn_on()

    async def async_turn_off(self) -> None:
        await self.device.turn_off()

    async def async_set_brightness(self, brightness: int) -> None:
        await self.device.set_brightness(round(brightness * 100 / 255))

    async def async_set_rgb_color(self, r: int, g: int, b: int) -> None:
        await self.device.set_rgb_color(r, g, b)

    async def async_set_color_temp(self, kelvin: int) -> None:
        await self.device.set_temperature(kelvin)

    async def async_set_effect(self, ptreal_cmds: list[str]) -> None:
        """Apply a ptReal scene effect and power the device on."""
        raw_packets: list[bytes | bytearray] = [
            bytearray(base64.b64decode(c)) for c in ptreal_cmds
        ]
        self.send_ptreal_scene(raw_packets)
        await self.device.turn_on()

    async def async_apply_effect(self, effect: str) -> None:
        """Apply a named scene effect."""
        if not self._scenes_data or not self._effect_map:
            _LOGGER.warning("Effect map not loaded yet, skipping effect %r", effect)
            return
        if effect not in self._effect_map:
            _LOGGER.warning(
                "Effect %r not found in effect map. Available: %s",
                effect, list(self._effect_map.keys())[:5],
            )
            return
        scene_entry: dict[str, Any] = self._scenes_data[self._effect_map[effect]]
        ptreal_cmds: list[str] = scene_entry["ptreal_cmds"]
        _LOGGER.debug("Sending effect %r: %d packets", effect, len(ptreal_cmds))
        await self.async_set_effect(ptreal_cmds)

    # ── Discovery helpers (used by config_flow) ──────────────────────────────

    @staticmethod
    async def discover_devices(scan_timeout: float = 5.0) -> list[GoveeDevice]:
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
            await asyncio.sleep(scan_timeout)
        except OSError:
            pass
        finally:
            controller.cleanup()

        return found

    @staticmethod
    async def test_connectivity(ip: str, connect_timeout: float = 5.0) -> bool:
        """Return True if a Govee LAN device at *ip* responds within *connect_timeout* seconds."""
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
            await asyncio.wait_for(device_found.wait(), timeout=connect_timeout)
            return True
        except (TimeoutError, OSError):
            return False
        finally:
            controller.cleanup()

    # ── LAN internals ─────────────────────────────────────────────────────

    def send_ptreal_scene(self, ptreal_cmds: list[bytes | bytearray]) -> None:
        """Send all ptReal packets as a single UDP datagram via the controller transport."""
        transport = self.controller._transport  # type: ignore[attr-defined]
        if transport is None:
            _LOGGER.warning("LAN controller transport not ready; cannot send scene")
            return
        msg = PtRealMessage(ptreal_cmds, do_checksum=False)
        port = self.controller._device_command_port  # type: ignore[attr-defined]
        transport.sendto(bytes(msg), (self.device.ip, port))

    def _on_device_update(self, device: GoveeDevice) -> None:
        """Called by the govee-local-api controller when a devStatus response arrives."""
        self.is_on = device.on
        self.brightness_raw = device.brightness
        if device.temperature_color > 0:
            self.color_temp_kelvin = device.temperature_color
            self.rgb_color = None
        else:
            self.rgb_color = device.rgb_color
            self.color_temp_kelvin = None
        if not self._available:
            _LOGGER.info("LAN device %s (%s) is available", device.sku, device.ip)
            self._available = True
        self.async_set_updated_data(self._state_snapshot())

    async def _async_update_data(self) -> dict[str, Any]:
        """Required by DataUpdateCoordinator; returns a snapshot of current state."""
        return self._state_snapshot()

    def _state_snapshot(self) -> dict[str, Any]:
        return {
            "is_on": self.is_on,
            "brightness_raw": self.brightness_raw,
            "rgb_color": self.rgb_color,
            "color_temp_kelvin": self.color_temp_kelvin,
        }
