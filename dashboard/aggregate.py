"""Pure CSV -> stats functions for the traintap dashboard.

No web framework here so it can be unit-tested directly. Reads the CSVs that
traintap writes (trains.csv, passes.csv, signal.csv) and produces JSON-able
dicts. Stdlib only (data is small).
"""

from __future__ import annotations

import csv
import os
import statistics
from datetime import datetime

import speed as _speed

RANGES = {"24h": 86_400, "7d": 604_800, "all": None}
SOURCES = ("EOT", "HOT", "DPU")


def load_csv(path: str) -> list[dict]:
    try:
        with open(path, newline="") as f:
            return list(csv.DictReader(f))
    except FileNotFoundError:
        return []


def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _parse_dt(s: str) -> float:
    """Local 'YYYY-MM-DD HH:MM:SS' -> epoch (naive treated as local)."""
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d %H:%M:%S").timestamp()
    except (TypeError, ValueError):
        return 0.0


def _hour_key(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:00")


def _cutoff(now: float, rng: str) -> float:
    secs = RANGES.get(rng, 86_400)
    return 0.0 if secs is None else now - secs


# --- status (real-time, from trains.csv) -------------------------------------

def status(trains: list[dict], now: float, near_window: float = 180.0) -> dict:
    valid = [r for r in trains if r.get("valid") == "1"]
    if not valid:
        return {"train_near": False, "minutes_since": None,
                "last_packet_epoch": None, "last_unit": None,
                "last_source": None, "last_pressure": None, "now": now}
    last = max(valid, key=lambda r: _f(r["epoch"]))
    ep = _f(last["epoch"])
    since = now - ep
    return {
        "train_near": since <= near_window,
        "minutes_since": round(since / 60.0, 1),
        "last_packet_epoch": ep,
        "last_unit": last.get("unit_addr") or None,
        "last_source": last.get("source"),
        "last_pressure": last.get("pressure_psig") or None,
        "now": now,
    }


# --- aggregated stats --------------------------------------------------------

def _hour_buckets(cutoff: float, now: float) -> list[str]:
    """Contiguous hourly bucket keys from cutoff..now (so empty hours show)."""
    if cutoff <= 0:
        cutoff = now - 86_400  # cap 'all' bucket axis to last 24h for readability
    start = int(cutoff // 3600 * 3600)
    end = int(now // 3600 * 3600)
    return [_hour_key(t) for t in range(start, end + 3600, 3600)]


def _rolling_median(points: list[tuple[float, float]], window: int = 9):
    out = []
    vals = [v for _, v in points]
    for i in range(len(points)):
        lo = max(0, i - window // 2)
        hi = min(len(vals), i + window // 2 + 1)
        out.append([points[i][0], round(statistics.median(vals[lo:hi]), 1)])
    return out


def hour_day_heatmap(passes, now: float, rng: str = "7d",
                     max_days: int = 120, min_days: int = 7) -> dict:
    """GitHub-style grid: rows = days, columns = 24 hours, cell = distinct trains
    heard that hour. Each pass counts once, in the local hour of its `start` (the
    first hour it's heard); distinct = unique EOT units within the cell.

    Always spans at least `min_days` days (default 7) regardless of the range
    selector, showing real train data across that whole window."""
    cutoff = _cutoff(now, rng)
    if cutoff <= 0:  # "all": start at the earliest pass we actually have
        starts = [_parse_dt(r.get("start", "")) for r in passes if r.get("start")]
        cutoff = min(starts) if starts else now
    cutoff = min(cutoff, now - min_days * 86_400)   # always show >= min_days days

    start_day = datetime.fromtimestamp(cutoff).date()
    end_day = datetime.fromtimestamp(now).date()
    days = []
    d = start_day
    while d <= end_day:
        days.append(d)
        d = d.fromordinal(d.toordinal() + 1)
    days = days[-max_days:]                       # cap rows for sanity
    day_index = {d.isoformat(): i for i, d in enumerate(days)}

    # accumulate a set of distinct units per (day, hour) cell
    cells = [[set() for _ in range(24)] for _ in days]
    for r in passes:
        e = _parse_dt(r.get("start", ""))
        if e < cutoff:
            continue
        dt = datetime.fromtimestamp(e)
        di = day_index.get(dt.date().isoformat())
        if di is None:
            continue
        units = [u for u in (r.get("eot_units") or "").split("|") if u]
        cells[di][dt.hour].update(units or ["?"])   # count unit-less passes as 1

    grid = [[len(c) for c in row] for row in cells]
    return {"days": [d.isoformat() for d in days], "grid": grid,
            "max": max((v for row in grid for v in row), default=0)}


def stats(trains, passes, signal, now: float, rng: str = "24h",
          track_distance_m: float | None = None) -> dict:
    cutoff = _cutoff(now, rng)

    tr = [r for r in trains if _f(r["epoch"]) >= cutoff and r.get("valid") == "1"]
    pa = [r for r in passes if _parse_dt(r.get("start", "")) >= cutoff]
    sg = [r for r in signal if _f(r["epoch"]) >= cutoff]

    # source counts + decode quality
    source_counts = {s: 0 for s in SOURCES}
    clean = corrected = 0
    pressure_series = []
    for r in tr:
        source_counts[r.get("source", "EOT")] = \
            source_counts.get(r.get("source", "EOT"), 0) + 1
        if _f(r.get("corrected")) > 0:
            corrected += 1
        else:
            clean += 1
        if r.get("source") == "EOT" and r.get("pressure_psig"):
            pressure_series.append([_f(r["epoch"]), int(_f(r["pressure_psig"]))])

    # per-hour buckets
    buckets = _hour_buckets(cutoff, now)
    bset = set(buckets)
    trains_per_hour = {b: 0 for b in buckets}
    packets_per_hour = {b: {s: 0 for s in SOURCES} for b in buckets}
    hod = [0] * 24  # trains per hour-of-day
    for r in pa:
        k = _hour_key(_parse_dt(r["start"]))
        if k in trains_per_hour:
            trains_per_hour[k] += 1
        hod[datetime.fromtimestamp(_parse_dt(r["start"])).hour] += 1
    for r in tr:
        k = _hour_key(_f(r["epoch"]))
        if k in bset:
            packets_per_hour[k][r.get("source", "EOT")] += 1

    # unique units / meets / busiest
    units = set()
    meets = []
    for r in pa:
        us = [u for u in (r.get("eot_units") or "").split("|") if u]
        units.update(us)
        if len(us) > 1 or (r.get("dpu_units") or ""):
            meets.append({"start": r.get("start"), "end": r.get("end"),
                          "eot_units": us, "dpu_units":
                          [u for u in (r.get("dpu_units") or "").split("|") if u]})
    busiest = max(trains_per_hour.items(), key=lambda kv: kv[1], default=("", 0))

    # signal series (+ rolling median) for antenna placement
    sig_points = sorted(([_f(r["epoch"]), _f(r["activity_db"])] for r in sg),
                        key=lambda p: p[0])
    sig_median = _rolling_median([(e, v) for e, v in sig_points])

    # recent trains (from passes): peak dB + Doppler speed from signal samples in window
    def pass_signal(start_s, end_s):
        s0, s1 = _parse_dt(start_s), _parse_dt(end_s) + 90
        return [r for r in sg if s0 - 30 <= _f(r["epoch"]) <= s1]

    recent = []
    for r in sorted(pa, key=lambda r: _parse_dt(r.get("start", "")), reverse=True)[:15]:
        us = [u for u in (r.get("eot_units") or "").split("|") if u]
        dus = [u for u in (r.get("dpu_units") or "").split("|") if u]
        win = pass_signal(r.get("start"), r.get("end"))
        peak_db = round(max((_f(x["activity_db"]) for x in win), default=0), 1) \
            if win else None
        sp = _speed.estimate_speed(
            [(_f(x["epoch"]), x.get("freq_offset_hz", "")) for x in win],
            track_distance_m)
        recent.append({
            "start": r.get("start"), "duration_s": int(_f(r.get("duration_s"))),
            "eot_units": us, "eot_pkts": int(_f(r.get("eot_pkts"))),
            "dpu_units": dus, "peak_db": peak_db,
            "speed_mph": sp.get("speed_mph"), "speed_quality": sp.get("quality"),
            "meet": len(us) > 1 or bool(dus)})

    speeds = [e["speed_mph"] for e in recent if e["speed_mph"] is not None]
    median_speed = round(statistics.median(speeds), 1) if speeds else None

    return {
        "range": rng, "now": now,
        "total_trains": len(pa),
        "unique_units": len(units),
        "source_counts": source_counts,
        "decode_quality": {"clean": clean, "corrected": corrected},
        "busiest_hour": {"hour": busiest[0], "count": busiest[1]},
        "trains_per_hour": [{"hour": b, "count": trains_per_hour[b]} for b in buckets],
        "packets_per_hour": [
            {"hour": b, **packets_per_hour[b]} for b in buckets],
        "hour_of_day": hod,
        "pressure_series": sorted(pressure_series, key=lambda p: p[0]),
        "signal_series": sig_points,
        "signal_median": sig_median,
        "meets": meets,
        "recent_trains": recent,
        "median_speed_mph": median_speed,
        "track_distance_m": track_distance_m,
        "heatmap": hour_day_heatmap(passes, now, rng),
    }


def all_stats(data_dir: str, now: float, rng: str = "24h",
              track_distance_m: float | None = None) -> dict:
    return stats(load_csv(os.path.join(data_dir, "trains.csv")),
                 load_csv(os.path.join(data_dir, "passes.csv")),
                 load_csv(os.path.join(data_dir, "signal.csv")), now, rng,
                 track_distance_m)


def current_status(data_dir: str, now: float) -> dict:
    return status(load_csv(os.path.join(data_dir, "trains.csv")), now)
