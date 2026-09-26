"""
This class represents Govee light entities.
It only contains the basic methods, and uses govee to talk to govee devices.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components import bluetooth
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_EFFECT,
    ATTR_RGB_COLOR,
    ATTR_TRANSITION,
    EFFECT_OFF,
    LightEntity,
)
from homeassistant.components.light.const import ColorMode, LightEntityFeature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_platform
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.restore_state import RestoreEntity
import voluptuous as vol

from .const import CONF_FADE, CONF_FADE_OFF, CONF_FADE_ON, CONF_LAN_SKU, DEFAULT_FADE, DOMAIN
from .fade import FADE_INTERVAL, run_fade
from .govee_ble import GoveeBLE

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import GoveeCoordinator
    from .fade import RGBColor

_LOGGER = logging.getLogger(__name__)

SERVICE_SET_SEGMENT_COLOR = "set_segment_color"
ATTR_SEGMENTS = "segments"


async def async_setup_entry(
    hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    hub: GoveeCoordinator = config_entry.runtime_data
    entities: list[LightEntity] = [GoveeLight(hub, config_entry)]
    entities.extend(GoveeSegmentLight(hub, index) for index in range(hub.segment_count))
    async_add_entities(entities)

    entity_platform.async_get_current_platform().async_register_entity_service(
        SERVICE_SET_SEGMENT_COLOR,
        {
            vol.Required(ATTR_SEGMENTS): vol.All(
                cv.ensure_list, [vol.All(vol.Coerce(int), vol.Range(min=1))]
            ),
            vol.Required(ATTR_RGB_COLOR): vol.All(
                vol.Coerce(tuple), vol.ExactSequence((cv.byte, cv.byte, cv.byte))
            ),
            vol.Optional(ATTR_BRIGHTNESS): vol.All(vol.Coerce(int), vol.Range(min=0, max=255)),
        },
        "async_set_segment_color",
    )


def _scale(color: RGBColor, brightness: int) -> RGBColor:
    """Emulate per-segment brightness by scaling the colour - the protocol has
    no per-segment brightness command (MineNights/govee_lights c3a2545)."""
    r, g, b = (round(channel * brightness / 255) for channel in color)
    return (r, g, b)


class GoveeLight(LightEntity, RestoreEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_supported_features = LightEntityFeature.EFFECT | LightEntityFeature.TRANSITION

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

        # Software crossfade durations (seconds; 0 = instant), per-device
        # options set via the config-entry options flow. Govee firmware has
        # no native fade - see fade.py.
        options = config_entry.options
        self._fade: float = options.get(CONF_FADE, DEFAULT_FADE)
        self._fade_on: float = options.get(CONF_FADE_ON, DEFAULT_FADE)
        self._fade_off: float = options.get(CONF_FADE_OFF, DEFAULT_FADE)

        # Bumped on every new fade; a stale in-flight one checks this and
        # stops rather than interleaving frames.
        self._render_epoch = 0

        # True while a fade owns the displayed colour.
        # _handle_coordinator_update checks it so a routine
        # device notification (e.g. the state query _ensure_connected always
        # runs right after reconnecting, mid-fade if the link had gone idle)
        # can't stomp the render's own frames with a stale colour. Found live
        # (2026-09-25): the BLE traffic itself faded cleanly the whole time -
        # only the reported entity state glitched.
        self._fading = False

        # The colour/CT in effect just before the last fade-to-black, so a
        # later bare turn-on (no explicit colour) restores it instead of
        # powering on dark. Cleared whenever an explicit colour/CT/effect is
        # set - only a fade-to-black should make a bare turn-on repaint.
        self._pre_off_rgb_color: tuple[int, int, int] | None = None
        self._pre_off_color_temp_kelvin: int | None = None

    @property
    def color_mode(self) -> ColorMode | None:
        """BRIGHTNESS while a scene runs (HA allows it only then); once no
        effect is active, the last real mode - even on paths that end a
        scene without committing a colour (e.g. the segment service), where
        HA would otherwise reject the state write."""
        mode = self._attr_color_mode
        if mode == ColorMode.BRIGHTNESS and self._attr_effect in (None, EFFECT_OFF):
            return ColorMode.COLOR_TEMP if self._attr_color_temp_kelvin else ColorMode.RGB
        return mode

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Diagnostic attributes for automations and support."""
        attrs: dict[str, Any] = {"govee_model": self._device_label}

        if self._coordinator.segment_count:
            attrs["govee_segments"] = self._coordinator.segment_count

        rssi = self._rssi()
        if rssi is not None:
            attrs["govee_rssi"] = rssi

        return attrs

    def _rssi(self) -> int | None:
        """Return the last reported RSSI for a BLE device, if known.

        LAN coordinators have no ``address`` attribute (govee-local-api's UDP
        transport has no RSSI concept), so this is naturally BLE-only.
        """
        address = getattr(self._coordinator, "address", None)
        if address is None:
            return None
        info = bluetooth.async_last_service_info(self.hass, address.upper(), True)
        return info.rssi if info is not None else None

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
        # Initial connect/state-query is triggered once from
        # __init__.py::async_setup_entry (shared by all platforms), not here.

    @callback
    def _handle_coordinator_update(self) -> None:
        coord = self._coordinator
        self._attr_available = coord.available

        self._attr_is_on = coord.is_on
        if coord.brightness_raw:
            self._attr_brightness = coord.brightness

        # While a fade runs, it - not the coordinator - owns the displayed
        # colour; see _fading.
        if not self._fading:
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

    async def _async_commit_rgb(self, color: RGBColor) -> None:
        """Apply an exact RGB colour now (verified write) and update state."""
        await self._coordinator.async_set_rgb_color(*color, verify=True)
        self._attr_rgb_color = color
        self._attr_color_temp_kelvin = None
        self._attr_color_mode = ColorMode.RGB

    async def _async_commit_color_temp(self, kelvin: int) -> None:
        """Apply an exact colour temperature now (verified write) and update state."""
        await self._coordinator.async_set_color_temp(kelvin, verify=True)
        self._attr_color_temp_kelvin = kelvin
        self._attr_rgb_color = None
        self._attr_color_mode = ColorMode.COLOR_TEMP

    async def _async_fade_to(
        self,
        target_rgb: RGBColor,
        fade: float,
        start_from_black: bool,
        commit_final: Callable[[], Awaitable[None]],
    ) -> bool:
        """Crossfade the visual colour toward *target_rgb*, then apply the
        exact requested state via *commit_final* (a plain RGB set, or a
        native colour-temperature command). Returns True if this render was
        still the current one when it finished (see below) - False means a
        newer command superseded it, and the caller should not act as if
        its own follow-up (e.g. turn_off's power-off after a fade-to-black)
        is still wanted.

        Firmware has no native fade for either, so every intermediate frame
        is an RGB approximation sent without state verification (a fade can
        be dozens of frames; verifying each would flood the device with
        state queries - see govee_ble.py's ``verify`` plumbing). Only the
        final frame is the exact, verified command actually requested -
        this matters even when the animated approach lands on the same
        RGB values, since it can also be a mode change (e.g. into true
        colour-temperature/white-balance mode).

        Only the most recent render survives: a newer call bumps the render
        epoch, and this fade (and any in-flight one) aborts before writing
        its next frame. Adapted from Laserology/govee_ble_lights'
        ``_async_render_target`` (commits `9a0b66f`, `174c1fd`); the return
        value plays the role of their separate operation serial (a turn-on
        racing a fade-out must cancel the stale power-off).
        """
        self._render_epoch += 1
        my_epoch = self._render_epoch
        self._fading = True

        start: RGBColor | None = None
        if start_from_black:
            start = (0, 0, 0)
        elif self._attr_rgb_color is not None:
            start = self._attr_rgb_color
        elif self._attr_color_temp_kelvin is not None:
            start = GoveeBLE.kelvin_to_rgb(self._attr_color_temp_kelvin)

        async def send_frame(color: RGBColor) -> None:
            if self._render_epoch != my_epoch:
                return
            if color == target_rgb:
                await commit_final()
            else:
                await self._coordinator.async_set_rgb_color(*color, verify=False)
                self._attr_rgb_color = color
                self._attr_color_temp_kelvin = None
                self._attr_color_mode = ColorMode.RGB
            self.async_write_ha_state()

        try:
            if start is None or fade <= 0:
                await send_frame(target_rgb)
            else:
                await run_fade(
                    start, target_rgb, fade, FADE_INTERVAL, send_frame,
                    lambda: self._render_epoch == my_epoch,
                )
        finally:
            # Only release ownership if a newer fade hasn't already taken
            # it - otherwise this stale fade's cleanup would incorrectly
            # hand control back to the coordinator mid-newer-fade.
            if self._render_epoch == my_epoch:
                self._fading = False

        return self._render_epoch == my_epoch

    # ── Service: set_segment_color ───────────────────────────────────────────

    async def async_set_segment_color(
        self,
        segments: list[int],
        rgb_color: RGBColor,
        brightness: int | None = None,
    ) -> None:
        """Paint the given segments (1-based) one colour, others untouched."""
        hub = self._coordinator
        count = hub.segment_count
        if not count:
            raise ServiceValidationError(f"{self._device_label} has no segment control")
        invalid = sorted(s for s in set(segments) if not 1 <= s <= count)
        if invalid:
            raise ServiceValidationError(
                f"{self._device_label} has segments 1-{count}; got {invalid}"
            )
        color = _scale(rgb_color, 255 if brightness is None else brightness)
        if not hub.is_on:
            await hub.async_turn_on()
        await hub.async_set_segment_colors({s - 1: color for s in segments})
        self._attr_effect = EFFECT_OFF
        self.async_write_ha_state()

    # ── Turn on / off ────────────────────────────────────────────────────────

    def _resume_target_rgb(self) -> RGBColor:
        """Colour to repaint on a bare turn-on: the colour active before the
        last fade-to-black, else whatever is already tracked, else white -
        so a strip with no known colour at all never powers on dark."""
        return self._pre_off_rgb_color or self._attr_rgb_color or (255, 255, 255)

    async def _async_bare_turn_on(self, transition: float | None) -> None:
        """Turn-on with no colour, CT or effect given, from off: repaint the
        last known colour - the device has no memory of its own, so without
        this it would power on dark (Laserology/govee_ble_lights c15d311)."""
        fade = transition if transition is not None else self._fade_on
        if self._pre_off_color_temp_kelvin is not None:
            kelvin = self._pre_off_color_temp_kelvin
            await self._async_fade_to(
                GoveeBLE.kelvin_to_rgb(kelvin), fade, True,
                lambda: self._async_commit_color_temp(kelvin),
            )
        else:
            target = self._resume_target_rgb()
            await self._async_fade_to(
                target, fade, True, lambda: self._async_commit_rgb(target)
            )

    async def async_turn_on(self, **kwargs: Any) -> None:
        was_off = not self._attr_is_on
        effect_name: str | None = kwargs.get(ATTR_EFFECT)
        # Cloud scenes power the device on themselves (their ptReal burst ends
        # in a POWER packet); everything else needs it here.
        cloud_effect = ATTR_EFFECT in kwargs and effect_name != EFFECT_OFF
        transition = kwargs.get(ATTR_TRANSITION)
        self._attr_is_on = True
        try:
            if not cloud_effect:
                await self._coordinator.async_turn_on()

            if ATTR_BRIGHTNESS in kwargs:
                self._attr_brightness = kwargs[ATTR_BRIGHTNESS]
                await self._coordinator.async_set_brightness(kwargs[ATTR_BRIGHTNESS])

            if ATTR_RGB_COLOR in kwargs:
                r, g, b = kwargs[ATTR_RGB_COLOR]
                fade = transition if transition is not None else (
                    self._fade_on if was_off else self._fade
                )
                await self._async_fade_to(
                    (r, g, b), fade, was_off, lambda: self._async_commit_rgb((r, g, b))
                )
                self._attr_effect = EFFECT_OFF
                self._pre_off_rgb_color = None
                self._pre_off_color_temp_kelvin = None

            elif ATTR_COLOR_TEMP_KELVIN in kwargs:
                kelvin = kwargs[ATTR_COLOR_TEMP_KELVIN]
                fade = transition if transition is not None else (
                    self._fade_on if was_off else self._fade
                )
                await self._async_fade_to(
                    GoveeBLE.kelvin_to_rgb(kelvin), fade, was_off,
                    lambda: self._async_commit_color_temp(kelvin),
                )
                self._attr_effect = EFFECT_OFF
                self._pre_off_rgb_color = None
                self._pre_off_color_temp_kelvin = None

            elif ATTR_EFFECT not in kwargs and was_off:
                await self._async_bare_turn_on(transition)

            if effect_name == EFFECT_OFF:
                # Leaving an effect: repaint the last solid colour so the
                # pattern is actually cleared.
                await self._async_commit_rgb(self._resume_target_rgb())
                self._attr_effect = EFFECT_OFF
            elif ATTR_EFFECT in kwargs:
                _LOGGER.debug("Effect requested: %r", effect_name)
                if not effect_name:
                    _LOGGER.warning("Effect name is empty, skipping")
                else:
                    await self._coordinator.async_apply_effect(effect_name)
                    self._attr_effect = effect_name
                    self._attr_color_mode = ColorMode.BRIGHTNESS
        except Exception as err:
            _LOGGER.error("Failed to send command to %s: %s", self._attr_unique_id, err)

        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        # Remember the real colour before any fade-to-black erases it, so a
        # later bare turn-on can restore it instead of powering on dark.
        self._pre_off_rgb_color = self._attr_rgb_color
        self._pre_off_color_temp_kelvin = self._attr_color_temp_kelvin

        transition = kwargs.get(ATTR_TRANSITION)
        fade_off = transition if transition is not None else self._fade_off

        if fade_off > 0:
            try:
                still_current = await self._async_fade_to(
                    (0, 0, 0), fade_off, False, lambda: self._async_commit_rgb((0, 0, 0))
                )
            except Exception as err:
                _LOGGER.error(
                    "Failed to fade %s to black before turn-off: %s", self._attr_unique_id, err
                )
            else:
                if not still_current:
                    # A newer command (e.g. a turn-on) superseded this
                    # fade-to-black; sending a stale power-off now would
                    # undo it.
                    return

        try:
            await self._coordinator.async_turn_off()
        except Exception as err:
            _LOGGER.error("Failed to send turn-off command to %s: %s", self._attr_unique_id, err)
        self._attr_is_on = False
        self._attr_effect = EFFECT_OFF
        self.async_write_ha_state()


class GoveeSegmentLight(LightEntity):
    """One addressable segment of a strip, as its own light.

    Disabled by default - a 15-segment strip would otherwise add 15 entities
    nobody asked for; the ``govee_lights.set_segment_color`` service on the
    main light covers most uses without them. Per-segment brightness is
    emulated by scaling the colour. State comes from real per-segment
    readback on BLE (SEGMENT group queries, only sent while at least one of
    these is enabled); LAN has no readback, so there it's optimistic.

    Adapted from MineNights/govee_lights c3a2545 - generalised from H617E
    only to every segmented model, sized per model, and without
    RestoreEntity (which, lacking readback, claimed colours the strip might
    not have after a restart).
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_registry_enabled_default = False
    _attr_translation_key = "segment"

    def __init__(self, hub: GoveeCoordinator, index: int) -> None:
        self._coordinator = hub
        self._index = index
        self._attr_unique_id = f"{hub.unique_device_id}_segment_{index + 1}"
        self._attr_translation_placeholders = {"segment": str(index + 1)}
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, hub.unique_device_id)})
        self._attr_supported_color_modes = {ColorMode.RGB}
        self._attr_color_mode = ColorMode.RGB

    @property
    def _color(self) -> RGBColor | None:
        colors = self._coordinator.segment_colors
        return colors[self._index] if self._index < len(colors) else None

    @property
    def available(self) -> bool:
        return self._coordinator.available

    @property
    def assumed_state(self) -> bool:
        return not self._coordinator.segment_readback or self._color is None

    @property
    def is_on(self) -> bool:
        if not self._coordinator.is_on:
            return False
        color = self._color
        return color is None or color != (0, 0, 0)

    @property
    def brightness(self) -> int | None:
        color = self._color
        return max(color) if color else None

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        color = self._color
        if not color or max(color) == 0:
            return None
        peak = max(color)
        r, g, b = (round(channel * 255 / peak) for channel in color)
        return (r, g, b)

    async def async_added_to_hass(self) -> None:
        hub = self._coordinator
        hub.segment_state_listeners += 1

        def _release() -> None:
            hub.segment_state_listeners -= 1

        self.async_on_remove(_release)
        self.async_on_remove(hub.async_add_listener(self.async_write_ha_state))

    async def async_turn_on(self, **kwargs: Any) -> None:
        base = kwargs.get(ATTR_RGB_COLOR) or self.rgb_color or (255, 255, 255)
        current = self.brightness
        brightness = kwargs.get(ATTR_BRIGHTNESS) or (current if current else 255)
        hub = self._coordinator
        if not hub.is_on:
            await hub.async_turn_on()
        await hub.async_set_segment_colors({self._index: _scale(tuple(base), brightness)})

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._coordinator.async_set_segment_colors({self._index: (0, 0, 0)})

    async def async_set_segment_color(self, **kwargs: Any) -> None:
        raise ServiceValidationError(
            "set_segment_color targets the main Govee light entity, not a segment"
        )
