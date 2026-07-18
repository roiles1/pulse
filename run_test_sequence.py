#!/usr/bin/env python3
"""
Pi-side TX test sequence for the B206mini.

Reads tests.json and transmits each test case for tx_seconds (default 10 s)
with gap_seconds of silence between tests, in the exact order the HackRF
verifier on the Mac expects. Start hackrf_verify.py on the Mac FIRST, then
run this:

    sudo python3 run_test_sequence.py            # gain 40 (over-the-air, ~1 m)
    sudo python3 run_test_sequence.py --gain 20  # cabled through >=30 dB attenuator

Use --only NAME to transmit a single test case (the Mac side has a matching
--only flag).
"""
import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--gain", type=float, default=40.0, help="TX gain, dB (default 40)")
    p.add_argument("--tests", default=os.path.join(HERE, "tests.json"))
    p.add_argument("--only", default=None, help="run only the named test")
    p.add_argument("--countdown", type=float, default=5.0,
                   help="seconds before the first test starts")
    args = p.parse_args()

    with open(args.tests) as f:
        plan = json.load(f)
    tests = plan["tests"]
    if args.only:
        tests = [t for t in tests if t["name"] == args.only]
        if not tests:
            sys.exit(f"no test named {args.only!r} in {args.tests}")

    print(f"Sequence: {len(tests)} test(s), {plan['tx_seconds']:.0f} s each, "
          f"{plan['gap_seconds']:.0f} s gap, gain {args.gain:.0f} dB")
    print("Make sure hackrf_verify.py is already waiting on the Mac.")
    for s in range(int(args.countdown), 0, -1):
        print(f"  starting in {s}...", end="\r", flush=True)
        time.sleep(1)
    print()

    failures = []
    for i, t in enumerate(tests, 1):
        print(f"\n=== [{i}/{len(tests)}] {t['name']}: {t['desc']} ===")
        cmd = [sys.executable, os.path.join(HERE, "chirp_tx.py"),
               "--freq", str(plan["carrier"]),
               "--chirp-bw", str(t["chirp_bw"]),
               "--prf", str(t["prf"]),
               "--pulse-len", str(t["pulse_len"]),
               "--gain", str(args.gain),
               "--duration", str(plan["tx_seconds"])]
        r = subprocess.run(cmd)
        if r.returncode != 0:
            print(f"!!! {t['name']} exited with code {r.returncode}")
            failures.append(t["name"])
        if i < len(tests):
            print(f"--- {plan['gap_seconds']:.0f} s silence gap ---")
            time.sleep(plan["gap_seconds"])

    print("\n=== TX sequence done ===")
    if failures:
        print(f"TX-side failures: {failures}")
        sys.exit(1)
    print("All transmissions completed. Check the verifier output on the Mac.")


if __name__ == "__main__":
    main()
