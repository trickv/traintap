"""Native I/Q DSP for traintap.

Pipeline (one call per scan dwell; no cross-block state needed):

    complex I/Q @ fs_in
      -> NCO mix the channel (at +offset) down to baseband
      -> resample to 48 kHz + complex low-pass (channel select)
      -> FM demod (angle of x[n]*conj(x[n-1]))  -> 1200/1800 Hz AFSK audio
      -> matched filters at mark/space -> soft decision (|mark|-|space|)
      -> Direwolf-style DPLL @ 1200 baud -> bit string

The EOT/HOT link is Bell-202-like AFSK: after FM demod the data is carried as
1200 Hz (mark/'1') and 1800 Hz (space/'0') tones at 1200 baud — the same thing
minimodem's `-M 1200 -S 1800` decodes from rtl_fm's audio. We do it in-process.

`synthesize_iq()` is the inverse, used by tests and `traintap --selftest` to
generate a known signal end-to-end with no radio attached.
"""

from __future__ import annotations

from math import gcd

import numpy as np
from scipy.signal import firwin, lfilter, resample_poly, welch

AUDIO_FS = 48_000
BAUD = 1200
MARK_HZ = 1200
SPACE_HZ = 1800
DEFAULT_OFFSET_HZ = 250_000   # tune this far below the channel to dodge the DC spike
# Channel-select cutoff before FM demod. Kept tight (~6 kHz) for selectivity: the
# AAR channels are spaced 12.5 kHz, so DPU (457.9250) and the 457.9500 neighbor
# sit ±12.5 kHz from EOT and MUST be rejected — otherwise they bleed in, corrupt
# the demod, and trip the carrier detector. Real-world captures showed the dongle
# is on-frequency (~-125 Hz), so no extra width is needed for ppm; set --ppm if a
# given dongle drifts. (A brief 15 kHz experiment let neighbors through; reverted.)
CHANNEL_LPF_HZ = 6_000


def _resample_factors(fs_in: int, fs_out: int) -> tuple[int, int]:
    g = gcd(int(fs_in), int(fs_out))
    return fs_out // g, fs_in // g


def _matched_filters(audio_fs: int) -> tuple[np.ndarray, np.ndarray]:
    """Length-one-bit complex correlator taps for mark and space tones."""
    n = np.arange(round(audio_fs / BAUD))
    mark = np.exp(2j * np.pi * MARK_HZ / audio_fs * n)
    space = np.exp(2j * np.pi * SPACE_HZ / audio_fs * n)
    return mark, space


def iq_to_audio(iq: np.ndarray, fs_in: int, offset_hz: float,
                audio_fs: int = AUDIO_FS) -> np.ndarray:
    """NCO-mix the channel to baseband, decimate, channel-filter, FM-demod."""
    iq = np.asarray(iq, dtype=np.complex64)
    n = np.arange(len(iq))
    iq = iq * np.exp(-2j * np.pi * offset_hz / fs_in * n)

    up, down = _resample_factors(fs_in, audio_fs)
    baseband = resample_poly(iq, up, down)

    taps = firwin(65, CHANNEL_LPF_HZ, fs=audio_fs)
    baseband = lfilter(taps, 1.0, baseband)

    # FM discriminator: instantaneous frequency.
    return np.angle(baseband[1:] * np.conj(baseband[:-1]))


def audio_to_soft(audio: np.ndarray, audio_fs: int = AUDIO_FS) -> np.ndarray:
    """Matched-filter the AFSK audio into a soft decision (>0 => mark/'1')."""
    mark_taps, space_taps = _matched_filters(audio_fs)
    y_mark = np.abs(lfilter(mark_taps, 1.0, audio))
    y_space = np.abs(lfilter(space_taps, 1.0, audio))
    return y_mark - y_space


def channel_activity_db(iq: np.ndarray, fs_in: int, offset_hz: float,
                        halfbw: float = 5_000.0,
                        guard_hz: float = 120_000) -> float:
    """In-channel vs guard-band power ratio (dB) — a carrier-present detector.

    Measured in the frequency domain (Welch PSD) so the in-band window is sharp:
    only ±`halfbw` around the EOT carrier counts, which rejects the ±12.5 kHz
    adjacent channels (DPU 457.9250, neighbor 457.9500) that a wide/short FIR
    would leak. Ratio-based, so robust to the dongle's AGC. ~0 dB for noise; a
    real EOT carrier pushes it well positive even at low burst duty cycle.
    """
    f, P = welch(iq, fs=fs_in, nperseg=8192, return_onesided=False)
    in_band = P[np.abs(f - offset_hz) < halfbw]
    guard = P[np.abs(f - (offset_hz - guard_hz)) < halfbw]
    return float(10 * np.log10(in_band.mean() / (guard.mean() + 1e-20)))


def _s32(x: int) -> int:
    x &= 0xFFFFFFFF
    return x - (1 << 32) if x >= (1 << 31) else x


def soft_to_bits(soft: np.ndarray, audio_fs: int = AUDIO_FS,
                 inertia: float = 0.75) -> str:
    """Direwolf-style digital PLL bit/clock recovery -> "0"/"1" string.

    A signed-32-bit accumulator advances by one bit per ~`sps` samples; each
    overflow is a sampling instant. On every soft-decision sign change (a symbol
    transition) the accumulator is pulled toward center, locking the sampler to
    mid-symbol. Robust for 1200-baud AFSK and cheap enough in pure Python.
    """
    step = int((1 << 32) * BAUD / audio_fs)
    pll = 0
    out = []
    prev = bool(soft[0] > 0)
    for s in soft:
        cur = bool(s > 0)
        prev_pll = pll
        pll = _s32(pll + step)
        if prev_pll > 0 and pll < 0:        # accumulator overflowed -> sample
            out.append("1" if s > 0 else "0")
        if cur != prev:                     # symbol transition -> resync phase
            pll = _s32(int(pll * inertia))
        prev = cur
    return "".join(out)


def iq_to_bits(iq: np.ndarray, fs_in: int, offset_hz: float = DEFAULT_OFFSET_HZ,
               audio_fs: int = AUDIO_FS) -> tuple[str, str]:
    """Full front end: I/Q -> (bits, inverted_bits).

    Both polarities are returned because mark/space->1/0 mapping can invert with
    spectral flips; the caller searches the sync word in each.
    """
    audio = iq_to_audio(iq, fs_in, offset_hz, audio_fs)
    soft = audio_to_soft(audio, audio_fs)
    bits = soft_to_bits(soft, audio_fs)
    inv = bits.translate(str.maketrans("01", "10"))
    return bits, inv


# --- Synthesis (tests / --selftest) ------------------------------------------

def synthesize_iq(bits: str, fs_out: int, offset_hz: float = DEFAULT_OFFSET_HZ,
                  deviation_hz: float = 3000.0, baud: int = BAUD,
                  noise: float = 0.0, seed: int = 0) -> np.ndarray:
    """Generate complex I/Q for an AFSK bitstring, as a real EOT signal would
    appear `offset_hz` above the SDR center frequency. Inverse of iq_to_bits."""
    sps = fs_out / baud
    total = int(round(len(bits) * sps))
    t = np.arange(total)
    # Continuous-phase audio tone (1200 for '1', 1800 for '0').
    inst_audio = np.empty(total)
    for i in range(total):
        b = bits[min(int(i / sps), len(bits) - 1)]
        inst_audio[i] = MARK_HZ if b == "1" else SPACE_HZ
    audio_phase = 2 * np.pi * np.cumsum(inst_audio) / fs_out
    msg = np.cos(audio_phase)                       # the tone, in [-1, 1]

    f_rf = offset_hz + deviation_hz * msg           # FM: carrier rides the tone
    rf_phase = 2 * np.pi * np.cumsum(f_rf) / fs_out
    iq = np.exp(1j * rf_phase).astype(np.complex64)

    if noise > 0:
        rng = np.random.default_rng(seed)
        iq = iq + noise * (rng.standard_normal(total) + 1j * rng.standard_normal(total))
    return iq
