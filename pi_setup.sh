#!/bin/bash
# One-shot setup for the Raspberry Pi 5 + USRP B206mini.
# Safe to re-run. Does NOT transmit: the final check runs --setup-check only.
#
#   curl -fsSL https://raw.githubusercontent.com/roiles1/pulse/pa-gating/pi_setup.sh | bash
#   (or: cd ~/pulse && ./pi_setup.sh)
set -e
BRANCH="${PULSE_BRANCH:-pa-gating}"
DIR="${PULSE_DIR:-$HOME/pulse}"

echo "== 1/6 packages"
need=()
for p in git uhd-host python3-uhd python3-numpy; do
    dpkg -s "$p" >/dev/null 2>&1 || need+=("$p")
done
if [ ${#need[@]} -gt 0 ]; then
    sudo apt-get update && sudo apt-get install -y "${need[@]}"
else
    echo "all present"
fi
python3 -c "import uhd, numpy; print('python uhd', uhd.get_version_string())"

echo "== 2/6 UHD FPGA/firmware images"
if ! ls /usr/share/uhd/images/usrp_b206mini* /usr/share/uhd/*/images/usrp_b206mini* >/dev/null 2>&1; then
    sudo uhd_images_downloader -t b2xx || sudo uhd_images_downloader
else
    echo "b206mini images present"
fi

echo "== 3/6 USB permissions (udev)"
if ! ls /etc/udev/rules.d/*uhd* /lib/udev/rules.d/*uhd* >/dev/null 2>&1; then
    r=$(ls /usr/lib/uhd/utils/uhd-usrp.rules /usr/libexec/uhd/utils/uhd-usrp.rules 2>/dev/null | head -1)
    if [ -n "$r" ]; then
        sudo cp "$r" /etc/udev/rules.d/ && sudo udevadm control --reload-rules && sudo udevadm trigger
        echo "installed $r — replug the USRP once"
    else
        echo "WARNING: no uhd udev rules found; run chirp_tx with sudo"
    fi
else
    echo "udev rules present"
fi

echo "== 4/6 repo ($BRANCH)"
if [ -d "$DIR/.git" ]; then
    git -C "$DIR" fetch origin "$BRANCH"
    git -C "$DIR" checkout "$BRANCH"
    git -C "$DIR" pull --ff-only origin "$BRANCH"
else
    git clone -b "$BRANCH" https://github.com/roiles1/pulse "$DIR"
fi
cd "$DIR"
chmod +x usb_check.sh pi_setup.sh run_gui.sh gpio_find.py

echo "== 5/6 CPU governor + USB 3"
echo performance | sudo tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor >/dev/null
echo "governor: $(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor)"
command -v vcgencmd >/dev/null && echo "throttled: $(vcgencmd get_throttled)  temp: $(vcgencmd measure_temp)"
./usb_check.sh || echo "!! fix USB before transmitting"

echo "== 6/6 device + gate check (no RF is transmitted)"
python3 -u chirp_tx.py --freq 2000e6 --prf 2000 --pulse-len 5e-6 \
    --gate --gate-factor 1.5 --gate-lead 2e-6 --setup-check

echo
echo "Done. Start the GUI with:  $DIR/run_gui.sh   -> http://<pi-ip>:8800"
