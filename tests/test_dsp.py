"""DSP tests: synthesize a known packet to I/Q and decode it end-to-end."""

import numpy as np
import pytest

from traintap import frame
from traintap.cli import decode_dwell
from traintap.dsp import synthesize_iq

FS = 1_024_000


def _signal(noise=0.0, seed=0, **fields):
    preamble = "10" * 48
    pkt = frame.encode_eot(**fields)
    bits = preamble + pkt + "10" * 8
    return synthesize_iq(bits, fs_out=FS, noise=noise, seed=seed)


def test_clean_decode():
    iq = _signal(unit_addr=45678, pressure=92, batt_charge=100,
                 batt_cond="OK", motion=1, marker_light=1)
    pkts = decode_dwell("EOT", 457_937_500, iq, FS, 250_000)
    valid = [p for p in pkts if p.valid]
    assert valid, "no valid packet decoded from clean signal"
    p = valid[0]
    assert p.unit_addr == 45678
    assert p.pressure == 92
    assert p.motion == 1


@pytest.mark.parametrize("noise", [0.05, 0.15, 0.3])
def test_noisy_decode(noise):
    iq = _signal(noise=noise, seed=3, unit_addr=2048, pressure=55)
    valid = [p for p in decode_dwell("EOT", 457_937_500, iq, FS, 250_000) if p.valid]
    assert valid
    assert valid[0].unit_addr == 2048
    assert valid[0].pressure == 55


def test_pure_noise_yields_no_valid_packets():
    rng = np.random.default_rng(0)
    n = int(FS * 0.2)
    iq = (rng.standard_normal(n) + 1j * rng.standard_normal(n)).astype(np.complex64)
    valid = [p for p in decode_dwell("EOT", 457_937_500, iq, FS, 250_000) if p.valid]
    assert not valid, "BCH should reject noise-only input"
