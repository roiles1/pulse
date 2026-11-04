#!/bin/bash
# Linux/Pi launcher for the technician GUI (TechGUI.command is the Mac one).
# Uses system python3 for both server and TX unless PULSE_*_PY are set.
cd "$(dirname "$0")"
export PULSE_B206_PY="${PULSE_B206_PY:-$(command -v python3)}"
exec python3 -u tech_gui.py
