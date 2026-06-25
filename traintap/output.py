"""Output sinks for decoded packets: console, CSV, and live stats/dedupe."""

from __future__ import annotations

import csv
import os
import sys
import time
from dataclasses import dataclass, field

from .frame import Packet

CSV_COLUMNS = [
    "timestamp", "epoch", "source", "freq_hz", "valid", "corrected", "unit_addr",
    "pressure_psig", "batt_charge_pct", "batt_cond", "message_type",
    "arm_status", "motion", "marker_light", "turbine", "valve_ckt",
    "data_block", "checkbits_rx",
]


def _open_append_csv(path: str, columns: list[str]):
    """Open `path` for append with a header matching `columns`.

    If an existing file's header differs (e.g. a column was added between runs),
    archive it to `path.N` and start fresh — otherwise new rows would be appended
    under a mismatched header and silently misalign the CSV.
    """
    if os.path.exists(path) and os.path.getsize(path) > 0:
        with open(path, newline="") as f:
            header = f.readline().rstrip("\r\n").split(",")
        if header != columns:
            i = 1
            while os.path.exists(f"{path}.{i}"):
                i += 1
            os.rename(path, f"{path}.{i}")
    new = not os.path.exists(path) or os.path.getsize(path) == 0
    fh = open(path, "a", newline="")
    writer = csv.DictWriter(fh, fieldnames=columns)
    if new:
        writer.writeheader()
        fh.flush()
    return fh, writer


def _row(pkt: Packet, epoch: float) -> dict:
    return {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))
        + f".{int((epoch % 1) * 1000):03d}",
        "epoch": f"{epoch:.3f}",
        "source": pkt.source,
        "freq_hz": pkt.freq_hz,
        "valid": int(pkt.valid),
        "corrected": pkt.corrected,
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
    fec = f" ~{pkt.corrected}b" if pkt.corrected else ""   # BCH-corrected marker
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
                + (f"  [{tail}]" if tail else "") + fec)
    return (f"{ts} {pkt.source} {mhz:.4f}  msg-type {pkt.message_type}"
            f"  (data {pkt.data_block}){fec}")


@dataclass
class _Stat:
    count: int = 0
    last_epoch: float = 0.0
    last_console_epoch: float = 0.0


PASS_CSV_COLUMNS = ["start", "end", "duration_s", "eot_units", "eot_pkts",
                    "dpu_units", "dpu_pkts", "hot_units", "hot_pkts"]


@dataclass
class PassTracker:
    """Groups packets heard close in time into one 'train pass'.

    Because EOT and DPU are decoded from the same dwell, packets within `gap`
    seconds of each other belong to the same physical train going by. Each pass
    is tagged by its EOT unit address(es) (the train's identity) and lists any
    DPU (and HOT) units heard alongside — i.e. the coordination between an EOT
    train ID and the distributed-power telemetry of the same train.
    """

    gap: float = 90.0
    csv_path: str | None = None
    console: bool = True
    n_passes: int = field(default=0, init=False)
    _members: dict = field(default_factory=dict, init=False)   # source -> {unit: count}
    _start: float = field(default=0.0, init=False)
    _last: float = field(default=0.0, init=False)
    _csv_file: object = field(default=None, init=False)
    _csv_writer: object = field(default=None, init=False)

    def __post_init__(self):
        if self.csv_path:
            self._csv_file, self._csv_writer = _open_append_csv(
                self.csv_path, PASS_CSV_COLUMNS)

    def add(self, pkt: Packet, epoch: float) -> None:
        if self._members and (epoch - self._last) > self.gap:
            self.flush()
        if not self._members:
            self._start = epoch
        d = self._members.setdefault(pkt.source, {})
        u = "?" if pkt.unit_addr is None else pkt.unit_addr
        d[u] = d.get(u, 0) + 1
        self._last = epoch

    def tick(self, now: float) -> None:
        if self._members and (now - self._last) > self.gap:
            self.flush()

    def _fmt(self, source: str) -> str:
        d = self._members.get(source, {})
        return " ".join(f"{u}x{c}" for u, c in sorted(d.items(), key=lambda kv: -kv[1])) or "-"

    def flush(self) -> None:
        if not self._members:
            return
        self.n_passes += 1
        dur = self._last - self._start
        if self.console:
            t = lambda e: time.strftime("%H:%M:%S", time.localtime(e))
            print(f"== PASS {t(self._start)}-{t(self._last)} ({dur:.0f}s)  "
                  f"EOT {self._fmt('EOT')}  DPU {self._fmt('DPU')}", file=sys.stderr)
        if self._csv_writer is not None:
            units = lambda s: "|".join(str(u) for u in self._members.get(s, {}))
            pkts = lambda s: sum(self._members.get(s, {}).values())
            fmt = lambda e: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e))
            self._csv_writer.writerow({
                "start": fmt(self._start), "end": fmt(self._last),
                "duration_s": f"{dur:.0f}",
                "eot_units": units("EOT"), "eot_pkts": pkts("EOT"),
                "dpu_units": units("DPU"), "dpu_pkts": pkts("DPU"),
                "hot_units": units("HOT"), "hot_pkts": pkts("HOT")})
            self._csv_file.flush()
        self._members = {}

    def close(self) -> None:
        self.flush()
        if self._csv_file is not None:
            self._csv_file.close()
            self._csv_file = None


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
    passes_csv: str | None = None
    pass_gap: float = 90.0

    _csv_file: object = field(default=None, init=False)
    _csv_writer: object = field(default=None, init=False)
    _stats: dict = field(default_factory=dict, init=False)
    _total: int = field(default=0, init=False)
    _invalid: int = field(default=0, init=False)
    _last_summary: float = field(default=0.0, init=False)
    _passes: object = field(default=None, init=False)

    def __post_init__(self):
        if self.csv_path:
            self._csv_file, self._csv_writer = _open_append_csv(self.csv_path, CSV_COLUMNS)
        self._passes = PassTracker(gap=self.pass_gap, csv_path=self.passes_csv,
                                   console=self.console)

    def report(self, pkt: Packet, epoch: float | None = None) -> None:
        epoch = time.time() if epoch is None else epoch
        if not pkt.valid and not self.keep_invalid:
            self._invalid += 1
            return
        self._total += 1

        if self._csv_writer is not None:
            self._csv_writer.writerow(_row(pkt, epoch))
            self._csv_file.flush()

        self._passes.add(pkt, epoch)

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

    def tick(self, now: float) -> None:
        """Flush a completed train pass once the channel goes quiet."""
        if self._passes is not None:
            self._passes.tick(now)

    def close(self) -> None:
        if self._passes is not None:
            self._passes.close()
        if self._csv_file is not None:
            self._csv_file.close()
            self._csv_file = None
