"""Train-pass correlation (EOT IDs <-> DPU)."""
import csv

from traintap.frame import Packet
from traintap.output import PassTracker, Reporter, SignalLog, _open_append_csv


def test_signal_log_writes_rows(tmp_path):
    path = tmp_path / "signal.csv"
    sl = SignalLog(str(path))
    sl.log(1000.0, 457_937_500, "EOT", 9.4, 2)
    sl.log(1004.0, 457_937_500, "EOT", 12.1, 0)
    sl.close()
    rows = list(csv.DictReader(open(path)))
    assert len(rows) == 2
    assert rows[0]["activity_db"] == "9.4" and rows[0]["source"] == "EOT"
    assert rows[0]["n_valid"] == "2" and rows[1]["n_valid"] == "0"


def test_signal_log_noop_without_path():
    SignalLog(None).log(1.0, 1, "EOT", 5.0, 1)  # must not raise


def _epkt(source, unit, corrected=0):
    return Packet(source=source, freq_hz=457_937_500, valid=True,
                  data_block="d" + str(unit), checkbits_rx="c",
                  unit_addr=unit, corrected=corrected)


def _rows(path):
    return list(csv.DictReader(open(path)))


def _reporter(path):
    return Reporter(csv_path=str(path), console=False, dedupe=0, stats_interval=0,
                    pass_gap=90)


def test_uncorrected_emitted_immediately(tmp_path):
    p = tmp_path / "t.csv"; r = _reporter(p)
    r.report(_epkt("EOT", 100, corrected=0), 1000.0)
    r.close()
    assert [x["unit_addr"] for x in _rows(p)] == ["100"]


def test_isolated_corrected_is_dropped(tmp_path):
    p = tmp_path / "t.csv"; r = _reporter(p)
    r.report(_epkt("DPU", 555, corrected=2), 1000.0)   # isolated -> pending
    r.tick(1000.0 + 100)                               # aged past gap -> dropped
    r.close()
    assert _rows(p) == []


def test_corrected_corroborated_by_clean(tmp_path):
    p = tmp_path / "t.csv"; r = _reporter(p)
    r.report(_epkt("EOT", 200, corrected=0), 1000.0)   # clean confirms unit
    r.report(_epkt("EOT", 200, corrected=1), 1002.0)   # corrected -> accepted
    r.close()
    assert len(_rows(p)) == 2


def test_corrected_pair_corroborate_each_other(tmp_path):
    p = tmp_path / "t.csv"; r = _reporter(p)
    r.report(_epkt("EOT", 300, corrected=2), 1000.0)   # pending
    r.report(_epkt("EOT", 300, corrected=2), 1003.0)   # repeat -> both emitted
    r.close()
    assert len(_rows(p)) == 2


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


def test_pass_handles_out_of_order_epochs(tmp_path):
    # A corroborated corrected packet can be emitted with an older timestamp.
    path = tmp_path / "p.csv"
    pt = PassTracker(gap=90, csv_path=str(path), console=False)
    pt.add(_pkt("EOT", 68777), 1000.0)
    pt.add(_pkt("EOT", 68777), 938.0)        # released late, older epoch
    pt.close()
    row = list(csv.DictReader(open(path)))[0]
    assert int(row["duration_s"]) >= 0       # never negative
    assert row["start"] <= row["end"]


def test_tick_flushes_idle_pass(tmp_path):
    path = tmp_path / "p.csv"
    pt = PassTracker(gap=60, csv_path=str(path), console=False)
    pt.add(_pkt("EOT", 1), 100.0)
    pt.tick(130.0)                       # within gap -> still open
    assert pt.n_passes == 0
    pt.tick(200.0)                       # gap exceeded -> flush
    assert pt.n_passes == 1
