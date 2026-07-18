#!/usr/bin/env python3
"""
Hardware-free regression test for hackrf_verify.py.

Synthesizes HackRF-format captures (int8 interleaved IQ) of the pulsed LFM
waveform — including receiver noise, HackRF DC offset, a 15 kHz frequency
error (~15 ppm at 1 GHz) and random per-pulse phase — and runs them through
the real analyzer. Every test in tests.json must PASS.

Run on either machine:  python3 selftest_synthetic.py
"""
import json
import os
import subprocess
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
FS = 20e6           # HackRF sample rate
DUR = 1.5           # seconds of synthetic capture
FREQ_ERR = 15e3     # simulated combined LO error, Hz
AMP = 60 / 128.0    # pulse amplitude in int8 fullscale units
NOISE = 2 / 128.0   # noise sigma
DC = 3 / 128.0      # simulated HackRF DC offset


def synth(test, rng):
    n = int(FS * DUR)
    x = (rng.standard_normal(n) + 1j * rng.standard_normal(n)).astype(np.complex64) * NOISE
    x += DC
    n_pri = int(round(FS / test["prf"]))
    n_pulse = max(2, int(round(FS * test["pulse_len"])))
    bw = test["chirp_bw"]
    t = np.arange(n_pulse) / FS
    dur = n_pulse / FS
    k = bw / dur if dur > 0 else 0.0
    base_phase = 2 * np.pi * (-bw / 2 * t + 0.5 * k * t * t)
    fc = -test.get("rx_offset", 0.0) + FREQ_ERR   # where the signal sits in RX baseband
    carrier_ph = 2 * np.pi * fc * t
    for start in range(0, n - n_pulse, n_pri):
        ph0 = rng.uniform(0, 2 * np.pi)
        x[start:start + n_pulse] += AMP * np.exp(1j * (base_phase + carrier_ph + ph0))
    iq = np.empty(2 * n, np.int8)
    iq[0::2] = np.clip(np.round(x.real * 128), -128, 127).astype(np.int8)
    iq[1::2] = np.clip(np.round(x.imag * 128), -128, 127).astype(np.int8)
    return iq


def main():
    with open(os.path.join(HERE, "tests.json")) as f:
        tests = json.load(f)["tests"]
    rng = np.random.default_rng(42)
    failures = []
    for test in tests:
        path = os.path.join(tempfile.gettempdir(), f"synth_{test['name']}.iq")
        synth(test, rng).tofile(path)
        r = subprocess.run([sys.executable, os.path.join(HERE, "hackrf_verify.py"),
                            "--analyze-file", path, "--test", test["name"]],
                           capture_output=True, text=True)
        ok = r.returncode == 0 and "FAIL" not in r.stdout
        print(f"{'PASS' if ok else 'FAIL'}  {test['name']}")
        if not ok:
            failures.append(test["name"])
            print(r.stdout)
            print(r.stderr)
        os.remove(path)
    if failures:
        sys.exit(f"self-test FAILED for: {failures}")
    print("\nAll synthetic self-tests passed — analyzer DSP is good.")


if __name__ == "__main__":
    main()
