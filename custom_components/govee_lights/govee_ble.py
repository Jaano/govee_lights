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
BLE_UUID_NOTIFY_CHARACTERISTIC: str = "00010203-0405-0607-0809-0a0b0c0d2b10"

# ── Model feature flags ─────────────────────────────────────────────────────
# (Model capability lists are defined on GoveeHelper in govee.py.)
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

# Coordinator internals
_DISCONNECT_DELAY_SECONDS: int = 5  # idle seconds before auto-disconnect after last command
_RETRY_BACKOFF_SECONDS: float = 2.0
_DEVICE_DISCOVERY_RETRIES: int = 4

# ── Pre-built state-query frames ─────────────────────────────────────────
def _make_query_frame(cmd: int) -> bytes:
    frame = bytes([0xAA, cmd]) + bytes(17)
    chk = 0
    for b in frame:
        chk ^= b
    return frame + bytes([chk & 0xFF])

BLE_QUERY_POWER = _make_query_frame(0x01)
BLE_QUERY_BRIGHTNESS = _make_query_frame(0x04)
BLE_QUERY_COLOR_MODE = _make_query_frame(0x05)


# ── GoveeBLE - low-level static helpers ─────────────────────────────────────


class GoveeBLE(GoveeHelper):
    """Static helper class for Govee BLE packet construction and low-level I/O."""

    class LEDCommand(IntEnum):
        POWER = 0x01
        BRIGHTNESS = 0x04
        COLOR = 0x05

    class LEDMode(IntEnum):
        MANUAL = 0x02
        COLOUR_D = 0x0D  # same semantics as MANUAL but used by H613B-family
        MUSIC = 0x13
        SCENES = 0x05
        SEGMENTS = 0x15

    UUID_CONTROL_CHARACTERISTIC: str = BLE_UUID_CONTROL_CHARACTERISTIC
    UUID_NOTIFY_CHARACTERISTIC: str = BLE_UUID_NOTIFY_CHARACTERISTIC
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


# ── GoveeBLECoordinator ──────────────────────────────────────────────────────


class GoveeBLECoordinator(GoveeCoordinator):
    """
    Manages the BLE connection lifecycle for a single Govee device.

    Connects on demand when a command is sent, subscribes to GATT notifications,
    and auto-disconnects after a short idle period.  State (is_on, brightness_raw,
    rgb_color, mode, music_mode_id) is updated from GATT notifications and pushed
    to all registered listeners via async_set_updated_data().

    When the device goes unavailable (GATT writes fail after all retries) a BLE
    advertisement watcher is registered via bluetooth.async_register_callback so
    that the connection is re-established as soon as the device is seen again by
    the HA scanner, without requiring a restart.
    """

    def __init__(self, hass: HomeAssistant, address: str, model: str) -> None:
        super().__init__(hass, _LOGGER, f"Govee {model} ({address})")
        self.address = address
        self.model = model

        self._client: BleakClient | None = None
        self._lock = asyncio.Lock()
        self._cancel_disconnect: CALLBACK_TYPE | None = None
        self._unsub_advertisement: CALLBACK_TYPE | None = None
        self._graceful_disconnect: bool = False

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
        """RGB color to display — None when not in a plain-colour mode."""
        if self.mode not in (GoveeBLE.LEDMode.MANUAL, GoveeBLE.LEDMode.COLOUR_D):
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
        """Cancel the advertisement watcher if active."""
        self._cancel_advertisement_watcher()

    async def async_setup(self) -> None:
        """Connect to the device and query initial state. Safe to call from a background task."""
        try:
            await self._ensure_connected()
            self._cancel_advertisement_watcher()
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
            self._register_advertisement_watcher()

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
        """Send a native colour-temperature packet (white-balance mode).

        Packet layout (per homebridge-govee):
          33 05 [mode] FF FF FF 01 r g b ... checksum
        The 0xFF 0xFF 0xFF prefix + 0x01 flag tells the device to enter its
        white-balance/CT mode rather than plain RGB mode.  Without it the device
        accepts the colour but the on-device mode byte stays MANUAL (0x02) and
        future state-query notifications return a garbled colour.
        """
        r, g, b = GoveeBLE.kelvin_to_rgb(kelvin)
        mode_byte = (
            GoveeBLE.LEDMode.COLOUR_D
            if self.model in GoveeBLE.COLOUR_D_MODELS
            else GoveeBLE.LEDMode.MANUAL
        )
        packet = GoveeBLE.build_single_packet(
            GoveeBLE.LEDCommand.COLOR,
            [mode_byte, 0xFF, 0xFF, 0xFF, 0x01, r, g, b],
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
            _LOGGER.debug("Reusing existing connection to %s (%s)", self.model, self.address)
            self._reset_disconnect_timer()
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

        _LOGGER.debug("Connecting to %s (%s)", self.model, self.address)
        self._client = await brc.establish_connection(
            BleakClient, ble_device, self.address,
            disconnected_callback=self._handle_ble_disconnect,
        )
        _LOGGER.debug("Connected to %s (%s)", self.model, self.address)
        self._reset_disconnect_timer()
        await self._start_notify()
        await self._send_state_queries()
        return self._client

    @callback
    def _handle_ble_disconnect(self, _client: BleakClient) -> None:
        """Called by Bleak when the GATT connection drops."""
        self._client = None
        if self._graceful_disconnect:
            # We initiated this disconnect (idle timer) — device is still reachable.
            self._graceful_disconnect = False
            _LOGGER.debug("BLE idle disconnect for %s (%s)", self.model, self.address)
            return
        _LOGGER.debug("BLE connection lost for %s (%s)", self.model, self.address)
        if self._cancel_disconnect:
            self._cancel_disconnect()
            self._cancel_disconnect = None
        if self._available:
            self._available = False
            self.async_set_updated_data(self._state_snapshot())
        self._register_advertisement_watcher()

    def _register_advertisement_watcher(self) -> None:
        """Register a one-time BLE advertisement callback to reconnect when the device reappears."""
        if self._unsub_advertisement:
            return  # already watching

        @callback
        def _on_advertisement(
            _service_info: bluetooth.BluetoothServiceInfoBleak,
            _change: bluetooth.BluetoothChange,
        ) -> None:
            if self._available or (self._client and self._client.is_connected):
                return
            _LOGGER.debug(
                "BLE advertisement from %s (%s): scheduling reconnect",
                self.model, self.address,
            )
            self.hass.async_create_background_task(
                self.async_setup(),
                f"govee_lights_reconnect_{self.address}",
            )

        self._unsub_advertisement = bluetooth.async_register_callback(
            self.hass,
            _on_advertisement,
            bluetooth.BluetoothCallbackMatcher(address=self.address.upper()),
            bluetooth.BluetoothScanningMode.ACTIVE,
        )
        _LOGGER.debug("Watching for BLE advertisements from %s (%s)", self.model, self.address)

    def _cancel_advertisement_watcher(self) -> None:
        """Unregister the BLE advertisement callback."""
        if self._unsub_advertisement:
            self._unsub_advertisement()
            self._unsub_advertisement = None

    def _reset_disconnect_timer(self) -> None:
        """(Re)schedule the idle auto-disconnect timer."""
        if self._cancel_disconnect:
            self._cancel_disconnect()

        @callback
        def _on_timeout(_now: datetime) -> None:
            self.hass.async_create_task(self.disconnect())

        self._cancel_disconnect = async_call_later(self.hass, _DISCONNECT_DELAY_SECONDS, _on_timeout)

    # ── BLE notifications ─────────────────────────────────────────────────────

    async def _start_notify(self) -> None:
        if self._client and self._client.is_connected:
            try:
                await self._client.start_notify(BLE_UUID_NOTIFY_CHARACTERISTIC, self._notify_callback)
            except BleakError as err:
                _LOGGER.debug("Failed to start notify for %s: %s", self.address, err)

    def _notify_callback(self, _sender: Any, data: bytearray) -> None:
        if len(data) < 3 or data[0] != 0xAA:
            return
        domain, payload = data[1], bytes(data[2:])
        _LOGGER.debug("rx %s domain=0x%02x payload=%s", self.address, domain, payload.hex())
        try:
            if domain == GoveeBLE.LEDCommand.POWER:
                self.is_on = payload[0] == 0x01
            elif domain == GoveeBLE.LEDCommand.BRIGHTNESS:
                self.brightness_raw = payload[0]
            elif domain == GoveeBLE.LEDCommand.COLOR:
                self.mode = payload[0]
                if self.mode == GoveeBLE.LEDMode.MUSIC and len(payload) >= 2:
                    self.music_mode_id = payload[1]
                elif (
                    self.mode == GoveeBLE.LEDMode.SEGMENTS
                    and len(payload) >= 5
                    and payload[1] == 0x01
                ):
                    self.rgb_color = (payload[2], payload[3], payload[4])
                elif (
                    self.mode in (GoveeBLE.LEDMode.MANUAL, GoveeBLE.LEDMode.COLOUR_D)
                    and len(payload) >= 4
                ):
                    # Detect native CT response: [mode, 0xFF, 0xFF, 0xFF, 0x01, r, g, b]
                    # The white-ref prefix + 0x01 flag means the device is in white-balance mode.
                    is_ct = (
                        len(payload) >= 8
                        and payload[1] == 0xFF
                        and payload[2] == 0xFF
                        and payload[3] == 0xFF
                        and payload[4] == 0x01
                    )
                    if is_ct:
                        self.rgb_color = (payload[5], payload[6], payload[7])
                        # color_temp_kelvin already set by async_set_color_temp; preserve it.
                    else:
                        self.rgb_color = (payload[1], payload[2], payload[3])
                        self.color_temp_kelvin = None  # device is in plain RGB mode
            if not self._available:
                self._available = True
            self.async_set_updated_data(self._state_snapshot())
        except (IndexError, ValueError):
            _LOGGER.debug("Failed to parse notify from %s: %s", self.address, data.hex())

    # ── State query ───────────────────────────────────────────────────────────

    async def _send_state_queries(self) -> bool:
        """Send state-query packets after connecting; returns False if the write fails."""
        if not self._client or not self._client.is_connected:
            return False
        _LOGGER.debug("Querying state from %s (%s)", self.model, self.address)
        try:
            w = self._client.write_gatt_char
            await w(BLE_UUID_CONTROL_CHARACTERISTIC, BLE_QUERY_POWER, response=False)
            await w(BLE_UUID_CONTROL_CHARACTERISTIC, BLE_QUERY_BRIGHTNESS, response=False)
            await w(BLE_UUID_CONTROL_CHARACTERISTIC, BLE_QUERY_COLOR_MODE, response=False)
            return True
        except BleakError:
            return False

    async def _query_state_after_command(self) -> None:
        """Wait briefly then query state to confirm a command took effect.

        On a fresh connection, _send_state_queries() fires *before* the actual
        command packet is written, so its responses reflect the pre-command state
        and briefly overwrite any optimistic UI update.  By querying again 0.5 s
        after the command we always end up with the correct post-command state,
        even when the device does not echo the command as an unsolicited 0xAA
        notification.
        """
        await asyncio.sleep(0.5)
        async with self._lock:
            await self._send_state_queries()

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
        for attempt in range(BLE_CONNECT_ATTEMPTS):
            try:
                client = await self._ensure_connected()
                for i, pkt in enumerate(packets):
                    _LOGGER.debug(
                        "tx %s [%d/%d]: %s",
                        self.address, i + 1, len(packets), bytes(pkt).hex(),
                    )
                    await client.write_gatt_char(GoveeBLE.UUID_CONTROL_CHARACTERISTIC, pkt, False)
                    if delay > 0 and i < len(packets) - 1:
                        await asyncio.sleep(delay)
                if not self._available:
                    self._available = True
                    self.async_set_updated_data(self._state_snapshot())
                # Query state after command so HA reflects the new device state,
                # correcting any stale snapshot from the pre-command initial query.
                self.hass.async_create_background_task(
                    self._query_state_after_command(),
                    f"govee_ble_state_verify_{self.address}",
                )
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
                    if self._available:
                        self._available = False
                        self.async_set_updated_data(self._state_snapshot())
                    self._register_advertisement_watcher()
                    raise
                await asyncio.sleep(_RETRY_BACKOFF_SECONDS * (attempt + 1))

    # ── Disconnect ────────────────────────────────────────────────────────────

    async def disconnect(self) -> None:
        """Gracefully disconnect from the device."""
        _LOGGER.debug("Disconnecting from %s (%s)", self.model, self.address)
        self._graceful_disconnect = True
        if self._cancel_disconnect:
            self._cancel_disconnect()
            self._cancel_disconnect = None
        await self._disconnect_client()

    async def _disconnect_client(self) -> None:
        if self._client and self._client.is_connected:
            with contextlib.suppress(BleakError):
                await self._client.disconnect()
            _LOGGER.debug("Disconnected from %s (%s)", self.model, self.address)
        self._client = None
