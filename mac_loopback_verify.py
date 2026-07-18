#!/usr/bin/env python3
"""Single-machine loopback verification: B206mini TX and HackRF RX both on one Mac.

Runs all 8 tests from tests.json end to end without the Pi. For each test it
starts chirp_tx FIRST, waits for steady-state streaming (the "transmitting"
line), THEN starts hackrf_verify --only. Starting the verifier only once TX is
streaming avoids capturing during B206 device-init (LO leakage with no pulses),
which otherwise shows up as 'signal vanished during capture'.

Uses the isolated b206 conda env for TX (uhd) and radioconda's base python for
the verifier. Bench config baked in: TX gain 89 + amp 1.0, RX LNA 40 / VGA 44
(the loopback link on this bench runs ~30 dB below an antennas-at-1m setup).

    ~/radioconda/bin/python3 mac_loopback_verify.py

Result (2026-07-18): all 8/8 PASS, SNR ~17 dB per test."""
import json, os, subprocess, time

HERE = "/Users/roielesnik/Desktop/pulse-e"
VPY = os.path.expanduser("~/radioconda/bin/python3")
TPY = os.path.expanduser("~/radioconda/envs/b206/bin/python3")

with open(os.path.join(HERE, "tests.json")) as f:
    plan = json.load(f)

results = []
for t in plan["tests"]:
    name = t["name"]
    print(f"\n########## {name} ##########", flush=True)
    tlog = f"/tmp/t_{name}.log"
    tf = open(tlog, "w")
    tx = subprocess.Popen(
        [TPY, "-u", os.path.join(HERE, "chirp_tx.py"),
         "--freq", str(plan["carrier"]),
         "--chirp-bw", str(t["chirp_bw"]),
         "--prf", str(t["prf"]),
         "--pulse-len", str(t["pulse_len"]),
         "--gain", "89", "--amp", "1.0", "--duration", "30"],
        cwd=HERE, stdout=tf, stderr=subprocess.STDOUT)
    # wait for steady-state streaming
    t0 = time.time()
    streaming = False
    while time.time() - t0 < 40:
        with open(tlog) as fh:
            if "transmitting" in fh.read():
                streaming = True
                break
        time.sleep(0.5)
    if not streaming:
        print("TX never reached streaming state!", flush=True)
        tx.kill(); tf.close()
        results.append((name, False))
        continue
    time.sleep(1.5)  # let the pulse train stabilize
    v = subprocess.run(
        [VPY, "-u", os.path.join(HERE, "hackrf_verify.py"),
         "--only", name, "--lna", "40", "--vga", "44"],
        cwd=HERE, capture_output=True, text=True, timeout=120)
    tx.terminate()
    try:
        tx.wait(timeout=10)
    except subprocess.TimeoutExpired:
        tx.kill()
    tf.close()
    print(v.stdout, flush=True)
    ok = "ALL TESTS PASSED" in v.stdout
    results.append((name, ok))
    time.sleep(2)

print("\n########## FINAL ##########", flush=True)
for name, ok in results:
    print(f"  {name:20s} {'PASS' if ok else 'FAIL'}", flush=True)
print(f"{sum(o for _, o in results)}/8 tests passed", flush=True)
