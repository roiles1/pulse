#!/bin/bash
# Definitive USB-3 check for the USRP B206mini.
#
# The B206mini enumerates twice: the bare Cypress FX3 bootloader is USB 2-only
# (480 Mbps), and only after UHD loads the firmware does it re-enumerate as
# SuperSpeed. So we load the firmware FIRST, then read the negotiated speed.

find_dev() { grep -l 2500 /sys/bus/usb/devices/*/idVendor 2>/dev/null | head -1; }

d=$(find_dev)
if [ -z "$d" ]; then
    echo "❌ No USRP found on USB at all — check power/cable."
    exit 1
fi

echo "USRP found. Loading UHD firmware (this is required for a valid speed reading)..."
uhd_find_devices >/dev/null 2>&1
sleep 2

d=$(find_dev)
if [ -z "$d" ]; then
    echo "❌ Device vanished after firmware load — replug and rerun."
    exit 1
fi

dir=$(dirname "$d")
spd=$(cat "$dir/speed")
ver=$(cat "$dir/version" | tr -d ' ')

case "$spd" in
    5000|10000)
        echo "✅ USB $ver at ${spd} Mbps — SuperSpeed. 25 MS/s will work."
        ;;
    480)
        echo "❌ Still 480 Mbps AFTER firmware load — this is a real USB 2 fallback."
        echo "   Try: the other blue port, a different USB-3 cable, removing adapters."
        exit 1
        ;;
    *)
        echo "?  Unexpected speed: ${spd} Mbps"
        ;;
esac
