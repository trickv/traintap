"""Unit tests for the dashboard aggregation logic."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import aggregate  # noqa: E402

NOW = 1_000_000.0


def _train(epoch, source="EOT", valid="1", corrected="0", unit="100", press="88"):
    return {"epoch": str(epoch), "source": source, "valid": valid,
            "corrected": corrected, "unit_addr": unit, "pressure_psig": press}


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
    assert h["days"] == ["2026-06-29", "2026-06-30", "2026-07-01", "2026-07-02"]
    assert all(len(row) == 24 for row in h["grid"])
    assert h["grid"][0][14] == 2 and h["grid"][0][15] == 0   # distinct, start-hour only
    assert h["grid"][1][9] == 2
    assert h["grid"][2] == [0] * 24 and h["grid"][3] == [0] * 24  # empty days shown
    assert h["max"] == 2


def test_stats_signal_series_and_median():
    sig = [{"epoch": str(NOW - i * 10), "activity_db": str(10 + i)}
           for i in range(5)]
    d = aggregate.stats([], [], sig, NOW, "24h")
    assert len(d["signal_series"]) == 5
    assert len(d["signal_median"]) == 5
    # sorted ascending by epoch
    xs = [p[0] for p in d["signal_series"]]
    assert xs == sorted(xs)
