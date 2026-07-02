"""
Govee 'Connected' binary sensor.

Reports whether the underlying BLE/LAN transport currently considers the
device reachable. Unlike the light entity, this sensor is always available
itself (it just reads on/off) so it stays useful even while the device is
powered off or out of range.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import GoveeCoordinator


async def async_setup_entry(
    hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    hub: GoveeCoordinator = config_entry.runtime_data
    async_add_entities([GoveeConnectedSensor(hub)])


class GoveeConnectedSensor(BinarySensorEntity):
    """Connectivity sensor mirroring GoveeCoordinator.available."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_available = True
    _attr_name = "Connected"

    def __init__(self, hub: GoveeCoordinator) -> None:
        self._coordinator = hub
        self._attr_unique_id = f"{hub.unique_device_id}_connected"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, hub.unique_device_id)},
        )
        self._attr_is_on = hub.available

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            self._coordinator.async_add_listener(self._handle_coordinator_update)
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        self._attr_is_on = self._coordinator.available
        self.async_write_ha_state()
