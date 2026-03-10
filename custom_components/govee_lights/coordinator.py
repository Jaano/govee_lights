from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

if TYPE_CHECKING:
    import logging

    from homeassistant.components.light.const import ColorMode
    from homeassistant.core import HomeAssistant


class GoveeCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Base coordinator shared by GoveeBLECoordinator and GoveeLANCoordinator."""

    def __init__(self, hass: HomeAssistant, logger: logging.Logger, name: str) -> None:
        super().__init__(hass, logger, name=name, update_interval=None)
        self._available: bool = False
        self._effect_list: list[str] | None = None
        self._effect_map: dict[str, int] | None = None
        self._scenes_data: list[dict[str, Any]] | None = None
        # Shared device state (updated from transport callbacks)
        self.is_on: bool = False
        self.brightness_raw: int = 0
        self.rgb_color: tuple[int, int, int] | None = None
        self.color_temp_kelvin: int | None = None

    @property
    def available(self) -> bool:
        return self._available

    @property
    def effect_list(self) -> list[str] | None:
        return self._effect_list

    @property
    def effect_map(self) -> dict[str, int] | None:
        return self._effect_map

    @property
    def scenes_data(self) -> list[dict[str, Any]] | None:
        return self._scenes_data

    # ── Interface (implemented by subclasses) ──────────────────────────────

    @property
    def unique_device_id(self) -> str:
        raise NotImplementedError

    @property
    def device_key(self) -> str:
        raise NotImplementedError

    @property
    def supported_color_modes(self) -> set[ColorMode]:
        raise NotImplementedError

    @property
    def min_color_temp_kelvin(self) -> int | None:
        raise NotImplementedError

    @property
    def max_color_temp_kelvin(self) -> int | None:
        raise NotImplementedError

    @property
    def supports_music_modes(self) -> bool:
        raise NotImplementedError

    @property
    def setup_in_background(self) -> bool:
        raise NotImplementedError

    @property
    def brightness(self) -> int:
        raise NotImplementedError

    @property
    def current_rgb_color(self) -> tuple[int, int, int] | None:
        raise NotImplementedError

    @property
    def inferred_effect(self) -> str | None:
        raise NotImplementedError

    def cleanup(self) -> None:
        """Override in subclass to release resources."""

    async def async_setup(self) -> None:
        raise NotImplementedError

    async def async_load_effects(self, config_dir: str) -> None:
        raise NotImplementedError

    async def async_turn_on(self) -> None:
        raise NotImplementedError

    async def async_turn_off(self) -> None:
        raise NotImplementedError

    async def async_set_brightness(self, brightness: int) -> None:
        raise NotImplementedError

    async def async_set_rgb_color(self, r: int, g: int, b: int) -> None:
        raise NotImplementedError

    async def async_set_color_temp(self, kelvin: int) -> None:
        raise NotImplementedError

    async def async_apply_effect(self, effect: str) -> None:
        raise NotImplementedError

