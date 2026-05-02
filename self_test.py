"""
ComponentTester self-test.

Test setup:
- Connect all probes together at one node (including DUT ground sense).
- Script applies four voltage combinations:
  (V1, V2) = (1,1), (1,4), (4,1), (4,4) volts.
- For each condition, it captures multiple MEAS:ALL? samples.

Output:
- Console summary with mean values and simple PASS/FAIL checks.
- CSV file with raw samples: self_test_results.csv
"""

from __future__ import annotations

import csv
import statistics
import sys
import time

import serial
import serial.tools.list_ports

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
PORT = None               # Set to e.g. "COM3" to skip auto-detection
BAUD = 115200
TIMEOUT = 2.0             # seconds
SETTLE_S = 0.15           # seconds after each source change
SAMPLES_PER_CONDITION = 50
CSV_FILE = "self_test_results.csv"

# Current MEAS:ALL? order from firmware:
# V1V,V1I,V2V,V2I,GNDI,VSV,VSI
COLUMNS = [
    "V1_V", "V1_I_A", "V2_V", "V2_I_A", "GND_I_A", "VS_V", "VS_I_A"
]

# Self-test criteria (tuned for quick validation, not precision calibration)
TRACKING_TOL_V = 0.10       # |V1_V - V2_V| when probes are tied
SETPOINT_TOL_V = 0.25       # allowed error for equal-source conditions (1/1 and 4/4)
MIXED_EXPECTED_V = 2.5      # expected tied-node voltage for mixed 1V/4V drive
MIXED_EXPECTED_I_A = 0.0136 # expected branch current magnitude (13.6 mA)
MIXED_TOL_V = 0.30          # tolerance around expected mixed-condition voltage
MIXED_TOL_I_A = 0.004       # tolerance around expected mixed-condition current magnitude

CONDITIONS = [
    ("C00", 1.0, 1.0),
    ("C01", 1.0, 4.0),
    ("C10", 4.0, 1.0),
    ("C11", 4.0, 4.0),
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
# Test logic
# ---------------------------------------------------------------------------
def collect_condition(
    ser: serial.Serial,
    label: str,
    v1: float,
    v2: float,
    samples: int,
) -> list[list[float]]:
    print(f"\n[{label}] Set V1={v1:.3f} V, V2={v2:.3f} V")
    send(ser, f"SOUR1:VOLT {v1}")
    send(ser, f"SOUR2:VOLT {v2}")
    time.sleep(SETTLE_S)

    rows: list[list[float]] = []
    for i in range(samples):
        rows.append(meas_all(ser))
        if (i + 1) % 10 == 0:
            print(f"  {i + 1}/{samples}")
    return rows


def mean_of_col(rows: list[list[float]], name: str) -> float:
    idx = COLUMNS.index(name)
    return statistics.mean(r[idx] for r in rows)


def summarize_condition(label: str, v1_set: float, v2_set: float, rows: list[list[float]]) -> dict:
    v1_mean = mean_of_col(rows, "V1_V")
    v2_mean = mean_of_col(rows, "V2_V")
    i1_mean = mean_of_col(rows, "V1_I_A")
    i2_mean = mean_of_col(rows, "V2_I_A")
    ig_mean = mean_of_col(rows, "GND_I_A")

    track_err = abs(v1_mean - v2_mean)
    tracking_ok = track_err <= TRACKING_TOL_V

    equal_setpoint_ok = True
    setpoint_err = None
    mixed_voltage_ok = True
    mixed_current_ok = True
    mixed_voltage_err = None
    mixed_current_err = None

    if abs(v1_set - v2_set) < 1e-9:
        target = v1_set
        setpoint_err = max(abs(v1_mean - target), abs(v2_mean - target))
        equal_setpoint_ok = setpoint_err <= SETPOINT_TOL_V
    else:
        mixed_voltage_err = max(
            abs(v1_mean - MIXED_EXPECTED_V),
            abs(v2_mean - MIXED_EXPECTED_V),
        )
        mixed_current_err = max(
            abs(abs(i1_mean) - MIXED_EXPECTED_I_A),
            abs(abs(i2_mean) - MIXED_EXPECTED_I_A),
        )
        mixed_voltage_ok = mixed_voltage_err <= MIXED_TOL_V
        mixed_current_ok = mixed_current_err <= MIXED_TOL_I_A

    return {
        "label": label,
        "v1_set": v1_set,
        "v2_set": v2_set,
        "v1_mean": v1_mean,
        "v2_mean": v2_mean,
        "i1_mean": i1_mean,
        "i2_mean": i2_mean,
        "ig_mean": ig_mean,
        "track_err": track_err,
        "tracking_ok": tracking_ok,
        "setpoint_err": setpoint_err,
        "equal_setpoint_ok": equal_setpoint_ok,
        "mixed_voltage_err": mixed_voltage_err,
        "mixed_current_err": mixed_current_err,
        "mixed_voltage_ok": mixed_voltage_ok,
        "mixed_current_ok": mixed_current_ok,
    }


def print_summary(results: list[dict]) -> bool:
    print("\n=== Self-Test Summary ===")
    print(
        "Condition   V1_set  V2_set   V1_mean   V2_mean   |dV|    "
        "I1_mean(A)  I2_mean(A)  Ignd_mean(A)   Result"
    )

    all_ok = True
    for r in results:
        if abs(r["v1_set"] - r["v2_set"]) < 1e-9:
            row_ok = r["tracking_ok"] and r["equal_setpoint_ok"]
        else:
            row_ok = r["tracking_ok"] and r["mixed_voltage_ok"] and r["mixed_current_ok"]

        all_ok = all_ok and row_ok
        status = "PASS" if row_ok else "FAIL"

        print(
            f"{r['label']:>8}  {r['v1_set']:>7.3f} {r['v2_set']:>7.3f} "
            f"{r['v1_mean']:>9.4f} {r['v2_mean']:>9.4f} {r['track_err']:>7.4f} "
            f"{r['i1_mean']:>11.6f} {r['i2_mean']:>11.6f} {r['ig_mean']:>12.6f}   {status}"
        )

        if r["setpoint_err"] is not None:
            print(f"           setpoint error check: {r['setpoint_err']:.4f} V")
        if r["mixed_voltage_err"] is not None:
            print(
                "           mixed checks: "
                f"Verr={r['mixed_voltage_err']:.4f} V, "
                f"Ierr={r['mixed_current_err']:.6f} A"
            )

    print("\nOverall:", "PASS" if all_ok else "FAIL")
    print(
        f"Criteria: tracking <= {TRACKING_TOL_V:.3f} V; "
        f"equal-setpoint error <= {SETPOINT_TOL_V:.3f} V; "
        f"mixed target {MIXED_EXPECTED_V:.3f} V +/- {MIXED_TOL_V:.3f} V; "
        f"mixed |I| target {MIXED_EXPECTED_I_A:.3f} A +/- {MIXED_TOL_I_A:.3f} A"
    )
    return all_ok


def write_csv(all_rows: list[tuple[str, float, float, int, list[float]]]) -> None:
    with open(CSV_FILE, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["condition", "v1_set", "v2_set", "sample"] + COLUMNS)
        for label, v1_set, v2_set, sample_idx, values in all_rows:
            w.writerow([label, f"{v1_set:.3f}", f"{v2_set:.3f}", sample_idx] + [f"{v:.6f}" for v in values])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    port = PORT or find_port()
    print(f"Connecting to {port} @ {BAUD} baud")

    all_rows: list[tuple[str, float, float, int, list[float]]] = []
    summaries: list[dict] = []

    with serial.Serial(port, BAUD, timeout=TIMEOUT) as ser:
        time.sleep(2.0)  # allow reset after opening serial
        ser.reset_input_buffer()

        idn = query(ser, "*IDN?")
        print(f"Connected: {idn}")

        send(ser, "*RST")

        for label, v1_set, v2_set in CONDITIONS:
            rows = collect_condition(ser, label, v1_set, v2_set, SAMPLES_PER_CONDITION)
            summaries.append(summarize_condition(label, v1_set, v2_set, rows))

            for i, vals in enumerate(rows, start=1):
                all_rows.append((label, v1_set, v2_set, i, vals))

        # Leave outputs safe
        send(ser, "SOUR1:VOLT 0")
        send(ser, "SOUR2:VOLT 0")

    overall_ok = print_summary(summaries)
    write_csv(all_rows)
    print(f"\nSaved raw samples to {CSV_FILE}")

    if not overall_ok:
        sys.exit(2)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nAborted by user.")
        sys.exit(0)
    except Exception as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        sys.exit(1)
