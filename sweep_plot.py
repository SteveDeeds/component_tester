"""Reusable sweep-data plot widget.

Provides SweepPlotWidget — a self-contained QWidget with axis selectors and
a pyqtgraph PlotWidget.  Import this into gui_app.py for live sweeps or into
csv_viewer.py (or any other app) for offline data inspection.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pyqtgraph as pg
from scipy.interpolate import RectBivariateSpline, griddata
from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

# Default column list matches the live-sweep schema; callers can override.
DEFAULT_COLUMNS: list[str] = [
    "sample", "V1_set", "V2_set",
    "V1_V", "I1_A", "V2_V", "I2_A", "GND_I_A",
    "VS_V", "VS_I_A",
]

# Don't render per-point symbols above this total point count — symbols are
# the single biggest pyqtgraph performance cost (one Qt item per point).
_SYMBOL_THRESHOLD = 500
_CONTOUR_MIN_GRID_SIZE = 50


def _eng_format(value: float) -> str:
    """Format a number in engineering notation with SI suffix."""
    if not np.isfinite(value):
        return ""
    if value == 0.0:
        return "0"
    prefixes = [
        (1e12, "T"), (1e9, "G"), (1e6, "M"), (1e3, "k"),
        (1e0, ""),
        (1e-3, "m"), (1e-6, "µ"), (1e-9, "n"), (1e-12, "p"),
    ]
    abs_val = abs(value)
    for factor, prefix in prefixes:
        if abs_val >= factor * 0.9999:
            scaled = value / factor
            if abs(scaled) >= 100:
                s = f"{scaled:.0f}"
            elif abs(scaled) >= 10:
                s = f"{scaled:.1f}"
            else:
                s = f"{scaled:.2f}"
            return f"{s}{prefix}"
    return f"{value:.2e}"


class SweepPlotWidget(QWidget):
    """Self-contained plot panel: axis selectors + live / static plot.

    Usage
    -----
    widget = SweepPlotWidget(columns)   # pass column names for the combos
    widget.set_data(df)                 # supply / update a DataFrame
    widget.refresh()                    # force redraw without new data
    """

    plot_clicked = Signal(float, float)

    def __init__(self, columns: list[str] | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self._df: pd.DataFrame = pd.DataFrame()
        self._raw_df: pd.DataFrame = pd.DataFrame()
        self._plot_items: list[object] = []
        # V2 groups cached on set_data() — dict {v2_val: sorted_df} keyed by x_col
        self._v2_group_cache: dict[str, list[tuple[float, pd.DataFrame]]] = {}
        self._sweep_x_cols: set[str] = {"V1_set", "V1_V"}
        self._group_by_col = "V2_set"
        self._setpoint_cols: list[str] = ["V1_set", "V2_set"]

        # ── Axis selector row ─────────────────────────────────────────────
        axis_row = QWidget()
        axis_layout = QHBoxLayout(axis_row)
        axis_layout.setContentsMargins(0, 0, 0, 0)
        axis_layout.setSpacing(8)

        self.x_axis_combo = QComboBox()
        self.y_axis_combo = QComboBox()
        self.x_axis_combo.setFixedHeight(24)
        self.y_axis_combo.setFixedHeight(24)

        self.x_log_cb = QCheckBox("Log")
        self.y_log_cb = QCheckBox("Log")
        self.x_log_cb.setFixedHeight(22)
        self.y_log_cb.setFixedHeight(22)

        self.x_neg_cb = QCheckBox("Negate")
        self.y_neg_cb = QCheckBox("Negate")
        self.x_neg_cb.setFixedHeight(22)
        self.y_neg_cb.setFixedHeight(22)

        self.color_axis_combo = QComboBox()
        self.color_axis_combo.setFixedHeight(24)

        self.contour_cb = QCheckBox("Contour")
        self.contour_cb.setFixedHeight(22)
        self.z_log_cb = QCheckBox("Z Log")
        self.z_log_cb.setFixedHeight(22)
        self.z_neg_cb = QCheckBox("Z Negate")
        self.z_neg_cb.setFixedHeight(22)

        axis_layout.addWidget(QLabel("X axis:"))
        axis_layout.addWidget(self.x_axis_combo)
        axis_layout.addWidget(self.x_log_cb)
        axis_layout.addWidget(self.x_neg_cb)
        axis_layout.addSpacing(16)
        axis_layout.addWidget(QLabel("Y axis:"))
        axis_layout.addWidget(self.y_axis_combo)
        axis_layout.addWidget(self.y_log_cb)
        axis_layout.addWidget(self.y_neg_cb)
        axis_layout.addSpacing(16)
        axis_layout.addWidget(QLabel("Color by:"))
        axis_layout.addWidget(self.color_axis_combo)
        axis_layout.addWidget(self.contour_cb)
        axis_layout.addWidget(self.z_log_cb)
        axis_layout.addWidget(self.z_neg_cb)
        axis_layout.addStretch(1)

        # ── Plot widget ───────────────────────────────────────────────────
        self.plot_widget = pg.PlotWidget()
        self.plot_widget.setBackground("#f8fafc")
        self.plot_widget.showGrid(x=True, y=True, alpha=0.3)
        # Let pyqtgraph skip rendering points that are outside the view or
        # too close together — free performance win, no data is dropped.
        self.plot_widget.setDownsampling(auto=True, mode="peak")
        self.plot_widget.setClipToView(True)
        # Enable drag-to-zoom rectangle while avoiding pan behavior.
        self.plot_widget.getPlotItem().vb.setMouseMode(pg.ViewBox.RectMode)
        self.plot_widget.setMouseEnabled(x=True, y=True)
        self.plot_widget.setMenuEnabled(False)

        # Live cursor coordinate readout shown inside the chart.
        self._coord_text = pg.TextItem(anchor=(0, 1))
        self._coord_text.setZValue(1_000_000)
        self._coord_text.hide()
        self.plot_widget.addItem(self._coord_text, ignoreBounds=True)
        self._mouse_proxy = pg.SignalProxy(
            self.plot_widget.scene().sigMouseMoved,
            rateLimit=60,
            slot=self._on_mouse_moved,
        )
        self._click_proxy = pg.SignalProxy(
            self.plot_widget.scene().sigMouseClicked,
            rateLimit=60,
            slot=self._on_mouse_clicked,
        )

        # ── Layout ────────────────────────────────────────────────────────
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(axis_row, 0)
        layout.addWidget(self.plot_widget, 1)

        # Populate combos now so signals fire correctly after connection.
        self.set_columns(columns or DEFAULT_COLUMNS)

        # ── Signals ───────────────────────────────────────────────────────
        self.x_axis_combo.currentTextChanged.connect(self.refresh)
        self.y_axis_combo.currentTextChanged.connect(self.refresh)
        self.x_log_cb.toggled.connect(self.refresh)
        self.y_log_cb.toggled.connect(self.refresh)
        self.x_neg_cb.toggled.connect(self.refresh)
        self.y_neg_cb.toggled.connect(self.refresh)
        self.color_axis_combo.currentIndexChanged.connect(self.refresh)
        self.contour_cb.toggled.connect(self.refresh)
        self.z_log_cb.toggled.connect(self.refresh)
        self.z_neg_cb.toggled.connect(self.refresh)

    # ── Public API ────────────────────────────────────────────────────────

    def set_columns(self, columns: list[str]) -> None:
        """Repopulate axis combos (call when the data schema changes)."""
        for combo in (self.x_axis_combo, self.y_axis_combo):
            combo.blockSignals(True)
            current = combo.currentText()
            combo.clear()
            for col in columns:
                combo.addItem(col)
            if current in columns:
                combo.setCurrentText(current)
            combo.blockSignals(False)

        self.color_axis_combo.blockSignals(True)
        current_color = self.color_axis_combo.currentData()
        self.color_axis_combo.clear()
        self.color_axis_combo.addItem("None", None)
        for col in columns:
            self.color_axis_combo.addItem(col, col)
        if current_color in columns:
            idx = self.color_axis_combo.findData(current_color)
            if idx >= 0:
                self.color_axis_combo.setCurrentIndex(idx)
        self.color_axis_combo.blockSignals(False)

        # Infer the sweep pair from incoming columns so custom names
        # (e.g. Vb_set / Vc_set) keep grouped plotting behavior.
        set_cols = [c for c in columns if c.startswith("V") and c.endswith("_set")]
        if len(set_cols) >= 2:
            primary_set = set_cols[0]
            secondary_set = set_cols[1]
            self._setpoint_cols = [primary_set, secondary_set]
            primary_v = primary_set.replace("_set", "_V")
            self._sweep_x_cols = {primary_set}
            if primary_v in columns:
                self._sweep_x_cols.add(primary_v)
            self._group_by_col = secondary_set

    def set_data(self, df: pd.DataFrame) -> None:
        """Replace the current dataset and redraw."""
        self._raw_df = df

        # Display averaged values for repeated measurements at identical setpoints.
        # Keep all original rows in self._raw_df so CSV export remains lossless.
        self._df = df.copy()
        if not self._df.empty:
            numeric_like_cols: set[str] = set()
            for col in self._df.columns:
                coerced = pd.to_numeric(self._df[col], errors="coerce")
                if coerced.notna().any():
                    self._df[col] = coerced
                    numeric_like_cols.add(col)

            group_cols = [c for c in self._setpoint_cols if c in self._df.columns]
            if not group_cols:
                group_cols = [c for c in self._df.columns if c.endswith("_set")]

            if group_cols:
                agg_spec: dict[str, str] = {}
                for col in self._df.columns:
                    if col in group_cols:
                        continue
                    agg_spec[col] = "mean" if col in numeric_like_cols else "first"

                if agg_spec:
                    grouped = self._df.groupby(
                        group_cols,
                        sort=True,
                        dropna=False,
                        as_index=False,
                    ).agg(agg_spec)
                    ordered_cols = [c for c in df.columns if c in grouped.columns]
                    self._df = grouped.reindex(columns=ordered_cols)
                else:
                    self._df = self._df.drop_duplicates(subset=group_cols).reset_index(drop=True)

        self._v2_group_cache.clear()
        # Pre-sort all V2 groups for each sweep x-axis column so refresh()
        # never has to run groupby/sort_values on the hot path.
        if not self._df.empty and self._group_by_col in self._df.columns:
            for x_col in self._sweep_x_cols:
                if x_col in self._df.columns:
                    groups = self._df.groupby(self._group_by_col, sort=True)
                    self._v2_group_cache[x_col] = [
                        (v2_val, grp.sort_values(x_col))
                        for v2_val, grp in groups
                    ]
        self.refresh()

    def refresh(self) -> None:
        """Redraw all curves from the current dataset."""
        x_col = self.x_axis_combo.currentText()
        y_col = self.y_axis_combo.currentText()
        x_log = self.x_log_cb.isChecked()
        y_log = self.y_log_cb.isChecked()
        color_col = self.color_axis_combo.currentData()
        contour_enabled = self.contour_cb.isChecked()
        z_log = self.z_log_cb.isChecked()
        z_neg = self.z_neg_cb.isChecked()

        self.plot_widget.setLabel("bottom", x_col)
        self.plot_widget.setLabel("left", y_col)
        self.plot_widget.setLogMode(x=x_log, y=y_log)

        for item in self._plot_items:
            self.plot_widget.removeItem(item)
        self._plot_items.clear()

        if self._df.empty or x_col not in self._df.columns or y_col not in self._df.columns:
            return

        x_neg = self.x_neg_cb.isChecked()
        y_neg = self.y_neg_cb.isChecked()

        n_total = len(self._df)
        use_symbols = n_total <= _SYMBOL_THRESHOLD

        if contour_enabled and color_col and color_col in self._df.columns:
            if self._render_contour_plot(
                x_col,
                y_col,
                color_col,
                x_log,
                y_log,
                x_neg,
                y_neg,
                z_log,
                z_neg,
            ):
                return

        # ── Multi-curve mode: one line per V2_set group ───────────────────
        # Skipped when the user explicitly picks a Color-by column — the
        # colour-mapped scatter below handles that case instead.
        sort_by_setpoints = (
            x_col in self._sweep_x_cols
            and self._group_by_col in self._df.columns
            and not color_col
        )

        if sort_by_setpoints:
            # Use pre-cached sorted groups when available.
            cached = self._v2_group_cache.get(x_col)
            if cached is None:
                groups_iter = [
                    (v2_val, grp.sort_values(x_col))
                    for v2_val, grp in self._df.groupby(self._group_by_col, sort=True)
                ]
            else:
                groups_iter = cached

            n_groups = len(groups_iter)
            cmap = pg.colormap.get("viridis")
            sym = "o" if use_symbols else None
            for idx, (v2_val, group_sorted) in enumerate(groups_iter):
                x = group_sorted[x_col].to_numpy(dtype=float)
                y = group_sorted[y_col].to_numpy(dtype=float)
                if x_neg:
                    x = -x
                if y_neg:
                    y = -y
                t = idx / max(n_groups - 1, 1)
                rgba = cmap.map(float(t), mode="byte")
                r, g, b = int(rgba[0]), int(rgba[1]), int(rgba[2])
                pen = pg.mkPen(color=QColor(r, g, b), width=2)
                curve = self.plot_widget.plot(
                    x, y,
                    pen=pen,
                    symbol=sym,
                    symbolSize=5,
                    symbolBrush=pg.mkBrush(r, g, b, 220) if use_symbols else None,
                    symbolPen=None,
                    name=f"{self._group_by_col.replace('_set', '')}={v2_val:.3f}V",
                )
                self._plot_items.append(curve)

        else:
            # ── Single-curve / colour-mapped scatter ──────────────────────
            x = self._df[x_col].to_numpy(dtype=float)
            y = self._df[y_col].to_numpy(dtype=float)
            if x_neg:
                x = -x
            if y_neg:
                y = -y

            if color_col and color_col in self._df.columns:
                # ScatterPlotItem does not receive PlotItem log transforms,
                # so apply x/y log mapping explicitly in this branch.
                x_scatter = self._transform_axis_values(x, axis_log=x_log, axis_neg=False)
                y_scatter = self._transform_axis_values(y, axis_log=y_log, axis_neg=False)
                c_vals = self._transform_z_values(
                    self._df[color_col].to_numpy(dtype=float),
                    z_log=z_log,
                    z_neg=z_neg,
                )
                valid = np.isfinite(x_scatter) & np.isfinite(y_scatter) & np.isfinite(c_vals)
                x_scatter = x_scatter[valid]
                y_scatter = y_scatter[valid]
                c_vals = c_vals[valid]
                if len(c_vals) == 0:
                    return
                c_min, c_max = c_vals.min(), c_vals.max()
                span = c_max - c_min if c_max != c_min else 1.0
                normalized = (c_vals - c_min) / span
                cmap = pg.colormap.get("viridis")
                rgba = cmap.map(normalized, mode="byte")
                # Always use ScatterPlotItem for per-point colour data.
                # Passing a brush list to plot() causes a pyqtgraph crash when
                # the view clips x/y to fewer points than the brush list has
                # entries (viewRangeChanged → updateItems mismatch).
                size = 6 if use_symbols else 5
                spots = [
                    {"pos": (float(x_scatter[i]), float(y_scatter[i])),
                     "brush": pg.mkBrush(int(rgba[i, 0]), int(rgba[i, 1]),
                                         int(rgba[i, 2]), 200),
                     "size": size, "pen": None}
                    for i in range(len(x_scatter))
                ]
                scatter = pg.ScatterPlotItem(spots=spots)
                self.plot_widget.addItem(scatter)
                self._plot_items.append(scatter)
                return
            else:
                sym = "o" if use_symbols else None
                curve = self.plot_widget.plot(
                    x, y,
                    pen=pg.mkPen(color=QColor("#0b7285"), width=2),
                    symbol=sym,
                    symbolSize=5,
                    symbolBrush=QColor("#0b7285") if use_symbols else None,
                    symbolPen=None,
                )
            self._plot_items.append(curve)

    def _on_mouse_moved(self, evt: tuple[object]) -> None:
        pos = evt[0]
        plot_item = self.plot_widget.getPlotItem()
        view_box = plot_item.vb
        if not self.plot_widget.sceneBoundingRect().contains(pos):
            self._coord_text.hide()
            return

        mouse_point = view_box.mapSceneToView(pos)
        x = float(mouse_point.x())
        y = float(mouse_point.y())
        self._coord_text.setHtml(
            "<div style=\""
            "background-color: rgba(255, 255, 255, 230);"
            "color: rgb(15, 23, 42);"
            "border: 1px solid rgba(15, 23, 42, 110);"
            "border-radius: 3px;"
            "padding: 1px 3px;"
            "\">"
            f"x={x:.6g}, y={y:.6g}"
            "</div>"
        )
        self._coord_text.setPos(x, y)
        self._coord_text.show()

    def _on_mouse_clicked(self, evt: tuple[object]) -> None:
        click_event = evt[0]
        if click_event.button() != Qt.MouseButton.LeftButton:
            return

        pos = click_event.scenePos()
        if not self.plot_widget.sceneBoundingRect().contains(pos):
            return

        view_box = self.plot_widget.getPlotItem().vb
        mouse_point = view_box.mapSceneToView(pos)
        self.plot_clicked.emit(float(mouse_point.x()), float(mouse_point.y()))

    def _transform_z_values(
        self,
        values: np.ndarray,
        *,
        z_log: bool,
        z_neg: bool,
    ) -> np.ndarray:
        z = values.astype(float).copy()
        if z_neg:
            z = -z
        if z_log:
            z = np.where(z > 0, np.log10(z), np.nan)
        return z

    def _transform_axis_values(
        self,
        values: np.ndarray,
        *,
        axis_log: bool,
        axis_neg: bool,
    ) -> np.ndarray:
        out = values.astype(float).copy()
        if axis_neg:
            out = -out
        if axis_log:
            out = np.where(out > 0, np.log10(out), np.nan)
        return out

    def _display_z_level(self, level: float, *, z_log: bool) -> float:
        """Convert an internal contour level to the value shown to users."""
        if not np.isfinite(level):
            return float("nan")
        if z_log:
            return float(10.0 ** level)
        return float(level)

    def _render_contour_plot(
        self,
        x_col: str,
        y_col: str,
        z_col: str,
        x_log: bool,
        y_log: bool,
        x_neg: bool,
        y_neg: bool,
        z_log: bool,
        z_neg: bool,
    ) -> bool:
        x = self._transform_axis_values(
            self._df[x_col].to_numpy(dtype=float),
            axis_log=x_log,
            axis_neg=x_neg,
        )
        y = self._transform_axis_values(
            self._df[y_col].to_numpy(dtype=float),
            axis_log=y_log,
            axis_neg=y_neg,
        )
        z = self._transform_z_values(
            self._df[z_col].to_numpy(dtype=float),
            z_log=z_log,
            z_neg=z_neg,
        )

        valid = np.isfinite(x) & np.isfinite(y) & np.isfinite(z)
        x = x[valid]
        y = y[valid]
        z = z[valid]
        if len(z) < 4:
            return False

        x_vals = np.unique(np.round(x, 9))
        y_vals = np.unique(np.round(y, 9))
        if len(x_vals) < 2 or len(y_vals) < 2:
            return False

        x0 = float(x_vals.min())
        x1 = float(x_vals.max())
        y0 = float(y_vals.min())
        y1 = float(y_vals.max())

        # Normalize x and y to [0, 1] so interpolation distance is balanced.
        x_span = x1 - x0 if x1 > x0 else 1.0
        y_span = y1 - y0 if y1 > y0 else 1.0
        x_norm = (x - x0) / x_span
        y_norm = (y - y0) / y_span

        nx = max(_CONTOUR_MIN_GRID_SIZE, len(x_vals))
        ny = max(_CONTOUR_MIN_GRID_SIZE, len(y_vals))
        x_grid_norm = np.linspace(0, 1, nx)
        y_grid_norm = np.linspace(0, 1, ny)

        # Build a dense regular grid in normalized space.
        xx, yy = np.meshgrid(x_grid_norm, y_grid_norm, indexing="ij")
        # xx[i, j] = x_grid[i], yy[i, j] = y_grid[j]
        # xx.shape = yy.shape = (len(x_grid), len(y_grid))

        # Prefer RectBivariateSpline on full rectangular sweep grids.
        try:
            src_x_norm = np.sort(np.unique(np.round(x_norm, 9)))
            src_y_norm = np.sort(np.unique(np.round(y_norm, 9)))

            # Build z(x_norm, y_norm) plane if we have a complete rectangular grid.
            z_plane = np.full((len(src_x_norm), len(src_y_norm)), np.nan, dtype=float)
            x_idx = {float(v): i for i, v in enumerate(src_x_norm)}
            y_idx = {float(v): j for j, v in enumerate(src_y_norm)}
            for xv, yv, zv in zip(np.round(x_norm, 9), np.round(y_norm, 9), z):
                i = x_idx.get(float(xv))
                j = y_idx.get(float(yv))
                if i is not None and j is not None:
                    z_plane[i, j] = float(zv)

            if np.isfinite(z_plane).all() and len(src_x_norm) >= 2 and len(src_y_norm) >= 2:
                kx = min(3, len(src_x_norm) - 1)
                ky = min(3, len(src_y_norm) - 1)
                spline = RectBivariateSpline(src_x_norm, src_y_norm, z_plane, kx=kx, ky=ky, s=0)
                grid = spline(x_grid_norm, y_grid_norm)
            else:
                points = np.column_stack([x_norm, y_norm])
                xi = np.column_stack([xx.ravel(), yy.ravel()])
                zi = griddata(points, z, xi, method="linear", fill_value=np.nan)
                grid = zi.reshape(xx.shape)
        except Exception:
            return False
        # grid[i, j] = z at (x_grid[i], y_grid[j]) — matches ImageItem expectations exactly

        if not np.isfinite(grid).any():
            return False
        finite_mask = np.isfinite(grid)
        if not finite_mask.all():
            grid[~finite_mask] = float(np.nanmean(grid))

        cmap = pg.colormap.get("viridis")
        lut = cmap.getLookupTable(nPts=256)
        z_min = float(np.nanmin(grid))
        z_max = float(np.nanmax(grid))
        if z_max <= z_min:
            z_max = z_min + 1.0

        image = pg.ImageItem(grid)
        image.setLookupTable(lut)
        image.setLevels((z_min, z_max))

        # Set the image rect in data coordinates.
        # grid[0, 0] = z(x_min, y_min) = bottom-left, matching setRect(x0, y0, w, h).
        width = x1 - x0 if x1 > x0 else 1.0
        height = y1 - y0 if y1 > y0 else 1.0
        
        image.setRect(QRectF(x0, y0, width, height))
        self.plot_widget.addItem(image)
        self._plot_items.append(image)

        levels = np.linspace(z_min, z_max, 8)
        cx = (nx - 1) / 2.0
        cy = (ny - 1) / 2.0
        # Grid is in normalized space [0, 1], so convert back to original coordinates.
        for level in levels[1:-1]:
            lv = float(level)
            curve = pg.IsocurveItem(level=lv, pen=pg.mkPen(QColor(15, 23, 42, 180), width=1))
            curve.setData(grid)
            curve.setParentItem(image)
            curve.setZValue(10)
            self._plot_items.append(curve)

            # Find the crossing of this isocurve nearest the centre of the grid.
            # Grid indices are in [0, nx-1] and [0, ny-1], but grid data is in normalized space.
            best_dist = float("inf")
            label_x = label_y = None
            for xi in range(nx - 1):
                for yi in range(ny):
                    v0 = grid[xi, yi]
                    v1 = grid[xi + 1, yi]
                    if np.isfinite(v0) and np.isfinite(v1) and (v0 - lv) * (v1 - lv) <= 0:
                        t = (lv - v0) / (v1 - v0) if v1 != v0 else 0.5
                        dist = (xi + t - cx) ** 2 + (yi - cy) ** 2
                        if dist < best_dist:
                            best_dist = dist
                            # Convert grid indices (in normalized space) back to original coordinates.
                            x_norm_at_crossing = x_grid_norm[xi] + t * (x_grid_norm[xi + 1] - x_grid_norm[xi])
                            y_norm_at_crossing = y_grid_norm[yi]
                            label_x = x0 + x_norm_at_crossing * x_span
                            label_y = y0 + y_norm_at_crossing * y_span
            if label_x is not None:
                display_level = self._display_z_level(lv, z_log=z_log)
                lbl = pg.TextItem(
                    html=(
                        "<div style=\""
                        "background-color: rgba(255, 255, 255, 230);"
                        "color: rgb(15, 23, 42);"
                        "border: 1px solid rgba(15, 23, 42, 110);"
                        "border-radius: 3px;"
                        "padding: 1px 3px;"
                        "\">"
                        f"{_eng_format(display_level)}"
                        "</div>"
                    ),
                    anchor=(0.5, 0.5),
                )
                font = lbl.textItem.font()
                font.setPointSize(7)
                lbl.textItem.setFont(font)
                lbl.setPos(label_x, label_y)
                self.plot_widget.addItem(lbl)
                self._plot_items.append(lbl)

        scatter = pg.ScatterPlotItem(
            x=x,
            y=y,
            size=4,
            brush=pg.mkBrush(255, 255, 255, 80),
            pen=pg.mkPen(QColor(15, 23, 42, 120), width=0.6),
        )
        self.plot_widget.addItem(scatter)
        self._plot_items.append(scatter)
        return True
