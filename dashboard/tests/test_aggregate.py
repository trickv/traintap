"""Unit tests for the dashboard aggregation logic."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import aggregate  # noqa: E402

NOW = 1_000_000.0


def _train(epoch, source="EOT", valid="1", corrected="0", unit="100", press="88",
           motion="1"):
    return {"epoch": str(epoch), "source": source, "valid": valid,
            "corrected": corrected, "unit_addr": unit, "pressure_psig": press,
            "motion": motion, "timestamp": "2001-09-09 01:46:40"}


def test_parked_unit_excluded_from_trains():
    # unit 999 parked (8 packets, all stopped); unit 111 a real moving train
    trains = ([_train(NOW - 200 + i, unit="999", motion="0") for i in range(8)]
              + [_train(NOW - 100 + i, unit="111", motion="1") for i in range(3)])
    passes = [
        {"start": "2001-09-09 01:40:00", "end": "2001-09-09 02:00:00",
         "duration_s": "1200", "eot_units": "999", "eot_pkts": "8"},   # parked-only
        {"start": "2001-09-09 01:50:00", "end": "2001-09-09 01:50:30",
         "duration_s": "30", "eot_units": "111", "eot_pkts": "3"},     # real train
        {"start": "2001-09-09 01:51:00", "end": "2001-09-09 01:51:30",
         "duration_s": "30", "eot_units": "999|111", "eot_pkts": "5"},  # not a meet
    ]
    d = aggregate.stats(trains, passes, [], NOW + 100, "all")
    assert d["total_trains"] == 2                     # parked-only pass excluded
    assert d["unique_units"] == 1                     # only 111
    assert d["meets"] == []                           # 999|111 is not a real meet
    assert [p["unit"] for p in d["parked_units"]] == ["999"]
    assert d["parked_units"][0]["packets"] == 8


def test_status_live_speed():
    import speed as _sp
    # a moving train right now, with a Doppler swing across its signal samples
    trains = [_train(NOW - 40 + i * 10, unit="111", motion="1") for i in range(5)]
    A = _sp.F_HZ * 26.0 / _sp.C           # ~26 m/s asymptote
    sig = [{"epoch": str(NOW - 40 + i * 10), "activity_db": "15",
            "freq_offset_hz": str(-100 + A * [-1, -0.7, 0, 0.7, 1][i])}
           for i in range(5)]
    s = aggregate.status(trains, NOW + 1, signal=sig, track_distance_m=173.0)
    assert s["train_near"] is True
    assert s["speed_mph"] is not None and 5 < s["speed_mph"] < 80


def test_parked_units_have_duration_and_ago():
    trains = [_train(NOW - 600 + i * 60, unit="999", motion="0") for i in range(8)]
    d = aggregate.stats(trains, [], [], NOW + 10, "all")
    p = d["parked_units"][0]
    assert p["duration_s"] == 420 and p["last_ago_s"] >= 10   # 7 gaps * 60s


def test_status_ignores_parked_for_train_near():
    # only a parked unit heard recently -> not TRAIN NEAR, but parked_near flagged
    trains = [_train(NOW - 30 + i, unit="999", motion="0") for i in range(8)]
    s = aggregate.status(trains, NOW)
    assert s["train_near"] is False
    assert s["parked_near"] is True and s["parked_unit"] == "999"


def test_status_train_near():
    rows = [_train(NOW - 30), _train(NOW - 5000)]
    s = aggregate.status(rows, NOW, near_window=180)
    assert s["train_near"] is True
    assert s["minutes_since"] == 0.5
    assert s["last_unit"] == "100"


def test_status_not_near_and_empty():
    assert aggregate.status([_train(NOW - 600)], NOW)["train_near"] is False
    empty = aggregate.status([], NOW)
    assert empty["train_near"] is False and empty["minutes_since"] is None


def test_status_ignores_invalid():
    s = aggregate.status([_train(NOW - 10, valid="0")], NOW)
    assert s["minutes_since"] is None   # no valid packets


def test_stats_source_and_quality():
    trains = [_train(NOW - 100), _train(NOW - 100, source="DPU"),
              _train(NOW - 100, corrected="2")]
    d = aggregate.stats(trains, [], [], NOW, "24h")
    assert d["source_counts"]["EOT"] == 2 and d["source_counts"]["DPU"] == 1
    assert d["decode_quality"] == {"clean": 2, "corrected": 1}


def test_stats_trains_per_hour_and_meets():
    # one ordinary pass + one meet (two EOT units)
    passes = [
        {"start": "2001-09-09 01:46:40", "end": "2001-09-09 01:47:20",
         "duration_s": "40", "eot_units": "100", "eot_pkts": "3",
         "dpu_units": "", "hot_units": ""},
        {"start": "2001-09-09 01:50:00", "end": "2001-09-09 01:50:30",
         "duration_s": "30", "eot_units": "200|300", "eot_pkts": "2",
         "dpu_units": "", "hot_units": ""},
    ]
    d = aggregate.stats([], passes, [], 1_000_200.0, "all")
    assert d["total_trains"] == 2
    assert d["unique_units"] == 3          # 100, 200, 300
    assert len(d["meets"]) == 1            # the 200|300 pass
    assert d["recent_trains"][0]["meet"] is True  # newest first


def test_hour_day_heatmap():
    from datetime import datetime
    now = datetime(2026, 7, 2, 12, 0, 0).timestamp()
    passes = [
        # two distinct trains in 06-29 hour 14 (one starts at :55 -> still hour 14)
        {"start": "2026-06-29 14:55:00", "eot_units": "100"},
        {"start": "2026-06-29 14:05:00", "eot_units": "200"},
        {"start": "2026-06-29 14:30:00", "eot_units": "100"},   # dup unit -> not +1
        # two distinct trains in 06-30 hour 9
        {"start": "2026-06-30 09:10:00", "eot_units": "300|400"},
        {"start": "2026-06-30 09:50:00", "eot_units": "300"},    # dup -> not +1
    ]
    h = aggregate.hour_day_heatmap(passes, now, "all")
    idx = {d: i for i, d in enumerate(h["days"])}
    assert len(h["days"]) >= 7                                # always >= 7 days
    assert all(len(row) == 24 for row in h["grid"])
    assert h["grid"][idx["2026-06-29"]][14] == 2             # distinct, start-hour only
    assert h["grid"][idx["2026-06-29"]][15] == 0
    assert h["grid"][idx["2026-06-30"]][9] == 2
    assert h["grid"][idx["2026-06-28"]] == [0] * 24          # empty day shown
    assert h["max"] == 2


def test_heatmap_shows_min_7_days_even_on_24h_range():
    from datetime import datetime
    now = datetime(2026, 7, 2, 12, 0, 0).timestamp()
    passes = [{"start": "2026-07-02 08:00:00", "eot_units": "500"}]  # only today
    h = aggregate.hour_day_heatmap(passes, now, "24h")
    assert len(h["days"]) >= 7                                # forced minimum
    assert h["grid"][h["days"].index("2026-07-02")][8] == 1


def test_stats_signal_series_and_median():
    sig = [{"epoch": str(NOW - i * 10), "activity_db": str(10 + i)}
           for i in range(5)]
    d = aggregate.stats([], [], sig, NOW, "24h")
    assert len(d["signal_series"]) == 5
    assert len(d["signal_median"]) == 5
    # sorted ascending by epoch
    xs = [p[0] for p in d["signal_series"]]
    assert xs == sorted(xs)
