"""
Waveform capture test using the Arduino firmware's WAV subsystem.
Sets V1 to 1V and captures a waveform using a configurable ADC prescaler.
Performs FFT to identify peak frequency.
"""

import csv
import io
import sys
import time

import matplotlib.pyplot as plt
import numpy as np
from scipy import signal
import serial
import serial.tools.list_ports

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
PORT      = None        # Set to e.g. "COM3" to skip auto-detection
BAUD      = 115200
TIMEOUT   = 5.0         # seconds per readline (waveforms can be large)
ADC_PRESCALER = 128

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


def query_multiline(ser: serial.Serial, cmd: str, timeout: float = 5.0) -> str:
    """Send a query and read multiple lines until timeout, returning concatenated result."""
    send(ser, cmd)
    result = []
    ser.timeout = timeout
    try:
        while True:
            line = ser.readline().decode(errors="replace").strip()
            if not line:
                break
            result.append(line)
    except serial.SerialException:
        pass
    finally:
        ser.timeout = TIMEOUT
    return "\n".join(result)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    port = PORT or find_port()
    print(f"Connecting to {port} at {BAUD} baud...")

    with serial.Serial(port, BAUD, timeout=TIMEOUT) as ser:
        time.sleep(2)  # wait for Arduino reset after DTR toggle
        ser.reset_input_buffer()

        # Verify identity
        idn = query(ser, "*IDN?")
        print(f"Connected: {idn}\n")

        # Reset the device
        send(ser, "*RST")
        time.sleep(0.1)

        # Set ADC prescaler for waveform capture.
        print(f"Setting ADC prescaler to {ADC_PRESCALER}")
        send(ser, f"SENS:ADC:PRES {ADC_PRESCALER}")
        time.sleep(0.1)

        # Set V1 to 1V
        print("Setting V1 = 1.0 V")
        send(ser, "SOUR1:VOLT 1.0")
        time.sleep(0.5)

        # Configure waveform capture
        print("\nConfiguring waveform capture...")

        # Set signal list to capture only V1V
        signals = "V1V"
        send(ser, f"WAV:SIGN {signals}")
        print(f"  Signals: {signals}")

        # Get max points for this signal list
        max_pts = query(ser, "WAV:POIN:MAX?")
        print(f"  Max points available: {max_pts}")

        # Set to maximum points
        send(ser, "WAV:POIN MAX")
        pts_set = query(ser, "WAV:POIN?")
        print(f"  Set to: {pts_set} points")

        # Capture waveform
        print("\nCapturing waveform...")
        wav_csv = query_multiline(ser, "WAV:DATA?", timeout=10.0)
        lines = wav_csv.strip().split("\n")
        print(f"  Captured {len(lines)} lines")

        # Parse CSV - skip metadata lines (they don't have comma separators or start with INDEX)
        print("\nParsing waveform data...")
        csv_lines = []
        for line in lines:
            # Skip metadata lines (WAV, POINTS, SIGNALS, etc.)
            if line.startswith("INDEX") or ("," in line and not line.startswith("WAV")):
                csv_lines.append(line)
        
        wav_csv_clean = "\n".join(csv_lines)
        reader = csv.DictReader(io.StringIO(wav_csv_clean))
        rows = list(reader)
        print(f"  Data points: {len(rows)}")

        if len(rows) == 0:
            print("ERROR: No data captured")
            print(f"Raw output:\n{wav_csv}")
            return

        # Extract columns
        columns = list(rows[0].keys())
        print(f"  Columns: {columns}")

        # Convert to numeric
        data = {col: [] for col in columns}
        for row in rows:
            for col in columns:
                try:
                    data[col].append(float(row[col]))
                except (ValueError, KeyError):
                    pass

        # Plot
        print("\nPlotting waveform...")
        
        # Calculate sampling rate
        # Arduino 16 MHz clock divided by the configured prescaler.
        # ADC takes 13 cycles per conversion.
        adc_clock_hz = 16e6 / ADC_PRESCALER
        cycles_per_sample = 13
        fs = adc_clock_hz / cycles_per_sample
        ts = 1.0 / fs  # time between samples
        time_ms = np.arange(len(rows)) * ts * 1000  # convert to milliseconds

        print(f"Sampling rate: {fs/1e3:.1f} kHz")
        print(f"Time window: {time_ms[-1]:.3f} ms")

        # Extract V1V data (check both naming conventions)
        v1v_data = None
        for col_name in ["V1_V", "V1V"]:
            if col_name in data:
                v1v_data = np.array(data[col_name])
                print(f"Found signal column: {col_name}")
                break
        
        if v1v_data is None or len(v1v_data) == 0:
            print("ERROR: No V1V/V1_V data found")
            print(f"Available columns: {list(data.keys())}")
            return

        plot_data = v1v_data[1:]
        plot_time_ms = time_ms[1:]
        if len(plot_data) == 0:
            print("ERROR: Not enough V1V data after removing the first sample")
            return

        # Compute FFT
        print("\nPerforming FFT analysis...")
        fft_input = plot_data

        fft_vals = np.fft.fft(fft_input)
        fft_freqs = np.fft.fftfreq(len(fft_input), ts)
        fft_mag = np.abs(fft_vals) / len(fft_input)  # Normalize

        # Keep only positive frequencies
        pos_mask = fft_freqs > 0
        pos_freqs = fft_freqs[pos_mask]
        pos_mag = fft_mag[pos_mask]

        # Find peak frequency
        peak_idx = np.argmax(pos_mag)
        peak_freq = pos_freqs[peak_idx]
        peak_mag = pos_mag[peak_idx]
        print(f"Peak frequency: {peak_freq/1e3:.2f} kHz (magnitude: {peak_mag:.4f} V)")
        print(f"Is this PWM? Typical Arduino PWM: 490 Hz (timer0) or 977 Hz (timer1/2)")

        # Plot time-domain and frequency-domain
        fig, axes = plt.subplots(2, 1, figsize=(12, 8))

        # Time-domain plot
        ax_time = axes[0]
        ax_time.plot(plot_time_ms, plot_data, linewidth=0.8, alpha=0.8, color="steelblue")
        ax_time.set_title("V1V Waveform (Time Domain)", fontweight="bold")
        ax_time.set_xlabel("Time (ms)")
        ax_time.set_ylabel("Voltage (V)")
        ax_time.grid(True, alpha=0.3)

        # Frequency-domain plot
        ax_freq = axes[1]
        ax_freq.semilogy(pos_freqs / 1e3, pos_mag, linewidth=0.8, alpha=0.8, color="coral")
        ax_freq.axvline(peak_freq / 1e3, color="red", linestyle="--", linewidth=2, 
                       label=f"Peak: {peak_freq/1e3:.2f} kHz")
        ax_freq.set_title("V1V Spectrum (Frequency Domain, FFT)", fontweight="bold")
        ax_freq.set_xlabel("Frequency (kHz)")
        ax_freq.set_ylabel("Magnitude (V)")
        ax_freq.set_xlim([0, fs / 2e3])  # Nyquist limit in kHz
        ax_freq.grid(True, alpha=0.3, which="both")
        ax_freq.legend()

        fig.suptitle(f"V1V Waveform Capture (Prescaler={ADC_PRESCALER}, Fs={fs/1e3:.0f} kHz)", 
                    fontsize=14, fontweight="bold")
        plt.tight_layout()
        plt.savefig("waveform_capture.png", dpi=100, bbox_inches="tight")
        print(f"  Plot saved to waveform_capture.png")

        # Save CSV
        with open("waveform_capture.csv", "w", newline="") as f:
            f.write(wav_csv_clean)
        print(f"  Data saved to waveform_capture.csv")

        plt.show()

        # Print summary
        print("\n--- V1V Statistics ---")
        print(f"V1V          min={np.min(v1v_data):>10.6f}  max={np.max(v1v_data):>10.6f}  "
              f"mean={np.mean(v1v_data):>10.6f}  stdev={np.std(v1v_data):>10.6f}")
        print(f"\n--- FFT Analysis ---")
        print(f"Peak frequency: {peak_freq:.1f} Hz")
        if peak_freq < 1000:
            print(f"  This appears to be PWM (Arduino PWM: 490 Hz / 977 Hz)")
        elif peak_freq < 100000:
            print(f"  This is in the kilohertz range (likely sampling noise or switching artifact)")
        else:
            print(f"  This is in the high-frequency range (likely ADC or switching noise)")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(0)
    except Exception as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)
