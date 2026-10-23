#!/usr/bin/env python3
"""Hold the ESP32 BOOT line (GPIO0) low through the CH343 DTR output while the board is power-cycled.

On the WB-MGU v1.2 prototype the esptool auto-reset sequence does not get GPIO0 low in time,
so the ROM boots the application instead of entering the serial downloader. Holding DTR with
RTS released keeps Q7 pulling GPIO0 low; a power cycle while the line is held makes the chip
sample GPIO0 = 0 and stay in download mode. Flash afterwards with `esptool --before no-reset`.

Usage: usb_boot_hold.py <COM port> [seconds, default 120]
"""
import sys
import time

import serial


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    port = argv[1]
    hold_s = float(argv[2]) if len(argv) > 2 else 120.0

    s = serial.Serial()
    s.port = port
    s.baudrate = 115200
    s.timeout = 0.1
    s.dtr = True
    s.rts = True
    s.open()
    s.dtr = True        # DTR asserted, RTS released: GPIO0 low, EN untouched
    s.rts = False
    print(f"GPIO0 held low for {hold_s:.0f} s: power-cycle the board now (leave USB connected)", flush=True)

    deadline = time.time() + hold_s
    data = b""
    while time.time() < deadline:
        data += s.read(4096)
        if b"waiting for download" in data:
            print("ROM reports: waiting for download", flush=True)
            break
    s.close()
    if b"waiting for download" not in data:
        print("no download-mode banner seen; the chip may still have booted the app", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
