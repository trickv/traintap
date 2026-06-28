#!/usr/bin/env python3
"""Backfill signal.csv from historical monitor.log carrier lines.

The live monitor prints lines like:
    ** carrier on EOT (+9.4 dB) -> saved captures/active_EOT_1782260946.npz
which carry both the signal strength and (via the capture filename) the epoch.
This reconstructs a signal-strength history so the dashboard's antenna-placement
graph has data from before --signal-csv existed.

Usage: python scripts/backfill_signal.py [monitor.log] [signal.csv]
Merges with any existing signal.csv (dedup by epoch), sorted by time.
"""
import csv
import re
import sys
import time

LINE = re.compile(
    r"carrier on (\w+) \(([-+]?[0-9.]+) dB\) -> saved \S*active_\w+_(\d+)\.npz")
COLUMNS = ["epoch", "timestamp", "freq_hz", "source", "activity_db", "n_valid"]
FREQ = {"EOT": 457937500, "HOT": 452937500, "DPU": 457925000}


def main():
    log_path = sys.argv[1] if len(sys.argv) > 1 else "monitor.log"
    out_path = sys.argv[2] if len(sys.argv) > 2 else "data/signal.csv"

    rows = {}
    # keep existing rows (so re-running is idempotent / additive)
    try:
        with open(out_path, newline="") as f:
            for r in csv.DictReader(f):
                rows[r["epoch"]] = r
    except FileNotFoundError:
        pass

    added = 0
    with open(log_path) as f:
        for line in f:
            m = LINE.search(line)
            if not m:
                continue
            source, db, epoch = m.group(1), m.group(2), m.group(3)
            ep = f"{float(epoch):.3f}"
            if ep in rows:
                continue
            rows[ep] = {
                "epoch": ep,
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S",
                                           time.localtime(float(epoch))),
                "freq_hz": FREQ.get(source, ""), "source": source,
                "activity_db": db, "n_valid": ""}
            added += 1

    ordered = sorted(rows.values(), key=lambda r: float(r["epoch"]))
    with open(out_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(ordered)
    print(f"wrote {len(ordered)} rows to {out_path} ({added} new)")


if __name__ == "__main__":
    main()
