"""Modern control GUI for component test equipment.

Features:
- Dense sweep controls for V1 and V2 with linked step size <-> number of steps math
- Large plotting area
- Monospace 6-line console-style TX/RX log window
"""

from __future__ import annotations

import math
import random
import sys
import time
from dataclasses import dataclass

import numpy as np
import pandas as pd
import pyqtgraph as pg
from PySide6.QtCore import QThread, QTimer, Qt, Signal
from sweep_plot import SweepPlotWidget
from instrument_client import InstrumentSession, list_serial_devices, serial_is_available
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QProgressBar,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

# Number of scalar values returned by MEAS:ALL?
MEAS_VALUE_COUNT = 7


@dataclass
class SweepValues:
    enabled: bool
    start: float
    stop: float
    step_size: float
    steps: int


@dataclass(frozen=True)
class SourceLimits:
    min_v: float
    max_v: float


@dataclass(frozen=True)
class MeasuredPoint:
    v1_set: float
    v2_set: float
    values: tuple[float, ...]


class DeviceScannerThread(QThread):
    """Background thread to scan for USB serial devices without blocking UI."""
    devices_found = Signal(list)  # list of (port, description) tuples

    def run(self) -> None:
        self.devices_found.emit(list_serial_devices())


class SweepControl(QGroupBox):
    """Reusable sweep control block with linked step size and steps fields."""

    def __init__(self, title: str, default_signal_name: str) -> None:
        super().__init__(title)
        self._updating = False
        self._default_signal_name = default_signal_name

        self.enable_cb = QCheckBox("Sweep")
        self.enable_cb.setChecked(True)

        self.signal_name_edit = QLineEdit()
        self.signal_name_edit.setText(default_signal_name)
        self.signal_name_edit.setMaxLength(16)
        self.signal_name_edit.setFixedHeight(24)

        self.start_spin = QDoubleSpinBox()
        self.start_spin.setRange(-1000.0, 1000.0)
        self.start_spin.setDecimals(4)
        self.start_spin.setSingleStep(0.1)
        self.start_spin.setValue(0.0)

        self.stop_spin = QDoubleSpinBox()
        self.stop_spin.setRange(-1000.0, 1000.0)
        self.stop_spin.setDecimals(4)
        self.stop_spin.setSingleStep(0.1)
        self.stop_spin.setValue(5.0)

        self.step_size_spin = QDoubleSpinBox()
        self.step_size_spin.setRange(0.0001, 1000.0)
        self.step_size_spin.setDecimals(4)
        self.step_size_spin.setSingleStep(0.05)
        self.step_size_spin.setValue(1.0)

        self.steps_spin = QSpinBox()
        self.steps_spin.setRange(2, 1_000_000)
        self.steps_spin.setValue(6)

        for spin in (self.start_spin, self.stop_spin, self.step_size_spin, self.steps_spin):
            spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
            spin.setFixedHeight(24)

        self.enable_cb.setFixedHeight(22)

        self.points_label = QLabel("Points: 11")
        self.points_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.points_label.setFixedHeight(16)

        layout = QGridLayout()
        layout.setContentsMargins(6, 8, 6, 6)
        layout.setHorizontalSpacing(8)
        layout.setVerticalSpacing(2)
        layout.addWidget(self.enable_cb, 0, 0, 1, 2)
        layout.addWidget(QLabel("Signal"), 1, 0)
        layout.addWidget(self.signal_name_edit, 1, 1)
        layout.addWidget(QLabel("Start"), 2, 0)
        layout.addWidget(self.start_spin, 2, 1)
        layout.addWidget(QLabel("Stop"), 3, 0)
        layout.addWidget(self.stop_spin, 3, 1)
        layout.addWidget(QLabel("Step Size"), 4, 0)
        layout.addWidget(self.step_size_spin, 4, 1)
        layout.addWidget(QLabel("# Steps"), 5, 0)
        layout.addWidget(self.steps_spin, 5, 1)
        layout.addWidget(self.points_label, 6, 0, 1, 2)
        self.setLayout(layout)

        self.start_spin.valueChanged.connect(self._recalc_from_step_size)
        self.stop_spin.valueChanged.connect(self._recalc_from_step_size)
        self.step_size_spin.valueChanged.connect(self._recalc_from_step_size)
        self.steps_spin.valueChanged.connect(self._recalc_from_steps)

        self._recalc_from_step_size()

    def signal_token(self) -> str:
        raw = self.signal_name_edit.text().strip()
        if not raw:
            raw = self._default_signal_name
        token = "".join(ch for ch in raw if ch.isalnum() or ch == "_")
        return token or self._default_signal_name

    def _span(self) -> float:
        return abs(self.stop_spin.value() - self.start_spin.value())

    def _recalc_from_step_size(self) -> None:
        if self._updating:
            return
        self._updating = True
        try:
            span = self._span()
            step = max(self.step_size_spin.value(), 0.0001)
            steps = int(math.floor(span / step)) + 1
            steps = max(2, steps)
            self.steps_spin.setValue(steps)
            self.points_label.setText(f"Points: {steps}")
        finally:
            self._updating = False

    def _recalc_from_steps(self) -> None:
        if self._updating:
            return
        self._updating = True
        try:
            span = self._span()
            steps = max(self.steps_spin.value(), 2)
            step = span / (steps - 1) if steps > 1 else span
            step = max(step, 0.0001)
            self.step_size_spin.setValue(step)
            self.points_label.setText(f"Points: {steps}")
        finally:
            self._updating = False

    def values(self) -> SweepValues:
        return SweepValues(
            enabled=self.enable_cb.isChecked(),
            start=self.start_spin.value(),
            stop=self.stop_spin.value(),
            step_size=self.step_size_spin.value(),
            steps=self.steps_spin.value(),
        )


class InstrumentGui(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Component Tester Console")
        self.resize(1280, 820)

        self._df: pd.DataFrame = pd.DataFrame()
        self._tick = 0
        self._sample_offset = 0
        self._sweep_plan: list[tuple[float, float]] = []
        self._meas_all_count = 0
        self._instrument = InstrumentSession(logger=self._log, baud=115200, timeout_s=5.0)
        self._device_scanner_thread = None
        self._setpoint_columns: list[str] = []
        self._measurement_columns: list[str] = []
        self._plot_columns: list[str] = []
        self._source_limits: dict[int, SourceLimits] = {
            1: SourceLimits(0.0, 5.0),
            2: SourceLimits(0.0, 5.0),
        }

        root = QWidget()
        self.setCentralWidget(root)

        main_layout = QHBoxLayout(root)
        main_layout.setContentsMargins(12, 12, 12, 12)
        main_layout.setSpacing(10)

        controls_panel = QFrame()
        controls_panel.setObjectName("controlsPanel")
        controls_panel.setMinimumWidth(300)
        controls_layout = QVBoxLayout(controls_panel)
        controls_layout.setContentsMargins(6, 6, 6, 6)
        controls_layout.setSpacing(6)

        title = QLabel("Sweep Control")
        title.setObjectName("panelTitle")

        self.v1_sweep = SweepControl("V1", "1")
        self.v2_sweep = SweepControl("V2", "2")

        run_group = QGroupBox("Run")
        run_layout = QGridLayout(run_group)
        run_layout.setContentsMargins(6, 8, 6, 6)
        run_layout.setHorizontalSpacing(6)
        run_layout.setVerticalSpacing(4)
        self.start_btn = QPushButton("Start")
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        self.clear_btn = QPushButton("Clear Plot")
        self.save_csv_btn = QPushButton("Save CSV…")
        self.avg_spin = QSpinBox()
        self.avg_spin.setRange(1, 1000)
        self.avg_spin.setValue(1)
        self.avg_spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.avg_spin.setFixedHeight(24)
        self.repeat_spin = QSpinBox()
        self.repeat_spin.setRange(1, 1000)
        self.repeat_spin.setValue(1)
        self.repeat_spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.repeat_spin.setFixedHeight(24)
        run_layout.addWidget(self.start_btn, 0, 0)
        run_layout.addWidget(self.stop_btn, 0, 1)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFixedHeight(14)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFormat("%p%")
        run_layout.addWidget(self.progress_bar, 1, 0, 1, 2)
        run_layout.addWidget(self.clear_btn, 2, 0)
        run_layout.addWidget(self.save_csv_btn, 2, 1)
        run_layout.addWidget(QLabel("Avg Points"), 3, 0)
        run_layout.addWidget(self.avg_spin, 3, 1)
        run_layout.addWidget(QLabel("Repetitions"), 4, 0)
        run_layout.addWidget(self.repeat_spin, 4, 1)
        self.adc_pres_combo = QComboBox()
        for v in (2, 4, 8, 16, 32, 64, 128):
            self.adc_pres_combo.addItem(str(v), v)
        self.adc_pres_combo.setCurrentText("128")
        self.adc_pres_combo.setFixedHeight(24)
        self.adc_pres_btn = QPushButton("Set")
        self.adc_pres_btn.setFixedHeight(24)
        run_layout.addWidget(QLabel("ADC Prescale"), 5, 0)
        adc_pres_row = QWidget()
        adc_pres_row_layout = QHBoxLayout(adc_pres_row)
        adc_pres_row_layout.setContentsMargins(0, 0, 0, 0)
        adc_pres_row_layout.setSpacing(4)
        adc_pres_row_layout.addWidget(self.adc_pres_combo, 1)
        adc_pres_row_layout.addWidget(self.adc_pres_btn)
        run_layout.addWidget(adc_pres_row, 5, 1)
        self.fw_avg_spin = QSpinBox()
        self.fw_avg_spin.setRange(1, 255)
        self.fw_avg_spin.setValue(255)
        self.fw_avg_spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.fw_avg_spin.setFixedHeight(24)
        self.fw_avg_btn = QPushButton("Set")
        self.fw_avg_btn.setFixedHeight(24)
        run_layout.addWidget(QLabel("FW Averages"), 6, 0)
        fw_avg_row = QWidget()
        fw_avg_row_layout = QHBoxLayout(fw_avg_row)
        fw_avg_row_layout.setContentsMargins(0, 0, 0, 0)
        fw_avg_row_layout.setSpacing(4)
        fw_avg_row_layout.addWidget(self.fw_avg_spin, 1)
        fw_avg_row_layout.addWidget(self.fw_avg_btn)
        run_layout.addWidget(fw_avg_row, 6, 1)
        self.random_order_cb = QCheckBox("Random Order")
        self.random_order_cb.setChecked(True)
        self.random_order_cb.setFixedHeight(22)
        run_layout.addWidget(self.random_order_cb, 7, 0, 1, 2)

        comms_group = QGroupBox("Instrument IO")
        comms_form = QFormLayout(comms_group)
        comms_form.setContentsMargins(6, 8, 6, 6)
        comms_form.setHorizontalSpacing(8)
        comms_form.setVerticalSpacing(4)
        self.tx_period_ms = QSpinBox()
        self.tx_period_ms.setRange(20, 5000)
        self.tx_period_ms.setValue(250)
        self.tx_period_ms.setSuffix(" ms")
        self.tx_period_ms.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.tx_period_ms.setFixedHeight(24)

        device_selector_widget = QWidget()
        device_selector_layout = QHBoxLayout(device_selector_widget)
        device_selector_layout.setContentsMargins(0, 0, 0, 0)
        device_selector_layout.setSpacing(4)
        self.device_combo = QComboBox()
        self.device_combo.setFixedHeight(24)
        self.refresh_devices_btn = QPushButton("Refresh")
        self.refresh_devices_btn.setFixedHeight(24)
        self.refresh_devices_btn.setFixedWidth(70)
        device_selector_layout.addWidget(self.device_combo, 1)
        device_selector_layout.addWidget(self.refresh_devices_btn)

        link_status_widget = QWidget()
        link_status_layout = QHBoxLayout(link_status_widget)
        link_status_layout.setContentsMargins(0, 0, 0, 0)
        link_status_layout.setSpacing(6)
        self.link_led = QFrame()
        self.link_led.setFixedSize(12, 12)
        self.link_led.setObjectName("linkLed")
        self.link_state_label = QLabel("Disconnected")
        link_status_layout.addWidget(self.link_led)
        link_status_layout.addWidget(self.link_state_label)
        link_status_layout.addStretch(1)

        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setFixedHeight(24)
        self.disconnect_btn = QPushButton("Disconnect")
        self.disconnect_btn.setFixedHeight(24)
        self.disconnect_btn.setEnabled(False)

        comms_form.addRow("Sample Period", self.tx_period_ms)
        comms_form.addRow("Device", device_selector_widget)
        comms_form.addRow("Link", link_status_widget)
        comms_form.addRow(self.connect_btn, self.disconnect_btn)

        for btn in (self.start_btn, self.stop_btn, self.clear_btn, self.save_csv_btn):
            btn.setFixedHeight(24)

        controls_layout.addWidget(title)
        controls_layout.addWidget(comms_group)
        controls_layout.addWidget(self.v1_sweep)
        controls_layout.addWidget(self.v2_sweep)
        controls_layout.addWidget(run_group)
        controls_layout.addStretch(1)

        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setSpacing(10)

        self._refresh_column_schema()
        self.plot_panel = SweepPlotWidget(self._plot_columns)

        console_group = QGroupBox("Console")
        console_layout = QVBoxLayout(console_group)
        self.console = QTextEdit()
        self.console.setReadOnly(True)
        self.console.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        mono = QFont("Cascadia Mono", 10)
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self.console.setFont(mono)

        row_height = self.console.fontMetrics().lineSpacing()
        self.console.setFixedHeight((row_height * 6) + 18)

        console_layout.addWidget(self.console)

        right_layout.addWidget(self.plot_panel, 1)
        right_layout.addWidget(console_group, 0)

        main_layout.addWidget(controls_panel, 0)
        main_layout.addWidget(right_panel, 1)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll_instrument)

        self.start_btn.clicked.connect(self._start)
        self.stop_btn.clicked.connect(self._stop)
        self.clear_btn.clicked.connect(self._clear_plot)
        self.save_csv_btn.clicked.connect(self._save_csv)
        self.adc_pres_btn.clicked.connect(self._send_adc_prescale)
        self.fw_avg_btn.clicked.connect(self._send_fw_averages)
        self.connect_btn.clicked.connect(self._connect_manual)
        self.disconnect_btn.clicked.connect(self._disconnect_manual)
        self.refresh_devices_btn.clicked.connect(self._scan_devices_bg)
        self.v1_sweep.signal_name_edit.editingFinished.connect(self._refresh_column_schema)
        self.v2_sweep.signal_name_edit.editingFinished.connect(self._refresh_column_schema)
        self.plot_panel.plot_clicked.connect(self._on_plot_clicked)

        self._set_link_led(False)
        self._log("System ready. Configure V1/V2 and press Start.")
        self._scan_devices_bg()

    def _style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget {
                background: #eef3f8;
                color: #1e293b;
                font-family: 'Segoe UI', 'Bahnschrift', sans-serif;
                font-size: 10pt;
            }
            #controlsPanel {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                           stop:0 #ffffff, stop:1 #f4f7fb);
                border: 1px solid #d8e1ea;
                border-radius: 10px;
                padding: 4px;
            }
            #panelTitle {
                font-size: 13pt;
                font-weight: 700;
                color: #0f172a;
                padding: 0 0 2px 2px;
            }
            QGroupBox {
                border: 1px solid #d8e1ea;
                border-radius: 10px;
                margin-top: 10px;
                font-weight: 600;
                background: #ffffff;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 8px;
                padding: 0 4px;
                color: #334155;
            }
            #controlsPanel QGroupBox {
                border-radius: 8px;
                margin-top: 7px;
            }
            #controlsPanel QGroupBox::title {
                left: 6px;
                padding: 0 2px;
            }
            #controlsPanel QLabel {
                font-size: 9pt;
            }
            #controlsPanel QFrame, #controlsPanel QWidget {
                font-size: 9pt;
            }
            QPushButton {
                background: #0b7285;
                border: none;
                border-radius: 8px;
                color: white;
                font-weight: 600;
                padding: 8px;
            }
            #controlsPanel QPushButton {
                border-radius: 6px;
                padding: 4px 6px;
                min-height: 20px;
                font-size: 9pt;
            }
            QPushButton:disabled {
                background: #9fb7bf;
                color: #e9f0f2;
            }
            QPushButton:hover:!disabled {
                background: #0d849a;
            }
            QDoubleSpinBox, QSpinBox {
                background: #f8fafc;
                border: 1px solid #cfdbe7;
                border-radius: 6px;
                padding: 4px;
                min-height: 20px;
            }
            #controlsPanel QDoubleSpinBox, #controlsPanel QSpinBox {
                border-radius: 5px;
                padding: 2px 4px;
                min-height: 14px;
                font-size: 9pt;
            }
            QCheckBox {
                spacing: 8px;
                font-weight: 600;
            }
            #controlsPanel QCheckBox {
                spacing: 5px;
                font-size: 9pt;
            }
            #linkLed {
                border-radius: 6px;
                border: 1px solid #0f172a;
                background: #0f172a;
            }
            QTextEdit {
                background: #0f172a;
                color: #d8f3dc;
                border-radius: 8px;
                border: 1px solid #24364a;
                padding: 6px;
            }
            QProgressBar {
                border: 1px solid #cfdbe7;
                border-radius: 5px;
                background: #e8f0f6;
                text-align: center;
                font-size: 8pt;
                font-weight: 600;
                color: #1e293b;
            }
            QProgressBar::chunk {
                background: #0b7285;
                border-radius: 4px;
            }
            """
        )

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        self._style()

    def _start(self) -> None:
        self._refresh_column_schema()
        if self._df.empty:
            self._df = pd.DataFrame(columns=self._plot_columns)
        else:
            # Preserve prior rows between runs while adapting to any schema change.
            self._df = self._df.reindex(columns=self._plot_columns)
        self._tick = 0
        self._sample_offset = len(self._df)
        self._meas_all_count = 0
        self.plot_panel.set_data(self._df)

        if not self._instrument.is_connected:
            self._log("TX ABORT | device not connected")
            return

        self._sweep_plan = self._build_sweep_plan()
        if not self._sweep_plan:
            self._log("TX ABORT | empty sweep plan")
            return

        self.progress_bar.setValue(0)

        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)

        self._send_adc_prescale()
        self._send_fw_averages()

        self.timer.start(self.tx_period_ms.value())

        v1 = self.v1_sweep.values()
        v2 = self.v2_sweep.values()
        self._log(
            f"TX START | V1={'ON' if v1.enabled else 'OFF'} [{v1.start:.3f}->{v1.stop:.3f}] "
            f"step={v1.step_size:.3f} n={v1.steps}"
        )
        self._log(
            f"TX START | V2={'ON' if v2.enabled else 'OFF'} [{v2.start:.3f}->{v2.stop:.3f}] "
            f"step={v2.step_size:.3f} n={v2.steps}"
        )
        self._log(
            f"TX PLAN | points={len(self._sweep_plan)} repeats={self.repeat_spin.value()}"
        )

    def _send_adc_prescale(self) -> None:
        pres = self.adc_pres_combo.currentData()
        self._send_scpi(f"SENS:ADC:PRES {pres}")

    def _send_fw_averages(self) -> None:
        self._send_scpi(f"SENS:AVER:COUN {self.fw_avg_spin.value()}")
    def _stop(self) -> None:
        self.timer.stop()
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.progress_bar.setValue(0)
        self._log(f"TX STOP | MEAS:ALL? count={self._meas_all_count}")

    def _clear_plot(self) -> None:
        self._refresh_column_schema()
        self._df = pd.DataFrame(columns=self._plot_columns)
        self._sample_offset = 0
        self.plot_panel.set_data(self._df)
        self._log("TX CLEAR_PLOT")

    def _save_csv(self) -> None:
        if self._df.empty:
            self._log("SAVE | no data to save")
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save data as CSV",
            "",
            "CSV files (*.csv);;All files (*)",
        )
        if not path:
            return
        if not path.lower().endswith(".csv"):
            path += ".csv"
        self._df.to_csv(path, index=False)
        self._log(f"SAVE | {path} ({len(self._df)} rows)")

    def _poll_instrument(self) -> None:
        if self._tick >= len(self._sweep_plan):
            self._log(f"TX DONE | completed points={len(self._sweep_plan)}")
            self._stop()
            return

        sample_idx = self._sample_offset + self._tick + 1
        v1_set, v2_set = self._sweep_plan[self._tick]
        self._tick += 1

        measured = self._measure_point(v1_set, v2_set, stop_on_error=True)
        if measured is None:
            return

        row = {
            "sample": float(sample_idx),
            self._setpoint_columns[0]: measured.v1_set,
            self._setpoint_columns[1]: measured.v2_set,
        }
        row.update(dict(zip(self._measurement_columns, measured.values)))
        self._df = pd.concat([self._df, pd.DataFrame([row])], ignore_index=True)
        pct = int(self._tick / len(self._sweep_plan) * 100)
        self.progress_bar.setValue(pct)
        self.plot_panel.set_data(self._df)

    def _on_plot_clicked(self, x_click: float, y_click: float) -> None:
        if self.timer.isActive():
            self._log("CLICK MEASURE | ignored while sweep is running")
            return
        if not self._instrument.is_connected:
            self._log("CLICK MEASURE | device not connected")
            return
        if self._df.empty:
            self._log("CLICK MEASURE | no data to anchor click")
            return

        x_col = self.plot_panel.x_axis_combo.currentText()
        y_col = self.plot_panel.y_axis_combo.currentText()
        if x_col not in self._df.columns or y_col not in self._df.columns:
            self._log("CLICK MEASURE | selected axes unavailable in data")
            return

        x_vals = self._df[x_col].to_numpy(dtype=float)
        y_vals = self._df[y_col].to_numpy(dtype=float)
        if self.plot_panel.x_neg_cb.isChecked():
            x_vals = -x_vals
        if self.plot_panel.y_neg_cb.isChecked():
            y_vals = -y_vals

        valid = np.isfinite(x_vals) & np.isfinite(y_vals)
        if not np.any(valid):
            self._log("CLICK MEASURE | no valid points for nearest lookup")
            return

        idx_valid = np.where(valid)[0]
        x_v = x_vals[valid]
        y_v = y_vals[valid]

        # Normalise distances by current view span so x and y axes are comparable.
        vr = self.plot_panel.plot_widget.viewRange()
        x_span = max(abs(vr[0][1] - vr[0][0]), 1e-30)
        y_span = max(abs(vr[1][1] - vr[1][0]), 1e-30)
        dx_n = (x_v - x_click) / x_span
        dy_n = (y_v - y_click) / y_span
        norm_dist = np.sqrt(dx_n ** 2 + dy_n ** 2)

        nearest_vi = int(np.argmin(norm_dist))
        nearest_row_idx = int(idx_valid[nearest_vi])
        v1_set_col, v2_set_col = self._setpoint_columns

        x_neg = self.plot_panel.x_neg_cb.isChecked()
        y_neg = self.plot_panel.y_neg_cb.isChecked()
        x_data_click = -x_click if x_neg else x_click
        y_data_click = -y_click if y_neg else y_click
        self._log(
            f"CLICK MEASURE | click=({x_click:.4f}, {y_click:.4f}) "
            f"x_col={x_col} y_col={y_col} norm_dist_nearest={norm_dist[nearest_vi]:.4f}"
        )

        _SNAP_THRESHOLD = 0.05
        if norm_dist[nearest_vi] <= _SNAP_THRESHOLD:
            v1_set = float(self._df.at[nearest_row_idx, v1_set_col])
            v2_set = float(self._df.at[nearest_row_idx, v2_set_col])
            self._log(
                f"CLICK MEASURE | SNAP row={nearest_row_idx + 1} "
                f"{v1_set_col}={v1_set:.4f}, {v2_set_col}={v2_set:.4f}"
            )
            debug_cols = ["sample"] + self._setpoint_columns + self._measurement_columns
            snap_payload = {c: self._df.at[nearest_row_idx, c] for c in debug_cols if c in self._df.columns}
            self._log(f"CLICK MEASURE | SNAP details {snap_payload}")
        else:
            eps = 1e-12

            def _pick_directional_neighbor(direction: str, excluded: set[int]) -> int | None:
                candidates: list[tuple[float, int]] = []
                for vi, row_idx in enumerate(idx_valid):
                    if int(row_idx) in excluded:
                        continue
                    dx = float(x_v[vi] - x_click)
                    dy = float(y_v[vi] - y_click)
                    d = float(norm_dist[vi])
                    if direction == "right" and dx > 0:
                        # Prefer points truly on the right, penalize vertical offset.
                        score = d * (1.0 + abs(dy) / (abs(dx) + eps))
                    elif direction == "left" and dx < 0:
                        score = d * (1.0 + abs(dy) / (abs(dx) + eps))
                    elif direction == "up" and dy > 0:
                        # Prefer points truly above, penalize horizontal offset.
                        score = d * (1.0 + abs(dx) / (abs(dy) + eps))
                    elif direction == "down" and dy < 0:
                        score = d * (1.0 + abs(dx) / (abs(dy) + eps))
                    else:
                        continue
                    candidates.append((score, int(row_idx)))
                if not candidates:
                    return None
                candidates.sort(key=lambda t: t[0])
                return candidates[0][1]

            selected: dict[str, int] = {}
            used_rows: set[int] = set()
            for direction in ("left", "right", "up", "down"):
                picked = _pick_directional_neighbor(direction, used_rows)
                if picked is None:
                    # Fallback: allow reuse if no unique row exists in that direction.
                    picked = _pick_directional_neighbor(direction, set())
                if picked is not None:
                    selected[direction] = picked
                    used_rows.add(picked)

            if not selected:
                self._log("CLICK MEASURE | no directional neighbors found")
                return

            debug_cols = ["sample"] + self._setpoint_columns + self._measurement_columns
            weighted_rows: list[int] = []
            weights: list[float] = []
            for direction in ("left", "right", "up", "down"):
                if direction not in selected:
                    continue
                row_idx = selected[direction]
                row_x = float(x_vals[row_idx])
                row_y = float(y_vals[row_idx])
                d = math.hypot((row_x - x_click) / x_span, (row_y - y_click) / y_span)
                w = 1.0 / max(d, 1e-6)
                weighted_rows.append(row_idx)
                weights.append(w)

                payload = {c: self._df.at[row_idx, c] for c in debug_cols if c in self._df.columns}
                payload["x_plot"] = row_x
                payload["y_plot"] = row_y
                payload["norm_dist"] = d
                payload["weight"] = w
                self._log(f"CLICK MEASURE | neighbor[{direction}] row={row_idx + 1} {payload}")

            w_arr = np.array(weights, dtype=float)
            w_arr /= w_arr.sum()
            v1_vals = np.array([float(self._df.at[r, v1_set_col]) for r in weighted_rows], dtype=float)
            v2_vals = np.array([float(self._df.at[r, v2_set_col]) for r in weighted_rows], dtype=float)

            v1_set = float(np.dot(w_arr, v1_vals))
            v2_set = float(np.dot(w_arr, v2_vals))

            # If a setpoint is directly on an axis, use click coordinate exactly.
            if x_col == v1_set_col:
                v1_set = float(x_data_click)
                v1_src = "x-axis direct"
            elif y_col == v1_set_col:
                v1_set = float(y_data_click)
                v1_src = "y-axis direct"
            else:
                v1_src = f"dir-weighted ({len(weighted_rows)} neighbors)"

            if x_col == v2_set_col:
                v2_set = float(x_data_click)
                v2_src = "x-axis direct"
            elif y_col == v2_set_col:
                v2_set = float(y_data_click)
                v2_src = "y-axis direct"
            else:
                v2_src = f"dir-weighted ({len(weighted_rows)} neighbors)"

            self._log(
                f"CLICK MEASURE | NEW {v1_set_col}={v1_set:.4f} ({v1_src}), "
                f"{v2_set_col}={v2_set:.4f} ({v2_src})"
            )

        measured = self._measure_point(v1_set, v2_set, stop_on_error=False)
        if measured is None:
            return

        sample_idx = len(self._df) + 1
        row = {
            "sample": float(sample_idx),
            v1_set_col: measured.v1_set,
            v2_set_col: measured.v2_set,
        }
        row.update(dict(zip(self._measurement_columns, measured.values)))
        self._df = pd.concat([self._df, pd.DataFrame([row])], ignore_index=True)
        self.plot_panel.set_data(self._df)

    def _measure_point(self, v1_set: float, v2_set: float, *, stop_on_error: bool) -> MeasuredPoint | None:
        requested_v1 = v1_set
        requested_v2 = v2_set
        v1_set = self._clamp_source_voltage(1, requested_v1)
        v2_set = self._clamp_source_voltage(2, requested_v2)

        if v1_set != requested_v1:
            self._log(
                f"CLAMP | SOUR1 requested={requested_v1:.4f}V, applied={v1_set:.4f}V "
                f"range=[{self._source_limits[1].min_v:.4f}, {self._source_limits[1].max_v:.4f}]"
            )
        if v2_set != requested_v2:
            self._log(
                f"CLAMP | SOUR2 requested={requested_v2:.4f}V, applied={v2_set:.4f}V "
                f"range=[{self._source_limits[2].min_v:.4f}, {self._source_limits[2].max_v:.4f}]"
            )

        self._send_scpi(f"SOUR1:VOLT {v1_set:.4f}")
        self._send_scpi(f"SOUR2:VOLT {v2_set:.4f}")

        n_avg = max(1, self.avg_spin.value())
        accumulated: list[tuple[float, ...]] = []
        meas_timeout = self._meas_timeout_s()
        for _ in range(n_avg):
            rx_line = self._send_scpi("MEAS:ALL?", expect_response=True, timeout_s=meas_timeout)
            if not rx_line:
                self._log("ERROR | no response to MEAS:ALL?")
                if stop_on_error:
                    self._stop()
                return None
            self._meas_all_count += 1
            try:
                accumulated.append(self._parse_meas_all(rx_line))
            except ValueError as exc:
                self._log(f"ERROR | {exc}")
                if stop_on_error:
                    self._stop()
                return None

        return MeasuredPoint(
            v1_set=v1_set,
            v2_set=v2_set,
            values=tuple(
                sum(sample[i] for sample in accumulated) / len(accumulated)
                for i in range(len(self._measurement_columns))
            ),
        )

    def _build_sweep_plan(self) -> list[tuple[float, float]]:
        v1 = self.v1_sweep.values()
        v2 = self.v2_sweep.values()
        repetitions = self.repeat_spin.value()

        v1_points = self._sweep_points(v1)
        v2_points = self._sweep_points(v2)
        base_plan: list[tuple[float, float]] = [
            (v1_val, v2_val)
            for v2_val in v2_points
            for v1_val in v1_points
        ]

        plan: list[tuple[float, float]] = []
        for _ in range(repetitions):
            repeat_plan = list(base_plan)
            if self.random_order_cb.isChecked():
                random.shuffle(repeat_plan)
            plan.extend(repeat_plan)
        return plan

    def _sweep_points(self, sweep: SweepValues) -> list[float]:
        if not sweep.enabled:
            return [sweep.start]
        steps = max(2, int(sweep.steps))
        return [float(x) for x in np.linspace(sweep.start, sweep.stop, steps)]

    def _log(self, message: str) -> None:
        self.console.append(message)
        print(message)

    def _open_serial(self, port: str | None) -> bool:
        if not port:
            self._set_link_led(False)
            self.link_state_label.setText("Disconnected")
            self._log("ERROR | No port specified")
            return False

        try:
            self._instrument.open(port)
            self._set_link_led(True)
            self.link_state_label.setText("Connected")
            self.connect_btn.setEnabled(False)
            self.disconnect_btn.setEnabled(True)
            self._log(f"LINK OPEN | {port} @ 115200")
            self._wait_for_instrument_ready()
            self._query_source_capabilities()
            return True
        except Exception as exc:
            self._set_link_led(False)
            self.link_state_label.setText("Disconnected")
            self.connect_btn.setEnabled(True)
            self.disconnect_btn.setEnabled(False)
            self._log(f"ERROR | Serial open failed: {exc}")
            return False

    def _close_serial(self) -> None:
        self._instrument.close()
        self._set_link_led(False)
        self.link_state_label.setText("Disconnected")
        self.connect_btn.setEnabled(True)
        self.disconnect_btn.setEnabled(False)

    def _connect_manual(self) -> None:
        port = self.device_combo.currentData()
        if not port:
            self._log("ERROR | No device selected")
            return
        if self._open_serial(port):
            self._log("LINK READY")

    def _disconnect_manual(self) -> None:
        self._close_serial()
        self._log("LINK CLOSED")

    def _scan_devices_bg(self) -> None:
        """Launch device scanner in background thread."""
        if self._device_scanner_thread is not None and self._device_scanner_thread.isRunning():
            return
        self._device_scanner_thread = DeviceScannerThread()
        self._device_scanner_thread.devices_found.connect(self._on_devices_found)
        self._device_scanner_thread.start()

    def _on_devices_found(self, devices: list[tuple[str, str]]) -> None:
        """Update device combo and auto-select likely Arduino-compatible ports."""
        previous_port = self.device_combo.currentData()
        self.device_combo.clear()
        if not devices:
            self.device_combo.addItem("No devices found", None)
            return

        for port, desc in devices:
            self.device_combo.addItem(f"{port} ({desc})", port)

        # Keep current selection if still present after refresh.
        for idx in range(self.device_combo.count()):
            if self.device_combo.itemData(idx) == previous_port:
                self.device_combo.setCurrentIndex(idx)
                return

        # Otherwise pick the first Arduino-like device by description/name.
        arduino_tokens = ("arduino", "ch340", "wchusbserial", "cp210", "usb serial")
        for idx, (_port, desc) in enumerate(devices):
            if any(tok in desc.lower() for tok in arduino_tokens):
                self.device_combo.setCurrentIndex(idx)
                self._log(f"LINK DETECT | auto-selected {devices[idx][0]} ({devices[idx][1]})")
                return

    def _send_scpi(self, cmd: str, expect_response: bool = False, timeout_s: float | None = None) -> str:
        line = self._instrument.send_scpi(
            cmd,
            expect_response=expect_response,
            timeout_s=timeout_s if timeout_s is not None else self._meas_timeout_s(),
        )
        if not self._instrument.is_connected and self.link_state_label.text() == "Connected":
            self._close_serial()
        return line

    def _wait_for_instrument_ready(self) -> None:
        # Uno-class boards often auto-reset when the serial port opens.
        # Give firmware time to boot before issuing capability queries.
        boot_wait_s = 1.6
        self._log(f"LINK WAIT | boot {boot_wait_s:.1f}s")
        time.sleep(boot_wait_s)
        self._instrument.drain_rx()

        for _ in range(3):
            idn = self._send_scpi("*IDN?", expect_response=True, timeout_s=0.8)
            if idn:
                self._log(f"LINK IDN | {idn}")
                return
            time.sleep(0.2)

        self._log("LINK WARN | no IDN response yet; continuing")

    def _query_source_capabilities(self) -> None:
        updated: list[tuple[int, SourceLimits]] = []
        for src in (1, 2):
            limits = self._query_source_limits(src)
            if limits is None:
                current = self._source_limits[src]
                self._log(
                    f"CAPS | SOUR{src} limits unavailable, using "
                    f"[{current.min_v:.4f}, {current.max_v:.4f}]"
                )
                continue
            self._source_limits[src] = limits
            updated.append((src, limits))

        if updated:
            summary = " ".join(
                f"SOUR{src}=[{lim.min_v:.4f},{lim.max_v:.4f}]"
                for src, lim in updated
            )
            self._log(f"CAPS | {summary}")
        self._apply_source_limits_to_controls()

    def _query_source_limits(self, source: int) -> SourceLimits | None:
        min_line = ""
        max_line = ""
        for _ in range(3):
            min_line = self._send_scpi(f"SOUR{source}:VOLT:MIN?", expect_response=True, timeout_s=0.8)
            max_line = self._send_scpi(f"SOUR{source}:VOLT:MAX?", expect_response=True, timeout_s=0.8)
            if min_line and max_line:
                break
            time.sleep(0.2)
        try:
            min_v = float(min_line.strip())
            max_v = float(max_line.strip())
        except Exception:
            return None
        if not (math.isfinite(min_v) and math.isfinite(max_v) and max_v >= min_v):
            return None
        return SourceLimits(min_v=min_v, max_v=max_v)

    def _apply_source_limits_to_controls(self) -> None:
        self._apply_limits_to_sweep(self.v1_sweep, self._source_limits[1])
        self._apply_limits_to_sweep(self.v2_sweep, self._source_limits[2])

    def _apply_limits_to_sweep(self, sweep: SweepControl, limits: SourceLimits) -> None:
        sweep.start_spin.setRange(limits.min_v, limits.max_v)
        sweep.stop_spin.setRange(limits.min_v, limits.max_v)
        sweep.start_spin.setValue(self._clamp_value(sweep.start_spin.value(), limits.min_v, limits.max_v))
        sweep.stop_spin.setValue(self._clamp_value(sweep.stop_spin.value(), limits.min_v, limits.max_v))

    @staticmethod
    def _clamp_value(value: float, min_v: float, max_v: float) -> float:
        return min(max(value, min_v), max_v)

    def _clamp_source_voltage(self, source: int, requested: float) -> float:
        limits = self._source_limits[source]
        return self._clamp_value(requested, limits.min_v, limits.max_v)

    def _meas_timeout_s(self) -> float:
        """Estimate serial read timeout for one MEAS:ALL? response.

        At prescaler P on a 16 MHz AVR, one 10-bit conversion takes
        P/16e6 * 13 seconds.  readAnalogVolts() discards the first and
        keeps the second, so 2 conversions per channel, 6 channels (A0-A5) = 12
        conversions per firmware-average iteration (estimated conservatively as 16).
        Returns 2× the estimate plus a 0.5 s absolute margin.
        """
        pres = self.adc_pres_combo.currentData() or 128
        fw_avg = self.fw_avg_spin.value()
        conv_s = pres / 16e6 * 13
        estimated = fw_avg * 16 * conv_s
        return max(0.5, estimated * 2 + 0.5)

    def _parse_meas_all(self, line: str) -> tuple[float, ...]:
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < MEAS_VALUE_COUNT:
            raise ValueError("Bad MEAS:ALL? format")
        try:
            vals = tuple(float(parts[i]) for i in range(MEAS_VALUE_COUNT))
            return vals
        except Exception:
            raise ValueError("Non-numeric MEAS:ALL? response")

    def _refresh_column_schema(self) -> None:
        sig1 = self.v1_sweep.signal_token()
        sig2 = self.v2_sweep.signal_token()
        self._setpoint_columns = [f"V{sig1}_set", f"V{sig2}_set"]
        self._measurement_columns = [
            f"V{sig1}_V",
            f"I{sig1}_A",
            f"V{sig2}_V",
            f"I{sig2}_A",
            "GND_I_A",
            "VS_V",
            "VS_I_A",
        ]
        self._plot_columns = ["sample"] + self._setpoint_columns + self._measurement_columns
        if hasattr(self, "plot_panel"):
            self.plot_panel.set_columns(self._plot_columns)

    def _set_link_led(self, connected: bool) -> None:
        color = "#22c55e" if connected else "#0f172a"
        self.link_led.setStyleSheet(
            f"background:{color}; border:1px solid #0f172a; border-radius:6px;"
        )

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._close_serial()
        if self._device_scanner_thread is not None:
            self._device_scanner_thread.quit()
            self._device_scanner_thread.wait()
        super().closeEvent(event)


def main() -> None:
    app = QApplication(sys.argv)

    pg.setConfigOptions(antialias=True)

    win = InstrumentGui()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
