"""Standalone CSV viewer using the shared SweepPlotWidget.

Open one or more CSV files from the File menu and explore them
with the same axis / colour controls as the live sweep GUI.
"""

from __future__ import annotations

import sys

import pandas as pd
import pyqtgraph as pg
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QLabel,
    QMainWindow,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from sweep_plot import SweepPlotWidget


class CsvViewer(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Sweep CSV Viewer")
        self.resize(1280, 780)

        self.plot_panel = SweepPlotWidget()

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(self.plot_panel)
        self.setCentralWidget(central)

        self._status = QStatusBar()
        self.setStatusBar(self._status)
        self._status.showMessage("No file loaded — use File → Open CSV. Use Color by + Contour for Z maps.")

        self._build_menu()

    def _build_menu(self) -> None:
        menu_bar = self.menuBar()

        file_menu = menu_bar.addMenu("&File")

        open_act = file_menu.addAction("&Open CSV…")
        open_act.setShortcut(QKeySequence.StandardKey.Open)
        open_act.triggered.connect(self._open_csv)

        file_menu.addSeparator()

        quit_act = file_menu.addAction("&Quit")
        quit_act.setShortcut(QKeySequence.StandardKey.Quit)
        quit_act.triggered.connect(self.close)

    def _open_csv(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Open CSV file(s)",
            "",
            "CSV files (*.csv);;All files (*)",
        )
        if not paths:
            return

        frames: list[pd.DataFrame] = []
        for path in paths:
            try:
                frames.append(pd.read_csv(path))
            except Exception as exc:
                self._status.showMessage(f"Error loading {path}: {exc}")
                return

        if len(frames) == 1:
            df = frames[0]
            label = paths[0]
        else:
            # Stack multiple files; add a 'file' column so they're identifiable.
            for i, (frame, path) in enumerate(zip(frames, paths)):
                frame.insert(0, "file", i)
            df = pd.concat(frames, ignore_index=True)
            label = f"{len(paths)} files"

        self.plot_panel.set_columns(df.columns.tolist())
        self.plot_panel.set_data(df)

        self._status.showMessage(
            f"Loaded {label}  —  {len(df):,} rows × {len(df.columns)} columns"
        )
        self.setWindowTitle(f"Sweep CSV Viewer — {label}")


def main() -> None:
    app = QApplication(sys.argv)
    pg.setConfigOptions(antialias=True)
    win = CsvViewer()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
