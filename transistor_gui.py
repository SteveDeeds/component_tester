#!/usr/bin/env python3
"""
transistor_gui.py

Dedicated transistor characterisation GUI.

Layout:
  Left  - Arduino connection, transistor type + wiring diagram,
          sweep parameters (Vbe / Vce), run controls, console
  Right - Three tabbed plots:
            1. Gummel plot  (log Ic + log Ib vs Vb_V, FA only, colour = Vce_set)
            2. Current gain (Ic vs Ib, colour = Vce_set, β fit line)
            3. Output characteristics (Ic vs Vc_V, colour = Vbe_set, Early voltage)
          All with legend on the right.  Extracted SPICE parameters (n, Is, β, VAF)
          are displayed in the status bar below the plots.

Wiring (NPN default):
  V1 → Base (B)         [Vbe source, inner sweep]
  V2 → Collector (C)    [Vce source, outer sweep]
  GND → Emitter (E)
"""

from __future__ import annotations

import sys
import time
import random
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
import matplotlib.cm as cm
from instrument_client import InstrumentSession, list_serial_devices, serial_is_available
from PySide6.QtCore import QThread, QTimer, Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QSpinBox,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

# ── Constants ─────────────────────────────────────────────────────────────────
MEAS_VALUE_COUNT = 7
MEAS_VALUE_COUNT_LEGACY = -1  # No legacy mode; firmware returns 7 values directly
VT = 0.02585           # kT/q ≈ 300 K
ON_CURRENT_A = 200e-6
ON_VOLTAGE_V = 1e-3

# Hidden sweep strategy (not user-editable)
COARSE_VBE_START = 0.0
COARSE_VBE_STOP = 5.0
COARSE_VBE_STEP = 1.0
COARSE_VCE_START = 0.0
COARSE_VCE_STOP = 5.0
COARSE_VCE_STEP = 1.0
FINE_STEP_V = 0.2
FINE_WINDOW_V = 0.5
SLOPE_THRESHOLD_DECADES_PER_V = 1.0
SAMPLE_PERIOD_MS = 250
MEAS_SETTLE_MS = 50          # delay after source change before measuring
ADC_PRESCALE = 128
FW_AVERAGES = 255
FIXTURE_OFF_CURRENT_A = 200e-6
FIXTURE_ON_CURRENT_A = 200e-6

# Column names for the 7 MEAS:ALL? values (V1=Base, V2=Collector)
MEAS_COLS = ["Vb_V", "Ib_A", "Vc_V", "Ic_A", "Ie_A", "VS_V", "VS_I_A"]

# Probe wiring tables: (terminal label, probe tag, hex colour)
WIRING_TABLES = {
    "NPN": [
        ("Base (B)",        "V1",  "#0b7285"),
        ("Collector (C)",   "V2",  "#7c3aed"),
        ("Emitter (E)",     "GND", "#374151"),
    ],
    "PNP": [
        ("Base (B)",        "V1",  "#0b7285"),
        ("Emitter (E)",     "V2",  "#7c3aed"),
        ("Collector (C)",   "GND", "#374151"),
    ],
}


# ── Background device scanner ─────────────────────────────────────────────────
class DeviceScannerThread(QThread):
    devices_found = Signal(list)

    def run(self) -> None:
        self.devices_found.emit(list_serial_devices())


# ── Transistor state classification ──────────────────────────────────────────
def _classify_row(row: dict) -> str:
    """Classify a measurement row as OFF / FA / RA / SAT (NPN, emitter grounded)."""
    ic  = float(row.get("Ic_A", 0.0))
    ib  = float(row.get("Ib_A", 0.0))
    ie  = float(row.get("Ie_A", 0.0))
    vb  = float(row.get("Vb_V", 0.0))
    vc  = float(row.get("Vc_V", 0.0))

    max_i = max(abs(ic), abs(ib), abs(ie))
    vbe   = vb          # emitter at 0 V
    vbc   = -(vc - vb)  # vbc = vb - vc

    current_on = max_i > ON_CURRENT_A
    voltage_on = max(abs(vbe), abs(vbc)) > ON_VOLTAGE_V
    if not (current_on and voltage_on):
        return "OFF"

    be_fwd = vbe > ON_VOLTAGE_V
    bc_fwd = vbc > ON_VOLTAGE_V

    if be_fwd and not bc_fwd:
        return "FA"
    if be_fwd and bc_fwd:
        return "SAT"
    if not be_fwd and bc_fwd:
        return "RA"
    return "OFF"


# ── Diode fit ─────────────────────────────────────────────────────────────────
def _fit_diode(vbe: np.ndarray, i: np.ndarray) -> tuple:
    """Fit I = Is * exp(Vbe / (n*Vt)).  Returns (n, Is, r²) or (None, None, None)."""
    mask = i > 0
    if mask.sum() < 3:
        return None, None, None
    v, ln_i = vbe[mask], np.log(i[mask])
    coeffs  = np.polyfit(v, ln_i, 1)
    fitted  = np.polyval(coeffs, v)
    ss_res  = float(np.sum((ln_i - fitted) ** 2))
    ss_tot  = float(np.sum((ln_i - np.mean(ln_i)) ** 2))
    r2      = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    n       = 1.0 / (coeffs[0] * VT)
    Is      = float(np.exp(coeffs[1]))
    return n, Is, r2


# ── Early voltage estimation ──────────────────────────────────────────────────
def _fit_early_voltage(fa: pd.DataFrame) -> float | None:
    """Estimate VAF from FA output characteristics.

    Placeholder for future Vbe-clamped fixture mode.  Currently, with the
    selectable base-resistor shunt (100Ω or 1kΩ), Ib ≈ (Vbe_set − Vb_V) / R_shunt,
    making Ic nearly constant across Vce in each Vbe_set group.  The output
    characteristics plot still visualizes the surface; future work could fit
    Ic vs Vce over constant-Ib contour lines via interpolation.

    For now, always returns None.
    """
    return None


# ── Gummel plot canvas ────────────────────────────────────────────────────────
class GummelCanvas(FigureCanvasQTAgg):
    """Log Ic and log Ib vs Vb_V, FA points only, coloured by Vce_set."""

    plot_clicked = Signal(str, float, float)

    def __init__(self, parent=None):
        self._fig = Figure(figsize=(7, 5))
        super().__init__(self._fig)
        self._ax = self._fig.add_subplot(111)
        self._mpl_click_cid = self.mpl_connect("button_press_event", self._on_click)
        self._init_axes()

    def _init_axes(self) -> None:
        self._ax.cla()
        self._ax.grid(True, which="both", alpha=0.3)
        self._ax.set_xlabel("Vb_V  –  measured base voltage (V)")
        self._ax.set_ylabel("Current  (A)")
        self._ax.set_title("Gummel Plot  (FA region only)")
        self._ax.set_yscale("log")
        self._fig.subplots_adjust(left=0.11, right=0.67, top=0.91, bottom=0.11)
        self.draw()

    def update_plot(self, df: pd.DataFrame) -> None:
        ax = self._ax
        ax.cla()
        ax.grid(True, which="both", alpha=0.3)
        ax.set_xlabel("Vb_V  –  measured base voltage (V)")
        ax.set_ylabel("Current  (A)")
        ax.set_title("Gummel Plot  (FA region only)")

        if df.empty or "Transistor_State" not in df.columns:
            ax.set_yscale("log")
            self.draw()
            return

        fa = df[df["Transistor_State"] == "FA"].copy()
        if fa.empty:
            ax.set_yscale("log")
            self.draw()
            return

        vce_vals = sorted(fa["Vce_set"].unique()) if "Vce_set" in fa.columns else [None]
        palette  = cm.plasma(np.linspace(0.1, 0.88, max(len(vce_vals), 1)))
        cmap     = dict(zip(vce_vals, palette))

        for vce in vce_vals:
            sub = (
                fa[fa["Vce_set"] == vce].sort_values("Vb_V")
                if vce is not None
                else fa.sort_values("Vb_V")
            )
            col = cmap.get(vce, palette[0])
            lbl = f"Vce={vce:.1f} V" if vce is not None else ""
            ic_ok = sub[sub["Ic_A"] > 0]
            ib_ok = sub[sub["Ib_A"] > 0]
            if not ic_ok.empty:
                ax.semilogy(ic_ok["Vb_V"], ic_ok["Ic_A"],
                            "o-", color=col, ms=4, lw=1.2, label=f"Ic  {lbl}")
            if not ib_ok.empty:
                ax.semilogy(ib_ok["Vb_V"], ib_ok["Ib_A"],
                            "s--", color=col, ms=4, lw=1, alpha=0.65, label=f"Ib  {lbl}")

        all_ic = fa[fa["Ic_A"] > 0]
        all_ib = fa[fa["Ib_A"] > 0]
        if len(all_ic) >= 4 and len(all_ib) >= 4:
            n_c, Is_c, _ = _fit_diode(all_ic["Vb_V"].values, all_ic["Ic_A"].values)
            n_b, Is_b, _ = _fit_diode(all_ib["Vb_V"].values, all_ib["Ib_A"].values)
            v_fit = np.linspace(fa["Vb_V"].min(), fa["Vb_V"].max(), 300)
            if n_c:
                ax.semilogy(v_fit, Is_c * np.exp(v_fit / (n_c * VT)),
                            "b-", lw=2.0, label=f"Ic fit: n={n_c:.2f}, Is={Is_c:.2e} A")
            if n_b:
                ax.semilogy(v_fit, Is_b * np.exp(v_fit / (n_b * VT)),
                            "r-", lw=2.0, label=f"Ib fit: n={n_b:.2f}, Is={Is_b:.2e} A")

        ax.legend(loc="upper left", bbox_to_anchor=(1.02, 1.0),
                  borderaxespad=0, fontsize=7, frameon=False)
        self.draw()

    def clear(self) -> None:
        self._init_axes()

    def _on_click(self, event) -> None:
        if event.inaxes is not self._ax or event.xdata is None or event.ydata is None:
            return
        self.plot_clicked.emit("gummel", float(event.xdata), float(event.ydata))


# ── Beta (current gain) canvas ────────────────────────────────────────────────
class BetaCanvas(FigureCanvasQTAgg):
    """Ic vs Ib scatter, coloured by Vce_set, with a β fit line through the origin."""

    plot_clicked = Signal(str, float, float)

    def __init__(self, parent=None):
        self._fig = Figure(figsize=(7, 5))
        super().__init__(self._fig)
        self._ax = self._fig.add_subplot(111)
        self._mpl_click_cid = self.mpl_connect("button_press_event", self._on_click)
        self._init_axes()

    def _init_axes(self) -> None:
        self._ax.cla()
        self._ax.grid(True, alpha=0.3)
        self._ax.set_xlabel("Ib  (µA)")
        self._ax.set_ylabel("Ic  (mA)")
        self._ax.set_title("Current Gain  –  Ic vs Ib  (color = Vce)")
        self._fig.subplots_adjust(left=0.11, right=0.67, top=0.91, bottom=0.11)
        self.draw()

    def update_plot(self, df: pd.DataFrame) -> None:
        ax = self._ax
        ax.cla()
        ax.grid(True, alpha=0.3)
        ax.set_xlabel("Ib  (µA)")
        ax.set_ylabel("Ic  (mA)")
        ax.set_title("Current Gain  –  Ic vs Ib  (color = Vce)")

        if df.empty or "Transistor_State" not in df.columns:
            self.draw()
            return

        fa = df[df["Transistor_State"] == "FA"].copy()
        if fa.empty:
            self.draw()
            return

        vce_vals = sorted(fa["Vce_set"].unique()) if "Vce_set" in fa.columns else [None]
        palette  = cm.plasma(np.linspace(0.1, 0.88, max(len(vce_vals), 1)))
        cmap     = dict(zip(vce_vals, palette))

        for vce in vce_vals:
            sub = (
                fa[fa["Vce_set"] == vce].sort_values("Ib_A")
                if vce is not None
                else fa.sort_values("Ib_A")
            )
            both = sub[(sub["Ic_A"] > 0) & (sub["Ib_A"] > 0)]
            if both.empty:
                continue
            col = cmap.get(vce, palette[0])
            lbl = f"Vce={vce:.1f} V" if vce is not None else ""
            ax.plot(both["Ib_A"] * 1e6, both["Ic_A"] * 1e3,
                    "o-", color=col, ms=4, lw=1.2, alpha=0.9, label=lbl, zorder=3)

        fa_both = fa[(fa["Ic_A"] > 0) & (fa["Ib_A"] > 0)].copy()
        if len(fa_both) >= 3:
            ib_all = fa_both["Ib_A"].to_numpy(dtype=float)
            ic_all = fa_both["Ic_A"].to_numpy(dtype=float)
            # Fit Ic = β·Ib through origin using least squares.
            beta_fit = float(np.dot(ib_all, ic_all) / np.dot(ib_all, ib_all))
            ib_range = np.linspace(0, ib_all.max(), 200)
            ax.plot(ib_range * 1e6, beta_fit * ib_range * 1e3,
                    "k--", lw=1.5, alpha=0.7, label=f"fit: β = {beta_fit:.1f}")

        ax.set_xlim(left=0)
        ax.set_ylim(bottom=0)
        ax.legend(loc="upper left", bbox_to_anchor=(1.02, 1.0),
                  borderaxespad=0, fontsize=7, frameon=False)
        self.draw()

    def clear(self) -> None:
        self._init_axes()

    def _on_click(self, event) -> None:
        if event.inaxes is not self._ax or event.xdata is None or event.ydata is None:
            return
        self.plot_clicked.emit("beta", float(event.xdata), float(event.ydata))


# ── Output characteristics canvas ─────────────────────────────────────────────
class OutputCanvas(FigureCanvasQTAgg):
    """Ic vs Vc_V (output characteristics), FA points coloured by Vbe_set.

    Each Vbe_set trace is extended as a dashed line to the x-axis to visualise
    where the extrapolated lines converge at Vce = −VAF.
    """

    plot_clicked = Signal(str, float, float)

    def __init__(self, parent=None):
        self._fig = Figure(figsize=(7, 5))
        super().__init__(self._fig)
        self._ax = self._fig.add_subplot(111)
        self._mpl_click_cid = self.mpl_connect("button_press_event", self._on_click)
        self._init_axes()

    def _init_axes(self) -> None:
        self._ax.cla()
        self._ax.grid(True, alpha=0.3)
        self._ax.set_xlabel("Vc_V  –  measured collector voltage (V)")
        self._ax.set_ylabel("Ic  (mA)")
        self._ax.set_title("Output Characteristics  (FA region)  ·  Early Voltage")
        self._fig.subplots_adjust(left=0.11, right=0.67, top=0.91, bottom=0.11)
        self.draw()

    def update_plot(self, df: pd.DataFrame) -> None:
        ax = self._ax
        ax.cla()
        ax.grid(True, alpha=0.3)
        ax.set_xlabel("Vc_V  –  measured collector voltage (V)")
        ax.set_ylabel("Ic  (mA)")
        ax.set_title("Output Characteristics  (FA region)  ·  Early Voltage")

        if df.empty or "Transistor_State" not in df.columns:
            self.draw()
            return

        fa = df[df["Transistor_State"] == "FA"].copy()
        if fa.empty:
            self.draw()
            return

        vbe_vals = sorted(fa["Vbe_set"].unique()) if "Vbe_set" in fa.columns else [None]
        palette  = cm.viridis(np.linspace(0.1, 0.88, max(len(vbe_vals), 1)))
        cmap     = dict(zip(vbe_vals, palette))

        for vbe in vbe_vals:
            sub = (
                fa[fa["Vbe_set"] == vbe].sort_values("Vc_V")
                if vbe is not None
                else fa.sort_values("Vc_V")
            )
            sub = sub[sub["Ic_A"] > 0]
            if sub.empty:
                continue
            col = cmap.get(vbe, palette[0])
            lbl = f"Vbe={vbe:.2f} V" if vbe is not None else ""
            ax.plot(sub["Vc_V"], sub["Ic_A"] * 1e3,
                    "o-", color=col, ms=4, lw=1.2, label=lbl)

            # Dashed extrapolation to the x-axis for Early voltage visualisation.
            if len(sub) >= 3:
                vce    = sub["Vc_V"].to_numpy(dtype=float)
                ic     = sub["Ic_A"].to_numpy(dtype=float)
                coeffs = np.polyfit(vce, ic, 1)
                slope, intercept = coeffs
                if slope > 1e-15:
                    va_this = intercept / slope
                    if 1.0 < va_this < 1000.0:
                        x_ext = np.array([-va_this, float(vce[0])])
                        ax.plot(x_ext, np.polyval(coeffs, x_ext) * 1e3,
                                "--", color=col, lw=0.8, alpha=0.45)

        va_est = _fit_early_voltage(fa)
        if va_est is not None:
            ax.axvline(-va_est, color="gray", lw=1.2, ls=":",
                       alpha=0.8, label=f"−VAF ≈ {va_est:.1f} V")
            ax.set_xlim(left=min(ax.get_xlim()[0], -va_est * 1.1))

        ax.set_ylim(bottom=0)
        ax.legend(loc="upper left", bbox_to_anchor=(1.02, 1.0),
                  borderaxespad=0, fontsize=7, frameon=False)
        self.draw()

    def clear(self) -> None:
        self._init_axes()

    def _on_click(self, event) -> None:
        if event.inaxes is not self._ax or event.xdata is None or event.ydata is None:
            return
        self.plot_clicked.emit("output", float(event.xdata), float(event.ydata))


# ── Main GUI ──────────────────────────────────────────────────────────────────
class TransistorGui(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Transistor Characterisation")
        self.resize(1200, 760)

        self._df: pd.DataFrame = pd.DataFrame()
        self._tick = 0
        self._sweep_plan: list[tuple[float, float]] = []   # (Vbe_set, Vce_set)
        self._coarse_plan: list[tuple[float, float]] = []
        self._fine_plan: list[tuple[float, float]] = []
        self._fine_seen: set[tuple[float, float]] = set()
        self._replicate_fa_points: set[tuple[float, float]] = set()
        self._replicate_target = 1
        self._replicate_idx = 1
        self._phase = "idle"
        self._meas_count = 0
        self._logged_legacy_meas_warning = False
        self._last_fixture_failures: list[str] = []
        self._instrument = InstrumentSession(logger=self._log, baud=115200, timeout_s=5.0)
        self._scanner: DeviceScannerThread | None = None

        root = QWidget()
        self.setCentralWidget(root)
        layout = QHBoxLayout(root)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        layout.addWidget(self._build_left_panel(), 0)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setSpacing(6)
        rl.setContentsMargins(0, 0, 0, 0)

        self._canvas_gummel = GummelCanvas()
        self._canvas_beta   = BetaCanvas()
        self._canvas_output = OutputCanvas()
        for _c in (self._canvas_gummel, self._canvas_beta, self._canvas_output):
            _c.plot_clicked.connect(self._on_canvas_clicked)
        self._tabs = QTabWidget()
        self._tabs.addTab(self._canvas_gummel, "Gummel Plot")
        self._tabs.addTab(self._canvas_beta,   "Current Gain")
        self._tabs.addTab(self._canvas_output, "Output Characteristics")
        rl.addWidget(self._tabs, 1)

        self._param_lbl = QLabel("Run a sweep to see extracted parameters.")
        self._param_lbl.setObjectName("paramLbl")
        self._param_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._param_lbl.setFixedHeight(28)
        rl.addWidget(self._param_lbl, 0)

        layout.addWidget(right, 1)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll)

        self._scan_devices()
        self._apply_style()

    # ── Panel builders ────────────────────────────────────────────────────────
    def _build_left_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("leftPanel")
        panel.setFixedWidth(285)
        vl = QVBoxLayout(panel)
        vl.setSpacing(7)
        vl.setContentsMargins(8, 8, 8, 8)

        vl.addWidget(self._build_connection_group())
        vl.addWidget(self._build_type_group())
        vl.addWidget(self._build_sweep_group())
        vl.addWidget(self._build_run_group())
        vl.addWidget(self._build_console_group())
        vl.addStretch(1)
        return panel

    def _build_connection_group(self) -> QGroupBox:
        grp = QGroupBox("Arduino Connection")
        fl = QFormLayout(grp)
        fl.setContentsMargins(6, 14, 6, 8)
        fl.setSpacing(5)

        dev_row = QWidget()
        drl = QHBoxLayout(dev_row)
        drl.setContentsMargins(0, 0, 0, 0)
        drl.setSpacing(4)
        self.device_combo = QComboBox()
        self.device_combo.setFixedHeight(24)
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.setFixedSize(56, 24)
        drl.addWidget(self.device_combo, 1)
        drl.addWidget(self.refresh_btn)

        status_row = QWidget()
        srl = QHBoxLayout(status_row)
        srl.setContentsMargins(0, 0, 0, 0)
        srl.setSpacing(6)
        self.link_led = QFrame()
        self.link_led.setFixedSize(12, 12)
        self.link_led.setObjectName("linkLed")
        self.link_lbl = QLabel("Disconnected")
        srl.addWidget(self.link_led)
        srl.addWidget(self.link_lbl)
        srl.addStretch(1)

        btn_row = QWidget()
        brl = QHBoxLayout(btn_row)
        brl.setContentsMargins(0, 0, 0, 0)
        brl.setSpacing(6)
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setFixedHeight(24)
        self.disconnect_btn = QPushButton("Disconnect")
        self.disconnect_btn.setFixedHeight(24)
        self.disconnect_btn.setEnabled(False)
        brl.addWidget(self.connect_btn, 1)
        brl.addWidget(self.disconnect_btn, 1)

        fl.addRow("Device", dev_row)
        fl.addRow("Status", status_row)
        fl.addRow(btn_row)

        self.connect_btn.clicked.connect(self._connect)
        self.disconnect_btn.clicked.connect(self._disconnect)
        self.refresh_btn.clicked.connect(self._scan_devices)
        self._set_led(False)
        return grp

    def _build_type_group(self) -> QGroupBox:
        grp = QGroupBox("Transistor Type & Wiring")
        vl = QVBoxLayout(grp)
        vl.setContentsMargins(6, 14, 6, 8)
        vl.setSpacing(6)

        type_row = QWidget()
        trl = QHBoxLayout(type_row)
        trl.setContentsMargins(0, 0, 0, 0)
        trl.setSpacing(8)
        trl.addWidget(QLabel("Type:"))
        self.type_combo = QComboBox()
        self.type_combo.addItems(["NPN", "PNP"])
        self.type_combo.setFixedHeight(24)
        trl.addWidget(self.type_combo, 1)
        vl.addWidget(type_row)

        # Container for the 3 wiring rows
        self._wiring_box = QWidget()
        wl = QVBoxLayout(self._wiring_box)
        wl.setContentsMargins(2, 0, 2, 0)
        wl.setSpacing(1)
        vl.addWidget(self._wiring_box)

        self.type_combo.currentTextChanged.connect(self._refresh_wiring)
        self._refresh_wiring("NPN")
        return grp

    def _refresh_wiring(self, t: str = "") -> None:
        t = t or self.type_combo.currentText()
        rows = WIRING_TABLES.get(t, WIRING_TABLES["NPN"])
        layout = self._wiring_box.layout()
        # Clear previous rows
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for terminal, probe, colour in rows:
            row = QWidget()
            row.setFixedHeight(22)
            rl = QHBoxLayout(row)
            rl.setContentsMargins(4, 0, 4, 0)
            rl.setSpacing(0)
            t_lbl = QLabel(terminal)
            t_lbl.setFixedWidth(105)
            t_lbl.setStyleSheet("font-weight:600; font-size:9pt;")
            arrow = QLabel("→")
            arrow.setFixedWidth(18)
            arrow.setAlignment(Qt.AlignmentFlag.AlignCenter)
            p_lbl = QLabel(probe)
            p_lbl.setStyleSheet(
                f"font-weight:700; font-size:10pt; color:{colour};"
            )
            rl.addWidget(t_lbl)
            rl.addWidget(arrow)
            rl.addWidget(p_lbl, 1)
            layout.addWidget(row)

    def _build_sweep_group(self) -> QGroupBox:
        grp = QGroupBox("Test Strategy")
        vl = QVBoxLayout(grp)
        vl.setContentsMargins(8, 14, 8, 8)
        vl.setSpacing(4)

        points_per_axis = int(round((COARSE_VBE_STOP - COARSE_VBE_START) / COARSE_VBE_STEP)) + 1
        total_points = points_per_axis * points_per_axis

        lbl1 = QLabel("1) Fixed full sweep (hidden):")
        lbl2 = QLabel(
            f"   Vbe {COARSE_VBE_START:.1f}->{COARSE_VBE_STOP:.1f} V, step {COARSE_VBE_STEP:.1f} V\n"
            f"   Vce {COARSE_VCE_START:.1f}->{COARSE_VCE_STOP:.1f} V, step {COARSE_VCE_STEP:.1f} V"
        )
        lbl3 = QLabel(f"2) Total grid points: {total_points} ({points_per_axis} x {points_per_axis})")
        lbl5 = QLabel(f"Sample period: {SAMPLE_PERIOD_MS} ms")
        lbl6 = QLabel(f"ADC prescaler: {ADC_PRESCALE} (max)   |   FW averages: {FW_AVERAGES}")

        for l in (lbl1, lbl3):
            l.setStyleSheet("font-weight:700; font-size:9pt;")
        for l in (lbl2, lbl5, lbl6):
            l.setStyleSheet("font-size:8.5pt; color:#475569;")

        vl.addWidget(lbl1)
        vl.addWidget(lbl2)
        vl.addWidget(lbl3)
        vl.addWidget(lbl5)
        vl.addWidget(lbl6)

        return grp

    def _build_run_group(self) -> QGroupBox:
        grp = QGroupBox("Run")
        vl = QVBoxLayout(grp)
        vl.setContentsMargins(6, 14, 6, 8)
        vl.setSpacing(5)

        btn_row1 = QWidget()
        br1 = QHBoxLayout(btn_row1)
        br1.setContentsMargins(0, 0, 0, 0)
        br1.setSpacing(6)
        self.start_btn = QPushButton("▶  Start")
        self.stop_btn  = QPushButton("■  Stop")
        self.stop_btn.setEnabled(False)
        br1.addWidget(self.start_btn, 1)
        br1.addWidget(self.stop_btn, 1)

        btn_row2 = QWidget()
        br2 = QHBoxLayout(btn_row2)
        br2.setContentsMargins(0, 0, 0, 0)
        br2.setSpacing(6)
        self.save_btn  = QPushButton("Save CSV…")
        self.clear_btn = QPushButton("Clear")
        br2.addWidget(self.save_btn, 1)
        br2.addWidget(self.clear_btn, 1)

        rep_row = QWidget()
        rrl = QHBoxLayout(rep_row)
        rrl.setContentsMargins(0, 0, 0, 0)
        rrl.setSpacing(6)
        rep_lbl = QLabel("Replicates")
        self.reps_spin = QSpinBox()
        self.reps_spin.setRange(1, 10)
        self.reps_spin.setValue(1)
        self.reps_spin.setFixedHeight(24)
        self.reps_spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.PlusMinus)
        rrl.addWidget(rep_lbl)
        rrl.addWidget(self.reps_spin, 1)

        for btn in (self.start_btn, self.stop_btn, self.save_btn, self.clear_btn):
            btn.setFixedHeight(24)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFixedHeight(14)
        self.progress.setFormat("%p%")

        self.run_status_lbl = QLabel("Status: Idle")
        self.run_status_lbl.setObjectName("runStatusLbl")
        self.run_status_lbl.setFixedHeight(18)

        vl.addWidget(btn_row1)
        vl.addWidget(rep_row)
        vl.addWidget(btn_row2)
        vl.addWidget(self.progress)
        vl.addWidget(self.run_status_lbl)

        self.start_btn.clicked.connect(self._start)
        self.stop_btn.clicked.connect(self._stop)
        self.save_btn.clicked.connect(self._save_csv)
        self.clear_btn.clicked.connect(self._clear)
        return grp

    def _build_console_group(self) -> QGroupBox:
        grp = QGroupBox("Console")
        vl = QVBoxLayout(grp)
        vl.setContentsMargins(6, 14, 6, 8)
        self.console = QTextEdit()
        self.console.setReadOnly(True)
        self.console.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        mono = QFont("Cascadia Mono", 8)
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self.console.setFont(mono)
        lh = self.console.fontMetrics().lineSpacing()
        self.console.setFixedHeight(lh * 5 + 18)
        vl.addWidget(self.console)
        return grp

    # ── Sweep logic ───────────────────────────────────────────────────────────
    def _build_plan(
        self,
        vbe_start: float,
        vbe_stop: float,
        vbe_step: float,
        vce_start: float,
        vce_stop: float,
        vce_step: float,
        repetitions: int = 1,
        randomize: bool = False,
    ) -> list[tuple[float, float]]:
        """Return (Vbe_set, Vce_set) pairs: outer loop = Vce, inner = Vbe."""
        def _pts(start: float, stop: float, step: float) -> list[float]:
            pts: list[float] = []
            v = start
            while v <= stop + step * 0.01:
                pts.append(round(v, 6))
                v += step
                v = round(v, 9)
            return pts

        vbe_pts = _pts(vbe_start, vbe_stop, vbe_step)
        vce_pts = _pts(vce_start, vce_stop, vce_step)
        base = [(vbe, vce) for vce in vce_pts for vbe in vbe_pts]

        plan: list[tuple[float, float]] = []
        reps = max(1, int(repetitions))
        for _ in range(reps):
            batch = base.copy()
            if randomize:
                random.shuffle(batch)
            plan.extend(batch)
        return plan

    def _build_coarse_plan(self) -> list[tuple[float, float]]:
        return self._build_plan(
            COARSE_VBE_START,
            COARSE_VBE_STOP,
            COARSE_VBE_STEP,
            COARSE_VCE_START,
            COARSE_VCE_STOP,
            COARSE_VCE_STEP,
            repetitions=1,
            randomize=True,
        )

    def _build_replicate_plan(self) -> list[tuple[float, float]]:
        """Build one replicate pass over FA seed points from replicate #1."""
        out = sorted(self._replicate_fa_points, key=lambda t: (t[1], t[0]))
        random.shuffle(out)
        return out

    def _key(self, vbe: float, vce: float) -> tuple[float, float]:
        return (round(float(vbe), 4), round(float(vce), 4))

    def _build_fa_replicate_average_df(self, source_df: pd.DataFrame) -> pd.DataFrame:
        """Return dataframe where replicate FA setpoints are replaced by their mean rows."""
        if self._replicate_target <= 1 or not self._replicate_fa_points or source_df.empty:
            return source_df
        if "Replicate" not in source_df.columns:
            return source_df

        df = source_df.copy()
        df["_key"] = list(zip(df["Vbe_set"].round(4), df["Vce_set"].round(4)))
        avg_rows: list[dict] = []

        for key in sorted(self._replicate_fa_points, key=lambda t: (t[1], t[0])):
            sub = df[df["_key"] == key]
            if sub.empty:
                continue

            means = sub[MEAS_COLS].mean(numeric_only=True)
            row: dict = {
                "sample": float("nan"),
                "Vbe_set": key[0],
                "Vce_set": key[1],
                "Sweep_Phase": "rep_avg",
                "Replicate": self._replicate_target,
                "Replicate_Count": int(len(sub)),
            }
            for c in MEAS_COLS:
                row[c] = float(means.get(c, float("nan")))
            row["Transistor_State"] = _classify_row(row)
            avg_rows.append(row)

        base = df[(df["Replicate"] == 1) & (~df["_key"].isin(self._replicate_fa_points))].copy()
        base = base.drop(columns=["_key"], errors="ignore")
        if not avg_rows:
            return base
        return pd.concat([base, pd.DataFrame(avg_rows)], ignore_index=True)

    def _apply_fa_replicate_average(self) -> None:
        """Replace replicate-1 FA rows with averaged values across all replicates."""
        if self._replicate_target <= 1 or not self._replicate_fa_points or self._df.empty:
            return

        self._df = self._build_fa_replicate_average_df(self._df)
        avg_count = int(
            ((self._df.get("Sweep_Phase") == "rep_avg").sum())
            if "Sweep_Phase" in self._df.columns else 0
        )
        self._log(f"REPLICATES | averaged {avg_count} FA setpoints across {self._replicate_target} replicate(s)")

    def _build_refine_plan(self, coarse_df: pd.DataFrame) -> list[tuple[float, float]]:
        """Seed fine plan from orthogonal neighbors of coarse FA points."""
        if coarse_df.empty or "Transistor_State" not in coarse_df.columns:
            return []

        R = 4
        existing = {
            (round(float(r["Vbe_set"]), R), round(float(r["Vce_set"]), R))
            for _, r in coarse_df.iterrows()
        }
        targets: set[tuple[float, float]] = set()

        fa = coarse_df[coarse_df["Transistor_State"] == "FA"]
        for _, row in fa.iterrows():
            vbe = round(float(row["Vbe_set"]), R)
            vce = round(float(row["Vce_set"]), R)
            for dvbe, dvce in ((FINE_STEP_V, 0.0), (-FINE_STEP_V, 0.0), (0.0, FINE_STEP_V), (0.0, -FINE_STEP_V)):
                nvbe = round(vbe + dvbe, R)
                nvce = round(vce + dvce, R)
                if not (COARSE_VBE_START <= nvbe <= COARSE_VBE_STOP):
                    continue
                if not (COARSE_VCE_START <= nvce <= COARSE_VCE_STOP):
                    continue
                key = (nvbe, nvce)
                if key in existing:
                    continue
                targets.add(key)

        out = sorted(targets, key=lambda t: (t[1], t[0]))
        random.shuffle(out)
        return out

    def _enqueue_fine_neighbors(self, vbe: float, vce: float) -> int:
        """Add orthogonal fine neighbors for FA fan-out expansion."""
        R = 4
        vbe = round(float(vbe), R)
        vce = round(float(vce), R)
        candidates: list[tuple[float, float]] = []
        for dvbe, dvce in ((FINE_STEP_V, 0.0), (-FINE_STEP_V, 0.0), (0.0, FINE_STEP_V), (0.0, -FINE_STEP_V)):
            nvbe = round(vbe + dvbe, R)
            nvce = round(vce + dvce, R)
            if not (COARSE_VBE_START <= nvbe <= COARSE_VBE_STOP):
                continue
            if not (COARSE_VCE_START <= nvce <= COARSE_VCE_STOP):
                continue
            key = (nvbe, nvce)
            if key in self._fine_seen:
                continue
            candidates.append(key)
            self._fine_seen.add(key)

        random.shuffle(candidates)
        self._sweep_plan.extend(candidates)
        return len(candidates)

    def _start(self) -> None:
        self._df = pd.DataFrame()
        self._tick = 0
        self._meas_count = 0
        self._phase = "coarse"
        self._replicate_target = int(self.reps_spin.value())
        self._replicate_idx = 1
        self._replicate_fa_points = set()
        for _c in (self._canvas_gummel, self._canvas_beta, self._canvas_output):
            _c.clear()
        self._param_lbl.setText("Sweep running…")
        self._set_run_status("Fixture Test")

        if not self._instrument.is_connected:
            self._log("ABORT | device not connected")
            self._set_run_status("Aborted")
            return

        while True:
            fixture_ok = self._run_fixture_check()
            if fixture_ok:
                break

            msg = QMessageBox(self)
            msg.setWindowTitle("Fixture Check Failed")
            details = "\n".join(self._last_fixture_failures) if self._last_fixture_failures else "Unknown fixture issue"
            msg.setText(
                "Fixture check failed.\n"
                "Please verify transistor wiring and contact quality."
            )
            msg.setInformativeText("Retry fixture check or continue anyway?")
            msg.setDetailedText(details)
            retry_btn = msg.addButton("Retry", QMessageBox.ButtonRole.AcceptRole)
            continue_btn = msg.addButton("Continue", QMessageBox.ButtonRole.DestructiveRole)
            msg.exec()

            clicked = msg.clickedButton()
            if clicked is retry_btn:
                self._log("FIXTURE | user selected Retry")
                continue

            if clicked is continue_btn:
                self._log("FIXTURE | user selected Continue (proceeding despite failure)")
                break

            self._log("ABORT | fixture check dialog dismissed")
            self._set_run_status("Aborted")
            return

        self._coarse_plan = self._build_coarse_plan()
        self._fine_plan = []
        self._fine_seen = set()
        self._sweep_plan = self._coarse_plan
        if not self._sweep_plan:
            self._log("ABORT | empty sweep plan")
            self._set_run_status("Aborted")
            return

        self.progress.setValue(0)
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)

        # Force acquisition quality settings at start of each run.
        self._send_scpi(f"SENS:ADC:PRES {ADC_PRESCALE}")
        self._send_scpi(f"SENS:AVER:COUN {FW_AVERAGES}")
        self._log(f"SETUP | ADC prescaler={ADC_PRESCALE}, FW averages={FW_AVERAGES}")

        total = len(self._coarse_plan)
        vbe_pts = len(set(p[0] for p in self._sweep_plan))
        vce_pts = len(set(p[1] for p in self._sweep_plan))
        self._log(
            f"START | coarse phase: {total} points  Vbe×{vbe_pts}  Vce×{vce_pts}  "
            f"replicates={self._replicate_target}  random order enabled"
        )
        self._set_run_status("Coarse Measurements")
        self.timer.start(SAMPLE_PERIOD_MS)

    def _run_fixture_check(self) -> bool:
        """Apply three binary V1/V2 patterns and validate resulting currents/state."""
        patterns = [
            (0.0, 1.0, "OFF", "OFF expected"),
            (1.0, 0.0, "SAT", "SAT/ON expected"),
            (1.0, 1.0, "ON", "FA or SAT expected"),
        ]

        failures: list[str] = []
        self._log("FIXTURE CHECK | V1 V2 I1 I2 State")

        for v1, v2, expect, expect_note in patterns:
            self._send_scpi(f"SOUR1:VOLT {v1:.4f}")
            self._send_scpi(f"SOUR2:VOLT {v2:.4f}")
            time.sleep(MEAS_SETTLE_MS / 1000)
            rx = self._send_scpi("MEAS:ALL?", expect_response=True, timeout_s=self._timeout_s())
            if not rx:
                failures.append(f"V1={v1:.1f}, V2={v2:.1f}: no response")
                self._log(f"FIXTURE | {v1:.1f} {v2:.1f}  <no data>")
                continue

            vals = self._parse_meas(rx)
            if vals is None:
                failures.append(f"V1={v1:.1f}, V2={v2:.1f}: parse failed")
                self._log(f"FIXTURE | {v1:.1f} {v2:.1f}  <parse error>")
                continue

            row = {
                "Vb_V": vals[0],
                "Ib_A": vals[1],
                "Vc_V": vals[2],
                "Ic_A": vals[3],
                "Ie_A": vals[4],
            }
            i1 = float(vals[1])
            i2 = float(vals[3])
            state = _classify_row(row)
            self._log(f"FIXTURE | {v1:.1f} {v2:.1f}  {i1:+.6f}  {i2:+.6f}  {state}")

            if expect == "OFF":
                # Only check base current and state; collector leakage (Icbo)
                # is expected when Vce is applied with no base drive.
                if abs(i1) > FIXTURE_OFF_CURRENT_A or state != "OFF":
                    failures.append(
                        f"V1={v1:.1f}, V2={v2:.1f}: expected OFF, got state={state}, I1={i1:.3e}, I2={i2:.3e}"
                    )
            elif expect == "SAT":
                if state == "OFF" or abs(i1) < FIXTURE_ON_CURRENT_A:
                    failures.append(
                        f"V1={v1:.1f}, V2={v2:.1f}: expected SAT/ON, got state={state}, I1={i1:.3e}, I2={i2:.3e}"
                    )
            elif expect == "ON":
                if state not in {"FA", "SAT"} or abs(i2) < FIXTURE_ON_CURRENT_A:
                    failures.append(
                        f"V1={v1:.1f}, V2={v2:.1f}: expected FA/SAT, got state={state}, I1={i1:.3e}, I2={i2:.3e}"
                    )

            if expect_note:
                self._log(f"FIXTURE | expectation: {expect_note}")

        self._send_scpi("SOUR1:VOLT 0.0000")
        self._send_scpi("SOUR2:VOLT 0.0000")

        self._last_fixture_failures = failures
        if failures:
            self._log("FIXTURE CHECK | FAIL")
            return False

        self._log("FIXTURE CHECK | PASS")
        return True

    def _stop(self, status_text: str = "Stopped") -> None:
        self.timer.stop()
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self._phase = "idle"
        self._set_run_status(status_text)
        self._log(f"STOP | {self._meas_count} measurements")

    def _clear(self) -> None:
        self._df = pd.DataFrame()
        self._replicate_fa_points = set()
        for _c in (self._canvas_gummel, self._canvas_beta, self._canvas_output):
            _c.clear()
        self._param_lbl.setText("Run a sweep to see extracted parameters.")
        self._set_run_status("Idle")
        self._log("CLEAR")

    def _set_run_status(self, text: str) -> None:
        self.run_status_lbl.setText(f"Status: {text}")

    def _save_csv(self) -> None:
        if self._df.empty:
            self._log("SAVE | no data")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save CSV", "", "CSV files (*.csv);;All files (*)"
        )
        if not path:
            return
        if not path.lower().endswith(".csv"):
            path += ".csv"
        self._df.to_csv(path, index=False)
        self._log(f"SAVE | {path}  ({len(self._df)} rows)")

    # ── Polling / measurement ─────────────────────────────────────────────────
    def _poll(self) -> None:
        if self._tick >= len(self._sweep_plan):
            if self._phase == "coarse":
                self._fine_plan = self._build_refine_plan(self._df)
                if self._fine_plan:
                    self._fine_seen = {
                        (round(float(r["Vbe_set"]), 4), round(float(r["Vce_set"]), 4))
                        for _, r in self._df.iterrows()
                    }
                    self._fine_seen.update(self._fine_plan)
                    self._log(
                        f"COARSE done | {self._meas_count} pts  →  "
                        f"fine phase: {len(self._fine_plan)} pts"
                    )
                    self._phase = "fine"
                    self._set_run_status("Fine Measurements")
                    self._sweep_plan = self._fine_plan
                    self._tick = 0
                    self.progress.setValue(0)
                    return
                self._log("COARSE done | no steep regions found, skipping fine phase")

            if self._phase in {"coarse", "fine"}:
                if self._replicate_idx == 1:
                    first = self._df[self._df.get("Replicate", 1) == 1]
                    fa = first[first["Transistor_State"] == "FA"]
                    self._replicate_fa_points = {
                        self._key(r["Vbe_set"], r["Vce_set"]) for _, r in fa.iterrows()
                    }
                if self._replicate_idx < self._replicate_target and self._replicate_fa_points:
                    self._replicate_idx += 1
                    self._phase = "replicate"
                    self._sweep_plan = self._build_replicate_plan()
                    self._tick = 0
                    self.progress.setValue(0)
                    self._set_run_status(f"FA Replication {self._replicate_idx}/{self._replicate_target}")
                    self._log(
                        f"REPLICATE {self._replicate_idx}/{self._replicate_target} | "
                        f"rerun {len(self._sweep_plan)} FA points"
                    )
                    return

            if self._phase == "replicate":
                if self._replicate_idx < self._replicate_target and self._replicate_fa_points:
                    self._replicate_idx += 1
                    self._sweep_plan = self._build_replicate_plan()
                    self._tick = 0
                    self.progress.setValue(0)
                    self._set_run_status(f"FA Replication {self._replicate_idx}/{self._replicate_target}")
                    self._log(
                        f"REPLICATE {self._replicate_idx}/{self._replicate_target} | "
                        f"rerun {len(self._sweep_plan)} FA points"
                    )
                    return

            self._apply_fa_replicate_average()
            self._canvas_gummel.update_plot(self._df)
            self._canvas_beta.update_plot(self._df)
            self._canvas_output.update_plot(self._df)
            self._update_params(self._df)
            self._log(f"DONE | {self._meas_count} points collected ({self._phase} phase complete)")
            self._save_debug_surface_plots()
            self._phase = "done"
            self._stop("Completed")
            return

        vbe_set, vce_set = self._sweep_plan[self._tick]
        self._tick += 1

        vals = self._measure_point(vbe_set, vce_set, stop_on_error=True)
        if vals is None:
            # Keep progress responsive even if a line is malformed.
            self.progress.setValue(int(self._tick / len(self._sweep_plan) * 100))
            return

        row: dict = {"sample": float(self._tick), "Vbe_set": vbe_set, "Vce_set": vce_set}
        row.update(dict(zip(MEAS_COLS, vals)))
        row["Transistor_State"] = _classify_row(row)
        row["Sweep_Phase"] = self._phase
        row["Replicate"] = self._replicate_idx

        self._df = pd.concat([self._df, pd.DataFrame([row])], ignore_index=True)

        if self._phase == "fine" and row["Transistor_State"] == "FA":
            added = self._enqueue_fine_neighbors(vbe_set, vce_set)
            if added:
                self._log(f"FINE EXPAND | seed=({vbe_set:.2f},{vce_set:.2f}) added {added} neighbors")

        plot_df = self._df
        if self._phase == "replicate" and self._replicate_target > 1:
            plot_df = self._build_fa_replicate_average_df(self._df)

        self.progress.setValue(int(self._tick / len(self._sweep_plan) * 100))
        self._canvas_gummel.update_plot(plot_df)
        self._canvas_beta.update_plot(plot_df)
        self._canvas_output.update_plot(plot_df)
        self._update_params(plot_df)

    def _on_canvas_clicked(self, axis_name: str, x_click: float, y_click: float) -> None:
        if self.timer.isActive():
            self._log("CLICK MEASURE | ignored while sweep is running")
            return
        if not self._instrument.is_connected:
            self._log("CLICK MEASURE | device not connected")
            return
        if self._df.empty:
            self._log("CLICK MEASURE | no data to anchor click")
            return

        if axis_name == "beta":
            ib_uA = self._df["Ib_A"].to_numpy(dtype=float) * 1e6
            ic_mA = self._df["Ic_A"].to_numpy(dtype=float) * 1e3
            valid = np.isfinite(ib_uA) & np.isfinite(ic_mA)
            if not np.any(valid):
                self._log("CLICK MEASURE | no valid beta points")
                return
            idx_valid = np.where(valid)[0]
            dist2 = (ib_uA[valid] - x_click) ** 2 + (ic_mA[valid] - y_click) ** 2
            row_idx = int(idx_valid[int(np.argmin(dist2))])
        elif axis_name == "output":
            vc  = self._df["Vc_V"].to_numpy(dtype=float)
            ic  = self._df["Ic_A"].to_numpy(dtype=float)
            valid = np.isfinite(vc) & np.isfinite(ic) & (ic > 0)
            if not np.any(valid):
                self._log("CLICK MEASURE | no valid output points")
                return
            idx_valid = np.where(valid)[0]
            dist2 = (vc[valid] - x_click) ** 2 + (ic[valid] * 1e3 - y_click) ** 2
            row_idx = int(idx_valid[int(np.argmin(dist2))])
        else:  # gummel
            vb = self._df["Vb_V"].to_numpy(dtype=float)
            ic = self._df["Ic_A"].to_numpy(dtype=float)
            valid = np.isfinite(vb) & np.isfinite(ic) & (ic > 0)
            if not np.any(valid):
                self._log("CLICK MEASURE | no valid gummel points")
                return
            idx_valid = np.where(valid)[0]
            dist2 = (vb[valid] - x_click) ** 2 + (np.log10(ic[valid]) - np.log10(max(y_click, 1e-15))) ** 2
            row_idx = int(idx_valid[int(np.argmin(dist2))])

        vbe_set = float(self._df.at[row_idx, "Vbe_set"])
        vce_set = float(self._df.at[row_idx, "Vce_set"])
        self._log(f"CLICK MEASURE | nearest row={row_idx + 1} at Vbe_set={vbe_set:.4f}, Vce_set={vce_set:.4f}")

        vals = self._measure_point(vbe_set, vce_set, stop_on_error=False)
        if vals is None:
            return

        sample_idx = len(self._df) + 1
        row: dict = {"sample": float(sample_idx), "Vbe_set": vbe_set, "Vce_set": vce_set}
        row.update(dict(zip(MEAS_COLS, vals)))
        row["Transistor_State"] = _classify_row(row)
        row["Sweep_Phase"] = "manual_click"
        row["Replicate"] = self._replicate_idx
        self._df = pd.concat([self._df, pd.DataFrame([row])], ignore_index=True)

        self._canvas_gummel.update_plot(self._df)
        self._canvas_beta.update_plot(self._df)
        self._canvas_output.update_plot(self._df)
        self._update_params(self._df)

    def _measure_point(self, vbe_set: float, vce_set: float, *, stop_on_error: bool) -> tuple | None:
        # V1 = Base (Vbe), V2 = Collector (Vce)
        self._send_scpi(f"SOUR1:VOLT {vbe_set:.4f}")
        self._send_scpi(f"SOUR2:VOLT {vce_set:.4f}")
        time.sleep(MEAS_SETTLE_MS / 1000)

        rx = self._send_scpi("MEAS:ALL?", expect_response=True, timeout_s=self._timeout_s())
        if not rx:
            self._log("ERROR | no response to MEAS:ALL?")
            if stop_on_error:
                self._stop("Error")
            return None
        self._meas_count += 1

        vals = self._parse_meas(rx)
        if vals is None and stop_on_error:
            self._stop("Error")
        return vals

    def _update_params(self, source_df: pd.DataFrame | None = None) -> None:
        """Recompute and display n, Is, β from current FA data."""
        df = self._df if source_df is None else source_df
        if "Transistor_State" not in df.columns:
            return
        fa = df[df["Transistor_State"] == "FA"]
        if len(fa) < 4:
            return

        n_c, Is_c, r2_c = _fit_diode(
            fa["Vb_V"].values, fa["Ic_A"].values
        )
        fa_both = fa[(fa["Ic_A"] > 0) & (fa["Ib_A"] > 0)].copy()
        if fa_both.empty:
            return
        fa_both = fa_both.copy()
        fa_both["Beta"] = fa_both["Ic_A"] / fa_both["Ib_A"]
        beta_med = fa_both["Beta"].median()

        parts: list[str] = [f"FA points: {len(fa)}"]
        if n_c is not None:
            parts.append(f"n = {n_c:.2f}")
            parts.append(f"Is = {Is_c:.2e} A")
        parts.append(f"β median = {beta_med:.1f}")
        self._param_lbl.setText("   |   ".join(parts))

    def _parse_meas(self, line: str) -> tuple | None:
        parts = [p.strip() for p in line.split(",")]
        try:
            if len(parts) == MEAS_VALUE_COUNT:
                return tuple(float(parts[i]) for i in range(MEAS_VALUE_COUNT))

            self._log(f"ERROR | bad MEAS:ALL? ({len(parts)} values, expected {MEAS_VALUE_COUNT})")
            return None
        except ValueError:
            self._log("ERROR | non-numeric MEAS:ALL? response")
            return None

    def _timeout_s(self) -> float:
        """Estimate MEAS:ALL? timeout from forced prescaler/average settings."""
        conv_s = ADC_PRESCALE / 16e6 * 13
        # 8 channels, readAnalogVolts() does throw-away + kept sample => 16 conversions.
        estimated = FW_AVERAGES * 16 * conv_s
        return max(2.0, estimated * 2 + 0.5)

    def _save_debug_surface_plots(self) -> None:
        """Save one V1/V2 topographic map per measurement channel for debug."""
        if self._df.empty:
            return

        required = {"Vbe_set", "Vce_set"}
        if not required.issubset(set(self._df.columns)):
            self._log("DEBUG TOPO | skipped (missing required columns)")
            return

        out_dir = Path("plots") / "topo_debug" / datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir.mkdir(parents=True, exist_ok=True)

        x = self._df["Vbe_set"].to_numpy(dtype=float)  # V1 set
        y = self._df["Vce_set"].to_numpy(dtype=float)  # V2 set

        for col in MEAS_COLS:
            if col not in self._df.columns:
                continue

            z = self._df[col].to_numpy(dtype=float)
            if len(z) < 3:
                continue

            valid = np.isfinite(x) & np.isfinite(y) & np.isfinite(z)
            xv = x[valid]
            yv = y[valid]
            zv = z[valid]
            if len(zv) < 3:
                continue

            fig = Figure(figsize=(8.5, 6.5))
            ax = fig.add_subplot(111)

            try:
                filled = ax.tricontourf(xv, yv, zv, levels=20, cmap="viridis")
                ax.tricontour(xv, yv, zv, levels=10, colors="k", linewidths=0.35, alpha=0.45)
                fig.colorbar(filled, ax=ax, shrink=0.9, pad=0.03, label=col)
            except Exception:
                pass

            # Show measured samples explicitly as small dots.
            ax.scatter(xv, yv, c=zv, cmap="viridis", s=10, linewidths=0.2, edgecolors="black", alpha=0.9)

            ax.set_title(f"{col} topo map vs V1/V2 setpoints")
            ax.set_xlabel("V1 set = Vbe_set (V)")
            ax.set_ylabel("V2 set = Vce_set (V)")
            ax.set_xlim(COARSE_VBE_START, COARSE_VBE_STOP)
            ax.set_ylim(COARSE_VCE_START, COARSE_VCE_STOP)
            ax.grid(True, alpha=0.2)

            out_path = out_dir / f"topo_{col}.png"
            fig.savefig(out_path, dpi=140, bbox_inches="tight")

        self._log(f"DEBUG TOPO | saved to {out_dir}")

    # ── Serial helpers ────────────────────────────────────────────────────────
    def _log(self, text: str) -> None:
        self.console.append(text)
        cursor = self.console.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self.console.setTextCursor(cursor)

    def _connect(self) -> None:
        port = self.device_combo.currentData()
        if not port:
            self._log("ERROR | no device selected")
            return
        self._open_serial(port)

    def _disconnect(self) -> None:
        self._close_serial()
        self._log("LINK CLOSED")

    def _scan_devices(self) -> None:
        if self._scanner is not None and self._scanner.isRunning():
            return
        self._scanner = DeviceScannerThread()
        self._scanner.devices_found.connect(self._on_devices_found)
        self._scanner.start()

    def _on_devices_found(self, devices: list[tuple[str, str]]) -> None:
        self.device_combo.clear()
        if not devices:
            self.device_combo.addItem("No devices found", None)
            return
        for port, desc in devices:
            self.device_combo.addItem(f"{port}  ({desc})", port)

    def _open_serial(self, port: str) -> bool:
        if self._instrument.is_connected:
            return True
        if not serial_is_available():
            self._log("ERROR | pyserial not installed")
            return False
        try:
            self._instrument.open(port)
            self._set_led(True)
            self.link_lbl.setText("Connected")
            self.connect_btn.setEnabled(False)
            self.disconnect_btn.setEnabled(True)
            self._log(f"LINK OPEN | {port}")
            return True
        except Exception as exc:
            self._set_led(False)
            self.link_lbl.setText("Disconnected")
            self._log(f"ERROR | {exc}")
            return False

    def _close_serial(self) -> None:
        self._instrument.close()
        self._set_led(False)
        self.link_lbl.setText("Disconnected")
        self.connect_btn.setEnabled(True)
        self.disconnect_btn.setEnabled(False)

    def _send_scpi(
        self,
        cmd: str,
        expect_response: bool = False,
        timeout_s: float | None = None,
    ) -> str:
        line = self._instrument.send_scpi(
            cmd,
            expect_response=expect_response,
            timeout_s=timeout_s if timeout_s is not None else self._timeout_s(),
        )
        if not self._instrument.is_connected and self.link_lbl.text() == "Connected":
            self._close_serial()
        return line

    # ── Misc ──────────────────────────────────────────────────────────────────
    def _set_led(self, on: bool) -> None:
        colour = "#22c55e" if on else "#1e293b"
        self.link_led.setStyleSheet(
            f"background:{colour}; border:1px solid #0f172a; border-radius:6px;"
        )

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget {
                background: #eef3f8;
                color: #1e293b;
                font-family: 'Segoe UI', sans-serif;
                font-size: 10pt;
            }
            #leftPanel {
                background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
                    stop:0 #ffffff, stop:1 #f4f7fb);
                border: 1px solid #d8e1ea;
                border-radius: 10px;
            }
            QGroupBox {
                border: 1px solid #d8e1ea;
                border-radius: 8px;
                margin-top: 8px;
                font-weight: 600;
                background: #ffffff;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 8px;
                padding: 0 4px;
                color: #334155;
            }
            QPushButton {
                background: #0b7285;
                border: none;
                border-radius: 6px;
                color: white;
                font-weight: 600;
                padding: 4px 8px;
                font-size: 9pt;
            }
            QPushButton:hover:!disabled { background: #0d849a; }
            QPushButton:disabled        { background: #9fb7bf; color: #dce9ec; }
            QDoubleSpinBox, QSpinBox, QComboBox {
                background: #f8fafc;
                border: 1px solid #cfdbe7;
                border-radius: 5px;
                padding: 2px 4px;
                font-size: 9pt;
            }
            QLabel { font-size: 9pt; }
            QTextEdit {
                background: #0f172a;
                color: #d8f3dc;
                border-radius: 6px;
                border: 1px solid #24364a;
                padding: 4px;
            }
            QProgressBar {
                border: 1px solid #cfdbe7;
                border-radius: 5px;
                background: #e8f0f6;
                text-align: center;
                font-size: 8pt;
                font-weight: 600;
            }
            QProgressBar::chunk { background: #0b7285; border-radius: 4px; }
            #runStatusLbl {
                font-size: 8.5pt;
                font-weight: 600;
                color: #334155;
                padding-left: 2px;
            }
            #paramLbl {
                background: #f0f7ff;
                border: 1px solid #bcd4e8;
                border-radius: 6px;
                padding: 4px 10px;
                font-size: 9pt;
                font-weight: 600;
                color: #1e3a5f;
            }
            """
        )

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._close_serial()
        if self._scanner is not None:
            self._scanner.quit()
            self._scanner.wait()
        super().closeEvent(event)


# ── Entry point ───────────────────────────────────────────────────────────────
def main() -> None:
    app = QApplication(sys.argv)
    import pyqtgraph as pg
    pg.setConfigOptions(antialias=True)
    win = TransistorGui()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
