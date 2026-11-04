#!/usr/bin/env python3
"""
Find the gate wire on the 12-pin GPIO cable / adapter board. NO RF is sent.

Toggles one GPIO slowly (default GPIO_0, 1 Hz) in manual mode so you can find
it with a multimeter, LED+resistor or scope: ~3.3 V high, 0 V low.
Ctrl-C leaves the pin LOW.

    python3 gpio_find.py            # GPIO_0
    python3 gpio_find.py --pin 3    # another pin
    python3 gpio_find.py --all      # walk GPIO_0..7, 3 s each, prints which is high
"""
import argparse
import time

import uhd

p = argparse.ArgumentParser()
p.add_argument("--pin", type=int, default=0)
p.add_argument("--hz", type=float, default=1.0)
p.add_argument("--all", action="store_true")
p.add_argument("--args", default="")
a = p.parse_args()

usrp = uhd.usrp.MultiUSRP(a.args)
banks = list(usrp.get_gpio_banks(0))
bank = "FP0" if "FP0" in banks else banks[0]
ALL = 0xFF
usrp.set_gpio_attr(bank, "CTRL", 0, ALL)   # manual
usrp.set_gpio_attr(bank, "DDR", ALL, ALL)  # outputs
usrp.set_gpio_attr(bank, "OUT", 0, ALL)    # all low
print(f"[gpio] bank {bank}, manual mode, all pins low")

try:
    if a.all:
        while True:
            for pin in range(8):
                usrp.set_gpio_attr(bank, "OUT", 1 << pin, ALL)
                print(f"  GPIO_{pin} HIGH (others low)")
                time.sleep(3)
    else:
        m, half = 1 << a.pin, 0.5 / a.hz
        print(f"[gpio] toggling GPIO_{a.pin} at {a.hz} Hz — Ctrl-C to stop")
        while True:
            usrp.set_gpio_attr(bank, "OUT", m, m); time.sleep(half)
            usrp.set_gpio_attr(bank, "OUT", 0, m); time.sleep(half)
except KeyboardInterrupt:
    pass
finally:
    usrp.set_gpio_attr(bank, "OUT", 0, ALL)
    print("[gpio] all pins LOW")
