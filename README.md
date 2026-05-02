# Component Tester — Arduino-Based Test Equipment

A full-stack test equipment controller built on Arduino with Python GUI and analysis tools for rapid transistor characterization. Features dual programmable voltage sources, multi-channel data acquisition, and live waveform capture via SCPI serial protocol.

## Features

- **Dual Programmable Sources**: V1 (fast 12-bit PWM) and V2 (standard 8-bit PWM)
- **Multi-Channel ADC**: Real-time voltage and current sensing on V1, V2, ground, and supply rails
- **Waveform Capture**: Time-domain acquisition with configurable sampling
- **SCPI Protocol**: Standard IEEE 488.2 serial command interface
- **Python GUI**: Interactive sweep plots with live parameter adjustment
- **Data Analysis**: CSV export, contour plots, and thermal mapping

---

## Prerequisites

### Hardware
- Arduino board (compatible with the firmware in `component_tester/component_tester.ino`)
- USB cable for programming and serial communication
- Test circuit matching the documented pin assignments

### Software
- **Arduino IDE** (for uploading the sketch)
- **Python 3.8+** (for the GUI and analysis tools)

---

## Setup Instructions

### 1. Arduino Setup

#### Download and Install Arduino IDE
1. Visit https://www.arduino.cc/en/software
2. Download the appropriate version for your OS (Windows, macOS, or Linux)
3. Run the installer and follow the on-screen prompts
4. Ensure the Arduino core libraries are installed during setup

#### Upload the Sketch
1. Open Arduino IDE
2. Go to **File** → **Open** and navigate to `component_tester/component_tester.ino`
3. Select your Arduino board:
   - Go to **Tools** → **Board** and choose your board type
   - Go to **Tools** → **Port** and select the COM port where your Arduino is connected
4. Verify the sketch by clicking **Sketch** → **Verify/Compile**
5. Upload the sketch by clicking **Sketch** → **Upload** (or press Ctrl+U)
6. Wait for the upload to complete (you should see a confirmation message)
7. Open the Serial Monitor (**Tools** → **Serial Monitor**) and verify:
   - Set baud rate to **115200**
   - Send `*IDN?` to verify the device responds with identification

---

### 2. Python Environment Setup

#### Create a Virtual Environment
Open a terminal/PowerShell in the project root and run:

```bash
# Windows (PowerShell)
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# macOS / Linux (Bash)
python3 -m venv .venv
source .venv/bin/activate
```

#### Install Dependencies
```bash
pip install -r requirements.txt
```

The dependencies include:
- **pyserial**: Serial communication with Arduino
- **PySide6**: GUI framework
- **pyqtgraph**: High-performance plotting
- **qt-material**: Modern UI styling
- **pandas**: Data manipulation and CSV I/O
- **scipy**: Interpolation and numerical analysis

---

## Usage

### Launch the GUI
```bash
python transistor_gui.py
```

The GUI provides:
- **Axis Selectors**: Choose X, Y, and color dimensions from your data
- **Transform Controls**: Apply log scale and negate axes
- **Contour Mode**: 2D heatmap visualization for 3-variable plots
- **Mouse Readout**: Live coordinate display on hover
- **Interactive Zooming**: Click and drag to zoom; right-click to pan

### Analyze Existing Data
```bash
python csv_viewer.py
```

Load and inspect any CSV file previously exported from sweeps or tests.

### Plot Standalone
```bash
python plot_transistor.py
```

Quick visualization of transistor characteristics from CSV files.

---

## Hardware Pin Mapping

| Function           | Pin   | Notes                          |
|--------------------|-------|--------------------------------|
| V1 Output (PWM)    | D9    | 12-bit, 3.906 kHz              |
| V2 Output (PWM)    | D3    | 8-bit, 490 Hz                  |
| V1 Voltage Sense   | A0    | DUT side measurement           |
| V1 Current Sense   | A1    | Test equipment side            |
| V2 Voltage Sense   | A2    | DUT side measurement           |
| V2 Current Sense   | A3    | Test equipment side            |
| GND Current Sense  | A4    | Ground reference / current out |
| VS Supply Sense    | A5    | Supply voltage monitoring      |

---

## SCPI Command Reference

Quick reference for serial commands (115200 baud, LF or CRLF terminated):

```
# System
*IDN?                      → Device identification
*RST                       → Reset to safe state

# Source Control (V1 & V2: 0.0–5.0 V)
SOUR1:VOLT <value>         → Set V1 voltage
SOUR1:VOLT?                → Query V1 setpoint
SOUR2:VOLT <value>         → Set V2 voltage
SOUR2:VOLT?                → Query V2 setpoint

# Measurement (all in SI: V or A)
MEAS:VOLT1?                → V1 voltage
MEAS:VOLT2?                → V2 voltage
MEAS:CURR1?                → V1 current
MEAS:CURR2?                → V2 current
MEAS:ALL?                  → All measurements (CSV)

# Calibration (shunt resistances)
CAL:SHUN:V1 <ohms>         → Set V1 shunt
CAL:SHUN:V2 <ohms>         → Set V2 shunt
CAL:SHUN:GND <ohms>        → Set ground shunt
```

For full command documentation, see comments in `component_tester/component_tester.ino`.

---

## File Structure

```
component_tester/
├── component_tester.ino          # Arduino firmware
├── transistor_gui.py             # Main GUI application
├── gui_app.py                    # Legacy GUI (predecessor to transistor_gui.py)
├── csv_viewer.py                 # Standalone CSV data viewer
├── plot_transistor.py            # Quick plot utility
├── sweep_plot.py                 # Reusable sweep plot widget
├── instrument_client.py          # Serial communication layer
├── transistor_analysis.py        # Analysis utilities
├── requirements.txt              # Python dependencies
└── README.md                     # This file
```

---

## Troubleshooting

### Arduino Not Detected
- Check USB cable and connection
- Verify the correct COM port in Tools → Port
- Try a different USB port on your computer
- Restart Arduino IDE

### Serial Communication Fails
- Ensure baud rate is set to **115200**
- Verify Arduino sketch uploaded successfully
- Close any other serial monitors (only one program can access the port at a time)
- Check `instrument_client.py` for timeout settings if needed

### Python Dependencies Missing
```bash
# Upgrade pip
pip install --upgrade pip

# Reinstall all requirements
pip install --force-reinstall -r requirements.txt
```

### GUI Won't Start
- Ensure virtual environment is activated
- On some systems, you may need to set `QT_QPA_PLATFORM` environment variable:
  ```bash
  # Windows PowerShell
  $env:QT_QPA_PLATFORM = "windows"
  python transistor_gui.py
  ```

---

## Contributing

When making changes:
1. Keep firmware and Python in sync (document breaking protocol changes)
2. Test with multiple transistor types before committing
3. Update SCPI command documentation if adding new commands
4. Add CSV test data to `.gitignore` (data files are not tracked)

---

## License

[Add your license here]

---

## References

- [Arduino Official Documentation](https://www.arduino.cc/en/Guide)
- [SCPI Standard (IEEE 488.2)](https://en.wikipedia.org/wiki/SCPI)
- [PyQtGraph Documentation](http://www.pyqtgraph.org/)
- [PySide6 Documentation](https://doc.qt.io/qtforpython/)
