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
from qgis.PyQt import uic
from qgis.PyQt import QtWidgets
from qgis.PyQt.QtCore import Qt, QPointF, QRectF, QVariant
from qgis.PyQt.QtWidgets import QMessageBox, QFileDialog, QWidget
from qgis.PyQt.QtGui import QColor, QPainter, QPen, QBrush, QFont, QPalette, QPainterPath, QImage
from qgis.core import (
    QgsProject, QgsRasterLayer, QgsMapLayerProxyModel, QgsPointXY, QgsRaster,
    QgsVectorLayer, QgsField, QgsFeature, QgsGeometry, QgsWkbTypes,
    QgsLineSymbol, QgsSingleSymbolRenderer
)
from qgis.gui import QgsMapToolEmitPoint, QgsRubberBand
from .utils import restore_ui_focus, push_message


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
        painter.setPen(QPen(QColor(0, 100, 255), 2))
        painter.drawPath(path)
            
        # Draw Fill (area below profile)
        painter.setOpacity(0.15)
        painter.setBrush(QBrush(QColor(0, 100, 255)))
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
        
        # Setup
        self.cmbDemLayer.setFilters(QgsMapLayerProxyModel.RasterLayer)
        
        # Connect signals
        self.btnDrawLine.clicked.connect(self.start_drawing)
        self.btnClear.clicked.connect(self.clear_profile)
        self.btnExportCsv.clicked.connect(self.export_csv)
        self.btnExportImage.clicked.connect(self.export_image)
        self.btnClose.clicked.connect(self.cleanup_and_close)
        
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
        try:
            start = self.points[0]
            end = self.points[1]
            num_samples = self.spinSamples.value()
            
            self.profile_data = []
            total_distance = start.distance(end)
            
            push_message(self.iface, "단면 분석", f"시작점에서 끝점까지 {total_distance:.1f}m, {num_samples}개 샘플 추출 중...", level=0)
            
            valid_samples = 0
            for i in range(num_samples + 1):
                fraction = i / num_samples
                x = start.x() + fraction * (end.x() - start.x())
                y = start.y() + fraction * (end.y() - start.y())
                
                result = dem_layer.dataProvider().identify(
                    QgsPointXY(x, y), 
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
                        dist = fraction * total_distance
                        self.profile_data.append({
                            'distance': dist,
                            'elevation': float(value),
                            'x': x,
                            'y': y
                        })
                        valid_samples += 1
            
            if self.profile_data:
                self.chart.set_data(self.profile_data)
                self.update_stats()
                self.btnExportCsv.setEnabled(True)
                self.btnExportImage.setEnabled(True)
                
                # Save line to persistent layer
                self.save_line_to_layer(total_distance)
                
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
    def get_or_create_profile_layer(self):
        """Get or create a memory layer to store profile lines"""
        layer_name = "Terrain Profile Lines"
        layers = QgsProject.instance().mapLayersByName(layer_name)
        
        if layers:
            return layers[0]
        
        # Create new memory layer
        crs = self.canvas.mapSettings().destinationCrs().authid()
        layer = QgsVectorLayer(f"LineString?crs={crs}", layer_name, "memory")
        
        # Add fields
        pr = layer.dataProvider()
        pr.addAttributes([
            QgsField("no", QVariant.Int),
            QgsField("distance", QVariant.Double, "m", 10, 2),
            QgsField("min_elev", QVariant.Double, "m", 10, 2),
            QgsField("max_elev", QVariant.Double, "m", 10, 2),
            QgsField("date", QVariant.String)
        ])
        layer.updateFields()

        symbol = QgsLineSymbol.createSimple({'color': 'red', 'width': '1.0'})
        layer.setRenderer(QgsSingleSymbolRenderer(symbol))

        QgsProject.instance().addMapLayer(layer)
        return layer

    def save_line_to_layer(self, total_distance):
        """Save the profile line to the memory layer"""
        layer = self.get_or_create_profile_layer()
        if not layer: return

        elevs = [p['elevation'] for p in self.profile_data]
        
        feat = QgsFeature(layer.fields())
        feat.setGeometry(QgsGeometry.fromPolylineXY([self.points[0], self.points[1]]))
        feat.setAttributes([
            layer.featureCount() + 1,
            total_distance,
            min(elevs),
            max(elevs),
            datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        ])
        
        layer.dataProvider().addFeatures([feat])
        layer.updateExtents()
        layer.triggerRepaint()
    
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
        self.lblStats.setText("지도를 클릭하여 단면을 생성하세요.")
        self.btnExportCsv.setEnabled(False)
        self.btnExportImage.setEnabled(False)
        
        # Also clear the persistent layer features
        layer_name = "Terrain Profile Lines"
        layers = QgsProject.instance().mapLayersByName(layer_name)
        if layers:
            layer = layers[0]
            if layer.isEditable() or layer.dataProvider().capabilities() & layer.dataProvider().DeleteFeatures:
                # Remove all features
                layer.dataProvider().truncate()
                layer.triggerRepaint()
    
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
    
    def _cleanup(self):
        """Internal cleanup method - removes rubber bands and temporary layer"""
        try:
            # Clear rubber bands completely
            if hasattr(self, 'rubber_band') and self.rubber_band:
                self.rubber_band.reset(QgsWkbTypes.LineGeometry)
                self.rubber_band.hide()
                # Try to remove from canvas scene
                if self.canvas and self.canvas.scene():
                    try:
                        self.canvas.scene().removeItem(self.rubber_band)
                    except:
                        pass
            
            if hasattr(self, 'hover_marker') and self.hover_marker:
                self.hover_marker.reset(QgsWkbTypes.PointGeometry)
                self.hover_marker.hide()
                if self.canvas and self.canvas.scene():
                    try:
                        self.canvas.scene().removeItem(self.hover_marker)
                    except:
                        pass
            
            # Remove the temporary profile layer from project
            layer_name = "Terrain Profile Lines"
            layers = QgsProject.instance().mapLayersByName(layer_name)
            for layer in layers:
                try:
                    QgsProject.instance().removeMapLayer(layer.id())
                except:
                    pass
            
            # Restore original map tool
            if hasattr(self, 'original_tool') and self.original_tool:
                try:
                    self.canvas.setMapTool(self.original_tool)
                except:
                    pass
            
            # Refresh canvas
            if self.canvas:
                self.canvas.refresh()
        except Exception as e:
            print(f"Cleanup error: {e}")


class ProfileLineTool(QgsMapToolEmitPoint):
    def __init__(self, canvas, dialog):
        super().__init__(canvas)
        self.dialog = dialog
    
    def canvasReleaseEvent(self, event):
        point = self.toMapCoordinates(event.pos())
        self.dialog.add_point(point)
