"""
This class represents Govee light entities.
It only contains the basic methods, and uses govee_ble to talk to govee devices.
"""

from __future__ import annotations

from pathlib import Path
from datetime import timedelta
import logging
import asyncio
import base64
import json
import time
import requests

from homeassistant.components import bluetooth
from homeassistant.components.light import (
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_BRIGHTNESS,
    ATTR_RGB_COLOR,
    ATTR_EFFECT,
    EFFECT_OFF,
    LightEntity)
from homeassistant.components.light.const import LightEntityFeature, ColorMode

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.restore_state import RestoreEntity
import homeassistant.util.color as color_util
from homeassistant.core import HomeAssistant

from .govee_ble import (
    GoveeBLE,
    BLE_IDLE_DISCONNECT_TIMEOUT,
    BLE_INTER_FRAME_DELAY,
    BLE_KEEPALIVE_INTERVAL,
)
from govee_local_api import GoveeDevice
from govee_local_api.message import PtRealMessage
from .const import DOMAIN, CONF_LAN_SKU
from . import Hub

_LOGGER = logging.getLogger(__name__)

_GOVEE_SCENE_DOWNLOAD_TIMEOUT: int = 10  # seconds for scene data HTTP requests


# ---------------------------------------------------------------------------
# Module-level scene helpers (shared by BLE and LAN light entities)
# ---------------------------------------------------------------------------

def _build_ptreal_cmds(scene_code: int, scence_param: str) -> list[str]:
    """Encode a scene into pre-built BLE/ptReal packet frames (base64-encoded 20-byte packets).
    Mirrors SetSceneCode::encode from govee2mqtt/src/ble.rs."""
    payload = base64.b64decode(scence_param)
    raw = bytearray([0xa3, 0x00, 0x01, 0x00, 0x02])  # header; byte[3] patched below
    num_lines = 0
    last_line_marker = 1
    for b in payload:
        if len(raw) % 19 == 0:
            num_lines += 1
            raw.append(0xa3)
            last_line_marker = len(raw)
            raw.append(num_lines)
        raw.append(b)
    raw[last_line_marker] = 0xFF    # mark last data line
    raw[3] = num_lines + 1          # total frame count
    packets = []
    for i in range(0, len(raw), 19):
        chunk = bytes(raw[i: i + 19])
        padded = chunk + bytes(19 - len(chunk))
        xor = 0
        for byte in padded:
            xor ^= byte
        packets.append(padded + bytes([xor]))
    lo = scene_code & 0xFF
    hi = (scene_code >> 8) & 0xFF
    code_pkt = bytes([0x33, 0x05, 0x04, lo, hi]) + bytes(14)
    xor = 0
    for byte in code_pkt:
        xor ^= byte
    packets.append(code_pkt + bytes([xor]))
    return [base64.b64encode(p).decode() for p in packets]


def _parse_api_scene_response(model: str, data: dict) -> list[dict]:
    """Parse a Govee API response into the flat scene list format,
    selecting the model-specific specialEffect scenceParam where available."""
    scenes = []
    for cat in data["data"]["categories"]:
        cat_name = cat["categoryName"]
        for scene in cat["scenes"]:
            for effect in scene["lightEffects"]:
                code = effect["sceneCode"]
                param = effect.get("scenceParam", "")
                for spe in effect.get("specialEffect", []):
                    if model in spe.get("supportSku", []):
                        param = spe["scenceParam"]
                        break
                if not param:
                    continue
                scenes.append({
                    "category": cat_name,
                    "scene_name": scene["sceneName"],
                    "scene_id": scene["sceneId"],
                    "scene_code": code,
                    "scence_param": param,
                    "ptreal_cmds": _build_ptreal_cmds(code, param) if code != 0 else [],
                })
    return scenes


def _download_model_scenes(model: str) -> list[dict]:
    """Download scene data from Govee's public light-effect-library endpoint and parse it."""
    url = f"https://app2.govee.com/appsku/v1/light-effect-libraries?sku={model}"
    headers = {
        "AppVersion": "5.6.01",
        "User-Agent": (
            "GoveeHome/5.6.01 (com.ihoment.GoVeeSensor; build:2; iOS 16.5.0) Alamofire/5.6.4"
        ),
    }
    _LOGGER.info("Downloading scene data for %s from Govee API", model)
    resp = requests.get(url, headers=headers, timeout=_GOVEE_SCENE_DOWNLOAD_TIMEOUT)
    resp.raise_for_status()
    json_path = Path(__file__).parent / "jsons" / f"{model}.json"
    _LOGGER.warning(
        "Scene data for %s was fetched at runtime. "
        "To avoid this on future restarts, save the raw API response alongside the component:\n"
        "  curl -s '%s' \\\n"
        "    -H 'AppVersion: 5.6.01' \\\n"
        "    -H 'User-Agent: GoveeHome/5.6.01 (com.ihoment.GoVeeSensor; build:2; iOS 16.5.0) "
        "Alamofire/5.6.4' \\\n"
        "    -o '%s'",
        model, url, json_path,
    )
    return _parse_api_scene_response(model, resp.json())


def _load_model_scenes_from_file(model: str) -> list[dict]:
    """Load scenes from the local jsons/{model}.json file, or download if not present.
    Accepts both the flat list format and the raw Govee API response dict."""
    json_path = Path(__file__).parent / "jsons" / f"{model}.json"
    if json_path.exists():
        data = json.loads(json_path.read_text())
        if isinstance(data, list):
            _LOGGER.debug("Loaded flat scene data from %s", json_path)
            return data
        if isinstance(data, dict) and "data" in data:
            _LOGGER.debug("Loaded raw API scene data from %s; parsing", json_path)
            return _parse_api_scene_response(model, data)
        _LOGGER.debug("Unrecognised format in %s; downloading fresh data", json_path)
    else:
        _LOGGER.debug("No scene file for %s; downloading from Govee API", model)
    return _download_model_scenes(model)


def build_model_effect_list(
    model: str,
) -> tuple[list[dict], dict[str, int], list[str]]:
    """Load and return *(scenes_data, effect_map, effect_list)* for *model*.
    Intended to be called from an executor thread.
    Music-mode pseudo-effects are appended at the end of *effect_list*."""
    scenes = _load_model_scenes_from_file(model)
    effect_map: dict[str, int] = {}
    effect_list: list[str] = []
    for idx, scene in enumerate(scenes):
        if not scene.get('ptreal_cmds'):
            continue
        name = scene['category'] + ' - ' + scene['scene_name']
        unique_name = name
        counter = 2
        while unique_name in effect_map:
            unique_name = f"{name} ({counter})"
            counter += 1
        effect_map[unique_name] = idx
        effect_list.append(unique_name)
    effect_list.extend(GoveeBLE.MUSIC_MODES.keys())
    _LOGGER.debug("Loaded %d effects for model %s", len(effect_list), model)
    return scenes, effect_map, effect_list


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities):
    hub: Hub = config_entry.runtime_data

    if hub.address is not None:
        ble_device = bluetooth.async_ble_device_from_address(hass, hub.address.upper(), False)
        async_add_entities([GoveeBluetoothLight(hub, ble_device, config_entry)])
    elif hub.lan_device is not None:
        async_add_entities([GoveeLANLight(hub, config_entry)])


class GoveeBluetoothLight(LightEntity):
    _attr_supported_features = LightEntityFeature(LightEntityFeature.EFFECT)
    _attr_supported_color_modes = {ColorMode.RGB}
    _attr_has_entity_name = True

    def __init__(self, hub: Hub, ble_device, config_entry: ConfigEntry) -> None:
        """Initialize a bluetooth light."""

        # Initialize variables.
        self._mac = hub.address
        self._model = config_entry.data["model"]
        self._is_segmented = self._model in GoveeBLE.SEGMENTED_MODELS
        self._use_percent = self._model in GoveeBLE.PERCENT_MODELS
        self._ble_device = ble_device
        self._brightness = 0
        self._state = False
        self._rgb_color = None
        self._client = None
        self._current_effect: str | None = None
        self._effect_list: list[str] | None = None
        self._effect_map: dict[str, int] | None = None
        self._scenes_data: list[dict] | None = None
        self._idle_timeout: int = BLE_IDLE_DISCONNECT_TIMEOUT
        self._last_command_time: float = 0.0
        self._attr_available = False
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._mac)},
            name=self._model,
            manufacturer="Govee",
            model=self._model,
        )

    def _load_effect_list(self) -> list[str]:
        """Load scene/effect list for this model. Runs in an executor thread."""
        scenes, effect_map, effect_list = build_model_effect_list(self._model)
        self._scenes_data = scenes
        self._effect_map = effect_map
        return effect_list

    def _load_scenes_data(self) -> list[dict]:
        """Load scenes from the local JSON file, or download from Govee API if not found.
        Accepts both the flat list format and the raw Govee API response dict."""
        return _load_model_scenes_from_file(self._model)

    def _download_scenes(self) -> list[dict]:
        return _download_model_scenes(self._model)

    def _parse_api_response(self, data: dict) -> list[dict]:
        return _parse_api_scene_response(self._model, data)

    async def _post_command(self) -> None:
        """Record time of last BLE command; disconnect immediately when idle_timeout == 0."""
        self._last_command_time = time.monotonic()
        if self._idle_timeout == 0 and self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:
                pass

    async def async_added_to_hass(self) -> None:
        """Load effect list, query initial device state, and start background keepalive task."""
        _LOGGER.debug("Loading effect list for model %s", self._model)
        try:
            self._effect_list = await self.hass.async_add_executor_job(self._load_effect_list)
            _LOGGER.debug("Effect list loaded: %d effects", len(self._effect_list))
        except Exception as err:
            _LOGGER.error("Failed to load effect list for model %s: %s", self._model, err)

        # Restore last known state immediately so HA has something to show while we connect
        last_state = await self.async_get_last_state()
        if last_state is not None:
            self._state = last_state.state == "on"
            attrs = last_state.attributes
            if attrs.get(ATTR_BRIGHTNESS) is not None:
                self._brightness = attrs[ATTR_BRIGHTNESS]
            if attrs.get(ATTR_RGB_COLOR) is not None:
                self._rgb_color = tuple(attrs[ATTR_RGB_COLOR])
            effect = attrs.get(ATTR_EFFECT)
            self._current_effect = effect if effect and effect != EFFECT_OFF else None
            _LOGGER.debug("Restored last state for %s: on=%s", self._mac, self._state)
            self.async_write_ha_state()

        try:
            self._client = await GoveeBLE.connect_to(self._ble_device, self.unique_id)
            state = await GoveeBLE.query_state(self._client)
            _LOGGER.debug("Initial device state: %s", state)

            if state['power'] is not None:
                self._state = state['power']

            if state['brightness'] is not None:
                raw = state['brightness']
                # Convert percent models back to 0-255 scale for HA
                self._brightness = int(raw * 255 / 100) if self._use_percent else raw

            if state['rgb'] is not None and state['mode'] == GoveeBLE.LEDMode.MANUAL:
                self._rgb_color = state['rgb']

            if state['mode'] == GoveeBLE.LEDMode.MUSIC and state.get('music_mode_id') is not None:
                reverse = {v: k for k, v in GoveeBLE.MUSIC_MODES.items()}
                self._current_effect = reverse.get(state['music_mode_id'])

            self._attr_available = True
            self.async_write_ha_state()
            await self._post_command()
        except Exception as err:
            _LOGGER.error("Failed to query initial state for %s: %s", self._mac, err)
            self.async_write_ha_state()

        self.hass.async_create_background_task(
            self.ensure_connection(), f"govee_ble_keepalive_{self._mac}"
        )

    @property
    def effect_list(self) -> list[str] | None:
        return self._effect_list

    @property
    def effect(self) -> str | None:
        """Return the current effect."""
        return self._current_effect

    @property
    def color_mode(self) -> ColorMode:
        """Return current color mode. BRIGHTNESS when an effect is active."""
        if self._current_effect and self._current_effect != EFFECT_OFF:
            return ColorMode.BRIGHTNESS
        return ColorMode.RGB

    @property
    def name(self) -> str | None:
        """Return None — this is the primary entity for the device."""
        return None

    @property
    def unique_id(self) -> str:
        """Return a unique, Home Assistant friendly identifier for this entity."""
        return self._mac.replace(":", "")

    @property
    def brightness(self):
        return self._brightness

    @property
    def is_on(self) -> bool | None:
        """Return true if light is on."""
        return self._state

    async def async_turn_on(self, **kwargs) -> None:
        if self._client is None:
            self._client = await GoveeBLE.connect_to(self._ble_device, self.unique_id)

        setting_effect = ATTR_EFFECT in kwargs

        # Send power-on first, unless we're setting an effect (effect data should be loaded before activation)
        if not setting_effect:
            await GoveeBLE.send_single_packet(self._client, GoveeBLE.LEDCommand.POWER, [0x1])
        self._state = True

        if ATTR_BRIGHTNESS in kwargs:
            self._brightness = kwargs.get(ATTR_BRIGHTNESS, 255)

            # Some models require a percentage instead of the raw value of a byte.
            await GoveeBLE.send_single_packet(
                self._client,
                GoveeBLE.LEDCommand.BRIGHTNESS, # Command
                [int(self._brightness * 100 / 255) if self._use_percent else self._brightness]) # Data

        if ATTR_RGB_COLOR in kwargs:
            red, green, blue = kwargs.get(ATTR_RGB_COLOR)

            if self._is_segmented:
                await GoveeBLE.send_single_packet(
                    self._client,
                    GoveeBLE.LEDCommand.COLOR, # Command
                    [GoveeBLE.LEDMode.SEGMENTS, 0x01, red, green, blue, 0x00, 0x00, 0x00, 0x00, 0x00, 0xFF, 0x7F]) # Data
            else:
                await GoveeBLE.send_single_packet(
                    self._client,
                    GoveeBLE.LEDCommand.COLOR, # Command
                    [GoveeBLE.LEDMode.MANUAL, red, green, blue]) # Data

            self._rgb_color = (red, green, blue)
            self._current_effect = EFFECT_OFF

        if ATTR_EFFECT in kwargs:
            effect = kwargs.get(ATTR_EFFECT)
            _LOGGER.debug("Effect requested: %r", effect)
            if not effect:
                _LOGGER.warning("Effect name is empty, skipping")
            elif effect in GoveeBLE.MUSIC_MODES:
                # Music-reactive mode — single 20-byte packet, 0x33 0x05 0x13 ...
                mode_id = GoveeBLE.MUSIC_MODES[effect]
                packet = GoveeBLE.build_music_packet(mode_id)
                _LOGGER.debug("Sending music mode %r (id=0x%02x)", effect, mode_id)
                try:
                    await GoveeBLE.send_single_frame(self._client, bytearray(packet))
                    self._current_effect = effect
                    await GoveeBLE.send_single_packet(self._client, GoveeBLE.LEDCommand.POWER, [0x1])
                except Exception as err:
                    _LOGGER.error("Failed to send music mode %r: %s", effect, err)
            elif not self._effect_map:
                _LOGGER.warning("Effect map is not loaded yet, skipping effect %r", effect)
            elif effect not in self._effect_map:
                _LOGGER.warning("Effect %r not found in effect map. Available: %s", effect, list(self._effect_map.keys())[:5])
            else:
                scene_entry = self._scenes_data[self._effect_map[effect]]
                ptreal_cmds = scene_entry['ptreal_cmds']
                _LOGGER.debug("Sending effect %r: %d pre-built packets", effect, len(ptreal_cmds))
                try:
                    for cmd in ptreal_cmds:
                        await GoveeBLE.send_single_frame(self._client, bytearray(base64.b64decode(cmd)))
                        await asyncio.sleep(BLE_INTER_FRAME_DELAY)
                    _LOGGER.debug("Effect %r sent successfully, sending power-on", effect)
                    self._current_effect = effect
                    # Power-on after effect data so the device activates with the effect already loaded
                    await GoveeBLE.send_single_packet(self._client, GoveeBLE.LEDCommand.POWER, [0x1])
                except Exception as err:
                    _LOGGER.error("Failed to send effect %r: %s", effect, err)

        await self._post_command()

    async def async_turn_off(self, **kwargs) -> None:
        if self._client is None:
            self._client = await GoveeBLE.connect_to(self._ble_device, self.unique_id)

        await GoveeBLE.send_single_packet(self._client, GoveeBLE.LEDCommand.POWER, [0x0])
        self._state = False
        self._current_effect = EFFECT_OFF
        await self._post_command()

    async def ensure_connection(self) -> None:
        """
        Background task managing the BLE connection lifetime based on _idle_timeout:
          0   — disconnect-immediately mode: no persistent connection; skip all reconnect logic.
          -1  — keep-alive forever: reconnect and refresh state whenever the link drops.
          >0  — idle-timeout mode: disconnect after _idle_timeout seconds of inactivity;
                do NOT reconnect proactively (reconnect happens per-command via send_single_frame).
        """
        while True:
            await asyncio.sleep(BLE_KEEPALIVE_INTERVAL)

            # Disconnect-immediately mode: nothing to manage.
            if self._idle_timeout == 0:
                continue

            connected = self._client is not None and self._client.is_connected

            # Idle-timeout mode: disconnect when idle; rely on send_single_frame to reconnect.
            if self._idle_timeout > 0:
                if connected:
                    idle = time.monotonic() - self._last_command_time
                    if idle >= self._idle_timeout:
                        _LOGGER.debug(
                            "BLE client for %s idle for %.0fs (>=%ds), disconnecting",
                            self._mac, idle, self._idle_timeout,
                        )
                        try:
                            await self._client.disconnect()
                        except Exception:
                            pass
                continue

            # Keep-alive mode (timeout == -1): reconnect when dropped.
            if connected:
                continue
            try:
                ble_device = bluetooth.async_ble_device_from_address(
                    self.hass, self._mac.upper(), False
                )
                if ble_device is None:
                    _LOGGER.debug("BLE device %s not yet visible, retrying later", self._mac)
                    continue
                self._ble_device = ble_device
                self._client = await GoveeBLE.connect_to(self._ble_device, self.unique_id)
                state = await GoveeBLE.query_state(self._client)
                if state['power'] is not None:
                    self._state = state['power']
                if state['brightness'] is not None:
                    raw = state['brightness']
                    self._brightness = int(raw * 255 / 100) if self._use_percent else raw
                if state['rgb'] is not None and state['mode'] == GoveeBLE.LEDMode.MANUAL:
                    self._rgb_color = state['rgb']
                if state['mode'] == GoveeBLE.LEDMode.MUSIC and state.get('music_mode_id') is not None:
                    reverse = {v: k for k, v in GoveeBLE.MUSIC_MODES.items()}
                    self._current_effect = reverse.get(state['music_mode_id'])
                if not self._attr_available:
                    _LOGGER.info("BLE device %s is available again", self._mac)
                    self._attr_available = True
                self.async_write_ha_state()
            except Exception as err:
                _LOGGER.debug("Keepalive reconnect failed for %s: %s", self._mac, err)
                self._client = None
                if self._attr_available:
                    _LOGGER.warning("BLE device %s is unavailable: %s", self._mac, err)
                    self._attr_available = False
                    self.async_write_ha_state()


class GoveeLANLight(LightEntity):
    """Govee light controlled via the local LAN (UDP/Wi-Fi) API using govee-local-api.

    The GoveeController (held by Hub) sends devStatus requests every 10 seconds and
    pushes state updates into this entity via the device update callback.  Control
    commands are dispatched through GoveeDevice methods.  ptReal scene packets are
    sent as a single batched UDP message via the controller's transport.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_supported_features = LightEntityFeature(LightEntityFeature.EFFECT)

    def __init__(self, hub: "Hub", config_entry: ConfigEntry) -> None:
        self._lan_device: GoveeDevice = hub.lan_device
        self._sku: str = config_entry.data[CONF_LAN_SKU]
        self._unique_id: str = config_entry.unique_id or self._sku

        self._state: bool = False
        self._brightness: int = 0               # 0-255 (HA scale)
        self._rgb_color: tuple[int, int, int] | None = None
        self._color_temp_kelvin: int | None = None
        self._current_effect: str | None = None
        self._effect_list: list[str] | None = None
        self._effect_map: dict[str, int] | None = None
        self._scenes_data: list[dict] | None = None
        self._attr_available = False

        self._attr_supported_color_modes = {ColorMode.RGB, ColorMode.COLOR_TEMP}
        self._attr_color_mode = ColorMode.RGB
        self._attr_min_color_temp_kelvin = 2000
        self._attr_max_color_temp_kelvin = 9000

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._unique_id)},
            name=self._sku,
            manufacturer="Govee",
            model=self._sku,
        )

    # --- HA lifecycle --------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        """Load effects, register update callback, and request an initial status."""
        try:
            scenes, effect_map, effect_list = await self.hass.async_add_executor_job(
                build_model_effect_list, self._sku
            )
            self._scenes_data = scenes
            self._effect_map = effect_map
            # Music modes are only supported via BLE — strip them from LAN options.
            self._effect_list = [
                e for e in effect_list if e not in GoveeBLE.MUSIC_MODES
            ]
            _LOGGER.debug("LAN: loaded %d effects for %s", len(self._effect_list), self._sku)
        except Exception as err:
            _LOGGER.error("Failed to load effects for LAN device %s: %s", self._sku, err)

        # Register callback — the controller calls this whenever a devStatus response
        # arrives for our device (every update_interval seconds).
        self._lan_device.set_update_callback(self._on_device_update)

        # Request an immediate status update so the entity reflects the current state
        # as soon as it is added, without waiting for the next polling cycle.
        self._lan_device.controller.send_update_message()

        def _remove_callback():
            self._lan_device.set_update_callback(None)

        self.async_on_remove(_remove_callback)

    # --- Push-update callback ------------------------------------------------

    def _on_device_update(self, device: GoveeDevice) -> None:
        """Called by the govee-local-api controller when a devStatus response arrives."""
        self._apply_status(device)
        if not self._attr_available:
            _LOGGER.info("LAN device %s (%s) is available", self._sku, device.ip)
            self._attr_available = True
        self.async_write_ha_state()

    # --- Properties ----------------------------------------------------------

    @property
    def unique_id(self) -> str:
        return self._unique_id

    @property
    def name(self) -> str | None:
        return None  # uses device name

    @property
    def is_on(self) -> bool | None:
        return self._state

    @property
    def brightness(self) -> int | None:
        return self._brightness

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        return self._rgb_color

    @property
    def color_temp_kelvin(self) -> int | None:
        return self._color_temp_kelvin

    @property
    def color_mode(self) -> ColorMode:
        if self._current_effect and self._current_effect != EFFECT_OFF:
            return ColorMode.BRIGHTNESS
        return self._attr_color_mode

    @property
    def effect_list(self) -> list[str] | None:
        return self._effect_list

    @property
    def effect(self) -> str | None:
        return self._current_effect

    # --- State mapping -------------------------------------------------------

    def _apply_status(self, device: GoveeDevice) -> None:
        """Map a GoveeDevice state onto entity state attributes."""
        self._state = device.on
        # Library brightness is 0-100; HA expects 0-255
        self._brightness = round(device.brightness * 255 / 100)
        if device.temperature_color > 0:
            self._color_temp_kelvin = device.temperature_color
            self._attr_color_mode = ColorMode.COLOR_TEMP
            self._rgb_color = None
        else:
            self._rgb_color = device.rgb_color
            self._attr_color_mode = ColorMode.RGB
            self._color_temp_kelvin = None

    # --- ptReal scene helper -------------------------------------------------

    def _send_ptreal_scene(self, ptreal_cmds: list[str]) -> None:
        """Send all ptReal packets as a single UDP message via the controller transport.

        The Govee LAN protocol expects the full packet array in one 'ptReal' datagram.
        The library's send_raw_command only supports single-packet sends, so we build
        the message ourselves using govee_local_api.message.PtRealMessage and dispatch
        it directly through the controller's transport.
        """
        controller = self._lan_device.controller
        if controller._transport is None:
            _LOGGER.warning("LAN controller transport not ready; cannot send scene")
            return
        raw_packets = [base64.b64decode(cmd) for cmd in ptreal_cmds]
        msg = PtRealMessage(raw_packets, do_checksum=False)
        controller._transport.sendto(
            bytes(msg), (self._lan_device.ip, controller._device_command_port)
        )

    # --- Control commands ----------------------------------------------------

    async def async_turn_on(self, **kwargs) -> None:
        if ATTR_BRIGHTNESS in kwargs:
            pct = round(kwargs[ATTR_BRIGHTNESS] * 100 / 255)
            await self._lan_device.set_brightness(pct)
            self._brightness = kwargs[ATTR_BRIGHTNESS]

        if ATTR_RGB_COLOR in kwargs:
            r, g, b = kwargs[ATTR_RGB_COLOR]
            await self._lan_device.set_rgb_color(r, g, b)
            self._rgb_color = (r, g, b)
            self._color_temp_kelvin = None
            self._attr_color_mode = ColorMode.RGB
            self._current_effect = EFFECT_OFF

        if ATTR_COLOR_TEMP_KELVIN in kwargs:
            kelvin = kwargs[ATTR_COLOR_TEMP_KELVIN]
            await self._lan_device.set_temperature(kelvin)
            self._color_temp_kelvin = kelvin
            self._rgb_color = None
            self._attr_color_mode = ColorMode.COLOR_TEMP
            self._current_effect = EFFECT_OFF

        if ATTR_EFFECT in kwargs:
            effect = kwargs[ATTR_EFFECT]
            if self._effect_map and effect in self._effect_map:
                scene_entry = self._scenes_data[self._effect_map[effect]]
                ptreal_cmds = scene_entry['ptreal_cmds']
                _LOGGER.debug(
                    "LAN: sending effect %r (%d packets) to %s",
                    effect, len(ptreal_cmds), self._lan_device.ip,
                )
                self._send_ptreal_scene(ptreal_cmds)
                self._current_effect = effect
            else:
                _LOGGER.warning(
                    "LAN: unknown or unloaded effect %r for %s", effect, self._sku
                )

        await self._lan_device.turn_on()
        self._state = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        await self._lan_device.turn_off()
        self._state = False
        self._current_effect = EFFECT_OFF
        self.async_write_ha_state()

