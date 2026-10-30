"""Explicit, short WASAPI loopback level probe. Never starts implicitly; the caller logs every call."""
from __future__ import annotations

import time

import numpy as np


def audio_level(probe_ms: int = 150, device: str | None = None) -> dict:
    """Open every WASAPI loopback device at once for `probe_ms`, report which one carries signal.

    The Windows default output is NOT always the one in use (here it is a silent virtual 7.1 device while the headset
    plays), so all render devices are measured and the loudest is picked. `device` (substring) overrides the choice.
    """
    import pyaudiowpatch as pa  # imported late: capture.py (dxcam/comtypes) must already be loaded

    p = pa.PyAudio()
    try:
        try:
            default_name = p.get_default_wasapi_loopback()["name"]
        except Exception:
            default_name = None
        devs = list(p.get_loopback_device_info_generator())
        acc: dict[int, list] = {d["index"]: [0.0, 0.0, 0] for d in devs}  # peak, sum of squares, samples
        streams = []
        errors: dict[str, str] = {}

        def make_cb(idx):
            def cb(data, frames, info, status):
                a = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
                if a.size:
                    t = acc[idx]
                    t[0] = max(t[0], float(np.abs(a).max()))
                    t[1] += float((a * a).sum())
                    t[2] += a.size
                return (None, pa.paContinue)
            return cb

        for d in devs:
            try:
                s = p.open(format=pa.paInt16, channels=int(d["maxInputChannels"]), rate=int(d["defaultSampleRate"]),
                           input=True, input_device_index=d["index"], frames_per_buffer=480, stream_callback=make_cb(d["index"]))
                s.start_stream()
                streams.append(s)
            except Exception as e:
                errors[d["name"]] = str(e)[:80]
        time.sleep(max(probe_ms, 60) / 1000)
        for s in streams:
            try:
                s.stop_stream()
                s.close()
            except Exception:
                pass
        rows = []
        for d in devs:
            peak, sq, n = acc[d["index"]]
            rows.append({"device": d["name"].replace(" [Loopback]", ""), "peak": round(peak, 4),
                         "rms": round((sq / n) ** 0.5, 5) if n else 0.0, "default": d["name"] == default_name})
        chosen = None
        if device:
            chosen = next((r for r in rows if device.lower() in r["device"].lower()), None)
        if chosen is None:
            chosen = max(rows, key=lambda r: (r["peak"], r["default"])) if rows else None
        playing = bool(chosen and chosen["peak"] > 0.001)
        out = {"device": chosen["device"] if chosen else None, "peak": chosen["peak"] if chosen else 0.0,
               "rms": chosen["rms"] if chosen else 0.0, "playing": playing, "probe_ms": probe_ms,
               "default_device": (default_name or "").replace(" [Loopback]", "") or None,
               "devices": sorted(rows, key=lambda r: -r["peak"])}
        if not playing and chosen:
            out["note"] = "nothing is playing on any output device; 'device' is the Windows default"
            out["device"] = out["default_device"] or out["device"]
        if errors:
            out["errors"] = errors
        return out
    finally:
        p.terminate()
