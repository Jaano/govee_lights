"""
This class represents Govee light entities.
It only contains the basic methods, and uses govee to talk to govee devices.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_EFFECT,
    ATTR_RGB_COLOR,
    EFFECT_OFF,
    LightEntity,
)
from homeassistant.components.light.const import ColorMode, LightEntityFeature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.restore_state import RestoreEntity

from .const import CONF_LAN_SKU, DOMAIN

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import GoveeCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    hub: GoveeCoordinator = config_entry.runtime_data
    async_add_entities([GoveeLight(hub, config_entry)])


class GoveeLight(LightEntity, RestoreEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_supported_features = LightEntityFeature.EFFECT

    def __init__(self, hub: GoveeCoordinator, config_entry: ConfigEntry) -> None:
        self._coordinator: GoveeCoordinator = hub
        self._device_label: str = config_entry.data.get("model") or config_entry.data[CONF_LAN_SKU]
        self._attr_unique_id: str | None = hub.unique_device_id
        self._attr_name: str | None = None

        self._attr_supported_color_modes = hub.supported_color_modes
        if hub.min_color_temp_kelvin is not None:
            self._attr_min_color_temp_kelvin = hub.min_color_temp_kelvin
        if hub.max_color_temp_kelvin is not None:
            self._attr_max_color_temp_kelvin = hub.max_color_temp_kelvin

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, hub.unique_device_id)},
            name=self._device_label,
            manufacturer="Govee",
            model=self._device_label,
        )

        self._attr_is_on: bool | None = False
        self._attr_brightness: int | None = 0
        self._attr_rgb_color: tuple[int, int, int] | None = None
        self._attr_color_temp_kelvin: int | None = None
        self._attr_color_mode: ColorMode | None = ColorMode.RGB
        self._attr_effect: str | None = None
        self._attr_effect_list: list[str] | None = None
        self._attr_available = False

    async def async_added_to_hass(self) -> None:
        _LOGGER.debug("Loading effect list for %s", self._device_label)
        try:
            await self._coordinator.async_load_effects(self.hass.config.config_dir)
            self._attr_effect_list = self._coordinator.effect_list
            effect_count = len(self._attr_effect_list or [])
            _LOGGER.debug("Loaded %d effects for %s", effect_count, self._device_label)
        except Exception as err:
            _LOGGER.error("Failed to load effect list for %s: %s", self._device_label, err)

        last_state = await self.async_get_last_state()
        if last_state is not None:
            self._attr_is_on = last_state.state == "on"
            attrs = last_state.attributes
            if attrs.get(ATTR_BRIGHTNESS) is not None:
                self._attr_brightness = attrs[ATTR_BRIGHTNESS]
            if attrs.get(ATTR_RGB_COLOR) is not None:
                self._attr_rgb_color = tuple(attrs[ATTR_RGB_COLOR])
            effect = attrs.get(ATTR_EFFECT)
            self._attr_effect = effect if effect and effect != EFFECT_OFF else None
            _LOGGER.debug(
                "Restored last state for %s: on=%s", self._attr_unique_id, self._attr_is_on
            )
            self.async_write_ha_state()

        self.async_on_remove(
            self._coordinator.async_add_listener(self._handle_coordinator_update)
        )

        if self._coordinator.setup_in_background:
            self.hass.async_create_background_task(
                self._coordinator.async_setup(),
                f"govee_lights_initial_connect_{self._attr_unique_id}",
            )
        else:
            await self._coordinator.async_setup()

    @callback
    def _handle_coordinator_update(self) -> None:
        coord = self._coordinator
        self._attr_available = coord.available

        self._attr_is_on = coord.is_on
        if coord.brightness_raw:
            self._attr_brightness = coord.brightness

        if coord.color_temp_kelvin:
            self._attr_color_temp_kelvin = coord.color_temp_kelvin
            self._attr_color_mode = ColorMode.COLOR_TEMP
            self._attr_rgb_color = None
        elif coord.current_rgb_color is not None:
            self._attr_rgb_color = coord.current_rgb_color
            self._attr_color_mode = ColorMode.RGB
            self._attr_color_temp_kelvin = None

        inferred = coord.inferred_effect
        if inferred is not None:
            self._attr_effect = inferred
            self._attr_color_mode = ColorMode.BRIGHTNESS

        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        setting_effect = ATTR_EFFECT in kwargs
        self._attr_is_on = True
        try:
            if not setting_effect:
                await self._coordinator.async_turn_on()

            if ATTR_BRIGHTNESS in kwargs:
                self._attr_brightness = kwargs[ATTR_BRIGHTNESS]
                await self._coordinator.async_set_brightness(kwargs[ATTR_BRIGHTNESS])

            if ATTR_RGB_COLOR in kwargs:
                r, g, b = kwargs[ATTR_RGB_COLOR]
                await self._coordinator.async_set_rgb_color(r, g, b)
                self._attr_rgb_color = (r, g, b)
                self._attr_color_temp_kelvin = None
                self._attr_color_mode = ColorMode.RGB
                self._attr_effect = EFFECT_OFF

            if ATTR_COLOR_TEMP_KELVIN in kwargs:
                kelvin = kwargs[ATTR_COLOR_TEMP_KELVIN]
                await self._coordinator.async_set_color_temp(kelvin)
                self._attr_color_temp_kelvin = kelvin
                self._attr_rgb_color = None
                self._attr_color_mode = ColorMode.COLOR_TEMP
                self._attr_effect = EFFECT_OFF

            if ATTR_EFFECT in kwargs:
                effect = kwargs[ATTR_EFFECT]
                _LOGGER.debug("Effect requested: %r", effect)
                if not effect:
                    _LOGGER.warning("Effect name is empty, skipping")
                else:
                    await self._coordinator.async_apply_effect(effect)
                    self._attr_effect = effect
                    self._attr_color_mode = ColorMode.BRIGHTNESS
        except Exception as err:
            _LOGGER.error("Failed to send command to %s: %s", self._attr_unique_id, err)

        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        try:
            await self._coordinator.async_turn_off()
        except Exception as err:
            _LOGGER.error("Failed to send turn-off command to %s: %s", self._attr_unique_id, err)
        self._attr_is_on = False
        self._attr_effect = EFFECT_OFF
        self.async_write_ha_state()

