"""
HA diagnostics support: the "Download diagnostics" button per config entry.

HA discovers this module by name; no manifest or setup wiring needed.
Adapted from Laserology/govee_ble_lights (diagnostics.py, 42ddfd1), extended
with what our coordinators track that theirs don't: transport kind,
availability, connection/idle-disconnect state, effect count, the repair
issue key, and - for LAN - the bound device's ``lastseen`` timestamp.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import async_redact_data

from .govee_ble import GoveeBLECoordinator
from .govee_lan import GoveeLANCoordinator
from .models import get_segment_count, is_segmented_model

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

# BLE address / LAN IP identify physical hardware and its network location;
# diagnostics downloads are often pasted into public issue reports.
# repair_issue_key embeds the BLE address or the LAN IP.
TO_REDACT = {"address", "ip", "device_id", "repair_issue_key"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for one config entry."""
    hub = entry.runtime_data
    if hub is None:
        return {"error": "integration not set up"}

    data: dict[str, Any] = {
        "device_key": hub.device_key,
        "available": hub.available,
        "is_on": hub.is_on,
        "brightness_raw": hub.brightness_raw,
        "rgb_color": hub.rgb_color,
        "color_temp_kelvin": hub.color_temp_kelvin,
        "effect_count": len(hub.effect_list or []),
    }

    if isinstance(hub, GoveeBLECoordinator):
        data["transport"] = "BLE"
        data["address"] = hub.address
        data["model"] = {
            "id": hub.model,
            "segmented": is_segmented_model(hub.model),
            "segments": get_segment_count(hub.model) if is_segmented_model(hub.model) else None,
        }
        data["connected"] = hub.is_connected
        data["idle_disconnect_scheduled"] = hub._cancel_disconnect is not None
        data["advertisement_watcher_active"] = hub._unsub_advertisement is not None
        data["mode"] = hub.mode
        data["music_mode_id"] = hub.music_mode_id
        data["repair_issue_key"] = hub._issue_key
    elif isinstance(hub, GoveeLANCoordinator):
        data["transport"] = "LAN"
        data["ip"] = hub.ip
        data["sku"] = hub.sku
        device = hub.device
        data["bound"] = device is not None
        data["device_id"] = device.fingerprint if device is not None else None
        data["lastseen"] = device.lastseen.isoformat() if device is not None else None
        data["repair_issue_key"] = hub._issue_key

    return async_redact_data(data, TO_REDACT)
