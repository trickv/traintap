"""Protocol tests: framing, BCH validation, field round-trip."""

from traintap import frame


def test_roundtrip_recovers_fields():
    bits = frame.encode_eot(unit_addr=12345, pressure=78, batt_charge=120,
                            batt_cond="OK", motion=1, marker_light=1, turbine=0)
    pkts = list(frame.find_frames("1010" + bits + "0000", freq_hz=457_937_500))
    assert len(pkts) == 1
    p = pkts[0]
    assert p.valid
    assert p.unit_addr == 12345
    assert p.pressure == 78
    assert p.motion == 1
    assert p.batt_cond == "OK"


def test_batt_cond_values():
    for cond in ("OK", "Low", "Very Low", "Not Monitored"):
        bits = frame.encode_eot(unit_addr=1, pressure=0, batt_cond=cond)
        p = next(frame.find_frames(bits, 457_937_500))
        assert p.batt_cond == cond


def test_arm_status_from_message_type():
    arming = next(frame.find_frames(
        frame.encode_eot(unit_addr=7, pressure=0, message_type="111", conf_ind=0),
        457_937_500))
    armed = next(frame.find_frames(
        frame.encode_eot(unit_addr=7, pressure=0, message_type="111", conf_ind=1),
        457_937_500))
    assert arming.arm_status == "Arming"
    assert armed.arm_status == "Armed"


def test_single_bit_error_fails_bch():
    bits = frame.encode_eot(unit_addr=999, pressure=42)
    # flip one bit inside the data block (after the 17-bit sync)
    flip = len(frame.SYNC) + 20
    corrupt = bits[:flip] + ("1" if bits[flip] == "0" else "0") + bits[flip + 1:]
    p = next(frame.find_frames(corrupt, 457_937_500))
    assert not p.valid


def test_pressure_and_address_extremes():
    p = next(frame.find_frames(
        frame.encode_eot(unit_addr=(1 << 17) - 1, pressure=(1 << 7) - 1),
        457_937_500))
    assert p.valid
    assert p.unit_addr == (1 << 17) - 1
    assert p.pressure == 127


def test_compute_checkbits_length():
    bits = frame.encode_eot(unit_addr=1, pressure=1)
    data = bits[len(frame.SYNC):len(frame.SYNC) + frame.DATA_BLOCK_LEN]
    assert len(frame.compute_checkbits(data)) == frame.CHECKBITS_LEN
