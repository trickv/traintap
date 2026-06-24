"""Scan-policy tests: activity weighting and HOT linger-on-hit."""

from traintap import EOT_FREQ_HZ, HOT_FREQ_HZ
from traintap.capture import ScanPlan


def test_single_channel_modes():
    assert ScanPlan(mode="eot").next_dwell(0)[0] == "EOT"
    assert ScanPlan(mode="hot").next_dwell(0)[0] == "HOT"


def test_active_train_favors_eot():
    plan = ScanPlan(mode="scan", active_eot_runs=6)
    # Simulate an EOT hit to mark the train active.
    plan.record_result("EOT", valid_count=1, now=100.0)
    names = []
    t = 100.0
    for _ in range(14):
        name, freq, dwell = plan.next_dwell(t)
        names.append(name)
        plan.record_result(name, valid_count=(1 if name == "EOT" else 0), now=t)
        t += dwell
    # While active, EOT should dominate (roughly active_eot_runs per HOT dip).
    assert names.count("EOT") > names.count("HOT") * 3
    assert "HOT" in names  # but HOT still gets visited


def test_idle_alternates_more_evenly():
    plan = ScanPlan(mode="scan")
    names = []
    t = 0.0
    for _ in range(10):
        name, freq, dwell = plan.next_dwell(t)
        names.append(name)
        plan.record_result(name, valid_count=0, now=t)
        t += dwell
    # No EOT activity -> both channels get meaningful share.
    assert names.count("HOT") >= 3


def test_hot_linger_on_hit():
    plan = ScanPlan(mode="scan")
    # Force a HOT dwell by exhausting EOT credits.
    plan.next_dwell(0.0)              # EOT (credits start 0 -> returns EOT path)
    name, freq, _ = plan.next_dwell(0.0)
    # Drive to a HOT dwell then report a HOT hit -> next must linger on HOT.
    # Walk until we get a HOT dwell:
    t = 0.0
    while True:
        name, freq, dwell = plan.next_dwell(t)
        if name == "HOT":
            break
        plan.record_result(name, 0, t)
        t += dwell
    plan.record_result("HOT", valid_count=1, now=t)
    assert plan.next_dwell(t)[0] == "HOT"
    assert freq == HOT_FREQ_HZ
