"""traintap command-line interface."""

from __future__ import annotations

import argparse
import os
import sys
import time

from . import DPU_FREQ_HZ, EOT_FREQ_HZ, HOT_FREQ_HZ, __version__
from .dsp import DEFAULT_OFFSET_HZ, channel_activity_db, iq_to_bits, synthesize_iq
from .frame import DEFAULT_MAX_CORRECT, Packet, encode_eot, find_frames
from .output import Reporter, SignalLog


def decode_dwell(name: str, freq_hz: int, iq, fs: int, offset_hz: float,
                 keep_invalid: bool = False,
                 max_correct: int = DEFAULT_MAX_CORRECT) -> list[Packet]:
    """Decode one dwell's I/Q into packets (both bit polarities searched)."""
    bits, inv = iq_to_bits(iq, fs, offset_hz)
    out: list[Packet] = []
    seen: set[str] = set()
    for stream in (bits, inv):
        for pkt in find_frames(stream, freq_hz, source=name, max_correct=max_correct):
            if not pkt.valid and not keep_invalid:
                continue
            if pkt.data_block in seen:
                continue
            seen.add(pkt.data_block)
            out.append(pkt)
    return out


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="traintap",
        description="RTL-SDR End/Head-of-Train (EOT/HOT) telemetry decoder.")
    p.add_argument("--version", action="version", version=f"traintap {__version__}")

    g = p.add_argument_group("radio")
    g.add_argument("--device", type=int, default=0, help="RTL-SDR device index")
    g.add_argument("--sample-rate", type=int, default=1_024_000)
    g.add_argument("--gain", default="auto", help="'auto' or gain in dB")
    g.add_argument("--ppm", type=int, default=0, help="frequency correction (ppm)")
    g.add_argument("--offset", type=float, default=DEFAULT_OFFSET_HZ,
                   help="tuning offset below channel to dodge the DC spike (Hz)")
    g.add_argument("--list-devices", action="store_true")

    s = p.add_argument_group("scan")
    s.add_argument("--mode", choices=["scan", "eot", "hot", "hotwatch"],
                   default="scan",
                   help="hotwatch: watch HOT as an approach trigger, then lock "
                        "onto EOT/DPU once a train's head end is heard")
    s.add_argument("--eot-dwell", type=float, default=4.0)
    s.add_argument("--hot-dwell", type=float, default=1.0)
    s.add_argument("--hot-fraction", type=float, default=0.7,
                   help="hotwatch: idle fraction of time spent on HOT (default 0.7)")
    s.add_argument("--focus-minutes", type=float, default=10.0,
                   help="hotwatch: minutes to lock onto EOT/DPU after a HOT hit")
    s.add_argument("--no-dpu", dest="dpu", action="store_false",
                   help="don't also decode DPU (457.9250) from the EOT capture")

    o = p.add_argument_group("output")
    o.add_argument("--csv", help="append decoded packets to this CSV file")
    o.add_argument("--dedupe", type=float, default=30.0,
                   help="seconds to suppress repeat console lines per unit (0=off)")
    o.add_argument("--stats-interval", type=float, default=60.0,
                   help="seconds between live stats summaries (0=off)")
    o.add_argument("--passes-csv", metavar="FILE",
                   help="log one row per train pass, correlating the EOT unit ID "
                        "with DPU (and HOT) units heard together")
    o.add_argument("--pass-gap", type=float, default=90.0,
                   help="seconds of silence that ends a train pass (default 90)")
    o.add_argument("--signal-csv", metavar="FILE",
                   help="log in-channel signal strength (dB) per carrier dwell, "
                        "for tracking antenna placement / reception over time")
    o.add_argument("--quiet", action="store_true", help="no per-packet console output")
    o.add_argument("--keep-invalid", action="store_true",
                   help="also report BCH-failed packets (debugging)")
    o.add_argument("--bch-correct", type=int, default=DEFAULT_MAX_CORRECT,
                   metavar="N", help="BCH-correct up to N bit errors per frame "
                   "(0 disables; default %(default)s). Corrected packets are "
                   "logged with their correction count.")

    r = p.add_argument_group("capture / replay / test")
    r.add_argument("--record", metavar="FILE.npz",
                   help="capture one dwell of I/Q to FILE and exit")
    r.add_argument("--record-seconds", type=float, default=10.0)
    r.add_argument("--record-channel", choices=["eot", "hot"], default="eot")
    r.add_argument("--replay", metavar="FILE.npz",
                   help="decode recorded I/Q instead of a live radio")
    r.add_argument("--selftest", action="store_true",
                   help="synthesize a known packet through the full DSP and exit")
    r.add_argument("--save-active", metavar="DIR",
                   help="while scanning, save raw I/Q of any dwell with a carrier "
                        "present (timestamped .npz) for offline analysis")
    r.add_argument("--activity-threshold", type=float, default=6.0,
                   help="in-channel/guard power ratio (dB) that counts as a carrier")
    r.add_argument("--save-active-max", type=int, default=200,
                   help="max raw captures to retain (~32 MB each); oldest pruned "
                        "so --save-active can't fill the disk (0 = unlimited)")
    r.add_argument("--signal-meter", action="store_true",
                   help="live in-channel signal strength readout (for aiming the "
                        "antenna); peak it on a steady reference via --meter-freq")
    r.add_argument("--meter-freq", type=float, default=0.0,
                   help="frequency (Hz) for --signal-meter; default = EOT channel. "
                        "Tip: point at a strong steady carrier, e.g. 460000000")
    return p


def _channel(name: str) -> int:
    return EOT_FREQ_HZ if name == "eot" else HOT_FREQ_HZ


def cmd_selftest(args) -> int:
    preamble = "10" * 40
    pkt_bits = encode_eot(unit_addr=45678, pressure=92, batt_charge=100,
                          batt_cond="OK", motion=1, marker_light=1)
    iq = synthesize_iq(preamble + pkt_bits + "10" * 8, fs_out=args.sample_rate,
                       offset_hz=args.offset, noise=0.1, seed=1)
    pkts = decode_dwell("EOT", EOT_FREQ_HZ, iq, args.sample_rate, args.offset)
    ok = any(p.valid and p.unit_addr == 45678 and p.pressure == 92 for p in pkts)
    print(f"selftest: {'PASS' if ok else 'FAIL'} "
          f"({len(pkts)} packet(s) decoded)")
    return 0 if ok else 1


def cmd_meter(args) -> int:
    """Live in-channel signal-strength readout for antenna aiming."""
    from .capture import RtlSource
    freq = int(args.meter_freq) if args.meter_freq else EOT_FREQ_HZ
    src = RtlSource(device_index=args.device, sample_rate=args.sample_rate,
                    gain=args.gain, ppm=args.ppm, offset_hz=args.offset).open()
    print(f"Signal meter @ {freq/1e6:.4f} MHz  (Ctrl-C to stop)\n"
          f"Tip: aim/orient the antenna to maximize dB. Vertical polarization.",
          file=sys.stderr)
    peak = -1e9
    try:
        while True:
            iq = src.read(freq, 0.25)
            act = channel_activity_db(iq, src.sample_rate, src.offset_hz)
            peak = max(peak, act)
            bars = "#" * max(0, min(50, int(act * 1.5)))
            print(f"\r{act:+6.1f} dB  peak {peak:+6.1f}  |{bars:<50}|",
                  end="", flush=True)
    except KeyboardInterrupt:
        print("\nstopped.", file=sys.stderr)
    finally:
        src.close()
    return 0


def cmd_record(args) -> int:
    from .capture import RtlSource, record_iq
    src = RtlSource(device_index=args.device, sample_rate=args.sample_rate,
                    gain=args.gain, ppm=args.ppm, offset_hz=args.offset).open()
    try:
        ch = _channel(args.record_channel)
        print(f"Recording {args.record_seconds:g}s @ {ch/1e6:.4f} MHz ...",
              file=sys.stderr)
        iq = src.read(ch, args.record_seconds)
        record_iq(args.record, iq, args.sample_rate, ch, args.offset)
        print(f"Wrote {len(iq)} samples to {args.record}", file=sys.stderr)
    finally:
        src.close()
    return 0


def _prune_captures(dir_path: str, keep: int) -> None:
    """Keep only the newest `keep` capture files so --save-active can't fill the
    disk. Validated decodes live in the CSV; raw captures are disposable."""
    if keep <= 0:
        return
    import glob
    files = sorted(glob.glob(os.path.join(dir_path, "active_*.npz")),
                   key=os.path.getmtime)
    for f in files[:-keep]:
        try:
            os.remove(f)
        except OSError:
            pass


def run_loop(source, plan, reporter, *, is_replay: bool,
             save_active_dir: str | None = None,
             activity_threshold: float = 6.0,
             max_correct: int = DEFAULT_MAX_CORRECT,
             decode_dpu_too: bool = True,
             signal_log=None,
             save_active_max: int = 200) -> None:
    from .capture import record_iq
    if save_active_dir:
        os.makedirs(save_active_dir, exist_ok=True)
    while True:
        now = time.time()
        name, freq, dwell = plan.next_dwell(now)
        try:
            iq = source.read(freq, dwell)
        except Exception as e:  # hardware hiccup: report and continue
            print(f"read error: {e}", file=sys.stderr)
            time.sleep(0.5)
            continue

        # In-channel signal strength for save-active and/or the signal log.
        act = None
        if (save_active_dir or signal_log is not None) and not is_replay:
            act = channel_activity_db(iq, source.sample_rate, source.offset_hz)
            if save_active_dir and act >= activity_threshold:
                path = os.path.join(save_active_dir,
                                    f"active_{name}_{int(now)}.npz")
                try:  # a save failure (e.g. disk full) must never kill the monitor
                    record_iq(path, iq, source.sample_rate, freq, source.offset_hz)
                    print(f"** carrier on {name} ({act:+.1f} dB) -> saved {path}",
                          file=sys.stderr)
                    _prune_captures(save_active_dir, save_active_max)
                except OSError as e:
                    print(f"capture save failed ({e}); continuing", file=sys.stderr)

        pkts = decode_dwell(name, freq, iq, source.sample_rate, source.offset_hz,
                            keep_invalid=reporter.keep_invalid,
                            max_correct=max_correct)
        eot_valid = sum(1 for p in pkts if p.valid)
        if signal_log is not None and act is not None and act >= activity_threshold:
            signal_log.log(now, freq, name, act, eot_valid)
        # DPU (457.9250) sits 12.5 kHz below EOT, inside the same capture -- decode
        # it from the same I/Q at the DPU offset, no retuning.
        if decode_dpu_too and name == "EOT":
            dpu_off = source.offset_hz + (DPU_FREQ_HZ - EOT_FREQ_HZ)
            pkts += decode_dwell("DPU", DPU_FREQ_HZ, iq, source.sample_rate,
                                 dpu_off, keep_invalid=reporter.keep_invalid,
                                 max_correct=max_correct)
        for pkt in pkts:
            reporter.report(pkt)
        reporter.tick(time.time())   # close out a finished train pass on silence
        plan.record_result(name, eot_valid, time.time())
        if is_replay and source.exhausted():
            break


def cmd_run(args) -> int:
    from .capture import ReplaySource, RtlSource, ScanPlan

    if args.replay:
        source = ReplaySource.load(args.replay).open()
        # Decode as whatever channel was recorded (single channel/dwell).
        plan = ScanPlan(mode="hot" if source.channel_hz == HOT_FREQ_HZ else "eot")
    else:
        source = RtlSource(device_index=args.device, sample_rate=args.sample_rate,
                           gain=args.gain, ppm=args.ppm, offset_hz=args.offset).open()
        plan = ScanPlan(mode=args.mode, eot_dwell=args.eot_dwell,
                        hot_dwell=args.hot_dwell, hot_fraction=args.hot_fraction,
                        focus_minutes=args.focus_minutes)

    reporter = Reporter(csv_path=args.csv, dedupe=args.dedupe,
                        stats_interval=args.stats_interval,
                        console=not args.quiet, keep_invalid=args.keep_invalid,
                        passes_csv=args.passes_csv, pass_gap=args.pass_gap)
    signal_log = SignalLog(args.signal_csv)
    try:
        run_loop(source, plan, reporter, is_replay=bool(args.replay),
                 save_active_dir=args.save_active,
                 activity_threshold=args.activity_threshold,
                 max_correct=args.bch_correct,
                 decode_dpu_too=args.dpu,
                 signal_log=signal_log,
                 save_active_max=args.save_active_max)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
    finally:
        reporter.close()
        signal_log.close()
        source.close()
        print(reporter.summary(), file=sys.stderr)
    return 0


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    if args.selftest:
        return cmd_selftest(args)
    from .capture import RadioUnavailable
    try:
        if args.list_devices:
            from .capture import list_devices
            devs = list_devices()
            print("\n".join(devs) if devs else "No RTL-SDR devices found.")
            return 0
        if args.signal_meter:
            return cmd_meter(args)
        if args.record:
            return cmd_record(args)
        return cmd_run(args)
    except RadioUnavailable as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
