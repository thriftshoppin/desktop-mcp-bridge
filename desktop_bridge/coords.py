"""Pure coordinate mapping between screenshot pixels and real (physical) virtual-desktop pixels.

No Windows calls here, so it is fully unit-testable. All real coordinates are physical pixels in the
virtual desktop (the process is per-monitor-DPI-aware), which may be negative.

A screenshot covers a *real region* (rx, ry, rw, rh). The image is downscaled so its long edge is at most
MAX_EDGE, giving `scale` = image pixels per real pixel (<= 1.0). A point (x, y) in the image maps to
real = (rx + x / scale, ry + y / scale).
"""
from __future__ import annotations

from dataclasses import dataclass

MAX_EDGE = 1568


@dataclass(frozen=True)
class Monitor:
    id: int
    name: str
    x: int
    y: int
    w: int
    h: int
    dpi: int = 96
    primary: bool = False
    refresh_hz: int = 0

    @property
    def scale_pct(self) -> int:
        return round(self.dpi / 96 * 100)

    @property
    def rect(self) -> tuple[int, int, int, int]:
        return (self.x, self.y, self.w, self.h)

    def contains(self, px: float, py: float) -> bool:
        return self.x <= px < self.x + self.w and self.y <= py < self.y + self.h


@dataclass(frozen=True)
class ShotContext:
    """What a screenshot covered; used to map later screenshot-space coordinates back to real pixels."""
    monitor: int  # monitor id, or -1 when the region spans/crosses monitors
    rx: int
    ry: int
    rw: int
    rh: int
    scale: float
    iw: int
    ih: int

    @property
    def origin(self) -> tuple[int, int]:
        return (self.rx, self.ry)


def plan_shot(region: tuple[int, int, int, int], monitor: int, max_edge: int = MAX_EDGE,
              upscale: float = 1.0) -> ShotContext:
    rx, ry, rw, rh = region
    if rw <= 0 or rh <= 0:
        raise ValueError("empty region")
    scale = min(1.0, max_edge / max(rw, rh)) if upscale == 1.0 else min(upscale, max_edge / max(rw, rh))
    iw = max(1, round(rw * scale))
    ih = max(1, round(rh * scale))
    return ShotContext(monitor, rx, ry, rw, rh, scale, iw, ih)


def shot_to_screen(ctx: ShotContext, x: float, y: float, clamp: bool = True) -> tuple[int, int]:
    if clamp:
        x = min(max(x, 0), ctx.iw - 1)
        y = min(max(y, 0), ctx.ih - 1)
    # centre of the image pixel -> centre of the covered real-pixel span
    return (round(ctx.rx + (x + 0.5) / ctx.scale - 0.5), round(ctx.ry + (y + 0.5) / ctx.scale - 0.5))


def screen_to_shot(ctx: ShotContext, px: float, py: float) -> tuple[int, int]:
    return (round((px - ctx.rx + 0.5) * ctx.scale - 0.5), round((py - ctx.ry + 0.5) * ctx.scale - 0.5))


def region_shot_to_screen(ctx: ShotContext, region: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """(x, y, w, h) in screenshot pixels -> real pixels, clamped to the shot's real region."""
    x, y, w, h = region
    x0 = ctx.rx + x / ctx.scale
    y0 = ctx.ry + y / ctx.scale
    x1 = ctx.rx + (x + w) / ctx.scale
    y1 = ctx.ry + (y + h) / ctx.scale
    x0 = max(ctx.rx, min(round(x0), ctx.rx + ctx.rw - 1))
    y0 = max(ctx.ry, min(round(y0), ctx.ry + ctx.rh - 1))
    x1 = max(x0 + 1, min(round(x1), ctx.rx + ctx.rw))
    y1 = max(y0 + 1, min(round(y1), ctx.ry + ctx.rh))
    return (x0, y0, x1 - x0, y1 - y0)


def monitor_at(monitors: list[Monitor], px: float, py: float) -> Monitor | None:
    for m in monitors:
        if m.contains(px, py):
            return m
    return None


def intersect(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> tuple[int, int, int, int] | None:
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[0] + a[2], b[0] + b[2]), min(a[1] + a[3], b[1] + b[3])
    if x1 <= x0 or y1 <= y0:
        return None
    return (x0, y0, x1 - x0, y1 - y0)


def to_absolute_65535(px: int, py: int, vx: int, vy: int, vw: int, vh: int) -> tuple[int, int]:
    """Normalise a virtual-desktop pixel to SendInput's 0..65535 MOUSEEVENTF_VIRTUALDESK range (kept for reference/tests;
    the bridge itself uses SetCursorPos with physical pixels, which is exact)."""
    return (round((px - vx) * 65535 / max(vw - 1, 1)), round((py - vy) * 65535 / max(vh - 1, 1)))
