"""
Govee LAN (Wi-Fi) DataUpdateCoordinator.

GoveeLANCoordinator - manages a govee-local-api controller and device for a
                      single LAN device, pushing state updates to listeners.
"""

from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timedelta
import logging
from typing import TYPE_CHECKING, Any

from govee_local_api import GoveeController, GoveeDevice
from govee_local_api.message import PtRealMessage
from homeassistant.components.light.const import ColorMode
from homeassistant.core import callback
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers.event import async_track_time_interval

if TYPE_CHECKING:
    from homeassistant.core import CALLBACK_TYPE, HomeAssistant

from .coordinator import GoveeCoordinator
from .govee import GoveeHelper

_LOGGER = logging.getLogger(__name__)

# Consecutive missed 10 s update polls (+ margin) before a bound device is
# considered offline.
LAN_OFFLINE_AFTER_SECONDS: int = 35
# How long a discovered-but-never-responding device is given before we
# suspect it needs the LAN API re-enabled in the Govee app.
LAN_NOT_RESPONDING_AFTER_SECONDS: int = 60
LAN_LIVENESS_CHECK_INTERVAL: int = 10


# ── GoveeLANCoordinator ──────────────────────────────────────────────────────


class GoveeLANCoordinator(GoveeCoordinator):
    """Manages a govee-local-api controller and device for a single LAN (Wi-Fi) device.

    The coordinator is constructed immediately from config-entry data (IP + SKU)
    without waiting for the device to respond, so a powered-off device never
    prevents the config entry from loading.  ``self.device`` is bound lazily as
    soon as discovery sees it, and a liveness timer (based on
    ``GoveeDevice.lastseen``) marks the device unavailable again if it goes
    quiet, without requiring an HA restart in either direction.
    """

    def __init__(self, hass: HomeAssistant, ip: str, sku: str) -> None:
        super().__init__(hass, _LOGGER, f"Govee {sku} ({ip})")
        self.ip = ip
        self.sku = sku
        self.device: GoveeDevice | None = None
        self.controller: GoveeController | None = None

        self._bound_at: datetime | None = None
        self._unsub_liveness: CALLBACK_TYPE | None = None
        self._issue_key = f"lan_not_responding_{sku}_{ip.replace('.', '_')}"

    # ── Factory ───────────────────────────────────────────────────────────────

    @classmethod
    async def async_create(cls, hass: HomeAssistant, ip: str, sku: str) -> GoveeLANCoordinator:
        """Build a coordinator for *ip*/*sku* and start discovery, without blocking on it."""
        coord = cls(hass, ip, sku)

        controller = GoveeController(
            discovered_callback=coord._on_discovered,
            discovery_enabled=False,
            update_enabled=True,
            update_interval=LAN_LIVENESS_CHECK_INTERVAL,
        )
        controller.add_device_to_discovery_queue(ip)

        try:
            await controller.start()
        except OSError as err:
            controller.cleanup()
            # A host-level problem (socket bind), not a device problem - HA
            # should retry entry setup, unlike a merely-offline device.
            raise ConfigEntryNotReady(f"Could not bind LAN API socket: {err}") from err

        coord.controller = controller
        coord._unsub_liveness = async_track_time_interval(
            hass, coord._check_liveness, timedelta(seconds=LAN_LIVENESS_CHECK_INTERVAL)
        )
        return coord

    @callback
    def _on_discovered(self, device: GoveeDevice, is_new: bool) -> bool:
        """Bind (or rebind) the device once discovery finds *ip*."""
        if device.ip != self.ip:
            return True
        if self.device is not device:
            self.device = device
            self._bound_at = datetime.now()
            device.set_update_callback(self._on_device_update)
            if self.controller is not None:
                self.controller.send_update_message()
        return True

    # ── Public coordinator interface ─────────────────────────────────────────

    @property
    def unique_device_id(self) -> str:
        """Unique identifier for this device (SKU, from config-entry data)."""
        return self.sku

    @property
    def device_key(self) -> str:
        """SKU, used for effect list loading and device labelling."""
        return self.sku

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
        if self._unsub_liveness:
            self._unsub_liveness()
            self._unsub_liveness = None
        if self.device:
            self.device.set_update_callback(None)
        if self.controller:
            self.controller.cleanup()
        self._clear_repair_issue(self._issue_key)

    async def async_setup(self) -> None:
        """Send an initial state-query to the device, if already bound."""
        if self.controller is not None:
            self.controller.send_update_message()

    async def async_load_effects(self, config_dir: str) -> None:
        """Load the scene/effect list in an executor thread and store results."""
        scenes, effect_map, effect_list = await self.hass.async_add_executor_job(
            GoveeHelper.build_model_effect_list, self.device_key, config_dir
        )
        self._scenes_data = scenes
        self._effect_map = effect_map
        self._effect_list = effect_list

    def _require_device(self) -> GoveeDevice:
        """Return the bound GoveeDevice, or raise if the device is unreachable."""
        if self.device is None:
            raise HomeAssistantError(f"Govee device at {self.ip} is unreachable")
        return self.device

    async def async_turn_on(self) -> None:
        await self._require_device().turn_on()

    async def async_turn_off(self) -> None:
        await self._require_device().turn_off()

    async def async_set_brightness(self, brightness: int) -> None:
        await self._require_device().set_brightness(round(brightness * 100 / 255))

    async def async_set_rgb_color(self, r: int, g: int, b: int) -> None:
        await self._require_device().set_rgb_color(r, g, b)

    async def async_set_color_temp(self, kelvin: int) -> None:
        await self._require_device().set_temperature(kelvin)

    async def async_set_effect(self, ptreal_cmds: list[str]) -> None:
        """Apply a ptReal scene effect and power the device on."""
        device = self._require_device()
        raw_packets: list[bytes | bytearray] = [
            bytearray(base64.b64decode(c)) for c in ptreal_cmds
        ]
        self.send_ptreal_scene(raw_packets)
        await device.turn_on()

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
        device = self._require_device()
        if self.controller is None or self.controller._transport is None:  # type: ignore[attr-defined]
            _LOGGER.warning("LAN controller transport not ready; cannot send scene")
            return
        msg = PtRealMessage(ptreal_cmds, do_checksum=False)
        transport = self.controller._transport  # type: ignore[attr-defined]
        port = self.controller._device_command_port  # type: ignore[attr-defined]
        transport.sendto(bytes(msg), (device.ip, port))

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
        self._clear_repair_issue(self._issue_key)
        self.async_set_updated_data(self._state_snapshot())

    @callback
    def _check_liveness(self, _now: datetime) -> None:
        """Periodic check: mark the device unavailable if it has gone quiet.

        Also raises a Repairs issue when the device is discovered (answers
        broadcast scans) but never answers status requests - typically means
        the LAN API needs to be re-enabled for it in the Govee app.
        """
        if self.device is None:
            return

        if self._available:
            age = (datetime.now() - self.device.lastseen).total_seconds()
            if age > LAN_OFFLINE_AFTER_SECONDS:
                _LOGGER.info("LAN device %s (%s) is unreachable", self.sku, self.ip)
                self._available = False
                self.async_set_updated_data(self._state_snapshot())
            return

        if self._bound_at is not None:
            since_bound = (datetime.now() - self._bound_at).total_seconds()
            if since_bound > LAN_NOT_RESPONDING_AFTER_SECONDS:
                self._raise_repair_issue(
                    self._issue_key, "lan_not_responding", sku=self.sku, ip=self.ip
                )

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
