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

# Confidence gates
_MIN_N = 4                  # need a few bursts
_MIN_SWING_HZ = 12.0        # swing must clear the ~4 Hz per-sample noise


def _fit_scurve(ts, fs, d):
    """Grid-search (v, t0) for Delta_f(t)=c0 + (f*v/c)*x/sqrt(d^2+x^2),
    x=v*(t-t0); c0 solved analytically per candidate. Returns v (m/s) or None."""
    t_lo, t_hi = min(ts), max(ts)
    n = len(ts)
    best = (float("inf"), None)
    # v: 1..50 m/s (~2..112 mph) in 0.5 steps; t0 over the sample span, 1 s steps
    v = 1.0
    while v <= 50.0:
        A = F_HZ * v / C
        steps = int(t_hi - t_lo) + 1
        for k in range(steps + 1):
            t0 = t_lo + k
            model = [A * (v * (t - t0)) / math.sqrt(d * d + (v * (t - t0)) ** 2)
                     for t in ts]
            c0 = sum(fs[i] - model[i] for i in range(n)) / n
            sse = sum((fs[i] - model[i] - c0) ** 2 for i in range(n))
            if sse < best[0]:
                best = (sse, v)
        v += 0.5
    return best[1]


def estimate_speed(samples, track_distance_m: float | None = None) -> dict:
    """`samples` = iterable of (epoch, freq_offset_hz, [activity_db]). Returns
    {speed_mph, speed_ms, speed_kmh, quality, method, n, swing_hz}; speed_mph is
    None when there aren't enough samples or the swing is within the noise."""
    pts = []
    for s in samples:
        e, fo = s[0], s[1]
        if fo in (None, ""):
            continue
        try:
            pts.append((float(e), float(fo)))
        except (TypeError, ValueError):
            continue
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

    return {"speed_ms": round(v, 2), "speed_mph": round(v * MS_TO_MPH, 1),
            "speed_kmh": round(v * MS_TO_KMH, 1),
            "quality": "good" if method == "fit" else "fair",
            "method": method, "n": n, "swing_hz": round(swing, 1)}
