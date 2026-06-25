"""Train-pass correlation (EOT IDs <-> DPU)."""
import csv

from traintap.frame import Packet
from traintap.output import PassTracker, _open_append_csv


def test_csv_schema_change_archives_old(tmp_path):
    p = tmp_path / "x.csv"
    f, w = _open_append_csv(str(p), ["a", "b"])
    w.writerow({"a": 1, "b": 2}); f.close()
    # reopening with a changed schema must archive the old file, not misalign it
    f2, _ = _open_append_csv(str(p), ["a", "b", "c"])
    f2.close()
    assert (tmp_path / "x.csv.1").exists()
    assert open(p).readline().strip() == "a,b,c"


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
