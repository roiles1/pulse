#!/usr/bin/env python3
"""
Mac-side HackRF verifier for the B206mini pulsed-LFM transmitter.

Run this on the Mac (HackRF plugged in) BEFORE starting run_test_sequence.py
on the Pi:

    python3 hackrf_verify.py

For each test in tests.json (same order as the Pi transmits), it:
  1. tunes the HackRF and waits for the signal to appear,
  2. captures ~2 s of IQ,
  3. blindly measures PRF, pulse width, duty cycle, chirp sweep rate/direction,
     swept bandwidth and carrier offset,
  4. checks each against the expected values, prints PASS/FAIL,
  5. waits for the inter-test silence gap, then moves to the next test.

Requirements on the Mac:
    brew install hackrf        (provides hackrf_transfer / hackrf_info)
    pip3 install numpy

Offline mode (no HackRF, analyze a recorded int8-interleaved IQ file):
    python3 hackrf_verify.py --analyze-file cap.iq --test chirp_full_bw

Sync note: the two sides never talk to each other — this script keys off
signal-present / signal-absent transitions, so start it first and it will
simply wait for the Pi.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------- tolerances ----------------
TOL_PRF_REL = 0.03          # 3 %
TOL_WIDTH_REL = 0.25        # 25 % ...
TOL_WIDTH_ABS = 0.25e-6     # ... or 0.25 us, whichever is larger
TOL_DUTY_REL = 0.30
TOL_BW_REL = 0.25
TOL_CARRIER_HZ = 150e3      # covers B206 (2 ppm) + HackRF (20 ppm) + estimator bias
CW_MAX_SWEEP_HZ = 1e6       # a "CW" pulse must sweep less than this
PRESENCE_FACTOR = 6.0       # peak must exceed this x noise floor to count as signal


# ---------------- DSP helpers ----------------
def moving_avg(x, w):
    if w <= 1:
        return x
    c = np.cumsum(np.concatenate(([0.0], x)))
    y = (c[w:] - c[:-w]) / w
    pl = (w - 1) // 2
    pr = w - 1 - pl
    return np.concatenate([np.full(pl, y[0]), y, np.full(pr, y[-1])])


def load_iq_int8(path, max_samples=None):
    count = -1 if max_samples is None else 2 * int(max_samples)
    raw = np.fromfile(path, dtype=np.int8, count=count)
    raw = raw[: 2 * (len(raw) // 2)]
    x = raw[0::2].astype(np.float32) + 1j * raw[1::2].astype(np.float32)
    x /= 128.0
    x -= x.mean()           # remove HackRF DC offset
    return x


def signal_present(x):
    env = np.abs(x)
    noise = max(float(np.median(env)), 1e-4)
    thr = PRESENCE_FACTOR * noise
    # require a cluster of hot samples, not a lone spike
    return int((env > thr).sum()) > 20, noise, float(env.max())


def find_pulses(mask, min_gap, min_w):
    d = np.diff(mask.astype(np.int8))
    rises = np.flatnonzero(d == 1) + 1
    falls = np.flatnonzero(d == -1) + 1
    # drop partial pulse at start/end so every rise has a matching fall
    if len(falls) and (not len(rises) or falls[0] <= rises[0]):
        falls = falls[1:]
    n = min(len(rises), len(falls))
    rises, falls = rises[:n], falls[:n]
    if n == 0:
        return rises, falls
    # merge pulses separated by less than min_gap (envelope ripple)
    if n > 1:
        keep = (rises[1:] - falls[:-1]) >= min_gap
        rises = rises[np.concatenate(([True], keep))]
        falls = falls[np.concatenate((keep, [True]))]
    ok = (falls - rises) >= min_w
    return rises[ok], falls[ok]


def analyze(x, fs, test):
    """Blind measurement of the pulse train. Returns dict or None if no signal."""
    exp_w_samp = max(2, int(round(test["pulse_len"] * fs)))
    smooth = max(1, min(exp_w_samp // 5, 200))
    smooth |= 1          # odd window -> symmetric kernel, no half-sample timing bias
    env = moving_avg(np.abs(x), smooth)
    noise = max(float(np.median(env)), 1e-4)
    peak = float(env.max())
    if peak < PRESENCE_FACTOR * noise:
        return None
    thr = max(4.0 * noise, 0.25 * peak)
    min_gap = max(4, int(0.25 * fs / test["prf"]))
    min_w = max(2, int(0.3 * exp_w_samp))
    rises, falls = find_pulses(env > thr, min_gap, min_w)
    if len(rises) < 3:
        return None

    res = {"n_pulses": int(len(rises)), "snr_db": 20 * np.log10(peak / noise)}
    res["prf"] = fs / float(np.median(np.diff(rises)))
    res["width"] = float(np.median(falls - rises)) / fs
    res["duty"] = res["prf"] * res["width"]

    # per-pulse instantaneous frequency: slope (sweep rate) + center
    ks, fcs = [], []
    for s, e in list(zip(rises, falls))[:60]:
        n = e - s
        a, b = s + int(0.2 * n), e - int(0.2 * n)   # central 60 %: avoids edge
        if b - a < 8:                               # transients + Nyquist wrap
            continue
        ph = np.unwrap(np.angle(x[a:b]))
        finst = np.gradient(ph) * fs / (2 * np.pi)
        t = np.arange(len(finst)) / fs
        slope, intercept = np.polyfit(t, finst, 1)
        ks.append(slope)
        # carrier = fitted f_inst evaluated at the pulse's ENERGY CENTROID:
        # immune to threshold/edge asymmetry, which matters on steep sweeps
        # (at 4 MHz/us a 45 ns center error is already a 180 kHz carrier error)
        w2 = np.abs(x[s:e]) ** 2
        t_c = (s + float((w2 * np.arange(len(w2))).sum() / w2.sum()) - a) / fs
        fcs.append(slope * t_c + intercept)
    if ks:
        res["k"] = float(np.median(ks))                    # Hz/s, >0 = upchirp
        res["f_center"] = float(np.median(fcs))            # Hz, baseband
        res["bw_meas"] = res["k"] * res["width"]           # swept Hz over pulse
    return res


def judge(res, test):
    """Compare measurements against expectations. Returns list of (check, ok, detail)."""
    exp_prf = test["prf"]
    exp_w = test["pulse_len"]
    exp_duty = exp_prf * exp_w
    exp_fc = -test.get("rx_offset", 0.0)   # signal position in RX baseband
    out = []
    for chk in test["checks"]:
        if chk == "prf":
            ok = abs(res["prf"] - exp_prf) / exp_prf < TOL_PRF_REL
            out.append((chk, ok, f"{res['prf']:.1f} Hz (expect {exp_prf:.0f})"))
        elif chk == "width":
            tol = max(TOL_WIDTH_REL * exp_w, TOL_WIDTH_ABS)
            ok = abs(res["width"] - exp_w) < tol
            out.append((chk, ok, f"{res['width']*1e6:.2f} us (expect {exp_w*1e6:.2f})"))
        elif chk == "duty":
            ok = abs(res["duty"] - exp_duty) / exp_duty < TOL_DUTY_REL
            out.append((chk, ok, f"{res['duty']*100:.2f} % (expect {exp_duty*100:.2f})"))
        elif chk == "bw":
            if "bw_meas" not in res:
                out.append((chk, False, "could not measure sweep (pulses too short?)"))
                continue
            up = res["k"] > 0
            ok = up and abs(res["bw_meas"] - test["chirp_bw"]) / test["chirp_bw"] < TOL_BW_REL
            out.append((chk, ok, f"{res['bw_meas']/1e6:.2f} MHz "
                                 f"{'up' if up else 'DOWN'}chirp "
                                 f"(expect {test['chirp_bw']/1e6:.0f} MHz up)"))
        elif chk == "cw":
            if "bw_meas" not in res:
                out.append((chk, False, "could not measure sweep"))
                continue
            ok = abs(res["bw_meas"]) < CW_MAX_SWEEP_HZ
            out.append((chk, ok, f"residual sweep {res['bw_meas']/1e3:.0f} kHz "
                                 f"(CW limit {CW_MAX_SWEEP_HZ/1e3:.0f} kHz)"))
        elif chk == "carrier":
            if "f_center" not in res:
                out.append((chk, False, "could not measure center"))
                continue
            ok = abs(res["f_center"] - exp_fc) < TOL_CARRIER_HZ
            out.append((chk, ok, f"offset {(res['f_center']-exp_fc)/1e3:+.1f} kHz "
                                 f"(tol +/-{TOL_CARRIER_HZ/1e3:.0f} kHz)"))
    return out


def best_match_hint(res, tests):
    """If a test fails, guess which test the capture actually looks like."""
    best, best_score = None, 1e9
    for t in tests:
        s = abs(res["prf"] - t["prf"]) / t["prf"] \
            + abs(res["width"] - t["pulse_len"]) / t["pulse_len"]
        if s < best_score:
            best, best_score = t["name"], s
    return best if best_score < 0.5 else None


# ---------------- HackRF capture ----------------
def capture(freq_hz, rate, nsamples, lna, vga, amp, path):
    cmd = ["hackrf_transfer", "-r", path, "-f", str(int(freq_hz)),
           "-s", str(int(rate)), "-b", str(int(rate)), "-n", str(int(nsamples)),
           "-l", str(int(lna)), "-g", str(int(vga)), "-a", "1" if amp else "0"]
    r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if r.returncode != 0 or not os.path.exists(path) or os.path.getsize(path) == 0:
        raise RuntimeError("hackrf_transfer failed — check `hackrf_info`, cabling, "
                           "and that no other program holds the HackRF")


def wait_for(state, a, rx_freq, tmp, timeout):
    """Poll 0.3 s captures until signal presence == state. Returns False on timeout."""
    t0 = time.monotonic()
    consecutive = 0
    need = 1 if state else 2      # silence must be seen twice (gap, not a lull)
    while time.monotonic() - t0 < timeout:
        capture(rx_freq, a.rate, int(0.3 * a.rate), a.lna, a.vga, a.amp, tmp)
        present, _, _ = signal_present(load_iq_int8(tmp))
        consecutive = consecutive + 1 if present == state else 0
        if consecutive >= need:
            return True
        time.sleep(0.2)
    return False


# ---------------- main ----------------
def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tests", default=os.path.join(HERE, "tests.json"))
    p.add_argument("--rate", type=float, default=20e6, help="HackRF sample rate")
    p.add_argument("--lna", type=int, default=24, help="HackRF LNA gain 0..40, step 8")
    p.add_argument("--vga", type=int, default=20, help="HackRF VGA gain 0..62, step 2")
    p.add_argument("--amp", action="store_true", help="enable HackRF RF amp (weak OTA only)")
    p.add_argument("--capture-sec", type=float, default=2.0)
    p.add_argument("--only", default=None, help="verify only the named test")
    p.add_argument("--analyze-file", default=None, help="offline: analyze this IQ file")
    p.add_argument("--test", default=None, help="offline: which test the file contains")
    args = p.parse_args()

    with open(args.tests) as f:
        plan = json.load(f)
    tests = plan["tests"]
    carrier = plan["carrier"]

    # ---- offline mode ----
    if args.analyze_file:
        if not args.test:
            sys.exit("--analyze-file needs --test NAME")
        test = next((t for t in tests if t["name"] == args.test), None)
        if test is None:
            sys.exit(f"no test named {args.test!r}")
        res = analyze(load_iq_int8(args.analyze_file), args.rate, test)
        if res is None:
            sys.exit("no signal found in file")
        report(test, res, tests)
        return

    # ---- live mode ----
    if shutil.which("hackrf_transfer") is None:
        sys.exit("hackrf_transfer not found — install with:  brew install hackrf")
    if args.only:
        tests = [t for t in tests if t["name"] == args.only]
        if not tests:
            sys.exit(f"no test named {args.only!r}")

    tmp = os.path.join(tempfile.gettempdir(), "hackrf_verify_cap.iq")
    results = []
    print(f"Verifier ready: {len(tests)} test(s), carrier {carrier/1e6:.0f} MHz, "
          f"rate {args.rate/1e6:.0f} MS/s, LNA {args.lna}, VGA {args.vga}")
    print("Start run_test_sequence.py on the Pi now.\n")

    for i, test in enumerate(tests):
        rx = carrier + test.get("rx_offset", 0.0)
        print(f"[{i+1}/{len(tests)}] {test['name']}: waiting for signal "
              f"(HackRF at {rx/1e6:.1f} MHz)...")
        if not wait_for(True, args, rx, tmp, timeout=240 if i == 0 else 90):
            print("  TIMEOUT waiting for signal — sequence out of sync, aborting.")
            results.append((test["name"], None))
            break
        capture(rx, args.rate, int(args.capture_sec * args.rate),
                args.lna, args.vga, args.amp, tmp)
        res = analyze(load_iq_int8(tmp), args.rate, test)
        if res is None:
            print("  signal vanished during capture — FAIL")
            results.append((test["name"], False))
        else:
            results.append((test["name"], report(test, res, tests)))
        if i < len(tests) - 1:
            if not wait_for(False, args, rx, tmp, timeout=60):
                print("  TIMEOUT waiting for silence gap — aborting.")
                break

    print("\n================ SUMMARY ================")
    all_ok = True
    for name, ok in results:
        status = "PASS" if ok else ("NO SIGNAL" if ok is None else "FAIL")
        all_ok &= bool(ok)
        print(f"  {name:20s} {status}")
    print("=========================================")
    print("ALL TESTS PASSED — pulsed LFM verified." if all_ok and results
          else "Some tests failed — see details above.")
    sys.exit(0 if (all_ok and results) else 1)


def report(test, res, all_tests):
    print(f"  captured {res['n_pulses']} pulses, SNR ~{res['snr_db']:.0f} dB")
    checks = judge(res, test)
    ok_all = all(ok for _, ok, _ in checks)
    for chk, ok, detail in checks:
        print(f"    {'PASS' if ok else 'FAIL':4s}  {chk:8s} {detail}")
    if not ok_all:
        hint = best_match_hint(res, all_tests)
        if hint and hint != test["name"]:
            print(f"    hint: capture looks like test '{hint}' — sequence may be out of sync")
    return ok_all


if __name__ == "__main__":
    main()
