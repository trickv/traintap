"""AAR S-9152 EOT/HOT framing, BCH validation, and field decode.

The protocol heart of traintap. Operates on bit *strings* ("0"/"1" chars) so the
BCH math can be a faithful port of the proven PyEOT/EOTDecode implementation
(Eric Reuter, 2018; russinnes, 2023 — GPL), which is what lets us validate real
over-the-air packets rather than only our own synthetic ones.

Frame on the wire (after the 1200-baud FFSK is sliced to bits):

    ... preamble (1010..) ... | SYNC (17b) | data block (45b) | BCH check (18b)

The data block field offsets below are relative to the start of the data block
(EOTDecode indexes them relative to a 74-bit buffer that includes 11 trailing
sync bits, i.e. offset = their index - 11). Several multi-bit fields are stored
LSB-first, so we reverse each slice before int() — exactly as the original does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, Optional

# --- Wire constants (verbatim from the AAR S-9152 reverse engineering) --------

SYNC = "10101011100010010"          # 17-bit frame sync word
GENERATOR = "1111001101000001111"   # BCH generator polynomial (19 bits)
CIPHER_KEY = "101011011101110000"   # XOR applied to computed checkbits (18 bits)

DATA_BLOCK_LEN = 45
CHECKBITS_LEN = 18                  # == len(GENERATOR) - 1
FRAME_LEN = DATA_BLOCK_LEN + CHECKBITS_LEN  # bits consumed after the sync word

# Data-block field offsets (start, stop) relative to the data block (0..44).
F_BATT_COND = (2, 4)
F_MESSAGE_TYPE = (4, 7)
F_UNIT_ADDR = (7, 24)
F_PRESSURE = (24, 31)
F_BATT_CHARGE = (31, 38)
F_SPARE = 38
F_VALVE_CKT = 39
F_CONF_IND = 40
F_TURBINE = 41
F_MOTION = 42
F_MKR_BATT = 43
F_MKR_LIGHT = 44

BATT_COND = {"11": "OK", "10": "Low", "01": "Very Low", "00": "Not Monitored"}


# --- BCH helpers (faithful port of EOTDecode helpers.py, GPL) -----------------

def xor(a: str, b: str) -> str:
    return "".join("0" if a[i] == b[i] else "1" for i in range(len(b)))


def reverse(data: str) -> str:
    return data[::-1]


def mod2div(dividend: str, divisor: str) -> str:
    """Modulo-2 (carryless) division; returns the remainder string."""
    pick = len(divisor)
    tmp = dividend[0:pick]
    while pick < len(dividend):
        if tmp[0] == "1":
            tmp = xor(divisor[1:], tmp[1:]) + dividend[pick]
        else:
            tmp = xor(("0" * pick)[1:], tmp[1:]) + dividend[pick]
        pick += 1
    if tmp[0] == "1":
        tmp = xor(divisor[1:], tmp[1:])
    else:
        tmp = xor(("0" * pick)[1:], tmp[1:])
    return tmp


def checkbits(data: str, key: str = GENERATOR) -> str:
    """Raw BCH remainder for `data` under generator `key` (pre-cipher)."""
    appended = data + "0" * (len(key) - 1)
    return mod2div(appended, key)


def compute_checkbits(data_block: str) -> str:
    """The 18 check bits that should accompany `data_block` on the wire.

    Mirrors EOTDecode: reverse the data block, take the BCH remainder, then XOR
    with the cipher key. encode/decode are exact inverses of this.
    """
    return xor(checkbits(reverse(data_block), GENERATOR), CIPHER_KEY)


# --- BCH error correction (syndrome decoding) --------------------------------
# The 63-bit codeword W = reverse(data_block) ++ (checkbits XOR CIPHER_KEY) is
# divisible by GENERATOR for a clean frame. We can therefore *correct* up to a
# few bit errors via classic syndrome decoding instead of only pass/fail. The
# generator gives an 18-bit syndrome; precomputed single- and double-error
# syndrome tables let us locate up to 3 errors in O(63). Default correction is
# capped (see DEFAULT_MAX_CORRECT) to bound false positives: a frame found only
# via exact sync + heavy correction could be a coincidence, so corrections are
# always recorded on the Packet (`corrected`) so callers can weigh them.

_GEN_INT = int(GENERATOR, 2)            # degree 18
DEFAULT_MAX_CORRECT = 2


def _polymod(w: int) -> int:
    for i in range(FRAME_LEN - 1, CHECKBITS_LEN - 1, -1):
        if (w >> i) & 1:
            w ^= _GEN_INT << (i - CHECKBITS_LEN)
    return w


_SYN = [_polymod(1 << p) for p in range(FRAME_LEN)]
_SYN_POS = {s: i for i, s in enumerate(_SYN)}
_SYN_PAIR: dict[int, tuple[int, int]] = {}
for _i in range(FRAME_LEN):
    for _j in range(_i + 1, FRAME_LEN):
        _SYN_PAIR.setdefault(_SYN[_i] ^ _SYN[_j], (_i, _j))


def _data_from_W(W: int) -> str:
    s = format(W, "0%db" % FRAME_LEN)
    return s[:DATA_BLOCK_LEN][::-1]


def correct_frame(data_block: str, checkbits_rx: str,
                  max_errors: int = DEFAULT_MAX_CORRECT):
    """Try to BCH-correct a frame. Return (corrected_data_block, n_errors) or
    (None, None) if the syndrome needs more than `max_errors` bit flips."""
    W = int(reverse(data_block) + xor(checkbits_rx, CIPHER_KEY), 2)
    s = _polymod(W)
    if s == 0:
        return data_block, 0
    if max_errors >= 1 and s in _SYN_POS:
        return _data_from_W(W ^ (1 << _SYN_POS[s])), 1
    if max_errors >= 2 and s in _SYN_PAIR:
        i, j = _SYN_PAIR[s]
        return _data_from_W(W ^ (1 << i) ^ (1 << j)), 2
    if max_errors >= 3:
        for p in range(FRAME_LEN):
            r = s ^ _SYN[p]
            if r in _SYN_PAIR:
                i, j = _SYN_PAIR[r]
                if p not in (i, j):
                    return _data_from_W(W ^ (1 << p) ^ (1 << i) ^ (1 << j)), 3
    return None, None


# --- Decoded packet -----------------------------------------------------------

@dataclass
class Packet:
    source: str                 # "EOT", "HOT", or "DPU"
    freq_hz: int
    valid: bool                 # BCH check passed (possibly after correction)
    data_block: str             # 45-bit data block (corrected if `corrected` > 0)
    checkbits_rx: str           # raw 18 received check bits
    corrected: int = 0          # number of BCH-corrected bit errors (0 = clean)
    unit_addr: Optional[int] = None
    pressure: Optional[int] = None          # psig
    batt_charge_pct: Optional[int] = None
    batt_cond: Optional[str] = None
    message_type: Optional[str] = None
    arm_status: Optional[str] = None
    motion: Optional[int] = None
    marker_light: Optional[int] = None
    turbine: Optional[int] = None
    valve_ckt: Optional[int] = None
    conf_ind: Optional[int] = None
    fields: dict = field(default_factory=dict)


def _rev_int(block: str, span: tuple[int, int]) -> int:
    return int(block[span[0]:span[1]][::-1], 2)


def decode_eot(data_block: str, checkbits_rx: str, freq_hz: int,
               max_correct: int = DEFAULT_MAX_CORRECT) -> Packet:
    """Decode a 45-bit EOT data block + 18 received check bits into a Packet.

    If the raw BCH check fails, attempt to correct up to `max_correct` bit errors
    (0 disables). Decoded fields come from the corrected data block; `corrected`
    records how many bits were flipped.
    """
    corrected = 0
    if compute_checkbits(data_block) == checkbits_rx:
        valid = True
    else:
        cdata, n = correct_frame(data_block, checkbits_rx, max_correct)
        if cdata is not None:
            data_block, corrected, valid = cdata, n, True
        else:
            valid = False

    batt_cond_bits = data_block[F_BATT_COND[0]:F_BATT_COND[1]][::-1]
    message_type = data_block[F_MESSAGE_TYPE[0]:F_MESSAGE_TYPE[1]]
    conf_ind = int(data_block[F_CONF_IND])

    if message_type == "111":
        arm_status = "Arming" if conf_ind == 0 else "Armed"
    else:
        arm_status = "Normal"

    return Packet(
        source="EOT",
        freq_hz=freq_hz,
        valid=valid,
        data_block=data_block,
        checkbits_rx=checkbits_rx,
        corrected=corrected,
        unit_addr=_rev_int(data_block, F_UNIT_ADDR),
        pressure=_rev_int(data_block, F_PRESSURE),
        batt_charge_pct=int(_rev_int(data_block, F_BATT_CHARGE) / 127 * 100),
        batt_cond=BATT_COND.get(batt_cond_bits, "?"),
        message_type=message_type,
        arm_status=arm_status,
        motion=int(data_block[F_MOTION]),
        marker_light=int(data_block[F_MKR_LIGHT]),
        turbine=int(data_block[F_TURBINE]),
        valve_ckt=int(data_block[F_VALVE_CKT]),
        conf_ind=conf_ind,
    )


def decode_hot(data_block: str, checkbits_rx: str, freq_hz: int,
               max_correct: int = DEFAULT_MAX_CORRECT) -> Packet:
    """Decode a Head-of-Train frame.

    HOT (loco->rear) uses the same 1200-baud FFSK, sync, 45+18 framing, and BCH,
    but its data-block field semantics (arm/comm-test/emergency commands) are not
    as well documented as EOT. We validate (with correction) and surface the
    message type + raw bits; richer field mapping is intentionally deferred until
    we have real HOT captures to characterize against. Provisional, but honest.
    """
    corrected = 0
    if compute_checkbits(data_block) == checkbits_rx:
        valid = True
    else:
        cdata, n = correct_frame(data_block, checkbits_rx, max_correct)
        if cdata is not None:
            data_block, corrected, valid = cdata, n, True
        else:
            valid = False
    return Packet(
        source="HOT",
        freq_hz=freq_hz,
        valid=valid,
        data_block=data_block,
        checkbits_rx=checkbits_rx,
        corrected=corrected,
        message_type=data_block[F_MESSAGE_TYPE[0]:F_MESSAGE_TYPE[1]],
        fields={"raw_data_block": data_block},
    )


# --- Frame search -------------------------------------------------------------

def decode_dpu(data_block: str, checkbits_rx: str, freq_hz: int,
               max_correct: int = DEFAULT_MAX_CORRECT) -> Packet:
    """Decode a Distributed-Power (DPU, 457.9250 MHz) frame.

    DPU is part of the same AAR S-9152 telemetry family as EOT (same 1200-baud
    FFSK, sync, 45+18 framing, and BCH), so we decode it with the EOT field
    layout and relabel the source. The unit address and frame validity are the
    reliable bits; the EOT-specific fields (pressure, marker, etc.) are
    PROVISIONAL for DPU pending a clean live capture to confirm the layout.
    """
    pkt = decode_eot(data_block, checkbits_rx, freq_hz, max_correct)
    pkt.source = "DPU"
    return pkt


_DECODERS = {"HOT": decode_hot, "DPU": decode_dpu, "EOT": decode_eot}


def find_frames(bits: str, freq_hz: int, source: str = "EOT",
                max_correct: int = DEFAULT_MAX_CORRECT) -> Iterator[Packet]:
    """Yield a Packet for every sync occurrence followed by a full frame.

    Scans `bits` for SYNC and, where enough bits follow, decodes the 45-bit data
    block + 18 check bits (BCH-correcting up to `max_correct` errors). Both valid
    and invalid packets are yielded; callers decide whether to keep invalid ones.
    """
    decoder = _DECODERS.get(source, decode_eot)
    start = 0
    while True:
        idx = bits.find(SYNC, start)
        if idx < 0:
            return
        data_start = idx + len(SYNC)
        if data_start + FRAME_LEN > len(bits):
            return
        data_block = bits[data_start:data_start + DATA_BLOCK_LEN]
        checkbits_rx = bits[data_start + DATA_BLOCK_LEN:data_start + FRAME_LEN]
        yield decoder(data_block, checkbits_rx, freq_hz, max_correct)
        # Advance past this sync; overlapping syncs are implausible.
        start = idx + 1


# --- Encoder (used by tests and the synthetic-signal generator) ---------------

def _set_rev(block: list[str], span: tuple[int, int], value: int) -> None:
    width = span[1] - span[0]
    bits = format(value, f"0{width}b")[::-1]  # store LSB-first
    block[span[0]:span[1]] = list(bits)


def encode_eot(
    *,
    unit_addr: int,
    pressure: int,
    batt_charge: int = 127,
    batt_cond: str = "OK",
    message_type: str = "000",
    motion: int = 0,
    marker_light: int = 1,
    turbine: int = 0,
    valve_ckt: int = 0,
    conf_ind: int = 0,
    with_sync: bool = True,
) -> str:
    """Build a valid on-the-wire EOT bitstring (exact inverse of decode_eot).

    `batt_charge` is the raw 0..127 field value. Returns SYNC+data+checkbits when
    `with_sync` is True (default), else just data+checkbits.
    """
    block = ["0"] * DATA_BLOCK_LEN
    # batt_cond is stored reversed of the lookup key
    cond_key = {v: k for k, v in BATT_COND.items()}[batt_cond]
    block[F_BATT_COND[0]:F_BATT_COND[1]] = list(cond_key[::-1])
    block[F_MESSAGE_TYPE[0]:F_MESSAGE_TYPE[1]] = list(message_type)
    _set_rev(block, F_UNIT_ADDR, unit_addr)
    _set_rev(block, F_PRESSURE, pressure)
    _set_rev(block, F_BATT_CHARGE, batt_charge)
    block[F_SPARE] = "0"
    block[F_VALVE_CKT] = str(valve_ckt)
    block[F_CONF_IND] = str(conf_ind)
    block[F_TURBINE] = str(turbine)
    block[F_MOTION] = str(motion)
    block[F_MKR_BATT] = "1"
    block[F_MKR_LIGHT] = str(marker_light)

    data_block = "".join(block)
    frame = data_block + compute_checkbits(data_block)
    return (SYNC + frame) if with_sync else frame
