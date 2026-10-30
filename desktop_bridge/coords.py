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


# ----------------------------------------------------------------------------- mouse paths between monitors
# A real cursor cannot cross the "dead" areas of the virtual desktop that belong to no monitor (Windows clamps it to the nearest monitor edge).
# So a glide from one monitor to another goes through the edge the monitors share, like a hand would.

def _inside(r: tuple[int, int, int, int], x: float, y: float) -> bool:
    return r[0] <= x < r[0] + r[2] and r[1] <= y < r[1] + r[3]


def rect_index(rects: list[tuple[int, int, int, int]], x: float, y: float) -> int:
    for i, r in enumerate(rects):
        if _inside(r, x, y):
            return i
    best, bi = 1e18, 0  # not on any monitor: nearest rectangle
    for i, r in enumerate(rects):
        dx = max(r[0] - x, 0, x - (r[0] + r[2] - 1))
        dy = max(r[1] - y, 0, y - (r[1] + r[3] - 1))
        if dx * dx + dy * dy < best:
            best, bi = dx * dx + dy * dy, i
    return bi


def shared_edge(ra: tuple[int, int, int, int], rb: tuple[int, int, int, int]):
    """('v', xa, xb, y0, y1) if ra/rb touch along a vertical edge (xa = last pixel column inside ra, xb = first inside rb, y0..y1 inclusive overlap),
    ('h', ya, yb, x0, x1) for a horizontal edge, else None. The overlap must be at least 3 px."""
    ax0, ay0, ax1, ay1 = ra[0], ra[1], ra[0] + ra[2], ra[1] + ra[3]
    bx0, by0, bx1, by1 = rb[0], rb[1], rb[0] + rb[2], rb[1] + rb[3]
    oy0, oy1 = max(ay0, by0), min(ay1, by1)
    ox0, ox1 = max(ax0, bx0), min(ax1, bx1)
    if ax1 == bx0 and oy1 - oy0 >= 3:
        return ("v", ax1 - 1, bx0, oy0, oy1 - 1)
    if bx1 == ax0 and oy1 - oy0 >= 3:
        return ("v", ax0, bx1 - 1, oy0, oy1 - 1)
    if ay1 == by0 and ox1 - ox0 >= 3:
        return ("h", ay1 - 1, by0, ox0, ox1 - 1)
    if by1 == ay0 and ox1 - ox0 >= 3:
        return ("h", ay0, by1 - 1, ox0, ox1 - 1)
    return None


def plan_path(a: tuple[int, int], b: tuple[int, int], rects: list[tuple[int, int, int, int]]) -> list[tuple[int, int]]:
    """Polyline from a to b that stays on monitors. Same monitor (or no route found): straight line."""
    i, j = rect_index(rects, *a), rect_index(rects, *b)
    if i == j:
        return [a, b]
    prev = {i: None}  # BFS over monitors that share an edge
    queue = [i]
    while queue and j not in prev:
        k = queue.pop(0)
        for m in range(len(rects)):
            if m not in prev and shared_edge(rects[k], rects[m]):
                prev[m] = k
                queue.append(m)
    if j not in prev:
        return [a, b]
    route = [j]
    while prev[route[-1]] is not None:
        route.append(prev[route[-1]])
    route.reverse()
    pts = [a]
    cur = a
    for m1, m2 in zip(route, route[1:]):
        e = shared_edge(rects[m1], rects[m2])
        if e[0] == "v":
            _, xa, xb, y0, y1 = e
            yc = int(min(max(round((cur[1] + b[1]) / 2), y0 + 1), y1 - 1))
            pts += [(xa, yc), (xb, yc)]
            cur = (xb, yc)
        else:
            _, ya, yb, x0, x1 = e
            xc = int(min(max(round((cur[0] + b[0]) / 2), x0 + 1), x1 - 1))
            pts += [(xc, ya), (xc, yb)]
            cur = (xc, yb)
    pts.append(b)
    return pts


def point_along(path: list[tuple[int, int]], frac: float) -> tuple[int, int]:
    """Point at fraction 0..1 of the total length of a polyline."""
    segs = [((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5 for (x0, y0), (x1, y1) in zip(path, path[1:])]
    total = sum(segs)
    if total == 0 or frac <= 0:
        return path[0]
    if frac >= 1:
        return path[-1]
    d = frac * total
    for ((x0, y0), (x1, y1)), s in zip(zip(path, path[1:]), segs):
        if d <= s and s > 0:
            t = d / s
            return (round(x0 + (x1 - x0) * t), round(y0 + (y1 - y0) * t))
        d -= s
    return path[-1]


def path_length(path: list[tuple[int, int]]) -> float:
    return sum(((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5 for (x0, y0), (x1, y1) in zip(path, path[1:]))
