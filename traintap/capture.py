"""RTL-SDR capture, offset tuning, I/Q record/replay, and the scan policy.

`rtlsdr` (pyrtlsdr) is imported lazily so the rest of traintap — DSP, framing,
tests, replay — works with no dongle and no system librtlsdr installed.

The single-dongle reality: EOT (457.9375) and HOT (452.9375) are 5 MHz apart,
beyond an RTL-SDR's usable bandwidth, so we time-share by retuning. ScanPlan is
pure policy (no I/O) and is unit-tested: it favors EOT while a train is active,
splits evenly when idle, and lingers on HOT when a HOT packet just decoded.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from . import EOT_FREQ_HZ, HOT_FREQ_HZ
from .dsp import DEFAULT_OFFSET_HZ


class RadioUnavailable(RuntimeError):
    """Raised when pyrtlsdr / the native librtlsdr can't be loaded."""


def _import_rtlsdr():
    """Lazy import with an actionable error instead of a raw ImportError."""
    try:
        from rtlsdr import RtlSdr
    except ImportError as e:
        raise RadioUnavailable(
            "Could not load librtlsdr. Install the native library and driver:\n"
            "  sudo apt install rtl-sdr librtlsdr-dev\n"
            "  echo 'blacklist dvb_usb_rtl28xxu' | "
            "sudo tee /etc/modprobe.d/blacklist-rtl.conf\n"
            "then replug the dongle. (No radio needed for --selftest / --replay.)"
        ) from e
    return RtlSdr


def list_devices() -> list[str]:
    RtlSdr = _import_rtlsdr()
    try:
        serials = RtlSdr.get_device_serial_addresses()
    except Exception:
        serials = []
    count = getattr(RtlSdr, "get_device_count", lambda: len(serials))()
    return [f"#{i} serial={serials[i] if i < len(serials) else '?'}"
            for i in range(count)]


@dataclass
class RtlSource:
    """Live RTL-SDR I/Q source with offset tuning."""

    device_index: int = 0
    sample_rate: int = 1_024_000
    gain: object = "auto"            # "auto" or float dB
    ppm: int = 0
    offset_hz: float = DEFAULT_OFFSET_HZ
    _sdr: object = field(default=None, init=False)

    def open(self) -> "RtlSource":
        RtlSdr = _import_rtlsdr()
        sdr = RtlSdr(device_index=self.device_index)
        sdr.sample_rate = self.sample_rate
        if self.ppm:
            sdr.freq_correction = int(self.ppm)
        sdr.gain = "auto" if self.gain == "auto" else float(self.gain)
        self._sdr = sdr
        return self

    def read(self, channel_hz: int, seconds: float) -> np.ndarray:
        """Tune `offset_hz` below the channel (dodging the DC spike) and read.

        Reads are chunked: a single libusb bulk transfer can't allocate tens of
        MB, so long dwells are split into <=CHUNK-sample reads (each a multiple
        of 256 samples, as pyrtlsdr requires) and concatenated.
        """
        self._sdr.center_freq = int(channel_hz - self.offset_hz)
        CHUNK = 1 << 19  # 524288 samples = 1 MiB I/Q per transfer
        total = (int(self.sample_rate * seconds) // 256) * 256
        parts = []
        got = 0
        while got < total:
            n = min(CHUNK, total - got)
            parts.append(np.asarray(self._sdr.read_samples(n), dtype=np.complex64))
            got += n
        return np.concatenate(parts) if parts else np.empty(0, np.complex64)

    def close(self) -> None:
        if self._sdr is not None:
            self._sdr.close()
            self._sdr = None


@dataclass
class ReplaySource:
    """Replays recorded I/Q (an .npz with iq/fs/channel/offset) as one dwell."""

    iq: np.ndarray
    sample_rate: int
    channel_hz: int
    offset_hz: float = DEFAULT_OFFSET_HZ
    _served: bool = field(default=False, init=False)

    @classmethod
    def load(cls, path: str) -> "ReplaySource":
        d = np.load(path, allow_pickle=False)
        return cls(iq=d["iq"], sample_rate=int(d["fs"]),
                   channel_hz=int(d["channel"]),
                   offset_hz=float(d["offset"]) if "offset" in d else DEFAULT_OFFSET_HZ)

    def open(self) -> "ReplaySource":
        return self

    def read(self, channel_hz: int, seconds: float) -> np.ndarray:
        self._served = True
        return self.iq

    def exhausted(self) -> bool:
        return self._served

    def close(self) -> None:
        pass


def record_iq(path: str, iq: np.ndarray, fs: int, channel: int,
              offset: float = DEFAULT_OFFSET_HZ) -> None:
    if not path.endswith(".npz"):
        path = path + ".npz"
    np.savez(path, iq=iq.astype(np.complex64), fs=fs, channel=channel, offset=offset)


# --- Scan policy (pure, testable) --------------------------------------------

@dataclass
class ScanPlan:
    """Decides the next (name, freq_hz, dwell_seconds) and adapts to activity.

    mode "eot"/"hot": stay on one channel. mode "scan": time-share with EOT
    favored. `active_eot_runs` EOT dwells happen between HOT dips while a train
    is active (an EOT decoded within `active_window` s); when idle, EOT and HOT
    alternate evenly. A HOT dwell that yields a packet triggers linger (another
    HOT dwell immediately) — HOT bursts cluster.
    """

    mode: str = "scan"
    eot_dwell: float = 4.0
    hot_dwell: float = 1.0
    active_window: float = 15.0
    active_eot_runs: int = 6

    _last_eot_epoch: float = field(default=-1e18, init=False)
    _eot_credits: int = field(default=0, init=False)
    _hot_linger: bool = field(default=False, init=False)

    def next_dwell(self, now: float) -> tuple[str, int, float]:
        if self.mode == "eot":
            return ("EOT", EOT_FREQ_HZ, self.eot_dwell)
        if self.mode == "hot":
            return ("HOT", HOT_FREQ_HZ, self.hot_dwell)

        if self._hot_linger:
            return ("HOT", HOT_FREQ_HZ, self.hot_dwell)

        active = (now - self._last_eot_epoch) < self.active_window
        if self._eot_credits > 0:
            return ("EOT", EOT_FREQ_HZ, self.eot_dwell)
        # credits exhausted -> take a HOT dip, then refill based on activity.
        self._eot_credits = self.active_eot_runs if active else 1
        return ("HOT", HOT_FREQ_HZ, self.hot_dwell)

    def record_result(self, name: str, valid_count: int, now: float) -> None:
        if name == "EOT":
            if valid_count > 0:
                self._last_eot_epoch = now
            if self._eot_credits > 0:
                self._eot_credits -= 1
            self._hot_linger = False
        else:  # HOT
            self._hot_linger = valid_count > 0
            if not self._hot_linger and self._eot_credits == 0:
                # ensure forward progress back to EOT after a dry HOT dip
                self._eot_credits = self.active_eot_runs \
                    if (now - self._last_eot_epoch) < self.active_window else 1
