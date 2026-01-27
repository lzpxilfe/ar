# -*- coding: utf-8 -*-
#
# ArchToolkit - Archaeology Toolkit for QGIS
# Copyright (C) 2026 balguljang2
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""
지구화학도(WMS 등) RGB 래스터를 범례 기반으로 수치화하고, 구간별 폴리곤으로 변환합니다.

중요: WMS는 원자료 수치가 아니라 "렌더링된 이미지"이므로, 이 도구는 범례(색-값)를 이용한 역추정입니다.
따라서 안티앨리어싱/경계선/투명도 등으로 인한 오차가 있을 수 있습니다.

현재 프리셋: Fe2O3 (산화철) (사용자가 제공한 범례 포인트 기반)
"""

import math
import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
from osgeo import gdal

import processing
from qgis.PyQt import QtWidgets
from qgis.PyQt.QtCore import Qt, QVariant
from qgis.PyQt.QtGui import QColor, QIcon
from qgis.core import (
    Qgis,
    QgsCategorizedSymbolRenderer,
    QgsCoordinateTransform,
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsMapLayerProxyModel,
    QgsProject,
    QgsRasterLayer,
    QgsRectangle,
    QgsRendererCategory,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.gui import QgsMapLayerComboBox

from .utils import log_message, push_message, restore_ui_focus
from .live_log_dialog import ensure_live_log_dialog


PARENT_GROUP_NAME = "ArchToolkit - GeoChem"


@dataclass(frozen=True)
class LegendPoint:
    value: float
    rgb: Tuple[int, int, int]


@dataclass(frozen=True)
class GeoChemPreset:
    key: str
    label: str
    unit: str
    points: Sequence[LegendPoint]


FE2O3_POINTS: List[LegendPoint] = [
    LegendPoint(0.0, (204, 204, 204)),
    LegendPoint(3.1, (0, 38, 115)),
    LegendPoint(3.5, (0, 112, 255)),
    LegendPoint(3.9, (0, 197, 255)),
    LegendPoint(4.5, (0, 255, 0)),
    LegendPoint(5.7, (85, 255, 0)),
    LegendPoint(7.1, (255, 255, 0)),
    LegendPoint(8.5, (255, 170, 0)),
    LegendPoint(9.4, (255, 85, 0)),
    LegendPoint(12.0, (230, 0, 0)),
    LegendPoint(51.0, (115, 12, 12)),
]

PRESETS: Dict[str, GeoChemPreset] = {
    "fe2o3": GeoChemPreset(key="fe2o3", label="Fe2O3 (산화철)", unit="%", points=FE2O3_POINTS),
}


def _points_to_breaks(points: Sequence[LegendPoint]) -> List[float]:
    vals = [float(p.value) for p in points]
    vals = sorted(set(vals))
    return vals


def _interp_rgb_to_value(*, r: np.ndarray, g: np.ndarray, b: np.ndarray, points: Sequence[LegendPoint]) -> np.ndarray:
    """Vectorized mapping: RGB -> scalar value by projecting to the nearest legend polyline segment in RGB space."""
    if r.shape != g.shape or r.shape != b.shape:
        raise ValueError("RGB bands must have the same shape")
    if len(points) < 2:
        raise ValueError("Need at least 2 legend points")

    rr = r.astype(np.float32, copy=False)
    gg = g.astype(np.float32, copy=False)
    bb = b.astype(np.float32, copy=False)

    out = np.full(rr.shape, np.nan, dtype=np.float32)
    min_dist = np.full(rr.shape, np.float32(np.inf), dtype=np.float32)

    pts = list(points)
    for i in range(len(pts) - 1):
        v1 = float(pts[i].value)
        v2 = float(pts[i + 1].value)
        c1 = pts[i].rgb
        c2 = pts[i + 1].rgb

        vr = float(c2[0] - c1[0])
        vg = float(c2[1] - c1[1])
        vb = float(c2[2] - c1[2])
        v_len_sq = vr * vr + vg * vg + vb * vb
        if v_len_sq <= 0:
            continue

        t = ((rr - float(c1[0])) * vr + (gg - float(c1[1])) * vg + (bb - float(c1[2])) * vb) / float(v_len_sq)
        t = np.clip(t, 0.0, 1.0)
        pr = float(c1[0]) + t * vr
        pg = float(c1[1]) + t * vg
        pb = float(c1[2]) + t * vb
        dist_sq = (rr - pr) ** 2 + (gg - pg) ** 2 + (bb - pb) ** 2

        mask = dist_sq < min_dist
        if not np.any(mask):
            continue
        out = np.where(mask, np.float32(v1 + t * (v2 - v1)), out)
        min_dist = np.where(mask, dist_sq.astype(np.float32, copy=False), min_dist)

    return out


def _mask_black_lines(r: np.ndarray, g: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Detect neutral dark 'linework' (not intense red/brown) and return mask."""
    rr = r.astype(np.int16, copy=False)
    gg = g.astype(np.int16, copy=False)
    bb = b.astype(np.int16, copy=False)
    return (
        (rr < 75)
        & (gg < 75)
        & (bb < 75)
        & (np.abs(rr - gg) < 15)
        & (np.abs(gg - bb) < 15)
    )


def _gdal_fill_nodata_nearestish(*, arr: np.ndarray, nodata: float, max_search_dist_px: int) -> np.ndarray:
    """Fill nodata/NaN using GDAL FillNodata (fast, no scipy).

    Note: This is not a perfect 'nearest' in Euclidean sense, but with smoothingIterations=0 it preserves edges well.
    """
    a = arr.astype(np.float32, copy=True)
    a[~np.isfinite(a)] = float(nodata)

    ysize, xsize = a.shape
    drv = gdal.GetDriverByName("MEM")
    ds = drv.Create("", int(xsize), int(ysize), 1, gdal.GDT_Float32)
    band = ds.GetRasterBand(1)
    band.WriteArray(a)
    band.SetNoDataValue(float(nodata))
    try:
        gdal.FillNodata(
            targetBand=band,
            maskBand=None,
            maxSearchDist=int(max(1, max_search_dist_px)),
            smoothingIterations=0,
        )
    except Exception:
        pass
    filled = band.ReadAsArray().astype(np.float32, copy=False)
    ds = None
    return filled


def _classify_to_bins(*, values: np.ndarray, breaks: Sequence[float], nodata_class: int = 0) -> np.ndarray:
    br = [float(x) for x in breaks]
    if len(br) < 2:
        raise ValueError("Need at least 2 breaks")

    v = values.astype(np.float32, copy=False)
    cls = np.full(v.shape, int(nodata_class), dtype=np.int16)

    finite = np.isfinite(v)
    if not np.any(finite):
        return cls

    vmin = float(br[0])
    vmax = float(br[-1])
    vv = np.clip(v, vmin, vmax)

    bins = br[1:-1]  # internal thresholds
    idx = np.digitize(vv, bins=bins, right=False).astype(np.int16, copy=False)  # 0..n-1
    cls[finite] = idx[finite] + 1  # 1..n_intervals
    return cls


def _interval_label(v0: float, v1: float, unit: str) -> str:
    if unit:
        return f"{v0:g}-{v1:g}{unit}"
    return f"{v0:g}-{v1:g}"


class GeoChemPolygonizeDialog(QtWidgets.QDialog):
    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.setWindowTitle("지구화학도 폴리곤화 (GeoChem WMS → Polygons) - ArchToolkit")

        try:
            plugin_dir = os.path.dirname(os.path.dirname(__file__))
            fallback_icon = os.path.join(plugin_dir, "terrain_icon.png")
            if os.path.exists(fallback_icon):
                self.setWindowIcon(QIcon(fallback_icon))
        except Exception:
            pass

        self._tmp_dir = None

        layout = QtWidgets.QVBoxLayout(self)

        desc = QtWidgets.QLabel(
            "<b>지구화학도 폴리곤화</b><br>"
            "WMS 등 RGB 래스터(이미지)를 범례 기반으로 <b>수치화</b>한 뒤,<br>"
            "<b>구간별 폴리곤</b>으로 변환합니다. (WMS 원자료 수치가 아닌 ‘역추정’입니다.)"
        )
        desc.setWordWrap(True)
        desc.setStyleSheet("background:#f0f0f0; padding:6px; border-radius:4px;")
        layout.addWidget(desc)

        grp_in = QtWidgets.QGroupBox("1. 입력")
        grid = QtWidgets.QGridLayout(grp_in)

        self.cmbRaster = QgsMapLayerComboBox(grp_in)
        self.cmbRaster.setFilters(QgsMapLayerProxyModel.RasterLayer)
        self.cmbAoi = QgsMapLayerComboBox(grp_in)
        self.cmbAoi.setFilters(QgsMapLayerProxyModel.VectorLayer)

        grid.addWidget(QtWidgets.QLabel("RGB 래스터(WMS)"), 0, 0)
        grid.addWidget(self.cmbRaster, 0, 1, 1, 2)
        grid.addWidget(QtWidgets.QLabel("AOI 폴리곤"), 1, 0)
        grid.addWidget(self.cmbAoi, 1, 1, 1, 2)

        self.chkSelectedOnly = QtWidgets.QCheckBox("AOI 선택 피처만 사용")
        self.chkSelectedOnly.setChecked(False)
        grid.addWidget(self.chkSelectedOnly, 2, 0, 1, 3)

        layout.addWidget(grp_in)

        grp_preset = QtWidgets.QGroupBox("2. 원소/범례 프리셋")
        h = QtWidgets.QHBoxLayout(grp_preset)
        self.cmbPreset = QtWidgets.QComboBox(grp_preset)
        for k, p in PRESETS.items():
            self.cmbPreset.addItem(p.label, k)
        self.txtUnit = QtWidgets.QLineEdit(grp_preset)
        self.txtUnit.setText(PRESETS["fe2o3"].unit)
        self.txtUnit.setMaximumWidth(80)
        self.txtUnit.setToolTip("구간 라벨 표시용 단위(예: %, wt%).")
        h.addWidget(QtWidgets.QLabel("프리셋"))
        h.addWidget(self.cmbPreset, 1)
        h.addWidget(QtWidgets.QLabel("단위"))
        h.addWidget(self.txtUnit)
        layout.addWidget(grp_preset)

        grp_clip = QtWidgets.QGroupBox("3. 저장/처리 옵션")
        grid2 = QtWidgets.QGridLayout(grp_clip)
        self.spinPixelSize = QtWidgets.QDoubleSpinBox(grp_clip)
        self.spinPixelSize.setDecimals(2)
        self.spinPixelSize.setMinimum(0.0)
        self.spinPixelSize.setMaximum(1000000.0)
        self.spinPixelSize.setSingleStep(1.0)
        self.spinPixelSize.setValue(0.0)
        self.spinPixelSize.setToolTip("0이면 현재 지도 해상도(캔버스 mapUnitsPerPixel)를 사용합니다.")

        self.spinExtentBuffer = QtWidgets.QDoubleSpinBox(grp_clip)
        self.spinExtentBuffer.setDecimals(0)
        self.spinExtentBuffer.setMinimum(0.0)
        self.spinExtentBuffer.setMaximum(10000000.0)
        self.spinExtentBuffer.setSingleStep(100.0)
        self.spinExtentBuffer.setValue(0.0)
        self.spinExtentBuffer.setToolTip("AOI 경계의 바깥쪽으로 버퍼(m)를 줍니다. 0이면 버퍼 없음.")

        self.chkDissolve = QtWidgets.QCheckBox("구간별로 합치기(dissolve)")
        self.chkDissolve.setChecked(True)
        self.chkDissolve.setToolTip("같은 구간(class)끼리 하나의 멀티폴리곤으로 합칩니다(도면/분석이 단순해짐).")

        self.chkFixMax = QtWidgets.QCheckBox("최댓값을 범례 최댓값으로 보정")
        self.chkFixMax.setChecked(False)
        self.chkFixMax.setToolTip("색상 매칭 결과의 최댓값이 범례 최댓값보다 낮게 나오면, 전체를 비례 스케일합니다.")

        self.chkInpaint = QtWidgets.QCheckBox("검은 경계선 제거(보간)")
        self.chkInpaint.setChecked(True)
        self.chkInpaint.setToolTip("무채색 계열의 어두운 경계선을 NoData로 보고 주변 값으로 메웁니다.")
        self.spinFillDist = QtWidgets.QSpinBox(grp_clip)
        self.spinFillDist.setMinimum(1)
        self.spinFillDist.setMaximum(500)
        self.spinFillDist.setValue(30)
        self.spinFillDist.setToolTip("보간 시 검색 거리(픽셀). 클수록 잘 메우지만 느릴 수 있습니다.")

        grid2.addWidget(QtWidgets.QLabel("픽셀 크기(지도 단위/px)"), 0, 0)
        grid2.addWidget(self.spinPixelSize, 0, 1)
        grid2.addWidget(QtWidgets.QLabel("AOI extent 버퍼(m)"), 0, 2)
        grid2.addWidget(self.spinExtentBuffer, 0, 3)

        grid2.addWidget(self.chkDissolve, 1, 0, 1, 2)
        grid2.addWidget(self.chkFixMax, 1, 2, 1, 2)

        grid2.addWidget(self.chkInpaint, 2, 0, 1, 2)
        grid2.addWidget(QtWidgets.QLabel("보간 거리(px)"), 2, 2)
        grid2.addWidget(self.spinFillDist, 2, 3)

        layout.addWidget(grp_clip)

        btn_row = QtWidgets.QHBoxLayout()
        btn_row.addStretch(1)
        self.btnRun = QtWidgets.QPushButton("실행")
        self.btnClose = QtWidgets.QPushButton("닫기")
        btn_row.addWidget(self.btnRun)
        btn_row.addWidget(self.btnClose)
        layout.addLayout(btn_row)

        self.btnClose.clicked.connect(self.reject)
        self.btnRun.clicked.connect(self.run)
        self.cmbPreset.currentIndexChanged.connect(self._on_preset_changed)

        self.resize(640, 540)

    def _on_preset_changed(self):
        try:
            key = str(self.cmbPreset.currentData() or "")
            p = PRESETS.get(key)
            if p:
                self.txtUnit.setText(p.unit or "")
        except Exception:
            pass

    def _cleanup_tmp(self):
        if self._tmp_dir and os.path.isdir(self._tmp_dir):
            try:
                shutil.rmtree(self._tmp_dir, ignore_errors=True)
            except Exception:
                pass
        self._tmp_dir = None

    def reject(self):
        self._cleanup_tmp()
        super().reject()

    def closeEvent(self, event):
        self._cleanup_tmp()
        event.accept()

    def run(self):
        ensure_live_log_dialog(self.iface, owner=self, show=True, clear=True)

        raster = self.cmbRaster.currentLayer()
        aoi = self.cmbAoi.currentLayer()
        if raster is None or not isinstance(raster, QgsRasterLayer):
            push_message(self.iface, "오류", "RGB 래스터(WMS) 레이어를 선택해주세요.", level=2, duration=7)
            restore_ui_focus(self)
            return
        if aoi is None or not isinstance(aoi, QgsVectorLayer):
            push_message(self.iface, "오류", "AOI 폴리곤 레이어를 선택해주세요.", level=2, duration=7)
            restore_ui_focus(self)
            return
        if aoi.geometryType() != QgsWkbTypes.PolygonGeometry:
            push_message(self.iface, "오류", "AOI는 폴리곤 레이어여야 합니다.", level=2, duration=7)
            restore_ui_focus(self)
            return

        key = str(self.cmbPreset.currentData() or "")
        preset = PRESETS.get(key)
        if preset is None:
            push_message(self.iface, "오류", "범례 프리셋이 올바르지 않습니다.", level=2, duration=7)
            restore_ui_focus(self)
            return

        unit = (self.txtUnit.text() or "").strip()
        do_dissolve = bool(self.chkDissolve.isChecked())
        do_fix_max = bool(self.chkFixMax.isChecked())
        do_inpaint = bool(self.chkInpaint.isChecked())
        fill_dist = int(self.spinFillDist.value())

        # AOI extent (bounding rectangle), optionally buffered.
        try:
            feats = aoi.selectedFeatures() if bool(self.chkSelectedOnly.isChecked()) else list(aoi.getFeatures())
        except Exception:
            feats = aoi.selectedFeatures() if bool(self.chkSelectedOnly.isChecked()) else []
        if not feats:
            push_message(self.iface, "오류", "AOI 피처가 없습니다. (선택 또는 레이어 내용 확인)", level=2, duration=7)
            restore_ui_focus(self)
            return

        aoi_geom = None
        for ft in feats:
            g = ft.geometry()
            if g is None or g.isEmpty():
                continue
            aoi_geom = g if aoi_geom is None else aoi_geom.combine(g)
        if aoi_geom is None or aoi_geom.isEmpty():
            push_message(self.iface, "오류", "AOI 지오메트리를 만들 수 없습니다.", level=2, duration=7)
            restore_ui_focus(self)
            return

        extent = aoi_geom.boundingBox()
        buf = float(self.spinExtentBuffer.value() or 0.0)
        if buf > 0:
            extent.grow(buf)

        # Transform AOI extent to raster CRS if needed.
        try:
            if aoi.crs() != raster.crs():
                ct = QgsCoordinateTransform(aoi.crs(), raster.crs(), QgsProject.instance())
                extent = ct.transformBoundingBox(extent)
        except Exception as e:
            log_message(f"GeoChem: extent transform failed: {e}", level=Qgis.Warning)

        if extent.isEmpty() or extent.width() <= 0 or extent.height() <= 0:
            push_message(self.iface, "오류", "AOI extent가 비어있습니다.", level=2, duration=7)
            restore_ui_focus(self)
            return

        # Choose pixel size: 0 = use current canvas resolution.
        px = float(self.spinPixelSize.value() or 0.0)
        if px <= 0:
            try:
                px = float(self.iface.mapCanvas().mapUnitsPerPixel())
            except Exception:
                px = 0.0
        if px <= 0:
            px = max(extent.width(), extent.height()) / 1024.0
        px = max(px, 1e-9)

        width = max(1, int(math.ceil(extent.width() / px)))
        height = max(1, int(math.ceil(extent.height() / px)))

        log_message(f"GeoChem: export extent {extent.toString()} px={px:g} => {width}x{height}", level=Qgis.Info)

        self._cleanup_tmp()
        self._tmp_dir = tempfile.mkdtemp(prefix="ArchToolkit_GeoChem_")
        run_id = uuid.uuid4().hex[:6]
        rgb_path = os.path.join(self._tmp_dir, f"wms_rgb_{run_id}.tif")
        val_path = os.path.join(self._tmp_dir, f"{preset.key}_value_{run_id}.tif")
        cls_path = os.path.join(self._tmp_dir, f"{preset.key}_class_{run_id}.tif")

        try:
            # 1) Export WMS RGB to GeoTIFF within AOI extent (rectangular).
            ok = self._export_raster_to_geotiff(raster=raster, out_path=rgb_path, extent=extent, width=width, height=height)
            if not ok:
                push_message(self.iface, "오류", "WMS 래스터를 GeoTIFF로 저장하지 못했습니다.", level=2, duration=9)
                restore_ui_focus(self)
                return

            # 2) RGB -> value raster
            log_message("GeoChem: reading exported RGB…", level=Qgis.Info)
            ds = gdal.Open(rgb_path)
            if ds is None:
                raise RuntimeError("Cannot open exported RGB GeoTIFF")
            band_count = int(ds.RasterCount or 0)
            if band_count < 3:
                raise RuntimeError("RGB 래스터는 최소 3밴드(R,G,B)가 필요합니다.")
            r = ds.GetRasterBand(1).ReadAsArray()
            g = ds.GetRasterBand(2).ReadAsArray()
            b = ds.GetRasterBand(3).ReadAsArray()
            a = None
            if band_count >= 4:
                try:
                    a = ds.GetRasterBand(4).ReadAsArray()
                except Exception:
                    a = None

            log_message("GeoChem: RGB -> value mapping…", level=Qgis.Info)
            out = _interp_rgb_to_value(r=r, g=g, b=b, points=preset.points)
            nodata_val = np.float32(-9999.0)

            # Transparent pixels (if alpha band exists) -> NoData
            if a is not None:
                try:
                    out = np.where(a.astype(np.int16, copy=False) <= 0, nodata_val, out)
                except Exception:
                    pass

            # Optional max correction (as in user's script)
            if do_fix_max:
                try:
                    br = _points_to_breaks(preset.points)
                    target_max = float(br[-1])
                    valid = np.isfinite(out) & (out >= 0)
                    if np.any(valid):
                        cur_max = float(np.nanmax(out[valid]))
                        if 0 < cur_max < target_max:
                            log_message(f"GeoChem: max correction {cur_max:g} -> {target_max:g}", level=Qgis.Info)
                            out[valid] = (out[valid] / cur_max) * target_max
                except Exception:
                    pass

            # Optional black line masking + fill
            if do_inpaint:
                log_message("GeoChem: masking dark linework…", level=Qgis.Info)
                try:
                    mask = _mask_black_lines(r, g, b)
                    out = out.astype(np.float32, copy=False)
                    out[mask] = np.nan
                except Exception:
                    pass
                log_message("GeoChem: filling masked pixels…", level=Qgis.Info)
                out = _gdal_fill_nodata_nearestish(arr=out, nodata=float(nodata_val), max_search_dist_px=fill_dist)

            # Ensure explicit nodata
            out = out.astype(np.float32, copy=False)
            out[~np.isfinite(out)] = nodata_val

            # Write value raster
            log_message("GeoChem: writing value raster…", level=Qgis.Info)
            self._write_single_band_geotiff(ds, out_path=val_path, data=out, nodata=float(nodata_val))

            # 3) Class raster
            breaks = _points_to_breaks(preset.points)
            log_message(f"GeoChem: classify to {len(breaks)-1} bins…", level=Qgis.Info)
            cls = _classify_to_bins(values=out, breaks=breaks, nodata_class=0)
            self._write_single_band_geotiff(ds, out_path=cls_path, data=cls.astype(np.int16, copy=False), nodata=0, gdal_type=gdal.GDT_Int16)
            ds = None

            # 4) Polygonize -> dissolve
            log_message("GeoChem: polygonize…", level=Qgis.Info)
            poly = processing.run(
                "gdal:polygonize",
                {
                    "INPUT": cls_path,
                    "BAND": 1,
                    "FIELD": "class_id",
                    "EIGHT_CONNECTEDNESS": True,
                    "OUTPUT": "memory:",
                },
            )["OUTPUT"]

            if not isinstance(poly, QgsVectorLayer):
                raise RuntimeError("Polygonize failed")

            # Drop nodata (class_id == 0)
            try:
                poly = processing.run(
                    "native:extractbyexpression",
                    {"INPUT": poly, "EXPRESSION": "\"class_id\" > 0", "OUTPUT": "memory:"},
                )["OUTPUT"]
            except Exception:
                pass

            if do_dissolve:
                log_message("GeoChem: dissolve by class…", level=Qgis.Info)
                poly = processing.run(
                    "native:dissolve",
                    {"INPUT": poly, "FIELD": ["class_id"], "OUTPUT": "memory:"},
                )["OUTPUT"]

            # Add descriptive fields
            self._decorate_polygons(layer=poly, preset=preset, unit=unit)

            # Add to project
            self._add_to_project(layer=poly, preset=preset, run_id=run_id, extent=extent)
            push_message(self.iface, "지구화학도 폴리곤화", "완료", level=0, duration=7)
        except Exception as e:
            log_message(f"GeoChem error: {e}", level=Qgis.Critical)
            push_message(self.iface, "오류", f"처리 실패: {e}", level=2, duration=10)
        finally:
            # Keep temp dir until dialog closes (useful for debugging), but remove if everything succeeded.
            # If users need the intermediate rasters, we can add an option later.
            try:
                self._cleanup_tmp()
            except Exception:
                pass
            restore_ui_focus(self)

    def _export_raster_to_geotiff(self, *, raster: QgsRasterLayer, out_path: str, extent: QgsRectangle, width: int, height: int) -> bool:
        """Export the raster (including WMS) to a GeoTIFF using QGIS raster writer."""
        try:
            from qgis.core import QgsRasterFileWriter, QgsRasterPipe
        except Exception:
            return False

        try:
            provider = raster.dataProvider()
            pipe = QgsRasterPipe()
            if not pipe.set(provider.clone()):
                # Fallback: some providers may not support clone() cleanly.
                if not pipe.set(provider):
                    return False
            writer = QgsRasterFileWriter(out_path)
            writer.setOutputFormat("GTiff")
            writer.setCreateOptions(["COMPRESS=LZW", "TILED=YES"])
            ctx = QgsProject.instance().transformContext()
            res = writer.writeRaster(pipe, int(width), int(height), extent, raster.crs(), ctx)
            if res != 0:
                log_message(f"GeoChem: writeRaster returned {res}", level=Qgis.Warning)
                return False
            return True
        except Exception as e:
            log_message(f"GeoChem: export failed: {e}", level=Qgis.Warning)
            return False

    def _write_single_band_geotiff(
        self,
        src_ds,
        *,
        out_path: str,
        data: np.ndarray,
        nodata: float,
        gdal_type=gdal.GDT_Float32,
    ):
        gt = src_ds.GetGeoTransform()
        proj = src_ds.GetProjection()
        ysize, xsize = data.shape
        drv = gdal.GetDriverByName("GTiff")
        ds = drv.Create(out_path, int(xsize), int(ysize), 1, gdal_type, options=["COMPRESS=LZW", "TILED=YES"])
        ds.SetGeoTransform(gt)
        ds.SetProjection(proj)
        band = ds.GetRasterBand(1)
        band.WriteArray(data)
        band.SetNoDataValue(float(nodata))
        ds.FlushCache()
        ds = None

    def _decorate_polygons(self, *, layer: QgsVectorLayer, preset: GeoChemPreset, unit: str):
        breaks = _points_to_breaks(preset.points)
        intervals = [(breaks[i], breaks[i + 1]) for i in range(len(breaks) - 1)]

        pr = layer.dataProvider()
        pr.addAttributes(
            [
                QgsField("element", QVariant.String),
                QgsField("unit", QVariant.String),
                QgsField("v_min", QVariant.Double),
                QgsField("v_max", QVariant.Double),
                QgsField("label", QVariant.String),
            ]
        )
        layer.updateFields()

        # Field aliases (Korean)
        try:
            def _alias(name: str, alias: str):
                idx = layer.fields().indexFromName(name)
                if idx >= 0:
                    layer.setFieldAlias(idx, alias)

            _alias("class_id", "구간ID")
            _alias("element", "원소/지표")
            _alias("unit", "단위")
            _alias("v_min", "구간 최소값")
            _alias("v_max", "구간 최대값")
            _alias("label", "구간 라벨")
        except Exception:
            pass

        # Apply attributes and style
        cats = []
        for i, (v0, v1) in enumerate(intervals, start=1):
            col = preset.points[i].rgb if i < len(preset.points) else preset.points[-1].rgb
            qcol = QColor(int(col[0]), int(col[1]), int(col[2]), 140)
            try:
                from qgis.core import QgsFillSymbol

                fs = QgsFillSymbol.createSimple(
                    {
                        "color": f"{qcol.red()},{qcol.green()},{qcol.blue()},{qcol.alpha()}",
                        "outline_color": "0,0,0,40",
                        "outline_width": "0.1",
                    }
                )
            except Exception:
                from qgis.core import QgsFillSymbol

                fs = QgsFillSymbol.createSimple({"color": "200,200,200,120", "outline_color": "0,0,0,40"})
            cats.append(QgsRendererCategory(int(i), fs, _interval_label(v0, v1, unit)))

        try:
            layer.setRenderer(QgsCategorizedSymbolRenderer("class_id", cats))
        except Exception:
            pass

        # Write per-feature attributes
        layer.startEditing()
        try:
            for ft in layer.getFeatures():
                cid = int(ft["class_id"]) if ft["class_id"] is not None else 0
                if cid <= 0 or cid > len(intervals):
                    continue
                v0, v1 = intervals[cid - 1]
                ft["element"] = preset.label
                ft["unit"] = unit
                ft["v_min"] = float(v0)
                ft["v_max"] = float(v1)
                ft["label"] = _interval_label(v0, v1, unit)
                layer.updateFeature(ft)
        finally:
            layer.commitChanges()
        layer.triggerRepaint()

    def _add_to_project(self, *, layer: QgsVectorLayer, preset: GeoChemPreset, run_id: str, extent: QgsRectangle):
        project = QgsProject.instance()
        root = project.layerTreeRoot()
        parent = root.findGroup(PARENT_GROUP_NAME)
        if parent is None:
            parent = root.insertGroup(0, PARENT_GROUP_NAME)

        name = f"{preset.key}_구간폴리곤_{run_id}"
        layer.setName(name)

        project.addMapLayer(layer, False)
        parent.insertLayer(0, layer)

        try:
            parent.setExpanded(True)
        except Exception:
            pass

        try:
            # Keep group near top
            if parent.parent() == root:
                idx = root.children().index(parent)
                if idx != 0:
                    root.removeChildNode(parent)
                    root.insertChildNode(0, parent)
        except Exception:
            pass

        try:
            self.iface.mapCanvas().setExtent(extent)
            self.iface.mapCanvas().refresh()
        except Exception:
            pass
