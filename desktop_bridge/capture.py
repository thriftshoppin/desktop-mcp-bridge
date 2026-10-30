"""Screen capture: DXGI (dxcam) primary, mss (GDI) fallback. IMPORT ORDER: dxcam/comtypes must load before any audio lib."""
from __future__ import annotations

import io
import time

import dxcam  # noqa: F401  (must be imported before PyAudioWPatch/any COM-initialising audio lib)
import mss
import numpy as np
from PIL import Image

from . import coords
from .coords import Monitor
from .safety import ToolRefused


class Capturer:
    def __init__(self) -> None:
        self._cams: dict[int, object] = {}
        self._cache: dict[int, tuple[float, np.ndarray]] = {}
        self._dx_failed: dict[int, str] = {}

    # --- DXGI
    def _camera(self, mon: Monitor):
        cam = self._cams.get(mon.id)
        if cam is not None:
            return cam
        for dev in range(2):
            for out in range(6):
                try:
                    c = dxcam.create(device_idx=dev, output_idx=out, output_color="BGRA")
                except Exception:
                    continue
                if c is None:
                    continue
                if (c.width, c.height) == (mon.w, mon.h):
                    self._cams[mon.id] = c
                    return c
        raise RuntimeError(f"no DXGI output matches {mon.w}x{mon.h}")

    def _grab_dxgi(self, mon: Monitor) -> tuple[np.ndarray, str]:
        cam = self._camera(mon)
        end = time.perf_counter() + 0.12
        while True:
            f = cam.grab()
            if f is not None:
                f = f[:, :, :3]
                self._cache[mon.id] = (time.time(), f)
                return f, "dxcam"
            if time.perf_counter() > end:
                break
            time.sleep(0.004)
        cached = self._cache.get(mon.id)
        if cached is not None:  # DXGI delivers a frame only when the screen changed, so "no frame" = unchanged = cache
            return cached[1], "dxcam(unchanged-frame)"
        raise RuntimeError("dxcam returned no frame and nothing is cached")

    def _grab_mss(self, region: tuple[int, int, int, int]) -> np.ndarray:
        x, y, w, h = region
        with mss.MSS() as s:
            shot = s.grab({"left": x, "top": y, "width": w, "height": h})
        return np.asarray(Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX"))[..., ::-1]  # -> BGR like dxcam

    def grab(self, region: tuple[int, int, int, int], monitors: list[Monitor], session: dict,
             prefer: str | None = None) -> tuple[np.ndarray, str, str]:
        """Returns (BGR array of the region, engine, note). region is real (physical) pixels."""
        if session.get("locked"):
            raise ToolRefused("SESSION_LOCKED", "The workstation is locked or on a secure desktop "
                              f"(input desktop = {session.get('input_desktop')!r}); capture would be black.")
        if session.get("wts_state") in ("disconnected", "down", "idle") and session.get("type") == "rdp":
            raise ToolRefused("SESSION_DISCONNECTED", "The RDP session is disconnected; nothing is being drawn.")
        note = ""
        engine = "mss"
        arr = None
        owner = [m for m in monitors if coords.intersect(region, m.rect) == region]
        use_dx = prefer != "mss" and session.get("type") != "rdp" and len(owner) == 1
        if session.get("type") == "rdp":
            note = "rdp session: DXGI duplication unavailable, using GDI (mss)"
        elif len(owner) != 1:
            note = "region spans monitors: using GDI (mss)"
        if use_dx:
            m = owner[0]
            try:
                full, engine = self._grab_dxgi(m)
                x0, y0 = region[0] - m.x, region[1] - m.y
                arr = full[y0:y0 + region[3], x0:x0 + region[2]]
            except Exception as e:
                self._cams.pop(m.id, None)
                note = f"dxcam failed ({str(e)[:80]}); fell back to GDI (mss)"
        if arr is None:
            arr = self._grab_mss(region)
            engine = "mss"
        if arr.size and int(arr.max()) <= 2 and arr.shape[0] * arr.shape[1] > 400:
            raise ToolRefused("BLANK_FRAME", f"Capture ({engine}) returned an all-black frame; the display may be asleep, "
                              "locked, protected content, or the session is not interactive.")
        return arr, engine, note

    def close(self) -> None:
        for c in self._cams.values():
            try:
                c.release()
            except Exception:
                pass
        self._cams.clear()


def encode(arr_bgr: np.ndarray, ctx: coords.ShotContext, fmt: str = "jpeg", quality: int = 85,
           redact: list[tuple[int, int, int, int]] | None = None) -> tuple[bytes, str]:
    """BGR region array -> resized image bytes. `redact` rects are real pixels (blacked out before encoding)."""
    arr = np.ascontiguousarray(arr_bgr)
    if redact:
        arr = arr.copy()
        for rx, ry, rw, rh in redact:
            r = coords.intersect((rx, ry, rw, rh), (ctx.rx, ctx.ry, ctx.rw, ctx.rh))
            if r:
                arr[r[1] - ctx.ry:r[1] - ctx.ry + r[3], r[0] - ctx.rx:r[0] - ctx.rx + r[2]] = 0
    img = Image.fromarray(arr[..., ::-1])
    if (img.width, img.height) != (ctx.iw, ctx.ih):
        img = img.resize((ctx.iw, ctx.ih), Image.LANCZOS if ctx.scale < 1 else Image.BICUBIC)
    buf = io.BytesIO()
    if fmt == "png":
        img.save(buf, "PNG", optimize=False)
        return buf.getvalue(), "png"
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue(), "jpeg"
