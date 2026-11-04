# CLAUDE.md — Pulse radar emulator, PA-gate bring-up on the Pi

You are running ON the Raspberry Pi 5 that the USRP B206mini is plugged into
(USB 3). You have a real shell and the real radio. Work on branch `pa-gating`.

## What this project is
- `chirp_tx.py` — pulsed LFM chirp transmitter (UHD/python). Default mode is
  timed bursts (`--mode burst`): only pulses go over USB, each burst carries a
  device-clock timestamp, so the FPGA sets pulse spacing.
- `tech_gui.py` + `tech_gui.html` — web GUI on port 8800 (`./run_gui.sh`).
- `hackrf_verify.py` / `tests.json` — RF-side PASS/FAIL with a HackRF.
- `pi_setup.sh` — one-shot install + checks (does NOT transmit).
- `gpio_find.py` — toggles a GPIO slowly in manual mode to find the wire (no RF).

## The new feature: PA gate for the client's SSPA
The SSPA needs a TTL gate that opens BEFORE the RF and wraps the RF pulse.
Implemented in `chirp_tx.py --gate`:
- each burst = `[lead zeros][RF pulse][tail zeros]`
- GPIO pin is put under ATR: high while the radio transmits a burst, low idle
- gate length = `--gate-factor` x pulse; RF starts `--gate-lead` (default 2 us)
  after the gate edge; `--gate-skew` = scope calibration of the fixed
  ATR->DAC pipeline offset; `--gate-pin` default 0
- refuses stream mode, PRF > `--max-burst-rate` (2500), gate shorter than
  lead+pulse, < 1 us gate-low per PRI
- gate forced LOW at startup and exit
- GUI: operator sees gate on/off + length factor; developer section has synch
  delay, skew, pin

Hardware facts (from NI B206mini-i spec): GPIO is the 12-pin J5 PicoBlade,
GPIO_0 = pin 2, GND = pin 6 (also 12), 3.3 V on pins 1/7. 3.3 V logic,
max ~5 mA per pin. The user is using the 12-pin GPIO cable / adapter board —
its labels may not match J5 pin numbers, so find the wire with `gpio_find.py`.
UHD bank name is expected to be `FP0` (code auto-detects from `get_gpio_banks`).

## Status — what is and is NOT verified
- Verified only against a MOCK USRP (in the cloud): burst timing, GPIO
  register sequence, error paths, gate-low on exit.
- NOT verified on hardware:
  1. that the real UHD python GPIO calls work on this B206mini
  2. that ATR drops the pin LOW between back-to-back timed bursts (main risk)
  3. the size of the fixed gate-vs-RF offset (needs a scope; user is buying one)
  4. SSPA gate input level: client spec says both 3.3 V and "TTL 5V" — open

## SAFETY RULES — do not break these
- No SSPA / amplifier is connected. Never ask the user to connect one, and
  never suggest it is safe to, until the gate timing has been checked on a scope.
- Keep TX gain at 0 (`--gain 0`) for every test unless the user explicitly
  asks otherwise. Output into a 50-ohm load or no antenna.
- Never connect the gate wire to the HackRF.
- Never kill chirp_tx while UHD is loading firmware/FPGA (wedges the device
  until replug) — wait for "transmitting" or let it exit.
- Report exact command outputs. If unsure whether something works, say so.

## Tasks, in order
1. `./pi_setup.sh` — fix whatever fails (apt packages, UHD images, udev rules,
   USB 3). Last line must be `[setup-check] device + streamer OK`. This step
   exercises the real GPIO calls; if they throw, fix them (check
   `usrp.get_gpio_banks(0)` and the pyuhd `set_gpio_attr` signature).
2. `python3 gpio_find.py` — tell the user GPIO_0 is toggling at 1 Hz so they
   find the wire with a multimeter (~3.3 V / 0 V). `--all` walks GPIO_0..7.
3. Gate pulsing test (multimeter, DC volts on the gate wire):
   ```
   python3 chirp_tx.py --freq 2000e6 --prf 2000 --pulse-len 5e-6 \
       --gate --gate-factor 10 --gain 0 --duration 30
   ```
   gate = 50 us every 500 us = 10 % -> expect ~0.33 V average.
   ~3.3 V = gate stuck high (ATR not dropping between bursts) -> task 5.
   0 V = not switching / wrong wire / wrong bank.
   Then `--gate-factor 4` (20 us / 500 us = 4 % -> ~0.13 V) to confirm the
   reading follows the factor. Check the log shows late=0.
4. RF unchanged with gate on: HackRF verification or the GUI spectrum —
   PRF / pulse width must match the no-gate run.
5. ONLY if task 3 shows the gate stuck high: implement a fallback in
   `chirp_tx.py` — keep the pin in manual mode and schedule edges with timed
   commands (`usrp.set_command_time(t)` + `set_gpio_attr(bank,"OUT",...)`,
   then `clear_command_time()`) aligned to each burst's time_spec
   (rise at burst start, fall at burst start + gate length). Watch the timed
   command queue depth: schedule only a little ahead of the TX bursts. Make it
   a flag (e.g. `--gate-method timed`), keep ATR as default, re-run task 3.
6. Commit to `pa-gating` with clear messages and push. Do not merge to main.

## Later (not now)
Client also wants a 7" touchscreen GUI with safety-bounded params
(freq 1990–2100 MHz, BW 0–20, gain 50–80, PW 0.3–50 us, duty 1–10 %,
PRF derived = duty/PW) and an integration API. Don't start this unless asked.
