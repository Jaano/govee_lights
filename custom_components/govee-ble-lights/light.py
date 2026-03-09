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
    LightEntityFeature,
    LightEntity,
    ColorMode)

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.storage import Store
import homeassistant.util.color as color_util
from homeassistant.core import HomeAssistant

from .govee_ble import (
    GoveeBLE,
    BLE_IDLE_DISCONNECT_TIMEOUT,
    BLE_INTER_FRAME_DELAY,
    BLE_KEEPALIVE_INTERVAL,
)
from .govee_api import GOVEE_API_TIMEOUT
from .const import DOMAIN
from . import Hub

_LOGGER = logging.getLogger(__name__)

# Polling interval for GoveeAPILight (cloud API entities)
SCAN_INTERVAL = timedelta(minutes=5)

async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities):
    hub: Hub = config_entry.runtime_data

    if hub.devices is not None:
        devices = hub.devices
        for device in devices:
            if device['type'] == 'devices.types.light':
                _LOGGER.info("Adding device: %s", device)
                async_add_entities([GoveeAPILight(hub, device)])
    elif hub.address is not None:
        ble_device = bluetooth.async_ble_device_from_address(hass, hub.address.upper(), False)
        async_add_entities([GoveeBluetoothLight(hub, ble_device, config_entry)])

class GoveeAPILight(LightEntity, dict):
    _attr_color_mode = ColorMode.RGB
    _attr_has_entity_name = True

    def __init__(self, hub: Hub, device: dict) -> None:
        """Initialize an API light."""
        super().__init__()

        self.hub = hub

        self._state = None
        self._brightness = None

        self.device_data = device
        self.sku = self.device_data["sku"]
        self.device = self.device_data["device"]

        self._attr_name = device["deviceName"]

        color_modes: set[ColorMode] = set()

        for cap in device["capabilities"]:
            if cap['instance'] == 'powerSwitch':
                color_modes.add(ColorMode.ONOFF)
            if cap['instance'] == 'brightness':
                color_modes.add(ColorMode.BRIGHTNESS)
            if cap['instance'] == 'colorTemperatureK':
                color_modes.add(ColorMode.COLOR_TEMP)
                self._attr_min_color_temp_kelvin = cap['parameters']['range']['min']
                self._attr_max_color_temp_kelvin = cap['parameters']['range']['max']
                self._attr_min_mireds = color_util.color_temperature_kelvin_to_mired(self._attr_min_color_temp_kelvin)
                self._attr_max_mireds = color_util.color_temperature_kelvin_to_mired(self._attr_max_color_temp_kelvin)
            if cap['instance'] == 'colorRgb':
                color_modes.add(ColorMode.RGB)
            if cap['instance'] == 'lightScene':
                self._attr_supported_features = LightEntityFeature(
                    LightEntityFeature.EFFECT | LightEntityFeature.FLASH | LightEntityFeature.TRANSITION
                )

        if ColorMode.ONOFF in color_modes:
            self._attr_supported_color_modes = {ColorMode.ONOFF}
        if ColorMode.BRIGHTNESS in color_modes:
            self._attr_supported_color_modes = {ColorMode.BRIGHTNESS}
        if ColorMode.COLOR_TEMP in color_modes:
            self._attr_supported_color_modes = {ColorMode.COLOR_TEMP}
        if ColorMode.RGB in color_modes:
            self._attr_supported_color_modes = {ColorMode.RGB}

        self._state = None
        self._brightness = None
        self._rgb_color = None
        self._attr_available = True

    async def async_update(self):
        """Retrieve latest state."""
        _LOGGER.info("Updating device: %s", self.device_data)

        try:
            state = await self.hub.api.get_device_state(self.sku, self.device)
            for cap in state["capabilities"]:
                if cap['instance'] == 'powerSwitch':
                    self._state = cap['state']['value'] == 1
                if cap['instance'] == 'brightness':
                    self._brightness = cap['state']['value']
                if cap['instance'] == 'colorTemperatureK':
                    value = cap['state']['value']
                    if value != 0:
                        self._attr_color_temp_kelvin = value
                        self._attr_color_temp = color_util.color_temperature_kelvin_to_mired(value)
                if cap['instance'] == 'colorRgb':
                    num = cap['state']['value']
                    self._attr_rgb_color = ((num >> 16) & 0xFF, (num >> 8) & 0xFF, num & 0xFF)
            if not self._attr_available:
                _LOGGER.info("Device %s is available again", self.device)
                self._attr_available = True
        except Exception as err:
            if self._attr_available:
                _LOGGER.warning("Device %s is unavailable: %s", self.device, err)
                self._attr_available = False

    async def async_added_to_hass(self) -> None:
        """Load scenes once the entity is added and self.hass is available."""
        if LightEntityFeature.EFFECT in self.supported_features:
            if not self._attr_effect_list:
                _LOGGER.info("Loading effects for %s", self.device_data)
                try:
                    store = Store(self.hass, 1, f"{DOMAIN}/effect_list_{self.sku}.json")
                    scenes = await self.hub.api.list_scenes(self.sku, self.device)
                    await store.async_save(scenes)
                    self._attr_effect_list = [scene['name'] for scene in scenes]
                except Exception as err:
                    _LOGGER.error("Failed to load effects for %s: %s", self.sku, err)

    @property
    def name(self) -> str:
        return self._attr_name

    @property
    def unique_id(self) -> str:
        return self.device

    @property
    def brightness(self):
        # HA expects 0-255; Govee API returns 0-100
        return round(self._brightness * 255 / 100) if self._brightness is not None else None

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        return self._rgb_color

    @property
    def is_on(self) -> bool | None:
        return self._state

    async def async_turn_on(self, **kwargs) -> None:
        self._state = True

        if ATTR_BRIGHTNESS in kwargs:
            brightness = kwargs.get(ATTR_BRIGHTNESS, 255)
            pct = round(brightness * 100 / 255)
            await self.hub.api.set_brightness(self.sku, self.device, pct)
            self._brightness = pct  # store as 0-100 to match API

        if ATTR_RGB_COLOR in kwargs:
            red, green, blue = kwargs.get(ATTR_RGB_COLOR)
            await self.hub.api.set_color_rgb(self.sku, self.device, red, green, blue)

        if ATTR_COLOR_TEMP_KELVIN in kwargs:
            kelvin = kwargs.get(ATTR_COLOR_TEMP_KELVIN)
            await self.hub.api.set_color_temp(self.sku, self.device, kelvin)

        if ATTR_EFFECT in kwargs:
            effect_name = kwargs.get(ATTR_EFFECT)
            store = Store(self.hass, 1, f"{DOMAIN}/effect_list_{self.sku}.json")
            stored = await store.async_load() or []
            scene = next((s for s in stored if s['name'] == effect_name), None)
            if scene is None:
                _LOGGER.warning("Effect %r not found in scene store", effect_name)
            else:
                _LOGGER.info("Set scene: %s", scene)
                await self.hub.api.set_scene(self.sku, self.device, scene['value'])

        await self.hub.api.toggle_power(self.sku, self.device, 1)

    async def async_turn_off(self, **kwargs) -> None:
        await self.hub.api.toggle_power(self.sku, self.device, 0)
        self._state = False

class GoveeBluetoothLight(LightEntity, RestoreEntity):
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
        """Load scenes from jsons/{model}.json (flat format) or download from Govee API.
        Runs in an executor thread."""
        scenes = self._load_scenes_data()
        self._scenes_data = scenes
        self._effect_map = {}
        effect_list = []
        for idx, scene in enumerate(scenes):
            if not scene.get('ptreal_cmds'):
                continue  # skip animation-only scenes without a BLE activation code
            name = scene['category'] + ' - ' + scene['scene_name']
            unique_name = name
            counter = 2
            while unique_name in self._effect_map:
                unique_name = f"{name} ({counter})"
                counter += 1
            self._effect_map[unique_name] = idx
            effect_list.append(unique_name)
        # Append static music-mode effects at the end
        effect_list.extend(GoveeBLE.MUSIC_MODES.keys())
        _LOGGER.debug("Loaded %d effects for model %s", len(effect_list), self._model)
        return effect_list

    def _load_scenes_data(self) -> list[dict]:
        """Load scenes from the local JSON file, or download from Govee API if not found.
        Accepts both the flat list format and the raw Govee API response dict."""
        json_path = Path(__file__).parent / "jsons" / f"{self._model}.json"
        if json_path.exists():
            data = json.loads(json_path.read_text())
            if isinstance(data, list):
                _LOGGER.debug("Loaded flat scene data from %s", json_path)
                return data
            if isinstance(data, dict) and "data" in data:
                _LOGGER.debug("Loaded raw API scene data from %s; parsing", json_path)
                return self._parse_api_response(data)
            _LOGGER.debug("Unrecognised format in %s; downloading fresh data", json_path)
        else:
            _LOGGER.debug("No scene file for %s; downloading from Govee API", self._model)
        return self._download_scenes()

    def _download_scenes(self) -> list[dict]:
        """Download scene data from Govee's public light-effect-library endpoint and parse it."""
        url = f"https://app2.govee.com/appsku/v1/light-effect-libraries?sku={self._model}"
        headers = {
            "AppVersion": "5.6.01",
            "User-Agent": (
                "GoveeHome/5.6.01 (com.ihoment.GoVeeSensor; build:2; iOS 16.5.0) Alamofire/5.6.4"
            ),
        }
        _LOGGER.info("Downloading scene data for %s from Govee API", self._model)
        resp = requests.get(url, headers=headers, timeout=GOVEE_API_TIMEOUT)
        resp.raise_for_status()
        json_path = Path(__file__).parent / "jsons" / f"{self._model}.json"
        _LOGGER.warning(
            "Scene data for %s was fetched at runtime. "
            "To avoid this on future restarts, save the raw API response alongside the component:\n"
            "  curl -s '%s' \\\'\n"
            "    -H 'AppVersion: 5.6.01' \\\'\n"
            "    -H 'User-Agent: GoveeHome/5.6.01 (com.ihoment.GoVeeSensor; build:2; iOS 16.5.0) Alamofire/5.6.4' \\\'\n"
            "    -o '%s'",
            self._model, url, json_path,
        )
        return self._parse_api_response(resp.json())

    def _parse_api_response(self, data: dict) -> list[dict]:
        """Parse a Govee API response into the flat scene list format,
        selecting the model-specific specialEffect scenceParam where available."""
        scenes = []
        for cat in data["data"]["categories"]:
            cat_name = cat["categoryName"]
            for scene in cat["scenes"]:
                for effect in scene["lightEffects"]:
                    code = effect["sceneCode"]
                    # Use the specialEffect entry matching this model; fall back to base param
                    param = effect.get("scenceParam", "")
                    for spe in effect.get("specialEffect", []):
                        if self._model in spe.get("supportSku", []):
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
                        "ptreal_cmds": self._build_ptreal_cmds(code, param) if code != 0 else [],
                    })
        return scenes

    async def _post_command(self) -> None:
        """Record time of last BLE command; disconnect immediately when idle_timeout == 0."""
        self._last_command_time = time.monotonic()
        if self._idle_timeout == 0 and self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:
                pass

    @staticmethod
    def _build_ptreal_cmds(scene_code: int, scence_param: str) -> list[str]:
        """Encode a scene into pre-built BLE packet frames (base64-encoded 20-byte packets).
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
