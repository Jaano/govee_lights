"""
Software crossfade helper.

Govee firmware has no native fade for either RGB colour or colour
temperature, so a smooth transition is done here by sending interpolated
RGB frames at a wall-clock-paced interval. Adapted from
Laserology/govee_ble_lights' ``effects.py::run_fade`` (commits `9a0b66f`,
`174c1fd`), generalised from their per-segment ``list[list[int]]`` state to
a single ``(r, g, b)`` tuple, since this integration does not yet have
per-segment control (see UPGRADE_PLAN.md item 3.2).
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

# Maximum spacing between interpolated frames. Matches the BLE inter-frame
# delay used elsewhere for scene/multi-packet writes; independent of it,
# since fades also run over LAN.
FADE_INTERVAL: float = 0.05

RGBColor = tuple[int, int, int]


def interpolate_rgb(start: RGBColor, target: RGBColor, fraction: float) -> RGBColor:
    """Linearly interpolate two RGB colours, channel-wise.

    Args:
        start: Starting colour.
        target: Target colour.
        fraction: Position between them, clamped to [0.0, 1.0].
    """
    fraction = max(0.0, min(1.0, fraction))
    r, g, b = (round(a + (b - a) * fraction) for a, b in zip(start, target, strict=True))
    return (r, g, b)


async def run_fade(
    start: RGBColor,
    target: RGBColor,
    fade: float,
    interval: float,
    send_frame: Callable[[RGBColor], Awaitable[None]],
    is_current: Callable[[], bool],
) -> None:
    """Crossfade *start* to *target* over *fade* seconds, wall-clock paced.

    The exact target is the final frame of the window: one frame's measured
    write time is reserved so it lands by ``fade``, not ``fade`` + write
    latency (which would otherwise stretch the fade out). Deadline pacing
    keeps transport write latency from accumulating over the fade.

    Args:
        start: Starting colour.
        target: Target colour.
        fade: Total fade duration in seconds (<= 0 sends *target* once).
        interval: Maximum spacing between frames.
        send_frame: Async callback painting one interpolated (or the final,
            exact) colour. Called with *target* itself as its last frame.
        is_current: Callable returning False once superseded; the fade then
            aborts without writing further frames, including the final one.
    """
    if fade <= 0:
        await send_frame(target)
        return

    begin = time.monotonic()
    last_write = 0.0
    last_frame = start
    while is_current():
        elapsed = time.monotonic() - begin
        # Reserve one frame's write time so the exact target lands by `fade`.
        if elapsed + last_write >= fade:
            await send_frame(target)
            break
        fraction = elapsed / fade
        frame = interpolate_rgb(start, target, fraction)
        # Skip frames that change nothing - including the start colour,
        # already on the strip. An intermediate frame that rounds to the
        # target is skipped too: only the final call may carry the target,
        # which callers treat as the exact, verified command.
        if frame not in (last_frame, target):
            write_start = time.monotonic()
            await send_frame(frame)
            last_write = time.monotonic() - write_start
            last_frame = frame
        await asyncio.sleep(min(interval, max(0.0, fade - (time.monotonic() - begin))))
