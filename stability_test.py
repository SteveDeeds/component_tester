"""
Stability test for ComponentTester.
Sets V1 and V2 to 1 V, collects 100 MEAS:ALL? samples, then sets both to 0 V
and collects another 100 samples.  Results are printed as a table and saved to
stability_results.csv.
"""

import csv
import statistics
import sys
import time

import matplotlib.pyplot as plt
import serial
import serial.tools.list_ports

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
PORT      = None        # Set to e.g. "COM3" to skip auto-detection
BAUD      = 115200
TIMEOUT   = 2.0         # seconds per readline
SAMPLES   = 100
SETTLE_S  = 0.1         # seconds to wait after setting a voltage before sampling
CSV_FILE  = "stability_results.csv"

# Column labels matching MEAS:ALL? order (SI units)
COLUMNS = ["V1_V", "V1_I_A", "V2_V", "V2_I_A", "GND_I_A",
           "VS_V", "VS_I_A"]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def find_port() -> str:
    """Return the first USB-serial port found, or raise if none."""
    ports = serial.tools.list_ports.comports()
    usb_ports = [p.device for p in ports if "USB" in (p.description or "").upper()
                 or "CH340" in (p.description or "").upper()
                 or "Arduino" in (p.description or "")]
    if usb_ports:
        return usb_ports[0]
    if ports:
        return ports[0].device
    raise RuntimeError("No serial ports found. Connect the Arduino and retry.")


def send(ser: serial.Serial, cmd: str) -> None:
    """Send a SCPI command (adds LF terminator)."""
    ser.write((cmd + "\n").encode())


def query(ser: serial.Serial, cmd: str) -> str:
    """Send a SCPI query and return the stripped response."""
    send(ser, cmd)
    response = ser.readline().decode(errors="replace").strip()
    if response.startswith("ERROR"):
        raise RuntimeError(f"Device error for '{cmd}': {response}")
    return response


def meas_all(ser: serial.Serial) -> list[float]:
    """Return MEAS:ALL? as a list of 7 floats."""
    raw = query(ser, "MEAS:ALL?")
    values = [float(x) for x in raw.split(",")]
    if len(values) != 7:
        raise ValueError(f"Expected 7 values from MEAS:ALL?, got: {raw!r}")
    return values


def collect(ser: serial.Serial, n: int, label: str) -> list[list[float]]:
    """Collect n samples, printing progress."""
    print(f"  Collecting {n} samples [{label}]...")
    rows = []
    for i in range(n):
        rows.append(meas_all(ser))
        if (i + 1) % 10 == 0:
            print(f"    {i + 1}/{n}")
    return rows


def print_stats(rows: list[list[float]], label: str) -> None:
    """Print mean ± stdev for each channel."""
    print(f"\n--- Statistics: {label} ---")
    header = f"{'Channel':<12} {'Mean':>12} {'StdDev':>12} {'Min':>12} {'Max':>12}"
    print(header)
    print("-" * len(header))
    for col_idx, col_name in enumerate(COLUMNS):
        vals = [r[col_idx] for r in rows]
        unit = "A" if "_I_" in col_name else "V"
        print(f"{col_name:<12} {statistics.mean(vals):>12.6f} "
              f"{statistics.stdev(vals):>12.6f} "
              f"{min(vals):>12.6f} {max(vals):>12.6f}  {unit}")


def plot_measurements(rows_1v: list[list[float]], rows_0v: list[list[float]]) -> None:
    """Plot measurements over time for both phases."""
    fig, axes = plt.subplots(3, 3, figsize=(14, 10))
    fig.suptitle("Stability Test Measurements", fontsize=14, fontweight="bold")
    axes = axes.flatten()

    for col_idx, col_name in enumerate(COLUMNS):
        ax = axes[col_idx]
        unit = "A" if "_I_" in col_name else "V"

        vals_1v = [r[col_idx] for r in rows_1v]
        ax.plot(vals_1v, "o-", label="1 V phase", markersize=3, linewidth=1, alpha=0.7)

        vals_0v = [r[col_idx] for r in rows_0v]
        x_0v = [len(vals_1v) + i for i in range(len(vals_0v))]
        ax.plot(x_0v, vals_0v, "s-", label="0 V phase", markersize=3, linewidth=1, alpha=0.7)

        ax.axvline(len(vals_1v), color="gray", linestyle="--", alpha=0.5, linewidth=1)
        ax.set_title(f"{col_name} ({unit})", fontsize=10)
        ax.set_xlabel("Sample #")
        ax.set_ylabel(col_name)
        ax.grid(True, alpha=0.3)
        if col_idx == 0:
            ax.legend(fontsize=8)

    plt.tight_layout()
    plt.savefig("stability_plot.png", dpi=100, bbox_inches="tight")
    print(f"\nPlot saved to stability_plot.png")
    plt.show()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    port = PORT or find_port()
    print(f"Connecting to {port} at {BAUD} baud...")

    with serial.Serial(port, BAUD, timeout=TIMEOUT) as ser:
        time.sleep(2)          # wait for Arduino reset after DTR toggle
        ser.reset_input_buffer()

        # Verify identity
        idn = query(ser, "*IDN?")
        print(f"Connected: {idn}\n")

        # Reset the device to a known state
        send(ser, "*RST")

        # --- Phase 1: 1 V ---
        print("Setting V1 = 1.0 V, V2 = 1.0 V")
        send(ser, "SOUR1:VOLT 1.0")
        send(ser, "SOUR2:VOLT 1.0")
        time.sleep(SETTLE_S)

        rows_1v = collect(ser, SAMPLES, "1 V")

        # --- Phase 2: 0 V ---
        print("\nSetting V1 = 0.0 V, V2 = 0.0 V")
        send(ser, "SOUR1:VOLT 0.0")
        send(ser, "SOUR2:VOLT 0.0")
        time.sleep(SETTLE_S)

        rows_0v = collect(ser, SAMPLES, "0 V")

    # --- Statistics ---
    print_stats(rows_1v, "1 V setpoint")
    print_stats(rows_0v, "0 V setpoint")

    # --- Plot ---
    plot_measurements(rows_1v, rows_0v)

    # --- Save CSV ---
    with open(CSV_FILE, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["phase", "sample"] + COLUMNS)
        for i, row in enumerate(rows_1v):
            writer.writerow(["1V", i + 1] + [f"{v:.6f}" for v in row])
        for i, row in enumerate(rows_0v):
            writer.writerow(["0V", i + 1] + [f"{v:.6f}" for v in row])

    print(f"\nResults saved to {CSV_FILE}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(0)
    except Exception as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        sys.exit(1)
