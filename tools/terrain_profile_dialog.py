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
from typing import List, Optional
from qgis.PyQt import uic
from qgis.PyQt import QtWidgets
from qgis.PyQt.QtCore import Qt, QPointF, QRectF, QVariant
from qgis.PyQt.QtWidgets import QMessageBox, QFileDialog, QWidget
from qgis.PyQt.QtGui import QColor, QPainter, QPen, QBrush, QPalette, QPainterPath, QImage
from qgis.core import (
    QgsProject, QgsMapLayerProxyModel, QgsPointXY, QgsRaster,
    QgsVectorLayer, QgsField, QgsFeature, QgsFeatureRequest, QgsGeometry, QgsWkbTypes,
    QgsLineSymbol, QgsSingleSymbolRenderer, QgsSymbolLayer, QgsProperty, Qgis, QgsDistanceArea
)
from qgis.gui import QgsMapToolEmitPoint, QgsRubberBand
from .utils import log_message, push_message, restore_ui_focus, transform_point
from .live_log_dialog import ensure_live_log_dialog

PROFILE_LAYER_NAME = "Terrain Profile Lines"
PROFILE_GROUP_NAME = "ArchToolkit - Terrain Profile"


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
        
        # Margins
        self.margin_left = 60
        self.margin_top = 30
        self.margin_right = 30
        self.margin_bottom = 40

    def set_data(self, data):
        self.data = data
        self.zoom_level = 1.0
        self.pan_offset = 0
        
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
                if self.on_hover_callback and self.hover_x and self.hover_y:
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
        
        # Setup
        self.cmbDemLayer.setFilters(QgsMapLayerProxyModel.RasterLayer)
        
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
        else:
            self.hover_marker.reset(QgsWkbTypes.PointGeometry)
            self.hover_marker.addPoint(QgsPointXY(x, y))
    
    def start_drawing(self):
        """Start drawing profile line on map"""
        dem_layer = self.cmbDemLayer.currentLayer()
        if not dem_layer:
            push_message(self.iface, "오류", "DEM 래스터를 선택해주세요", level=2)
            restore_ui_focus(self)
            return
        self.points = []
        self.rubber_band.reset()
        
        # Save original tool and set our tool
        self.original_tool = self.canvas.mapTool()
        self.map_tool = ProfileLineTool(self.canvas, self)
        self.canvas.setMapTool(self.map_tool)
        
        push_message(self.iface, "지형 단면", "지도에서 시작점과 끝점을 클릭하세요 (2번)", level=0)
        self.hide()
    
    def add_point(self, point):
        """Add point to profile line"""
        self.points.append(point)
        self.rubber_band.addPoint(point)
        
        if len(self.points) == 2:
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
                self.update_stats()
                self.btnExportCsv.setEnabled(True)
                self.btnExportImage.setEnabled(True)
                
                # Clear rubber band completely - line is now in vector layer
                self.rubber_band.reset(QgsWkbTypes.LineGeometry)
                self.rubber_band.hide()
                self.canvas.refresh()
                
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
            self.update_stats()
            self.btnExportCsv.setEnabled(True)
            self.btnExportImage.setEnabled(True)
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

        return color
    
    def update_stats(self):
        if not self.profile_data: return
        
        elevs = [p['elevation'] for p in self.profile_data]
        total_d = self.profile_data[-1]['distance']
        min_e = min(elevs)
        max_e = max(elevs)
        
        stats = (f"총 거리: {total_d:.1f}m | 고도 범위: {min_e:.1f}m ~ {max_e:.1f}m "
                 f"(차: {max_e-min_e:.1f}m)")
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
                writer.writerow(['Distance(m)', 'Elevation(m)', 'X', 'Y'])
                for p in self.profile_data:
                    writer.writerow([
                        round(p['distance'], 3), 
                        round(p['elevation'], 3), 
                        round(p['x'], 6), 
                        round(p['y'], 6)
                    ])
            self.iface.messageBar().pushMessage("저장 완료", f"파일: {path}", level=0)
        except Exception as e:
            QMessageBox.critical(self, "오류", f"파일 저장 실패: {str(e)}")

    def export_image(self):
        if not self.profile_data: return
        
        path, _ = QFileDialog.getSaveFileName(
            self, "이미지 저장", os.path.expanduser("~"), "JPEG Files (*.jpg)"
        )
        if not path: return
        
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
        self.rubber_band.reset()
        self.hover_marker.reset(QgsWkbTypes.PointGeometry)
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
        self._profile_layer = None
        self._profile_layer_id = None
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
