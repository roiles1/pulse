# Pulsed LFM Chirp — B206mini TX + HackRF Verification

RF front-end experiments for a tumor-detection medical device.
Transmitter: Ettus **USRP B206mini** on a **Raspberry Pi 5**.
Independent receive-side verification: **HackRF One** on a Mac.

## Waveform

Pulsed LFM **upchirp**, centered on the carrier: each pulse sweeps from
−BW/2 to +BW/2 around 1 GHz (BW = 20 MHz sweeps 990 → 1010 MHz).
BW = 0 gives an unmodulated **CW pulse**. Between pulses the stream is
zero-filled and runs continuously at 25 MS/s, so USB load — and underrun
behavior — is identical at any duty cycle.

Validated parameter ranges:

| Parameter | Range |
|---|---|
| Chirp bandwidth | 0 (CW) … 20 MHz |
| PRF | 333 … 2000 Hz |
| Pulse length | 0.3 … 50 µs |
| Duty cycle | 1 … 10 % (derived = PRF × pulse; out-of-range warns) |

Note: not every combination is physically possible — e.g. 10 % duty at
333 Hz would need a 300 µs pulse. The scripts reject infeasible combos
with a clear message.

## Files

| File | Runs on | Purpose |
|---|---|---|
| `chirp_tx.py` | Pi | The transmitter. `--prf`, `--pulse-len` or `--duty`, `--chirp-bw`, `--gain`, `--duration`; counts underruns per second |
| `run_test_sequence.py` | Pi | Transmits every test in `tests.json`, 10 s each, 6 s silence gaps |
| `hackrf_verify.py` | Mac | Captures each test with the HackRF and PASS/FAILs PRF, pulse width, duty, chirp BW + upchirp direction, carrier offset |
| `tests.json` | both | The shared test plan — **order matters**, both sides read the same file |
| `selftest_synthetic.py` | either | Hardware-free regression test of the analyzer DSP (synthesizes HackRF-format captures) |
| `usb_check.sh` | Pi | Verifies the USRP negotiated USB 3 (loads firmware first — see gotcha below) |

## Setup

### Pi (transmit side)
Already working on this Pi: UHD 4.10 + python3-uhd. Each boot:

```bash
echo performance | sudo tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor
./usb_check.sh        # must say SuperSpeed / 5000 Mbps
```

### Mac (verify side)

```bash
brew install hackrf
pip3 install numpy
hackrf_info           # must list the HackRF
python3 selftest_synthetic.py   # optional: verify the analyzer, no hardware needed
```

### RF connection — pick one

- **Over the air**: small antennas on both, 0.5–2 m apart, TX `--gain 40`
  (default). Do not use the HackRF `--amp` unless the signal is weak.
- **Cabled**: SMA through **at least 30 dB of attenuation**, TX `--gain 20`.
  The HackRF front end is damaged above ~+10 dBm — never cable directly.

1 GHz is licensed spectrum: keep it cabled/attenuated or very low power
indoors, per your local regulations.

## Running the verification

Order matters — the two sides sync by detecting signal/silence, not clocks:

```bash
# 1. Mac — start FIRST, it waits for the Pi:
python3 hackrf_verify.py

# 2. Pi:
sudo python3 run_test_sequence.py
```

~2.5 min total for the 8 tests, per-test detail plus a final summary
table on the Mac. Single test on both sides: `--only chirp_full_bw`.

You can also analyze any recorded int8-interleaved IQ capture offline:

```bash
python3 hackrf_verify.py --analyze-file cap.iq --test chirp_full_bw
```

## The 8 test cases

| # | Name | BW | PRF | Pulse | Duty | Checks |
|---|---|---|---|---|---|---|
| 1 | `cw_pulse` | 0 | 1 kHz | 30 µs | 3 % | no sweep, carrier |
| 2 | `chirp_full_bw` | 20 MHz | 1 kHz | 30 µs | 3 % | BW, upchirp |
| 3 | `chirp_mid_bw` | 10 MHz | 1 kHz | 30 µs | 3 % | BW, upchirp |
| 4 | `min_prf` | 20 MHz | 333 Hz | 30 µs | 1 % | PRF limit |
| 5 | `max_pulse_min_prf` | 20 MHz | 333 Hz | 50 µs | 1.7 % | pulse limit |
| 6 | `max_prf_min_duty` | 20 MHz | 2 kHz | 5 µs | 1 % | steepest sweep |
| 7 | `max_duty` | 20 MHz | 2 kHz | 50 µs | 10 % | heaviest case |
| 8 | `min_pulse` | 20 MHz | 2 kHz | 0.3 µs | 0.06 % | detection only¹ |

¹ 0.3 µs is ~6 samples at the HackRF's 20 MS/s — pulse presence, PRF and
width are checked, but the sweep can't be measured that short.

Tolerances (in `hackrf_verify.py`): PRF ±3 %, width ±25 % (min 0.25 µs),
duty ±30 %, BW ±25 % + sweep must be positive (upchirp), carrier within
±150 kHz (covers B206 2 ppm + HackRF 20 ppm oscillator error).

## Verification status

- **TX side**: all 8 cases transmitted from the B206mini with **0 underruns**
  each (10–60 s runs), even without RT priority and with the `ondemand`
  governor.
- **Analyzer DSP**: all 8 cases pass `selftest_synthetic.py` (synthetic
  captures with noise, DC offset, 15 kHz LO error, random pulse phase).
- **Live HackRF capture path** (`hackrf_transfer` invocation, gain choice,
  signal-detection sync): not yet exercised against real hardware — first
  live run may need `--lna/--vga` tuning. Start with defaults; if SNR
  printed per test is < 20 dB, raise gains or move antennas closer.

## Technician GUI (Mac)

Browser console for the LFM pulse block with **extended duty range**
(below 1% and above 10% — the validated 1–10% band is marked in the UI):

```bash
~/radioconda/bin/python3 tech_gui.py     # or double-click TechGUI.command
# -> http://localhost:8800
```

- **Flash & Transmit** starts `chirp_tx.py --extended`; **Update & Re-flash**
  stops the running TX and flashes again with the edited parameters.
- Live **HackRF spectrum** in the browser (canvas + peak hold), with a blind
  pulse measurement readout (PRF / width / duty / SNR) under the plot.
- TX status shows seconds sent + underflow count; UHD errors (e.g. USRP
  unplugged) surface directly in the page.

## Gotchas learned the hard way

- **USB 2 before firmware**: the B206mini's Cypress FX3 bootloader is
  USB 2-only. Any speed check made before UHD loads the firmware reads
  480 Mbps and looks like a fault. `usb_check.sh` handles this correctly;
  the authoritative sign is UHD printing `Operating over USB 3`.
- **pyuhd 4.10 `send()` wants a 2-D array** (channels × samples); a 1-D
  buffer fails with a confusing "channels (1) does not match dimensions (1)".
- **Scheduler jitter causes sporadic underruns** without `sudo` (RT
  priority) and the `performance` governor — and anything heavy running on
  the Pi (including Claude Code) makes it worse. For clean runs: both
  measures on, nothing else running.
- The CPU governor resets to `ondemand` on every reboot.
