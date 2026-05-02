"""
Manual output test.

Steps (press Enter to advance and capture a measurement at each step):
  1. V1 = 1 V, V2 = 0 V
  2. V1 = 4 V, V2 = 0 V
  3. V1 = 0 V, V2 = 1 V
  4. V1 = 0 V, V2 = 4 V

All measurements are printed to the console after each step and saved to a CSV.
"""

from __future__ import annotations

import csv
import sys
import time

import serial
import serial.tools.list_ports

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
PORT = None       # Set to e.g. "COM3" to skip auto-detection
BAUD = 115200
TIMEOUT = 2.0     # seconds
SETTLE_S = 0.15   # seconds after each source change
CSV_FILE = "manual_output_test_results.csv"

# MEAS:ALL? column order from firmware
COLUMNS = [
    "V1_V", "V1_I_A", "V2_V", "V2_I_A", "GND_I_A", "VS_V", "VS_I_A"
]

STEPS = [
    ("V1 = 1 V", 1.0, 0.0),
    ("V1 = 4 V", 4.0, 0.0),
    ("V2 = 1 V", 0.0, 1.0),
    ("V2 = 4 V", 0.0, 4.0),
]


# ---------------------------------------------------------------------------
# Serial helpers
# ---------------------------------------------------------------------------
def find_port() -> str:
    ports = serial.tools.list_ports.comports()
    usb_ports = [
        p.device for p in ports
        if "USB" in (p.description or "").upper()
        or "CH340" in (p.description or "").upper()
        or "ARDUINO" in (p.description or "").upper()
    ]
    if usb_ports:
        return usb_ports[0]
    if ports:
        return ports[0].device
    raise RuntimeError("No serial ports found. Connect the device and retry.")


def send(ser: serial.Serial, cmd: str) -> None:
    ser.write((cmd + "\n").encode())


def query(ser: serial.Serial, cmd: str) -> str:
    send(ser, cmd)
    line = ser.readline().decode(errors="replace").strip()
    if line.startswith("ERROR"):
        raise RuntimeError(f"Device error for {cmd!r}: {line}")
    return line


def meas_all(ser: serial.Serial) -> list[float]:
    raw = query(ser, "MEAS:ALL?")
    values = [float(x) for x in raw.split(",")]
    if len(values) != len(COLUMNS):
        raise ValueError(
            f"Expected {len(COLUMNS)} MEAS:ALL values, got {len(values)}: {raw!r}"
        )
    return values


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    port = PORT or find_port()
    print(f"Connecting to {port} @ {BAUD} baud")

    results: list[tuple[str, float, float, list[float]]] = []

    with serial.Serial(port, BAUD, timeout=TIMEOUT) as ser:
        time.sleep(2.0)  # allow reset after opening serial
        ser.reset_input_buffer()

        idn = query(ser, "*IDN?")
        print(f"Connected: {idn}")

        send(ser, "*RST")

        header = "  ".join(f"{c:>12}" for c in COLUMNS)
        print(f"\n{'Step':<22}  {header}")
        print("-" * (24 + 14 * len(COLUMNS)))

        for label, v1, v2 in STEPS:
            send(ser, f"SOUR1:VOLT {v1}")
            send(ser, f"SOUR2:VOLT {v2}")
            time.sleep(SETTLE_S)

            try:
                input(f"\n[{label}]  Press Enter to measure...")
            except EOFError:
                pass  # non-interactive environment — measure immediately

            values = meas_all(ser)
            results.append((label, v1, v2, values))

            row_str = "  ".join(f"{v:>12.6f}" for v in values)
            print(f"  {label:<20}  {row_str}")

        # Leave outputs safe
        send(ser, "SOUR1:VOLT 0")
        send(ser, "SOUR2:VOLT 0")

    # Save CSV
    with open(CSV_FILE, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["step", "v1_set", "v2_set"] + COLUMNS)
        for label, v1, v2, values in results:
            w.writerow([label, f"{v1:.3f}", f"{v2:.3f}"] + [f"{v:.6f}" for v in values])

    print(f"\nSaved results to {CSV_FILE}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nAborted by user.")
        sys.exit(0)
    except Exception as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        sys.exit(1)
