# -*- coding: utf-8 -*-
"""
AHP suitability (multi-criteria) tool for ArchToolkit.

Goal
- Combine existing environmental rasters into a single suitability raster using
  AHP (pairwise comparison) weights.

Design notes
- Best-effort and stable: never crash QGIS due to UI/processing errors.
- Uses GDAL processing (`gdal:rastercalculator`, `gdal:warpreproject`) which is
  available in QGIS by default.
"""

from __future__ import annotations

import math
import os
import tempfile
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None

import processing
from qgis.PyQt import QtWidgets
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor, QIcon
from qgis.core import (
    QgsColorRampShader,
    QgsCoordinateTransform,
    QgsProject,
    QgsRasterBandStats,
    QgsRasterLayer,
    QgsRasterShader,
    QgsRectangle,
    QgsSingleBandPseudoColorRenderer,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.gui import QgsMapLayerComboBox

from .live_log_dialog import ensure_live_log_dialog
from .utils import (
    get_archtoolkit_layer_metadata,
    log_exception,
    new_run_id,
    push_message,
    restore_ui_focus,
    set_archtoolkit_layer_metadata,
)


_RI_TABLE = {
    1: 0.00,
    2: 0.00,
    3: 0.58,
    4: 0.90,
    5: 1.12,
    6: 1.24,
    7: 1.32,
    8: 1.41,
    9: 1.45,
    10: 1.49,
}


_SCALE_OPTIONS: List[Tuple[str, float]] = [
    ("1/9", 1.0 / 9.0),
    ("1/8", 1.0 / 8.0),
    ("1/7", 1.0 / 7.0),
    ("1/6", 1.0 / 6.0),
    ("1/5", 1.0 / 5.0),
    ("1/4", 1.0 / 4.0),
    ("1/3", 1.0 / 3.0),
    ("1/2", 1.0 / 2.0),
    ("1", 1.0),
    ("2", 2.0),
    ("3", 3.0),
    ("4", 4.0),
    ("5", 5.0),
    ("6", 6.0),
    ("7", 7.0),
    ("8", 8.0),
    ("9", 9.0),
]


def _split_qgis_source_path(src: str) -> str:
    try:
        s = str(src or "")
        return (s.split("|", 1)[0] or "").strip()
    except Exception:
        return str(src or "").strip()


def _fmt_float(v: Any, *, digits: int = 4) -> str:
    try:
        if v is None:
            return "-"
        x = float(v)
        if not math.isfinite(x):
            return "-"
        return f"{x:.{int(digits)}f}"
    except Exception:
        return str(v)


def _aoi_extent_in_crs(aoi_layer: QgsVectorLayer, *, selected_only: bool, dst_crs) -> Optional[QgsRectangle]:
    if aoi_layer is None:
        return None
    try:
        if aoi_layer.geometryType() != QgsWkbTypes.PolygonGeometry:
            return None
    except Exception:
        return None

    geom = None
    feats = aoi_layer.selectedFeatures() if selected_only and aoi_layer.selectedFeatureCount() > 0 else aoi_layer.getFeatures()
    for f in feats:
        try:
            g = f.geometry()
        except Exception:
            continue
        if not g or g.isEmpty():
            continue
        if geom is None:
            geom = g
        else:
            try:
                geom = geom.combine(g)
            except Exception:
                pass

    if geom is None or geom.isEmpty():
        return None

    try:
        if aoi_layer.crs() != dst_crs:
            ct = QgsCoordinateTransform(aoi_layer.crs(), dst_crs, QgsProject.instance())
            g2 = type(geom)(geom)  # copy
            g2.transform(ct)
            geom = g2
    except Exception:
        return None

    try:
        return geom.boundingBox()
    except Exception:
        return None


@dataclass
class _Criterion:
    layer_id: str
    direction: str  # "benefit" or "cost"
    min_v: Optional[float] = None
    max_v: Optional[float] = None
    weight: Optional[float] = None


def _ahp_weights_from_matrix(mat: "np.ndarray") -> Tuple[List[float], float, float]:
    """Return (weights, lambda_max, CR)."""
    n = int(mat.shape[0])
    if n <= 0:
        return [], float("nan"), float("nan")
    if n == 1:
        return [1.0], 1.0, 0.0
    if np is None:
        return [1.0 / float(n)] * n, float("nan"), float("nan")

    try:
        vals, vecs = np.linalg.eig(mat)
        idx = int(np.argmax(np.real(vals)))
        lam = float(np.real(vals[idx]))
        v = np.real(vecs[:, idx])
        v = np.abs(v)
        if float(np.sum(v)) <= 0:
            w = np.ones((n,), dtype=float) / float(n)
        else:
            w = v / float(np.sum(v))
        w = [float(x) for x in w.tolist()]
    except Exception:
        w = [1.0 / float(n)] * n
        lam = float("nan")

    cr = 0.0
    try:
        if n <= 2:
            cr = 0.0
        else:
            ci = (float(lam) - float(n)) / float(n - 1)
            ri = float(_RI_TABLE.get(n, 0.0))
            cr = float(ci / ri) if ri > 0 else 0.0
    except Exception:
        cr = float("nan")
    return w, float(lam), float(cr)


class AhpSuitabilityDialog(QtWidgets.QDialog):
    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self._criteria: List[_Criterion] = []
        self._pairwise: Dict[Tuple[int, int], float] = {}
        self._setup_ui()
        self._rebuild_pairwise_table()

    def _setup_ui(self):
        self.setWindowTitle("AHP 입지적합도 (Suitability) - ArchToolkit")
        try:
            plugin_dir = os.path.dirname(os.path.dirname(__file__))
            for icon_name in ("AHP.png", "ahp.png", "icon.png"):
                icon_path = os.path.join(plugin_dir, icon_name)
                if os.path.exists(icon_path):
                    self.setWindowIcon(QIcon(icon_path))
                    break
        except Exception:
            pass

        layout = QtWidgets.QVBoxLayout(self)

        header = QtWidgets.QLabel(
            "<b>AHP 입지적합도</b><br>"
            "만들어진 환경변수(래스터)를 AHP(쌍대비교) 가중치로 통합해 적합도 래스터를 생성합니다.<br>"
            "<i>Tip: AOI를 지정하고 ‘AOI 범위로 자르기’를 켜면 결과가 가벼워집니다.</i><br>"
            "<span style='color:#455a64;'>Reference: Saaty (1980) The Analytic Hierarchy Process</span>"
        )
        header.setWordWrap(True)
        header.setStyleSheet("background:#f1f8e9; padding:10px; border:1px solid #dcedc8; border-radius:4px;")
        layout.addWidget(header)

        grp_in = QtWidgets.QGroupBox("1. 입력")
        form = QtWidgets.QFormLayout(grp_in)

        self.cmbAoi = QgsMapLayerComboBox(grp_in)
        try:
            from qgis.core import QgsMapLayerProxyModel

            try:
                poly_filter = QgsMapLayerProxyModel.Filter.PolygonLayer
            except Exception:
                poly_filter = QgsMapLayerProxyModel.PolygonLayer
            self.cmbAoi.setFilters(poly_filter)
        except Exception:
            pass
        form.addRow("AOI(선택):", self.cmbAoi)

        self.chkAoiSelectedOnly = QtWidgets.QCheckBox("AOI 선택 피처만 사용")
        form.addRow("", self.chkAoiSelectedOnly)

        self.chkClipToAoiExtent = QtWidgets.QCheckBox("AOI 범위로 자르기(권장)")
        self.chkClipToAoiExtent.setChecked(True)
        form.addRow("", self.chkClipToAoiExtent)

        self.chkAlignToFirst = QtWidgets.QCheckBox("첫 번째 기준 레이어에 정렬(리샘플)")
        self.chkAlignToFirst.setChecked(True)
        form.addRow("", self.chkAlignToFirst)

        layout.addWidget(grp_in)

        # 2) Criteria selection
        grp_crit = QtWidgets.QGroupBox("2. 기준(환경변수) 선택")
        vcrit = QtWidgets.QVBoxLayout(grp_crit)

        row_add = QtWidgets.QHBoxLayout()
        self.cmbRaster = QgsMapLayerComboBox(grp_crit)
        try:
            from qgis.core import QgsMapLayerProxyModel

            try:
                raster_filter = QgsMapLayerProxyModel.Filter.RasterLayer
            except Exception:
                raster_filter = QgsMapLayerProxyModel.RasterLayer
            self.cmbRaster.setFilters(raster_filter)
        except Exception:
            pass
        self.cmbRaster.setAllowEmptyLayer(True)

        self.cmbDirection = QtWidgets.QComboBox()
        self.cmbDirection.addItem("Benefit(값↑ 좋음)", "benefit")
        self.cmbDirection.addItem("Cost(값↓ 좋음)", "cost")

        self.btnAdd = QtWidgets.QPushButton("추가")
        self.btnAdd.clicked.connect(self._on_add_criterion)
        self.btnRemove = QtWidgets.QPushButton("선택 제거")
        self.btnRemove.clicked.connect(self._on_remove_selected_criteria)
        self.btnStats = QtWidgets.QPushButton("통계 계산(min/max)")
        self.btnStats.clicked.connect(self._on_compute_stats)

        row_add.addWidget(QtWidgets.QLabel("래스터:"))
        row_add.addWidget(self.cmbRaster, 1)
        row_add.addWidget(QtWidgets.QLabel("방향:"))
        row_add.addWidget(self.cmbDirection)
        row_add.addWidget(self.btnAdd)
        row_add.addWidget(self.btnRemove)
        row_add.addWidget(self.btnStats)
        vcrit.addLayout(row_add)

        self.tblCriteria = QtWidgets.QTableWidget()
        self.tblCriteria.setColumnCount(5)
        self.tblCriteria.setHorizontalHeaderLabels(["레이어", "방향", "min", "max", "weight"])
        self.tblCriteria.horizontalHeader().setStretchLastSection(True)
        self.tblCriteria.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.tblCriteria.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.tblCriteria.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        vcrit.addWidget(self.tblCriteria, 1)

        layout.addWidget(grp_crit, 1)

        # 3) Pairwise comparison
        grp_w = QtWidgets.QGroupBox("3. AHP 가중치(쌍대비교)")
        vw = QtWidgets.QVBoxLayout(grp_w)

        hint = QtWidgets.QLabel(
            "표의 (i, j) 값은 i 기준이 j 기준보다 얼마나 중요한지를 의미합니다.\n"
            "- 1: 동일 중요\n"
            "- 3/5/7/9: 점점 더 중요 (반대로 덜 중요하면 1/3, 1/5 ...)"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#455a64;")
        vw.addWidget(hint)

        self.tblPairwise = QtWidgets.QTableWidget()
        self.tblPairwise.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        vw.addWidget(self.tblPairwise, 1)

        row_w = QtWidgets.QHBoxLayout()
        self.btnResetPairwise = QtWidgets.QPushButton("초기화(모두 1)")
        self.btnResetPairwise.clicked.connect(self._on_reset_pairwise)
        self.lblConsistency = QtWidgets.QLabel("CR: -")
        self.lblConsistency.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        try:
            self.lblConsistency.setToolTip("일관성비율(CR). 일반적으로 CR ≤ 0.10 권장 (Saaty, 1980).")
        except Exception:
            pass
        row_w.addWidget(self.btnResetPairwise)
        row_w.addStretch(1)
        row_w.addWidget(self.lblConsistency)
        vw.addLayout(row_w)

        layout.addWidget(grp_w, 2)

        # 4) Output
        grp_out = QtWidgets.QGroupBox("4. 출력")
        fout = QtWidgets.QFormLayout(grp_out)

        self.txtOut = QtWidgets.QLineEdit()
        self.txtOut.setPlaceholderText("(비우면 임시 파일로 생성 후 프로젝트에 추가)")
        self.btnBrowse = QtWidgets.QPushButton("찾기…")
        self.btnBrowse.clicked.connect(self._on_browse_out)
        w_out = QtWidgets.QWidget()
        h_out = QtWidgets.QHBoxLayout(w_out)
        h_out.setContentsMargins(0, 0, 0, 0)
        h_out.addWidget(self.txtOut, 1)
        h_out.addWidget(self.btnBrowse)
        fout.addRow("출력 GeoTIFF:", w_out)

        self.chkScale100 = QtWidgets.QCheckBox("0–100 스케일로 변환")
        self.chkScale100.setChecked(False)
        fout.addRow("", self.chkScale100)

        self.chkAddToProject = QtWidgets.QCheckBox("완료 후 프로젝트에 추가")
        self.chkAddToProject.setChecked(True)
        fout.addRow("", self.chkAddToProject)

        layout.addWidget(grp_out)

        btn_row = QtWidgets.QHBoxLayout()
        self.btnRun = QtWidgets.QPushButton("실행")
        self.btnRun.clicked.connect(self._on_run)
        self.btnClose = QtWidgets.QPushButton("닫기")
        self.btnClose.clicked.connect(self.reject)
        btn_row.addWidget(self.btnRun)
        btn_row.addStretch(1)
        btn_row.addWidget(self.btnClose)
        layout.addLayout(btn_row)

        self.resize(920, 720)

    def _criterion_layer(self, crit: _Criterion) -> Optional[QgsRasterLayer]:
        try:
            lyr = QgsProject.instance().mapLayer(str(crit.layer_id or ""))
            return lyr if isinstance(lyr, QgsRasterLayer) else None
        except Exception:
            return None

    def _on_add_criterion(self):
        lyr = self.cmbRaster.currentLayer()
        if lyr is None or not isinstance(lyr, QgsRasterLayer):
            push_message(self.iface, "오류", "기준으로 사용할 래스터 레이어를 선택하세요.", level=2, duration=6)
            return

        lid = str(lyr.id() or "")
        if any(c.layer_id == lid for c in self._criteria):
            push_message(self.iface, "정보", "이미 추가된 레이어입니다.", level=1, duration=4)
            return

        direction = str(self.cmbDirection.currentData() or "benefit")
        self._criteria.append(_Criterion(layer_id=lid, direction=direction))
        self._refresh_criteria_table()
        self._rebuild_pairwise_table()

    def _on_remove_selected_criteria(self):
        rows = sorted({idx.row() for idx in self.tblCriteria.selectionModel().selectedRows()}, reverse=True)
        if not rows:
            return
        try:
            for r in rows:
                if 0 <= r < len(self._criteria):
                    del self._criteria[r]
        except Exception:
            pass
        self._refresh_criteria_table()
        self._rebuild_pairwise_table()

    def _refresh_criteria_table(self):
        self.tblCriteria.setRowCount(0)
        for i, crit in enumerate(self._criteria):
            lyr = self._criterion_layer(crit)
            name = str(lyr.name() if lyr is not None else "(레이어 없음)")

            self.tblCriteria.insertRow(i)

            it = QtWidgets.QTableWidgetItem(name)
            it.setData(Qt.UserRole, str(crit.layer_id))
            self.tblCriteria.setItem(i, 0, it)

            cmb = QtWidgets.QComboBox()
            cmb.addItem("Benefit(값↑)", "benefit")
            cmb.addItem("Cost(값↓)", "cost")
            try:
                idx = cmb.findData(str(crit.direction or "benefit"))
                if idx >= 0:
                    cmb.setCurrentIndex(idx)
            except Exception:
                pass

            def _on_dir_changed(_=None, row=i, w=cmb):
                try:
                    v = str(w.currentData() or "benefit")
                    if 0 <= int(row) < len(self._criteria):
                        self._criteria[int(row)].direction = v
                except Exception:
                    pass

            cmb.currentIndexChanged.connect(_on_dir_changed)
            self.tblCriteria.setCellWidget(i, 1, cmb)

            self.tblCriteria.setItem(i, 2, QtWidgets.QTableWidgetItem(_fmt_float(crit.min_v)))
            self.tblCriteria.setItem(i, 3, QtWidgets.QTableWidgetItem(_fmt_float(crit.max_v)))
            self.tblCriteria.setItem(i, 4, QtWidgets.QTableWidgetItem(_fmt_float(crit.weight, digits=6)))

        try:
            self.tblCriteria.resizeColumnsToContents()
        except Exception:
            pass

        self._update_consistency_and_weights()

    def _rebuild_pairwise_table(self):
        n = int(len(self._criteria))
        self._pairwise = {(i, j): 1.0 for i in range(n) for j in range(i + 1, n)}

        self.tblPairwise.clear()
        self.tblPairwise.setRowCount(n)
        self.tblPairwise.setColumnCount(n)

        headers = []
        for c in self._criteria:
            lyr = self._criterion_layer(c)
            name = str(lyr.name() if lyr is not None else "(레이어)")
            headers.append(name[:18] + ("…" if len(name) > 18 else ""))

        self.tblPairwise.setHorizontalHeaderLabels(headers)
        self.tblPairwise.setVerticalHeaderLabels(headers)

        for i in range(n):
            for j in range(n):
                if i == j:
                    item = QtWidgets.QTableWidgetItem("1")
                    item.setFlags(item.flags() & ~Qt.ItemIsEnabled)
                    self.tblPairwise.setItem(i, j, item)
                    continue

                if i < j:
                    cmb = QtWidgets.QComboBox()
                    for label, val in _SCALE_OPTIONS:
                        cmb.addItem(label, float(val))
                    cmb.setCurrentIndex(8)  # "1"

                    def _on_changed(_=None, row=i, col=j, w=cmb):
                        try:
                            v = float(w.currentData() or 1.0)
                        except Exception:
                            v = 1.0
                        self._pairwise[(int(row), int(col))] = float(v)
                        self._set_reciprocal_cell(int(row), int(col), float(v))
                        self._update_consistency_and_weights()

                    cmb.currentIndexChanged.connect(_on_changed)
                    self.tblPairwise.setCellWidget(i, j, cmb)
                else:
                    item = QtWidgets.QTableWidgetItem("1")
                    item.setFlags(item.flags() & ~Qt.ItemIsEnabled)
                    self.tblPairwise.setItem(i, j, item)

        try:
            self.tblPairwise.resizeColumnsToContents()
            self.tblPairwise.resizeRowsToContents()
        except Exception:
            pass
        self._update_consistency_and_weights()

    def _set_reciprocal_cell(self, i: int, j: int, v: float):
        try:
            vv = 1.0 / float(v) if float(v) > 0 else 0.0
        except Exception:
            vv = 1.0

        try:
            label = None
            for s_label, s_val in _SCALE_OPTIONS:
                try:
                    if abs(float(s_val) - float(vv)) <= 1e-9:
                        label = str(s_label)
                        break
                except Exception:
                    continue
            if label is None:
                label = _fmt_float(vv, digits=4)

            item = self.tblPairwise.item(int(j), int(i))
            if item is None:
                item = QtWidgets.QTableWidgetItem(label)
                item.setFlags(item.flags() & ~Qt.ItemIsEnabled)
                self.tblPairwise.setItem(int(j), int(i), item)
            else:
                item.setText(label)
        except Exception:
            pass

    def _on_reset_pairwise(self):
        self._rebuild_pairwise_table()

    def _build_pairwise_matrix(self) -> Optional["np.ndarray"]:
        n = int(len(self._criteria))
        if n <= 0 or np is None:
            return None
        mat = np.ones((n, n), dtype=float)
        for (i, j), v in (self._pairwise or {}).items():
            try:
                i0 = int(i)
                j0 = int(j)
                v0 = float(v)
                if i0 == j0:
                    continue
                if v0 <= 0:
                    v0 = 1.0
                mat[i0, j0] = v0
                mat[j0, i0] = 1.0 / v0
            except Exception:
                continue
        return mat

    def _update_consistency_and_weights(self):
        n = int(len(self._criteria))
        if n <= 0:
            self.lblConsistency.setText("CR: -")
            return

        mat = self._build_pairwise_matrix()
        if mat is None:
            for c in self._criteria:
                c.weight = 1.0 / float(n)
            self.lblConsistency.setText("CR: - (numpy 없음: 균등 가중치)")
            self._update_criteria_weight_column()
            return

        w, lam, cr = _ahp_weights_from_matrix(mat)
        for i, c in enumerate(self._criteria):
            try:
                c.weight = float(w[i])
            except Exception:
                c.weight = None

        cr_txt = _fmt_float(cr, digits=3) if math.isfinite(float(cr)) else "-"
        lam_txt = _fmt_float(lam, digits=3) if math.isfinite(float(lam)) else "-"
        note = ""
        try:
            if math.isfinite(float(cr)) and float(cr) > 0.10:
                note = " (주의: 0.10 초과)"
        except Exception:
            note = ""
        self.lblConsistency.setText(f"λmax={lam_txt}, CR={cr_txt}{note}")
        self._update_criteria_weight_column()

    def _update_criteria_weight_column(self):
        try:
            for r, c in enumerate(self._criteria):
                self.tblCriteria.setItem(r, 4, QtWidgets.QTableWidgetItem(_fmt_float(c.weight, digits=6)))
        except Exception:
            pass

    def _extent_for_raster_stats(self, raster: QgsRasterLayer) -> Optional[QgsRectangle]:
        aoi = self.cmbAoi.currentLayer()
        if aoi is None or not isinstance(aoi, QgsVectorLayer):
            return None
        if not self.chkClipToAoiExtent.isChecked():
            return None
        try:
            return _aoi_extent_in_crs(aoi, selected_only=bool(self.chkAoiSelectedOnly.isChecked()), dst_crs=raster.crs())
        except Exception:
            return None

    def _compute_minmax_for_layer(self, raster: QgsRasterLayer) -> Tuple[Optional[float], Optional[float]]:
        if raster is None or not isinstance(raster, QgsRasterLayer):
            return None, None
        try:
            dp = raster.dataProvider()
            extent = self._extent_for_raster_stats(raster)
            stats = dp.bandStatistics(1, QgsRasterBandStats.Min | QgsRasterBandStats.Max, extent or QgsRectangle(), 0)
            mn = float(stats.minimumValue) if stats is not None else None
            mx = float(stats.maximumValue) if stats is not None else None
            if mn is not None and mx is not None and math.isfinite(mn) and math.isfinite(mx):
                return mn, mx
            return None, None
        except Exception:
            return None, None

    def _on_compute_stats(self):
        if not self._criteria:
            return
        for c in self._criteria:
            lyr = self._criterion_layer(c)
            if lyr is None:
                continue
            mn, mx = self._compute_minmax_for_layer(lyr)
            c.min_v = mn
            c.max_v = mx
        self._refresh_criteria_table()
        push_message(self.iface, "AHP", "통계(min/max) 계산 완료", level=0, duration=4)

    def _on_browse_out(self):
        path, _flt = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "AHP 적합도 래스터 저장",
            "ahp_suitability.tif",
            "GeoTIFF (*.tif *.tiff);;All Files (*.*)",
        )
        if not path:
            return
        self.txtOut.setText(str(path))

    def _processing_warp_to_reference(
        self,
        *,
        input_path: str,
        ref_layer: QgsRasterLayer,
        out_path: str,
        extent_str: Optional[str],
        extent_crs_authid: Optional[str],
    ) -> str:
        pixel = None
        try:
            pixel = float(ref_layer.rasterUnitsPerPixelX())
        except Exception:
            pixel = None
        if pixel is None or (not math.isfinite(pixel)) or pixel <= 0:
            pixel = None

        params = {
            "INPUT": str(input_path),
            "SOURCE_CRS": None,
            "TARGET_CRS": str(ref_layer.crs().authid() or ""),
            "RESAMPLING": 1,  # bilinear
            "NODATA": None,
            "TARGET_RESOLUTION": pixel,
            "OPTIONS": "",
            "DATA_TYPE": 0,
            "TARGET_EXTENT": extent_str,
            "TARGET_EXTENT_CRS": extent_crs_authid,
            "MULTITHREADING": False,
            "EXTRA": "",
            "OUTPUT": str(out_path),
        }
        processing.run("gdal:warpreproject", params)
        return str(out_path)

    def _processing_raster_calc(
        self,
        *,
        input_a: str,
        input_b: Optional[str] = None,
        formula: str,
        out_path: str,
        rtype: int = 5,  # Float32
    ) -> str:
        params: Dict[str, Any] = {
            "INPUT_A": str(input_a),
            "BAND_A": 1,
            "FORMULA": str(formula),
            "OUTPUT": str(out_path),
            "RTYPE": int(rtype),
        }
        if input_b:
            params["INPUT_B"] = str(input_b)
            params["BAND_B"] = 1
        processing.run("gdal:rastercalculator", params)
        return str(out_path)

    def _apply_suitability_style(self, layer: QgsRasterLayer):
        if layer is None or not isinstance(layer, QgsRasterLayer) or (not layer.isValid()):
            return
        try:
            shader = QgsRasterShader()
            ramp = QgsColorRampShader()
            ramp.setColorRampType(QgsColorRampShader.Interpolated)
            items = [
                QgsColorRampShader.ColorRampItem(0.0, QColor("#d73027"), "Low"),
                QgsColorRampShader.ColorRampItem(0.5, QColor("#fee08b"), "Mid"),
                QgsColorRampShader.ColorRampItem(1.0, QColor("#1a9850"), "High"),
            ]
            ramp.setColorRampItemList(items)
            shader.setRasterShaderFunction(ramp)
            renderer = QgsSingleBandPseudoColorRenderer(layer.dataProvider(), 1, shader)
            layer.setRenderer(renderer)
            layer.triggerRepaint()
        except Exception:
            pass

    def _add_output_to_project(self, out_path: str, *, run_id: str, cr: Optional[float]) -> Optional[QgsRasterLayer]:
        try:
            layer_name = "AHP Suitability"
            try:
                aoi = self.cmbAoi.currentLayer()
                if aoi is not None:
                    layer_name = f"AHP Suitability ({aoi.name()})"
            except Exception:
                pass
            layer = QgsRasterLayer(str(out_path), layer_name)
        except Exception:
            return None

        if layer is None or not layer.isValid():
            return None

        try:
            params = {
                "criteria": [
                    {
                        "layer_id": c.layer_id,
                        "layer_name": (self._criterion_layer(c).name() if self._criterion_layer(c) is not None else ""),
                        "direction": c.direction,
                        "min": c.min_v,
                        "max": c.max_v,
                        "weight": c.weight,
                        "archtoolkit_meta": (get_archtoolkit_layer_metadata(self._criterion_layer(c)) if self._criterion_layer(c) is not None else {}),
                    }
                    for c in self._criteria
                ],
                "consistency_ratio": cr,
                "clip_to_aoi_extent": bool(self.chkClipToAoiExtent.isChecked()),
                "align_to_first": bool(self.chkAlignToFirst.isChecked()),
                "scale_0_100": bool(self.chkScale100.isChecked()),
            }
            set_archtoolkit_layer_metadata(
                layer,
                tool_id="ahp_suitability",
                run_id=str(run_id),
                kind="suitability",
                units="0-100" if self.chkScale100.isChecked() else "0-1",
                params=params,
            )
        except Exception:
            pass

        project = QgsProject.instance()
        root = project.layerTreeRoot()
        parent_name = "ArchToolkit - AHP"
        parent_group = root.findGroup(parent_name)
        if parent_group is None:
            parent_group = root.insertGroup(0, parent_name)
        try:
            if parent_group.parent() == root:
                idx = root.children().index(parent_group)
                if idx != 0:
                    root.removeChildNode(parent_group)
                    root.insertChildNode(0, parent_group)
        except Exception:
            pass

        try:
            run_group = parent_group.insertGroup(0, f"AHP_{run_id}")
            run_group.setExpanded(False)
        except Exception:
            run_group = parent_group

        try:
            project.addMapLayer(layer, False)
            run_group.insertLayer(0, layer)
        except Exception:
            try:
                project.addMapLayer(layer, True)
            except Exception:
                pass

        self._apply_suitability_style(layer)
        return layer

    def _on_run(self):
        if not self._criteria:
            push_message(self.iface, "오류", "기준(래스터)을 최소 1개 이상 추가하세요.", level=2, duration=6)
            restore_ui_focus(self)
            return

        try:
            ensure_live_log_dialog(self.iface, owner=self, show=True, clear=True)
        except Exception:
            pass

        run_id = new_run_id("ahp")
        push_message(self.iface, "AHP", "가중치/통계 계산 중…", level=0, duration=4)

        # 1) Weights
        n = int(len(self._criteria))
        cr = None
        if n == 1:
            self._criteria[0].weight = 1.0
            cr = 0.0
        else:
            mat = self._build_pairwise_matrix()
            if mat is None:
                for c in self._criteria:
                    c.weight = 1.0 / float(n)
                cr = None
            else:
                w, _lam, cr0 = _ahp_weights_from_matrix(mat)
                for i, c in enumerate(self._criteria):
                    try:
                        c.weight = float(w[i])
                    except Exception:
                        c.weight = None
                cr = float(cr0) if math.isfinite(float(cr0)) else None
        self._refresh_criteria_table()

        try:
            if cr is not None and cr > 0.10:
                push_message(self.iface, "주의", f"AHP 일관성비율(CR)이 높습니다: {cr:.3f} (권장 ≤ 0.10)", level=1, duration=8)
        except Exception:
            pass

        # 2) Stats
        for c in self._criteria:
            if c.min_v is not None and c.max_v is not None:
                continue
            lyr = self._criterion_layer(c)
            if lyr is None:
                continue
            mn, mx = self._compute_minmax_for_layer(lyr)
            c.min_v = mn
            c.max_v = mx
        self._refresh_criteria_table()

        # 3) Reference raster
        ref_layer = self._criterion_layer(self._criteria[0])
        if ref_layer is None or not ref_layer.isValid():
            push_message(self.iface, "오류", "첫 번째 기준 레이어를 찾을 수 없습니다.", level=2, duration=7)
            return

        # 4) AOI extent (optional)
        aoi_layer = self.cmbAoi.currentLayer()
        if aoi_layer is not None and isinstance(aoi_layer, QgsVectorLayer):
            try:
                if aoi_layer.geometryType() != QgsWkbTypes.PolygonGeometry:
                    push_message(self.iface, "오류", "AOI는 폴리곤 레이어여야 합니다.", level=2, duration=7)
                    return
            except Exception:
                pass

        extent_str = None
        extent_crs = None
        if self.chkClipToAoiExtent.isChecked() and aoi_layer is not None and isinstance(aoi_layer, QgsVectorLayer):
            try:
                ext = _aoi_extent_in_crs(aoi_layer, selected_only=bool(self.chkAoiSelectedOnly.isChecked()), dst_crs=ref_layer.crs())
                if ext is not None and (not ext.isEmpty()):
                    extent_str = f"{ext.xMinimum()},{ext.xMaximum()},{ext.yMinimum()},{ext.yMaximum()}"
                    extent_crs = str(ref_layer.crs().authid() or "")
            except Exception:
                extent_str = None
                extent_crs = None

        # 5) Output path
        out_path_user = str(self.txtOut.text() or "").strip()
        if out_path_user:
            out_path_user = os.path.abspath(out_path_user)
            if not out_path_user.lower().endswith((".tif", ".tiff")):
                out_path_user = out_path_user + ".tif"
        else:
            out_path_user = os.path.join(tempfile.gettempdir(), f"archtoolkit_ahp_suitability_{run_id}.tif")

        tmp_paths: List[str] = []

        def _tmp(name: str) -> str:
            p = os.path.join(tempfile.gettempdir(), f"archtoolkit_ahp_{name}_{run_id}.tif")
            tmp_paths.append(p)
            return p

        def _safe_rm(path: str):
            try:
                if path and os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass

        # 6) Compute suitability
        try:
            push_message(self.iface, "AHP", "래스터 정규화/가중합 계산 중…", level=0, duration=6)

            align = bool(self.chkAlignToFirst.isChecked())
            acc_path = None

            for idx, c in enumerate(self._criteria):
                lyr = self._criterion_layer(c)
                if lyr is None or (not lyr.isValid()):
                    raise Exception("기준 레이어가 유효하지 않습니다.")

                src0 = _split_qgis_source_path(lyr.source())
                if not src0:
                    raise Exception("래스터 소스 경로를 읽을 수 없습니다.")

                in_path = src0
                if align:
                    warped = _tmp(f"warp_{idx}")
                    in_path = self._processing_warp_to_reference(
                        input_path=in_path,
                        ref_layer=ref_layer,
                        out_path=warped,
                        extent_str=extent_str,
                        extent_crs_authid=extent_crs,
                    )

                try:
                    mn = float(c.min_v) if c.min_v is not None else None
                    mx = float(c.max_v) if c.max_v is not None else None
                except Exception:
                    mn, mx = None, None

                if mn is None or mx is None or (not math.isfinite(mn)) or (not math.isfinite(mx)):
                    raise Exception(f"min/max 통계가 없습니다: {lyr.name()}")

                denom = float(mx - mn)
                if (not math.isfinite(denom)) or denom == 0:
                    norm_path = _tmp(f"norm_{idx}")
                    self._processing_raster_calc(input_a=in_path, formula="A*0", out_path=norm_path)
                else:
                    if str(c.direction or "benefit") == "cost":
                        formula = f"({mx} - A) / ({mx} - {mn})"
                    else:
                        formula = f"(A - {mn}) / ({mx} - {mn})"
                    norm_path = _tmp(f"norm_{idx}")
                    self._processing_raster_calc(input_a=in_path, formula=formula, out_path=norm_path)

                w0 = float(c.weight) if c.weight is not None else (1.0 / float(n))
                weighted_path = _tmp(f"w_{idx}")
                self._processing_raster_calc(input_a=norm_path, formula=f"A * {w0}", out_path=weighted_path)

                if acc_path is None:
                    acc_path = weighted_path
                else:
                    new_acc = _tmp(f"acc_{idx}")
                    self._processing_raster_calc(input_a=acc_path, input_b=weighted_path, formula="A + B", out_path=new_acc)
                    _safe_rm(acc_path)
                    _safe_rm(weighted_path)
                    acc_path = new_acc

            if acc_path is None:
                raise Exception("가중합 결과를 생성할 수 없습니다.")

            if self.chkScale100.isChecked():
                scaled = _tmp("scaled")
                self._processing_raster_calc(input_a=acc_path, formula="A * 100.0", out_path=scaled)
                _safe_rm(acc_path)
                acc_path = scaled

            final_path = out_path_user
            if os.path.abspath(acc_path) != os.path.abspath(final_path):
                try:
                    os.makedirs(os.path.dirname(final_path), exist_ok=True)
                except Exception:
                    pass
                try:
                    if os.path.exists(final_path):
                        os.remove(final_path)
                except Exception:
                    pass
                try:
                    os.replace(acc_path, final_path)
                except Exception:
                    import shutil

                    shutil.copyfile(acc_path, final_path)
            else:
                final_path = acc_path

            push_message(self.iface, "AHP", f"완료: {final_path}", level=0, duration=6)

            if self.chkAddToProject.isChecked():
                lyr_out = self._add_output_to_project(final_path, run_id=str(run_id), cr=cr)
                if lyr_out is None:
                    push_message(self.iface, "경고", "결과 레이어를 프로젝트에 추가하지 못했습니다.", level=1, duration=6)

        except Exception as e:
            log_exception("AHP suitability tool error", e)
            push_message(self.iface, "오류", f"AHP 실행 실패: {e}", level=2, duration=10)
            restore_ui_focus(self)
        finally:
            try:
                keep = {os.path.abspath(out_path_user)}
            except Exception:
                keep = set()
            for p in tmp_paths:
                try:
                    ap = os.path.abspath(p)
                    if ap in keep:
                        continue
                    _safe_rm(p)
                except Exception:
                    pass
