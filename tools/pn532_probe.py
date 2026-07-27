#!/usr/bin/env python3
"""Send PN532 GetFirmwareVersion over a serial/PTY device."""

from __future__ import annotations

import argparse
import time

import serial


GET_FIRMWARE_VERSION = bytes(10) + bytes.fromhex("0000ff02fed4022a00")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("port", help="For example /dev/ttys012")
    parser.add_argument("--timeout", type=float, default=2.0)
    args = parser.parse_args()

    with serial.Serial(args.port, 115200, timeout=args.timeout) as connection:
        connection.reset_input_buffer()
        connection.write(GET_FIRMWARE_VERSION)
        connection.flush()
        time.sleep(0.5)
        response = connection.read_all()

    print(f"RX: {response.hex()}")
    if bytes.fromhex("d50332") not in response:
        print("PN532 firmware response not found")
        return 1
    print("PN532 responded successfully")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

