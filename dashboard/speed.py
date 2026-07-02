"""Estimate train speed from per-burst EOT carrier-frequency (Doppler) samples.

Pure stdlib (the dashboard image has no numpy/scipy). As the rear EOT passes, its
carrier is Doppler-shifted +f*v/c approaching -> 0 at closest approach -> -f*v/c
receding. Given the samples for one pass (and, ideally, the perpendicular
distance `d` from the receiver to the track), recover the speed `v`.
"""

from __future__ import annotations

import math

F_HZ = 457_937_500          # EOT carrier
C = 299_792_458             # m/s
MS_TO_MPH = 2.236_936
MS_TO_KMH = 3.6

# Confidence gates / robustness
_MIN_N = 4                  # need a few bursts
_MIN_SWING_HZ = 12.0        # swing must clear the ~4 Hz per-sample noise
_MIN_DB = 6.0               # ignore weak bursts (bad carrier estimates)
_OUTLIER_HZ = 200.0         # drop freq samples this far from the median (junk)
_MAX_MS = 36.0             # ~80 mph reject cap: freight won't exceed this here
_FIT_MAX_MS = 55.0         # grid goes higher so a "railed" fit lands > _MAX_MS -> rejected


def _median(xs):
    s = sorted(xs)
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2.0


def _fit_scurve(ts, fs, d):
    """Grid-search (v, t0) for Delta_f(t)=c0 + (f*v/c)*x/sqrt(d^2+x^2),
    x=v*(t-t0); c0 solved analytically per candidate. Returns v (m/s) or None.
    Coarse, bounded grid so it stays cheap across many passes."""
    t_lo, t_hi = min(ts), max(ts)
    n = len(ts)
    span = t_hi - t_lo
    t_step = max(1.0, span / 40.0)           # <=~40 t0 steps regardless of pass length
    best = (float("inf"), None)
    v = 1.0
    while v <= _FIT_MAX_MS:                    # grid past the reject cap to catch railers
        A = F_HZ * v / C
        t0 = t_lo
        while t0 <= t_hi:
            model = [A * (v * (t - t0)) / math.sqrt(d * d + (v * (t - t0)) ** 2)
                     for t in ts]
            c0 = sum(fs[i] - model[i] for i in range(n)) / n
            sse = sum((fs[i] - model[i] - c0) ** 2 for i in range(n))
            if sse < best[0]:
                best = (sse, v)
            t0 += t_step
        v += 1.0
    return best[1]


def estimate_speed(samples, track_distance_m: float | None = None) -> dict:
    """`samples` = iterable of (epoch, freq_offset_hz, [activity_db]). Returns
    {speed_mph, speed_ms, speed_kmh, quality, method, n, swing_hz}; speed_mph is
    None when there aren't enough good samples, the swing is within the noise, or
    the result is unphysical."""
    pts = []
    for s in samples:
        e, fo = s[0], s[1]
        db = s[2] if len(s) > 2 else None
        if fo in (None, ""):
            continue
        try:
            if db not in (None, "") and float(db) < _MIN_DB:  # weak burst -> skip
                continue
            pts.append((float(e), float(fo)))
        except (TypeError, ValueError):
            continue
    # drop carrier-estimate outliers (e.g. -563 Hz on a marginal burst)
    if pts:
        med = _median([p[1] for p in pts])
        pts = [p for p in pts if abs(p[1] - med) <= _OUTLIER_HZ]
    pts.sort()
    n = len(pts)
    if n < 2:
        return {"speed_mph": None, "quality": "none", "n": n, "swing_hz": 0.0}

    ts = [p[0] for p in pts]
    fs = [p[1] for p in pts]
    swing = max(fs) - min(fs)
    if n < _MIN_N or swing < _MIN_SWING_HZ:
        return {"speed_mph": None, "quality": "low", "n": n,
                "swing_hz": round(swing, 1)}

    v = C * (swing / 2.0) / F_HZ          # swing estimate (min if asymptotes hit)
    method = "swing"
    if track_distance_m and track_distance_m > 0 and n >= 5:
        vf = _fit_scurve(ts, fs, track_distance_m)
        if vf:
            v, method = vf, "fit"

    if v > _MAX_MS:                        # unphysical -> treat as noise
        return {"speed_mph": None, "quality": "low", "n": n,
                "swing_hz": round(swing, 1)}

    return {"speed_ms": round(v, 2), "speed_mph": round(v * MS_TO_MPH, 1),
            "speed_kmh": round(v * MS_TO_KMH, 1),
            "quality": "good" if method == "fit" else "fair",
            "method": method, "n": n, "swing_hz": round(swing, 1)}
