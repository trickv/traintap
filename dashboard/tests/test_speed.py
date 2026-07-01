"""Speed estimator tests: synthetic Doppler S-curves -> recovered speed."""
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import speed  # noqa: E402


def _scurve(v, d, t0, times, c0=100.0):
    """Doppler samples (epoch, freq_offset) for a pass at speed v, distance d."""
    A = speed.F_HZ * v / speed.C
    out = []
    for t in times:
        x = v * (t - t0)
        out.append((t, c0 + A * x / math.sqrt(d * d + x * x)))
    return out


def test_fit_recovers_speed_with_distance():
    # 20 m/s (~45 mph), receiver 120 m from track, well-sampled across the pass
    times = [i * 4.0 for i in range(21)]      # 0..80 s, every 4 s
    samples = _scurve(20.0, 120.0, 40.0, times, c0=-130.0)
    r = speed.estimate_speed(samples, track_distance_m=120.0)
    assert r["method"] == "fit"
    assert abs(r["speed_ms"] - 20.0) < 2.5    # within ~2.5 m/s
    assert r["quality"] == "good"


def test_swing_fallback_without_distance():
    times = [i * 4.0 for i in range(21)]
    samples = _scurve(15.0, 80.0, 40.0, times)   # asymptotes reached (x>>d)
    r = speed.estimate_speed(samples, track_distance_m=None)
    assert r["method"] == "swing"
    assert abs(r["speed_ms"] - 15.0) < 3.0       # swing ~= v when asymptotes hit


def test_low_confidence_returns_none():
    # too few samples
    assert speed.estimate_speed([(0, 10.0), (4, -10.0)])["speed_mph"] is None
    # enough samples but swing within noise (flat -> stationary/parked)
    flat = [(i * 4.0, -130.0 + (0.5 if i % 2 else -0.5)) for i in range(10)]
    r = speed.estimate_speed(flat, track_distance_m=120.0)
    assert r["speed_mph"] is None and r["quality"] == "low"


def test_faster_train_reads_faster():
    times = [i * 3.0 for i in range(25)]
    slow = speed.estimate_speed(_scurve(10.0, 100.0, 36.0, times), 100.0)
    fast = speed.estimate_speed(_scurve(30.0, 100.0, 36.0, times), 100.0)
    assert fast["speed_ms"] > slow["speed_ms"]
