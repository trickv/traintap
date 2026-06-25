"""Train-pass correlation (EOT IDs <-> DPU)."""
import csv

from traintap.frame import Packet
from traintap.output import PassTracker


def _pkt(source, unit):
    return Packet(source=source, freq_hz=0, valid=True,
                  data_block="", checkbits_rx="", unit_addr=unit)


def test_pass_correlates_eot_and_dpu(tmp_path):
    path = tmp_path / "passes.csv"
    pt = PassTracker(gap=90, csv_path=str(path), console=False)
    # One train pass: EOT 69686 + DPU 12345 heard together.
    pt.add(_pkt("EOT", 69686), 1000.0)
    pt.add(_pkt("DPU", 12345), 1002.0)
    pt.add(_pkt("EOT", 69686), 1004.0)
    # >90 s later -> a separate pass (EOT only).
    pt.add(_pkt("EOT", 55555), 1200.0)
    pt.close()

    rows = list(csv.DictReader(open(path)))
    assert len(rows) == 2
    assert rows[0]["eot_units"] == "69686" and rows[0]["eot_pkts"] == "2"
    assert rows[0]["dpu_units"] == "12345" and rows[0]["dpu_pkts"] == "1"
    assert rows[1]["eot_units"] == "55555" and rows[1]["dpu_units"] == ""


def test_tick_flushes_idle_pass(tmp_path):
    path = tmp_path / "p.csv"
    pt = PassTracker(gap=60, csv_path=str(path), console=False)
    pt.add(_pkt("EOT", 1), 100.0)
    pt.tick(130.0)                       # within gap -> still open
    assert pt.n_passes == 0
    pt.tick(200.0)                       # gap exceeded -> flush
    assert pt.n_passes == 1
