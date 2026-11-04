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

Two TX modes (--mode):
  burst  (default) timed bursts. Only the pulses go over USB; each burst carries
         a timestamp and the FPGA releases it on its own clock, so pulse spacing
         is exact even if the host stalls. A host stall can only DROP a pulse
         (reported as "late"), never shift or chop one. At high PRF several
         PRIs are grouped per burst to keep the host under ~2500 sends/s.
  stream continuous zero-filled stream (the original mode). An underrun here
         drops and SHIFTS pulses and can chop them, which on a detector looks
         like missing pulses plus odd partial pulses in between.

PA gate (--gate, burst mode only):
  The SSPA needs a TTL gate that opens BEFORE the RF and wraps it. Each burst is
  padded with zeros: [lead zeros][RF pulse][tail zeros]. The FPGA's ATR drives a
  GPIO pin high while the radio is transmitting a burst, so the gate covers the
  whole padded burst and the RF sits inside it, `--gate-lead` after the gate
  edge. Timing is set by the FPGA clock (40 ns steps at 25 MS/s), not the host.
    gate length  = --gate-factor x pulse length
    RF starts      --gate-lead after the gate opens
    tail         = gate length - lead - pulse   (must be >= 0)
  B206mini GPIO: 12-pin J5 PicoBlade, GPIO_0 = pin 2, GND = pin 6, 3.3 V logic,
  max ~5 mA per pin -> buffer it if the SSPA gate input draws more.
  The gate is forced LOW at startup and on exit.

Underrun-avoidance measures (both modes):
  - large USB send-frame pool via device args
  - SCHED_FIFO real-time priority if permitted (run with sudo or chrt -f)
  - async message polling: underflows / late bursts counted and reported per second

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
    p.add_argument("--mode", choices=("burst", "stream"), default="burst",
                   help="burst = timed bursts, FPGA-timed pulse spacing (robust); "
                        "stream = continuous zero-filled stream (legacy)")
    p.add_argument("--max-burst-rate", type=float, default=2500.0,
                   help="burst mode: max host send() calls per second; above this "
                        "PRF, several PRIs are grouped into one burst")
    p.add_argument("--setup-check", action="store_true",
                   help="initialize device and streamer, then exit WITHOUT transmitting")
    p.add_argument("--extended", action="store_true",
                   help="lift the validated parameter ranges (duty <1%% / >10%%, wider "
                        "PRF/pulse); only physical limits remain (pulse fits PRI, BW <= rate)")
    g = p.add_argument_group("PA gate (burst mode)")
    g.add_argument("--gate", action="store_true",
                   help="drive a per-pulse PA gate on a USRP GPIO pin (ATR)")
    g.add_argument("--gate-pin", type=int, default=0,
                   help="GPIO index 0..7 (B206mini J5: GPIO_0 = pin 2)")
    g.add_argument("--gate-lead", type=float, default=2e-6,
                   help="synch delay: gate opens this long before the RF, s")
    g.add_argument("--gate-factor", type=float, default=1.5,
                   help="gate length / RF pulse length")
    g.add_argument("--gate-skew", type=float, default=0.0,
                   help="calibration: extra RF delay vs gate to cancel the fixed "
                        "ATR-to-DAC pipeline offset measured on a scope, s (+/-)")
    g.add_argument("--gate-min-off", type=float, default=1e-6,
                   help="minimum gate-low time between pulses, s")
    return p.parse_args()


def gate_plan(a, pulse_len, n_pulse, n_pri, rate):
    """Sample counts for [lead][pulse][tail]. Exits on an impossible gate."""
    if not 0 <= a.gate_pin <= 7:
        sys.exit(f"--gate-pin {a.gate_pin} outside 0..7")
    if a.gate_factor < 1.0:
        sys.exit(f"--gate-factor {a.gate_factor} < 1: the gate must be at least "
                 f"as long as the RF pulse")
    n_lead = int(round((a.gate_lead + a.gate_skew) * rate))
    n_gate = int(round(a.gate_factor * pulse_len * rate))
    if n_lead < 0:
        sys.exit("gate lead + skew is negative: RF would start before the gate")
    n_tail = n_gate - n_lead - n_pulse
    if n_tail < 0:
        need = (n_lead + n_pulse) / (n_pulse)
        sys.exit(f"gate too short: lead {n_lead/rate*1e6:.2f} us + pulse "
                 f"{n_pulse/rate*1e6:.2f} us > gate {n_gate/rate*1e6:.2f} us. "
                 f"Raise --gate-factor to >= {need:.2f} or lower --gate-lead.")
    n_off = n_pri - n_gate
    if n_off < a.gate_min_off * rate:
        sys.exit(f"gate {n_gate/rate*1e6:.2f} us leaves only {n_off/rate*1e6:.2f} us "
                 f"gate-low in the {n_pri/rate*1e6:.2f} us PRI "
                 f"(min {a.gate_min_off*1e6:.2f} us). Lower factor/PRF.")
    return n_lead, n_tail, n_gate


def gpio_bank(usrp):
    banks = list(usrp.get_gpio_banks(0))
    for b in ("FP0", "GPIO"):
        if b in banks:
            return b
    if not banks:
        sys.exit("this USRP reports no GPIO banks — cannot gate")
    return banks[0]


def gate_setup(usrp, pin):
    """Pin under ATR: high only while TX is active. Starts LOW."""
    bank, m = gpio_bank(usrp), 1 << pin
    usrp.set_gpio_attr(bank, "CTRL", 0, m)      # manual first ...
    usrp.set_gpio_attr(bank, "DDR", m, m)       # ... as output
    usrp.set_gpio_attr(bank, "OUT", 0, m)       # ... driven low
    usrp.set_gpio_attr(bank, "ATR_0X", 0, m)    # idle      -> low
    usrp.set_gpio_attr(bank, "ATR_RX", 0, m)    # RX only   -> low
    usrp.set_gpio_attr(bank, "ATR_TX", m, m)    # TX only   -> HIGH
    usrp.set_gpio_attr(bank, "ATR_XX", m, m)    # full dup  -> HIGH
    usrp.set_gpio_attr(bank, "CTRL", m, m)      # hand the pin to ATR
    print(f"[gate] bank {bank} GPIO_{pin} under ATR (high during TX bursts)")
    return bank


def gate_safe_low(usrp, bank, pin):
    try:
        m = 1 << pin
        usrp.set_gpio_attr(bank, "CTRL", 0, m)
        usrp.set_gpio_attr(bank, "OUT", 0, m)
        print(f"[gate] GPIO_{pin} forced LOW")
    except Exception as e:  # never mask the real exit reason
        print(f"[gate] WARNING: could not force gate low: {e}")


def validate_pulse_params(a):
    """Returns pulse_len (s). Errors on out-of-range inputs, warns on derived values.
    With --extended the validated ranges become warnings; only physical limits
    (pulse fits PRI, BW <= rate, checked elsewhere) still abort."""
    fail = (lambda msg: print(f"[extended] {msg}")) if a.extended else sys.exit
    if not (BW_MIN <= a.chirp_bw <= BW_MAX):
        fail(f"chirp BW {a.chirp_bw/1e6:.2f} MHz outside device range "
             f"{BW_MIN/1e6:.0f}..{BW_MAX/1e6:.0f} MHz (0 = CW pulse)")
    if not (PRF_MIN <= a.prf <= PRF_MAX):
        fail(f"PRF {a.prf} Hz outside device range {PRF_MIN:.0f}..{PRF_MAX:.0f} Hz")

    if a.duty is not None:
        if not (DUTY_MIN <= a.duty <= DUTY_MAX):
            fail(f"duty {a.duty} outside device range {DUTY_MIN}..{DUTY_MAX}")
        pulse_len = a.duty / a.prf
        if not (PULSE_MIN <= pulse_len <= PULSE_MAX):
            fail(f"duty {a.duty*100:.1f}% at PRF {a.prf:.0f} Hz needs a "
                 f"{pulse_len*1e6:.1f} us pulse — outside "
                 f"{PULSE_MIN*1e6}..{PULSE_MAX*1e6} us. Pick a different PRF/duty combo.")
    else:
        pulse_len = a.pulse_len
        if not (PULSE_MIN <= pulse_len <= PULSE_MAX):
            fail(f"pulse length {pulse_len*1e6:.2f} us outside device range "
                 f"{PULSE_MIN*1e6}..{PULSE_MAX*1e6} us")

    duty = pulse_len * a.prf
    if duty >= 1.0:
        sys.exit(f"duty {duty*100:.1f}% >= 100%: pulse ({pulse_len*1e6:.2f} us) "
                 f"does not fit the PRI at PRF {a.prf:.0f} Hz")
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
    return np.tile(pri, reps).reshape(1, -1), n_pulse, n_pri, pri


def make_burst(pri, n_pulse, n_pri, prf, max_burst_rate, gate=None):
    """Burst = M consecutive PRIs, the trailing zeros of the last PRI dropped.
    Returns (burst (1,N) array, M). M=1 means the burst is just the pulse.
    With gate=(n_lead, n_tail, _): one pulse per burst, zero-padded
    [lead][pulse][tail] so the ATR gate wraps the RF."""
    if gate:
        n_lead, n_tail = gate[0], gate[1]
        b = np.zeros(n_lead + n_pulse + n_tail, np.complex64)
        b[n_lead:n_lead + n_pulse] = pri[:n_pulse]
        return b.reshape(1, -1), 1
    m = max(1, int(np.ceil(prf / max_burst_rate)))
    reps = np.tile(pri, m)
    burst = reps[: (m - 1) * n_pri + n_pulse]
    return np.ascontiguousarray(burst).reshape(1, -1), m


def time_at_sample(n, rate):
    """Exact TimeSpec for absolute sample index n (avoids float accumulation)."""
    if float(rate).is_integer():
        r = int(rate)
        return uhd.types.TimeSpec(int(n // r), float(n % r) / rate)
    return uhd.types.TimeSpec(n / rate)


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

    buf, n_pulse, n_pri, pri = make_pulse_buffer(actual_rate, a.chirp_bw, pulse_len, a.prf, a.amp)
    actual_prf = actual_rate / n_pri
    actual_pl = n_pulse / actual_rate
    mod = ("CW pulse (no sweep)" if a.chirp_bw == 0
           else f"upchirp {a.chirp_bw/1e6:.1f} MHz "
                f"({(a.freq-a.chirp_bw/2)/1e6:.1f} -> {(a.freq+a.chirp_bw/2)/1e6:.1f} MHz)")
    print(f"[tx] PRF {actual_prf:.1f} Hz | pulse {actual_pl*1e6:.2f} us ({n_pulse} samp) "
          f"| duty {actual_prf*actual_pl*100:.2f}% | {mod} "
          f"| buffer {buf.shape[1]} samp ({buf.shape[1]/actual_rate*1e3:.1f} ms)")

    gate = None
    if a.gate:
        if a.mode != "burst":
            sys.exit("--gate needs --mode burst (stream mode never goes idle, "
                     "so the ATR gate would stay high)")
        if actual_prf > a.max_burst_rate:
            sys.exit(f"--gate needs one burst per pulse: PRF {actual_prf:.0f} Hz > "
                     f"--max-burst-rate {a.max_burst_rate:.0f}. Lower PRF or raise "
                     f"--max-burst-rate (host must keep up, else pulses drop as 'late').")
        n_lead, n_tail, n_gate = gate_plan(a, actual_pl, n_pulse, n_pri, actual_rate)
        bank = gate_setup(usrp, a.gate_pin)
        gate = (n_lead, n_tail, bank)
        pa_duty = n_gate / n_pri * 100
        print(f"[gate] gate {n_gate/actual_rate*1e6:.2f} us | RF at +"
              f"{n_lead/actual_rate*1e6:.2f} us | tail {n_tail/actual_rate*1e6:.2f} us "
              f"| gate duty {pa_duty:.2f}%")
        if pa_duty > DUTY_MAX * 100:
            print(f"[warn] gate (PA) duty {pa_duty:.2f}% exceeds {DUTY_MAX*100:.0f}% "
                  f"— check the SSPA duty limit")

    if a.setup_check:
        if gate:
            gate_safe_low(usrp, gate[2], a.gate_pin)
        print("[setup-check] device + streamer OK, exiting without transmitting.")
        return

    running = [True]
    def stop(*_):
        running[0] = False
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    if a.mode == "burst":
        try:
            run_burst(a, usrp, streamer, pri, n_pulse, n_pri, actual_rate, actual_prf,
                      actual_pl, running, gate)
        finally:
            if gate:
                time.sleep(0.3)          # let the last scheduled bursts drain
                gate_safe_low(usrp, gate[2], a.gate_pin)
    else:
        run_stream(a, streamer, buf, actual_rate, actual_prf, actual_pl, running)


def run_stream(a, streamer, buf, actual_rate, actual_prf, actual_pl, running):
    md = uhd.types.TXMetadata()
    md.start_of_burst = False
    md.end_of_burst = False
    md.has_time_spec = False

    amd = uhd.types.TXAsyncMetadata()
    underflows = 0
    other_events = 0

    print("[tx] mode=stream (continuous zero-filled)")
    print("[tx] transmitting... Ctrl-C to stop"
          + (f" (auto-stop after {a.duration:.0f} s)" if a.duration > 0 else ""))
    sent = 0
    t_start = time.monotonic()
    t_report = t_start
    while running[0]:
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
            running[0] = False

    # clean shutdown: send an end-of-burst so the DAC stops cleanly
    md.end_of_burst = True
    streamer.send(np.zeros((1, 0), np.complex64), md)
    print(f"RESULT mode=stream prf={a.prf:.0f}Hz pulse={actual_pl*1e6:.2f}us "
          f"duty={actual_prf*actual_pl*100:.2f}% sent={sent/actual_rate:.1f}s "
          f"underflows={underflows} other={other_events}")


def run_burst(a, usrp, streamer, pri, n_pulse, n_pri, actual_rate, actual_prf,
              actual_pl, running, gate=None):
    """Timed bursts: every burst is scheduled at an exact sample index on the
    device clock, so pulse spacing is set by the FPGA, not by the host."""
    burst, m = make_burst(pri, n_pulse, n_pri, actual_prf, a.max_burst_rate, gate)
    period = m * n_pri                       # samples between burst starts
    lead = int(0.25 * actual_rate)           # schedule this far ahead of "now"

    print(f"[tx] mode=burst | {m} PRI(s) per burst | burst {burst.shape[1]} samp "
          f"| {actual_rate/period:.1f} bursts/s | USB load "
          f"{burst.shape[1]/period*100:.1f}% of continuous")

    usrp.set_time_now(uhd.types.TimeSpec(0.0))
    time.sleep(0.05)
    t0 = int(round(usrp.get_time_now().get_real_secs() * actual_rate)) + lead

    md = uhd.types.TXMetadata()
    md.start_of_burst = True
    md.end_of_burst = True
    md.has_time_spec = True

    amd = uhd.types.TXAsyncMetadata()
    underflows = late = other_events = short_sends = 0
    late_since_resync = 0

    print("[tx] transmitting... Ctrl-C to stop"
          + (f" (auto-stop after {a.duration:.0f} s)" if a.duration > 0 else ""))
    j = 0
    t_start = time.monotonic()
    t_report = t_start
    while running[0]:
        md.time_spec = time_at_sample(t0 + j * period, actual_rate)
        n = streamer.send(burst, md, 1.0)
        if n != burst.shape[1]:
            short_sends += 1
        j += 1

        while streamer.recv_async_msg(amd, 0.0):
            ec = amd.event_code
            if ec == uhd.types.TXMetadataEventCode.time_error:
                late += 1
                late_since_resync += 1
            elif ec in (uhd.types.TXMetadataEventCode.underflow,
                        uhd.types.TXMetadataEventCode.underflow_in_packet):
                underflows += 1
            elif ec != uhd.types.TXMetadataEventCode.burst_ack:
                other_events += 1

        # host fell behind the device clock: skip ahead on the same PRI grid
        # (pulses stay phase-coherent with the original schedule)
        if late_since_resync:
            now_s = int(usrp.get_time_now().get_real_secs() * actual_rate)
            j = max(j, -(-(now_s + lead - t0) // period))
            late_since_resync = 0

        now = time.monotonic()
        if now - t_report >= 1.0:
            print(f"[tx] {j*period/actual_rate:8.1f} s sent | underflows: {underflows} "
                  f"| late: {late} | other events: {other_events + short_sends}")
            t_report = now
        if a.duration > 0 and now - t_start >= a.duration:
            running[0] = False

    print(f"RESULT mode=burst prf={a.prf:.0f}Hz pulse={actual_pl*1e6:.2f}us "
          f"duty={actual_prf*actual_pl*100:.2f}% sent={j*period/actual_rate:.1f}s "
          f"underflows={underflows} late={late} other={other_events + short_sends}"
          + (" gate=on" if gate else ""))

if __name__ == "__main__":
    main()
