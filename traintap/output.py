"""Output sinks for decoded packets: console, CSV, and live stats/dedupe."""

from __future__ import annotations

import csv
import os
import sys
import time
from dataclasses import dataclass, field

from .frame import Packet

CSV_COLUMNS = [
    "timestamp", "epoch", "source", "freq_hz", "valid", "unit_addr",
    "pressure_psig", "batt_charge_pct", "batt_cond", "message_type",
    "arm_status", "motion", "marker_light", "turbine", "valve_ckt",
    "data_block", "checkbits_rx",
]


def _row(pkt: Packet, epoch: float) -> dict:
    return {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))
        + f".{int((epoch % 1) * 1000):03d}",
        "epoch": f"{epoch:.3f}",
        "source": pkt.source,
        "freq_hz": pkt.freq_hz,
        "valid": int(pkt.valid),
        "unit_addr": "" if pkt.unit_addr is None else pkt.unit_addr,
        "pressure_psig": "" if pkt.pressure is None else pkt.pressure,
        "batt_charge_pct": "" if pkt.batt_charge_pct is None else pkt.batt_charge_pct,
        "batt_cond": pkt.batt_cond or "",
        "message_type": pkt.message_type or "",
        "arm_status": pkt.arm_status or "",
        "motion": "" if pkt.motion is None else pkt.motion,
        "marker_light": "" if pkt.marker_light is None else pkt.marker_light,
        "turbine": "" if pkt.turbine is None else pkt.turbine,
        "valve_ckt": "" if pkt.valve_ckt is None else pkt.valve_ckt,
        "data_block": pkt.data_block,
        "checkbits_rx": pkt.checkbits_rx,
    }


def format_console(pkt: Packet, epoch: float) -> str:
    ts = time.strftime("%H:%M:%S", time.localtime(epoch))
    mhz = pkt.freq_hz / 1e6
    if pkt.source == "EOT":
        flags = []
        if pkt.motion:
            flags.append("MOVING")
        if pkt.marker_light:
            flags.append("light")
        if pkt.turbine:
            flags.append("turbine")
        if pkt.arm_status and pkt.arm_status != "Normal":
            flags.append(pkt.arm_status.upper())
        tail = " ".join(flags)
        return (f"{ts} EOT {mhz:.4f}  unit {pkt.unit_addr:<7} "
                f"{pkt.pressure:>3} psig  batt {pkt.batt_cond}/{pkt.batt_charge_pct}%"
                + (f"  [{tail}]" if tail else ""))
    return f"{ts} HOT {mhz:.4f}  msg-type {pkt.message_type}  (data {pkt.data_block})"


@dataclass
class _Stat:
    count: int = 0
    last_epoch: float = 0.0
    last_console_epoch: float = 0.0


@dataclass
class Reporter:
    """Routes packets to console + CSV with per-unit dedupe and live stats.

    Every valid packet is always written to CSV; console output for a given
    unit_addr is throttled to once per `dedupe` seconds to suppress the steady
    EOT repeat-spam while a train sits nearby. `--dedupe 0` disables throttling.
    """

    csv_path: str | None = None
    dedupe: float = 30.0
    stats_interval: float = 60.0
    console: bool = True
    keep_invalid: bool = False

    _csv_file: object = field(default=None, init=False)
    _csv_writer: object = field(default=None, init=False)
    _stats: dict = field(default_factory=dict, init=False)
    _total: int = field(default=0, init=False)
    _invalid: int = field(default=0, init=False)
    _last_summary: float = field(default=0.0, init=False)

    def __post_init__(self):
        if self.csv_path:
            new = not os.path.exists(self.csv_path) or os.path.getsize(self.csv_path) == 0
            self._csv_file = open(self.csv_path, "a", newline="")
            self._csv_writer = csv.DictWriter(self._csv_file, fieldnames=CSV_COLUMNS)
            if new:
                self._csv_writer.writeheader()
                self._csv_file.flush()

    def report(self, pkt: Packet, epoch: float | None = None) -> None:
        epoch = time.time() if epoch is None else epoch
        if not pkt.valid and not self.keep_invalid:
            self._invalid += 1
            return
        self._total += 1

        if self._csv_writer is not None:
            self._csv_writer.writerow(_row(pkt, epoch))
            self._csv_file.flush()

        key = (pkt.source, pkt.unit_addr)
        st = self._stats.setdefault(key, _Stat())
        st.count += 1
        st.last_epoch = epoch

        if self.console:
            show = self.dedupe <= 0 or (epoch - st.last_console_epoch) >= self.dedupe
            if show:
                st.last_console_epoch = epoch
                tag = "" if pkt.valid else "  !BCH"
                print(format_console(pkt, epoch) + tag)

        self._maybe_summary(epoch)

    def _maybe_summary(self, epoch: float) -> None:
        if self.stats_interval <= 0 or not self.console:
            return
        if epoch - self._last_summary < self.stats_interval:
            return
        self._last_summary = epoch
        units = sorted(
            (k for k in self._stats if k[1] is not None),
            key=lambda k: self._stats[k].last_epoch, reverse=True,
        )[:5]
        recent = ", ".join(f"{src}:{addr}({self._stats[(src, addr)].count})"
                            for src, addr in units)
        print(f"-- stats: {self._total} pkts, {len(self._stats)} units"
              + (f"; recent: {recent}" if recent else ""), file=sys.stderr)

    def summary(self) -> str:
        lines = [f"Total valid packets: {self._total}  (BCH-failed dropped: {self._invalid})"]
        for (src, addr), st in sorted(self._stats.items(),
                                      key=lambda kv: kv[1].count, reverse=True):
            lines.append(f"  {src} unit {addr}: {st.count} packets")
        return "\n".join(lines)

    def close(self) -> None:
        if self._csv_file is not None:
            self._csv_file.close()
            self._csv_file = None
