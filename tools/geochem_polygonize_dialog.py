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
from osgeo import gdal, ogr

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
    QgsPointXY,
    QgsProject,
    QgsRasterLayer,
    QgsRectangle,
    QgsRendererCategory,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.gui import QgsMapLayerComboBox

from .utils import log_exception, log_message, push_message, restore_ui_focus
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

PB_POINTS: List[LegendPoint] = [
    # Legend from percentile ramp (ppm): 5=18, 10=20, 15=21, 25=24, 50=28, 75=32, 90=36, 95=41, 99=57, 100=1363
    # We reuse the same base palette as other GeoChem layers (blue->red) to match the WMS styling.
    LegendPoint(0.0, (204, 204, 204)),  # Absent data
    LegendPoint(18.0, (0, 38, 115)),  # 5%
    LegendPoint(20.0, (0, 112, 255)),  # 10%
    LegendPoint(21.0, (0, 197, 255)),  # 15%
    LegendPoint(24.0, (0, 255, 0)),  # 25%
    LegendPoint(28.0, (85, 255, 0)),  # 50%
    LegendPoint(32.0, (255, 255, 0)),  # 75%
    LegendPoint(36.0, (255, 170, 0)),  # 90%
    LegendPoint(41.0, (255, 85, 0)),  # 95%
    LegendPoint(57.0, (230, 0, 0)),  # 99%
    LegendPoint(1363.0, (115, 12, 12)),  # 100%
]

CU_POINTS: List[LegendPoint] = [
    # Legend from percentile ramp (ppm): 5=10, 10=12, 15=14, 25=17, 50=23, 75=33, 90=45, 95=58, 99=104, 100=2104
    LegendPoint(0.0, (204, 204, 204)),  # Absent data
    LegendPoint(10.0, (0, 38, 115)),  # 5%
    LegendPoint(12.0, (0, 112, 255)),  # 10%
    LegendPoint(14.0, (0, 197, 255)),  # 15%
    LegendPoint(17.0, (0, 255, 0)),  # 25%
    LegendPoint(23.0, (85, 255, 0)),  # 50%
    LegendPoint(33.0, (255, 255, 0)),  # 75%
    LegendPoint(45.0, (255, 170, 0)),  # 90%
    LegendPoint(58.0, (255, 85, 0)),  # 95%
    LegendPoint(104.0, (230, 0, 0)),  # 99%
    LegendPoint(2104.0, (115, 12, 12)),  # 100%
]

PRESETS: Dict[str, GeoChemPreset] = {
    "fe2o3": GeoChemPreset(key="fe2o3", label="Fe2O3 (산화철)", unit="%", points=FE2O3_POINTS),
    "pb": GeoChemPreset(key="pb", label="Pb (납)", unit="ppm", points=PB_POINTS),
    "cu": GeoChemPreset(key="cu", label="Cu (구리)", unit="ppm", points=CU_POINTS),
}


def _points_to_breaks(points: Sequence[LegendPoint]) -> List[float]:
    vals = [float(p.value) for p in points]
    vals = sorted(set(vals))
    return vals


def _interp_rgb_to_value(
    *,
    r: np.ndarray,
    g: np.ndarray,
    b: np.ndarray,
    points: Sequence[LegendPoint],
    snap_last_t: Optional[float] = None,
) -> np.ndarray:
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
    last_seg_idx = len(pts) - 2
    snap_last = None
    if snap_last_t is not None:
        try:
            snap_last = float(snap_last_t)
        except Exception:
            snap_last = None
    if snap_last is not None and not (0.0 <= snap_last <= 1.0):
        snap_last = None

    for i in range(len(pts) - 1):
        v1 = float(pts[i].value)
        v2 = float(pts[i + 1].value)
        c1 = pts[i].rgb
        c2 = pts[i + 1].rgb

        c1r = np.float32(c1[0])
        c1g = np.float32(c1[1])
        c1b = np.float32(c1[2])
        vr = np.float32(c2[0] - c1[0])
        vg = np.float32(c2[1] - c1[1])
        vb = np.float32(c2[2] - c1[2])
        v_len_sq = np.float32(vr * vr + vg * vg + vb * vb)
        if v_len_sq <= 0:
            continue

        t = ((rr - c1r) * vr + (gg - c1g) * vg + (bb - c1b) * vb) / v_len_sq
        np.clip(t, np.float32(0.0), np.float32(1.0), out=t)
        if snap_last is not None and i == last_seg_idx:
            # Important: apply snap BEFORE distance comparison (affects which segment wins).
            try:
                t[t > np.float32(snap_last)] = np.float32(1.0)
            except Exception:
                pass
        pr = c1r + t * vr
        pg = c1g + t * vg
        pb = c1b + t * vb
        dist_sq = (rr - pr) ** 2 + (gg - pg) ** 2 + (bb - pb) ** 2

        mask = dist_sq < min_dist
        if not np.any(mask):
            continue

        base = np.float32(v1)
        delta = np.float32(v2 - v1)
        out[mask] = base + t[mask].astype(np.float32, copy=False) * delta
        min_dist[mask] = dist_sq[mask].astype(np.float32, copy=False)

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


def _gdal_rasterize_wkt_mask(
    *,
    geom_wkt: str,
    xsize: int,
    ysize: int,
    geotransform,
    projection_wkt: str,
) -> Optional[np.ndarray]:
    """Rasterize a polygon WKT into a boolean mask (True inside)."""
    if not geom_wkt:
        return None
    try:
        geom = ogr.CreateGeometryFromWkt(str(geom_wkt))
    except Exception:
        geom = None
    if geom is None:
        return None

    drv = gdal.GetDriverByName("MEM")
    ds = drv.Create("", int(xsize), int(ysize), 1, gdal.GDT_Byte)
    ds.SetGeoTransform(geotransform)
    ds.SetProjection(projection_wkt)
    band = ds.GetRasterBand(1)
    band.Fill(0)
    try:
        gdal.RasterizeGeometries(ds, [1], [geom], burn_values=[1], options=["ALL_TOUCHED=TRUE"])
    except Exception:
        ds = None
        return None
    arr = band.ReadAsArray()
    ds = None
    try:
        return arr.astype(np.uint8, copy=False) > 0
    except Exception:
        return None


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


def _classify_to_bins(
    *,
    values: np.ndarray,
    breaks: Sequence[float],
    nodata_class: int = 0,
    nodata_value: Optional[float] = None,
) -> np.ndarray:
    br = [float(x) for x in breaks]
    if len(br) < 2:
        raise ValueError("Need at least 2 breaks")

    v = values.astype(np.float32, copy=False)
    cls = np.full(v.shape, int(nodata_class), dtype=np.int16)

    valid = np.isfinite(v)
    if nodata_value is not None:
        try:
            valid &= v != np.float32(float(nodata_value))
        except Exception:
            pass

    if not np.any(valid):
        return cls

    vmin = float(br[0])
    vmax = float(br[-1])
    vv = np.clip(v, vmin, vmax)

    bins = br[1:-1]  # internal thresholds
    idx = np.digitize(vv, bins=bins, right=False).astype(np.int16, copy=False)  # 0..n-1
    cls[valid] = idx[valid] + 1  # 1..n_intervals
    return cls


def _interval_label(v0: float, v1: float, unit: str) -> str:
    if unit:
        return f"{v0:g}-{v1:g}{unit}"
    return f"{v0:g}-{v1:g}"


def _rgb_for_value(*, points: Sequence[LegendPoint], value: float) -> Tuple[int, int, int]:
    """Interpolate an RGB color for a scalar value using legend points (value-space interpolation)."""
    pts = sorted(points, key=lambda p: float(p.value))
    if not pts:
        return (204, 204, 204)
    v = float(value)

    if v <= float(pts[0].value):
        return pts[0].rgb
    if v >= float(pts[-1].value):
        return pts[-1].rgb

    for i in range(len(pts) - 1):
        v0 = float(pts[i].value)
        v1 = float(pts[i + 1].value)
        if v0 <= v <= v1:
            if v1 <= v0:
                return pts[i + 1].rgb
            t = (v - v0) / (v1 - v0)
            c0 = pts[i].rgb
            c1 = pts[i + 1].rgb
            r = int(round(float(c0[0]) + t * (float(c1[0]) - float(c0[0]))))
            g = int(round(float(c0[1]) + t * (float(c1[1]) - float(c0[1]))))
            b = int(round(float(c0[2]) + t * (float(c1[2]) - float(c0[2]))))
            return (max(0, min(255, r)), max(0, min(255, g)), max(0, min(255, b)))

    return pts[-1].rgb


class GeoChemPolygonizeDialog(QtWidgets.QDialog):
    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.setWindowTitle("지구화학도 래스터 수치화 (GeoChem WMS → Raster) - ArchToolkit")

        try:
            plugin_dir = os.path.dirname(os.path.dirname(__file__))
            icon_candidates = [
                os.path.join(plugin_dir, "tools", "geochem.png"),
                os.path.join(plugin_dir, "geochem.png"),
                os.path.join(plugin_dir, "terrain_icon.png"),
            ]
            for p in icon_candidates:
                if os.path.exists(p):
                    self.setWindowIcon(QIcon(p))
                    break
        except Exception:
            pass

        self._tmp_dir = None

        layout = QtWidgets.QVBoxLayout(self)

        desc = QtWidgets.QLabel(
            "<b>지구화학도 래스터 수치화</b><br>"
            "WMS 등 RGB 래스터(이미지)를 범례 기반으로 <b>값 래스터</b>로 복원하고,<br>"
            "원하면 <b>구간(class) 래스터 / 폴리곤</b>까지 생성합니다. (원자료 수치가 아닌 ‘역추정’입니다.)"
        )
        desc.setWordWrap(True)
        desc.setStyleSheet("background:#f0f0f0; padding:6px; border-radius:4px;")
        desc.setToolTip(
            "워크플로우:\n"
            "1) RGB 지구화학도(WMS/래스터)를 조사지역 경계(사각형)로 잘라 GeoTIFF로 저장\n"
            "2) RGB → 값(%) 래스터로 변환(범례 기반)\n"
            "3) (옵션) 값 → 구간(class) 래스터 생성\n"
            "4) (옵션) 구간 폴리곤 생성 + dissolve\n"
            "5) (옵션) 중심점(포인트) 생성\n\n"
            "팁:\n"
            "- 검은 경계선/텍스트가 지저분하면 '검은 경계선 제거(보간)'을 켜보세요.\n"
            "- 픽셀 크기를 작게 할수록 디테일은 좋아지지만 느려질 수 있습니다."
        )
        layout.addWidget(desc)

        grp_in = QtWidgets.QGroupBox("1. 입력")
        grid = QtWidgets.QGridLayout(grp_in)

        self.cmbRaster = QgsMapLayerComboBox(grp_in)
        self.cmbRaster.setFilters(QgsMapLayerProxyModel.RasterLayer)
        self.cmbAoi = QgsMapLayerComboBox(grp_in)
        self.cmbAoi.setFilters(QgsMapLayerProxyModel.VectorLayer)

        grid.addWidget(QtWidgets.QLabel("RGB 래스터(WMS)"), 0, 0)
        grid.addWidget(self.cmbRaster, 0, 1, 1, 2)
        grid.addWidget(QtWidgets.QLabel("조사지역 폴리곤"), 1, 0)
        grid.addWidget(self.cmbAoi, 1, 1, 1, 2)

        self.chkSelectedOnly = QtWidgets.QCheckBox("조사지역 선택 피처만 사용")
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

        grp_clip = QtWidgets.QGroupBox("3. 처리/보정 옵션")
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
        self.spinExtentBuffer.setToolTip("조사지역 경계(사각형)의 바깥쪽으로 버퍼(m)를 줍니다. 0이면 버퍼 없음.")

        self.chkMaskAoi = QtWidgets.QCheckBox("AOI 마스크 적용(폴리곤 내부만)")
        self.chkMaskAoi.setChecked(True)
        self.chkMaskAoi.setToolTip("조사지역 폴리곤 내부만 유효값으로 두고 바깥은 NoData로 처리합니다. (분석/중심점 계산에 권장)")

        self.chkLowAsNoData = QtWidgets.QCheckBox("0~최소값(회색) 구간을 NoData로 취급")
        self.chkLowAsNoData.setChecked(True)
        self.chkLowAsNoData.setToolTip(
            "범례의 최저값(보통 회색)은 실제 데이터가 아닌 배경/무자료로 보고 NoData(-9999)로 처리합니다.\n"
            "예: Fe2O3 프리셋은 0~3.1 구간을 NoData로 취급"
        )

        self.chkFixMax = QtWidgets.QCheckBox("최댓값을 범례 최댓값으로 보정")
        self.chkFixMax.setChecked(False)
        self.chkFixMax.setToolTip("색상 매칭 결과의 최댓값이 범례 최댓값보다 낮게 나오면, 전체를 비례 스케일합니다.")

        self.chkSnapMax = QtWidgets.QCheckBox("고농도 스냅(최댓값)")
        self.chkSnapMax.setChecked(False)
        self.chkSnapMax.setToolTip("마지막 구간(예: 12~51)에서 일정 이상이면 최댓값으로 강제합니다. (로컬 보정)")
        self.spinSnapT = QtWidgets.QDoubleSpinBox(grp_clip)
        self.spinSnapT.setDecimals(2)
        self.spinSnapT.setMinimum(0.0)
        self.spinSnapT.setMaximum(1.0)
        self.spinSnapT.setSingleStep(0.05)
        self.spinSnapT.setValue(0.7)
        self.spinSnapT.setToolTip("마지막 구간에서 t(0~1)가 이 값보다 크면 최댓값으로 스냅합니다.")

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
        grid2.addWidget(QtWidgets.QLabel("조사지역 경계(사각형) 버퍼(m)"), 0, 2)
        grid2.addWidget(self.spinExtentBuffer, 0, 3)

        grid2.addWidget(self.chkMaskAoi, 1, 0, 1, 2)
        grid2.addWidget(self.chkFixMax, 1, 2, 1, 2)

        grid2.addWidget(self.chkInpaint, 2, 0, 1, 2)
        grid2.addWidget(QtWidgets.QLabel("보간 거리(px)"), 2, 2)
        grid2.addWidget(self.spinFillDist, 2, 3)

        grid2.addWidget(self.chkSnapMax, 3, 0, 1, 2)
        grid2.addWidget(QtWidgets.QLabel("스냅 t(0~1)"), 3, 2)
        grid2.addWidget(self.spinSnapT, 3, 3)

        grid2.addWidget(self.chkLowAsNoData, 4, 0, 1, 4)

        layout.addWidget(grp_clip)

        grp_out = QtWidgets.QGroupBox("4. 출력 옵션")
        grid3 = QtWidgets.QGridLayout(grp_out)

        self.chkSaveRasters = QtWidgets.QCheckBox("값 래스터 저장(영구)")
        self.chkSaveRasters.setChecked(True)
        self.chkSaveRasters.setToolTip(
            "값(value) 래스터를 프로젝트 홈(없으면 QGIS 프로필) 하위에 저장합니다.\n"
            "※ '구간(class) 래스터 생성'을 켠 경우, class 래스터도 함께 저장됩니다."
        )

        self.chkAddRasters = QtWidgets.QCheckBox("프로젝트에 래스터 레이어로 추가")
        self.chkAddRasters.setChecked(True)
        self.chkAddRasters.setToolTip(
            "저장된 래스터 레이어를 ArchToolkit - GeoChem 그룹에 함께 추가합니다.\n"
            "- value: 항상 추가\n"
            "- class: '구간(class) 래스터 생성'을 켠 경우에만 추가"
        )

        self.chkMakeClassRaster = QtWidgets.QCheckBox("구간(class) 래스터 생성(옵션)")
        self.chkMakeClassRaster.setChecked(False)
        self.chkMakeClassRaster.setToolTip(
            "연속값(value)을 범례 구간대로 정수 class(1..N)로 재분류한 래스터를 만듭니다.\n"
            "다음이 필요할 때만 켜세요:\n"
            "- 구간형(범주형) 지도가 필요할 때\n"
            "- '폴리곤 생성(구간별)'을 사용할 때(내부적으로 class가 필요함)\n\n"
            "대부분의 분석(MaxEnt/통계/가중 중심점)은 value 래스터만으로 충분합니다."
        )

        self.chkMakePolygons = QtWidgets.QCheckBox("폴리곤 생성(구간별)")
        self.chkMakePolygons.setChecked(False)
        self.chkMakePolygons.setToolTip(
            "구간(class) 래스터를 폴리곤으로 변환합니다(요약/도면/편집/오버레이용).\n"
            "예: '구간별 면적'을 벡터로 보고 싶거나, 다른 레이어와 교차/편집이 필요할 때."
        )

        self.chkDissolve = QtWidgets.QCheckBox("구간별로 합치기(dissolve)")
        self.chkDissolve.setChecked(True)
        self.chkDissolve.setEnabled(False)
        self.chkDissolve.setToolTip("같은 구간(class)끼리 하나의 멀티폴리곤으로 합칩니다(도면/분석이 단순해짐).")

        self.chkDropNoData = QtWidgets.QCheckBox("NoData(투명) 폴리곤 제외")
        self.chkDropNoData.setChecked(True)
        self.chkDropNoData.setEnabled(False)
        self.chkDropNoData.setToolTip("class_id=0(투명/NoData) 폴리곤을 결과에서 제외합니다.")

        grid3.addWidget(self.chkSaveRasters, 0, 0)
        grid3.addWidget(self.chkAddRasters, 0, 1)
        grid3.addWidget(self.chkMakeClassRaster, 1, 0, 1, 2)
        grid3.addWidget(self.chkMakePolygons, 2, 0)
        grid3.addWidget(self.chkDissolve, 2, 1)
        grid3.addWidget(self.chkDropNoData, 3, 0, 1, 2)

        self.lblOutHelp = QtWidgets.QLabel(
            "TIP: value 래스터는 WMS 색상을 그대로 수치화한 ‘원본 데이터’입니다.\n"
            "- class 래스터/폴리곤은 ‘구간별(범주형) 결과’가 필요할 때만 켜세요."
        )
        self.lblOutHelp.setWordWrap(True)
        self.lblOutHelp.setStyleSheet("color: #555;")
        grid3.addWidget(self.lblOutHelp, 4, 0, 1, 2)
        layout.addWidget(grp_out)

        grp_center = QtWidgets.QGroupBox("5. 중심점(포인트)")
        grid4 = QtWidgets.QGridLayout(grp_center)
        self.chkWeightedCenter = QtWidgets.QCheckBox("중심점 생성")
        self.chkWeightedCenter.setChecked(False)
        self.chkWeightedCenter.setToolTip("값 래스터에서 중심점을 계산해 포인트 레이어로 추가합니다.")

        self.cmbCenterMethod = QtWidgets.QComboBox(grp_center)
        self.cmbCenterMethod.addItem("가중 평균 중심(질량중심)", "weighted_mean")
        self.cmbCenterMethod.addItem("무가중 평균 중심(선택 픽셀 중심)", "mean")
        self.cmbCenterMethod.addItem("최대값 픽셀(peak)", "peak")
        self.cmbCenterMethod.setToolTip(
            "중심점 산정 방식입니다.\n"
            "- 가중 평균: 값이 클수록 중심이 더 끌립니다.\n"
            "- 무가중 평균: 선택된 영역의 기하학적 중심(픽셀 평균)입니다.\n"
            "- peak: 선택된 픽셀 중 값이 가장 큰 위치입니다."
        )

        self.cmbWeightRule = QtWidgets.QComboBox(grp_center)
        self.cmbWeightRule.addItem("값 그대로 (w = value)", "value")
        self.cmbWeightRule.addItem("값 거듭제곱 (w = value^p)", "power")
        self.cmbWeightRule.addItem("임계값 이상만 (w = value, value>=t)", "threshold")
        self.cmbWeightRule.addItem("임계값 이상만 (w = 1, value>=t)", "binary")
        self.cmbWeightRule.addItem("상위 %만 (w = value, top X%)", "top_pct")

        self.spinWeightPower = QtWidgets.QSpinBox(grp_center)
        self.spinWeightPower.setMinimum(1)
        self.spinWeightPower.setMaximum(8)
        self.spinWeightPower.setValue(2)
        self.spinWeightPower.setToolTip("거듭제곱 지수 p (w = value^p)")

        self.spinWeightThreshold = QtWidgets.QDoubleSpinBox(grp_center)
        self.spinWeightThreshold.setDecimals(2)
        self.spinWeightThreshold.setMinimum(0.0)
        self.spinWeightThreshold.setMaximum(1_000_000.0)
        self.spinWeightThreshold.setValue(12.0)
        self.spinWeightThreshold.setToolTip("임계값 t (value>=t만 사용)")

        self.spinWeightTopPct = QtWidgets.QDoubleSpinBox(grp_center)
        self.spinWeightTopPct.setDecimals(1)
        self.spinWeightTopPct.setMinimum(0.1)
        self.spinWeightTopPct.setMaximum(100.0)
        self.spinWeightTopPct.setSingleStep(1.0)
        self.spinWeightTopPct.setValue(10.0)
        self.spinWeightTopPct.setToolTip("상위 X%만 사용 (예: 10이면 상위 10%)")

        grid4.addWidget(self.chkWeightedCenter, 0, 0, 1, 2)
        grid4.addWidget(QtWidgets.QLabel("중심점 방식"), 1, 0)
        grid4.addWidget(self.cmbCenterMethod, 1, 1)
        grid4.addWidget(QtWidgets.QLabel("가중치 규칙"), 2, 0)
        grid4.addWidget(self.cmbWeightRule, 2, 1)
        grid4.addWidget(QtWidgets.QLabel("p"), 3, 0)
        grid4.addWidget(self.spinWeightPower, 3, 1)
        grid4.addWidget(QtWidgets.QLabel("t"), 4, 0)
        grid4.addWidget(self.spinWeightThreshold, 4, 1)
        grid4.addWidget(QtWidgets.QLabel("X(%)"), 5, 0)
        grid4.addWidget(self.spinWeightTopPct, 5, 1)
        layout.addWidget(grp_center)

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
        self.chkMakePolygons.stateChanged.connect(self._update_polygon_ui)
        self.chkAddRasters.stateChanged.connect(self._on_add_rasters_changed)
        self.chkWeightedCenter.stateChanged.connect(self._update_weight_ui)
        self.cmbWeightRule.currentIndexChanged.connect(self._update_weight_ui)
        self.cmbCenterMethod.currentIndexChanged.connect(self._update_weight_ui)

        self._update_polygon_ui()
        self._update_weight_ui()

        # Tooltips: keep the dialog short, show detailed guidance on hover.
        try:
            self.cmbRaster.setToolTip(
                "RGB 지구화학도 래스터/WMS 레이어를 선택하세요.\n"
                "- 보통 3밴드(R,G,B)입니다.\n"
                "- DEM 같은 단일밴드 래스터는 대상이 아닙니다."
            )
            self.cmbAoi.setToolTip(
                "조사지역(폴리곤) 레이어를 선택하세요.\n"
                "- 선택 피처만 사용할 수도 있습니다.\n"
                "- 실제로는 폴리곤 클립이 아니라 '경계 사각형(extent)'으로 잘라냅니다."
            )
            self.chkSelectedOnly.setToolTip(
                "조사지역 레이어에서 '선택된 피처'만 사용해 경계(extent)를 계산합니다.\n"
                "선택이 없으면 전체 피처를 사용합니다."
            )
            self.cmbPreset.setToolTip(
                "원소/범례 프리셋을 선택합니다.\n"
                "현재는 Fe2O3(산화철)만 제공됩니다.\n"
                "다른 원소는 범례(이미지/값/색)를 받으면 추가할 수 있습니다."
            )
            self.txtUnit.setToolTip(
                "표시용 단위입니다.\n"
                "결과 폴리곤의 라벨(예: 3.1-3.5%)에 붙습니다."
            )
            self.spinPixelSize.setToolTip(
                "내보낼 래스터의 픽셀 크기(지도 단위/px)입니다.\n"
                "- 0이면 현재 지도 해상도(mapUnitsPerPixel)를 사용합니다.\n"
                "- 값이 작을수록 디테일↑, 처리시간/파일크기↑\n"
                "- WMS 색을 보존하려고 최근접(Nearest)으로 리샘플링합니다."
            )
            self.spinExtentBuffer.setToolTip(
                "조사지역 폴리곤을 감싸는 '경계 사각형(extent)'에 버퍼(m)를 추가합니다.\n"
                "0이면 버퍼 없음."
            )
            self.chkDissolve.setToolTip(
                "같은 구간(class)끼리 하나의 폴리곤으로 합칩니다.\n"
                "끄면 폴리곤 조각이 매우 많아질 수 있습니다."
            )
            self.chkFixMax.setToolTip(
                "색상 매칭 결과의 최댓값이 범례 최댓값(예: 51%)보다 작게 나오면\n"
                "전체를 비율로 스케일해서 0~최댓값 범위를 맞춥니다.\n"
                "지도 일부만 잘랐을 때(고농도 구간이 포함되지 않을 때) 유용합니다."
            )
            self.chkSnapMax.setToolTip(
                "마지막 구간(예: 12~51)에서 고농도 영역이 잘 안 잡힐 때 사용하는 로컬 보정입니다.\n"
                "- t는 마지막 구간의 색상 선분 위 위치(0~1)입니다.\n"
                "- t가 스냅값보다 크면 최댓값(예: 51)으로 강제합니다.\n"
                "※ 과하면 고농도 영역이 과대평가될 수 있습니다."
            )
            self.spinSnapT.setToolTip(
                "고농도 스냅 임계값입니다.\n"
                "- 0.7이면 마지막 구간 상위 30%를 최댓값으로 스냅합니다.\n"
                "- 값이 작을수록 더 많은 픽셀이 최댓값으로 들어갑니다."
            )
            self.chkInpaint.setToolTip(
                "무채색(검정/짙은 회색) 경계선을 NoData로 만든 뒤 주변 값으로 메웁니다.\n"
                "지괴 경계선/텍스트 등 '검은 선'이 결과를 깨뜨릴 때 켜세요.\n"
                "너무 과하면 경계 부근이 부드러워질 수 있습니다."
            )
            self.spinFillDist.setToolTip(
                "보간 최대 검색 거리(px)입니다.\n"
                "- 경계선이 두껍거나 지저분할수록 값을 키워보세요.\n"
                "- 값이 클수록 더 멀리까지 메우지만 느려질 수 있습니다."
            )
            self.btnRun.setToolTip("실행합니다. (중간 산출물은 창을 닫을 때 정리됩니다)")
            self.btnClose.setToolTip("닫기")
        except Exception:
            pass

        self.resize(700, 650)

    def _on_preset_changed(self):
        try:
            key = str(self.cmbPreset.currentData() or "")
            p = PRESETS.get(key)
            if p:
                self.txtUnit.setText(p.unit or "")
        except Exception:
            pass

    def _on_add_rasters_changed(self):
        try:
            if self.chkAddRasters.isChecked():
                self.chkSaveRasters.setChecked(True)
        except Exception:
            pass

    def _update_polygon_ui(self):
        try:
            enabled = bool(self.chkMakePolygons.isChecked())
            self.chkDissolve.setEnabled(enabled)
            self.chkDropNoData.setEnabled(enabled)
        except Exception:
            pass

    def _update_weight_ui(self):
        try:
            enabled = bool(self.chkWeightedCenter.isChecked())
            self.cmbCenterMethod.setEnabled(enabled)
            self.cmbWeightRule.setEnabled(enabled)
            rule = str(self.cmbWeightRule.currentData() or "")
            self.spinWeightPower.setEnabled(enabled and rule == "power")
            self.spinWeightThreshold.setEnabled(enabled and rule in ("threshold", "binary"))
            self.spinWeightTopPct.setEnabled(enabled and rule == "top_pct")
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
            push_message(self.iface, "오류", "조사지역 폴리곤 레이어를 선택해주세요.", level=2, duration=7)
            restore_ui_focus(self)
            return
        if aoi.geometryType() != QgsWkbTypes.PolygonGeometry:
            push_message(self.iface, "오류", "조사지역은 폴리곤 레이어여야 합니다.", level=2, duration=7)
            restore_ui_focus(self)
            return

        key = str(self.cmbPreset.currentData() or "")
        preset = PRESETS.get(key)
        if preset is None:
            push_message(self.iface, "오류", "범례 프리셋이 올바르지 않습니다.", level=2, duration=7)
            restore_ui_focus(self)
            return

        unit = (self.txtUnit.text() or "").strip()
        do_mask_aoi = bool(getattr(self, "chkMaskAoi", None) and self.chkMaskAoi.isChecked())
        do_low_as_nodata = bool(getattr(self, "chkLowAsNoData", None) and self.chkLowAsNoData.isChecked())
        do_make_polygons = bool(getattr(self, "chkMakePolygons", None) and self.chkMakePolygons.isChecked())
        do_make_class_raster = bool(getattr(self, "chkMakeClassRaster", None) and self.chkMakeClassRaster.isChecked())
        do_dissolve = bool(self.chkDissolve.isChecked()) and do_make_polygons
        do_fix_max = bool(self.chkFixMax.isChecked())
        do_snap_max = bool(getattr(self, "chkSnapMax", None) and self.chkSnapMax.isChecked())
        snap_t = float(getattr(self, "spinSnapT", None).value()) if getattr(self, "spinSnapT", None) else 0.0
        do_inpaint = bool(self.chkInpaint.isChecked())
        fill_dist = int(self.spinFillDist.value())
        do_drop_nodata = bool(getattr(self, "chkDropNoData", None) and self.chkDropNoData.isChecked()) and do_make_polygons
        do_save_rasters = bool(getattr(self, "chkSaveRasters", None) and self.chkSaveRasters.isChecked())
        do_add_rasters = bool(getattr(self, "chkAddRasters", None) and self.chkAddRasters.isChecked())
        do_weight_center = bool(getattr(self, "chkWeightedCenter", None) and self.chkWeightedCenter.isChecked())
        center_method = (
            str(getattr(self, "cmbCenterMethod", None).currentData() or "weighted_mean")
            if getattr(self, "cmbCenterMethod", None)
            else "weighted_mean"
        )
        weight_rule = str(getattr(self, "cmbWeightRule", None).currentData() or "value") if getattr(self, "cmbWeightRule", None) else "value"
        weight_power = int(getattr(self, "spinWeightPower", None).value()) if getattr(self, "spinWeightPower", None) else 2
        weight_threshold = float(getattr(self, "spinWeightThreshold", None).value()) if getattr(self, "spinWeightThreshold", None) else 0.0
        weight_top_pct = float(getattr(self, "spinWeightTopPct", None).value()) if getattr(self, "spinWeightTopPct", None) else 10.0
        if do_add_rasters:
            do_save_rasters = True

        # Survey area extent (bounding rectangle), optionally buffered.
        try:
            feats = aoi.selectedFeatures() if bool(self.chkSelectedOnly.isChecked()) else list(aoi.getFeatures())
        except Exception:
            feats = aoi.selectedFeatures() if bool(self.chkSelectedOnly.isChecked()) else []
        if not feats:
            push_message(self.iface, "오류", "조사지역 피처가 없습니다. (선택 또는 레이어 내용 확인)", level=2, duration=7)
            restore_ui_focus(self)
            return

        aoi_geom = None
        for ft in feats:
            g = ft.geometry()
            if g is None or g.isEmpty():
                continue
            aoi_geom = g if aoi_geom is None else aoi_geom.combine(g)
        if aoi_geom is None or aoi_geom.isEmpty():
            push_message(self.iface, "오류", "조사지역 지오메트리를 만들 수 없습니다.", level=2, duration=7)
            restore_ui_focus(self)
            return

        extent_aoi = aoi_geom.boundingBox()
        buf = float(self.spinExtentBuffer.value() or 0.0)
        if buf > 0:
            extent_aoi.grow(buf)

        # Keep a canvas-compatible extent for zooming (destination CRS).
        extent_canvas = QgsRectangle(extent_aoi)
        try:
            dest_crs = self.iface.mapCanvas().mapSettings().destinationCrs()
            if dest_crs and aoi.crs() != dest_crs:
                ct_canvas = QgsCoordinateTransform(aoi.crs(), dest_crs, QgsProject.instance())
                extent_canvas = ct_canvas.transformBoundingBox(extent_canvas)
        except Exception:
            pass

        # Transform survey-area extent to raster CRS for export.
        extent_export = QgsRectangle(extent_aoi)
        aoi_geom_raster = QgsGeometry(aoi_geom)
        try:
            if aoi.crs() != raster.crs():
                ct = QgsCoordinateTransform(aoi.crs(), raster.crs(), QgsProject.instance())
                extent_export = ct.transformBoundingBox(extent_export)
                try:
                    aoi_geom_raster.transform(ct)
                except Exception:
                    pass
        except Exception as e:
            log_message(f"GeoChem: extent transform failed: {e}", level=Qgis.Warning)
            try:
                aoi_geom_raster = QgsGeometry(aoi_geom)
            except Exception:
                aoi_geom_raster = None

        if extent_export.isEmpty() or extent_export.width() <= 0 or extent_export.height() <= 0:
            push_message(self.iface, "오류", "조사지역 경계(사각형)가 비어있습니다.", level=2, duration=7)
            restore_ui_focus(self)
            return

        # Choose pixel size: 0 = use current canvas resolution.
        px_input = float(self.spinPixelSize.value() or 0.0)
        px = px_input
        if px <= 0:
            try:
                # mapUnitsPerPixel is in destination CRS units; only use it when raster CRS matches.
                dest_crs = self.iface.mapCanvas().mapSettings().destinationCrs()
                if dest_crs and dest_crs == raster.crs():
                    px = float(self.iface.mapCanvas().mapUnitsPerPixel())
                else:
                    px = 0.0
            except Exception:
                px = 0.0
        if px <= 0:
            px = max(extent_export.width(), extent_export.height()) / 1024.0
        px = max(px, 1e-9)

        width = max(1, int(math.ceil(extent_export.width() / px)))
        height = max(1, int(math.ceil(extent_export.height() / px)))

        # Guardrail: prevent accidental huge rasters (memory explosion during numpy processing).
        MAX_PIXELS = 12_000_000
        total_px = int(width) * int(height)
        if total_px > MAX_PIXELS:
            if px_input > 0:
                push_message(
                    self.iface,
                    "오류",
                    f"요청 해상도가 너무 큽니다: {width}x{height} ({total_px:,} px). 픽셀 크기를 키우거나 조사지역 범위를 줄여주세요.",
                    level=2,
                    duration=10,
                )
                restore_ui_focus(self)
                return

            scale = math.sqrt(float(total_px) / float(MAX_PIXELS))
            px *= max(scale, 1.0)
            width = max(1, int(math.ceil(extent_export.width() / px)))
            height = max(1, int(math.ceil(extent_export.height() / px)))
            total_px = int(width) * int(height)
            log_message(
                f"GeoChem: auto-adjusted px to {px:g} to cap size => {width}x{height} ({total_px:,} px)",
                level=Qgis.Warning,
            )

        log_message(f"GeoChem: export extent {extent_export.toString()} px={px:g} => {width}x{height}", level=Qgis.Info)

        self._cleanup_tmp()
        self._tmp_dir = tempfile.mkdtemp(prefix="ArchToolkit_GeoChem_")
        run_id = uuid.uuid4().hex[:6]
        rgb_path = os.path.join(self._tmp_dir, f"wms_rgb_{run_id}.tif")
        val_path = os.path.join(self._tmp_dir, f"{preset.key}_value_{run_id}.tif")
        cls_path = os.path.join(self._tmp_dir, f"{preset.key}_class_{run_id}.tif")
        log_message(f"GeoChem: tmp={self._tmp_dir}", level=Qgis.Info)
        log_message(f"GeoChem: out rgb={rgb_path}", level=Qgis.Info)
        log_message(f"GeoChem: out val={val_path}", level=Qgis.Info)
        log_message(f"GeoChem: out cls={cls_path}", level=Qgis.Info)

        try:
            # 1) Export WMS RGB to GeoTIFF within survey-area extent (rectangular).
            ok = self._export_raster_to_geotiff(
                raster=raster, out_path=rgb_path, extent=extent_export, width=width, height=height
            )
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
            gt = None
            proj_wkt = ""
            try:
                gt = ds.GetGeoTransform()
                proj_wkt = str(ds.GetProjection() or "")
            except Exception:
                gt = None
                proj_wkt = ""
            r = ds.GetRasterBand(1).ReadAsArray()
            g = ds.GetRasterBand(2).ReadAsArray()
            b = ds.GetRasterBand(3).ReadAsArray()
            a = None
            if band_count >= 4:
                try:
                    a = ds.GetRasterBand(4).ReadAsArray()
                except Exception:
                    a = None
            try:
                log_message(
                    f"GeoChem: exported bands={band_count} shape={getattr(r, 'shape', None)} dtype={getattr(r, 'dtype', None)}",
                    level=Qgis.Info,
                )
            except Exception:
                pass

            log_message("GeoChem: RGB -> value mapping…", level=Qgis.Info)
            if do_snap_max:
                log_message(f"GeoChem: high-end snap enabled (last segment t>{snap_t:g} -> max)", level=Qgis.Info)
            out = _interp_rgb_to_value(
                r=r,
                g=g,
                b=b,
                points=preset.points,
                snap_last_t=snap_t if do_snap_max else None,
            )
            nodata_val = np.float32(-9999.0)

            # Transparent pixels (if alpha band exists) -> NoData
            transparent = None
            if a is not None:
                try:
                    transparent = a.astype(np.int16, copy=False) <= 0
                    out = out.astype(np.float32, copy=False)
                    out[transparent] = nodata_val
                    try:
                        total = int(transparent.size)
                        tr = int(np.count_nonzero(transparent))
                        log_message(f"GeoChem: alpha transparent {tr:,}/{total:,} ({(tr/total*100.0):.2f}%)", level=Qgis.Info)
                    except Exception:
                        pass
                except Exception:
                    transparent = None

            low_nodata_mask = None
            if do_low_as_nodata:
                try:
                    br = _points_to_breaks(preset.points)
                    min_valid = float(br[1]) if len(br) >= 2 else None
                    if min_valid is not None:
                        low_nodata_mask = np.isfinite(out) & (out != nodata_val) & (out < np.float32(min_valid))
                        n_low = int(np.count_nonzero(low_nodata_mask))
                        out = out.astype(np.float32, copy=False)
                        out[low_nodata_mask] = nodata_val
                        log_message(
                            f"GeoChem: treat <{min_valid:g} as nodata {n_low:,} px (legend low-end)",
                            level=Qgis.Info,
                        )
                except Exception:
                    low_nodata_mask = None

            try:
                total = int(out.size)
                nd = int(np.count_nonzero(out == nodata_val))
                log_message(f"GeoChem: nodata pixels {nd:,}/{total:,} ({(nd/total*100.0):.2f}%)", level=Qgis.Info)
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
                    if transparent is not None:
                        mask &= ~transparent
                    try:
                        m = int(np.count_nonzero(mask))
                        t = int(mask.size)
                        log_message(f"GeoChem: linework mask {m:,}/{t:,} ({(m/t*100.0):.2f}%)", level=Qgis.Info)
                    except Exception:
                        pass
                    out = out.astype(np.float32, copy=False)
                    out[mask] = np.nan
                except Exception:
                    pass
                log_message("GeoChem: filling masked pixels…", level=Qgis.Info)
                out = _gdal_fill_nodata_nearestish(arr=out, nodata=float(nodata_val), max_search_dist_px=fill_dist)
                if transparent is not None:
                    try:
                        out = out.astype(np.float32, copy=False)
                        out[transparent] = nodata_val
                    except Exception:
                        pass
                if low_nodata_mask is not None:
                    try:
                        out = out.astype(np.float32, copy=False)
                        out[low_nodata_mask] = nodata_val
                    except Exception:
                        pass
                try:
                    total = int(out.size)
                    nd = int(np.count_nonzero(out == nodata_val))
                    log_message(
                        f"GeoChem: nodata pixels after fill {nd:,}/{total:,} ({(nd/total*100.0):.2f}%)",
                        level=Qgis.Info,
                    )
                except Exception:
                    pass

            # Optional AOI mask: keep only pixels inside the AOI polygon (outside -> NoData).
            if do_mask_aoi:
                try:
                    if gt is not None and aoi_geom_raster is not None and not aoi_geom_raster.isEmpty():
                        mask_aoi = _gdal_rasterize_wkt_mask(
                            geom_wkt=aoi_geom_raster.asWkt(),
                            xsize=int(ds.RasterXSize),
                            ysize=int(ds.RasterYSize),
                            geotransform=gt,
                            projection_wkt=proj_wkt,
                        )
                    else:
                        mask_aoi = None
                    if mask_aoi is not None:
                        out = out.astype(np.float32, copy=False)
                        out[~mask_aoi] = nodata_val
                        try:
                            total = int(mask_aoi.size)
                            inside = int(np.count_nonzero(mask_aoi))
                            log_message(
                                f"GeoChem: AOI mask inside {inside:,}/{total:,} ({(inside/total*100.0):.2f}%)",
                                level=Qgis.Info,
                            )
                        except Exception:
                            pass
                        try:
                            total2 = int(out.size)
                            nd2 = int(np.count_nonzero(out == nodata_val))
                            log_message(
                                f"GeoChem: nodata pixels after AOI mask {nd2:,}/{total2:,} ({(nd2/total2*100.0):.2f}%)",
                                level=Qgis.Info,
                            )
                        except Exception:
                            pass
                except Exception:
                    pass

            # Ensure explicit nodata
            out = out.astype(np.float32, copy=False)
            out[~np.isfinite(out)] = nodata_val

            # Write value raster
            log_message("GeoChem: writing value raster…", level=Qgis.Info)
            self._write_single_band_geotiff(ds, out_path=val_path, data=out, nodata=float(nodata_val))

            try:
                valid = np.isfinite(out) & (out != nodata_val)
                if np.any(valid):
                    vmin = float(np.nanmin(out[valid]))
                    vmax = float(np.nanmax(out[valid]))
                    p99 = float(np.nanpercentile(out[valid], 99.0))
                    log_message(f"GeoChem: value stats min={vmin:g} max={vmax:g} p99={p99:g}", level=Qgis.Info)
                else:
                    log_message("GeoChem: value stats (no valid pixels)", level=Qgis.Warning)
            except Exception:
                pass
            need_class = bool(do_make_polygons or do_make_class_raster)
            if need_class:
                # 3) Class raster (optional output, required internally for polygonize)
                breaks = _points_to_breaks(preset.points)
                log_message(f"GeoChem: classify to {len(breaks)-1} bins…", level=Qgis.Info)
                cls = _classify_to_bins(values=out, breaks=breaks, nodata_class=0, nodata_value=float(nodata_val))
                try:
                    flat = cls.ravel()
                    counts = np.bincount(flat.astype(np.int64, copy=False), minlength=len(breaks))
                    parts = [f"class0={int(counts[0]):,}"]
                    for i in range(1, len(breaks)):
                        v0 = float(breaks[i - 1])
                        v1 = float(breaks[i])
                        parts.append(f"class{i}({v0:g}-{v1:g})={int(counts[i]):,}")
                    log_message("GeoChem: class counts " + " | ".join(parts), level=Qgis.Info)

                    # Store counts so we can write per-class pixel stats into the polygon layer.
                    pix_counts = {}
                    try:
                        for i in range(len(breaks)):  # 0..n_classes
                            pix_counts[int(i)] = int(counts[i]) if i < len(counts) else 0
                    except Exception:
                        pix_counts = {}
                    self._last_geochem_pix_counts = pix_counts
                except Exception:
                    self._last_geochem_pix_counts = {}
                    pass

                # Per-class mean value (useful as a representative numeric attribute on dissolved polygons).
                try:
                    valid_mean = (cls > 0) & np.isfinite(out) & (out != nodata_val)
                    if np.any(valid_mean):
                        flat_cls = cls[valid_mean].astype(np.int64, copy=False)
                        flat_out = out[valid_mean].astype(np.float64, copy=False)
                        sums = np.bincount(flat_cls, weights=flat_out, minlength=len(breaks))
                        sums2 = np.bincount(flat_cls, weights=flat_out * flat_out, minlength=len(breaks))
                        cnts = np.bincount(flat_cls, minlength=len(breaks))
                        means = {}
                        stds = {}
                        for i in range(1, len(breaks)):
                            if i < len(cnts) and cnts[i] > 0:
                                mu = float(sums[i] / cnts[i])
                                means[int(i)] = mu
                                try:
                                    var = float(sums2[i] / cnts[i]) - (mu * mu)
                                    if var < 0 and var > -1e-12:
                                        var = 0.0
                                    stds[int(i)] = float(math.sqrt(var)) if var > 0 else 0.0
                                except Exception:
                                    stds[int(i)] = 0.0
                        self._last_geochem_class_mean = means
                        self._last_geochem_class_std = stds
                        try:
                            last_cid = len(breaks) - 1
                            if int(last_cid) in means:
                                log_message(f"GeoChem: class{last_cid} mean={means[int(last_cid)]:g}", level=Qgis.Info)
                        except Exception:
                            pass
                    else:
                        self._last_geochem_class_mean = {}
                        self._last_geochem_class_std = {}
                except Exception:
                    self._last_geochem_class_mean = {}
                    self._last_geochem_class_std = {}

                self._write_single_band_geotiff(
                    ds,
                    out_path=cls_path,
                    data=cls.astype(np.int16, copy=False),
                    nodata=0,
                    gdal_type=gdal.GDT_Int16,
                )
            else:
                self._last_geochem_pix_counts = {}
                self._last_geochem_class_mean = {}
                self._last_geochem_class_std = {}
            ds = None

            persist_val_path = None
            persist_cls_path = None
            if do_save_rasters:
                try:
                    persist_val_path, persist_cls_path = self._persist_geochem_rasters(
                        preset=preset,
                        run_id=run_id,
                        val_path=val_path,
                        cls_path=cls_path if do_make_class_raster else None,
                    )
                    log_message(
                        f"GeoChem: saved rasters val={persist_val_path} cls={persist_cls_path}",
                        level=Qgis.Info,
                    )
                except Exception as e:
                    log_message(f"GeoChem: failed to save rasters: {e}", level=Qgis.Warning)
                    persist_val_path = None
                    persist_cls_path = None

            center_layer = None
            if do_weight_center:
                try:
                    if not do_mask_aoi:
                        log_message(
                            "GeoChem: AOI 마스크가 꺼져 있어 가중 중심점이 경계 사각형(extent) 기준으로 계산됩니다.",
                            level=Qgis.Warning,
                        )
                except Exception:
                    pass
                try:
                    center_layer = self._make_weighted_center_layer(
                        values=out,
                        nodata_value=float(nodata_val),
                        geotransform=gt,
                        src_crs=raster.crs(),
                        dest_crs=self.iface.mapCanvas().mapSettings().destinationCrs(),
                        preset=preset,
                        unit=unit,
                        run_id=run_id,
                        center_method=center_method,
                        weight_rule=weight_rule,
                        weight_power=weight_power,
                        weight_threshold=weight_threshold,
                        weight_top_pct=weight_top_pct,
                    )
                except Exception as e:
                    log_message(f"GeoChem: center failed: {e}", level=Qgis.Warning)
                    center_layer = None

            poly: Optional[QgsVectorLayer] = None
            if do_make_polygons:
                # 4) Polygonize -> dissolve
                log_message("GeoChem: polygonize…", level=Qgis.Info)
                poly_path = os.path.join(self._tmp_dir, f"{preset.key}_poly_{run_id}.gpkg")
                try:
                    if os.path.exists(poly_path):
                        os.remove(poly_path)
                except Exception:
                    pass

                poly_out = processing.run(
                    "gdal:polygonize",
                    {
                        "INPUT": cls_path,
                        "BAND": 1,
                        "FIELD": "class_id",
                        "EIGHT_CONNECTEDNESS": True,
                        "OUTPUT": poly_path,
                    },
                ).get("OUTPUT")

                log_message(f"GeoChem: polygonize OUTPUT type={type(poly_out).__name__} value={poly_out}", level=Qgis.Info)

                if isinstance(poly_out, QgsVectorLayer):
                    poly = poly_out
                else:
                    out_str = str(poly_out) if poly_out is not None else ""
                    out_path = (out_str.split("|", 1)[0] or "").strip()
                    if out_path:
                        poly_path = out_path

                    try:
                        exists = os.path.exists(poly_path)
                        size = os.path.getsize(poly_path) if exists else 0
                        log_message(f"GeoChem: polygonize file exists={exists} size={size}", level=Qgis.Info)
                    except Exception:
                        pass

                    # Try to discover the layer name from the GeoPackage and load it reliably.
                    layer_name = None
                    try:
                        vds = gdal.OpenEx(poly_path, gdal.OF_VECTOR)
                        if vds is not None:
                            names = []
                            try:
                                n = int(vds.GetLayerCount() or 0)
                            except Exception:
                                n = 0
                            for i in range(n):
                                try:
                                    lyr = vds.GetLayerByIndex(i)
                                    if lyr is not None:
                                        names.append(str(lyr.GetName()))
                                except Exception:
                                    continue
                            vds = None
                            log_message(f"GeoChem: gpkg layers={names}", level=Qgis.Info)
                            if names:
                                layer_name = names[0]
                    except Exception:
                        pass

                    uri_candidates = []
                    if layer_name:
                        uri_candidates.append(f"{poly_path}|layername={layer_name}")
                    uri_candidates.append(poly_path)

                    for uri in uri_candidates:
                        try:
                            cand = QgsVectorLayer(uri, f"{preset.label} polygons", "ogr")
                            if cand.isValid():
                                poly = cand
                                break
                        except Exception:
                            continue

                if poly is None or not isinstance(poly, QgsVectorLayer) or not poly.isValid():
                    raise RuntimeError("Polygonize failed (no valid vector layer output)")

                # Drop nodata (class_id == 0)
                if do_drop_nodata:
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

            # Add to project (rasters / polygon / point)
            self._add_to_project(
                layer=poly,
                center_layer=center_layer,
                preset=preset,
                unit=unit,
                run_id=run_id,
                extent=extent_canvas,
                value_raster_path=persist_val_path if do_add_rasters else None,
                class_raster_path=persist_cls_path if do_add_rasters else None,
            )
            push_message(self.iface, "지구화학도 래스터 수치화", "완료", level=0, duration=7)
        except Exception as e:
            log_exception("GeoChem error", e)
            try:
                log_message(f"GeoChem: kept temp folder for debug: {self._tmp_dir}", level=Qgis.Warning)
            except Exception:
                pass
            push_message(self.iface, "오류", f"처리 실패: {e}", level=2, duration=10)
        finally:
            restore_ui_focus(self)

    def _export_raster_to_geotiff(self, *, raster: QgsRasterLayer, out_path: str, extent: QgsRectangle, width: int, height: int) -> bool:
        """Export the raster (including WMS) to a GeoTIFF.

        1) Try QGIS raster writer (fast, works for many providers).
        2) Fallback to Processing GDAL warp (more robust for some WMS providers).
        """
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
                    raise RuntimeError("pipe.set(provider) failed")
            writer = QgsRasterFileWriter(out_path)
            writer.setOutputFormat("GTiff")
            writer.setCreateOptions(["COMPRESS=LZW", "TILED=YES"])
            ctx = QgsProject.instance().transformContext()
            res = writer.writeRaster(pipe, int(width), int(height), extent, raster.crs(), ctx)
            if res != 0:
                log_message(f"GeoChem: writeRaster returned {res}", level=Qgis.Warning)
                raise RuntimeError(f"writeRaster failed ({res})")
            if os.path.exists(out_path):
                return True
        except Exception as e:
            log_message(f"GeoChem: QGIS export failed, trying GDAL warp… ({e})", level=Qgis.Warning)

        # Fallback: GDAL warp through Processing (more provider-compatible)
        try:
            extent_str = f"{extent.xMinimum()},{extent.xMaximum()},{extent.yMinimum()},{extent.yMaximum()}"
            # Match the requested width/height via target resolution in layer CRS units.
            px = max(extent.width() / max(1, int(width)), extent.height() / max(1, int(height)))
            px = float(px) if px > 0 else None
            processing.run(
                "gdal:warpreproject",
                {
                    "INPUT": raster,
                    "SOURCE_CRS": None,
                    "TARGET_CRS": None,
                    "RESAMPLING": 0,  # Nearest (preserve legend colors)
                    "NODATA": None,
                    "TARGET_RESOLUTION": px,
                    "OPTIONS": "COMPRESS=LZW|TILED=YES",
                    "DATA_TYPE": 0,
                    "TARGET_EXTENT": extent_str,
                    "TARGET_EXTENT_CRS": raster.crs().authid() if raster.crs() else None,
                    "MULTITHREADING": False,
                    "EXTRA": "",
                    "OUTPUT": out_path,
                },
            )
            if os.path.exists(out_path):
                return True
            log_message("GeoChem: GDAL warp completed but output missing", level=Qgis.Warning)
            return False
        except Exception as e:
            log_message(f"GeoChem: GDAL warp export failed: {e}", level=Qgis.Warning)
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

    def _persist_geochem_rasters(
        self,
        *,
        preset: GeoChemPreset,
        run_id: str,
        val_path: str,
        cls_path: Optional[str] = None,
    ) -> Tuple[str, Optional[str]]:
        """Persist generated rasters so they remain usable after the dialog closes."""
        out_dir = ""
        try:
            base = (QgsProject.instance().homePath() or "").strip()
            if base:
                out_dir = os.path.join(base, "ArchToolkit_outputs", "geochem")
            else:
                from qgis.core import QgsApplication

                out_dir = os.path.join(QgsApplication.qgisSettingsDirPath(), "ArchToolkit", "outputs", "geochem")
        except Exception:
            out_dir = ""

        if not out_dir:
            try:
                from qgis.PyQt.QtCore import QStandardPaths

                out_dir = os.path.join(
                    QStandardPaths.writableLocation(QStandardPaths.DocumentsLocation),
                    "ArchToolkit_outputs",
                    "geochem",
                )
            except Exception:
                out_dir = os.path.join(os.path.expanduser("~"), "ArchToolkit_outputs", "geochem")

        os.makedirs(out_dir, exist_ok=True)
        val_dst = os.path.join(out_dir, os.path.basename(val_path))
        shutil.copy2(val_path, val_dst)
        cls_dst = None
        if cls_path:
            try:
                if os.path.exists(cls_path):
                    cls_dst = os.path.join(out_dir, os.path.basename(cls_path))
                    shutil.copy2(cls_path, cls_dst)
            except Exception:
                cls_dst = None
        return val_dst, cls_dst

    def _make_weighted_center_layer(
        self,
        *,
        values: np.ndarray,
        nodata_value: float,
        geotransform,
        src_crs,
        dest_crs,
        preset: GeoChemPreset,
        unit: str,
        run_id: str,
        center_method: str,
        weight_rule: str,
        weight_power: int,
        weight_threshold: float,
        weight_top_pct: float,
    ) -> Optional[QgsVectorLayer]:
        """Compute a center point from a value raster and return a point memory layer (or None)."""
        if geotransform is None:
            return None

        gt = list(geotransform) if geotransform is not None else None
        if not gt or len(gt) != 6:
            return None

        v = values.astype(np.float32, copy=False)
        valid = np.isfinite(v)
        try:
            valid &= v != np.float32(float(nodata_value))
        except Exception:
            pass
        if not np.any(valid):
            log_message("GeoChem: center skipped (no valid pixels)", level=Qgis.Warning)
            return None

        method = (center_method or "weighted_mean").strip()
        if method not in ("weighted_mean", "mean", "peak"):
            method = "weighted_mean"

        rule = (weight_rule or "value").strip()
        thr_used = None
        sel = np.zeros(v.shape, dtype=bool)

        # Build weight array (float64) with zeros outside selection.
        w = np.zeros(v.shape, dtype=np.float64)
        try:
            if rule == "power":
                p = max(1, int(weight_power))
                sel = valid
                vv = np.maximum(v[sel].astype(np.float64, copy=False), 0.0)
                w[sel] = np.power(vv, float(p))
                param = float(p)
            elif rule == "threshold":
                t = float(weight_threshold)
                sel = valid & (v >= np.float32(t))
                w[sel] = v[sel].astype(np.float64, copy=False)
                param = float(t)
            elif rule == "binary":
                t = float(weight_threshold)
                sel = valid & (v >= np.float32(t))
                w[sel] = 1.0
                param = float(t)
            elif rule == "top_pct":
                pct = float(weight_top_pct)
                pct = min(max(pct, 0.1), 100.0)
                vv = v[valid].astype(np.float64, copy=False)
                thr = float(np.nanpercentile(vv, 100.0 - pct))
                thr_used = thr
                sel = valid & (v >= np.float32(thr))
                w[sel] = v[sel].astype(np.float64, copy=False)
                param = float(pct)
            else:
                sel = valid
                vv = np.maximum(v[sel].astype(np.float64, copy=False), 0.0)
                w[sel] = vv
                param = float(1.0)
        except Exception:
            log_message("GeoChem: center skipped (weight computation failed)", level=Qgis.Warning)
            return None

        if not np.any(sel):
            log_message("GeoChem: center skipped (no pixels after selection)", level=Qgis.Warning)
            return None

        sum_w = float(np.sum(w))
        pix_n = 0
        try:
            pix_n = int(np.count_nonzero(sel))
        except Exception:
            pix_n = 0

        mean_row = None
        mean_col = None
        peak_val = None

        if method == "peak":
            try:
                vv = np.where(sel, v, np.float32(-np.inf))
                idx = int(np.argmax(vv))
                ncol = int(v.shape[1])
                mean_row = float(idx // ncol)
                mean_col = float(idx % ncol)
                peak_val = float(vv[int(mean_row), int(mean_col)])
            except Exception:
                log_message("GeoChem: center skipped (peak computation failed)", level=Qgis.Warning)
                return None
        else:
            if method == "weighted_mean":
                if not math.isfinite(sum_w) or sum_w <= 0:
                    log_message("GeoChem: center skipped (sum_w <= 0)", level=Qgis.Warning)
                    return None
                denom = float(sum_w)
                try:
                    row_sums = np.sum(w, axis=1, dtype=np.float64)
                    col_sums = np.sum(w, axis=0, dtype=np.float64)
                except Exception:
                    log_message("GeoChem: center skipped (centroid accumulation failed)", level=Qgis.Warning)
                    return None
            else:
                denom = float(pix_n)
                if not math.isfinite(denom) or denom <= 0:
                    log_message("GeoChem: center skipped (pix_n <= 0)", level=Qgis.Warning)
                    return None
                try:
                    row_sums = np.sum(sel, axis=1, dtype=np.float64)
                    col_sums = np.sum(sel, axis=0, dtype=np.float64)
                except Exception:
                    log_message("GeoChem: center skipped (centroid accumulation failed)", level=Qgis.Warning)
                    return None

            try:
                rows = np.arange(v.shape[0], dtype=np.float64)
                cols = np.arange(v.shape[1], dtype=np.float64)
                mean_row = float(np.dot(row_sums, rows) / denom)
                mean_col = float(np.dot(col_sums, cols) / denom)
            except Exception:
                log_message("GeoChem: center skipped (centroid accumulation failed)", level=Qgis.Warning)
                return None

        if mean_row is None or mean_col is None:
            return None

        # Convert pixel-space centroid to map coordinates (affine geotransform).
        x = float(gt[0] + (mean_col + 0.5) * gt[1] + (mean_row + 0.5) * gt[2])
        y = float(gt[3] + (mean_col + 0.5) * gt[4] + (mean_row + 0.5) * gt[5])

        # Transform to destination CRS for display.
        pt = QgsPointXY(x, y)
        try:
            if dest_crs and src_crs and dest_crs != src_crs:
                ct = QgsCoordinateTransform(src_crs, dest_crs, QgsProject.instance())
                pt = QgsPointXY(ct.transform(pt))
        except Exception:
            pass

        # Create output point layer
        crs = dest_crs if dest_crs else src_crs
        uri = "Point?crs=EPSG:4326"
        try:
            auth = (crs.authid() or "").strip() if crs else ""
            if auth:
                uri = f"Point?crs={auth}"
        except Exception:
            pass

        layer = QgsVectorLayer(uri, f"{preset.key}_중심점_{run_id}", "memory")
        if not layer.isValid():
            return None
        try:
            if crs:
                layer.setCrs(crs)
        except Exception:
            pass

        pr = layer.dataProvider()
        pr.addAttributes(
            [
                QgsField("element", QVariant.String),
                QgsField("unit", QVariant.String),
                QgsField("c_method", QVariant.String),
                QgsField("w_rule", QVariant.String),
                QgsField("w_param", QVariant.Double),
                QgsField("w_thr", QVariant.Double),
                QgsField("w_sum", QVariant.Double),
                QgsField("pix_n", QVariant.Int),
            ]
        )
        layer.updateFields()

        ft = QgsFeature(layer.fields())
        ft.setGeometry(QgsGeometry.fromPointXY(pt))
        ft["element"] = preset.label
        ft["unit"] = unit
        ft["c_method"] = method
        ft["w_rule"] = rule
        ft["w_param"] = float(param)
        try:
            ft["w_thr"] = float(thr_used) if thr_used is not None else None
        except Exception:
            ft["w_thr"] = None
        ft["w_sum"] = float(sum_w)
        ft["pix_n"] = int(pix_n)
        pr.addFeatures([ft])
        layer.updateExtents()

        try:
            from qgis.core import QgsMarkerSymbol, QgsSingleSymbolRenderer

            sym = QgsMarkerSymbol.createSimple({"name": "circle", "color": "230,0,0,255", "size": "4"})
            layer.setRenderer(QgsSingleSymbolRenderer(sym))
        except Exception:
            pass

        try:
            extra = ""
            if peak_val is not None:
                extra = f" peak={peak_val:g}"
            log_message(
                f"GeoChem: center method={method} rule={rule} param={param:g} sum_w={sum_w:g} pix_n={pix_n:,}{extra}",
                level=Qgis.Info,
            )
        except Exception:
            pass

        return layer

    def _style_value_raster(self, *, layer: QgsRasterLayer, preset: GeoChemPreset, unit: str):
        """Apply legend-based pseudo-color styling to a value raster layer."""
        try:
            from qgis.core import QgsColorRampShader, QgsRasterShader, QgsSingleBandPseudoColorRenderer

            shader = QgsRasterShader()
            ramp = QgsColorRampShader()
            ramp.setColorRampType(QgsColorRampShader.Interpolated)
            items = []
            for p in preset.points:
                try:
                    val = float(p.value)
                    col = QColor(int(p.rgb[0]), int(p.rgb[1]), int(p.rgb[2]))
                    items.append(QgsColorRampShader.ColorRampItem(val, col, f"{val:g}{unit}"))
                except Exception:
                    continue
            if not items:
                return
            ramp.setColorRampItemList(items)
            try:
                ramp.setMinimumValue(float(items[0].value))
                ramp.setMaximumValue(float(items[-1].value))
            except Exception:
                pass
            shader.setRasterShaderFunction(ramp)
            renderer = QgsSingleBandPseudoColorRenderer(layer.dataProvider(), 1, shader)
            try:
                renderer.setClassificationMin(float(items[0].value))
                renderer.setClassificationMax(float(items[-1].value))
            except Exception:
                pass
            layer.setRenderer(renderer)
            layer.triggerRepaint()
        except Exception:
            pass

    def _style_class_raster(self, *, layer: QgsRasterLayer, preset: GeoChemPreset, unit: str):
        """Apply legend-based palette styling to a class raster layer."""
        try:
            from qgis.core import QgsPalettedRasterRenderer

            breaks = _points_to_breaks(preset.points)
            classes = []
            try:
                nd = preset.points[0].rgb if preset.points else (204, 204, 204)
                classes.append(QgsPalettedRasterRenderer.Class(0, QColor(int(nd[0]), int(nd[1]), int(nd[2])), "NoData"))
            except Exception:
                pass
            for i in range(1, len(breaks)):
                v0 = float(breaks[i - 1])
                v1 = float(breaks[i])
                mid = (v0 + v1) / 2.0
                col = _rgb_for_value(points=preset.points, value=mid)
                classes.append(
                    QgsPalettedRasterRenderer.Class(int(i), QColor(int(col[0]), int(col[1]), int(col[2])), _interval_label(v0, v1, unit))
                )
            if not classes:
                return
            renderer = QgsPalettedRasterRenderer(layer.dataProvider(), 1, classes)
            layer.setRenderer(renderer)
            layer.triggerRepaint()
        except Exception:
            pass

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
                QgsField("v_mid", QVariant.Double),
                QgsField("val_mean", QVariant.Double),
                QgsField("val_std", QVariant.Double),
                QgsField("label", QVariant.String),
                QgsField("pix_n", QVariant.Int),
                QgsField("pix_pct", QVariant.Double),
                QgsField("area_m2", QVariant.Double),
                QgsField("area_ha", QVariant.Double),
                QgsField("area_pct", QVariant.Double),
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
            _alias("v_mid", "구간 대표값(중앙)")
            _alias("val_mean", "구간 평균값")
            _alias("label", "구간 라벨")
            _alias("pix_n", "픽셀 수")
            _alias("pix_pct", "픽셀 비율(%)")
            _alias("area_m2", "면적(m²)")
            _alias("area_ha", "면적(ha)")
            _alias("area_pct", "면적 비율(%)")
        except Exception:
            pass

        try:
            idx = layer.fields().indexFromName("val_std")
            if idx >= 0:
                layer.setFieldAlias(idx, "구간 표준편차")
        except Exception:
            pass

        # Apply attributes and style
        cats = []
        try:
            nd_col = preset.points[0].rgb if preset.points else (204, 204, 204)
            nd_qcol = QColor(int(nd_col[0]), int(nd_col[1]), int(nd_col[2]), 120)
            from qgis.core import QgsFillSymbol

            nd_sym = QgsFillSymbol.createSimple(
                {
                    "color": f"{nd_qcol.red()},{nd_qcol.green()},{nd_qcol.blue()},{nd_qcol.alpha()}",
                    "outline_color": "0,0,0,30",
                    "outline_width": "0.1",
                }
            )
            cats.append(QgsRendererCategory(int(0), nd_sym, "NoData"))
        except Exception:
            pass
        for i, (v0, v1) in enumerate(intervals, start=1):
            mid = (float(v0) + float(v1)) / 2.0
            col = _rgb_for_value(points=preset.points, value=mid)
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

        # Compute per-feature geometry area (m²) for statistics fields.
        # When dissolve is enabled, there is typically 1 feature per class_id.
        area_by_fid = {}
        features_per_class = {}
        has_class0 = False
        try:
            from qgis.core import QgsDistanceArea, QgsProject

            dist = QgsDistanceArea()
            dist.setSourceCrs(layer.crs(), QgsProject.instance().transformContext())
            try:
                ell = (QgsProject.instance().ellipsoid() or "").strip()
                if not ell or ell.upper() == "NONE":
                    ell = "WGS84"
                dist.setEllipsoid(ell)
                dist.setEllipsoidalMode(True)
            except Exception:
                pass

            for ft in layer.getFeatures():
                try:
                    try:
                        cid = int(ft["class_id"]) if ft["class_id"] is not None else 0
                        if cid == 0:
                            has_class0 = True
                            features_per_class[0] = int(features_per_class.get(0, 0)) + 1
                        elif cid > 0:
                            features_per_class[cid] = int(features_per_class.get(cid, 0)) + 1
                    except Exception:
                        pass
                    geom = ft.geometry()
                    if geom is None or geom.isEmpty():
                        continue
                    area_m2 = float(dist.measureArea(geom))
                    if math.isfinite(area_m2) and area_m2 > 0:
                        area_by_fid[int(ft.id())] = area_m2
                except Exception:
                    continue
        except Exception:
            area_by_fid = {}
            features_per_class = {}
            has_class0 = False

        total_area_m2 = float(sum(area_by_fid.values())) if area_by_fid else 0.0
        if not math.isfinite(total_area_m2) or total_area_m2 <= 0:
            total_area_m2 = 0.0

        # Pixel-count stats (if provided by run()).
        pix_n_by_cid = {}
        try:
            pix_n_by_cid = getattr(self, "_last_geochem_pix_counts", {}) or {}
        except Exception:
            pix_n_by_cid = {}
        mean_by_cid = {}
        try:
            mean_by_cid = getattr(self, "_last_geochem_class_mean", {}) or {}
        except Exception:
            mean_by_cid = {}
        std_by_cid = {}
        try:
            std_by_cid = getattr(self, "_last_geochem_class_std", {}) or {}
        except Exception:
            std_by_cid = {}
        is_one_feature_per_class = True
        try:
            is_one_feature_per_class = all(int(n) <= 1 for n in features_per_class.values())
        except Exception:
            is_one_feature_per_class = True
        if not is_one_feature_per_class:
            try:
                log_message(
                    "GeoChem: dissolve가 꺼져 있어 class_id별 픽셀 통계를 폴리곤 피처에 직접 기록하지 않습니다.",
                    level=Qgis.Warning,
                )
            except Exception:
                pass
        total_pix = 0
        try:
            for cid, n in pix_n_by_cid.items():
                if has_class0 and int(cid) >= 0:
                    total_pix += int(n)
                elif int(cid) > 0:
                    total_pix += int(n)
        except Exception:
            total_pix = 0

        # Write per-feature attributes
        layer.startEditing()
        try:
            for ft in layer.getFeatures():
                cid = int(ft["class_id"]) if ft["class_id"] is not None else 0
                ft["element"] = preset.label
                ft["unit"] = unit
                if cid == 0:
                    ft["v_min"] = None
                    ft["v_max"] = None
                    ft["v_mid"] = None
                    ft["val_mean"] = None
                    ft["val_std"] = None
                    ft["label"] = "NoData"
                    if is_one_feature_per_class:
                        try:
                            pix_n = int(pix_n_by_cid.get(0, 0) or 0)
                        except Exception:
                            pix_n = 0
                        ft["pix_n"] = int(pix_n)
                        if total_pix > 0:
                            ft["pix_pct"] = float(pix_n) * 100.0 / float(total_pix)
                        else:
                            ft["pix_pct"] = 0.0
                    else:
                        ft["pix_n"] = None
                        ft["pix_pct"] = None
                else:
                    if cid < 1 or cid > len(intervals):
                        continue
                    v0, v1 = intervals[cid - 1]
                    ft["v_min"] = float(v0)
                    ft["v_max"] = float(v1)
                    v_mid = (float(v0) + float(v1)) / 2.0
                    ft["label"] = _interval_label(v0, v1, unit)
                    if is_one_feature_per_class:
                        ft["v_mid"] = v_mid
                        try:
                            ft["val_mean"] = float(mean_by_cid.get(cid, v_mid))
                        except Exception:
                            ft["val_mean"] = v_mid
                        try:
                            ft["val_std"] = float(std_by_cid.get(cid, 0.0))
                        except Exception:
                            ft["val_std"] = 0.0
                        try:
                            pix_n = int(pix_n_by_cid.get(cid, 0) or 0)
                        except Exception:
                            pix_n = 0
                        ft["pix_n"] = int(pix_n)
                        if total_pix > 0:
                            ft["pix_pct"] = float(pix_n) * 100.0 / float(total_pix)
                        else:
                            ft["pix_pct"] = 0.0
                    else:
                        # Avoid misleading duplicated values when there are multiple features per class.
                        ft["v_mid"] = None
                        ft["val_mean"] = None
                        ft["val_std"] = None
                        ft["pix_n"] = None
                        ft["pix_pct"] = None

                area_m2 = float(area_by_fid.get(int(ft.id()), 0.0) or 0.0)
                if not math.isfinite(area_m2) or area_m2 < 0:
                    area_m2 = 0.0
                ft["area_m2"] = area_m2
                ft["area_ha"] = area_m2 / 10000.0 if area_m2 > 0 else 0.0
                if total_area_m2 > 0:
                    ft["area_pct"] = area_m2 * 100.0 / total_area_m2
                else:
                    ft["area_pct"] = 0.0
                layer.updateFeature(ft)
        finally:
            layer.commitChanges()
        layer.triggerRepaint()

    def _add_to_project(
        self,
        *,
        layer: Optional[QgsVectorLayer],
        center_layer: Optional[QgsVectorLayer] = None,
        preset: GeoChemPreset,
        unit: str,
        run_id: str,
        extent: QgsRectangle,
        value_raster_path: Optional[str] = None,
        class_raster_path: Optional[str] = None,
    ):
        project = QgsProject.instance()
        root = project.layerTreeRoot()
        parent = root.findGroup(PARENT_GROUP_NAME)
        if parent is None:
            parent = root.insertGroup(0, PARENT_GROUP_NAME)

        if layer is not None:
            try:
                name = f"{preset.key}_구간폴리곤_{run_id}"
                layer.setName(name)
            except Exception:
                pass
        if center_layer is not None:
            try:
                center_layer.setName(f"{preset.key}_중심점_{run_id}")
            except Exception:
                pass

        layers_to_add = []
        val_layer = None
        cls_layer = None
        if class_raster_path:
            try:
                cls_layer = QgsRasterLayer(class_raster_path, f"{preset.key}_class_{run_id}")
                if cls_layer.isValid():
                    self._style_class_raster(layer=cls_layer, preset=preset, unit=unit)
            except Exception:
                pass
        if value_raster_path:
            try:
                val_layer = QgsRasterLayer(value_raster_path, f"{preset.key}_value_{run_id}")
                if val_layer.isValid():
                    self._style_value_raster(layer=val_layer, preset=preset, unit=unit)
            except Exception:
                pass

        if val_layer is not None and val_layer.isValid():
            layers_to_add.append(val_layer)
        if cls_layer is not None and cls_layer.isValid():
            layers_to_add.append(cls_layer)
        if layer is not None and layer.isValid():
            layers_to_add.append(layer)
        if center_layer is not None and center_layer.isValid():
            layers_to_add.append(center_layer)

        for lyr in layers_to_add:
            project.addMapLayer(lyr, False)
            parent.insertLayer(0, lyr)

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
