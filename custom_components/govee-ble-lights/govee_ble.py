"""
This file contains all functionality pertaining to Govee BLE lights, including data structures.
"""

from enum import IntEnum
import array
import asyncio
import logging
from typing import Any

from bleak import BleakClient
import bleak_retry_connector as brc

_LOGGER = logging.getLogger(__name__)

# BLE GATT characteristic UUIDs
BLE_UUID_CONTROL_CHARACTERISTIC: str = '00010203-0405-0607-0809-0a0b0c0d2b11'
BLE_UUID_NOTIFY_CHARACTERISTIC: str  = '00010203-0405-0607-0809-0a0b0c0d2b12'

# Device model feature flags
BLE_SEGMENTED_MODELS: list[str] = ['H6053', 'H6072', 'H6102', 'H6199', 'H617A', 'H617C']
BLE_PERCENT_MODELS: list[str] = ['H617A', 'H617C']

# Music sub-mode IDs (byte after the 0x13 color mode byte)
BLE_MUSIC_MODES: dict[str, int] = {
    "Music mode - Energic": 0x05,
    "Music mode - Rhythm": 0x03,
    "Music mode - Spectrum": 0x04,
    "Music mode - Rolling": 0x06,
}

# BLE communication tuning
BLE_QUERY_RESPONSE_TIMEOUT: float = 3.0  # seconds to wait for a state-query notification
BLE_INTER_FRAME_DELAY: float = 0.05      # seconds between consecutive BLE frames
BLE_CONNECT_ATTEMPTS: int = 3            # max connection attempts before raising

# Keepalive / idle management
BLE_KEEPALIVE_INTERVAL: float = 5.0     # seconds between keepalive loop ticks

# How long (seconds) to keep the BLE connection open after the last communication.
#  0  = disconnect immediately after each command
# -1  = keep connected forever (reconnect on drop)
# >0  = disconnect after N idle seconds, reconnect on next command
BLE_IDLE_DISCONNECT_TIMEOUT: int = 10

class GoveeBLE:
    """ This class is used to connect to and control Govee branded BLE LED lights. """

    class LEDCommand(IntEnum):
        """ A control command packet's type. """
        POWER = 0x01
        BRIGHTNESS = 0x04
        COLOR = 0x05

    class LEDMode(IntEnum):
        """
        The mode in which a color change happens in. Only manual is supported.
        """
        MANUAL = 0x02
        MUSIC = 0x13
        SCENES = 0x05
        SEGMENTS = 0x15

    MUSIC_MODES: dict[str, int] = BLE_MUSIC_MODES
    UUID_CONTROL_CHARACTERISTIC: str = BLE_UUID_CONTROL_CHARACTERISTIC
    UUID_NOTIFY_CHARACTERISTIC: str  = BLE_UUID_NOTIFY_CHARACTERISTIC
    SEGMENTED_MODELS: list[str] = BLE_SEGMENTED_MODELS
    PERCENT_MODELS: list[str] = BLE_PERCENT_MODELS

    @staticmethod
    async def query_state(client: BleakClient) -> dict:
        """
        Queries power, brightness, and color state from the device in a single connection.
        Sends 0xAA query packets and collects notification responses.
        Returns a dict with keys: 'power' (bool|None), 'brightness' (int|None),
        'rgb' (tuple|None), 'mode' (int|None).
        """
        COMMANDS = [
            GoveeBLE.LEDCommand.POWER,
            GoveeBLE.LEDCommand.BRIGHTNESS,
            GoveeBLE.LEDCommand.COLOR,
        ]
        state: dict[str, Any] = {'power': None, 'brightness': None, 'rgb': None, 'mode': None, 'music_mode_id': None}
        events = {cmd: asyncio.Event() for cmd in COMMANDS}

        def notification_handler(sender, data: bytearray) -> None:
            _LOGGER.debug("State notification: %s", data.hex())
            if len(data) < 3 or data[0] != 0xAA:
                return
            cmd = data[1]
            if cmd == GoveeBLE.LEDCommand.POWER:
                state['power'] = data[2] == 0x01
                _LOGGER.debug("Power: %s", state['power'])
                events[GoveeBLE.LEDCommand.POWER].set()
            elif cmd == GoveeBLE.LEDCommand.BRIGHTNESS:
                state['brightness'] = data[2]
                _LOGGER.debug("Brightness: %d", state['brightness'])
                events[GoveeBLE.LEDCommand.BRIGHTNESS].set()
            elif cmd == GoveeBLE.LEDCommand.COLOR:
                state['mode'] = data[2]
                if state['mode'] == GoveeBLE.LEDMode.MUSIC and len(data) >= 4:
                    state['music_mode_id'] = data[3]
                    _LOGGER.debug("Music mode id: 0x%02x", state['music_mode_id'])
                elif len(data) >= 6:
                    state['rgb'] = (data[3], data[4], data[5])
                _LOGGER.debug("Mode: 0x%02x, RGB: %s", state['mode'], state['rgb'])
                events[GoveeBLE.LEDCommand.COLOR].set()

        try:
            # Some devices do not expose the notify characteristic at all.
            # Check before subscribing so we return cleanly instead of logging an error.
            if client.services.get_characteristic(GoveeBLE.UUID_NOTIFY_CHARACTERISTIC) is None:
                _LOGGER.warning(
                    "Notify characteristic %s not found on device; skipping state query",
                    GoveeBLE.UUID_NOTIFY_CHARACTERISTIC,
                )
                return state

            await client.start_notify(GoveeBLE.UUID_NOTIFY_CHARACTERISTIC, notification_handler)

            for cmd in COMMANDS:
                frame = bytes([0xAA, cmd]) + bytes(17)
                frame += bytes([GoveeBLE.sign_payload(frame)])
                _LOGGER.debug("Sending state query 0x%02x: %s", cmd, frame.hex())
                await client.write_gatt_char(GoveeBLE.UUID_CONTROL_CHARACTERISTIC, frame, False)
                try:
                    await asyncio.wait_for(events[cmd].wait(), timeout=BLE_QUERY_RESPONSE_TIMEOUT)
                except asyncio.TimeoutError:
                    _LOGGER.warning("Timeout waiting for response to query 0x%02x", cmd)

        except Exception as err:
            _LOGGER.error("Failed to query device state: %s", err)
        finally:
            try:
                await client.stop_notify(GoveeBLE.UUID_NOTIFY_CHARACTERISTIC)
            except Exception:
                pass

        return state

    @staticmethod
    async def send_multi_packet(client: BleakClient, protocol_type, header_array, data):
        """
        Creates a multi-packed packet.
        """

        result = []

        # Initialize the initial buffer
        header_length = len(header_array)
        header_offset = header_length + 4

        initial_buffer = array.array('B', [0] * 20)
        initial_buffer[0] = protocol_type
        initial_buffer[1] = 0
        initial_buffer[2] = 1
        initial_buffer[4:4+header_length] = header_array

        # Create the additional buffer
        additional_buffer = array.array('B', [0] * 20)
        additional_buffer[0] = protocol_type
        additional_buffer[1] = 255

        remaining_space = 14 - header_length + 1

        if len(data) <= remaining_space:
            initial_buffer[header_offset:header_offset + len(data)] = data
        else:
            excess = len(data) - remaining_space
            chunks = excess // 17
            remainder = excess % 17

            if remainder > 0:
                chunks += 1
            else:
                remainder = 17

            initial_buffer[header_offset:header_offset + remaining_space] = data[0:remaining_space]
            current_index = remaining_space

            for i in range(1, chunks + 1):
                chunk = array.array('B', [0] * 17)
                chunk_size = remainder if i == chunks else 17
                chunk[0:chunk_size] = data[current_index:current_index + chunk_size]
                current_index += chunk_size

                if i == chunks:
                    additional_buffer[2:2 + chunk_size] = chunk[0:chunk_size]
                else:
                    chunk_buffer = array.array('B', [0] * 20)
                    chunk_buffer[0] = protocol_type
                    chunk_buffer[1] = i
                    chunk_buffer[2:2+chunk_size] = chunk
                    chunk_buffer[19] = GoveeBLE.sign_payload(chunk_buffer[0:19])
                    result.append(chunk_buffer)

        initial_buffer[3] = len(result) + 2
        initial_buffer[19] = GoveeBLE.sign_payload(initial_buffer[0:19])
        result.insert(0, initial_buffer)

        additional_buffer[19] = GoveeBLE.sign_payload(additional_buffer[0:19])
        result.append(additional_buffer)

        for i, r in enumerate(result):
            _LOGGER.debug("Sending multi-packet frame %d/%d: %s", i + 1, len(result), r.tobytes().hex())
            await GoveeBLE.send_single_frame(client, r)
            await asyncio.sleep(BLE_INTER_FRAME_DELAY)

    @staticmethod
    async def send_single_packet(client: BleakClient, cmd, payload):
        """
        Creates, signs, and sends a complete BLE packet to the device.
        Functions according to the input command and payload.
        """

        if not isinstance(cmd, int):
            raise ValueError('Invalid command')
        if not isinstance(payload, bytes) and not (
                isinstance(payload, list) and all(isinstance(x, int) for x in payload)):
            raise ValueError('Invalid payload')
        if len(payload) > 17:
            raise ValueError('Payload too long')

        cmd = cmd & 0xFF
        payload = bytes(payload)

        frame = bytes([0x33, cmd]) + bytes(payload)
        # pad frame data to 19 bytes (plus checksum)
        frame += bytes([0] * (19 - len(frame)))
        frame += bytes([GoveeBLE.sign_payload(frame)])

        await GoveeBLE.send_single_frame(client, frame)

    @staticmethod
    async def send_single_frame(client: BleakClient, frame) -> None:
        """ Sends a pre-made BLE frame to the device. """
        retry = 0
        while not client.is_connected:
            if retry >= BLE_CONNECT_ATTEMPTS:
                raise TimeoutError
            await client.connect()
            retry += 1

        _LOGGER.debug("Writing frame: %s", bytes(frame).hex())
        await client.write_gatt_char(GoveeBLE.UUID_CONTROL_CHARACTERISTIC, frame, False)

    @staticmethod
    async def read_attribute(client: BleakClient, attribute: LEDCommand):
        """ Attempts to read a device attribute. """
        return await client.read_gatt_char(attribute)

    @staticmethod
    async def connect_to(device, identifier) -> BleakClient:
        """" This method connects to and returns a handle for the target BLE device. """
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
    def build_music_packet(mode_id: int, sensitivity: int = 100) -> bytes:
        """
        Build a 20-byte BLE packet to activate a music-reactive mode.
        Packet: 0x33 0x05 0x13 <mode_id> <sensitivity> 0x00 ... <checksum>
        sensitivity is clamped to 0-100.
        """
        sensitivity = max(0, min(100, sensitivity))
        payload = [GoveeBLE.LEDMode.MUSIC, mode_id, sensitivity, 0x00]
        frame = bytes([0x33, GoveeBLE.LEDCommand.COLOR] + payload)
        frame += bytes(19 - len(frame))
        frame += bytes([GoveeBLE.sign_payload(frame)])
        return frame

    @staticmethod
    def sign_payload(data):
        """ 'Signs' a payload. Not sure what it does. """
        checksum = 0
        for b in data:
            checksum ^= b
        return checksum & 0xFF
