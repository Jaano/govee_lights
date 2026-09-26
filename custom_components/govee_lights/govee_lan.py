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
import time
from typing import TYPE_CHECKING, Any

from govee_local_api import GoveeController, GoveeDevice
from govee_local_api.light_capabilities import GOVEE_LIGHT_CAPABILITIES
from govee_local_api.message import DevStatusMessage, PtRealMessage
from homeassistant.components.light.const import ColorMode
from homeassistant.core import callback
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers.event import async_call_later, async_track_time_interval

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from govee_local_api.message import GoveeMessage
    from homeassistant.core import CALLBACK_TYPE, HomeAssistant

from .const import DOMAIN
from .coordinator import GoveeCoordinator
from .govee import GoveeHelper
from .govee_ble import GoveeBLE

_LOGGER = logging.getLogger(__name__)

# Consecutive missed 10 s update polls (+ margin) before a bound device is
# considered offline.
LAN_OFFLINE_AFTER_SECONDS: int = 35
# How long a discovered-but-never-responding device is given before we
# suspect it needs the LAN API re-enabled in the Govee app.
LAN_NOT_RESPONDING_AFTER_SECONDS: int = 60
LAN_LIVENESS_CHECK_INTERVAL: int = 10

LAN_COMMAND_SPACING: float = 0.1
LAN_VERIFY_DELAY: float = 1.0
LAN_VERIFY_RETRIES: int = 1
LAN_VERIFY_EXPIRY: float = 5.0

_LAN_HUB_KEY = f"{DOMAIN}_lan_hub"
_LAN_HUB_LOCK_KEY = f"{DOMAIN}_lan_hub_lock"


# ── Shared controller ────────────────────────────────────────────────────────


class _LanHub:
    """The one govee-local-api controller shared by every LAN config entry.

    The library listens on UDP 4002 with SO_REUSEPORT, so with a controller
    per entry the kernel hands each reply to a hash-picked one of them, and
    a controller drops replies from devices it doesn't know. Seen live: all
    three LAN devices flapping unreachable once duplicate polling, which
    had masked it, was removed. HA core's govee_light_local shares one
    controller for the same reason. The config flow's scan and
    connectivity test borrow it too, for as long as they run.
    """

    def __init__(self, controller: GoveeController) -> None:
        self.controller = controller
        self.coordinators: dict[str, GoveeLANCoordinator] = {}
        self.listeners: set[Callable[[GoveeDevice], None]] = set()

    @callback
    def on_discovered(self, device: GoveeDevice, is_new: bool) -> bool:
        """Route a scan answer to its entry. Only configured devices are kept
        in the controller's registry - and so polled."""
        for listener in list(self.listeners):
            listener(device)
        coordinator = self.coordinators.get(device.ip)
        if coordinator is None:
            return False
        coordinator._bind(device)
        return True


async def _async_acquire_lan_hub(
    hass: HomeAssistant,
    *,
    coordinator: GoveeLANCoordinator | None = None,
    listener: Callable[[GoveeDevice], None] | None = None,
) -> _LanHub:
    """Get the shared controller, starting it if needed, and register a user.

    The lock only serialises startup; with the hub already running there's
    no await between lookup and registration, so a concurrent release can't
    tear it down in between.
    """
    lock: asyncio.Lock = hass.data.setdefault(_LAN_HUB_LOCK_KEY, asyncio.Lock())
    async with lock:
        hub: _LanHub | None = hass.data.get(_LAN_HUB_KEY)
        if hub is None:
            controller = GoveeController(
                discovery_enabled=False,
                update_enabled=True,
                update_interval=LAN_LIVENESS_CHECK_INTERVAL,
            )
            hub = _LanHub(controller)
            controller.set_device_discovered_callback(hub.on_discovered)
            try:
                await controller.start()
            except OSError as err:
                controller.cleanup()
                # A host-level problem (socket bind), not a device problem -
                # HA should retry entry setup, unlike a merely-offline device.
                raise ConfigEntryNotReady(f"Could not bind LAN API socket: {err}") from err
            hass.data[_LAN_HUB_KEY] = hub
        if coordinator is not None:
            hub.coordinators[coordinator.ip] = coordinator
        if listener is not None:
            hub.listeners.add(listener)
        return hub


@callback
def _release_lan_hub(
    hass: HomeAssistant,
    hub: _LanHub,
    *,
    coordinator: GoveeLANCoordinator | None = None,
    listener: Callable[[GoveeDevice], None] | None = None,
) -> None:
    """Unregister a user; stop the controller once nobody uses it."""
    if coordinator is not None and hub.coordinators.get(coordinator.ip) is coordinator:
        del hub.coordinators[coordinator.ip]
    if listener is not None:
        hub.listeners.discard(listener)
    if not hub.coordinators and not hub.listeners and hass.data.get(_LAN_HUB_KEY) is hub:
        del hass.data[_LAN_HUB_KEY]
        hub.controller.cleanup()


# ── GoveeLANCoordinator ──────────────────────────────────────────────────────


class GoveeLANCoordinator(GoveeCoordinator):
    """Manages a single LAN (Wi-Fi) device on the shared govee-local-api controller.

    The coordinator is constructed immediately from config-entry data (IP + SKU)
    without waiting for the device to respond, so a powered-off device never
    prevents the config entry from loading.  ``self.device`` is bound lazily as
    soon as discovery sees it, and a liveness timer (based on
    ``GoveeDevice.lastseen``) marks the device unavailable again if it goes
    quiet, without requiring an HA restart in either direction.
    """

    def __init__(self, hass: HomeAssistant, ip: str, sku: str, unique_id: str) -> None:
        super().__init__(hass, _LOGGER, f"Govee {sku} ({ip})")
        self.ip = ip
        self.sku = sku
        self._unique_id = unique_id
        self.device: GoveeDevice | None = None
        self.controller: GoveeController | None = None
        self._hub: _LanHub | None = None

        self._bound_at: datetime | None = None
        self._unsub_liveness: CALLBACK_TYPE | None = None
        self._issue_key = f"lan_not_responding_{sku}_{ip.replace('.', '_')}"
        # govee-local-api's per-SKU table, available before the device is
        # bound - segment entities are created at setup, not on discovery.
        capabilities = GOVEE_LIGHT_CAPABILITIES.get(sku)
        self._segment_count = capabilities.segments_count if capabilities else 0
        self.segment_colors = [None] * self._segment_count

        self._send_lock = asyncio.Lock()
        self._last_send = 0.0
        # What verified writes should have left on the device - keys "on",
        # "brightness" (device percent), "rgb", "kelvin" - until a settled
        # devStatus confirms it (see _on_device_update).
        self._expected: dict[str, Any] = {}
        self._settled_at = 0.0
        self._verify_retries = 0
        self._cancel_verify: CALLBACK_TYPE | None = None

    # ── Factory ───────────────────────────────────────────────────────────────

    @classmethod
    async def async_create(
        cls, hass: HomeAssistant, ip: str, sku: str, unique_id: str
    ) -> GoveeLANCoordinator:
        """Build a coordinator for *ip*/*sku* and start discovery, without blocking on it."""
        coord = cls(hass, ip, sku, unique_id)
        coord._hub = await _async_acquire_lan_hub(hass, coordinator=coord)
        coord.controller = coord._hub.controller
        coord.controller.add_device_to_discovery_queue(ip)
        coord._unsub_liveness = async_track_time_interval(
            hass, coord._check_liveness, timedelta(seconds=LAN_LIVENESS_CHECK_INTERVAL)
        )
        return coord

    @callback
    def _bind(self, device: GoveeDevice) -> None:
        """Bind (or rebind) the device the shared controller found at our IP."""
        if self.device is not device:
            self.device = device
            self._bound_at = datetime.now()
            device.set_update_callback(self._on_device_update)
            self._send_status_query()

    # ── Public coordinator interface ─────────────────────────────────────────

    @property
    def unique_device_id(self) -> str:
        """The device's own ID (see __init__._lan_unique_id) - not the SKU,
        which two strips of one model share."""
        return self._unique_id

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
    def segment_count(self) -> int:
        return self._segment_count

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
        if self._cancel_verify:
            self._cancel_verify()
            self._cancel_verify = None
        if self.device:
            self.device.set_update_callback(None)
        if self._hub is not None:
            # Forget the device so a reloaded entry rediscovers and rebinds
            # it; the controller itself stops with its last user.
            self._hub.controller.remove_device_from_discovery_queue(self.ip)
            if self.device is not None:
                self._hub.controller.remove_device(self.device)
            _release_lan_hub(self.hass, self._hub, coordinator=self)
            self._hub = None
        self._clear_repair_issue(self._issue_key)

    async def async_setup(self) -> None:
        """Send an initial state-query to the device, if already bound."""
        self._send_status_query()

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
        await self._paced(self._require_device().turn_on)
        self._expect(True, on=True)

    async def async_turn_off(self) -> None:
        await self._paced(self._require_device().turn_off)
        self._expect(True, on=False)

    async def async_set_brightness(self, brightness: int) -> None:
        device = self._require_device()
        percent = round(brightness * 100 / 255)
        await self._paced(lambda: device.set_brightness(percent))
        self._expect(True, brightness=percent)

    async def async_set_rgb_color(self, r: int, g: int, b: int, *, verify: bool = True) -> None:
        device = self._require_device()
        await self._paced(lambda: device.set_rgb_color(r, g, b))
        self._expect(verify, rgb=(r, g, b), kelvin=None)
        self.rgb_color = (r, g, b)
        self.color_temp_kelvin = None
        self.segment_colors = [(r, g, b)] * self._segment_count

    async def async_set_color_temp(self, kelvin: int, *, verify: bool = True) -> None:
        device = self._require_device()
        await self._paced(lambda: device.set_temperature(kelvin))
        self._expect(verify, kelvin=kelvin, rgb=None)
        self.color_temp_kelvin = kelvin
        self.rgb_color = None
        self.segment_colors = [None] * self._segment_count

    async def async_set_segment_colors(
        self, colors: dict[int, tuple[int, int, int]], *, verify: bool = True
    ) -> None:
        """Paint individual segments in one ptReal datagram.

        Same packets as BLE (govee_ble.py): one per distinct colour, mask bit
        i = segment i, low byte first - govee-local-api's SEGMENT_CODES use
        that layout too, but it sends one datagram per segment, which at
        LAN_COMMAND_SPACING would make painting all 10 segments take 1 s.

        Not verified: devStatus has no per-segment state (see
        _on_device_update), so segment state here is optimistic - what we
        last sent, not what the device reports.
        """
        self._require_device()
        count = self._segment_count
        masks: dict[tuple[int, int, int], int] = {}
        for index, color in colors.items():
            if not 0 <= index < count:
                raise HomeAssistantError(
                    f"Segment {index + 1} out of range for Govee {self.sku} (1-{count})"
                )
            masks[color] = masks.get(color, 0) | (1 << index)
        if not masks:
            return
        packets = [
            GoveeBLE.build_single_packet(
                GoveeBLE.LEDCommand.COLOR,
                [GoveeBLE.LEDMode.SEGMENTS, 0x01, r, g, b,
                 0x00, 0x00, 0x00, 0x00, 0x00, mask & 0xFF, (mask >> 8) & 0xFF],
            )
            for (r, g, b), mask in masks.items()
        ]
        await self._paced(lambda: self._send_raw(PtRealMessage(packets, do_checksum=False)))
        for index, color in colors.items():
            self.segment_colors[index] = color
        self.async_set_updated_data(self._state_snapshot())

    async def async_set_effect(self, ptreal_cmds: list[str]) -> None:
        """Apply a ptReal scene effect and power the device on."""
        device = self._require_device()
        raw_packets = [base64.b64decode(c) for c in ptreal_cmds]
        await self._paced(lambda: self._send_raw(PtRealMessage(raw_packets, do_checksum=False)))
        await self._paced(device.turn_on)
        # A scene's colours aren't in devStatus, so there's nothing to verify
        # them against; only power is.
        self._expected.clear()
        self._expect(True, on=True)
        self.segment_colors = [None] * self._segment_count

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

    # Both borrow the shared controller: a controller of their own would
    # split port 4002's replies with it (see _LanHub).

    @staticmethod
    async def discover_devices(hass: HomeAssistant, scan_timeout: float = 5.0) -> list[GoveeDevice]:
        """Discover Govee LAN devices on the local network via multicast."""
        found: dict[str, GoveeDevice] = {}

        def _on_device(device: GoveeDevice) -> None:
            found[device.fingerprint] = device

        try:
            hub = await _async_acquire_lan_hub(hass, listener=_on_device)
        except HomeAssistantError:
            return []
        try:
            hub.controller.set_discovery_enabled(True)
            await asyncio.sleep(scan_timeout)
        finally:
            hub.controller.set_discovery_enabled(False)
            _release_lan_hub(hass, hub, listener=_on_device)
        return list(found.values())

    @staticmethod
    async def test_connectivity(
        hass: HomeAssistant, ip: str, connect_timeout: float = 5.0
    ) -> bool:
        """Return True if a Govee LAN device at *ip* responds within *connect_timeout* seconds."""
        device_found = asyncio.Event()

        def _on_device(device: GoveeDevice) -> None:
            if device.ip == ip:
                device_found.set()

        try:
            hub = await _async_acquire_lan_hub(hass, listener=_on_device)
        except HomeAssistantError:
            return False
        queued = hub.controller.add_device_to_discovery_queue(ip)
        try:
            await asyncio.wait_for(device_found.wait(), timeout=connect_timeout)
            return True
        except TimeoutError:
            return False
        finally:
            # Leave the queue entry to a configured entry for this IP -
            # removing it would also unregister that entry's device.
            if queued and ip not in hub.coordinators:
                hub.controller.remove_device_from_discovery_queue(ip)
            _release_lan_hub(hass, hub, listener=_on_device)

    # ── LAN internals ─────────────────────────────────────────────────────

    def _send_raw(self, message: GoveeMessage) -> None:
        """Send one message as a single UDP datagram via the controller transport.

        For what govee-local-api has no single-datagram call for: multi-packet
        ptReal (scenes, grouped segments) and one-shot devStatus queries (its
        ``send_update_message`` also re-arms its own 10 s poll, so every call
        would add another polling chain).
        """
        device = self._require_device()
        if self.controller is None or self.controller._transport is None:  # type: ignore[attr-defined]
            _LOGGER.warning("LAN controller transport not ready; cannot send %s", message.command)
            return
        transport = self.controller._transport  # type: ignore[attr-defined]
        port = self.controller._device_command_port  # type: ignore[attr-defined]
        transport.sendto(bytes(message), (device.ip, port))

    async def _paced(self, send: Callable[[], Awaitable[None] | None]) -> None:
        """Run one device write, at least LAN_COMMAND_SPACING after the last."""
        async with self._send_lock:
            wait = self._last_send + LAN_COMMAND_SPACING - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            result = send()
            if result is not None:
                await result
            self._last_send = time.monotonic()

    def _send_status_query(self) -> None:
        """Ask the bound device for a devStatus now (answer: _on_device_update)."""
        if self.device is not None:
            self._send_raw(DevStatusMessage())

    @callback
    def _on_verify_timer(self, _now: datetime) -> None:
        self._cancel_verify = None
        self._send_status_query()

    def _expect(self, verify: bool, **fields: Any) -> None:
        """Record what a write should leave on the device, and (re)arm the check.

        A None value, or an unverified write (fade frames), drops that field's
        expectation instead - the final, verified frame re-adds it.
        """
        for key, value in fields.items():
            if verify and value is not None:
                self._expected[key] = value
            else:
                self._expected.pop(key, None)
        if verify:
            self._verify_retries = LAN_VERIFY_RETRIES
            self._arm_verify()

    def _arm_verify(self) -> None:
        if self._cancel_verify:
            self._cancel_verify()
        self._settled_at = time.monotonic() + LAN_VERIFY_DELAY
        self._cancel_verify = async_call_later(self.hass, LAN_VERIFY_DELAY, self._on_verify_timer)

    async def _async_resend(self, missed: dict[str, Any]) -> None:
        """Replay the fields the device didn't apply, in command order."""
        device = self.device
        if device is None:
            return
        if "on" in missed:
            await self._paced(device.turn_on if missed["on"] else device.turn_off)
        if "brightness" in missed:
            await self._paced(lambda: device.set_brightness(missed["brightness"]))
        if "rgb" in missed:
            await self._paced(lambda: device.set_rgb_color(*missed["rgb"]))
        if "kelvin" in missed:
            await self._paced(lambda: device.set_temperature(missed["kelvin"]))
        self._arm_verify()

    def _verify_pending(self, device: GoveeDevice) -> bool:
        """Check a devStatus against the last verified writes.

        Returns True while the report shouldn't be applied: it may predate
        our writes (not yet settled), or it shows a missed write that is
        being resent - applying it would flash the old state in HA first.
        """
        if not self._expected:
            return False
        now = time.monotonic()
        if now < self._settled_at:
            return True
        if now > self._settled_at + LAN_VERIFY_EXPIRY:
            self._expected.clear()
            return False
        reported = {
            "on": device.on,
            "brightness": device.brightness,
            "rgb": None if device.temperature_color else device.rgb_color,
            "kelvin": device.temperature_color or None,
        }
        missed = {k: v for k, v in self._expected.items() if reported[k] != v}
        if not missed:
            self._expected.clear()
            return False
        if self._verify_retries > 0:
            self._verify_retries -= 1
            # Hold off further checks until the resend has gone out and
            # re-armed; a report landing in between would burn the retry.
            self._settled_at = now + LAN_VERIFY_DELAY
            _LOGGER.debug(
                "Govee %s (%s) missed %s (reported %s); resending",
                self.sku, self.ip, missed, {k: reported[k] for k in missed},
            )
            self.hass.async_create_task(self._async_resend(missed))
            return True
        _LOGGER.warning(
            "Govee %s (%s) did not apply %s after %d resend(s); reported %s",
            self.sku, self.ip, missed, LAN_VERIFY_RETRIES, {k: reported[k] for k in missed},
        )
        self._expected.clear()
        return False

    def _on_device_update(self, device: GoveeDevice) -> None:
        """Called by the govee-local-api controller when a devStatus response arrives."""
        if not self._available:
            _LOGGER.info("LAN device %s (%s) is available", device.sku, device.ip)
            self._available = True
        self._clear_repair_issue(self._issue_key)
        if self._verify_pending(device):
            self.async_set_updated_data(self._state_snapshot())
            return
        self.is_on = device.on
        self.brightness_raw = device.brightness
        # devStatus has no per-segment state, and after a segment write it
        # may report either 0,0,0 or, unchanged, the last whole-strip colour
        # (both seen on H619C). So a report only resyncs the segments when
        # it differs from what we already had - i.e. the colour was changed
        # outside HA, e.g. in the Govee app - and 0,0,0 never overwrites the
        # last known colour with black.
        if device.temperature_color > 0:
            if device.temperature_color != self.color_temp_kelvin:
                self.segment_colors = [None] * self._segment_count
            self.color_temp_kelvin = device.temperature_color
            self.rgb_color = None
        elif device.rgb_color != (0, 0, 0):
            if device.rgb_color != self.rgb_color or self.color_temp_kelvin:
                self.segment_colors = [device.rgb_color] * self._segment_count
            self.rgb_color = device.rgb_color
            self.color_temp_kelvin = None
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
            "segment_colors": list(self.segment_colors),
        }
