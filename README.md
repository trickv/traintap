# traintap

Capture and decode **End-of-Train (EOT)** and **Head-of-Train (HOT)** telemetry
from passing freight trains with an RTL-SDR — a single self-contained Python CLI.

EOT devices (the "FRED" on the last car) transmit status — unit address, brake
line pressure, motion, marker light, battery — to the locomotive on the AAR
S-9152 wireless link. traintap tunes the radio, demodulates the signal natively
from I/Q, validates every packet against its built-in BCH check code, and logs
the decoded telemetry to your console and a CSV.

## How it works

```
RTL-SDR I/Q ─► NCO mix (offset tune) ─► decimate to 48 kHz + channel filter
            ─► FM demod ─► 1200/1800 Hz AFSK matched filters ─► DPLL @ 1200 baud
            ─► sync-word search ─► 45-bit data block + 18-bit BCH check ─► fields
```

No `rtl_fm`, `sox`, or patched `minimodem` — the whole chain is numpy/scipy in
one process. Only BCH-valid packets are reported, so output is trustworthy.

### Signal facts (North America)

| | Frequency | Direction | Traffic |
|---|---|---|---|
| **EOT** | 457.9375 MHz | rear → loco | frequent telemetry (the main prize) |
| **HOT** | 452.9375 MHz | loco → rear | infrequent commands (arm, comm-test, emergency) |
| **DPU** | 457.9250 MHz | distributed power | mid-train loco telemetry (decoded alongside EOT) |

Because DPU is only 12.5 kHz from EOT, it falls inside the same capture — traintap
demodulates it from the same I/Q at a second offset (no retuning), so you get DPU
for free whenever listening for EOT. (Disable with `--no-dpu`.) DPU shares EOT's
AAR framing/BCH; its non-address field semantics are provisional pending a clean
live capture.

### Train passes (EOT ↔ DPU correlation)

Packets heard within `--pass-gap` seconds of each other are the same train going
by. `--passes-csv` logs one row per pass — start/end, and the EOT unit ID(s)
alongside any DPU (and HOT) units heard together — so a mid-train distributed-
power unit is tied to the EOT that identifies the train. The console prints a
`== PASS hh:mm:ss-hh:mm:ss  EOT 69686x4  DPU 12345x2` line as each pass closes.

Modulation is 1200-baud FFSK in an ~8 kHz NFM channel (mark 1200 Hz, space
1800 Hz after FM demod).

### Why scanning (and its limits)

EOT and HOT are **5.0 MHz apart**, which exceeds a single RTL-SDR's usable
bandwidth (~2.4 MHz) — so **one dongle cannot watch both at once** (true with any
software, including KA9Q-radio). traintap **time-shares**: it dwells mostly on
EOT and dips to HOT periodically. It favors EOT while a train is active and
lingers on HOT briefly when a HOT packet decodes. Catching *all* HOT requires a
second receiver (a 2nd RTL-SDR on 452.9375, or a wide SDR + KA9Q-radio).

## Install

System dependency (the native RTL-SDR library — not pip):

```sh
sudo apt install rtl-sdr librtlsdr-dev
# free the dongle from the DVB kernel driver:
echo 'blacklist dvb_usb_rtl28xxu' | sudo tee /etc/modprobe.d/blacklist-rtl.conf
# then replug the dongle (or: sudo modprobe -r dvb_usb_rtl28xxu)
```

### udev rules (use the dongle without `sudo`)

The `rtl-sdr` package usually installs these at `/etc/udev/rules.d/rtl-sdr.rules`.
If missing (or you built from source), add them:

```sh
sudo tee /etc/udev/rules.d/20-rtlsdr.rules >/dev/null <<'EOF'
# RTL-SDR (Realtek RTL2832U) — grant access to the plugdev group
SUBSYSTEM=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2832", GROUP="plugdev", MODE="0666", SYMLINK+="rtl_sdr"
SUBSYSTEM=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2838", GROUP="plugdev", MODE="0666", SYMLINK+="rtl_sdr"
EOF
sudo udevadm control --reload-rules && sudo udevadm trigger
# then unplug and replug the dongle
```

Find your dongle's `idVendor:idProduct` with `lsusb` (look for "Realtek") if it
differs. Confirm membership in `plugdev` (`groups | grep plugdev`; add with
`sudo usermod -aG plugdev $USER` then re-login). After replugging, `rtl_test -t`
should run as a normal user with no "usb_claim_interface error -6".

Python package:

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -e .            # installs numpy, scipy, pyrtlsdr + the `traintap` command
```

Verify the radio: `rtl_test -t`, then `traintap --list-devices`.

## Usage

```sh
# Scan EOT+HOT, log to CSV, suppress repeat console lines within 30 s per unit:
traintap --mode scan --csv trains.csv

# EOT only (simplest, never misses the frequent telemetry):
traintap --mode eot

# Tuning aids:
traintap --gain 40 --ppm 12         # fixed gain (dB) and freq correction

# No hardware needed — prove the DSP+decoder work:
traintap --selftest
```

Console output (one line per packet, deduped):

```
14:02:11 EOT 457.9375  unit 45678    92 psig  batt OK/94%  [MOVING light]
```

### Capture & replay (for testing / tuning without a live train)

```sh
traintap --record pass.npz --record-seconds 20   # save 20 s of I/Q on EOT
traintap --replay pass.npz --csv pass.csv        # decode it offline
```

Record real I/Q while a train passes, then replay it as a regression fixture.

## Key options

| Flag | Meaning |
|---|---|
| `--mode {scan,eot,hot}` | channel strategy (default `scan`) |
| `--eot-dwell` / `--hot-dwell` | seconds per dwell while scanning |
| `--gain` | `auto` or gain in dB |
| `--ppm` | crystal frequency correction |
| `--offset` | tuning offset below channel to dodge the DC spike (default 250 kHz) |
| `--csv FILE` | append decoded packets (CSV, header on create) |
| `--bch-correct N` | BCH-correct up to N bit errors/frame (`0` = off; default 2) |
| `--passes-csv FILE` | log one row per train pass (EOT ID ↔ DPU/HOT units) |
| `--pass-gap S` | silence (s) that ends a train pass (default 90) |
| `--dedupe S` | suppress repeat console lines per unit for S seconds (`0` = off) |
| `--stats-interval S` | live summary cadence (`0` = off) |
| `--keep-invalid` | also show BCH-failed packets (debugging) |

## Status & roadmap

- **EOT** decoding is complete and BCH-validated, with syndrome-based error
  correction (up to 3 bit errors; `--bch-correct`, default 2). Corrected packets
  are flagged with their correction count in the console (`~Nb`) and CSV.
- **HOT** shares the front end and BCH check; its command-message field semantics
  are provisional pending real captures (`--record` to help characterize).
- **DPU** (457.9250) is decoded from the same EOT capture; field semantics beyond
  the unit address are provisional pending a clean live DPU capture to confirm.
- Future: simultaneous EOT+HOT via a second receiver.

## Credits

The BCH check algorithm and EOT field layout are ported from the GPL
[EOTDecode](https://github.com/russinnes/EOTDecode) / PyEOT (Eric Reuter, 2018).
traintap replaces the external `rtl_fm | sox | minimodem` chain with an
in-process native-I/Q front end. Receive-only by design.
