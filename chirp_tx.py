#!/usr/bin/env python3
"""
Pulsed LFM chirp transmitter for USRP B200-series (B206mini) on Raspberry Pi.

Each pulse is an LFM UPCHIRP centered on the carrier: the sweep runs from
-BW/2 to +BW/2 around 1 GHz (e.g. BW=20 MHz sweeps 990 -> 1010 MHz).
BW=0 gives an unmodulated CW pulse at the carrier. The chirp fills each pulse;
the rest of the PRI is zero-filled and the stream runs continuously, so USB
load (and underrun behavior) is identical regardless of duty cycle.

Device parameter ranges (validated):
  chirp BW     0 .. 20 MHz  (0 = CW pulse)
  PRF          333 .. 2000 Hz
  pulse length 0.3 .. 50 us
  duty cycle   1 .. 10 %   (derived = PRF x pulse length; warned if outside)

Give --prf plus either --pulse-len or --duty (duty derives the pulse length).

Underrun-avoidance measures:
  - one large precomputed TX buffer (integer number of PRIs), resent in a loop
  - large USB send-frame pool via device args
  - SCHED_FIFO real-time priority if permitted (run with sudo or chrt -f)
  - async message polling: underflows counted and reported per second

Stop with Ctrl-C (or use --duration N to exit after N seconds).
"""
import argparse
import os
import signal
import sys
import time

import numpy as np
import uhd

BW_MIN, BW_MAX = 0.0, 20e6
PRF_MIN, PRF_MAX = 333.0, 2000.0
PULSE_MIN, PULSE_MAX = 0.3e-6, 50e-6
DUTY_MIN, DUTY_MAX = 0.01, 0.10


def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--freq", type=float, default=1e9, help="carrier (center) frequency, Hz")
    p.add_argument("--rate", type=float, default=25e6, help="TX sample rate, Hz")
    p.add_argument("--chirp-bw", type=float, default=20e6,
                   help="LFM upchirp sweep width, Hz, centered on carrier (0 = CW pulse, max 20e6)")
    p.add_argument("--prf", type=float, default=1000.0, help="pulse repetition frequency, Hz")
    p.add_argument("--pulse-len", type=float, default=30e-6, help="pulse length, s")
    p.add_argument("--duty", type=float, default=None,
                   help="duty cycle 0.01..0.10; if given, overrides --pulse-len")
    p.add_argument("--gain", type=float, default=60.0, help="TX gain, dB (B206mini: 0..89.75)")
    p.add_argument("--amp", type=float, default=0.7, help="baseband amplitude, 0..1")
    p.add_argument("--duration", type=float, default=0.0,
                   help="stop after this many seconds (0 = run until Ctrl-C)")
    p.add_argument("--args", type=str,
                   default="num_send_frames=256,master_clock_rate=50e6",
                   help="UHD device args")
    p.add_argument("--setup-check", action="store_true",
                   help="initialize device and streamer, then exit WITHOUT transmitting")
    return p.parse_args()


def validate_pulse_params(a):
    """Returns pulse_len (s). Errors on out-of-range inputs, warns on derived values."""
    if not (BW_MIN <= a.chirp_bw <= BW_MAX):
        sys.exit(f"chirp BW {a.chirp_bw/1e6:.2f} MHz outside device range "
                 f"{BW_MIN/1e6:.0f}..{BW_MAX/1e6:.0f} MHz (0 = CW pulse)")
    if not (PRF_MIN <= a.prf <= PRF_MAX):
        sys.exit(f"PRF {a.prf} Hz outside device range {PRF_MIN:.0f}..{PRF_MAX:.0f} Hz")

    if a.duty is not None:
        if not (DUTY_MIN <= a.duty <= DUTY_MAX):
            sys.exit(f"duty {a.duty} outside device range {DUTY_MIN}..{DUTY_MAX}")
        pulse_len = a.duty / a.prf
        if not (PULSE_MIN <= pulse_len <= PULSE_MAX):
            sys.exit(f"duty {a.duty*100:.1f}% at PRF {a.prf:.0f} Hz needs a "
                     f"{pulse_len*1e6:.1f} us pulse — outside "
                     f"{PULSE_MIN*1e6}..{PULSE_MAX*1e6} us. Pick a different PRF/duty combo.")
    else:
        pulse_len = a.pulse_len
        if not (PULSE_MIN <= pulse_len <= PULSE_MAX):
            sys.exit(f"pulse length {pulse_len*1e6:.2f} us outside device range "
                     f"{PULSE_MIN*1e6}..{PULSE_MAX*1e6} us")

    duty = pulse_len * a.prf
    if not (DUTY_MIN <= duty <= DUTY_MAX):
        print(f"[warn] derived duty cycle {duty*100:.3f}% is outside the 1..10% range "
              f"(pulse {pulse_len*1e6:.2f} us x PRF {a.prf:.0f} Hz)")
    return pulse_len


def make_pulse_buffer(rate, bw, pulse_len, prf, amp):
    """One full PRI (chirp pulse + zeros), tiled to a large buffer."""
    n_pri = int(round(rate / prf))
    n_pulse = max(2, int(round(rate * pulse_len)))
    if n_pulse > n_pri:
        sys.exit("pulse longer than PRI — reduce pulse length or PRF")

    # LFM upchirp: instantaneous freq sweeps -bw/2 -> +bw/2 across the pulse
    # (centered on the carrier). bw=0 collapses to phase=0, i.e. a CW pulse.
    t = np.arange(n_pulse) / rate
    dur = n_pulse / rate
    k = bw / dur                        # sweep rate, Hz/s (0 for CW)
    phase = 2 * np.pi * (-bw / 2 * t + 0.5 * k * t * t)
    pri = np.zeros(n_pri, np.complex64)
    pri[:n_pulse] = (amp * np.exp(1j * phase)).astype(np.complex64)

    # tile to >= 250k samples (~10 ms at 25 MS/s) so each send() call is large
    reps = max(1, int(np.ceil(250_000 / n_pri)))
    # shape (1, N): pyuhd send() expects channels x samples
    return np.tile(pri, reps).reshape(1, -1), n_pulse, n_pri


def try_realtime():
    try:
        os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(60))
        print("[rt] SCHED_FIFO priority 60 set")
    except (PermissionError, AttributeError):  # AttributeError: macOS has no sched_setscheduler
        print("[rt] WARNING: no permission for real-time priority.")
        print("     For best results run:  sudo python3 chirp_tx.py ...")


def main():
    a = parse_args()
    if a.chirp_bw > a.rate:
        sys.exit(f"chirp bandwidth ({a.chirp_bw/1e6} MHz) exceeds sample rate ({a.rate/1e6} MS/s)")
    pulse_len = validate_pulse_params(a)

    try_realtime()

    usrp = uhd.usrp.MultiUSRP(a.args)
    usrp.set_tx_rate(a.rate)
    usrp.set_tx_freq(uhd.types.TuneRequest(a.freq))
    usrp.set_tx_gain(a.gain)
    usrp.set_tx_bandwidth(a.rate)

    actual_rate = usrp.get_tx_rate()
    print(f"[usrp] {usrp.get_mboard_name()}  freq={usrp.get_tx_freq()/1e6:.3f} MHz  "
          f"rate={actual_rate/1e6:.3f} MS/s  gain={usrp.get_tx_gain():.1f} dB")
    if abs(actual_rate - a.rate) > 1.0:
        print(f"[usrp] WARNING: requested {a.rate/1e6} MS/s, got {actual_rate/1e6} MS/s")

    st_args = uhd.usrp.StreamArgs("fc32", "sc16")
    streamer = usrp.get_tx_stream(st_args)

    buf, n_pulse, n_pri = make_pulse_buffer(actual_rate, a.chirp_bw, pulse_len, a.prf, a.amp)
    actual_prf = actual_rate / n_pri
    actual_pl = n_pulse / actual_rate
    mod = ("CW pulse (no sweep)" if a.chirp_bw == 0
           else f"upchirp {a.chirp_bw/1e6:.1f} MHz "
                f"({(a.freq-a.chirp_bw/2)/1e6:.1f} -> {(a.freq+a.chirp_bw/2)/1e6:.1f} MHz)")
    print(f"[tx] PRF {actual_prf:.1f} Hz | pulse {actual_pl*1e6:.2f} us ({n_pulse} samp) "
          f"| duty {actual_prf*actual_pl*100:.2f}% | {mod} "
          f"| buffer {buf.shape[1]} samp ({buf.shape[1]/actual_rate*1e3:.1f} ms)")

    if a.setup_check:
        print("[setup-check] device + streamer OK, exiting without transmitting.")
        return

    md = uhd.types.TXMetadata()
    md.start_of_burst = False
    md.end_of_burst = False
    md.has_time_spec = False

    amd = uhd.types.TXAsyncMetadata()
    underflows = 0
    other_events = 0

    running = True
    def stop(*_):
        nonlocal running
        running = False
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    print("[tx] transmitting... Ctrl-C to stop"
          + (f" (auto-stop after {a.duration:.0f} s)" if a.duration > 0 else ""))
    sent = 0
    t_start = time.monotonic()
    t_report = t_start
    while running:
        sent += streamer.send(buf, md)
        while streamer.recv_async_msg(amd, 0.0):
            ec = amd.event_code
            if ec in (uhd.types.TXMetadataEventCode.underflow,
                      uhd.types.TXMetadataEventCode.underflow_in_packet):
                underflows += 1
            elif ec != uhd.types.TXMetadataEventCode.burst_ack:
                other_events += 1
        now = time.monotonic()
        if now - t_report >= 1.0:
            print(f"[tx] {sent/actual_rate:8.1f} s sent | underflows: {underflows} "
                  f"| other events: {other_events}")
            t_report = now
        if a.duration > 0 and now - t_start >= a.duration:
            running = False

    # clean shutdown: send an end-of-burst so the DAC stops cleanly
    md.end_of_burst = True
    streamer.send(np.zeros((1, 0), np.complex64), md)
    print(f"RESULT prf={a.prf:.0f}Hz pulse={actual_pl*1e6:.2f}us "
          f"duty={actual_prf*actual_pl*100:.2f}% sent={sent/actual_rate:.1f}s "
          f"underflows={underflows} other={other_events}")


if __name__ == "__main__":
    main()
