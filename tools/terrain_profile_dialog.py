# -*- coding: utf-8 -*-

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
Terrain Profile Dialog for ArchToolkit
Draw a line on DEM and display elevation profile with graphical chart
"""
import os
import csv
import datetime
import math
from typing import List, Optional, Sequence, Tuple
from qgis.PyQt import uic
from qgis.PyQt import QtWidgets
from qgis.PyQt.QtCore import Qt, QPointF, QRectF, QVariant
from qgis.PyQt.QtWidgets import QMessageBox, QFileDialog, QWidget
from qgis.PyQt.QtGui import QColor, QPainter, QPen, QBrush, QPalette, QPainterPath, QImage
from qgis.core import (
    QgsProject, QgsMapLayerProxyModel, QgsPointXY, QgsRaster,
    QgsVectorLayer, QgsField, QgsFeature, QgsFeatureRequest, QgsGeometry, QgsWkbTypes,
    QgsLineSymbol, QgsSingleSymbolRenderer, QgsSymbolLayer, QgsProperty, Qgis, QgsDistanceArea, QgsCoordinateTransform
)
from qgis.gui import QgsMapLayerComboBox, QgsMapToolEmitPoint, QgsRubberBand
from .utils import log_message, push_message, restore_ui_focus, transform_point
from .live_log_dialog import ensure_live_log_dialog

PROFILE_LAYER_NAME = "Terrain Profile Lines"
PROFILE_GROUP_NAME = "ArchToolkit - Terrain Profile"
PROFILE_SINGLE_SUBGROUP_NAME = "단면선 (개별 레이어)"
PROFILE_KIND_PROP = "ArchToolkit/profile_kind"
PROFILE_KIND_SINGLE = "terrain_profile_single"


def _profile_color_palette() -> List[QColor]:
    # A small set of distinct, print-friendly colors (rotates when exceeded).
    return [
        QColor("#1f77b4"),  # blue
        QColor("#ff7f0e"),  # orange
        QColor("#2ca02c"),  # green
        QColor("#d62728"),  # red
        QColor("#9467bd"),  # purple
        QColor("#8c564b"),  # brown
        QColor("#e377c2"),  # pink
        QColor("#7f7f7f"),  # gray
        QColor("#bcbd22"),  # olive
        QColor("#17becf"),  # cyan
    ]


FORM_CLASS, _ = uic.loadUiType(os.path.join(
    os.path.dirname(__file__), 'terrain_profile_dialog_base.ui'))


class ProfileChartWidget(QWidget):
    """Custom widget to draw elevation profile using QPainter
    
    Features:
    - Scroll wheel to zoom in/out
    - Mouse tracking to show position info
    - Drag to pan when zoomed
    - Smooth line rendering
    """
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.data = []      # List of {'distance': d, 'elevation': e, 'x': x, 'y': y}
        self.smooth_data = []
        self.min_e = 0
        self.max_e = 100
        self.total_d = 0
        self.setMinimumHeight(250)
        self.setBackgroundRole(QPalette.Base)
        self.setAutoFillBackground(True)
        
        # Zoom and pan
        self.zoom_level = 1.0
        self.pan_offset = 0  # Horizontal offset in data units (distance)
        
        # Mouse tracking
        self.setMouseTracking(True)
        self.mouse_x = -1
        self.mouse_y = -1
        self.hover_distance = None
        self.hover_elevation = None
        self.hover_x = None  # Map X coordinate
        self.hover_y = None  # Map Y coordinate
        
        # Drag panning
        self.is_dragging = False
        self.drag_start_x = 0
        self.drag_start_offset = 0
        
        # Callback for map synchronization
        self.on_hover_callback = None  # Function(x, y) to show position on map

        # Profile line color (can be varied per saved profile)
        self.profile_color = QColor(0, 100, 255)

        # Highlight ranges on distance axis (e.g., AOI intersection)
        self.highlight_ranges: List[Tuple[float, float]] = []
        self.highlight_label: str = ""
        self.highlight_color = QColor(255, 193, 7, 40)  # amber with alpha

        # Overlay (optional): show another layer along the profile.
        # - Polygon layers: ranges (background shading)
        # - Point/line layers: markers (vertical lines + points on the profile)
        self.overlay_ranges: List[Tuple[float, float]] = []
        self.overlay_label: str = ""
        self.overlay_color = QColor(76, 175, 80, 40)  # green with alpha
        self.overlay_markers: List[Tuple[float, str]] = []  # (distance_m, label)
        self.overlay_marker_color = QColor(76, 175, 80, 180)
        
        # Margins
        self.margin_left = 60
        self.margin_top = 30
        self.margin_right = 30
        self.margin_bottom = 40

    def set_data(self, data):
        self.data = data
        self.zoom_level = 1.0
        self.pan_offset = 0
        self.highlight_ranges = []
        self.highlight_label = ""
        self.overlay_ranges = []
        self.overlay_label = ""
        self.overlay_markers = []
        
        if not data:
            self.smooth_data = []
            self.update()
            return

        # Simple moving average for smoothing
        elevations = [p['elevation'] for p in data]
        smoothed = []
        window = 3
        for i in range(len(elevations)):
            start = max(0, i - window)
            end = min(len(elevations), i + window + 1)
            avg = sum(elevations[start:end]) / (end - start)
            smoothed.append(avg)
        
        self.smooth_data = []
        for i in range(len(data)):
            self.smooth_data.append({
                'distance': data[i]['distance'],
                'elevation': smoothed[i]
            })

        self.min_e = min(elevations)
        self.max_e = max(elevations)
        self.total_d = data[-1]['distance']
        
        # Add some margin to elevation range
        margin = (self.max_e - self.min_e) * 0.1
        if margin == 0: margin = 1
        self.min_e -= margin
        self.max_e += margin
        
        self.update()

    def wheelEvent(self, event):
        """Zoom in/out with scroll wheel"""
        if not self.data:
            return
        
        # Get zoom direction
        delta = event.angleDelta().y()
        if delta > 0:
            self.zoom_level = min(10.0, self.zoom_level * 1.2)
        else:
            self.zoom_level = max(1.0, self.zoom_level / 1.2)
        
        # Adjust pan offset to keep zoom centered
        if self.zoom_level == 1.0:
            self.pan_offset = 0
        
        self.update()
    
    def mousePressEvent(self, event):
        """Start dragging for pan"""
        if event.button() == Qt.LeftButton and self.zoom_level > 1.0:
            self.is_dragging = True
            self.drag_start_x = event.x()
            self.drag_start_offset = self.pan_offset
            self.setCursor(Qt.ClosedHandCursor)
    
    def mouseReleaseEvent(self, event):
        """End dragging"""
        if event.button() == Qt.LeftButton:
            self.is_dragging = False
            self.setCursor(Qt.ArrowCursor)
    
    def mouseMoveEvent(self, event):
        """Track mouse position, handle drag panning, and sync with map"""
        if not self.data or not self.smooth_data:
            return
        
        self.mouse_x = event.x()
        self.mouse_y = event.y()
        
        # Calculate chart area
        w = self.width() - self.margin_left - self.margin_right
        h = self.height() - self.margin_top - self.margin_bottom
        visible_range = self.total_d / self.zoom_level
        
        # Handle drag panning
        if self.is_dragging and self.zoom_level > 1.0:
            delta_x = self.drag_start_x - self.mouse_x
            delta_distance = (delta_x / w) * visible_range
            new_offset = self.drag_start_offset + delta_distance
            
            # Clamp to valid range
            max_offset = self.total_d - visible_range
            self.pan_offset = max(0, min(max_offset, new_offset))
            self.update()
            return
        
        # Check if mouse is in chart area
        if (self.margin_left <= self.mouse_x <= self.margin_left + w and
            self.margin_top <= self.mouse_y <= self.margin_top + h):
            
            # Calculate distance at mouse position
            rel_x = (self.mouse_x - self.margin_left) / w
            distance = self.pan_offset + rel_x * visible_range
            
            # Find closest data point (with map coordinates)
            if 0 <= distance <= self.total_d:
                # Find from original data which has x, y coordinates
                closest = min(self.data, key=lambda p: abs(p['distance'] - distance))
                self.hover_distance = closest['distance']
                self.hover_elevation = closest['elevation']
                self.hover_x = closest.get('x')
                self.hover_y = closest.get('y')
                
                # Show tooltip
                tooltip_text = f"거리: {self.hover_distance:.1f}m\n고도: {self.hover_elevation:.1f}m"
                self.setToolTip(tooltip_text)
                
                # Notify map to show position
                if self.on_hover_callback and self.hover_x is not None and self.hover_y is not None:
                    self.on_hover_callback(self.hover_x, self.hover_y)
            else:
                self.hover_distance = None
                self.hover_elevation = None
                self.hover_x = None
                self.hover_y = None
                self.setToolTip("")
        else:
            self.hover_distance = None
            self.hover_elevation = None
            self.hover_x = None
            self.hover_y = None
            self.setToolTip("")
        
        self.update()
    
    def leaveEvent(self, event):
        """Clear hover state when mouse leaves widget"""
        self.hover_distance = None
        self.hover_elevation = None
        self.hover_x = None
        self.hover_y = None
        self.setToolTip("")
        # Clear map marker
        if self.on_hover_callback:
            self.on_hover_callback(None, None)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        self.draw_chart(painter, self.width(), self.height())

    def set_profile_color(self, color: QColor):
        try:
            self.profile_color = QColor(color)
        except Exception:
            self.profile_color = QColor(0, 100, 255)
        self.update()

    def set_highlight_ranges(self, ranges: Sequence[Tuple[float, float]], *, label: str = ""):
        cleaned: List[Tuple[float, float]] = []
        try:
            for a, b in ranges or []:
                try:
                    a = float(a)
                    b = float(b)
                except Exception:
                    continue
                if not math.isfinite(a) or not math.isfinite(b):
                    continue
                if b < a:
                    a, b = b, a
                if b <= a:
                    continue
                cleaned.append((a, b))
        except Exception:
            cleaned = []
        cleaned.sort(key=lambda t: t[0])
        self.highlight_ranges = cleaned
        self.highlight_label = str(label or "")
        self.update()

    def set_overlay_ranges(
        self,
        ranges: Sequence[Tuple[float, float]],
        *,
        label: str = "",
        color: Optional[QColor] = None,
    ):
        cleaned: List[Tuple[float, float]] = []
        try:
            for a, b in ranges or []:
                try:
                    a = float(a)
                    b = float(b)
                except Exception:
                    continue
                if not math.isfinite(a) or not math.isfinite(b):
                    continue
                if b < a:
                    a, b = b, a
                if b <= a:
                    continue
                cleaned.append((a, b))
        except Exception:
            cleaned = []
        cleaned.sort(key=lambda t: t[0])
        self.overlay_ranges = cleaned
        self.overlay_label = str(label or "")
        if color is not None:
            try:
                self.overlay_color = QColor(color)
            except Exception:
                pass
        self.update()

    def set_overlay_markers(self, markers: Sequence[Tuple[float, str]], *, color: Optional[QColor] = None):
        cleaned: List[Tuple[float, str]] = []
        try:
            for d, lbl in markers or []:
                try:
                    d = float(d)
                except Exception:
                    continue
                if not math.isfinite(d):
                    continue
                cleaned.append((d, str(lbl or "")))
        except Exception:
            cleaned = []
        cleaned.sort(key=lambda t: t[0])
        self.overlay_markers = cleaned
        if color is not None:
            try:
                self.overlay_marker_color = QColor(color)
            except Exception:
                pass
        self.update()

    def draw_chart(self, painter, width, height):
        if not self.data:
            painter.drawText(QRectF(0, 0, width, height), Qt.AlignCenter, "데이터가 없습니다.")
            return

        painter.setRenderHint(QPainter.Antialiasing)
        
        # Margins
        left, top, right, bottom = self.margin_left, self.margin_top, self.margin_right, self.margin_bottom
        w = width - left - right
        h = height - top - bottom
        
        # Calculate visible range based on zoom
        visible_range = self.total_d / self.zoom_level
        view_start = self.pan_offset
        view_end = view_start + visible_range
        
        # Fill background
        painter.fillRect(0, 0, width, height, QColor(255, 255, 255))

        # Draw background/grid
        painter.setPen(QPen(QColor(220, 220, 220), 1, Qt.DashLine))
        num_grids_y = 5
        for i in range(num_grids_y + 1):
            y = top + h - (i / num_grids_y) * h
            painter.drawLine(left, int(y), left + w, int(y))
            val = self.min_e + (i / num_grids_y) * (self.max_e - self.min_e)
            painter.drawText(5, int(y + 5), f"{val:.1f}m")
            
        num_grids_x = 5
        for i in range(num_grids_x + 1):
            x = left + (i / num_grids_x) * w
            painter.drawLine(int(x), top, int(x), top + h)
            dist = view_start + (i / num_grids_x) * visible_range
            painter.drawText(int(x - 15), top + h + 20, f"{dist:.0f}m")

        # Draw axis
        painter.setPen(QPen(Qt.black, 2))
        painter.drawLine(left, top, left, top + h)            # Y axis
        painter.drawLine(left, top + h, left + w, top + h)    # X axis
        
        # Highlight ranges (behind the profile line)
        if self.highlight_ranges:
            try:
                painter.save()
                painter.setPen(Qt.NoPen)
                painter.setBrush(QBrush(self.highlight_color))
                for d0, d1 in self.highlight_ranges:
                    if d1 <= view_start or d0 >= view_end:
                        continue
                    a = max(float(d0), float(view_start))
                    b = min(float(d1), float(view_end))
                    if b <= a:
                        continue
                    x0 = left + ((a - view_start) / visible_range) * w
                    x1 = left + ((b - view_start) / visible_range) * w
                    painter.drawRect(QRectF(x0, top, max(0.0, x1 - x0), h))
                if self.highlight_label:
                    painter.setPen(QPen(QColor(80, 80, 80)))
                    painter.drawText(left + 6, top + 18, self.highlight_label)
                painter.restore()
            except Exception:
                try:
                    painter.restore()
                except Exception:
                    pass

        # Overlay ranges (behind the profile line, below hover/markers)
        if self.overlay_ranges:
            try:
                painter.save()
                painter.setPen(Qt.NoPen)
                painter.setBrush(QBrush(self.overlay_color))
                for d0, d1 in self.overlay_ranges:
                    if d1 <= view_start or d0 >= view_end:
                        continue
                    a = max(float(d0), float(view_start))
                    b = min(float(d1), float(view_end))
                    if b <= a:
                        continue
                    x0 = left + ((a - view_start) / visible_range) * w
                    x1 = left + ((b - view_start) / visible_range) * w
                    painter.drawRect(QRectF(x0, top, max(0.0, x1 - x0), h))
                if self.overlay_label:
                    painter.setPen(QPen(QColor(60, 120, 60)))
                    # Avoid overlapping with AOI label (which is drawn at top+18)
                    painter.drawText(left + 6, top + 34, self.overlay_label)
                painter.restore()
            except Exception:
                try:
                    painter.restore()
                except Exception:
                    pass

        # Draw Profile Line using QPainterPath for smoothness
        path = QPainterPath()
        first_point = True
        
        for p in self.smooth_data:
            dist = p['distance']
            if dist < view_start or dist > view_end:
                continue
            
            px = left + ((dist - view_start) / visible_range) * w
            py = top + h - ((p['elevation'] - self.min_e) / (self.max_e - self.min_e)) * h
            
            if first_point:
                path.moveTo(px, py)
                first_point = False
            else:
                path.lineTo(px, py)
            
        # Draw the line
        painter.setPen(QPen(self.profile_color, 2))
        painter.drawPath(path)
             
        # Draw Fill (area below profile)
        painter.setOpacity(0.15)
        painter.setBrush(QBrush(self.profile_color))
        painter.setPen(Qt.NoPen)
        
        if not first_point:  # Only if we drew something
            fill_path = QPainterPath(path)
            # Find last drawn point
            visible_data = [p for p in self.smooth_data if view_start <= p['distance'] <= view_end]
            if visible_data:
                last_p = visible_data[-1]
                first_p = visible_data[0]
                end_x = left + ((last_p['distance'] - view_start) / visible_range) * w
                start_x = left + ((first_p['distance'] - view_start) / visible_range) * w
                fill_path.lineTo(end_x, top + h)
                fill_path.lineTo(start_x, top + h)
                fill_path.closeSubpath()
                painter.drawPath(fill_path)
        
        painter.setOpacity(1.0)

        # Overlay markers (on top of profile line)
        if self.overlay_markers:
            try:
                show_labels = len(self.overlay_markers) <= 12
                painter.save()
                if self.overlay_label and not self.overlay_ranges:
                    try:
                        painter.setPen(QPen(QColor(60, 120, 60)))
                        painter.drawText(left + 6, top + 34, self.overlay_label)
                    except Exception:
                        pass
                painter.setPen(QPen(self.overlay_marker_color, 1, Qt.DashLine))
                for dist, lbl in self.overlay_markers:
                    if dist < view_start or dist > view_end:
                        continue
                    x = left + ((dist - view_start) / visible_range) * w

                    # Vertical reference line
                    painter.drawLine(int(x), top, int(x), top + h)

                    # Marker on profile (nearest smoothed point)
                    try:
                        closest = min(self.smooth_data, key=lambda p: abs(float(p["distance"]) - float(dist)))
                        elev = float(closest.get("elevation"))
                        if math.isfinite(elev):
                            y = top + h - ((elev - self.min_e) / (self.max_e - self.min_e)) * h
                            painter.setPen(QPen(self.overlay_marker_color, 2))
                            painter.setBrush(QBrush(QColor(255, 255, 255)))
                            painter.drawEllipse(QPointF(x, y), 4, 4)
                    except Exception:
                        pass

                    if show_labels and lbl:
                        painter.setPen(QPen(QColor(30, 30, 30)))
                        painter.drawText(int(x) + 4, top + 14, lbl)
                painter.restore()
            except Exception:
                try:
                    painter.restore()
                except Exception:
                    pass
         
        # Draw hover indicator
        if self.hover_distance is not None and view_start <= self.hover_distance <= view_end:
            hover_x = left + ((self.hover_distance - view_start) / visible_range) * w
            hover_y = top + h - ((self.hover_elevation - self.min_e) / (self.max_e - self.min_e)) * h
            
            # Vertical line
            painter.setPen(QPen(QColor(255, 0, 0, 150), 1, Qt.DashLine))
            painter.drawLine(int(hover_x), top, int(hover_x), top + h)
            
            # Point marker
            painter.setPen(QPen(QColor(255, 0, 0), 2))
            painter.setBrush(QBrush(QColor(255, 255, 255)))
            painter.drawEllipse(QPointF(hover_x, hover_y), 5, 5)
            
            # Info label
            painter.setPen(QPen(Qt.black))
            info_text = f"{self.hover_distance:.1f}m / {self.hover_elevation:.1f}m"
            painter.drawText(int(hover_x) + 8, int(hover_y) - 5, info_text)
        
        # Zoom indicator
        if self.zoom_level > 1.0:
            painter.setPen(QPen(Qt.darkGray))
            painter.drawText(width - 80, 20, f"확대: {self.zoom_level:.1f}x")

    def save_to_image(self, path):
        # Create image with higher resolution for better quality
        img_w, img_h = 1200, 800
        image = QImage(img_w, img_h, QImage.Format_RGB32)
        image.fill(Qt.white)
        
        # Temporarily reset zoom for saving
        old_zoom = self.zoom_level
        old_pan = self.pan_offset
        self.zoom_level = 1.0
        self.pan_offset = 0
        
        painter = QPainter(image)
        self.draw_chart(painter, img_w, img_h)
        painter.end()
        
        # Restore zoom
        self.zoom_level = old_zoom
        self.pan_offset = old_pan
        
        ext = os.path.splitext(str(path or ""))[1].lower()
        if ext == ".png":
            return image.save(path, "PNG")
        return image.save(path, "JPG", 95)


class TerrainProfileDialog(QtWidgets.QDialog, FORM_CLASS):
     
    def __init__(self, iface, parent=None):
        super(TerrainProfileDialog, self).__init__(parent)
        self.setupUi(self)
        self.iface = iface
        self.canvas = iface.mapCanvas()
        
        # Custom Chart Widget
        self.chart = ProfileChartWidget()
        # Insert chart into layout (replace placeholder or add to vertical layout)
        # We named the layout chartLayout in UI
        self.chartLayout.insertWidget(0, self.chart)
        
        # Profile data
        self.points = []
        self.profile_data = []

        # Persistent profile layer (multi-profile support)
        self._profile_layer = None
        self._profile_layer_id = None
        self._ignore_selection_changed = False
        self._last_selected_fid = None
        self._ignore_current_layer_changed = False
        self._layer_tree_view = None
        self._single_layers_enabled = True

        # Optional: overlay another vector layer on the profile chart.
        self._overlay_layer = None
        self._overlay_selection_handler = None

        # Setup
        self.cmbDemLayer.setFilters(QgsMapLayerProxyModel.RasterLayer)

        # Extra options: fixed-length profile line + AOI highlight on chart
        self._last_profile_length_m: Optional[float] = None
        self._last_aoi_inside_m: Optional[float] = None
        try:
            self.grpExtra = QtWidgets.QGroupBox("추가 옵션 (길이/AOI)", self)
            grid_extra = QtWidgets.QGridLayout(self.grpExtra)

            self.chkFixedLength = QtWidgets.QCheckBox("같은 길이로 단면선(고정 길이)", self.grpExtra)
            self.chkFixedLength.setChecked(False)
            self.chkFixedLength.setToolTip(
                "첫 점 이후 두 번째 클릭은 '방향'만 결정하고, 길이는 고정 길이(m)로 맞춥니다.\n"
                "비교 단면(같은 길이/같은 샘플 수)을 여러 개 만들 때 유용합니다."
            )

            self.spinFixedLength = QtWidgets.QDoubleSpinBox(self.grpExtra)
            self.spinFixedLength.setDecimals(1)
            self.spinFixedLength.setMinimum(0.0)
            self.spinFixedLength.setMaximum(10_000_000.0)
            self.spinFixedLength.setSingleStep(100.0)
            self.spinFixedLength.setValue(0.0)
            self.spinFixedLength.setSuffix(" m")
            self.spinFixedLength.setEnabled(False)
            self.spinFixedLength.setToolTip("고정 길이(m). 0이면 적용되지 않습니다.")

            self.btnUseLastLength = QtWidgets.QPushButton("최근 길이", self.grpExtra)
            self.btnUseLastLength.setEnabled(False)
            self.btnUseLastLength.setToolTip("가장 최근에 만든 단면선 길이를 고정 길이에 적용합니다.")

            self.cmbAoiLayer = QgsMapLayerComboBox(self.grpExtra)
            self.cmbAoiLayer.setFilters(QgsMapLayerProxyModel.VectorLayer)
            self.cmbAoiLayer.setToolTip(
                "조사대상지(AOI) 폴리곤 레이어를 선택하세요.\n"
                "- 선택 피처가 있으면 선택 피처만 사용합니다.\n"
                "- 단면 그래프에 AOI 내부 구간을 음영으로 표시할 수 있습니다."
            )

            self.chkShowAoiOnProfile = QtWidgets.QCheckBox("단면 그래프에 조사대상지(AOI) 구간 표시", self.grpExtra)
            self.chkShowAoiOnProfile.setChecked(True)
            self.chkShowAoiOnProfile.setToolTip(
                "단면선이 AOI 내부를 지나는 구간을 그래프 배경(음영)으로 표시합니다.\n"
                "표시는 샘플링 점 기준으로 계산됩니다(샘플 수가 높을수록 경계가 정밀)."
            )

            grid_extra.addWidget(self.chkFixedLength, 0, 0, 1, 3)
            grid_extra.addWidget(QtWidgets.QLabel("고정 길이"), 1, 0)
            grid_extra.addWidget(self.spinFixedLength, 1, 1)
            grid_extra.addWidget(self.btnUseLastLength, 1, 2)
            grid_extra.addWidget(QtWidgets.QLabel("조사대상지(AOI)"), 2, 0)
            grid_extra.addWidget(self.cmbAoiLayer, 2, 1, 1, 2)
            grid_extra.addWidget(self.chkShowAoiOnProfile, 3, 0, 1, 3)

            self.chkSegmentStats = QtWidgets.QCheckBox("구간 통계(경사/누적상승) 계산", self.grpExtra)
            self.chkSegmentStats.setChecked(True)
            self.chkSegmentStats.setToolTip(
                "단면 프로파일에서 다음 통계를 계산합니다.\n"
                "- 구간별 평균 경사(예: 0–200m)\n"
                "- 누적 상승/하강\n"
                "CSV 저장 시 구간 요약표도 함께 저장됩니다."
            )

            self.spinSegmentLength = QtWidgets.QDoubleSpinBox(self.grpExtra)
            self.spinSegmentLength.setDecimals(0)
            self.spinSegmentLength.setMinimum(0.0)
            self.spinSegmentLength.setMaximum(1_000_000.0)
            self.spinSegmentLength.setSingleStep(50.0)
            self.spinSegmentLength.setValue(200.0)
            self.spinSegmentLength.setSuffix(" m")
            self.spinSegmentLength.setToolTip(
                "구간 통계에 사용할 거리 간격(m).\n"
                "예: 200m -> 0–200m, 200–400m ... 구간별 평균 경사.\n"
                "0이면 구간 통계를 계산하지 않습니다."
            )

            grid_extra.addWidget(QtWidgets.QLabel("구간 길이"), 4, 0)
            grid_extra.addWidget(self.spinSegmentLength, 4, 1)
            grid_extra.addWidget(self.chkSegmentStats, 4, 2)

            help_lbl = QtWidgets.QLabel(
                "TIP: value 비교용으로 같은 길이 단면을 만들거나,\n"
                "AOI 단면이라면 그래프에서 AOI 구간(배경 음영)을 확인할 수 있습니다.",
                self.grpExtra,
            )
            help_lbl.setWordWrap(True)
            help_lbl.setStyleSheet("color:#555;")
            grid_extra.addWidget(help_lbl, 5, 0, 1, 3)

            try:
                idx = int(self.verticalLayout.indexOf(self.groupProfile))
                if idx >= 0:
                    self.verticalLayout.insertWidget(idx, self.grpExtra)
                else:
                    self.verticalLayout.insertWidget(3, self.grpExtra)
            except Exception:
                self.verticalLayout.insertWidget(3, self.grpExtra)

            self.chkFixedLength.toggled.connect(self._update_fixed_length_ui)
            self.btnUseLastLength.clicked.connect(self._use_last_length)
            self.chkShowAoiOnProfile.toggled.connect(self._refresh_aoi_highlight)
            self.cmbAoiLayer.layerChanged.connect(self._refresh_aoi_highlight)
            try:
                self.chkSegmentStats.toggled.connect(self.update_stats)
                self.spinSegmentLength.valueChanged.connect(self.update_stats)
            except Exception:
                pass
        except Exception:
            self.grpExtra = None
            self.chkFixedLength = None
            self.spinFixedLength = None
            self.btnUseLastLength = None
            self.cmbAoiLayer = None
            self.chkShowAoiOnProfile = None
            self.chkSegmentStats = None
            self.spinSegmentLength = None

        # Optional: show a selected vector layer on the profile chart (intersection/inside).
        try:
            self.grpOverlay = QtWidgets.QGroupBox("단면 오버레이 (레이어 표시)", self)
            grid_ov = QtWidgets.QGridLayout(self.grpOverlay)

            self.chkShowOverlayOnProfile = QtWidgets.QCheckBox("단면 그래프에 레이어 표시", self.grpOverlay)
            self.chkShowOverlayOnProfile.setChecked(False)
            self.chkShowOverlayOnProfile.setToolTip(
                "선택한 벡터 레이어를 단면 그래프에 표시합니다.\n"
                "- 면(폴리곤): 단면선이 내부를 지나는 구간을 배경(음영)으로 표시\n"
                "- 점/선: 단면선과 교차(또는 근접)하는 지점을 마커로 표시"
            )

            self.cmbOverlayLayer = QgsMapLayerComboBox(self.grpOverlay)
            self.cmbOverlayLayer.setFilters(QgsMapLayerProxyModel.VectorLayer)
            self.cmbOverlayLayer.setToolTip("단면에 표시할 벡터 레이어를 선택하세요.")

            self.chkOverlaySelectedOnly = QtWidgets.QCheckBox("선택 피처만 사용", self.grpOverlay)
            self.chkOverlaySelectedOnly.setChecked(False)
            self.chkOverlaySelectedOnly.setToolTip("레이어에서 선택한 피처만 단면 표시 대상으로 사용합니다.")

            help_lbl_ov = QtWidgets.QLabel(
                "TIP: 점/선 레이어는 교차지점 마커로, 폴리곤 레이어는 내부 구간 음영으로 표시됩니다.",
                self.grpOverlay,
            )
            help_lbl_ov.setWordWrap(True)
            help_lbl_ov.setStyleSheet("color:#555;")

            grid_ov.addWidget(self.chkShowOverlayOnProfile, 0, 0, 1, 3)
            grid_ov.addWidget(QtWidgets.QLabel("레이어"), 1, 0)
            grid_ov.addWidget(self.cmbOverlayLayer, 1, 1, 1, 2)
            grid_ov.addWidget(self.chkOverlaySelectedOnly, 2, 0, 1, 3)
            grid_ov.addWidget(help_lbl_ov, 3, 0, 1, 3)

            try:
                idx = int(self.verticalLayout.indexOf(self.groupProfile))
                if idx >= 0:
                    self.verticalLayout.insertWidget(idx, self.grpOverlay)
                else:
                    self.verticalLayout.insertWidget(3, self.grpOverlay)
            except Exception:
                self.verticalLayout.insertWidget(3, self.grpOverlay)

            self.chkShowOverlayOnProfile.toggled.connect(self._refresh_overlay)
            self.chkOverlaySelectedOnly.toggled.connect(self._refresh_overlay)
            self.cmbOverlayLayer.layerChanged.connect(self._on_overlay_layer_changed)
        except Exception:
            self.grpOverlay = None
            self.chkShowOverlayOnProfile = None
            self.cmbOverlayLayer = None
            self.chkOverlaySelectedOnly = None
        
        # Connect signals
        self.btnDrawLine.clicked.connect(self.start_drawing)
        self.btnClear.clicked.connect(self.clear_profile)
        self.btnExportCsv.clicked.connect(self.export_csv)
        self.btnExportImage.clicked.connect(self.export_image)
        self.btnClose.clicked.connect(self.cleanup_and_close)

        try:
            self.btnClear.setText("현재 초기화")
            self.btnClear.setToolTip("현재 그래프/임시 표시만 초기화합니다. 저장된 단면선 레이어는 유지됩니다.")
        except Exception:
            pass
        try:
            self.label_Header.setToolTip(
                "팁: 저장된 단면선 레이어에서 선을 '선택'하면 해당 단면이 자동으로 열립니다.\n"
                f"- 레이어 이름: {PROFILE_LAYER_NAME}"
            )
        except Exception:
            pass

        # Optional: create a per-profile layer as well (so users can click a layer to reopen).
        try:
            self.chkSingleLayers = QtWidgets.QCheckBox("개별 레이어도 생성", self)
            self.chkSingleLayers.setObjectName("chkSingleLayers")
            self.chkSingleLayers.setChecked(True)
            self.chkSingleLayers.setToolTip(
                "단면선을 '1개=1개 레이어'로도 추가합니다.\n"
                "레이어 패널에서 해당 레이어를 클릭(현재 레이어)하면 단면 그래프가 자동으로 열립니다.\n"
                "많이 생성하면 레이어가 많아질 수 있어 필요할 때만 켜세요."
            )
            try:
                idx = int(self.horizontalLayout.indexOf(self.btnDrawLine))
                if idx >= 0:
                    self.horizontalLayout.insertWidget(idx, self.chkSingleLayers)
                else:
                    self.horizontalLayout.addWidget(self.chkSingleLayers)
            except Exception:
                self.horizontalLayout.addWidget(self.chkSingleLayers)

            def _sync_single_layers(on: bool):
                self._single_layers_enabled = bool(on)

            self.chkSingleLayers.toggled.connect(_sync_single_layers)
            self._single_layers_enabled = bool(self.chkSingleLayers.isChecked())
        except Exception:
            self.chkSingleLayers = None
            self._single_layers_enabled = True

        # Layer panel click → open profile (for per-profile layers).
        try:
            view = self.iface.layerTreeView()
            view.currentLayerChanged.connect(self._on_current_layer_changed)
            self._layer_tree_view = view
        except Exception:
            self._layer_tree_view = None
        
        # Rubber band for drawing line
        self.rubber_band = QgsRubberBand(self.canvas, QgsWkbTypes.LineGeometry)
        self.rubber_band.setColor(QColor(255, 0, 0))
        self.rubber_band.setWidth(2)
        
        # Hover marker for showing position on map
        self.hover_marker = QgsRubberBand(self.canvas, QgsWkbTypes.PointGeometry)
        self.hover_marker.setColor(QColor(255, 0, 0))
        self.hover_marker.setWidth(10)
        self.hover_marker.setIcon(QgsRubberBand.ICON_CIRCLE)
        
        # Connect chart hover callback
        self.chart.on_hover_callback = self.show_position_on_map
        
        # Map tool
        self.map_tool = None
        self.original_tool = None

        # If the profile layer already exists in the project, hook selection to open profiles.
        try:
            layers = QgsProject.instance().mapLayersByName(PROFILE_LAYER_NAME)
            if layers:
                self._ensure_profile_layer_schema(layers[0])
                self._connect_profile_layer(layers[0])
        except Exception:
            pass
    
    def show_position_on_map(self, x, y):
        """Show hover position on map"""
        if x is None or y is None:
            self.hover_marker.reset(QgsWkbTypes.PointGeometry)
            try:
                self.hover_marker.hide()
            except Exception:
                pass
        else:
            self.hover_marker.reset(QgsWkbTypes.PointGeometry)
            self.hover_marker.addPoint(QgsPointXY(x, y))
            try:
                self.hover_marker.show()
            except Exception:
                pass

    def _show_profile_line_on_map(self, *, start: QgsPointXY, end: QgsPointXY, color: Optional[QColor] = None):
        """Show the current profile line as a rubber band so users can see where it was drawn."""
        if start is None or end is None:
            return
        try:
            self.rubber_band.reset(QgsWkbTypes.LineGeometry)
        except Exception:
            self.rubber_band.reset()
        try:
            self.rubber_band.addPoint(QgsPointXY(start))
            self.rubber_band.addPoint(QgsPointXY(end))
        except Exception:
            return
        try:
            self.rubber_band.setColor(QColor(color) if color is not None else QColor(255, 0, 0))
        except Exception:
            pass
        try:
            self.rubber_band.setWidth(3)
        except Exception:
            pass
        try:
            self.rubber_band.show()
        except Exception:
            pass

    def _update_fixed_length_ui(self):
        enabled = False
        try:
            enabled = bool(self.chkFixedLength is not None and self.chkFixedLength.isChecked())
        except Exception:
            enabled = False

        try:
            if self.spinFixedLength is not None:
                self.spinFixedLength.setEnabled(enabled)
        except Exception:
            pass

        try:
            if enabled and self.spinFixedLength is not None:
                cur = float(self.spinFixedLength.value())
                if cur <= 0 and self._last_profile_length_m is not None and math.isfinite(self._last_profile_length_m):
                    self.spinFixedLength.setValue(float(self._last_profile_length_m))
        except Exception:
            pass

        try:
            if self.btnUseLastLength is not None:
                can_use = (
                    enabled
                    and self._last_profile_length_m is not None
                    and math.isfinite(self._last_profile_length_m)
                    and self._last_profile_length_m > 0
                )
                self.btnUseLastLength.setEnabled(bool(can_use))
        except Exception:
            pass

    def _use_last_length(self):
        try:
            if self._last_profile_length_m is None or not math.isfinite(self._last_profile_length_m):
                return
            if self.spinFixedLength is not None:
                self.spinFixedLength.setValue(float(self._last_profile_length_m))
            if self.chkFixedLength is not None:
                self.chkFixedLength.setChecked(True)
        except Exception:
            pass

    def _fixed_length_m(self) -> Optional[float]:
        try:
            if self.chkFixedLength is None or not self.chkFixedLength.isChecked():
                return None
            if self.spinFixedLength is None:
                return None
            v = float(self.spinFixedLength.value())
            if math.isfinite(v) and v > 0:
                return v
        except Exception:
            pass
        return None

    def _distance_area_canvas(self) -> QgsDistanceArea:
        canvas_crs = self.canvas.mapSettings().destinationCrs()
        distance_area = QgsDistanceArea()
        distance_area.setSourceCrs(canvas_crs, QgsProject.instance().transformContext())
        distance_area.setEllipsoid(QgsProject.instance().ellipsoid() or "WGS84")
        try:
            distance_area.setEllipsoidalMode(True)
        except AttributeError:
            pass
        return distance_area

    def _end_point_fixed_length(self, *, start: QgsPointXY, direction_point: QgsPointXY, length_m: float) -> QgsPointXY:
        dx = float(direction_point.x() - start.x())
        dy = float(direction_point.y() - start.y())
        if dx == 0.0 and dy == 0.0:
            return QgsPointXY(direction_point)

        try:
            distance_area = self._distance_area_canvas()
            cur = float(distance_area.measureLine(start, direction_point))
        except Exception:
            cur = 0.0

        if not math.isfinite(cur) or cur <= 0:
            # Fallback to planar scaling (best effort)
            norm = math.sqrt(dx * dx + dy * dy)
            if norm <= 0:
                return QgsPointXY(direction_point)
            scale = float(length_m) / norm
            return QgsPointXY(start.x() + dx * scale, start.y() + dy * scale)

        scale_total = float(length_m) / cur
        end = QgsPointXY(start.x() + dx * scale_total, start.y() + dy * scale_total)
        # Refine a couple of times (useful in geographic CRS where degrees->meters is nonlinear)
        for _ in range(2):
            try:
                cur2 = float(distance_area.measureLine(start, end))
            except Exception:
                break
            if not math.isfinite(cur2) or cur2 <= 0:
                break
            scale_total *= float(length_m) / cur2
            end = QgsPointXY(start.x() + dx * scale_total, start.y() + dy * scale_total)
        return end

    def update_preview(self, point: QgsPointXY):
        """Update temporary rubber band preview while drawing the profile line."""
        try:
            if len(self.points) != 1:
                return
            start = self.points[0]
            end = QgsPointXY(point)
            fixed = self._fixed_length_m()
            if fixed is not None:
                end = self._end_point_fixed_length(start=start, direction_point=end, length_m=fixed)
            self.rubber_band.reset(QgsWkbTypes.LineGeometry)
            self.rubber_band.addPoint(start)
            self.rubber_band.addPoint(end)
            self.rubber_band.show()
        except Exception:
            pass

    def _compute_aoi_highlight_ranges(self) -> List[Tuple[float, float]]:
        """Return AOI intersection ranges along distance axis using current profile_data (sample-based)."""
        self._last_aoi_inside_m = None
        if not self.profile_data:
            return []
        try:
            if self.chkShowAoiOnProfile is None or not self.chkShowAoiOnProfile.isChecked():
                return []
        except Exception:
            return []

        aoi_layer = None
        try:
            aoi_layer = self.cmbAoiLayer.currentLayer() if self.cmbAoiLayer is not None else None
        except Exception:
            aoi_layer = None
        if aoi_layer is None or not isinstance(aoi_layer, QgsVectorLayer):
            return []
        try:
            if aoi_layer.geometryType() != QgsWkbTypes.PolygonGeometry:
                return []
        except Exception:
            return []

        feats = []
        try:
            feats = aoi_layer.selectedFeatures()
        except Exception:
            feats = []
        if not feats:
            try:
                feats = list(aoi_layer.getFeatures())
            except Exception:
                feats = []
        if not feats:
            return []

        geoms = []
        for ft in feats:
            try:
                g = ft.geometry()
                if g is None or g.isEmpty():
                    continue
                geoms.append(g)
            except Exception:
                continue
        if not geoms:
            return []

        aoi_geom = None
        try:
            aoi_geom = QgsGeometry.unaryUnion(geoms)
        except Exception:
            aoi_geom = None
        if aoi_geom is None or aoi_geom.isEmpty():
            try:
                g0 = None
                for g in geoms:
                    g0 = g if g0 is None else g0.combine(g)
                aoi_geom = g0
            except Exception:
                aoi_geom = None
        if aoi_geom is None or aoi_geom.isEmpty():
            return []

        # Transform AOI geometry to canvas CRS once.
        try:
            canvas_crs = self.canvas.mapSettings().destinationCrs()
            aoi_crs = aoi_layer.crs()
            if aoi_crs != canvas_crs:
                ct = QgsCoordinateTransform(aoi_crs, canvas_crs, QgsProject.instance())
                aoi_geom = QgsGeometry(aoi_geom)  # copy
                aoi_geom.transform(ct)
        except Exception:
            return []

        ranges: List[Tuple[float, float]] = []
        run_start = None
        last_inside = None

        for p in self.profile_data:
            try:
                dist = float(p.get("distance", 0.0))
            except Exception:
                dist = 0.0
            inside = False
            try:
                x = float(p.get("x"))
                y = float(p.get("y"))
                pt_geom = QgsGeometry.fromPointXY(QgsPointXY(x, y))
                inside = bool(aoi_geom.intersects(pt_geom))
            except Exception:
                inside = False

            if inside:
                if run_start is None:
                    run_start = dist
                last_inside = dist
            else:
                if run_start is not None:
                    end = last_inside if last_inside is not None else dist
                    if end > run_start:
                        ranges.append((run_start, end))
                    run_start = None
                    last_inside = None

        if run_start is not None:
            end = last_inside if last_inside is not None else run_start
            if end > run_start:
                ranges.append((run_start, end))

        inside_len = 0.0
        try:
            inside_len = float(sum((b - a) for a, b in ranges if b > a))
        except Exception:
            inside_len = 0.0
        if math.isfinite(inside_len) and inside_len > 0:
            self._last_aoi_inside_m = inside_len

        return ranges

    def _refresh_aoi_highlight(self, *_args):
        """Recompute AOI highlight ranges for the current profile (if any)."""
        try:
            if not self.profile_data:
                self._last_aoi_inside_m = None
                try:
                    self.chart.set_highlight_ranges([], label="")
                except Exception:
                    pass
                return

            ranges = self._compute_aoi_highlight_ranges()
            label = ""
            if ranges:
                try:
                    lyr = self.cmbAoiLayer.currentLayer() if self.cmbAoiLayer is not None else None
                    label = f"AOI: {lyr.name()}" if lyr is not None else "AOI"
                except Exception:
                    label = "AOI"
            try:
                self.chart.set_highlight_ranges(ranges, label=label)
            except Exception:
                pass
            try:
                self.update_stats()
            except Exception:
                pass
        except Exception:
            pass

    def _on_overlay_layer_changed(self, layer):
        """Reconnect selectionChanged handler for the overlay layer and refresh overlay."""
        # Disconnect previous handler
        try:
            if self._overlay_layer is not None and self._overlay_selection_handler is not None:
                self._overlay_layer.selectionChanged.disconnect(self._overlay_selection_handler)
        except Exception:
            pass

        self._overlay_layer = layer if isinstance(layer, QgsVectorLayer) else None
        self._overlay_selection_handler = None

        if self._overlay_layer is not None:
            try:
                handler = lambda *_args: self._refresh_overlay()
                self._overlay_selection_handler = handler
                self._overlay_layer.selectionChanged.connect(handler)
            except Exception:
                self._overlay_selection_handler = None

        self._refresh_overlay()

    def _refresh_overlay(self, *_args):
        """Recompute overlay (ranges/markers) for the current profile and update the chart."""
        try:
            if not hasattr(self, "chart") or self.chart is None:
                return

            # Clear overlay if disabled or no profile
            try:
                enabled = bool(self.chkShowOverlayOnProfile is not None and self.chkShowOverlayOnProfile.isChecked())
            except Exception:
                enabled = False

            if (not enabled) or (not self.profile_data):
                try:
                    self.chart.set_overlay_ranges([], label="")
                except Exception:
                    pass
                try:
                    self.chart.set_overlay_markers([])
                except Exception:
                    pass
                return

            layer = None
            try:
                layer = self.cmbOverlayLayer.currentLayer() if self.cmbOverlayLayer is not None else None
            except Exception:
                layer = None
            if layer is None or not isinstance(layer, QgsVectorLayer) or not layer.isValid():
                try:
                    self.chart.set_overlay_ranges([], label="")
                except Exception:
                    pass
                try:
                    self.chart.set_overlay_markers([])
                except Exception:
                    pass
                return

            # Profile endpoints (canvas CRS)
            start_canvas = None
            end_canvas = None
            try:
                if len(self.points) >= 2:
                    start_canvas = QgsPointXY(self.points[0])
                    end_canvas = QgsPointXY(self.points[1])
            except Exception:
                start_canvas = None
                end_canvas = None

            if start_canvas is None or end_canvas is None:
                try:
                    start_canvas = QgsPointXY(float(self.profile_data[0]["x"]), float(self.profile_data[0]["y"]))
                    end_canvas = QgsPointXY(float(self.profile_data[-1]["x"]), float(self.profile_data[-1]["y"]))
                except Exception:
                    start_canvas = None
                    end_canvas = None

            if start_canvas is None or end_canvas is None:
                try:
                    self.chart.set_overlay_ranges([], label="")
                except Exception:
                    pass
                try:
                    self.chart.set_overlay_markers([])
                except Exception:
                    pass
                return

            # Total distance (meters) consistent with profile chart.
            total_distance_m = None
            try:
                if self._last_profile_length_m is not None and math.isfinite(self._last_profile_length_m):
                    total_distance_m = float(self._last_profile_length_m)
            except Exception:
                total_distance_m = None
            if total_distance_m is None or not math.isfinite(total_distance_m) or total_distance_m <= 0:
                try:
                    total_distance_m = float(self._distance_area_canvas().measureLine(start_canvas, end_canvas))
                except Exception:
                    total_distance_m = None
            if total_distance_m is None or not math.isfinite(total_distance_m) or total_distance_m <= 0:
                return

            vx = float(end_canvas.x() - start_canvas.x())
            vy = float(end_canvas.y() - start_canvas.y())
            vv = float(vx * vx + vy * vy)
            if vv <= 0:
                return

            def _fraction_for_xy(x: float, y: float) -> float:
                try:
                    t = ((float(x) - float(start_canvas.x())) * vx + (float(y) - float(start_canvas.y())) * vy) / vv
                except Exception:
                    t = 0.0
                if t < 0.0:
                    return 0.0
                if t > 1.0:
                    return 1.0
                return float(t)

            canvas_crs = self.canvas.mapSettings().destinationCrs()
            layer_crs = None
            try:
                layer_crs = layer.crs()
            except Exception:
                layer_crs = canvas_crs

            ct = None
            ct_inv = None
            try:
                if layer_crs != canvas_crs:
                    ct = QgsCoordinateTransform(layer_crs, canvas_crs, QgsProject.instance())
                    ct_inv = QgsCoordinateTransform(canvas_crs, layer_crs, QgsProject.instance())
            except Exception:
                ct = None
                ct_inv = None

            line_geom = QgsGeometry.fromPolylineXY([start_canvas, end_canvas])

            # Pixel-based tolerance (map units) for near-misses (mostly for point layers).
            tol_mu = 0.0
            try:
                tol_mu = float(self.canvas.mapUnitsPerPixel()) * 8.0
            except Exception:
                tol_mu = 0.0
            if not math.isfinite(tol_mu) or tol_mu < 0:
                tol_mu = 0.0

            selected_only = False
            try:
                selected_only = bool(self.chkOverlaySelectedOnly is not None and self.chkOverlaySelectedOnly.isChecked())
            except Exception:
                selected_only = False

            # Iterate candidate features (best-effort bbox filter when not using selected features).
            feats_iter = None
            if selected_only:
                try:
                    feats_iter = layer.selectedFeatures()
                except Exception:
                    feats_iter = []
            else:
                try:
                    rect = line_geom.boundingBox()
                    if tol_mu > 0:
                        rect.grow(tol_mu * 2.0)
                    if ct_inv is not None:
                        rect = ct_inv.transformBoundingBox(rect)
                    feats_iter = layer.getFeatures(QgsFeatureRequest().setFilterRect(rect))
                except Exception:
                    feats_iter = layer.getFeatures()

            # Best-effort feature label
            def _feature_label(ft: QgsFeature) -> str:
                try:
                    for name in ("name", "label", "title", "id"):
                        idx = layer.fields().indexFromName(name)
                        if idx >= 0:
                            v = ft.attribute(name)
                            if v is not None and str(v).strip():
                                return str(v).strip()
                except Exception:
                    pass
                try:
                    for fld in layer.fields():
                        if fld.type() == QVariant.String:
                            v = ft.attribute(fld.name())
                            if v is not None and str(v).strip():
                                return str(v).strip()
                except Exception:
                    pass
                try:
                    return str(int(ft.id()))
                except Exception:
                    return ""

            markers: List[Tuple[float, str]] = []
            ranges: List[Tuple[float, float]] = []

            geom_type = None
            try:
                geom_type = int(layer.geometryType())
            except Exception:
                geom_type = None

            for ft in feats_iter:
                try:
                    g = ft.geometry()
                except Exception:
                    g = None
                if g is None or g.isEmpty():
                    continue

                try:
                    g2 = QgsGeometry(g)
                    if ct is not None:
                        g2.transform(ct)
                except Exception:
                    continue

                # Polygon: inside-segments on the profile line
                if geom_type == QgsWkbTypes.PolygonGeometry:
                    try:
                        inter = g2.intersection(line_geom)
                    except Exception:
                        inter = None
                    if inter is None or inter.isEmpty():
                        continue

                    # Segment ranges (line geometry)
                    try:
                        if inter.type() == QgsWkbTypes.LineGeometry:
                            segs = []
                            if inter.isMultipart():
                                segs = inter.asMultiPolyline() or []
                            else:
                                seg = inter.asPolyline() or []
                                if seg:
                                    segs = [seg]
                            for seg in segs:
                                if not seg or len(seg) < 2:
                                    continue
                                t_vals = []
                                for p in (seg[0], seg[-1]):
                                    try:
                                        t_vals.append(_fraction_for_xy(float(p.x()), float(p.y())))
                                    except Exception:
                                        continue
                                if len(t_vals) < 2:
                                    continue
                                a = min(t_vals) * total_distance_m
                                b = max(t_vals) * total_distance_m
                                if math.isfinite(a) and math.isfinite(b) and b > a:
                                    ranges.append((a, b))
                        elif inter.type() == QgsWkbTypes.PointGeometry:
                            pts = []
                            if inter.isMultipart():
                                pts = inter.asMultiPoint() or []
                            else:
                                pts = [inter.asPoint()]
                            for p in pts:
                                try:
                                    t = _fraction_for_xy(float(p.x()), float(p.y()))
                                    d = t * total_distance_m
                                    if math.isfinite(d):
                                        markers.append((d, _feature_label(ft)))
                                except Exception:
                                    continue
                    except Exception:
                        continue
                    continue

                # Lines: intersection points (or overlap segments)
                if geom_type == QgsWkbTypes.LineGeometry:
                    try:
                        inter = g2.intersection(line_geom)
                    except Exception:
                        inter = None
                    if inter is None or inter.isEmpty():
                        continue
                    lbl = _feature_label(ft)
                    try:
                        if inter.type() == QgsWkbTypes.PointGeometry:
                            pts = []
                            if inter.isMultipart():
                                pts = inter.asMultiPoint() or []
                            else:
                                pts = [inter.asPoint()]
                            for p in pts:
                                try:
                                    t = _fraction_for_xy(float(p.x()), float(p.y()))
                                    d = t * total_distance_m
                                    if math.isfinite(d):
                                        markers.append((d, lbl))
                                except Exception:
                                    continue
                        elif inter.type() == QgsWkbTypes.LineGeometry:
                            segs = []
                            if inter.isMultipart():
                                segs = inter.asMultiPolyline() or []
                            else:
                                seg = inter.asPolyline() or []
                                if seg:
                                    segs = [seg]
                            for seg in segs:
                                if not seg or len(seg) < 2:
                                    continue
                                try:
                                    t0 = _fraction_for_xy(float(seg[0].x()), float(seg[0].y()))
                                    t1 = _fraction_for_xy(float(seg[-1].x()), float(seg[-1].y()))
                                    d0 = min(t0, t1) * total_distance_m
                                    d1 = max(t0, t1) * total_distance_m
                                    if math.isfinite(d0) and math.isfinite(d1) and d1 > d0:
                                        markers.append((d0, lbl))
                                        markers.append((d1, lbl))
                                except Exception:
                                    continue
                    except Exception:
                        continue
                    continue

                # Points: near the profile line (tolerance)
                if geom_type == QgsWkbTypes.PointGeometry:
                    lbl = _feature_label(ft)
                    try:
                        pts = []
                        if g2.isMultipart():
                            pts = g2.asMultiPoint() or []
                        else:
                            pts = [g2.asPoint()]
                        for p in pts:
                            try:
                                pg = QgsGeometry.fromPointXY(QgsPointXY(p))
                                if tol_mu > 0:
                                    if float(line_geom.distance(pg)) > float(tol_mu):
                                        continue
                                else:
                                    if float(line_geom.distance(pg)) > 0.0:
                                        continue
                                t = _fraction_for_xy(float(p.x()), float(p.y()))
                                d = t * total_distance_m
                                if math.isfinite(d):
                                    markers.append((d, lbl))
                            except Exception:
                                continue
                    except Exception:
                        continue

            # Merge overlapping ranges
            merged_ranges: List[Tuple[float, float]] = []
            try:
                ranges.sort(key=lambda t: t[0])
                for a, b in ranges:
                    if not merged_ranges:
                        merged_ranges.append((a, b))
                        continue
                    pa, pb = merged_ranges[-1]
                    if a <= pb + 0.5:
                        merged_ranges[-1] = (pa, max(pb, b))
                    else:
                        merged_ranges.append((a, b))
            except Exception:
                merged_ranges = ranges

            # Merge close markers
            merged_markers: List[Tuple[float, str]] = []
            try:
                markers.sort(key=lambda t: t[0])
                for d, lbl in markers:
                    if not merged_markers:
                        merged_markers.append((d, lbl))
                        continue
                    pd, pl = merged_markers[-1]
                    if abs(float(d) - float(pd)) <= 0.5:
                        # Keep the first label; avoid exploding labels.
                        continue
                    merged_markers.append((d, lbl))
            except Exception:
                merged_markers = markers

            layer_label = ""
            try:
                layer_label = f"레이어: {layer.name()}"
            except Exception:
                layer_label = "레이어"

            try:
                self.chart.set_overlay_ranges(merged_ranges, label=layer_label)
            except Exception:
                pass
            try:
                self.chart.set_overlay_markers(merged_markers)
            except Exception:
                pass
        except Exception:
            pass
    
    def start_drawing(self):
        """Start drawing profile line on map"""
        dem_layer = self.cmbDemLayer.currentLayer()
        if not dem_layer:
            push_message(self.iface, "오류", "DEM 래스터를 선택해주세요", level=2)
            restore_ui_focus(self)
            return
        self.points = []
        self.rubber_band.reset()
        try:
            # Always start drawing with a clear, visible preview style.
            self.rubber_band.setColor(QColor(255, 0, 0))
            self.rubber_band.setWidth(2)
        except Exception:
            pass
        
        # Save original tool and set our tool
        self.original_tool = self.canvas.mapTool()
        self.map_tool = ProfileLineTool(self.canvas, self)
        self.canvas.setMapTool(self.map_tool)

        fixed = self._fixed_length_m()
        if fixed is not None:
            push_message(
                self.iface,
                "지형 단면",
                f"고정 길이 {fixed:.0f}m: 시작점을 클릭한 뒤, 방향만 클릭하세요 (2번)",
                level=0,
            )
        else:
            push_message(self.iface, "지형 단면", "지도에서 시작점과 끝점을 클릭하세요 (2번)", level=0)
        self.hide()
    
    def add_point(self, point):
        """Add point to profile line"""
        if point is None:
            return
        if len(self.points) >= 2:
            return

        if len(self.points) == 0:
            self.points.append(QgsPointXY(point))
            try:
                self.rubber_band.reset(QgsWkbTypes.LineGeometry)
            except Exception:
                self.rubber_band.reset()
            self.rubber_band.addPoint(self.points[0])
            self.rubber_band.show()
            return

        # Second click: apply fixed length (direction-only click) if enabled.
        start = self.points[0]
        end = QgsPointXY(point)
        fixed = self._fixed_length_m()
        if fixed is not None:
            end = self._end_point_fixed_length(start=start, direction_point=end, length_m=fixed)

        self.points = [start, end]
        self.rubber_band.reset(QgsWkbTypes.LineGeometry)
        self.rubber_band.addPoint(start)
        self.rubber_band.addPoint(end)
        self.rubber_band.show()
        self.calculate_profile()
    
    def calculate_profile(self):
        dem_layer = self.cmbDemLayer.currentLayer()
        if not dem_layer or len(self.points) < 2:
            push_message(self.iface, "오류", "DEM 레이어가 선택되지 않았거나 점이 부족합니다.", level=2)
            restore_ui_focus(self)
            return

        # Live log window (non-modal) so users can see progress in real time.
        ensure_live_log_dialog(self.iface, owner=self, show=True, clear=True)
        try:
            start_canvas = self.points[0]
            end_canvas = self.points[1]
            num_samples = int(self.spinSamples.value())
            if num_samples <= 0:
                push_message(self.iface, "오류", "샘플 수는 1 이상이어야 합니다.", level=2)
                restore_ui_focus(self)
                return

            canvas_crs = self.canvas.mapSettings().destinationCrs()
            dem_crs = dem_layer.crs()
            
            self.profile_data = []

            # Always measure in meters (ellipsoidal) so geographic CRS projects don't break stats/exports.
            distance_area = QgsDistanceArea()
            distance_area.setSourceCrs(canvas_crs, QgsProject.instance().transformContext())
            distance_area.setEllipsoid(QgsProject.instance().ellipsoid() or "WGS84")
            try:
                distance_area.setEllipsoidalMode(True)
            except AttributeError:
                pass
            total_distance_m = float(distance_area.measureLine(start_canvas, end_canvas))
            try:
                self._last_profile_length_m = float(total_distance_m)
                self._update_fixed_length_ui()
            except Exception:
                pass
            
            push_message(
                self.iface,
                "단면 분석",
                f"시작점에서 끝점까지 {total_distance_m:.1f}m, {num_samples}개 샘플 추출 중...",
                level=0,
            )
            
            valid_samples = 0
            for i in range(num_samples + 1):
                fraction = i / num_samples
                x_canvas = start_canvas.x() + fraction * (end_canvas.x() - start_canvas.x())
                y_canvas = start_canvas.y() + fraction * (end_canvas.y() - start_canvas.y())
                sample_canvas = QgsPointXY(x_canvas, y_canvas)

                # Identify expects coordinates in DEM CRS.
                sample_dem = transform_point(sample_canvas, canvas_crs, dem_crs)
                
                result = dem_layer.dataProvider().identify(
                    sample_dem,
                    QgsRaster.IdentifyFormatValue
                )
                
                if result.isValid():
                    # Try band 1 first, then any available band
                    results_dict = result.results()
                    value = results_dict.get(1, None)
                    if value is None and results_dict:
                        # Fallback: get first available band value
                        value = list(results_dict.values())[0]
                    
                    if value is not None and value != dem_layer.dataProvider().sourceNoDataValue(1):
                        dist = fraction * total_distance_m
                        self.profile_data.append({
                            'distance': dist,
                            'elevation': float(value),
                            'x': x_canvas,
                            'y': y_canvas
                        })
                        valid_samples += 1
            
            if self.profile_data:
                # Save line to persistent layer first (assigns a per-profile color).
                profile_color = None
                try:
                    profile_color = self.save_line_to_layer(total_distance_m, dem_layer=dem_layer, num_samples=num_samples)
                except Exception:
                    profile_color = None

                if profile_color is not None:
                    try:
                        self.chart.set_profile_color(profile_color)
                    except Exception:
                        pass

                self.chart.set_data(self.profile_data)
                self._refresh_aoi_highlight()
                self._refresh_overlay()
                self.btnExportCsv.setEnabled(True)
                self.btnExportImage.setEnabled(True)
                try:
                    self._show_profile_line_on_map(start=start_canvas, end=end_canvas, color=profile_color)
                except Exception:
                    pass
                
                push_message(self.iface, "단면 완료", f"{valid_samples}개 유효 샘플 추출 완료!", level=0)
            else:
                push_message(self.iface, "경고", "유효한 고도 데이터를 추출하지 못했습니다. DEM 범위를 확인하세요.", level=1)
            
        except Exception as e:
            push_message(self.iface, "오류", f"계산 실패: {str(e)}", level=2)
        finally:
            if self.original_tool:
                self.canvas.setMapTool(self.original_tool)
            restore_ui_focus(self)

    def _connect_profile_layer(self, layer: QgsVectorLayer):
        if layer is None or not isinstance(layer, QgsVectorLayer):
            return

        try:
            if self._profile_layer_id == layer.id():
                return
        except Exception:
            pass

        # Best-effort disconnect previous.
        try:
            if self._profile_layer is not None:
                self._profile_layer.selectionChanged.disconnect(self._on_profile_layer_selection_changed)
        except Exception:
            pass

        self._profile_layer = layer
        try:
            self._profile_layer_id = layer.id()
        except Exception:
            self._profile_layer_id = None

        try:
            layer.selectionChanged.connect(self._on_profile_layer_selection_changed)
        except Exception:
            pass

    def _on_current_layer_changed(self, layer):
        if self._ignore_current_layer_changed:
            return
        if layer is None:
            return
        if not isinstance(layer, QgsVectorLayer):
            return
        try:
            kind = str(layer.customProperty(PROFILE_KIND_PROP, "") or "")
        except Exception:
            kind = ""
        if kind != PROFILE_KIND_SINGLE:
            return

        try:
            ft = None
            for f in layer.getFeatures(QgsFeatureRequest().setLimit(1)):
                ft = f
                break
            if ft is None:
                return
            self._open_profile_from_feature(layer, ft)
        except Exception as e:
            log_message(f"TerrainProfile: open from layer click failed: {e}", level=Qgis.Warning)

    def _on_profile_layer_selection_changed(self, *_args):
        if self._ignore_selection_changed:
            return
        layer = self._profile_layer
        if layer is None:
            return
        try:
            feats = layer.selectedFeatures()
        except Exception:
            feats = []
        if not feats:
            return

        ft = feats[0]
        try:
            fid = int(ft.id())
        except Exception:
            fid = None
        if fid is not None and fid == self._last_selected_fid:
            return
        self._last_selected_fid = fid

        try:
            self._open_profile_from_feature(layer, ft)
        except Exception as e:
            log_message(f"TerrainProfile: open from selection failed: {e}", level=Qgis.Warning)

    def _open_profile_from_feature(self, layer: QgsVectorLayer, ft: QgsFeature):
        """Recompute and show profile when a saved profile line is selected."""
        dem_layer = None
        try:
            dem_id = ft.attribute("dem_id")
            if dem_id:
                dem_layer = QgsProject.instance().mapLayer(str(dem_id))
        except Exception:
            dem_layer = None

        if dem_layer is None:
            dem_layer = self.cmbDemLayer.currentLayer()
        if dem_layer is None:
            push_message(self.iface, "오류", "프로파일을 열 DEM을 선택해주세요.", level=2)
            restore_ui_focus(self)
            return

        try:
            num_samples = int(ft.attribute("samples") or 0)
        except Exception:
            num_samples = 0
        if num_samples <= 0:
            num_samples = int(self.spinSamples.value())

        # Color (optional)
        try:
            r = int(ft.attribute("r"))
            g = int(ft.attribute("g"))
            b = int(ft.attribute("b"))
            self.chart.set_profile_color(QColor(r, g, b))
        except Exception:
            pass

        geom = ft.geometry()
        if geom is None or geom.isEmpty():
            return

        try:
            line_crs = layer.crs()
        except Exception:
            line_crs = self.canvas.mapSettings().destinationCrs()
        canvas_crs = self.canvas.mapSettings().destinationCrs()

        # Extract endpoints
        pts = None
        try:
            if geom.isMultipart():
                mp = geom.asMultiPolyline()
                if mp and mp[0]:
                    pts = mp[0]
            else:
                pts = geom.asPolyline()
        except Exception:
            pts = None
        if not pts or len(pts) < 2:
            return

        start_line = QgsPointXY(pts[0])
        end_line = QgsPointXY(pts[-1])
        start_canvas = transform_point(start_line, line_crs, canvas_crs)
        end_canvas = transform_point(end_line, line_crs, canvas_crs)

        self.points = [start_canvas, end_canvas]
        try:
            # Keep an on-top visual cue even when the line already exists in a layer.
            self._show_profile_line_on_map(start=start_canvas, end=end_canvas)
        except Exception:
            pass
        self._compute_profile_for_points(dem_layer=dem_layer, start_canvas=start_canvas, end_canvas=end_canvas, num_samples=num_samples)
        restore_ui_focus(self)

    def _compute_profile_for_points(self, *, dem_layer, start_canvas: QgsPointXY, end_canvas: QgsPointXY, num_samples: int):
        if dem_layer is None:
            return
        if num_samples <= 0:
            num_samples = 200

        ensure_live_log_dialog(self.iface, owner=self, show=True, clear=True)

        canvas_crs = self.canvas.mapSettings().destinationCrs()
        dem_crs = dem_layer.crs()

        self.profile_data = []

        distance_area = QgsDistanceArea()
        distance_area.setSourceCrs(canvas_crs, QgsProject.instance().transformContext())
        distance_area.setEllipsoid(QgsProject.instance().ellipsoid() or "WGS84")
        try:
            distance_area.setEllipsoidalMode(True)
        except AttributeError:
            pass

        total_distance_m = float(distance_area.measureLine(start_canvas, end_canvas))
        try:
            self._last_profile_length_m = float(total_distance_m)
            self._update_fixed_length_ui()
        except Exception:
            pass

        push_message(
            self.iface,
            "단면 분석",
            f"선택한 단면선 {total_distance_m:.1f}m, {num_samples}개 샘플 추출 중...",
            level=0,
        )

        valid_samples = 0
        for i in range(num_samples + 1):
            fraction = i / num_samples
            x_canvas = start_canvas.x() + fraction * (end_canvas.x() - start_canvas.x())
            y_canvas = start_canvas.y() + fraction * (end_canvas.y() - start_canvas.y())
            sample_canvas = QgsPointXY(x_canvas, y_canvas)

            sample_dem = transform_point(sample_canvas, canvas_crs, dem_crs)
            result = dem_layer.dataProvider().identify(sample_dem, QgsRaster.IdentifyFormatValue)
            if not result.isValid():
                continue
            results_dict = result.results()
            value = results_dict.get(1, None)
            if value is None and results_dict:
                value = list(results_dict.values())[0]
            if value is None:
                continue
            try:
                if value == dem_layer.dataProvider().sourceNoDataValue(1):
                    continue
            except Exception:
                pass
            try:
                elev = float(value)
            except Exception:
                continue
            dist = fraction * total_distance_m
            self.profile_data.append({"distance": dist, "elevation": elev, "x": x_canvas, "y": y_canvas})
            valid_samples += 1

        if self.profile_data:
            self.chart.set_data(self.profile_data)
            self._refresh_aoi_highlight()
            self._refresh_overlay()
            self.btnExportCsv.setEnabled(True)
            self.btnExportImage.setEnabled(True)
            try:
                self._show_profile_line_on_map(start=start_canvas, end=end_canvas)
            except Exception:
                pass
            push_message(self.iface, "단면 완료", f"{valid_samples}개 유효 샘플 추출 완료!", level=0)
        else:
            push_message(self.iface, "경고", "유효한 고도 데이터를 추출하지 못했습니다. DEM 범위를 확인하세요.", level=1)

    def _ensure_profile_layer_schema(self, layer: QgsVectorLayer):
        """Ensure older projects' profile layers have the fields/style needed for multi-profile viewing."""
        if layer is None or not isinstance(layer, QgsVectorLayer):
            return

        pr = layer.dataProvider()
        required = [
            QgsField("no", QVariant.Int),
            QgsField("distance", QVariant.Double, "m", 10, 2),
            QgsField("min_elev", QVariant.Double, "m", 10, 2),
            QgsField("max_elev", QVariant.Double, "m", 10, 2),
            QgsField("date", QVariant.String),
            QgsField("dem_id", QVariant.String),
            QgsField("samples", QVariant.Int),
            QgsField("r", QVariant.Int),
            QgsField("g", QVariant.Int),
            QgsField("b", QVariant.Int),
        ]

        missing = []
        for f in required:
            try:
                if layer.fields().indexFromName(f.name()) < 0:
                    missing.append(f)
            except Exception:
                missing.append(f)

        if missing:
            try:
                pr.addAttributes(missing)
                layer.updateFields()
            except Exception:
                pass

        # If the layer was created before we had per-feature colors, populate r/g/b for existing features.
        try:
            idx_r = layer.fields().indexFromName("r")
            idx_g = layer.fields().indexFromName("g")
            idx_b = layer.fields().indexFromName("b")
            if idx_r >= 0 and idx_g >= 0 and idx_b >= 0:
                palette = _profile_color_palette()
                if palette:
                    changes = {}
                    for ft in layer.getFeatures():
                        try:
                            r0 = ft.attribute("r")
                            g0 = ft.attribute("g")
                            b0 = ft.attribute("b")
                        except Exception:
                            r0 = g0 = b0 = None
                        has_color = False
                        try:
                            has_color = (r0 is not None) and (g0 is not None) and (b0 is not None)
                        except Exception:
                            has_color = False
                        if has_color:
                            continue
                        try:
                            no = int(ft.attribute("no") or 0)
                        except Exception:
                            no = 0
                        if no <= 0:
                            try:
                                no = int(ft.id()) + 1
                            except Exception:
                                no = 1
                        c = palette[(no - 1) % len(palette)]
                        changes[int(ft.id())] = {
                            idx_r: int(c.red()),
                            idx_g: int(c.green()),
                            idx_b: int(c.blue()),
                        }
                    if changes:
                        pr.changeAttributeValues(changes)
                        layer.triggerRepaint()
        except Exception:
            pass

        # Ensure renderer uses per-feature colors when possible.
        try:
            if layer.fields().indexFromName("r") >= 0:
                symbol = QgsLineSymbol.createSimple({'color': '0,0,0,200', 'width': '1.4'})
                sl = symbol.symbolLayer(0)
                if sl is not None:
                    sl.setDataDefinedProperty(
                        QgsSymbolLayer.PropertyStrokeColor,
                        QgsProperty.fromExpression('color_rgba("r","g","b",220)'),
                    )
                layer.setRenderer(QgsSingleSymbolRenderer(symbol))
                layer.triggerRepaint()
        except Exception:
            pass

    def _ensure_single_group(self):
        project = QgsProject.instance()
        root = project.layerTreeRoot()
        group = root.findGroup(PROFILE_GROUP_NAME)
        if group is None:
            group = root.insertGroup(0, PROFILE_GROUP_NAME)
        sub = group.findGroup(PROFILE_SINGLE_SUBGROUP_NAME)
        if sub is None:
            try:
                sub = group.insertGroup(0, PROFILE_SINGLE_SUBGROUP_NAME)
            except Exception:
                sub = group.addGroup(PROFILE_SINGLE_SUBGROUP_NAME)
            try:
                sub.setExpanded(False)
            except Exception:
                pass
        return sub

    def _create_single_profile_layer(
        self,
        *,
        no: int,
        total_distance: float,
        min_elev: float,
        max_elev: float,
        start: QgsPointXY,
        end: QgsPointXY,
        dem_layer,
        num_samples: int,
        color: QColor,
    ):
        """Create a '1 profile = 1 layer' line layer so users can click the layer to reopen the chart."""
        if not self._single_layers_enabled:
            return None

        crs = self.canvas.mapSettings().destinationCrs().authid()
        name = f"단면선_{int(no):03d} ({float(total_distance):.0f}m)"
        layer = QgsVectorLayer(f"LineString?crs={crs}", name, "memory")
        pr = layer.dataProvider()
        pr.addAttributes(
            [
                QgsField("no", QVariant.Int),
                QgsField("distance", QVariant.Double, "m", 10, 2),
                QgsField("min_elev", QVariant.Double, "m", 10, 2),
                QgsField("max_elev", QVariant.Double, "m", 10, 2),
                QgsField("date", QVariant.String),
                QgsField("dem_id", QVariant.String),
                QgsField("samples", QVariant.Int),
                QgsField("r", QVariant.Int),
                QgsField("g", QVariant.Int),
                QgsField("b", QVariant.Int),
            ]
        )
        layer.updateFields()

        f = QgsFeature(layer.fields())
        f.setGeometry(QgsGeometry.fromPolylineXY([start, end]))
        f.setAttributes(
            [
                int(no),
                float(total_distance),
                float(min_elev),
                float(max_elev),
                datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
                (dem_layer.id() if dem_layer is not None else ""),
                int(num_samples or 0),
                int(color.red()),
                int(color.green()),
                int(color.blue()),
            ]
        )
        pr.addFeatures([f])
        layer.updateExtents()

        # Fixed color for this layer.
        try:
            symbol = QgsLineSymbol.createSimple(
                {
                    "color": f"{int(color.red())},{int(color.green())},{int(color.blue())},220",
                    "width": "1.6",
                }
            )
            layer.setRenderer(QgsSingleSymbolRenderer(symbol))
        except Exception:
            pass

        try:
            layer.setCustomProperty(PROFILE_KIND_PROP, PROFILE_KIND_SINGLE)
        except Exception:
            pass

        project = QgsProject.instance()
        sub = self._ensure_single_group()

        try:
            self._ignore_current_layer_changed = True
            project.addMapLayer(layer, False)
            sub.insertLayer(0, layer)
        finally:
            self._ignore_current_layer_changed = False

        return layer

    def get_or_create_profile_layer(self):
        """Get or create a memory layer to store profile lines"""
        layers = QgsProject.instance().mapLayersByName(PROFILE_LAYER_NAME)
        
        if layers:
            layer = layers[0]
            try:
                self._ensure_profile_layer_schema(layer)
                self._connect_profile_layer(layer)
            except Exception:
                pass
            return layer
        
        # Create new memory layer
        crs = self.canvas.mapSettings().destinationCrs().authid()
        layer = QgsVectorLayer(f"LineString?crs={crs}", PROFILE_LAYER_NAME, "memory")
        
        # Add fields
        pr = layer.dataProvider()
        pr.addAttributes([
            QgsField("no", QVariant.Int),
            QgsField("distance", QVariant.Double, "m", 10, 2),
            QgsField("min_elev", QVariant.Double, "m", 10, 2),
            QgsField("max_elev", QVariant.Double, "m", 10, 2),
            QgsField("date", QVariant.String),
            QgsField("dem_id", QVariant.String),
            QgsField("samples", QVariant.Int),
            QgsField("r", QVariant.Int),
            QgsField("g", QVariant.Int),
            QgsField("b", QVariant.Int),
        ])
        layer.updateFields()

        symbol = QgsLineSymbol.createSimple({'color': '0,0,0,200', 'width': '1.4'})
        try:
            sl = symbol.symbolLayer(0)
            if sl is not None:
                sl.setDataDefinedProperty(
                    QgsSymbolLayer.PropertyStrokeColor,
                    QgsProperty.fromExpression('color_rgba("r","g","b",220)'),
                )
        except Exception:
            pass
        layer.setRenderer(QgsSingleSymbolRenderer(symbol))

        project = QgsProject.instance()
        root = project.layerTreeRoot()
        group = root.findGroup(PROFILE_GROUP_NAME)
        if group is None:
            group = root.insertGroup(0, PROFILE_GROUP_NAME)
        project.addMapLayer(layer, False)
        group.insertLayer(0, layer)

        try:
            # Keep group near top
            if group.parent() == root:
                idx = root.children().index(group)
                if idx != 0:
                    root.removeChildNode(group)
                    root.insertChildNode(0, group)
        except Exception:
            pass

        try:
            self._ensure_profile_layer_schema(layer)
            self._connect_profile_layer(layer)
        except Exception:
            pass
        return layer

    def save_line_to_layer(self, total_distance, *, dem_layer=None, num_samples: int = 0) -> Optional[QColor]:
        """Save the profile line to the memory layer"""
        layer = self.get_or_create_profile_layer()
        if not layer: return

        try:
            self._connect_profile_layer(layer)
        except Exception:
            pass

        elevs = [p['elevation'] for p in self.profile_data]

        next_no = int(layer.featureCount()) + 1
        palette = _profile_color_palette()
        color = palette[(next_no - 1) % len(palette)] if palette else QColor(0, 100, 255)
        
        feat = QgsFeature(layer.fields())
        feat.setGeometry(QgsGeometry.fromPolylineXY([self.points[0], self.points[1]]))
        feat.setAttributes([
            next_no,
            total_distance,
            min(elevs),
            max(elevs),
            datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
            (dem_layer.id() if dem_layer is not None else ""),
            int(num_samples or 0),
            int(color.red()),
            int(color.green()),
            int(color.blue()),
        ])
        
        layer.dataProvider().addFeatures([feat])
        layer.updateExtents()
        layer.triggerRepaint()

        # Highlight the newly added line so users can immediately see which line matches the current chart.
        try:
            self._ignore_selection_changed = True
            try:
                layer.removeSelection()
            except Exception:
                pass
            try:
                layer.selectByExpression(f"\"no\" = {int(next_no)}")
            except Exception:
                pass
        finally:
            self._ignore_selection_changed = False

        # Optional: also create a dedicated layer for this profile (so users can click the layer to reopen).
        try:
            self._create_single_profile_layer(
                no=int(next_no),
                total_distance=float(total_distance),
                min_elev=float(min(elevs)),
                max_elev=float(max(elevs)),
                start=self.points[0],
                end=self.points[1],
                dem_layer=dem_layer,
                num_samples=int(num_samples or 0),
                color=color,
            )
        except Exception:
            pass

        return color
    
    def update_stats(self):
        if not self.profile_data: return
        
        elevs = [float(p['elevation']) for p in self.profile_data]
        dists = [float(p['distance']) for p in self.profile_data]
        total_d = float(dists[-1]) if dists else 0.0
        min_e = min(elevs)
        max_e = max(elevs)

        # Derived metrics: slope (%), cumulative ascent/descent
        ascent = 0.0
        descent = 0.0
        max_abs_slope = 0.0
        for i in range(1, len(dists)):
            dd = float(dists[i]) - float(dists[i - 1])
            dz = float(elevs[i]) - float(elevs[i - 1])
            if dz > 0:
                ascent += dz
            else:
                descent += -dz
            if dd > 1e-9:
                s = abs(dz / dd) * 100.0
                if s > max_abs_slope:
                    max_abs_slope = s

        mean_abs_slope = ((ascent + descent) / total_d * 100.0) if total_d > 1e-9 else 0.0

        stats = (
            f"총 거리: {total_d:.1f}m | 고도 범위: {min_e:.1f}m ~ {max_e:.1f}m (차: {max_e-min_e:.1f}m)"
            f" | 누적상승: {ascent:.1f}m | 누적하강: {descent:.1f}m"
            f" | 평균경사(|%|): {mean_abs_slope:.1f}% | 최대경사(|%|): {max_abs_slope:.1f}%"
        )

        # Segment stats (example: 0–200m 평균경사)
        try:
            seg_len = float(self.spinSegmentLength.value()) if getattr(self, "spinSegmentLength", None) is not None else 0.0
            seg_enabled = bool(self.chkSegmentStats is not None and self.chkSegmentStats.isChecked())
        except Exception:
            seg_len = 0.0
            seg_enabled = False

        if seg_enabled and seg_len > 0 and total_d > 0 and len(dists) >= 2:
            try:
                seg0_end = min(total_d, seg_len)
                # Distance-weighted mean absolute slope for the first segment
                abs_dz_sum = 0.0
                run_sum = 0.0
                for i in range(1, len(dists)):
                    a = float(dists[i - 1])
                    b = float(dists[i])
                    if b <= 0 or a >= seg0_end:
                        continue
                    overlap_start = max(0.0, a)
                    overlap_end = min(seg0_end, b)
                    overlap = overlap_end - overlap_start
                    if overlap <= 0:
                        continue
                    dd = b - a
                    if dd <= 1e-9:
                        continue
                    dz = float(elevs[i]) - float(elevs[i - 1])
                    frac = overlap / dd
                    abs_dz_sum += abs(dz * frac)
                    run_sum += overlap
                seg0_mean_abs_slope = (abs_dz_sum / run_sum * 100.0) if run_sum > 1e-9 else 0.0
                stats += f" | 0–{seg0_end:.0f}m 평균경사: {seg0_mean_abs_slope:.1f}%"
            except Exception:
                pass

        try:
            inside = float(self._last_aoi_inside_m) if self._last_aoi_inside_m is not None else None
            if inside is not None and math.isfinite(inside) and inside > 0:
                stats += f" | AOI 구간: {inside:.1f}m"
        except Exception:
            pass
        self.lblStats.setText(stats)

    def export_csv(self):
        if not self.profile_data: return
        
        path, _ = QFileDialog.getSaveFileName(
            self, "CSV 저장", os.path.expanduser("~"), "CSV Files (*.csv)"
        )
        if not path: return
        
        try:
            with open(path, 'w', newline='', encoding='utf-8-sig') as f:
                writer = csv.writer(f)
                elevs = [float(p["elevation"]) for p in self.profile_data]
                dists = [float(p["distance"]) for p in self.profile_data]

                slopes = [0.0]
                cum_up = [0.0]
                cum_dn = [0.0]
                for i in range(1, len(dists)):
                    dd = float(dists[i]) - float(dists[i - 1])
                    dz = float(elevs[i]) - float(elevs[i - 1])
                    slopes.append((dz / dd * 100.0) if dd > 1e-9 else 0.0)
                    cum_up.append(cum_up[-1] + (dz if dz > 0 else 0.0))
                    cum_dn.append(cum_dn[-1] + ((-dz) if dz < 0 else 0.0))

                total_d = float(dists[-1]) if dists else 0.0
                min_e = float(min(elevs)) if elevs else 0.0
                max_e = float(max(elevs)) if elevs else 0.0
                ascent = float(cum_up[-1]) if cum_up else 0.0
                descent = float(cum_dn[-1]) if cum_dn else 0.0

                # Segment settings
                try:
                    seg_len = float(self.spinSegmentLength.value()) if getattr(self, "spinSegmentLength", None) is not None else 0.0
                    seg_enabled = bool(self.chkSegmentStats is not None and self.chkSegmentStats.isChecked())
                except Exception:
                    seg_len = 0.0
                    seg_enabled = False
                seg_len = seg_len if seg_enabled else 0.0

                seg_idx = []
                if seg_len > 0:
                    for d in dists:
                        try:
                            seg_idx.append(int(float(d) // float(seg_len)))
                        except Exception:
                            seg_idx.append(0)
                else:
                    seg_idx = [0 for _ in dists]

                # Summary block (key/value rows)
                writer.writerow(["metric", "value"])
                writer.writerow(["total_distance_m", round(total_d, 3)])
                writer.writerow(["min_elev_m", round(min_e, 3)])
                writer.writerow(["max_elev_m", round(max_e, 3)])
                writer.writerow(["elev_range_m", round(max_e - min_e, 3)])
                writer.writerow(["total_ascent_m", round(ascent, 3)])
                writer.writerow(["total_descent_m", round(descent, 3)])
                if seg_len > 0:
                    writer.writerow(["segment_length_m", round(seg_len, 3)])
                try:
                    inside = float(self._last_aoi_inside_m) if self._last_aoi_inside_m is not None else None
                    if inside is not None and math.isfinite(inside) and inside > 0:
                        writer.writerow(["aoi_inside_m", round(float(inside), 3)])
                except Exception:
                    pass

                writer.writerow([])

                # Per-sample table
                writer.writerow(
                    [
                        "Distance(m)",
                        "Elevation(m)",
                        "Slope(%)",
                        "CumAscent(m)",
                        "CumDescent(m)",
                        "Segment",
                        "X",
                        "Y",
                    ]
                )
                for i, p in enumerate(self.profile_data):
                    writer.writerow(
                        [
                            round(dists[i], 3),
                            round(elevs[i], 3),
                            round(slopes[i], 3),
                            round(cum_up[i], 3),
                            round(cum_dn[i], 3),
                            int(seg_idx[i]),
                            round(float(p["x"]), 6),
                            round(float(p["y"]), 6),
                        ]
                    )

                # Segment summary table
                if seg_len > 0 and len(dists) >= 2 and total_d > 0:
                    writer.writerow([])
                    writer.writerow(
                        [
                            "SegStart(m)",
                            "SegEnd(m)",
                            "Run(m)",
                            "NetSlope(%)",
                            "MeanAbsSlope(%)",
                            "Ascent(m)",
                            "Descent(m)",
                        ]
                    )
                    nseg = int(math.ceil(total_d / seg_len))
                    for sidx in range(nseg):
                        s0 = float(sidx) * float(seg_len)
                        s1 = min(total_d, float(sidx + 1) * float(seg_len))
                        run = float(s1 - s0)
                        if run <= 1e-9:
                            continue
                        net_dz = 0.0
                        abs_dz = 0.0
                        seg_up = 0.0
                        seg_dn = 0.0
                        for i in range(1, len(dists)):
                            a = float(dists[i - 1])
                            b = float(dists[i])
                            if b <= s0 or a >= s1:
                                continue
                            overlap_start = max(s0, a)
                            overlap_end = min(s1, b)
                            overlap = overlap_end - overlap_start
                            if overlap <= 0:
                                continue
                            dd = b - a
                            if dd <= 1e-9:
                                continue
                            dz = float(elevs[i]) - float(elevs[i - 1])
                            frac = overlap / dd
                            dz_seg = dz * frac
                            net_dz += dz_seg
                            abs_dz += abs(dz_seg)
                            if dz_seg > 0:
                                seg_up += dz_seg
                            else:
                                seg_dn += -dz_seg

                        net_slope = net_dz / run * 100.0
                        mean_abs_slope = abs_dz / run * 100.0
                        writer.writerow(
                            [
                                round(s0, 3),
                                round(s1, 3),
                                round(run, 3),
                                round(net_slope, 3),
                                round(mean_abs_slope, 3),
                                round(seg_up, 3),
                                round(seg_dn, 3),
                            ]
                        )
            self.iface.messageBar().pushMessage("저장 완료", f"파일: {path}", level=0)
        except Exception as e:
            QMessageBox.critical(self, "오류", f"파일 저장 실패: {str(e)}")

    def export_image(self):
        if not self.profile_data: return
        
        path, selected_filter = QFileDialog.getSaveFileName(
            self,
            "이미지 저장",
            os.path.expanduser("~"),
            "PNG Files (*.png);;JPEG Files (*.jpg)",
        )
        if not path: return
        try:
            if not os.path.splitext(path)[1]:
                if selected_filter and "PNG" in selected_filter:
                    path += ".png"
                else:
                    path += ".jpg"
        except Exception:
            pass
        
        try:
            success = self.chart.save_to_image(path)
            if success:
                self.iface.messageBar().pushMessage("저장 완료", f"이미지: {path}", level=0)
            else:
                QMessageBox.critical(self, "오류", "이미지 저장에 실패했습니다.")
        except Exception as e:
            QMessageBox.critical(self, "오류", f"이미지 저장 중 오류: {str(e)}")

    def clear_profile(self):
        self.points = []
        self.profile_data = []
        self._last_aoi_inside_m = None
        self.rubber_band.reset()
        try:
            self.rubber_band.hide()
        except Exception:
            pass
        self.hover_marker.reset(QgsWkbTypes.PointGeometry)
        try:
            self.hover_marker.hide()
        except Exception:
            pass
        self.chart.set_data([])
        try:
            self.chart.set_profile_color(QColor(0, 100, 255))
        except Exception:
            pass
        self.lblStats.setText("지도를 클릭하여 단면을 생성하세요.")
        self.btnExportCsv.setEnabled(False)
        self.btnExportImage.setEnabled(False)
    
    def cleanup_and_close(self):
        """Explicit cleanup called when Close button is clicked"""
        self._cleanup()
        self.close()
    
    def reject(self):
        """Called when ESC is pressed or dialog is rejected"""
        self._cleanup()
        super().reject()
    
    def closeEvent(self, event):
        """Clean up: remove temporary layer and map tools when dialog closes"""
        self._cleanup()
        event.accept()

    def cleanup_for_unload(self):
        """Called from plugin unload to disconnect signals safely."""
        try:
            if self._profile_layer is not None:
                self._profile_layer.selectionChanged.disconnect(self._on_profile_layer_selection_changed)
        except Exception:
            pass
        try:
            if self._layer_tree_view is not None:
                self._layer_tree_view.currentLayerChanged.disconnect(self._on_current_layer_changed)
        except Exception:
            pass
        try:
            if self._overlay_layer is not None and self._overlay_selection_handler is not None:
                self._overlay_layer.selectionChanged.disconnect(self._overlay_selection_handler)
        except Exception:
            pass
        self._profile_layer = None
        self._overlay_layer = None
        self._overlay_selection_handler = None
        self._profile_layer_id = None
        self._layer_tree_view = None
        self._cleanup()
     
    def _cleanup(self):
        """Internal cleanup method - removes rubber bands and restores map tool.

        Note: saved profile line layers are kept (multi-profile library).
        """
        try:
            # Clear rubber bands completely
            if hasattr(self, 'rubber_band') and self.rubber_band:
                self.rubber_band.reset(QgsWkbTypes.LineGeometry)
                self.rubber_band.hide()
                # Try to remove from canvas scene
                if self.canvas and self.canvas.scene():
                    try:
                        self.canvas.scene().removeItem(self.rubber_band)
                    except Exception:
                        pass
            
            if hasattr(self, 'hover_marker') and self.hover_marker:
                self.hover_marker.reset(QgsWkbTypes.PointGeometry)
                self.hover_marker.hide()
                if self.canvas and self.canvas.scene():
                    try:
                        self.canvas.scene().removeItem(self.hover_marker)
                    except Exception:
                        pass
             
            # Restore original map tool
            if hasattr(self, 'original_tool') and self.original_tool:
                try:
                    self.canvas.setMapTool(self.original_tool)
                except Exception:
                    pass
            
            # Refresh canvas
            if self.canvas:
                self.canvas.refresh()
        except Exception as e:
            log_message(f"Cleanup error: {e}", level=Qgis.Warning)


class ProfileLineTool(QgsMapToolEmitPoint):
    def __init__(self, canvas, dialog):
        super().__init__(canvas)
        self.dialog = dialog
    
    def canvasReleaseEvent(self, event):
        point = self.toMapCoordinates(event.pos())
        self.dialog.add_point(point)

    def canvasMoveEvent(self, event):
        try:
            point = self.toMapCoordinates(event.pos())
            self.dialog.update_preview(point)
        except Exception:
            pass
