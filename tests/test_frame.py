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


def test_single_bit_error_fails_raw_bch():
    bits = frame.encode_eot(unit_addr=999, pressure=42)
    # flip one bit inside the data block (after the 17-bit sync)
    flip = len(frame.SYNC) + 20
    corrupt = bits[:flip] + ("1" if bits[flip] == "0" else "0") + bits[flip + 1:]
    # with correction OFF, a single-bit error must fail the raw BCH check
    p = next(frame.find_frames(corrupt, 457_937_500, max_correct=0))
    assert not p.valid


def test_pressure_and_address_extremes():
    p = next(frame.find_frames(
        frame.encode_eot(unit_addr=(1 << 17) - 1, pressure=(1 << 7) - 1),
        457_937_500))
    assert p.valid
    assert p.unit_addr == (1 << 17) - 1
    assert p.pressure == 127


def _flip(bits, positions):
    b = list(bits)
    for p in positions:
        b[p] = "1" if b[p] == "0" else "0"
    return "".join(b)


def test_bch_corrects_up_to_3_errors():
    base = frame.encode_eot(unit_addr=45678, pressure=92, with_sync=False)
    data, par = base[:frame.DATA_BLOCK_LEN], base[frame.DATA_BLOCK_LEN:]
    assert frame.correct_frame(data, par, 3) == (data, 0)
    for t in (1, 2, 3):
        corrupt = _flip(base, range(0, t * 7, 7))  # spread t flips across the frame
        cd, par2 = corrupt[:frame.DATA_BLOCK_LEN], corrupt[frame.DATA_BLOCK_LEN:]
        rec, n = frame.correct_frame(cd, par2, 3)
        assert rec == data and n == t


def test_decode_eot_recovers_corrupted_frame():
    bits = frame.encode_eot(unit_addr=12345, pressure=78, motion=1)
    flip = len(frame.SYNC) + 20
    corrupt = _flip(bits, [flip])
    p = next(frame.find_frames(corrupt, 457_937_500, max_correct=2))
    assert p.valid and p.corrected == 1
    assert p.unit_addr == 12345 and p.pressure == 78 and p.motion == 1


def test_correction_disabled_and_overflow():
    bits = frame.encode_eot(unit_addr=999, pressure=42)
    one = _flip(bits, [len(frame.SYNC) + 20])
    # correction disabled -> invalid
    assert not next(frame.find_frames(one, 1, max_correct=0)).valid
    # too many errors -> uncorrectable
    many = _flip(bits, [len(frame.SYNC) + k for k in (1, 5, 9, 13, 17)])
    cd = many[len(frame.SYNC):len(frame.SYNC) + frame.DATA_BLOCK_LEN]
    cb = many[len(frame.SYNC) + frame.DATA_BLOCK_LEN:
              len(frame.SYNC) + frame.FRAME_LEN]
    assert frame.correct_frame(cd, cb, 3) == (None, None)


def test_compute_checkbits_length():
    bits = frame.encode_eot(unit_addr=1, pressure=1)
    data = bits[len(frame.SYNC):len(frame.SYNC) + frame.DATA_BLOCK_LEN]
    assert len(frame.compute_checkbits(data)) == frame.CHECKBITS_LEN
