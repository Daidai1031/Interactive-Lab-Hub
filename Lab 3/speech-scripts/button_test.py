#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
button_test.py — Minimal tester for Adafruit Mini PiTFT buttons on Raspberry Pi

Wiring / course defaults:
- TOP button  : BCM23 (pull-up, goes LOW when pressed)
- BOTTOM button: BCM24 (pull-up, goes LOW when pressed)
- Backlight    : BCM22 (not controlled here)

Usage (recommend):
  # If your course image starts a screen service, stop it first so GPIO is free
  sudo systemctl stop piscreen.service --now

  # Run in your venv
  source "/home/pi/Interactive-Lab-Hub/Lab 3/.venv/bin/activate"
  python "/home/pi/Interactive-Lab-Hub/Lab 3/speech-scripts/button_test.py"

Env overrides (optional):
  TOP_BUTTON_PIN=23 BOTTOM_BUTTON_PIN=24 DEBOUNCE_S=0.05 python button_test.py

Expected:
  Press the TOP/BOTTOM physical buttons → terminal prints events and (if espeak-ng is installed) speaks "top pressed" / "bottom pressed".

Troubleshooting:
- If you see "GPIO busy": some other process has the pins open. Stop services using the display/buttons (e.g., piscreen.service). Also avoid initializing TFT reset on GPIO24.
- If nothing prints: verify pins with `raspi-gpio get 22 23 24 25` and ensure you are using BCM numbering.
"""

import os
import sys
import time
import subprocess

try:
    from gpiozero import Button
except Exception as e:
    print("[ERR] gpiozero not available:", e)
    print("Install in your venv: pip install gpiozero")
    sys.exit(1)

TOP_PIN = int(os.getenv("TOP_BUTTON_PIN", "23"))
BOTTOM_PIN = int(os.getenv("BOTTOM_BUTTON_PIN", "24"))
DEBOUNCE_S = float(os.getenv("DEBOUNCE_S", "0.05"))


def say(text: str) -> None:
    """Speak a short phrase via espeak-ng if available (best-effort)."""
    try:
        subprocess.run([
            "espeak-ng", "-s", "170", text
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    except Exception:
        pass


def main() -> None:
    print("=== button_test.py ===")
    print(f"TOP pin: BCM{TOP_PIN}  BOTTOM pin: BCM{BOTTOM_PIN}")
    print("If you get 'GPIO busy', try: sudo systemctl stop piscreen.service --now")

    try:
        btn_top = Button(TOP_PIN, pull_up=True, bounce_time=DEBOUNCE_S)
        btn_bottom = Button(BOTTOM_PIN, pull_up=True, bounce_time=DEBOUNCE_S)
    except Exception as e:
        print("[ERR] Failed to init GPIO buttons:", e)
        print("Tips:\n - Ensure no other process (e.g., screen service) is using these pins.\n - Avoid assigning TFT reset to GPIO24 while testing buttons.\n - Double-check you're using BCM numbering.")
        sys.exit(2)

    def on_top() -> None:
        print("[BTN] TOP pressed", flush=True)
        say("top pressed")

    def on_bottom() -> None:
        print("[BTN] BOTTOM pressed", flush=True)
        say("bottom pressed")

    btn_top.when_pressed = on_top
    btn_bottom.when_pressed = on_bottom

    print("Waiting for button presses... (Ctrl+C to exit)")
    try:
        while True:
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("\n[EXIT] bye")


if __name__ == "__main__":
    main()
