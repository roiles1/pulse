#!/usr/bin/env python3
"""
Technician GUI server for the B206mini LFM chirp transmitter + HackRF spectrum.

Serves tech_gui.html at http://localhost:8800 and drives the hardware:

    GET  /api/status                     -> TX state, params, underruns, log tail
    POST /api/flash    {params}          -> (re)start chirp_tx.py with new params
                                            ("Update & Re-flash" = same endpoint:
                                            stops the running TX, flashes again)
    POST /api/stop                       -> stop transmitting
    POST /api/probe                      -> quick USRP presence check
    GET  /api/spectrum?center_mhz=&lna=&vga=  -> one HackRF capture: spectrum dB
                                            bins + blind pulse measurement

Extended ranges: duty below 1% and above 10% are allowed (chirp_tx.py --extended);
only physical limits remain (pulse fits PRI, BW <= rate, duty < 100%).

Run from a terminal (auto-opens the browser):

    ~/radioconda/bin/python3 tech_gui.py

TX uses the isolated b206 conda env; the server itself needs numpy (radioconda).
Override interpreters with PULSE_B206_PY / PULSE_RC_PY if your setup differs.
"""
import atexit
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 8800
TX_LOG = os.path.join(tempfile.gettempdir(), "tech_gui_tx.log")
CAP_IQ = os.path.join(tempfile.gettempdir(), "tech_gui_cap.iq")


def _pick_python(env_var, *candidates):
    v = os.environ.get(env_var)
    if v:
        return v
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return shutil.which("python3") or sys.executable


B206_PY = _pick_python("PULSE_B206_PY",
                       os.path.expanduser("~/radioconda/envs/b206/bin/python3"))

# physical/sanity limits for extended mode (NOT the validated 1..10% test range)
LIMITS = {
    "freq_mhz": (70.0, 6000.0),      # B206mini tuning range
    "bw_mhz":   (0.0, 20.0),         # validated chirp BW cap
    "prf_hz":   (1.0, 333000.0),
    "duty_pct": (0.001, 95.0),       # extended: below 1% and above 10% allowed
    "gain":     (0.0, 89.75),
    "amp":      (0.05, 1.0),
}
TX_RATE = 25e6

state = {"tx": None, "params": None, "streaming": False}
hackrf_lock = threading.Lock()


# ---------------- TX process management ----------------
def _kill(proc):
    if proc and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def tail(path, nchars=600):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - nchars))
            return f.read().decode(errors="ignore").strip()
    except OSError:
        return ""


def _log_contains(path, needle):
    try:
        with open(path, "r", errors="ignore") as f:
            return needle in f.read()
    except OSError:
        return False


def stop_tx():
    # NEVER kill chirp_tx while UHD is writing FX3 firmware / FPGA — interrupting
    # the load wedges the device until it is physically replugged.
    proc = state["tx"]
    if proc and proc.poll() is None:
        deadline = time.time() + 25
        while time.time() < deadline:
            log = tail(TX_LOG, 4000)
            if (state["streaming"] or "transmitting" in log
                    or ("Loading firmware" not in log and "Loading FPGA" not in log)):
                break
            if proc.poll() is not None:
                break
            time.sleep(0.5)
    _kill(proc)
    state["tx"] = None
    state["params"] = None
    state["streaming"] = False
    try:
        subprocess.run(["pkill", "-f", r"python[0-9.]* .*chirp_tx\.py"],
                       capture_output=True)
    except FileNotFoundError:
        pass


def validate(p):
    out = {}
    for k, (lo, hi) in LIMITS.items():
        try:
            v = float(p[k])
        except (KeyError, TypeError, ValueError):
            return None, f"missing/bad parameter: {k}"
        eps = 1e-9 * max(1.0, abs(hi))
        if not (lo - eps <= v <= hi + eps):
            return None, f"{k}={v} outside allowed range {lo}..{hi}"
        out[k] = v
    pulse_us = out["duty_pct"] / 100.0 / out["prf_hz"] * 1e6
    if pulse_us * 1e-6 * TX_RATE < 2:
        return None, (f"pulse would be {pulse_us:.3f} us — under 2 samples at "
                      f"{TX_RATE/1e6:.0f} MS/s. Raise duty or lower PRF.")
    if out["bw_mhz"] * 1e6 > TX_RATE:
        return None, f"chirp BW {out['bw_mhz']} MHz exceeds TX rate {TX_RATE/1e6} MS/s"
    out["pulse_us"] = pulse_us
    return out, None


def flash_tx(p):
    """Stop any running TX and start chirp_tx.py with the new params.
    Serves both first flash and 'Update & Re-flash'."""
    params, err = validate(p)
    if err:
        return {"ok": False, "error": err}
    stop_tx()
    cmd = [B206_PY, "-u", os.path.join(HERE, "chirp_tx.py"),
           "--extended",
           "--freq", f"{params['freq_mhz']}e6",
           "--rate", str(TX_RATE),
           "--chirp-bw", f"{params['bw_mhz']}e6",
           "--prf", str(params["prf_hz"]),
           "--pulse-len", f"{params['pulse_us']}e-6",
           "--gain", str(params["gain"]),
           "--amp", str(params["amp"])]
    log = open(TX_LOG, "w")
    state["tx"] = subprocess.Popen(cmd, cwd=HERE, stdout=log, stderr=log)
    state["params"] = params
    # wait briefly; firmware/FPGA load after replug can take 30+ s -> "pending"
    for _ in range(24):
        time.sleep(0.25)
        if state["tx"].poll() is not None:
            state["params"] = None
            detail = tail(TX_LOG, 1200)
            state["tx"] = None
            return {"ok": False, "error": "transmitter exited during startup",
                    "detail": detail}
        if "transmitting" in tail(TX_LOG, 4000):
            state["streaming"] = True
            return {"ok": True}
    return {"ok": True, "pending": True,
            "note": "USRP initializing (loading firmware/FPGA) — up to ~30 s"}


def parse_tx_stats():
    """Pull seconds-sent and underflow count from chirp_tx's per-second lines."""
    log = tail(TX_LOG, 3000)
    sent = under = None
    for line in reversed(log.splitlines()):
        if line.startswith("[tx]") and "underflows:" in line:
            try:
                sent = float(line.split("s sent")[0].split("]")[1])
                under = int(line.split("underflows:")[1].split("|")[0])
            except (IndexError, ValueError):
                pass
            break
    return sent, under


def status():
    proc = state["tx"]
    alive = proc is not None and proc.poll() is None
    if proc is not None and not alive:
        detail = tail(TX_LOG, 1200)
        state["tx"] = None
        state["params"] = None
        state["streaming"] = False
        return {"tx": False, "starting": False, "params": None,
                "detail": "transmitter stopped: " + detail[-400:]}
    if alive and not state["streaming"] and _log_contains(TX_LOG, "transmitting"):
        state["streaming"] = True
    streaming = alive and state["streaming"]
    sent, under = parse_tx_stats() if streaming else (None, None)
    return {"tx": streaming, "starting": alive and not streaming,
            "params": state["params"], "sent_s": sent, "underflows": under,
            "detail": tail(TX_LOG, 400) if alive else ""}


def probe_usrp():
    find = os.path.join(os.path.dirname(B206_PY), "uhd_find_devices")
    if not os.path.exists(find):
        return {"ok": False, "error": f"uhd_find_devices not found near {B206_PY}"}
    try:
        r = subprocess.run([find], capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "uhd_find_devices timed out"}
    found = "B206" in r.stdout or "b200" in r.stdout
    serial = ""
    for line in r.stdout.splitlines():
        if "serial" in line:
            serial = line.strip()
            break
    return {"ok": True, "found": found, "serial": serial}


# ---------------- HackRF spectrum ----------------
def capture_spectrum(center_mhz, lna, vga, rate=20e6, sec=0.15):
    """One HackRF capture -> Welch spectrum (dB) + blind pulse measurement."""
    if shutil.which("hackrf_transfer") is None:
        return {"ok": False, "error": "hackrf_transfer not found (brew install hackrf)"}
    n = int(sec * rate)
    cmd = ["hackrf_transfer", "-r", CAP_IQ, "-f", str(int(center_mhz * 1e6)),
           "-s", str(int(rate)), "-b", str(int(rate)), "-n", str(n),
           "-l", str(int(lna)), "-g", str(int(vga)), "-a", "0"]
    r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if r.returncode != 0 or not os.path.exists(CAP_IQ) or os.path.getsize(CAP_IQ) < 1000:
        return {"ok": False,
                "error": "HackRF capture failed — is it plugged in / free?"}
    raw = np.fromfile(CAP_IQ, dtype=np.int8)
    raw = raw[: 2 * (len(raw) // 2)]
    x = raw[0::2].astype(np.float32) + 1j * raw[1::2].astype(np.float32)
    x /= 128.0
    x -= x.mean()

    # Welch: 4096-pt segments, Hann, averaged, -> 1024 display bins (max-decimate)
    NF = 4096
    nseg = max(1, len(x) // NF)
    acc = np.zeros(NF)
    win = np.hanning(NF).astype(np.float32)
    for i in range(nseg):
        seg = x[i * NF:(i + 1) * NF] * win
        acc += np.abs(np.fft.fftshift(np.fft.fft(seg))) ** 2
    psd_db = 10 * np.log10(acc / nseg + 1e-12)
    bins = psd_db.reshape(1024, NF // 1024).max(axis=1)  # keep narrow peaks visible
    clip = int(np.abs(raw).max())

    # blind pulse measurement (same approach as hackrf_verify)
    meas = None
    env = np.abs(x).astype(np.float64)
    w = 25
    c = np.cumsum(np.concatenate(([0.0], env)))
    sm = (c[w:] - c[:-w]) / w
    noise = max(float(np.median(sm)), 1e-4)
    peak = float(sm.max())
    snr_db = 20 * np.log10(peak / noise)
    if peak > 4 * noise:
        thr = max(4 * noise, 0.25 * peak)
        m = sm > thr
        d = np.diff(m.astype(np.int8))
        rises = np.flatnonzero(d == 1) + 1
        falls = np.flatnonzero(d == -1) + 1
        if len(falls) and (not len(rises) or falls[0] <= rises[0]):
            falls = falls[1:]
        k = min(len(rises), len(falls))
        rises, falls = rises[:k], falls[:k]
        if k > 1:
            keep = (rises[1:] - falls[:-1]) >= 100
            rises = rises[np.concatenate(([True], keep))]
            falls = falls[np.concatenate((keep, [True]))]
        ok = (falls - rises) >= 3
        rises, falls = rises[ok], falls[ok]
        if len(rises) >= 3:
            prf = rate / float(np.median(np.diff(rises)))
            width = float(np.median(falls - rises)) / rate
            meas = {"prf_hz": round(prf, 1), "width_us": round(width * 1e6, 2),
                    "duty_pct": round(prf * width * 100, 3), "n": int(len(rises))}
    return {"ok": True, "center_mhz": center_mhz, "rate": rate,
            "db": [round(float(v), 1) for v in bins],
            "clip": clip, "snr_db": round(snr_db, 1), "pulse": meas}


# ---------------- HTTP ----------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            try:
                with open(os.path.join(HERE, "tech_gui.html"), "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except OSError:
                self._json({"error": "tech_gui.html not found"}, 404)
        elif u.path == "/api/status":
            self._json(status())
        elif u.path == "/api/spectrum":
            q = parse_qs(u.query)
            try:
                center = float(q.get("center_mhz", ["1000"])[0])
                lna = int(q.get("lna", ["24"])[0])
                vga = int(q.get("vga", ["26"])[0])
            except ValueError:
                self._json({"ok": False, "error": "bad query params"}, 400)
                return
            lna = max(0, min(40, lna - lna % 8))
            vga = max(0, min(62, vga - vga % 2))
            if not (1.0 <= center <= 6000.0):
                self._json({"ok": False, "error": "center out of range"}, 400)
                return
            with hackrf_lock:
                self._json(capture_spectrum(center, lna, vga))
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            payload = {}
        if self.path == "/api/flash":
            self._json(flash_tx(payload))
        elif self.path == "/api/stop":
            stop_tx()
            self._json({"ok": True})
        elif self.path == "/api/probe":
            self._json(probe_usrp())
        else:
            self._json({"error": "not found"}, 404)


def cleanup(*_a):
    stop_tx()
    os._exit(0)


def main():
    atexit.register(stop_tx)
    for name in ("SIGINT", "SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, cleanup)
            except (ValueError, OSError):
                pass
    # 0.0.0.0: reachable from other machines on the bench LAN (e.g. server on
    # the Pi, browser on the Mac at http://<pi-ip>:8800)
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    url = f"http://localhost:{PORT}"
    print(f"[tech-gui] serving {url}  (Ctrl-C to quit)")
    try:
        import socket
        lan_ip = socket.gethostbyname(socket.gethostname())
        if not lan_ip.startswith("127."):
            print(f"[tech-gui] LAN access: http://{lan_ip}:{PORT}")
    except OSError:
        pass
    print(f"[tech-gui] TX python: {B206_PY}")
    print("[tech-gui] TX stops automatically when this window is closed.")
    # auto-open the browser only for interactive terminal launches
    if sys.stdout.isatty() and os.environ.get("TECH_GUI_NO_OPEN") != "1":
        if sys.platform == "darwin":
            subprocess.Popen(["open", url])
        elif shutil.which("xdg-open"):   # Linux desktop (Raspberry Pi)
            subprocess.Popen(["xdg-open", url],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    srv.serve_forever()


if __name__ == "__main__":
    main()
