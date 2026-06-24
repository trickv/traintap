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


# --- Decoded packet -----------------------------------------------------------

@dataclass
class Packet:
    source: str                 # "EOT" or "HOT"
    freq_hz: int
    valid: bool                 # BCH check passed
    data_block: str             # raw 45-bit data block
    checkbits_rx: str           # raw 18 received check bits
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


def decode_eot(data_block: str, checkbits_rx: str, freq_hz: int) -> Packet:
    """Decode a 45-bit EOT data block + 18 received check bits into a Packet."""
    valid = compute_checkbits(data_block) == checkbits_rx

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


def decode_hot(data_block: str, checkbits_rx: str, freq_hz: int) -> Packet:
    """Decode a Head-of-Train frame.

    HOT (loco->rear) uses the same 1200-baud FFSK, sync, and 45+18 framing, but
    its data-block field semantics (arm/comm-test/emergency commands) are not as
    well documented as EOT. We BCH-validate with the same routine and surface the
    message type + raw bits; richer field mapping is intentionally deferred until
    we have real HOT captures to characterize against (see plan: "empirically
    characterize"). Provisional, but honest about what we don't yet know.
    """
    valid = compute_checkbits(data_block) == checkbits_rx
    message_type = data_block[F_MESSAGE_TYPE[0]:F_MESSAGE_TYPE[1]]
    return Packet(
        source="HOT",
        freq_hz=freq_hz,
        valid=valid,
        data_block=data_block,
        checkbits_rx=checkbits_rx,
        message_type=message_type,
        fields={"raw_data_block": data_block},
    )


# --- Frame search -------------------------------------------------------------

def find_frames(bits: str, freq_hz: int, source: str = "EOT") -> Iterator[Packet]:
    """Yield a Packet for every sync occurrence followed by a full frame.

    Scans `bits` for SYNC and, where enough bits follow, decodes the 45-bit data
    block + 18 check bits. Both valid and invalid (failed-BCH) packets are
    yielded; callers decide whether to keep invalid ones.
    """
    decoder = decode_hot if source == "HOT" else decode_eot
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
        yield decoder(data_block, checkbits_rx, freq_hz)
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
