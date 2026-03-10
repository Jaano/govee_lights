"""
Govee BLE low-level packet helpers and DataUpdateCoordinator.

GoveeBLE              - static helpers: packet building, checksum, scene encoding.
GoveeBLECoordinator   - manages a single BleakClient with keepalive, auto-reconnect,
                        idle-disconnect and pushes device-state updates to listeners.
"""

from __future__ import annotations

import array
import asyncio
import base64
import contextlib
from enum import IntEnum
import logging
import math
import time
from typing import TYPE_CHECKING, Any

from bleak import BleakClient
from bleak.exc import BleakError
import bleak_retry_connector as brc
from homeassistant.components import bluetooth
from homeassistant.components.light.const import ColorMode
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_call_later

from .const import DOMAIN
from .coordinator import GoveeCoordinator

if TYPE_CHECKING:
    from datetime import datetime

    from bleak.backends.device import BLEDevice
from .govee import GoveeHelper

_LOGGER = logging.getLogger(__name__)

# ── BLE GATT UUIDs ─────────────────────────────────────────────────────────
BLE_UUID_CONTROL_CHARACTERISTIC: str = "00010203-0405-0607-0809-0a0b0c0d2b11"
BLE_UUID_NOTIFY_CHARACTERISTIC: str = "00010203-0405-0607-0809-0a0b0c0d2b12"

# ── Model feature flags ─────────────────────────────────────────────────────
BLE_SEGMENTED_MODELS: list[str] = ["H6053", "H6072", "H6102", "H6199", "H617A", "H617C"]
BLE_PERCENT_MODELS: list[str] = ["H617A", "H617C"]
BLE_MUSIC_MODES: dict[str, int] = {
    "Music mode - Energic": 0x05,
    "Music mode - Rhythm": 0x03,
    "Music mode - Spectrum": 0x04,
    "Music mode - Rolling": 0x06,
}

# ── Timing constants ────────────────────────────────────────────────────────
BLE_QUERY_RESPONSE_TIMEOUT: float = 3.0  # seconds to wait for a state-query notification
BLE_INTER_FRAME_DELAY: float = 0.05  # seconds between consecutive BLE frames
BLE_CONNECT_ATTEMPTS: int = 3  # max connection attempts before raising
BLE_KEEPALIVE_INTERVAL: float = 5.0  # seconds between keepalive loop ticks

# How long (seconds) to keep the BLE connection open after the last communication.
#  0  = disconnect immediately after each command
# -1  = keep connected forever (reconnect on drop)
# >0  = disconnect after N idle seconds, reconnect on next command
BLE_IDLE_DISCONNECT_TIMEOUT: int = 0

# Coordinator internals
_DISCONNECT_DELAY_SECONDS: int = 120
_STATE_QUERY_EVERY_N_TICKS: int = 3
_RETRY_BACKOFF_SECONDS: float = 2.0
_DEVICE_DISCOVERY_RETRIES: int = 4


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  GoveeBLE - low-level static helpers                                     ║
# ╚══════════════════════════════════════════════════════════════════════════╝


class GoveeBLE(GoveeHelper):
    """Static helper class for Govee BLE packet construction and low-level I/O."""

    class LEDCommand(IntEnum):
        POWER = 0x01
        BRIGHTNESS = 0x04
        COLOR = 0x05

    class LEDMode(IntEnum):
        MANUAL = 0x02
        MUSIC = 0x13
        SCENES = 0x05
        SEGMENTS = 0x15

    UUID_CONTROL_CHARACTERISTIC: str = BLE_UUID_CONTROL_CHARACTERISTIC
    UUID_NOTIFY_CHARACTERISTIC: str = BLE_UUID_NOTIFY_CHARACTERISTIC
    SEGMENTED_MODELS: list[str] = BLE_SEGMENTED_MODELS
    PERCENT_MODELS: list[str] = BLE_PERCENT_MODELS
    MUSIC_MODES: dict[str, int] = BLE_MUSIC_MODES

    @staticmethod
    def kelvin_to_rgb(kelvin: int) -> tuple[int, int, int]:
        """Convert a colour temperature in Kelvin to an approximate sRGB triplet.

        Uses Tanner Helland's curve-fit algorithm, clamped to [1000, 40000] K.
        """
        temp = max(1000, min(40000, kelvin)) / 100.0

        if temp <= 66:
            red = 255
        else:
            red = max(0, min(255, round(329.698727446 * ((temp - 60) ** -0.1332047592))))

        if temp <= 66:
            green = max(0, min(255, round(99.4708025861 * math.log(temp) - 161.1195681661)))
        else:
            green = max(0, min(255, round(288.1221695283 * ((temp - 60) ** -0.0755148492))))

        if temp >= 66:
            blue = 255
        elif temp <= 19:
            blue = 0
        else:
            blue = max(0, min(255, round(138.5177312231 * math.log(temp - 10) - 305.0447927307)))

        return (red, green, blue)

    @staticmethod
    def sign_payload(data: bytes | bytearray | array.array[int]) -> int:
        """XOR checksum over all bytes."""
        checksum = 0
        for b in data:
            checksum ^= b
        return checksum & 0xFF

    @staticmethod
    def build_single_packet(cmd: int, payload: bytes | list[int]) -> bytes:
        """Build (but do not send) a signed 20-byte command packet."""
        if len(payload) > 17:
            raise ValueError("Payload too long")
        frame = bytes([0x33, cmd & 0xFF]) + bytes(payload)
        frame += bytes([0] * (19 - len(frame)))
        return frame + bytes([GoveeBLE.sign_payload(frame)])

    @staticmethod
    def build_music_packet(mode_id: int, sensitivity: int = 100) -> bytes:
        """Build a packet that activates a music-reactive mode."""
        sensitivity = max(0, min(100, sensitivity))
        payload: list[int] = [GoveeBLE.LEDMode.MUSIC, mode_id, sensitivity, 0x00]
        frame = bytes([0x33, GoveeBLE.LEDCommand.COLOR, *payload])
        frame += bytes(19 - len(frame))
        return frame + bytes([GoveeBLE.sign_payload(frame)])

    @staticmethod
    async def send_single_packet(client: BleakClient, cmd: int, payload: bytes | list[int]) -> None:
        """Build, sign, and send a 20-byte command packet (legacy helper)."""
        await GoveeBLE.send_single_frame(client, GoveeBLE.build_single_packet(cmd, payload))

    @staticmethod
    async def send_single_frame(
        client: BleakClient, frame: bytes | bytearray | array.array[int]
    ) -> None:
        """Write a pre-built 20-byte BLE frame (legacy helper)."""
        retry = 0
        while not client.is_connected:
            if retry >= BLE_CONNECT_ATTEMPTS:
                raise TimeoutError("Device not connected")
            await client.connect()
            retry += 1
        _LOGGER.debug("Writing frame: %s", bytes(frame).hex())
        await client.write_gatt_char(GoveeBLE.UUID_CONTROL_CHARACTERISTIC, frame, False)

    @staticmethod
    async def send_multi_packet(
        client: BleakClient,
        protocol_type: int,
        header_array: array.array[int],
        data: array.array[int],
    ) -> None:
        """Send a segmented multi-packet burst (legacy helper)."""
        result: list[array.array[int]] = []
        header_length = len(header_array)
        header_offset = header_length + 4

        initial_buffer = array.array("B", [0] * 20)
        initial_buffer[0] = protocol_type
        initial_buffer[1] = 0
        initial_buffer[2] = 1
        initial_buffer[4 : 4 + header_length] = header_array

        additional_buffer = array.array("B", [0] * 20)
        additional_buffer[0] = protocol_type
        additional_buffer[1] = 255

        remaining_space = 14 - header_length + 1

        if len(data) <= remaining_space:
            initial_buffer[header_offset : header_offset + len(data)] = data
        else:
            excess = len(data) - remaining_space
            chunks = excess // 17
            remainder = excess % 17
            if remainder > 0:
                chunks += 1
            else:
                remainder = 17

            initial_buffer[header_offset : header_offset + remaining_space] = data[
                0:remaining_space
            ]
            current_index = remaining_space

            for i in range(1, chunks + 1):
                chunk = array.array("B", [0] * 17)
                chunk_size = remainder if i == chunks else 17
                chunk[0:chunk_size] = data[current_index : current_index + chunk_size]
                current_index += chunk_size

                if i == chunks:
                    additional_buffer[2 : 2 + chunk_size] = chunk[0:chunk_size]
                else:
                    chunk_buffer = array.array("B", [0] * 20)
                    chunk_buffer[0] = protocol_type
                    chunk_buffer[1] = i
                    chunk_buffer[2 : 2 + chunk_size] = chunk
                    chunk_buffer[19] = GoveeBLE.sign_payload(chunk_buffer[0:19])
                    result.append(chunk_buffer)

        initial_buffer[3] = len(result) + 2
        initial_buffer[19] = GoveeBLE.sign_payload(initial_buffer[0:19])
        result.insert(0, initial_buffer)

        additional_buffer[19] = GoveeBLE.sign_payload(additional_buffer[0:19])
        result.append(additional_buffer)

        for i, r in enumerate(result):
            _LOGGER.debug("Multi-packet frame %d/%d: %s", i + 1, len(result), r.tobytes().hex())
            await GoveeBLE.send_single_frame(client, r)
            await asyncio.sleep(BLE_INTER_FRAME_DELAY)

    @staticmethod
    async def query_state(client: BleakClient) -> dict[str, Any]:
        """
        Query power, brightness, and color state from a connected device.
        Sends 0xAA query packets and collects notification responses.
        Returns a dict with keys: 'power', 'brightness', 'rgb', 'mode', 'music_mode_id'.
        """
        COMMANDS = [
            GoveeBLE.LEDCommand.POWER,
            GoveeBLE.LEDCommand.BRIGHTNESS,
            GoveeBLE.LEDCommand.COLOR,
        ]
        state: dict[str, Any] = {
            "power": None,
            "brightness": None,
            "rgb": None,
            "mode": None,
            "music_mode_id": None,
        }
        events = {cmd: asyncio.Event() for cmd in COMMANDS}

        def notification_handler(sender: Any, data: bytearray) -> None:
            _LOGGER.debug("State notification: %s", data.hex())
            if len(data) < 3 or data[0] != 0xAA:
                return
            cmd = data[1]
            if cmd == GoveeBLE.LEDCommand.POWER:
                state["power"] = data[2] == 0x01
                events[GoveeBLE.LEDCommand.POWER].set()
            elif cmd == GoveeBLE.LEDCommand.BRIGHTNESS:
                state["brightness"] = data[2]
                events[GoveeBLE.LEDCommand.BRIGHTNESS].set()
            elif cmd == GoveeBLE.LEDCommand.COLOR:
                state["mode"] = data[2]
                if state["mode"] == GoveeBLE.LEDMode.MUSIC and len(data) >= 4:
                    state["music_mode_id"] = data[3]
                elif len(data) >= 6:
                    state["rgb"] = (data[3], data[4], data[5])
                events[GoveeBLE.LEDCommand.COLOR].set()

        try:
            if client.services.get_characteristic(GoveeBLE.UUID_NOTIFY_CHARACTERISTIC) is None:
                _LOGGER.warning("Notify characteristic not found on device; skipping state query")
                return state

            await client.start_notify(GoveeBLE.UUID_NOTIFY_CHARACTERISTIC, notification_handler)

            for cmd in COMMANDS:
                frame = bytes([0xAA, cmd]) + bytes(17)
                frame += bytes([GoveeBLE.sign_payload(frame)])
                _LOGGER.debug("Sending state query 0x%02x: %s", cmd, frame.hex())
                await client.write_gatt_char(GoveeBLE.UUID_CONTROL_CHARACTERISTIC, frame, False)
                try:
                    await asyncio.wait_for(events[cmd].wait(), timeout=BLE_QUERY_RESPONSE_TIMEOUT)
                except TimeoutError:
                    _LOGGER.warning("Timeout waiting for response to query 0x%02x", cmd)
        except Exception as err:
            _LOGGER.error("Failed to query device state: %s", err)
        finally:
            with contextlib.suppress(Exception):
                await client.stop_notify(GoveeBLE.UUID_NOTIFY_CHARACTERISTIC)

        return state

    @staticmethod
    async def connect_to(device: BLEDevice, identifier: str) -> BleakClient:
        """Connect to a BLE device with retry and return the BleakClient."""
        last_err: Exception | None = None
        for _ in range(BLE_CONNECT_ATTEMPTS):
            try:
                return await brc.establish_connection(BleakClient, device, identifier)
            except Exception as err:
                last_err = err
        raise RuntimeError(
            f"Failed to connect to {identifier} after {BLE_CONNECT_ATTEMPTS} attempts"
        ) from last_err

    @staticmethod
    async def read_attribute(client: BleakClient, attribute: str) -> bytearray:
        """Read a GATT characteristic value."""
        return await client.read_gatt_char(attribute)


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  GoveeBLECoordinator                                                     ║
# ╚══════════════════════════════════════════════════════════════════════════╝


class GoveeBLECoordinator(GoveeCoordinator):
    """
    Manages the BLE connection lifecycle for a single Govee device.

    Connection modes (controlled by _idle_timeout):
      0   — disconnect immediately after each command
     -1   — keep alive forever; reconnect automatically on drop
     >0   — disconnect after N idle seconds; reconnect on next command

    State (is_on, brightness_raw, rgb_color, mode, music_mode_id) is updated
    whenever BLE notifications arrive and pushed to all registered listeners via
    DataUpdateCoordinator.async_set_updated_data().
    """

    def __init__(self, hass: HomeAssistant, address: str, model: str) -> None:
        super().__init__(hass, _LOGGER, f"Govee {model} ({address})")
        self.address = address
        self.model = model

        self._client: BleakClient | None = None
        self._lock = asyncio.Lock()
        self._cancel_disconnect: CALLBACK_TYPE | None = None
        self._keep_alive_task: asyncio.Task[None] | None = None
        self._keep_alive_ticks = 0
        self._idle_timeout: int = BLE_IDLE_DISCONNECT_TIMEOUT
        self._last_command_time: float = 0.0

        # Last-known device state (updated from BLE notifications)
        self.mode: int | None = None
        self.music_mode_id: int | None = None

        self._unsub_stop: CALLBACK_TYPE | None = hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP, self._handle_hass_stop
        )

    # ── Public coordinator interface ─────────────────────────────────────────

    @property
    def unique_device_id(self) -> str:
        """Unique identifier for this device (address without colons)."""
        return self.address.replace(":", "")

    @property
    def device_key(self) -> str:
        """Model name, used for effect list loading and device labelling."""
        return self.model

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
        return True

    @property
    def setup_in_background(self) -> bool:
        return True

    @property
    def brightness(self) -> int:
        """Brightness in HA scale (0-255)."""
        if self.model in GoveeBLE.PERCENT_MODELS:
            return round(self.brightness_raw * 255 / 100)
        return self.brightness_raw

    @property
    def current_rgb_color(self) -> tuple[int, int, int] | None:
        """RGB color to display — None when not in MANUAL mode."""
        if self.mode != GoveeBLE.LEDMode.MANUAL:
            return None
        return self.rgb_color

    @property
    def inferred_effect(self) -> str | None:
        """Derive the active effect name from BLE music mode state."""
        if self.mode == GoveeBLE.LEDMode.MUSIC and self.music_mode_id is not None:
            reverse = {v: k for k, v in GoveeBLE.MUSIC_MODES.items()}
            return reverse.get(self.music_mode_id)
        return None

    def cleanup(self) -> None:
        """No-op: BLE devices have no persistent resources to release."""

    async def async_setup(self) -> None:
        """Connect to the device and query initial state. Safe to call from a background task."""
        try:
            await self._ensure_connected()
            self._available = True
            self.async_set_updated_data(self._state_snapshot())
        except Exception as err:
            _LOGGER.warning(
                "Initial BLE connection failed for %s (%s): %s",
                self.model,
                self.address,
                err,
            )
            self._available = False
            self.async_set_updated_data(self._state_snapshot())

    async def async_load_effects(self, config_dir: str) -> None:
        """Load the scene/effect list in an executor thread and store results."""
        scenes, effect_map, effect_list = await self.hass.async_add_executor_job(
            GoveeBLE.build_model_effect_list, self.device_key, config_dir
        )
        effect_list = [*effect_list, *GoveeBLE.MUSIC_MODES.keys()]
        self._scenes_data = scenes
        self._effect_map = effect_map
        self._effect_list = effect_list

    async def async_turn_on(self) -> None:
        await self.send_command(GoveeBLE.build_single_packet(GoveeBLE.LEDCommand.POWER, [0x1]))

    async def async_turn_off(self) -> None:
        await self.send_command(GoveeBLE.build_single_packet(GoveeBLE.LEDCommand.POWER, [0x0]))

    async def async_set_brightness(self, brightness: int) -> None:
        """Set brightness. brightness is HA scale (0-255)."""
        val = int(brightness * 100 / 255) if self.model in GoveeBLE.PERCENT_MODELS else brightness
        await self.send_command(GoveeBLE.build_single_packet(GoveeBLE.LEDCommand.BRIGHTNESS, [val]))

    async def async_set_rgb_color(self, r: int, g: int, b: int) -> None:
        self.color_temp_kelvin = None
        if self.model in GoveeBLE.SEGMENTED_MODELS:
            packet = GoveeBLE.build_single_packet(
                GoveeBLE.LEDCommand.COLOR,
                [GoveeBLE.LEDMode.SEGMENTS, 0x01, r, g, b,
                 0x00, 0x00, 0x00, 0x00, 0x00, 0xFF, 0x7F],
            )
        else:
            packet = GoveeBLE.build_single_packet(
                GoveeBLE.LEDCommand.COLOR, [GoveeBLE.LEDMode.MANUAL, r, g, b]
            )
        await self.send_command(packet)

    async def async_set_color_temp(self, kelvin: int) -> None:
        """Emulate colour temperature by sending the equivalent RGB packet."""
        r, g, b = GoveeBLE.kelvin_to_rgb(kelvin)
        if self.model in GoveeBLE.SEGMENTED_MODELS:
            packet = GoveeBLE.build_single_packet(
                GoveeBLE.LEDCommand.COLOR,
                [GoveeBLE.LEDMode.SEGMENTS, 0x01, r, g, b,
                 0x00, 0x00, 0x00, 0x00, 0x00, 0xFF, 0x7F],
            )
        else:
            packet = GoveeBLE.build_single_packet(
                GoveeBLE.LEDCommand.COLOR, [GoveeBLE.LEDMode.MANUAL, r, g, b]
            )
        self.color_temp_kelvin = kelvin
        self.rgb_color = (r, g, b)
        await self.send_command(packet)

    async def async_set_effect(self, ptreal_cmds: list[str]) -> None:
        """Apply a ptReal scene effect and power the device on."""
        raw_packets: list[bytes | bytearray] = [
            bytearray(base64.b64decode(c)) for c in ptreal_cmds
        ]
        raw_packets.append(GoveeBLE.build_single_packet(GoveeBLE.LEDCommand.POWER, [0x1]))
        await self.send_commands(raw_packets, delay=BLE_INTER_FRAME_DELAY)

    async def async_send_music_mode(self, mode_id: int) -> None:
        """Send a music/rhythm mode packet."""
        packets = [
            GoveeBLE.build_music_packet(mode_id),
            GoveeBLE.build_single_packet(GoveeBLE.LEDCommand.POWER, [0x1]),
        ]
        await self.send_commands(packets, delay=BLE_INTER_FRAME_DELAY)

    async def async_apply_effect(self, effect: str) -> None:
        """Apply a named effect, dispatching to music mode or scene as appropriate."""
        if effect in GoveeBLE.MUSIC_MODES:
            mode_id = GoveeBLE.MUSIC_MODES[effect]
            _LOGGER.debug("Sending music mode %r (id=0x%02x)", effect, mode_id)
            await self.async_send_music_mode(mode_id)
            return
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

    # ── BLE internals ─────────────────────────────────────────────────────────

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.address)},
            name=self.model,
            manufacturer="Govee",
            model=self.model,
        )

    @callback
    def _handle_hass_stop(self, _event: Event) -> None:
        self._stop_keep_alive()
        if self._cancel_disconnect:
            self._cancel_disconnect()
            self._cancel_disconnect = None

    async def _async_update_data(self) -> dict[str, Any]:
        """Required by DataUpdateCoordinator; returns a snapshot of current state."""
        return self._state_snapshot()

    def _state_snapshot(self) -> dict[str, Any]:
        return {
            "is_on": self.is_on,
            "brightness_raw": self.brightness_raw,
            "rgb_color": self.rgb_color,
            "mode": self.mode,
            "music_mode_id": self.music_mode_id,
        }

    async def _ensure_connected(self) -> BleakClient:
        """Return a live BleakClient, establishing a new connection if required."""
        if self._client and self._client.is_connected:
            self._arm_disconnect_timer()
            return self._client

        ble_device = None
        for attempt in range(_DEVICE_DISCOVERY_RETRIES):
            ble_device = bluetooth.async_ble_device_from_address(
                self.hass, self.address.upper(), connectable=True
            )
            if ble_device is not None:
                break
            if attempt < _DEVICE_DISCOVERY_RETRIES - 1:
                await asyncio.sleep(_RETRY_BACKOFF_SECONDS)

        if ble_device is None:
            raise BleakError(
                f"Device {self.address} not found after {_DEVICE_DISCOVERY_RETRIES} attempts"
            )

        self._client = await brc.establish_connection(BleakClient, ble_device, self.address)
        _LOGGER.debug("Connected to %s (%s)", self.model, self.address)
        self._arm_disconnect_timer()
        await self._subscribe_notifications()
        await self._run_state_query()
        return self._client

    def _arm_disconnect_timer(self) -> None:
        """(Re)schedule an auto-disconnect based on _idle_timeout."""
        if self._cancel_disconnect:
            self._cancel_disconnect()
            self._cancel_disconnect = None

        # 0 = immediate (handled per-command); -1 = keep alive (no timer)
        if self._idle_timeout <= 0:
            return

        @callback
        def _on_timeout(_now: datetime) -> None:
            self.hass.async_create_task(self.disconnect())

        self._cancel_disconnect = async_call_later(self.hass, self._idle_timeout, _on_timeout)

    # ── BLE notifications ─────────────────────────────────────────────────────

    async def _subscribe_notifications(self) -> None:
        if not self._client or not self._client.is_connected:
            return
        if self._client.services.get_characteristic(GoveeBLE.UUID_NOTIFY_CHARACTERISTIC) is None:
            _LOGGER.debug("Notify characteristic not found on %s, skipping", self.address)
            return
        try:
            await self._client.start_notify(
                GoveeBLE.UUID_NOTIFY_CHARACTERISTIC, self._notification_handler
            )
            self._start_keep_alive()
        except BleakError as err:
            _LOGGER.debug("Failed to subscribe to notifications for %s: %s", self.address, err)

    def _notification_handler(self, _sender: Any, data: bytearray) -> None:
        if len(data) < 3 or data[0] != 0xAA:
            return
        cmd, payload = data[1], data[2:]
        _LOGGER.debug("Notification %s cmd=0x%02x: %s", self.address, cmd, data.hex())
        changed = False
        if cmd == GoveeBLE.LEDCommand.POWER:
            self.is_on = payload[0] == 0x01
            changed = True
        elif cmd == GoveeBLE.LEDCommand.BRIGHTNESS:
            self.brightness_raw = payload[0]
            changed = True
        elif cmd == GoveeBLE.LEDCommand.COLOR:
            self.mode = payload[0]
            if self.mode == GoveeBLE.LEDMode.MUSIC and len(payload) >= 2:
                self.music_mode_id = payload[1]
            elif len(payload) >= 4:
                self.rgb_color = (payload[1], payload[2], payload[3])
            changed = True

        if changed:
            if not self._available:
                self._available = True
            self.async_set_updated_data(self._state_snapshot())

    # ── State query ───────────────────────────────────────────────────────────

    async def _run_state_query(self) -> None:
        """Send 0xAA query packets for power, brightness, and color mode."""
        if not self._client or not self._client.is_connected:
            return
        for cmd in (
            GoveeBLE.LEDCommand.POWER,
            GoveeBLE.LEDCommand.BRIGHTNESS,
            GoveeBLE.LEDCommand.COLOR,
        ):
            frame = bytes([0xAA, cmd]) + bytes(17)
            frame += bytes([GoveeBLE.sign_payload(frame)])
            try:
                await self._client.write_gatt_char(
                    GoveeBLE.UUID_CONTROL_CHARACTERISTIC, frame, False
                )
                await asyncio.sleep(BLE_INTER_FRAME_DELAY)
            except BleakError as err:
                _LOGGER.debug("State query 0x%02x failed for %s: %s", cmd, self.address, err)

    # ── Keepalive loop ────────────────────────────────────────────────────────

    def _start_keep_alive(self) -> None:
        self._stop_keep_alive()
        self._keep_alive_ticks = 0
        self._keep_alive_task = self.hass.async_create_background_task(
            self._keep_alive_loop(), f"govee_keepalive_{self.address}"
        )

    def _stop_keep_alive(self) -> None:
        if self._keep_alive_task and not self._keep_alive_task.done():
            self._keep_alive_task.cancel()
        self._keep_alive_task = None

    async def _keep_alive_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(BLE_KEEPALIVE_INTERVAL)

                if not self._client or not self._client.is_connected:
                    _LOGGER.debug("BLE connection to %s dropped", self.address)
                    if self._idle_timeout == -1:
                        await self._attempt_reconnect()
                    break

                self._keep_alive_ticks += 1
                full_query = self._keep_alive_ticks % _STATE_QUERY_EVERY_N_TICKS == 0

                try:
                    if full_query:
                        await self._run_state_query()
                    else:
                        # Lightweight ping: re-query power state only
                        ping = bytes([0xAA, GoveeBLE.LEDCommand.POWER]) + bytes(17)
                        ping += bytes([GoveeBLE.sign_payload(ping)])
                        await self._client.write_gatt_char(
                            GoveeBLE.UUID_CONTROL_CHARACTERISTIC, ping, False
                        )
                except BleakError as err:
                    _LOGGER.debug("Keepalive write failed for %s: %s", self.address, err)
                    self._client = None
                    if self._idle_timeout == -1:
                        await self._attempt_reconnect()
                    break
        except asyncio.CancelledError:
            pass

    async def _attempt_reconnect(self) -> None:
        """Try to reconnect in keep-alive mode; update availability accordingly."""
        try:
            await self._ensure_connected()
            if not self._available:
                _LOGGER.info("Govee %s (%s) is available again", self.model, self.address)
                self._available = True
                self.async_set_updated_data(self._state_snapshot())
        except Exception as err:
            _LOGGER.debug("Reconnect failed for %s: %s", self.address, err)
            if self._available:
                _LOGGER.warning("Govee %s (%s) is unavailable: %s", self.model, self.address, err)
                self._available = False
                self.async_set_updated_data(self._state_snapshot())

    # ── Command dispatch ──────────────────────────────────────────────────────

    async def send_command(self, packet: bytes) -> None:
        """Send a single BLE packet, ensuring a live connection."""
        async with self._lock:
            await self._dispatch([packet])

    async def send_commands(
        self, packets: list[bytes | bytearray], delay: float = BLE_INTER_FRAME_DELAY
    ) -> None:
        """Send multiple BLE packets with an inter-frame delay, under a single lock."""
        async with self._lock:
            await self._dispatch(packets, delay=delay)

    async def _dispatch(self, packets: list[bytes | bytearray], delay: float = 0.0) -> None:
        """Internal: write packets to the device with retry on connection errors."""
        self._last_command_time = time.monotonic()
        for attempt in range(BLE_CONNECT_ATTEMPTS):
            try:
                client = await self._ensure_connected()
                for i, pkt in enumerate(packets):
                    await client.write_gatt_char(GoveeBLE.UUID_CONTROL_CHARACTERISTIC, pkt, False)
                    if delay > 0 and i < len(packets) - 1:
                        await asyncio.sleep(delay)
                if self._idle_timeout == 0:
                    await self._disconnect_client()
                return
            except BleakError as err:
                self._client = None
                if attempt == BLE_CONNECT_ATTEMPTS - 1:
                    _LOGGER.error(
                        "Failed to send to %s after %d attempts: %s",
                        self.address,
                        BLE_CONNECT_ATTEMPTS,
                        err,
                    )
                    raise
                await asyncio.sleep(_RETRY_BACKOFF_SECONDS * (attempt + 1))

    # ── Disconnect ────────────────────────────────────────────────────────────

    async def disconnect(self) -> None:
        """Gracefully disconnect from the device and cancel the keepalive loop."""
        self._stop_keep_alive()
        if self._cancel_disconnect:
            self._cancel_disconnect()
            self._cancel_disconnect = None
        await self._disconnect_client()

    async def _disconnect_client(self) -> None:
        if self._client and self._client.is_connected:
            with contextlib.suppress(BleakError):
                await self._client.disconnect()
        self._client = None
