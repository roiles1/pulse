#!/bin/zsh
# Technician GUI for the LFM pulse transmitter + HackRF spectrum.
# Double-click (or run from a terminal); opens http://localhost:8800
cd "$(dirname "$0")"
PY="$HOME/radioconda/bin/python3"
[ -x "$PY" ] || PY="$(command -v python3)"
exec "$PY" -u tech_gui.py
