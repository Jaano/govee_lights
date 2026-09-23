"""
Bundled device model quirks.

Only models that need non-default BLE handling are listed in
``_DEVICE_FEATURES`` below; a model absent from it is assumed to be a plain
device (solid-colour mode 0x02, brightness as a raw 0-255 byte, no segments,
no passive advert state) - which is the common case, so the dict holds
exceptions, not a whitelist of supported models. Nothing else in the
codebase should hard-code model IDs or per-model behaviour; import these
helpers instead so this stays the one source of truth.

Per-model flags:

- ``segmented``: device has individually addressable LED segments and needs
  segment-aware color commands.
- ``segments``: number of individually addressable segments (only meaningful
  for segmented models). Defaults to :data:`DEFAULT_SEGMENT_COUNT`.
- ``brightness_percent``: device expects brightness as a percentage (0-100)
  instead of a raw byte (0-255).
- ``colour_d``: device uses BLE mode byte ``0x0D`` instead of ``0x02`` for
  colour/color-temperature packets.
- ``advert_state``: device broadcasts on/off state in its BLE advertisement
  (as ``ec 00 0a 01 <state>``), so power state can be tracked passively
  without an open GATT connection.
"""

from __future__ import annotations

import re

# Fallback used when a segmented model does not declare a segment count. The
# segment bitmask in the protocol covers 15 segments.
DEFAULT_SEGMENT_COUNT = 15

# Largest addressable segment count supported by the mask protocol.
MAX_SEGMENT_COUNT = 15

# Govee model IDs look like ``H6006``, ``H617C`` or ``H61A0`` and appear
# inside BLE advertisement names (e.g. ``Govee_H617C_2482``).
_MODEL_PATTERN = re.compile(r"[Hh]\d{2,4}[A-Za-z]?\d?")

_DEVICE_FEATURES: dict[str, dict[str, bool | int]] = {
    "H6005": {"colour_d": True},
    "H6052": {"colour_d": True},
    "H6053": {"segmented": True, "segments": 12},
    "H6058": {"colour_d": True},
    "H6072": {"segmented": True},
    "H6102": {"segmented": True, "colour_d": True},
    "H613A": {"brightness_percent": True},
    "H613B": {"colour_d": True},
    "H613D": {"colour_d": True},
    "H617A": {"segmented": True, "brightness_percent": True, "advert_state": True},
    "H617C": {"segmented": True, "brightness_percent": True, "advert_state": True},
    "H617E": {"segmented": True, "brightness_percent": True, "colour_d": True},
    "H618C": {"segmented": True, "brightness_percent": True},
    "H6199": {"segmented": True, "brightness_percent": True},
}


def detect_model(device_name: str) -> str | None:
    """Return the model ID embedded in an advertisement name, if any.

    Govee names typically look like ``Govee_H617C_2482``; the first token
    shaped like a model ID is returned, uppercased. This is a best-effort
    guess for pre-filling the config flow - it does not check the result
    against :data:`_DEVICE_FEATURES`, since an unlisted model works fine
    with the default (no-quirks) behaviour above. Returns None when no such
    token appears at all.
    """
    if not device_name:
        return None
    match = _MODEL_PATTERN.search(device_name)
    return match.group(0).upper() if match else None


def _model_features(model: str) -> dict[str, bool | int]:
    """Return *model*'s raw feature entry from :data:`_DEVICE_FEATURES`
    (never raises; ``{}`` for an unlisted model)."""
    return _DEVICE_FEATURES.get(model, {})


def is_segmented_model(model: str) -> bool:
    """Return True when *model* uses individually addressable segments."""
    return bool(_model_features(model).get("segmented", False))


def uses_percent_brightness(model: str) -> bool:
    """Return True when *model* expects brightness as a percentage."""
    return bool(_model_features(model).get("brightness_percent", False))


def uses_colour_d_mode(model: str) -> bool:
    """Return True when *model* uses mode byte 0x0D instead of 0x02."""
    return bool(_model_features(model).get("colour_d", False))


def has_advert_state(model: str) -> bool:
    """Return True when *model* broadcasts on/off state in its BLE advert."""
    return bool(_model_features(model).get("advert_state", False))


def get_segment_count(model: str) -> int:
    """Return the number of addressable segments for *model*.

    Defaults to :data:`DEFAULT_SEGMENT_COUNT` when the model does not declare
    one and is capped at :data:`MAX_SEGMENT_COUNT`, the protocol limit.
    """
    return min(
        int(_model_features(model).get("segments", DEFAULT_SEGMENT_COUNT)),
        MAX_SEGMENT_COUNT,
    )
