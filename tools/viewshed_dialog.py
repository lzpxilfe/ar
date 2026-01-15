# -*- coding: utf-8 -*-

# ArchToolkit - Archaeology Toolkit for QGIS
# Copyright (C) 2026 balguljang2
# License: GPL v3
"""
Viewshed Analysis Dialog for ArchToolkit
Visibility analysis for archaeological applications: fortifications, temples, etc.

Reference:
- Wang, J., Robinson, G. J., & White, K. (1996). A Fast Solution to Local Viewshed 
  Computation Using Grid-Based Digital Elevation Models. PERS, 62(10), 1157-1164.
"""
import os
import tempfile
import uuid
import time
import math
import shutil
import processing
import numpy as np
from osgeo import gdal
from qgis.PyQt import uic, QtWidgets, QtCore
from qgis.PyQt.QtCore import Qt, QVariant, QRectF, QPointF
from qgis.PyQt.QtGui import QColor, QPainter, QPen, QBrush, QFont, QFontMetrics, QImage, QPainterPath, QPolygonF
from qgis.PyQt.QtWidgets import QDialog, QVBoxLayout, QPushButton, QWidget, QFileDialog, QHBoxLayout, QLabel
from qgis.core import (
    QgsProject, QgsRasterLayer, QgsVectorLayer, QgsMapLayerProxyModel, QgsRectangle,
    QgsPointXY, QgsWkbTypes, QgsFeature, QgsGeometry, QgsField,
    QgsCoordinateReferenceSystem, QgsCoordinateTransform,
    QgsRasterShader, QgsColorRampShader, QgsSingleBandPseudoColorRenderer,
    QgsRasterBandStats, QgsLineSymbol, QgsRendererCategory,
    QgsCategorizedSymbolRenderer, QgsSingleSymbolRenderer, QgsPointLocator,
    QgsMarkerSymbol, QgsPalLayerSettings, QgsTextFormat, QgsVectorLayerSimpleLabeling,
    QgsTextBufferSettings, QgsTextAnnotation
)
from qgis.gui import (
    QgsMapToolEmitPoint, QgsRubberBand, QgsMapMouseEvent, 
    QgsSnapIndicator, QgsVertexMarker, QgsMapCanvasAnnotationItem
)
from qgis.PyQt.QtGui import QTextDocument

from .utils import transform_point, cleanup_files, restore_ui_focus, push_message

# Load the UI file
FORM_CLASS, _ = uic.loadUiType(os.path.join(
    os.path.dirname(__file__), 'viewshed_dialog_base.ui'))


class ViewshedDialog(QtWidgets.QDialog, FORM_CLASS):
    
    def __init__(self, iface, parent=None):
        super(ViewshedDialog, self).__init__(parent)
        self.setupUi(self)
        self.iface = iface
        self.canvas = iface.mapCanvas()
        
        # Selected observer point(s)
        self.observer_point = None
        self.target_point = None  # For Line of Sight
        self.observer_points = []  # For multi-point viewshed
        self.point_labels = []  # Text annotations for point numbers
        self.multi_point_mode = False
        self.los_mode = False
        self.buffer_mode = False
        self.los_click_count = 0
        self.last_result_layer_id = None
        self.result_marker_map = {} # layer_id -> [markers]
        self.result_annotation_map = {} # layer_id -> [annotations] [v1.6.02]
        self.result_observer_layer_map = {} # [v1.6.18] viewshed_layer_id -> observer_layer_id
        self.label_layer = None # Core reference to prevent GC issues

        
        
        # Setup layer combos
        self.cmbDemLayer.setFilters(QgsMapLayerProxyModel.Filter.RasterLayer)
        self.cmbObserverLayer.setFilters(QgsMapLayerProxyModel.Filter.VectorLayer)
        
        # Connect signals
        self.btnRun.clicked.connect(self.run_analysis)
        self.btnClose.clicked.connect(self.close)
        self.btnSelectPoint.clicked.connect(self.start_point_selection)
        
        # Auto-sync source radio when layer is selected
        self.cmbObserverLayer.layerChanged.connect(self.on_layer_selection_changed)
        
        # Listen for layer removal for marker cleanup
        QgsProject.instance().layersWillBeRemoved.connect(self.on_layers_removed)
        
        # Mode radio buttons
        self.radioSinglePoint.toggled.connect(self.on_mode_changed)
        self.radioLineViewshed.toggled.connect(self.on_mode_changed)
        self.radioReverseViewshed.toggled.connect(self.on_mode_changed)
        self.radioMultiPoint.toggled.connect(self.on_mode_changed)
        self.radioLineOfSight.toggled.connect(self.on_mode_changed)
        self.radioBufferVisibility.toggled.connect(self.on_mode_changed)
        
        # Layer source radio buttons
        self.radioClickMap.toggled.connect(self.on_source_changed)
        self.radioFromLayer.toggled.connect(self.on_source_changed)
        
        # Default to Map Click as requested
        self.radioClickMap.setChecked(True)
        
        # Map tool for point selection
        self.map_tool = None
        self.original_tool = None
        
        # Rubber band for showing selected point
        self.point_marker = QgsRubberBand(self.canvas, QgsWkbTypes.PointGeometry)
        self.point_marker.setColor(QColor(255, 0, 0))
        self.point_marker.setWidth(3)
        self.point_marker.setIconSize(8)
        self.point_marker.setIcon(QgsRubberBand.ICON_CIRCLE)
        
        # Set default colors for visibility styling
        if hasattr(self, 'btnNotVisibleColor'):
            self.btnNotVisibleColor.setColor(QColor(255, 223, 223))  # #ffdfdf
        if hasattr(self, 'btnVisibleColor'):
            self.btnVisibleColor.setColor(QColor(0, 200, 0, 180))  # Semi-transparent green
        
        # [v1.5.95] Initialize scientific context and Higuchi signals
        if hasattr(self, 'chkHiguchi'):
            self.chkHiguchi.toggled.connect(self.on_higuchi_toggled)
        
        # Programmatically update tooltips for scientific basis
        if hasattr(self, 'chkCurvature'):
            self.chkCurvature.setToolTip("지구 곡률 보정: h' = h - d²/2R (지구 반경 6,371km)")
        if hasattr(self, 'chkRefraction'):
            self.chkRefraction.setToolTip("대기 굴절 보정: 표준 계수 0.13 사용 (빛의 휘어짐 보정)\n지역 대기 조건에 따라 관측 거리가 달라질 수 있습니다.")
        
        # [v1.6.0] Add Refraction UI programmatically since we can't edit .ui easily
        # Insert a spinbox next to the refraction checkbox if possible, or in a new layout
        self.spinRefraction = QtWidgets.QDoubleSpinBox(self)
        self.spinRefraction.setRange(0.0, 1.0)
        self.spinRefraction.setSingleStep(0.01)
        self.spinRefraction.setDecimals(2)
        self.spinRefraction.setValue(0.13) # Default refraction coefficient
        self.spinRefraction.setToolTip("대기 굴절 계수 (Refraction Coefficient)\n보통 0.13 사용 (표준 대기 상태)")
        self.spinRefraction.setEnabled(self.chkRefraction.isChecked())
        
        # [v1.5.96] Correctly inject Refraction UI into QGridLayout
        if hasattr(self, 'gridLayout_2'):
            layout = self.gridLayout_2
            # Move chkRefraction to col 0 (original was colspan 2)
            layout.removeWidget(self.chkRefraction)
            layout.addWidget(self.chkRefraction, 5, 0)
            # Add spinbox to col 1
            layout.addWidget(self.spinRefraction, 5, 1)
            
            # Add scientific basis label to a new row
            self.lblScienceHelp = QtWidgets.QLabel(self)
            self.lblScienceHelp.setText("<font size='2' color='#444'><b>[근거]</b> 곡률: h'=h-d²/2R | 굴절: k=0.13(표준)</font>")
            self.lblScienceHelp.setToolTip("지구 곡률 보정(R=6,371km) 및 대기 굴절(k) 보정 공식입니다.")
            layout.addWidget(self.lblScienceHelp, 6, 0, 1, 2)
            
        self.chkRefraction.toggled.connect(self.spinRefraction.setEnabled)
        
        # [v1.5.90] Code-level UI overrides for terminology and defaults
        self.radioLineViewshed.setText("선형 및 둘레 가시권 (Line/Perimeter)")
        self.radioLineViewshed.setToolTip("선형 경로(도로, 해안선)나 성곽 둘레(Perimeter)를 따라 이동하며 보이는 영역을 분석합니다.")
        
        if hasattr(self, "spinLineMaxPoints"):
            self.spinLineMaxPoints.setValue(50)
        if hasattr(self, "spinMaxPoints"):
            self.spinMaxPoints.setValue(50)

        # [v1.6.1] Fix Maximum Distance limit to allow > 2500m
        if hasattr(self, "spinMaxDistance"):
            self.spinMaxDistance.setMaximum(999999) # Allow large analysis radius
            # Set default if needed, but respect UI default usually
        
        # [v1.6.1] Safer Refraction Widget Insertion
        # If previous insertion failed (no parent layout found), try finding thegroupBox
        if self.spinRefraction.parent() == self:
             # It means it's just floating on the dialog, which might be invisible or wrongly placed
             # Let's try to add it to 'groupParameters' layout if exists
             if hasattr(self, 'groupParameters') and self.groupParameters.layout():
                 row = self.groupParameters.layout().rowCount()
                 self.groupParameters.layout().addWidget(QLabel("대기 굴절 계수 (Refraction):"), row, 0)
                 self.groupParameters.layout().addWidget(self.spinRefraction, row, 1)
             
             # Or if chkRefraction is in a specific layout
             elif self.chkRefraction.parentWidget():
                 layout = self.chkRefraction.parentWidget().layout()
                 if layout:
                     # Attempt to add to the layout
                     if isinstance(layout, QtWidgets.QGridLayout):
                         # Logic to find position? Too complex, just add to end
                         layout.addWidget(self.spinRefraction)
                     elif isinstance(layout, (QtWidgets.QVBoxLayout, QtWidgets.QHBoxLayout)):
                         layout.addWidget(self.spinRefraction)
    
    def transform_point(self, point, source_crs, dest_crs):
        """Wrapper method to call the utility transform_point function"""
        return transform_point(point, source_crs, dest_crs)
    
    def reset_selection(self):
        """Reset all manual point selections and markers"""
        self.observer_point = None
        self.target_point = None
        self.observer_points = []
        if hasattr(self, 'drawn_line_points'):
            self.drawn_line_points = []
        self.los_click_count = 0
        if hasattr(self, 'point_marker'):
            self.point_marker.reset(QgsWkbTypes.PointGeometry)
        
        # Clear point number labels (Canvas items)
        if hasattr(self, 'point_labels'):
            for item in self.point_labels:
                try:
                    if self.canvas and self.canvas.scene():
                        self.canvas.scene().removeItem(item)
                except:
                    pass
            self.point_labels = []
        
        # Move label layer to top logic - we use canvas items now, but results are in layers
        # Standardize result layer addition
        self._remove_label_layer()
        self.lblSelectedPoint.setText("선택된 관측점 없음")

        self.lblSelectedPoint.setStyleSheet("")
        self.canvas.refresh()
    
    def _add_point_to_label_canvas(self, point, number):
        """Add a numbered label directly to map canvas using Annotations (High Stability)"""
        try:
            # 1. Create a Text Annotation
            annotation = QgsTextAnnotation()
            
            # 2. Configure Text Document
            doc = QTextDocument()
            html = f'<div style="color: red; font-weight: bold; background-color: rgba(255,255,255,180); border: 1px solid red; padding: 1px 3px; border-radius: 3px;">{number}</div>'
            doc.setHtml(html)
            annotation.setDocument(doc)
            
            # 3. Position and Settings
            annotation.setMapPosition(point)
            annotation.setHasFixedMapPosition(True)
            annotation.setFrameSizeQt(QtCore.QSizeF(30, 20)) # Width, Height
            
            # Simple offset to top-right
            annotation.setRelativePosition(QtCore.QPointF(0.5, 0.5))
            
            # 4. Create Canvas Item (This actually shows it on map without project layer)
            item = QgsMapCanvasAnnotationItem(annotation, self.canvas)
            
            # Store for cleanup
            self.point_labels.append(item)
            return item
            
        except Exception as e:
            print(f"Canvas labeling error: {e}")
            return None
    
    # [v1.6.17] _get_or_create_label_layer REMOVED - deprecated, was returning None


    
    def _remove_label_layer(self):
        """Remove the temporary label layer"""
        layer_name = "관측점_번호_라벨"
        layers = QgsProject.instance().mapLayersByName(layer_name)
        for layer in layers:
            try:
                QgsProject.instance().removeMapLayer(layer.id())
            except:
                pass
        self.label_layer = None

                
    def update_layer_order(self):
        """Move the label layer to the top of the layer list to prevent it from being covered"""
        layer_name = "관측점_번호_라벨"
        layers = QgsProject.instance().mapLayersByName(layer_name)
        if layers:
            layer = layers[0]
            root = QgsProject.instance().layerTreeRoot()
            layer_node = root.findLayer(layer.id())
            if layer_node:
                # Store visibility state
                is_visible = layer_node.isVisible()
                # Clone and move to top (index 0)
                parent = layer_node.parent()
                clone = layer_node.clone()
                clone.setItemVisibilityChecked(is_visible)
                root.insertChildNode(0, clone)
                parent.removeChildNode(layer_node)

    def on_mode_changed(self):
        """Enable/disable options based on analysis mode"""
        # Clear previous selections when mode changes
        self.reset_selection()
        
        is_line_mode = self.radioLineViewshed.isChecked()
        is_multi_mode = self.radioMultiPoint.isChecked()
        is_los_mode = self.radioLineOfSight.isChecked()
        is_buffer_mode = self.radioBufferVisibility.isChecked()
        is_reverse_mode = self.radioReverseViewshed.isChecked()
        
        # Enable line options for appropriate modes
        self.groupLineOptions.setEnabled(is_line_mode or is_multi_mode or is_buffer_mode)
        
        # Show/Hide Count Only checkbox - relevant for Line and Multi-point
        if hasattr(self, 'chkCountOnly'):
            self.chkCountOnly.setVisible(is_line_mode or is_multi_mode)
        
        # Update internal mode flags
        self.multi_point_mode = is_multi_mode
        self.los_mode = is_los_mode
        self.buffer_mode = is_buffer_mode
        
        # === Mode-specific UI adjustments ===
        
        # 1. Line Mode: Enable Drawing OR Layer selection
        if is_line_mode:
            self.radioClickMap.setEnabled(True)
            self.groupObserver.setTitle("3. 분석 대상(선형/둘레) 설정")
            
            # Filter layer for Line/Polygon only
            self.cmbObserverLayer.setFilters(QgsMapLayerProxyModel.Filter.LineLayer | QgsMapLayerProxyModel.Filter.PolygonLayer)
            
            if self.radioFromLayer.isChecked():
                self.btnSelectPoint.setText("🖱️ 추가 관측점 클릭 (선택사항)")
                if hasattr(self, 'lblLayerHint'):
                    self.lblLayerHint.setText("💡 성곽(Polygon)이나 도로(Line) 레이어를 선택하세요.")
            else:
                self.btnSelectPoint.setText("🖱️ 지도에서 경로(둘레) 그리기")
                if hasattr(self, 'lblLayerHint'):
                    self.lblLayerHint.setText("💡 시작점 클릭 후 경로를 그리세요 (시작점 재클릭 시 자동 닫힘).")
            
            if hasattr(self, 'lblLayerHint'):
                self.lblLayerHint.setVisible(True)
        
        # 2. Point-based modes: Enable both options
        elif self.radioSinglePoint.isChecked():
            self.radioClickMap.setEnabled(True)
            self.groupObserver.setTitle("3. 관측점 설정")
            self.btnSelectPoint.setText("🖱️ 지도에서 관측점 선택")
            if hasattr(self, 'lblLayerHint'):
                self.lblLayerHint.setText("💡 레이어 선택 시: 피처의 중심점(Centroid)에서 가시권을 계산합니다.")
                self.lblLayerHint.setVisible(self.radioFromLayer.isChecked())
        
        elif is_multi_mode:
            self.radioClickMap.setEnabled(True)
            self.groupObserver.setTitle("3. 관측점 설정 (다중 선택)")
            self.btnSelectPoint.setText("🖱️ 추가 관측점 클릭")
            if hasattr(self, 'lblLayerHint'):
                self.lblLayerHint.setText("💡 레이어의 포인트 + 지도 클릭을 함께 사용할 수 있습니다.")
                self.lblLayerHint.setVisible(self.radioFromLayer.isChecked())
        
        elif is_los_mode:
            self.radioClickMap.setEnabled(True)
            self.radioClickMap.setChecked(True)
            self.radioFromLayer.setEnabled(False)  # LOS는 지도 클릭만
            self.groupObserver.setTitle("3. 가시선 설정")
            self.btnSelectPoint.setText("🖱️ 관측점 → 대상점 순서로 클릭")
            self.observer_point = None
            self.target_point = None
            self.los_click_count = 0
            if hasattr(self, 'lblLayerHint'):
                self.lblLayerHint.setVisible(False)
        
        elif is_buffer_mode:
            self.radioClickMap.setEnabled(True)
            self.groupObserver.setTitle("3. 중심점 설정")
            self.btnSelectPoint.setText("🖱️ 중심점 선택")
            if hasattr(self, 'lblLayerHint'):
                self.lblLayerHint.setVisible(False)
        
        elif is_reverse_mode:
            self.radioClickMap.setEnabled(True)
            self.groupObserver.setTitle("3. 대상물 위치 설정")
            self.btnSelectPoint.setText("🖱️ 지도에서 대상물 위치 선택")
            if hasattr(self, 'lblLayerHint'):
                self.lblLayerHint.setVisible(False)
        
        else:
            self.radioClickMap.setEnabled(True)
            self.groupObserver.setTitle("3. 관측점 설정")
            self.btnSelectPoint.setText("🖱️ 지도에서 위치 선택")
        
        # Reset layer selection radio for non-LOS modes
        if not is_los_mode:
            self.radioFromLayer.setEnabled(True)
            
        # 3. Target Height: Enable only for Reverse Viewshed or Line of Sight
        # (Target Height is the object height we are looking for in standard viewshed, 
        # but the user requested to limit it to avoid confusion)
        self.spinTargetHeight.setEnabled(is_reverse_mode or is_los_mode)
        if hasattr(self, 'lblTargetHeight'):
            self.lblTargetHeight.setEnabled(is_reverse_mode or is_los_mode)

        # 4. Layer filters: Filter by geometry type based on mode
        if is_line_mode:
            # Only show Line or Polygon layers for Line Viewshed (those with length/perimeter)
            self.cmbObserverLayer.setFilters(QgsMapLayerProxyModel.Filter.LineLayer | QgsMapLayerProxyModel.Filter.PolygonLayer)
        else:
            # Show Point and Polygon layers (to support centroid-based analysis)
            self.cmbObserverLayer.setFilters(QgsMapLayerProxyModel.Filter.PointLayer | QgsMapLayerProxyModel.Filter.PolygonLayer)
        
        # Trigger source change handler to update dependent UI
        self.on_source_changed()
    
    def on_source_changed(self):
        """Toggle between map click and layer selection
        
        Handles mode-specific text and behavior:
        - Line mode: Show layer selection hints
        - Point modes: Show point layer hints
        - Multi-point: Enable hybrid mode (layer + clicks)
        """
        from_layer = self.radioFromLayer.isChecked()
        is_multi = self.radioMultiPoint.isChecked()
        is_line_mode = self.radioLineViewshed.isChecked()
        
        # Update radio button text based on mode
        if is_line_mode:
            self.radioFromLayer.setText("레이어에서 선택")
        elif from_layer:
            self.radioFromLayer.setText("레이어에서 선택")
        else:
            self.radioFromLayer.setText("레이어에서 선택")
        
        # If switching to layer, clear manual selection
        if from_layer and not is_line_mode:
            self.reset_selection()
        
        self.cmbObserverLayer.setEnabled(from_layer)
        
        # Button enable logic
        if is_line_mode:
            # Line mode: allow additional point clicks
            self.btnSelectPoint.setEnabled(True)
        elif is_multi:
            # Multi-point: always allow manual clicks (hybrid mode)
            self.btnSelectPoint.setEnabled(True)
        else:
            # Other modes: disable button when using layer
            self.btnSelectPoint.setEnabled(not from_layer)
        
        # UI Feedback based on mode
        if from_layer:
            if is_line_mode:
                self.lblSelectedPoint.setText("소스: 선택된 선형/둘레 레이어")
            else:
                self.lblSelectedPoint.setText("소스: 선택된 레이어")
            
            if not is_multi and not is_line_mode:
                self.point_marker.reset(QgsWkbTypes.PointGeometry)
        else:
            if is_line_mode:
                if hasattr(self, 'drawn_line_points') and self.drawn_line_points:
                    self.lblSelectedPoint.setText(f"그려진 경로: {len(self.drawn_line_points)}개 정점 {'(폐곡선)' if self.is_line_closed else '(개곡선)'}")
                else:
                    self.lblSelectedPoint.setText("그려진 경로: 없음 (지도를 클릭하세요)")
            elif self.observer_point:
                self.lblSelectedPoint.setText(f"선택된 위치: {self.observer_point.x():.1f}, {self.observer_point.y():.1f}")
            else:
                self.lblSelectedPoint.setText("선택된 위치: 없음")

    def on_layer_selection_changed(self, layer):
        """Auto-check 'From Layer' when a layer is selected in the combo box"""
        if layer:
            self.radioFromLayer.setChecked(True)

    def on_layers_removed(self, layer_ids):
        """Clean up markers and annotations if the corresponding analysis layer is removed"""
        for lid in layer_ids:
            # 1. Clean up RubberBands (Red Dots)
            if lid in self.result_marker_map:
                markers = self.result_marker_map[lid]
                for m in markers:
                    try:
                        if m:
                            m.hide() # Force hide first
                            m.reset(QgsWkbTypes.PointGeometry) # Clear geometry
                            if self.canvas and self.canvas.scene():
                                self.canvas.scene().removeItem(m) # Remove from scene
                    except Exception as e:
                        print(f"Marker cleanup error: {e}")
                del self.result_marker_map[lid]
                
            # 2. Clean up Text Annotations (Labels) [v1.6.02]
            if lid in self.result_annotation_map:
                annotations = self.result_annotation_map[lid]
                for item in annotations:
                    try:
                        if item and self.canvas.scene():
                            self.canvas.scene().removeItem(item)
                    except Exception as e:
                        print(f"Annotation cleanup error: {e}")
                del self.result_annotation_map[lid]
            
            # 3. [v1.6.18] Clean up linked Observer Layer (red points layer)
            if lid in self.result_observer_layer_map:
                obs_layer_id = self.result_observer_layer_map[lid]
                try:
                    QgsProject.instance().removeMapLayer(obs_layer_id)
                except:
                    pass
                del self.result_observer_layer_map[lid]
        
        if self.last_result_layer_id in layer_ids:
            self.reset_selection()
            self.last_result_layer_id = None

    def get_context_point_and_crs(self):
        """Helper to get observer point(s) and their source CRS
        Returns a list of (point, crs) tuples.
        """
        points_with_crs = []
        canvas_crs = self.canvas.mapSettings().destinationCrs()
        
        # 1. Check for manual override (If user clicked on map, use it regardless of mode)
        if self.observer_point:
            points_with_crs.append((self.observer_point, canvas_crs))
            
        # 2. If no manual override, or in multi-point/layer mode, add layer features
        if self.radioFromLayer.isChecked():
            obs_layer = self.cmbObserverLayer.currentLayer()
            if obs_layer:
                # Prioritize selected features
                selected_features = obs_layer.selectedFeatures()
                features = selected_features if selected_features else []
                
                # If nothing selected and no manual point, fallback to first feature
                if not features and not points_with_crs:
                    first_feat = next(obs_layer.getFeatures(), None)
                    if first_feat:
                        features = [first_feat]
                
                for feat in features:
                    if not feat: continue
                    geom = feat.geometry()
                    if geom and not geom.isEmpty():
                        # Use centroid
                        pt = geom.centroid().asPoint()
                        # Only add if it's not already the manual point (edge case)
                        points_with_crs.append((pt, obs_layer.crs()))
        
        # 3. Handle multi-point clicks
        if self.multi_point_mode:
            for p in self.observer_points:
                points_with_crs.append((p, canvas_crs))
        
        return points_with_crs

    def start_point_selection(self):
        """Start point or line selection on map depending on mode"""
        # NO project modification here!
        self.original_tool = self.canvas.mapTool()

        
        # Use line drawing tool for Line Viewshed mode
        if self.radioLineViewshed.isChecked():
            self.map_tool = ViewshedLineTool(self.canvas, self)
            self.canvas.setMapTool(self.map_tool)
            self.iface.messageBar().pushMessage(
                "선형 및 둘레 가시권", "지도에서 라인을 그리세요. 클릭으로 점 추가, 시작점 클릭 시 자동 닫힘(Snap), 우클릭으로 완료", level=0
            )
        else:
            self.map_tool = ViewshedPointTool(self.canvas, self)
            self.canvas.setMapTool(self.map_tool)
            self.iface.messageBar().pushMessage(
                "가시권 분석", "지도에서 관측점을 클릭하세요", level=0
            )
        self.hide()
    
    def set_observer_point(self, point):
        """Called when user clicks on map"""
        if self.multi_point_mode:
            # Multi-point mode: add to list
            self.observer_points.append(point)
            self.point_marker.addPoint(point)
            
            count = len(self.observer_points)
            
            # Add point number on canvas
            self._add_point_to_label_canvas(point, count)

            
            self.lblSelectedPoint.setText(f"선택된 관측점: {count}개")
            self.lblSelectedPoint.setStyleSheet("color: #2196F3; font-weight: bold;")
            
            # Show message and continue adding
            self.iface.messageBar().pushMessage(
                "다중점 가시권", 
                f"▶ 점 {count} 추가됨. 계속 클릭하거나 ESC로 완료", 
                level=0
            )
            # Don't return to dialog yet - let user add more points
        
        elif self.los_mode:
            # Line of Sight mode: first click = observer, second click = target
            self.los_click_count += 1
            self.point_marker.addPoint(point)
            
            if self.los_click_count == 1:
                self.observer_point = point
                self.iface.messageBar().pushMessage(
                    "가시선 분석", 
                    "관측점 설정 완료. 이제 대상점을 클릭하세요", 
                    level=0
                )
            else:
                self.target_point = point
                self.lblSelectedPoint.setText(
                    f"관측점→대상점: ({self.observer_point.x():.0f},{self.observer_point.y():.0f}) → ({point.x():.0f},{point.y():.0f})"
                )
                self.lblSelectedPoint.setStyleSheet("color: #2196F3; font-weight: bold;")
                
                # Both points selected, return to dialog
                if self.original_tool:
                    self.canvas.setMapTool(self.original_tool)
                self.show()
        
        else:
            # Single point mode
            self.observer_point = point
            self.point_marker.reset(QgsWkbTypes.PointGeometry)
            self.point_marker.addPoint(point)
            
            self.lblSelectedPoint.setText(f"선택된 위치: {point.x():.1f}, {point.y():.1f}")
            self.lblSelectedPoint.setStyleSheet("color: #2196F3; font-weight: bold;")
            
            # Restore original tool and show dialog
            if self.original_tool:
                self.canvas.setMapTool(self.original_tool)
            self.show()
    
    def transform_to_dem_crs(self, point, dem_layer):
        """Transform point from Map Canvas CRS to DEM CRS - DEPRECATED (use transform_point)"""
        canvas_crs = self.canvas.mapSettings().destinationCrs()
        return self.transform_point(point, canvas_crs, dem_layer.crs())
    
    def set_line_from_tool(self, points, is_closed=False):
        """Set a user-drawn line for line viewshed analysis"""
        if not points: return
        
        # Store the drawn line points and closure state
        self.drawn_line_points = points
        self.is_line_closed = is_closed
        self.observer_point = points[0]
        
        # [v1.5.85] Maintain vertex visibility on the map
        self.point_marker.reset(QgsWkbTypes.LineGeometry)
        for pt in points:
            self.point_marker.addPoint(pt)
        if is_closed:
            self.point_marker.addPoint(points[0])
        
        self.lblSelectedPoint.setText(f"선택된 경로: {len(points)}개 정점 {'(폐곡선)' if is_closed else '(개곡선)'}")
        self.lblSelectedPoint.setStyleSheet("color: #2196F3; font-weight: bold;")
    
    def run_analysis(self):
        """Run the selected viewshed analysis"""
        dem_layer = self.cmbDemLayer.currentLayer()
        if not dem_layer:
            self.iface.messageBar().pushMessage("오류", "DEM 래스터를 선택해주세요", level=2)
            return
        
        # Check observer point
        # Check observer point (Supports single selection and multi-clicked list)
        has_manual = self.observer_point is not None or len(self.observer_points) > 0
        has_layer = self.radioFromLayer.isChecked() and self.cmbObserverLayer.currentLayer() is not None

        if not has_manual and not has_layer:
            self.iface.messageBar().pushMessage("오류", "관측점을 선택하거나 레이어를 지정해주세요", level=2)
            return
        
        # Get parameters
        observer_height = self.spinObserverHeight.value()
        target_height = self.spinTargetHeight.value()
        max_distance = self.spinMaxDistance.value()
        curvature = self.chkCurvature.isChecked()
        refraction = self.chkRefraction.isChecked()
        refraction_coeff = 0.13
        if hasattr(self, 'spinRefraction'):
            refraction_coeff = self.spinRefraction.value()
        
        self.iface.messageBar().pushMessage("처리 중", "가시권 분석 실행 중...", level=0)
        
        # [v1.5.97] REMOVED global self.hide() from here. 
        # It is now moved into each specialized run_* method to avoid freezes during warnings.
        
        try:
            if self.radioSinglePoint.isChecked():
                self.run_single_viewshed(
                    dem_layer, observer_height, target_height, 
                    max_distance, curvature, refraction, refraction_coeff
                )
            elif self.radioLineViewshed.isChecked():
                # [v1.6.13] Line Viewshed now uses run_multi_viewshed for proper union logic
                self.run_multi_viewshed(
                    dem_layer, observer_height, target_height,
                    max_distance, curvature, refraction, refraction_coeff
                )
            elif self.radioMultiPoint.isChecked():
                self.run_multi_viewshed(
                    dem_layer, observer_height, target_height,
                    max_distance, curvature, refraction, refraction_coeff
                )
            elif self.radioLineOfSight.isChecked():
                if not self.observer_point or not self.target_point:
                    self.iface.messageBar().pushMessage("오류", "관측점과 대상점을 모두 선택해주세요", level=2)
                    self.show()
                    return
                self.run_line_of_sight(
                    dem_layer, observer_height, target_height
                )
            elif self.radioBufferVisibility.isChecked():
                if not self.observer_point:
                    self.iface.messageBar().pushMessage("오류", "중심점을 선택해주세요", level=2)
                    self.show()
                    return
                self.run_buffer_visibility(
                    dem_layer, observer_height, target_height
                )
            else:  # Reverse viewshed
                self.run_reverse_viewshed(
                    dem_layer, observer_height, target_height,
                    max_distance, curvature, refraction, refraction_coeff
                )
        except Exception as e:
            self.iface.messageBar().pushMessage("오류", f"분석 중 오류: {str(e)}", level=2)
            self.show()
    
    def run_buffer_visibility(self, dem_layer, obs_height, tgt_height):
        """Analyze visibility from buffer perimeter to center point
        
        Creates points around buffer, checks LOS to center, and creates
        color-coded result showing visible (green) vs obstructed (red) directions.
        """
        center = self.observer_point
        # If observer_point is None, but we are in fromLayer mode, we need to pick the centroid
        if not center:
            pts = self.get_context_point_and_crs()
            if pts: center, _ = pts[0]

        if not center:
            push_message(self.iface, "오류", "중심점을 선택해주세요", level=2)
            restore_ui_focus(self)
            return

        # Transform to DEM CRS for accurate distance calculations
        center_dem = self.transform_to_dem_crs(center, dem_layer)
        
        buffer_radius = self.spinMaxDistance.value()  # Use max distance as buffer radius
        interval = self.spinLineInterval.value()
        
        # Calculate number of points based on circumference and interval
        circumference = 2 * math.pi * buffer_radius
        num_points = max(8, int(circumference / interval))
        
        # Generate points around buffer perimeter
        perimeter_points = []
        for i in range(num_points):
            angle = (2 * math.pi * i) / num_points
            x = center_dem.x() + buffer_radius * math.cos(angle)
            y = center_dem.y() + buffer_radius * math.sin(angle)
            perimeter_points.append(QgsPointXY(x, y))
        
        # Run LOS from each perimeter point to center
        provider = dem_layer.dataProvider()
        visible_results = []
        
        # Transform for visualization
        dem_to_canvas = QgsCoordinateTransform(dem_layer.crs(), 
                                               self.canvas.mapSettings().destinationCrs(), 
                                               QgsProject.instance())
        
        # Consolidate perimeter points into a single ring styling
        # Instead of rays, we draw the perimeter itself, colored by visibility from center.
        
        layer = QgsVectorLayer("LineString?crs=" + dem_layer.crs().authid(),
                              f"가시권_링분석_{int(buffer_radius)}m", "memory")
        pr = layer.dataProvider()
        pr.addAttributes([
            QgsField("status", QVariant.String),
            QgsField("score", QVariant.Double)
        ])
        layer.updateFields()
        
        # Reconstruct the perimeter segments based on visibility
        # We have the visibility status for each point 'i'
        visible_segments = []
        hidden_segments = []
        
        current_poly_pts = []
        current_status = None
        
        # We need to close the loop
        pts_loop = perimeter_points + [perimeter_points[0]]
        
        # To get status for segments between points, we can use the status of the starting point
        # OR we can supersample. For now, point status -> segment status.
        
        # Let's perform the check for all points first
        point_status = []
        visible_count = 0
        
        for pt in perimeter_points:
            # Check LOS Center <-> Point
            # Simple check at 3 points along ray to be sure? No, just end-to-end for speed
            # Use original sampling logic for accuracy
             # Sample along line to center
            dx = center_dem.x() - pt.x()
            dy = center_dem.y() - pt.y()
            dist = math.sqrt(dx*dx + dy*dy)
            
            # Quick Check: 10 samples
            is_visible = True
            for k in range(1, 11):
                f = k / 10.0
                sx = pt.x() + f * dx
                sy = pt.y() + f * dy
                
                res_s, elev_s = provider.sample(QgsPointXY(sx, sy), 1)
                res_p, elev_p = provider.sample(pt, 1)
                res_c, elev_c = provider.sample(center_dem, 1)
                
                if not (res_s and res_p and res_c): continue
                
                p_h = elev_p + obs_height
                c_h = elev_c + tgt_height
                
                sight = p_h + f * (c_h - p_h)
                if elev_s > sight:
                    is_visible = False
                    break
            
            point_status.append(is_visible)
            if is_visible: visible_count += 1
            
        # Creates segments
        for i in range(len(perimeter_points)):
            p1 = perimeter_points[i]
            p2 = perimeter_points[(i+1) % len(perimeter_points)]
            
            status = point_status[i]
            
            # Transform for visualization
            p1_can = dem_to_canvas.transform(p1)
            p2_can = dem_to_canvas.transform(p2)
            
            feat = QgsFeature(layer.fields())
            feat.setGeometry(QgsGeometry.fromPolylineXY([p1_can, p2_can]))
            feat.setAttributes(["감시 가능" if status else "사각지대", 1 if status else 0])
            pr.addFeature(feat)
        
        layer.updateExtents()
        
        # Style: Cleaner lines for perimeter ring
        categories = [
            QgsRendererCategory("감시 가능", QgsLineSymbol.createSimple({
                'color': '0,200,0', 'width': '1.0', 'line_style': 'solid'
            }), "감시 가능 (Visible)"),
            QgsRendererCategory("사각지대", QgsLineSymbol.createSimple({
                'color': '255,0,0', 'width': '1.0', 'line_style': 'solid'
            }), "사각지대 (Hidden)")
        ]
        layer.setRenderer(QgsCategorizedSymbolRenderer("status", categories))
        QgsProject.instance().addMapLayers([layer])
        self.last_result_layer_id = layer.id()
        
        # Ensure label layer is on top
        self.update_layer_order()
        
        # Link center marker
        self.link_current_marker_to_layer(layer.id(), [(center, self.canvas.mapSettings().destinationCrs())])
        
        # Summary message
        visibility_pct = (visible_count / len(perimeter_points) * 100) if perimeter_points else 0
        self.iface.messageBar().pushMessage(
            "가시권 링 분석 (Visibility Ring Analysis)",
            f"중심점 감시율: {visibility_pct:.1f}% ({visible_count}/{len(perimeter_points)}개 지점에서 보임)",
            level=0
        )
        
        self.accept()
    
    def create_observer_layer(self, name, points_info):
        """Create a persistent memory layer for manual observer points"""
        crs = self.canvas.mapSettings().destinationCrs().authid()
        
        # Check if we have points or lines
        is_line = False
        if not self.radioFromLayer.isChecked() and hasattr(self, 'drawn_line_points') and self.radioLineViewshed.isChecked():
            is_line = True
            
        if is_line:
            layer = QgsVectorLayer(f"LineString?crs={crs}", name, "memory")
        else:
            layer = QgsVectorLayer(f"Point?crs={crs}", name, "memory")
            
        pr = layer.dataProvider()
        
        # Add fields
        pr.addAttributes([QgsField("no", QVariant.Int)])
        layer.updateFields()
        
        # Add features
        features = []
        if is_line:
            feat = QgsFeature(layer.fields())
            feat.setGeometry(QgsGeometry.fromPolylineXY(self.drawn_line_points))
            feat.setAttributes([1])
            features.append(feat)
        else:
            for i, (pt, _) in enumerate(points_info):
                feat = QgsFeature(layer.fields())
                feat.setGeometry(QgsGeometry.fromPointXY(pt))
                feat.setAttributes([i + 1])
                features.append(feat)
        
        pr.addFeatures(features)
        
        # Style the layer
        if is_line:
            symbol = QgsLineSymbol.createSimple({'color': 'blue', 'width': '0.6'})
        else:
            # Create a red point marker
            symbol = QgsMarkerSymbol.createSimple({
                'name': 'circle',
                'color': 'red',
                'outline_color': 'white',
                'size': '3.0'
            })
            
            # Add labeling
            text_format = QgsTextFormat()
            text_format.setSize(10)
            text_format.setColor(QColor(255, 0, 0)) # Red text
            
            # Buffer around text for readability (Essential for topological maps)
            from qgis.core import QgsTextBufferSettings
            buffer_settings = QgsTextBufferSettings()
            buffer_settings.setEnabled(True)
            buffer_settings.setSize(1.2) # Slightly larger buffer
            buffer_settings.setColor(QColor(255, 255, 255, 230)) # Dense white buffer
            text_format.setBuffer(buffer_settings)
            
            label_settings = QgsPalLayerSettings()
            label_settings.setFormat(text_format)
            label_settings.fieldName = "no"
            label_settings.enabled = True
            
            # Placement: Around the point (more stable than OverPoint in some Python bindings)
            label_settings.placement = QgsPalLayerSettings.AroundPoint
            label_settings.dist = 1
            
            layer.setLabeling(QgsVectorLayerSimpleLabeling(label_settings))
            layer.setLabelsEnabled(True)
            
        layer.setRenderer(QgsSingleSymbolRenderer(symbol))
        
        QgsProject.instance().addMapLayers([layer])
        return layer

    def run_single_viewshed(self, dem_layer, obs_height, tgt_height, max_dist, curvature, refraction, refraction_coeff=0.13):
        """Run single point viewshed analysis with circular masking"""
        points_info = self.get_context_point_and_crs()
        if not points_info:
            push_message(self.iface, "오류", "관측점을 선택해주세요", level=2)
            restore_ui_focus(self)
            return
            
        # [v1.5.97] Hide dialog only when processing starts
        self.hide()
        QtWidgets.QApplication.processEvents()

        point, src_crs = points_info[0] # Take first one for single viewshed
        
        # If manual selection, create persistent point layer
        if not self.radioFromLayer.isChecked():
            self.create_observer_layer("가시권_관측점", points_info)
        
        run_id = str(uuid.uuid4())[:12]
        raw_output = os.path.join(tempfile.gettempdir(), f'archt_vs_raw_{run_id}.tif')
        final_output = os.path.join(tempfile.gettempdir(), f'archt_vs_final_{run_id}.tif')
        
        # Transform point to DEM CRS
        point_dem = self.transform_point(point, src_crs, dem_layer.crs())
        
        # Build params
        params = {
            'INPUT': dem_layer.source(),
            'BAND': 1,
            'OBSERVER': f"{point_dem.x()},{point_dem.y()}",
            'OBSERVER_HEIGHT': obs_height,
            'TARGET_HEIGHT': tgt_height,
            'MAX_DISTANCE': max_dist,
            'CURVATURE': curvature,
            'REFRACTION': refraction,
            'IV': refraction_coeff if refraction else 0, # Pass coefficient if enabled
            'OUTPUT': raw_output
        }
        
        # FOV (Azimuth) logic - only for single viewshed
        azimuth = self.spinAzimuth.value()
        azimuth_width = self.spinAngleWidth.value()
        if azimuth_width < 360:
            params['AZIMUTH'] = azimuth
            params['AZIMUTH_WIDTH'] = azimuth_width
        
        try:
            processing.run("gdal:viewshed", params)
            
            # Circular Masking: Clip raw output by a circular buffer
            if os.path.exists(raw_output):
                # Create a temporary memory layer for the circular mask
                mask_layer = QgsVectorLayer("Polygon?crs=" + dem_layer.crs().authid(), "temp_mask", "memory")
                pr = mask_layer.dataProvider()
                circle_feat = QgsFeature()
                # Create extremely detailed circle buffer for smooth edges
                circle_feat.setGeometry(QgsGeometry.fromPointXY(point_dem).buffer(max_dist, 128))
                pr.addFeatures([circle_feat])
                
                # Clip using universal algorithm
                # Force Float32 (6) and set NoData to -9999 to ensure absolute transparency
                processing.run("gdal:cliprasterbymasklayer", {
                    'INPUT': raw_output,
                    'MASK': mask_layer,
                    'NODATA': -9999,
                    'DATA_TYPE': 6, # Float32
                    'ALPHA_BAND': False,
                    'CROP_TO_CUTLINE': True,
                    'KEEP_RESOLUTION': True,
                    'OUTPUT': final_output
                })
                
                if not os.path.exists(final_output):
                    shutil.copy(raw_output, final_output)
            
            if os.path.exists(final_output):
                use_higuchi = self.chkHiguchi.isChecked()
                is_reverse = self.radioReverseViewshed.isChecked()
                if use_higuchi:
                    layer_name = f"가시권_히구치_{int(max_dist)}m"
                elif is_reverse:
                    layer_name = f"역방향_가시권_{int(max_dist)}m"
                else:
                    layer_name = f"가시권_단일점_{int(max_dist)}m"
                viewshed_layer = QgsRasterLayer(final_output, layer_name)
                
                if viewshed_layer.isValid():
                    if use_higuchi:
                        self.apply_higuchi_style(viewshed_layer, point, max_dist, dem_layer)
                    else:
                        self.apply_viewshed_style(viewshed_layer)
                    
                    QgsProject.instance().addMapLayers([viewshed_layer])
                    self.link_current_marker_to_layer(viewshed_layer.id(), [(point, src_crs)])
                    
                    # Ensure label layer is on top
                    self.update_layer_order()
                    cleanup_files([raw_output])
                    self.accept()
                else:
                    raise Exception("결과 레이어 로드 실패")
        except Exception as e:
            push_message(self.iface, "오류", f"분석 중 오류: {str(e)}", level=2)
            restore_ui_focus(self)
        finally:
            cleanup_files([raw_output])
    
    # [v1.6.17] run_line_viewshed REMOVED - Line Viewshed now uses run_multi_viewshed

    def run_reverse_viewshed(self, dem_layer, obs_height, tgt_height, max_dist, curvature, refraction, refraction_coeff=0.13):
        """Run reverse viewshed - from where can the target be seen?
        
        This swaps observer and target heights to answer:
        "From where can a structure of height X be seen?"
        """
        # For reverse viewshed, we swap the heights conceptually
        # The target location becomes the "observer" position
        # And we ask: from which cells can this point be seen by someone at ground level?
        
        # This is essentially the same as regular viewshed but with swapped heights
        self.run_single_viewshed(
            dem_layer, 
            tgt_height,  # Target becomes observer
            obs_height,  # Observer height becomes target
            max_dist, 
            curvature, 
            refraction,
            refraction_coeff
        )
    
    def run_line_of_sight(self, dem_layer, obs_height, tgt_height):
        """Run Line of Sight analysis between observer and target points
        
        Samples terrain along line, computes sight line, and detects obstructions.
        Creates visual output showing visible vs obstructed segments.
        """
        observer = self.observer_point
        target = self.target_point
        
        if not observer or not target:
            self.iface.messageBar().pushMessage("오류", "관측점과 대상점을 클릭하여 선택해주세요", level=2)
            self.show()
            return

        # Calculate distance
        dx = target.x() - observer.x()
        dy = target.y() - observer.y()
        total_dist = math.sqrt(dx*dx + dy*dy)
        
        # Sample terrain along line
        num_samples = max(200, int(total_dist / 5)) # Higher density for segmented LOS
        profile_data = []
        
        provider = dem_layer.dataProvider()
        
        for i in range(num_samples + 1):
            frac = i / num_samples
            x = observer.x() + frac * dx
            y = observer.y() + frac * dy
            dist = frac * total_dist
            
            # Sample elevation from DEM
            result, elev = provider.sample(QgsPointXY(x, y), 1)
            if result and elev is not None and not math.isnan(elev):
                profile_data.append({
                    'distance': dist,
                    'elevation': elev,
                    'x': x,
                    'y': y
                })
        
        if len(profile_data) < 2:
            push_message(self.iface, "오류", "지형 데이터를 샘플링할 수 없습니다", level=2)
            restore_ui_focus(self)
            return
        
        # Observer and target elevations (with height added)
        obs_elev = profile_data[0]['elevation'] + obs_height
        tgt_elev = profile_data[-1]['elevation'] + tgt_height
        
        # Create result layer
        layer = QgsVectorLayer("LineString?crs=" + dem_layer.crs().authid(), 
                              f"가시선_분석_{int(total_dist)}m", "memory")
        pr = layer.dataProvider()
        pr.addAttributes([
            QgsField("status", QVariant.String), # "보임" or "안보임"
            QgsField("distance", QVariant.Double)
        ])
        layer.updateFields()
        
        # Segment the profile using MAX-ANGLE algorithm
        # A point is visible if its angle (from observer) >= max angle seen so far
        segments = []
        current_segment = []
        last_status = None
        first_obstruction = None
        is_visible_overall = True
        max_angle = -float('inf')
        
        for i, pt in enumerate(profile_data):
            if pt['distance'] == 0:
                # Observer point is always "visible"
                status = "보임"
            else:
                # Calculate angle from observer to this terrain point
                angle = (pt['elevation'] - obs_elev) / pt['distance']
                
                if angle >= max_angle:
                    max_angle = angle
                    status = "보임"  # Visible - new max angle
                else:
                    status = "안보임"  # Hidden in shadow
                    if is_visible_overall:
                        is_visible_overall = False
                        first_obstruction = pt
            
            if status != last_status:
                if current_segment:
                    segments.append((last_status, current_segment))
                    current_segment = [current_segment[-1]]  # Continue from last point
                current_segment.append(QgsPointXY(pt['x'], pt['y']))
                last_status = status
            else:
                current_segment.append(QgsPointXY(pt['x'], pt['y']))
        
        if current_segment:
            segments.append((last_status, current_segment))

        # Add features for each segment
        for status, pts in segments:
            if len(pts) < 2: continue
            feat = QgsFeature(layer.fields())
            feat.setGeometry(QgsGeometry.fromPolylineXY(pts))
            feat.setAttributes([status, total_dist])
            pr.addFeature(feat)
            
        layer.updateExtents()
        
        # Style: Thin lines for visibility (Green/Red)
        categories = [
            QgsRendererCategory("보임", QgsLineSymbol.createSimple({
                'color': '0,200,0', 'width': '0.8'
            }), "보임"),
            QgsRendererCategory("안보임", QgsLineSymbol.createSimple({
                'color': '255,0,0', 'width': '0.6', 'line_style': 'dash'
            }), "안보임")
        ]
        layer.setRenderer(QgsCategorizedSymbolRenderer("status", categories))
        QgsProject.instance().addMapLayers([layer])
        self.last_result_layer_id = layer.id()
        
        # Ensure label layer is on top
        self.update_layer_order()
        
        # Add Start/End point markers on the map
        marker_layer = QgsVectorLayer("Point?crs=" + dem_layer.crs().authid(),
                                      f"가시선_마커", "memory")
        m_pr = marker_layer.dataProvider()
        m_pr.addAttributes([QgsField("유형", QVariant.String)])
        marker_layer.updateFields()
        
        # Start point
        start_feat = QgsFeature(marker_layer.fields())
        start_feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(profile_data[0]['x'], profile_data[0]['y'])))
        start_feat.setAttributes(["시작 (S)"])
        m_pr.addFeature(start_feat)
        
        # End point
        end_feat = QgsFeature(marker_layer.fields())
        end_feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(profile_data[-1]['x'], profile_data[-1]['y'])))
        end_feat.setAttributes(["끓 (E)"])
        m_pr.addFeature(end_feat)
        marker_layer.updateExtents()
        
        # Style markers
        marker_categories = [
            QgsRendererCategory("시작 (S)", QgsMarkerSymbol.createSimple({
                'name': 'circle', 'color': '0,100,255', 'size': '3'
            }), "시작 (S)"),
            QgsRendererCategory("끓 (E)", QgsMarkerSymbol.createSimple({
                'name': 'circle', 'color': '255,140,0', 'size': '3'
            }), "끓 (E)")
        ]
        marker_layer.setRenderer(QgsCategorizedSymbolRenderer("유형", marker_categories))
        # Create persistent markers
        if not self.radioFromLayer.isChecked():
            self.create_observer_layer("가시선_관측점", [(observer, self.canvas.mapSettings().destinationCrs()), 
                                                      (target, self.canvas.mapSettings().destinationCrs())])
        
        QgsProject.instance().addMapLayers([marker_layer])
        
        # If obstructed, mark the first obstacle
        if first_obstruction:
            obs_layer = QgsVectorLayer("Point?crs=" + dem_layer.crs().authid(),
                                       "첫번째_장애물", "memory")
            obs_pr = obs_layer.dataProvider()
            obs_pr.addAttributes([
                QgsField("distance", QVariant.Double),
                QgsField("elevation", QVariant.Double)
            ])
            obs_layer.updateFields()
            
            obs_feat = QgsFeature(obs_layer.fields())
            obs_feat.setGeometry(QgsGeometry.fromPointXY(
                QgsPointXY(first_obstruction['x'], first_obstruction['y'])
            ))
            obs_feat.setAttributes([
                first_obstruction['distance'],
                first_obstruction['elevation']
            ])
            obs_pr.addFeature(obs_feat)
            obs_layer.updateExtents()
            
            marker_symbol = QgsMarkerSymbol.createSimple({
                'name': 'circle',
                'color': '255,0,0',
                'size': '4'
            })
            obs_layer.setRenderer(QgsSingleSymbolRenderer(marker_symbol))
            QgsProject.instance().addMapLayer(obs_layer)
        
        # Show result message
        if is_visible_overall:
            self.iface.messageBar().pushMessage(
                "가시선 분석", 
                f"✅ 직시 가능! 거리: {total_dist:.0f}m", 
                level=0
            )
        else:
            if first_obstruction:
                self.iface.messageBar().pushMessage(
                    "가시선 분석", 
                    f"❌ 직시 불가! 장애물: {first_obstruction['distance']:.0f}m 지점 (고도 {first_obstruction['elevation']:.1f}m)", 
                    level=1
                )
            else:
                self.iface.messageBar().pushMessage(
                    "가시선 분석", 
                    f"❌ 직시 불가!", 
                    level=1
                )
        
        # Open Profiler for visualization
        self.show_profiler(profile_data, obs_height, tgt_height, total_dist)
        
        self.accept()
        
    def show_profiler(self, profile_data, obs_height, tgt_height, total_dist):
        """Open the 2D Profiler dialog"""
        try:
            profiler = ViewshedProfilerDialog(self.iface, profile_data, obs_height, tgt_height, total_dist, self)
            profiler.exec_()
        except Exception as e:
            print(f"Profiler error: {e}")
    
    def combine_viewsheds_numpy(self, dem_layer, viewshed_files, output_path, observer_points, max_dist, is_count_mode, grid_info, union_mode=False):
        """Highly optimized cumulative viewshed merging with unified grid alignment.
        """
        try:
            # 1. Get base parameters from grid_info
            target_xmin = grid_info['xmin']
            target_ymax = grid_info['ymax']
            target_width = grid_info['width']
            target_height = grid_info['height']
            dem_xres = grid_info['res']
            dem_yres = grid_info['res']
            
            dem_ds = gdal.Open(dem_layer.source(), gdal.GA_ReadOnly)
            dem_proj = dem_ds.GetProjection()
            dem_ds = None
            
            # 2. Initialize Arrays
            cumulative = np.zeros((target_height, target_width), dtype=np.float32)
            circular_mask = np.zeros((target_height, target_width), dtype=np.bool_)
            
            # Universal meshgrid for clipping
            r_full, c_full = np.ogrid[:target_height, :target_width]
            
            # 3. Process each viewshed
            for pt_idx, vs_file in viewshed_files:
                if not os.path.exists(vs_file): continue
                vs_ds = gdal.Open(vs_file, gdal.GA_ReadOnly)
                if not vs_ds: continue
                
                vs_band = vs_ds.GetRasterBand(1)
                vs_nodata = vs_band.GetNoDataValue()
                vs_data = vs_band.ReadAsArray().astype(np.float32)
                
                # [v1.6.12] Simplified Merging (Aligning is already handled by gdal:warpreproject)
                v_h, v_w = vs_data.shape
                h_overlap = min(target_height, v_h)
                w_overlap = min(target_width, v_w)
                
                # Define val_to_add for cumulative mode
                if not union_mode:
                    val_to_add = 1 if is_count_mode else (2 ** min(pt_idx, 30))
                
                # [v1.6.14] Always calculate circular_mask for buffer-shape boundary
                pt, pt_crs = observer_points[pt_idx]
                pt_dem = self.transform_point(pt, pt_crs, dem_layer.crs())
                c_col = (pt_dem.x() - target_xmin) / dem_xres
                c_row = (target_ymax - pt_dem.y()) / dem_yres
                rad_pix = max_dist / dem_xres
                point_mask = ((c_full - c_col)**2 + (r_full - c_row)**2 <= rad_pix**2)
                circular_mask |= point_mask
                
                # Robust Visibility Detection
                if union_mode:
                    vis_mask = (vs_data[:h_overlap, :w_overlap] > 0.5)
                else:
                    vis_mask = (vs_data[:h_overlap, :w_overlap] > 0.5) & point_mask[:h_overlap, :w_overlap]
                
                if vs_nodata is not None:
                    vis_mask &= (vs_data[:h_overlap, :w_overlap] != vs_nodata)
                
                if union_mode:
                    cumulative[:h_overlap, :w_overlap][vis_mask] = 255
                else:
                    cumulative[:h_overlap, :w_overlap][vis_mask] += val_to_add
                    
                vs_ds = None
            
            # 4. Final NoData masking
            # [v1.6.14] Apply circular buffer masking for ALL modes
            nodata_value = -9999
            cumulative[~circular_mask] = nodata_value
            
            # Save Result
            driver = gdal.GetDriverByName('GTiff')
            out_ds = driver.Create(output_path, target_width, target_height, 1, gdal.GDT_Float32)
            out_ds.SetGeoTransform((target_xmin, dem_xres, 0, target_ymax, 0, -dem_yres))
            out_ds.SetProjection(dem_proj)
            band = out_ds.GetRasterBand(1)
            band.SetNoDataValue(nodata_value)
            band.WriteArray(cumulative)
            out_ds = None
            return True
        except Exception as e:
            print(f"NumPy combine error: {e}")
            import traceback; traceback.print_exc()
            return False
    
    def run_multi_viewshed(self, dem_layer, obs_height, tgt_height, max_dist, curvature, refraction, refraction_coeff=0.13):
        """Run cumulative viewshed from multiple observer points
        
        Combines points from multiple sources:
        1. Point layer: all points from selected layer
        2. Line/Polygon layer: points generated along boundary at interval
        3. Manual clicks: additional points added by user
        
        Creates a raster where cell values indicate how many observer points
        can see that location. Color-coded from red (1 point) to green (all points).
        """
        points = [] # Start empty, we'll collect from all sources as (pt, crs)
        interval = self.spinLineInterval.value()
        canvas_crs = self.canvas.mapSettings().destinationCrs()

        # 1. Add manual clicks
        for p in self.observer_points:
            points.append((p, canvas_crs))
        if self.observer_point: # Also check the single selection if any
            points.append((self.observer_point, canvas_crs))
        
        # [v1.6.16] Handle manually drawn lines (from Line Viewshed tool)
        if hasattr(self, 'drawn_line_points') and self.drawn_line_points and len(self.drawn_line_points) >= 2:
            pts_for_geom = list(self.drawn_line_points)
            if getattr(self, 'is_line_closed', False):
                pts_for_geom.append(self.drawn_line_points[0])
            
            line_geom = QgsGeometry.fromPolylineXY(pts_for_geom)
            length = line_geom.length()
            
            if length > 0:
                num_pts = max(1, int(length / interval))
                for i in range(num_pts + 1):
                    frac = i / num_pts if num_pts > 0 else 0
                    pt = line_geom.interpolate(frac * length)
                    if pt and not pt.isEmpty():
                        points.append((pt.asPoint(), canvas_crs))
        
        # 2. Add points from layer if selected
        if self.radioFromLayer.isChecked():
            obs_layer = self.cmbObserverLayer.currentLayer()
            if obs_layer:
                # [v1.6.02] Auto-hide labels to reduce clutter
                try: obs_layer.setLabelsEnabled(False)
                except: pass
                
                # Use selection if exists
                selected_features = obs_layer.selectedFeatures()
                target_features = selected_features if selected_features else obs_layer.getFeatures()
                
                for feat in target_features:
                    geom = feat.geometry()
                    if not geom or geom.isEmpty(): continue
                    
                    if geom.type() == QgsWkbTypes.PointGeometry:
                        if geom.isMultipart():
                            for pt in geom.asMultiPoint():
                                points.append((pt, obs_layer.crs()))
                        else:
                            points.append((geom.asPoint(), obs_layer.crs()))
                    
                    elif geom.type() == QgsWkbTypes.LineGeometry:
                        length = geom.length()
                        num_pts = max(1, int(length / interval))
                        for i in range(num_pts + 1):
                            frac = i / num_pts if num_pts > 0 else 0
                            pt = geom.interpolate(frac * length).asPoint()
                            points.append((pt, obs_layer.crs()))
                    
                    elif geom.type() == QgsWkbTypes.PolygonGeometry:
                        if geom.isMultipart():
                            polygons = geom.asMultiPolygon()
                        else:
                            polygons = [geom.asPolygon()]
                        
                        for polygon in polygons:
                            if polygon and len(polygon) > 0:
                                exterior_ring = polygon[0]
                                ring_geom = QgsGeometry.fromPolylineXY(exterior_ring)
                                length = ring_geom.length()
                                num_pts = max(1, int(length / interval))
                                for i in range(num_pts + 1):
                                    frac = i / num_pts if num_pts > 0 else 0
                                    pt = ring_geom.interpolate(frac * length).asPoint()
                                    points.append((pt, obs_layer.crs()))
        
        if not points or len(points) < 1:
            push_message(self.iface, "오류", "관측점이 최소 1개 이상 필요합니다", level=2)
            restore_ui_focus(self)
            return

        # [v1.5.85] Robust point management for cumulative analysis
        total_needed = len(points)
        # [v1.6.15] Use UI spinMaxPoints value, default 50
        MAX_POINTS = 50
        if hasattr(self, 'spinMaxPoints'):
            MAX_POINTS = self.spinMaxPoints.value() 
        
        if total_needed > MAX_POINTS:
            from qgis.PyQt.QtWidgets import QMessageBox
            msg = QMessageBox(self)
            msg.setIcon(QMessageBox.Warning)
            msg.setWindowTitle("관측점 개수 경고")
            msg.setText(f"⚠️ 전체 분석에 {total_needed}개의 관측점이 포함되어 있습니다.\n"
                       f"성능을 위해 기본적으로 {MAX_POINTS}개로 제한됩니다.")
            msg.setInformativeText(f"고해상도 DEM과 많은 관측점은 수 분 이상 소요될 수 있습니다.\n\n"
                                  f"• 예(Yes): {MAX_POINTS}개로 축소하여 안전하게 진행\n"
                                  f"• 아니오(No): 전체 {total_needed}개 분석 (매우 느림)\n"
                                  f"• 취소(Cancel): 취소 및 설정으로 복귀")
            msg.setStandardButtons(QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel)
            msg.setDefaultButton(QMessageBox.Yes)
            
            res_msg = msg.exec_()
            if res_msg == QMessageBox.Cancel:
                self.show()
                self.raise_()
                self.activateWindow()
                return
            elif res_msg == QMessageBox.Yes:
                step = len(points) // MAX_POINTS
                points = points[::step][:MAX_POINTS]
                self.iface.messageBar().pushMessage("알림", f"관측점이 {len(points)}개로 샘플링되었습니다.", level=1)
            else:
                self.iface.messageBar().pushMessage("경고", f"{total_needed}개 전체 점에 대해 분석을 시작합니다. 처리 중 QGIS가 응답하지 않을 수 있습니다.", level=1)

        # [v1.5.97] Hide dialog ONLY after all warnings and user decisions
        self.hide()
        QtWidgets.QApplication.processEvents()

        # Setup progress dialog
        progress = QtWidgets.QProgressDialog("다중점 가시권 분석 초기화 중...", "취소", 0, len(points), self)
        progress.setWindowModality(QtCore.Qt.WindowModal)
        progress.show()
        QtWidgets.QApplication.processEvents() # Ensure visibility
        # [v1.5.65] Smart Analysis Extent Optimization
        total_obs_ext = QgsRectangle()
        total_obs_ext.setMinimal()
        for pt, p_crs in points:
            pt_dem = self.transform_point(pt, p_crs, dem_layer.crs())
            total_obs_ext.combineExtentWith(pt_dem.x(), pt_dem.y())
        
        smart_ext = QgsRectangle(
            total_obs_ext.xMinimum() - max_dist * 1.2, total_obs_ext.yMinimum() - max_dist * 1.2,
            total_obs_ext.xMaximum() + max_dist * 1.2, total_obs_ext.yMaximum() + max_dist * 1.2
        )
        final_ext = smart_ext.intersect(dem_layer.extent())
        if final_ext.isEmpty(): final_ext = dem_layer.extent()

        # [v1.5.75] Unified Grid Snapping - Calculate ONCE for both Warp and NumPy
        res = dem_layer.rasterUnitsPerPixelX()
        dem_ext = dem_layer.extent()
        
        # Snap the combined analysis extent to the DEM's pixel grid
        # snap_xmin = dem_origin_x + N * res
        snap_xmin = dem_ext.xMinimum() + math.floor((final_ext.xMinimum() - dem_ext.xMinimum()) / res) * res
        snap_ymax = dem_ext.yMaximum() - math.floor((dem_ext.yMaximum() - final_ext.yMaximum()) / res) * res
        snap_xmax = dem_ext.xMinimum() + math.ceil((final_ext.xMaximum() - dem_ext.xMinimum()) / res) * res
        snap_ymin = dem_ext.yMaximum() - math.ceil((dem_ext.yMaximum() - final_ext.yMinimum()) / res) * res
        
        target_rect = QgsRectangle(snap_xmin, snap_ymin, snap_xmax, snap_ymax)
        target_extent_str = f"{snap_xmin},{snap_ymin},{snap_xmax},{snap_ymax}"
        t_width = int(round((snap_xmax - snap_xmin) / res))
        t_height = int(round((snap_ymax - snap_ymin) / res))
        
        grid_info = {
            'xmin': snap_xmin, 'ymax': snap_ymax, 'xmax': snap_xmax, 'ymin': snap_ymin,
            'res': res, 'width': t_width, 'height': t_height
        }
        
        # Diagnostic Log
        self.iface.messageBar().pushMessage(
            "분석 정보", 
            f"스마트 범위 적용: {final_ext.width():.1f}x{final_ext.height():.1f}m (전체 대비 { (final_ext.area()/dem_ext.area())*100:.1f}%)", 
            level=0
        )

        temp_outputs = []
        viewshed_results = []
        for i, (point, p_crs) in enumerate(points):
            if progress.wasCanceled():
                break
            progress.setValue(i)
            QtWidgets.QApplication.processEvents()
            
            output_raw = os.path.join(tempfile.gettempdir(), f'archt_vs_raw_{i}_{uuid.uuid4().hex[:8]}.tif')
            pt_dem = self.transform_point(point, p_crs, dem_layer.crs())
            
            try:
                processing.run("gdal:viewshed", {
                    'INPUT': dem_layer.source(), 'BAND': 1, 'OBSERVER': f"{pt_dem.x()},{pt_dem.y()}",
                    'OBSERVER_HEIGHT': obs_height, 'TARGET_HEIGHT': tgt_height, 'MAX_DISTANCE': max_dist,
                    'CC': 1 if curvature else 0, 'IV': refraction_coeff if refraction else 0, 'OUTPUT': output_raw
                })
                
                if os.path.exists(output_raw):
                    temp_outputs.append(output_raw)
                    full_vs = os.path.join(tempfile.gettempdir(), f'archt_fullvs_{i}_{uuid.uuid4().hex[:8]}.tif')
                    try:
                        # [v1.6.11] ENSURE PERFECT ALIGNMENT: Warp each result to the combined target extent.
                        processing.run("gdal:warpreproject", {
                            'INPUT': output_raw, 
                            'TARGET_EXTENT': target_rect, 
                            'TARGET_EXTENT_CRS': dem_layer.crs().authid(),
                            'NODATA': -9999, 'TARGET_RESOLUTION': res, 'RESAMPLING': 0, 'DATA_TYPE': 5, 'OUTPUT': full_vs
                        })
                        if os.path.exists(full_vs):
                            temp_outputs.append(full_vs)
                            viewshed_results.append((i, full_vs))
                            try: os.remove(output_raw)
                            except: pass
                    except: pass
            except:
                continue
        
        progress.setValue(len(points))
        
        if not viewshed_results:
            self.iface.messageBar().pushMessage("오류", "유효한 가시권 분석 결과를 생성하지 못했습니다. 보간 또는 범위 설정을 확인하세요.", level=2)
            self.show()
            return
        
        # Combine all viewsheds by summing (cumulative viewshed)
        # Using a safer approach with processing.run("gdal:merge")
        final_output = os.path.join(tempfile.gettempdir(), f'archtoolkit_viewshed_cumulative_{int(time.time())}.tif')
        
        try:
            # [v1.5.68] Optimized Cumulative Viewshed Merge using NumPy
            progress.setLabelText("결과 통합 중 (NumPy)...")
            QtWidgets.QApplication.processEvents()
            
            # [v1.6.02] Determine Union Mode (Binary Visibility)
            # If Line Viewshed or > 20 points, use Union Mode (0/1) instead of bit-flags
            is_line_mode = self.radioLineViewshed.isChecked()
            is_union_mode = is_line_mode or len(points) > 20
            
            mode_str = "합집합(Union)" if is_union_mode else "누적(Cumulative)"
            self.iface.messageBar().pushMessage("분석 시작", f"모드: {mode_str}, 점 개수: {len(points)}", level=0)
            
            # viewshed_results is already [(idx, filepath), ...] as needed by combine_viewsheds_numpy
            success = self.combine_viewsheds_numpy(
                dem_layer=dem_layer,
                viewshed_files=viewshed_results,
                output_path=final_output,
                observer_points=points,
                max_dist=max_dist,
                is_count_mode=False,
                grid_info=grid_info,
                union_mode=is_union_mode
            )
            
            if not success or not os.path.exists(final_output):
                raise Exception("누적 가시권 결과 생성 실패 (NumPy)")
            
            # Clean up intermediate vs files
            cleanup_files(temp_outputs)
    
            
            
            # Add result to map
            layer_name = f"가시권_누적_{len(points)}개점"
            viewshed_layer = QgsRasterLayer(final_output, layer_name)
            
            if viewshed_layer.isValid():
                # Apply result style
                if is_union_mode:
                    self.apply_viewshed_style(viewshed_layer)
                else:
                    self.apply_cumulative_style(viewshed_layer, len(points))
                
                # [v1.5.80] Always create a numbered observer layer for cumulative analysis.
                # This ensures Point 1, 2, 3... are clearly visible and match the legend V(1,2).
                observer_layer = self.create_observer_layer("누적가시권_관측점", points)
                
                QgsProject.instance().addMapLayer(viewshed_layer)
                self.last_result_layer_id = viewshed_layer.id()
                
                # [v1.6.18] Link observer layer for cleanup when viewshed layer is deleted
                if observer_layer:
                    self.result_observer_layer_map[viewshed_layer.id()] = observer_layer.id()
                
                # Ensure label layer is on top
                self.update_layer_order()
                
                # Link markers and annotations [v1.6.02]
                current_annotations = list(self.point_labels)
                self.link_current_marker_to_layer(viewshed_layer.id(), points, annotations=current_annotations)
                self.point_labels = [] # Ownership transferred
                
                self.iface.messageBar().pushMessage(
                    "완료", 
                    f"누적 가시권 분석 완료 ({len(points)}개 관측점)", 
                    level=0
                )

                self.accept()
            else:
                push_message(self.iface, "오류", "결과 레이어 로드 실패", level=2)
                restore_ui_focus(self)
        except Exception as e:
            push_message(self.iface, "오류", f"병합 중 오류: {str(e)}", level=2)
            restore_ui_focus(self)
        finally:
            if 'progress' in locals():
                progress.close()
            cleanup_files(temp_outputs)
    
    def apply_frequency_style(self, layer, max_count):
        """Apply a standard color ramp (Viridis-like) for frequency count analysis"""
        shader = QgsRasterShader()
        color_ramp = QgsColorRampShader()
        color_ramp.setColorRampType(QgsColorRampShader.Interpolated)
        
        layer.dataProvider().setNoDataValue(1, -9999)
        
        # Get user-defined "Not Visible" color
        not_visible_color = self.btnNotVisibleColor.color()
        if not_visible_color.alpha() == 255:
            not_visible_color.setAlpha(0) # Transparent background for frequency
            
        # Standard Red-Yellow-Cyan ramp
        # 0: Transparent
        # 1: Red (Rarely seen)
        # Max/2: Yellow
        # Max: Cyan/Green (Frequently seen)
        
        colors = [
            QgsColorRampShader.ColorRampItem(0, not_visible_color, "보이지 않음 (0)"),
            QgsColorRampShader.ColorRampItem(1, QColor(255, 0, 0, 180), "1개소 관측 (최소)"),
            QgsColorRampShader.ColorRampItem(max_count / 2, QColor(255, 255, 0, 180), f"{max_count/2:.1f}개소 중첩"),
            QgsColorRampShader.ColorRampItem(max_count, QColor(0, 255, 255, 180), f"{max_count}개소 관측 (최대)")
        ]
        
        color_ramp.setColorRampItemList(colors)
        shader.setRasterShaderFunction(color_ramp)
        
        renderer = QgsSingleBandPseudoColorRenderer(layer.dataProvider(), 1, shader)
        renderer.setClassificationMax(max_count)
        renderer.setClassificationMin(0)
        layer.setRenderer(renderer)
        layer.setOpacity(0.8)
        layer.triggerRepaint()

    def apply_cumulative_style(self, layer, num_points):
        """Apply bit-flag based styling for cumulative viewshed
        
        Values: binary combination of observers (1, 2, 4, 8...)
        """
        shader = QgsRasterShader()
        color_ramp = QgsColorRampShader()
        color_ramp.setColorRampType(QgsColorRampShader.Discrete)
        
        layer.dataProvider().setNoDataValue(1, -9999)
        
        # Get user-defined "Not Visible" color
        not_visible_color = self.btnNotVisibleColor.color()
        if not_visible_color.alpha() == 255:
            not_visible_color.setAlpha(180)
            
        colors = [
            QgsColorRampShader.ColorRampItem(0, not_visible_color, "보이지 않음"),
        ]
        
        # Custom discrete color mixing for v1.5.5
        # Primary base colors for up to 8 observers
        base_colors = [
            QColor(255, 0, 0, 200),   # 1: Red
            QColor(0, 255, 0, 200),   # 2: Green
            QColor(0, 0, 255, 200),   # 3: Blue
            QColor(255, 255, 0, 200), # 4: Yellow
            QColor(255, 0, 255, 200), # 5: Magenta
            QColor(0, 255, 255, 200), # 6: Cyan
            QColor(255, 128, 0, 200), # 7: Orange
            QColor(128, 0, 255, 200)  # 8: Purple
        ]
        
        # Limit discrete entries to avoid lag (up to 128 combinations)
        max_combinations = min(2**num_points, 128)
        
        for v in range(1, max_combinations):
            # Find which points see this pixel
            component_colors = []
            seen_pts = []
            for i in range(num_points):
                if v & (1 << i):
                    seen_pts.append(str(i + 1))
                    if i < len(base_colors):
                        component_colors.append(base_colors[i])
            
            count = len(seen_pts)
            label = f"V({','.join(seen_pts)})"
            if count > 1:
                label += f" - {count}개소 중첩"
            else:
                label += " - 가시"
                
            # Mixed color logic
            if not component_colors:
                # Fallback for many points
                r, g, b = (v * 43) % 256, (v * 87) % 256, (v * 123) % 256
                mixed_color = QColor(r, g, b, 200)
            elif len(component_colors) == 1:
                mixed_color = component_colors[0]
            else:
                # Average components for intuitive mixing (Red + Green = Yellow-ish)
                avg_r = sum(c.red() for c in component_colors) // len(component_colors)
                avg_g = sum(c.green() for c in component_colors) // len(component_colors)
                avg_b = sum(c.blue() for c in component_colors) // len(component_colors)
                mixed_color = QColor(avg_r, avg_g, avg_b, 200)
            
            colors.append(QgsColorRampShader.ColorRampItem(v, mixed_color, label))
            
        color_ramp.setColorRampItemList(colors)
        shader.setRasterShaderFunction(color_ramp)
        
        renderer = QgsSingleBandPseudoColorRenderer(layer.dataProvider(), 1, shader)
        renderer.setClassificationMax(2**num_points - 1)
        renderer.setClassificationMin(0)
        layer.setRenderer(renderer)
        layer.setOpacity(0.7)
        layer.triggerRepaint()
    
    def apply_higuchi_style(self, layer, observer_point, max_dist, dem_layer):
        """Apply Higuchi (1975) distance-based landscape zone styling"""
        # Higuchi zones (in meters)
        NEAR_LIMIT = 500      # 근경: 세부 질감 인지
        MIDDLE_LIMIT = 2500   # 중경: 실루엣 파악
        
        # Set NoData value to ensure corners are transparent
        layer.dataProvider().setNoDataValue(1, -9999)
        
        shader = QgsRasterShader()
        color_ramp = QgsColorRampShader()
        color_ramp.setColorRampType(QgsColorRampShader.Discrete)
        
        colors = [
            QgsColorRampShader.ColorRampItem(0, QColor(255, 255, 255, 0), "보이지 않음"),
            QgsColorRampShader.ColorRampItem(85, QColor(255, 50, 50, 200), f"근경 (0~500m: 질감/세부 인지)"),     # Sharp Red
            QgsColorRampShader.ColorRampItem(170, QColor(255, 165, 0, 200), f"중경 (500m~2.5km: 형태/부피 파악)"), # Orange
            QgsColorRampShader.ColorRampItem(255, QColor(138, 43, 226, 200), f"원경 (2.5km~: 실루엣/스카이라인)"), # Purple/Blue
        ]
        
        color_ramp.setColorRampItemList(colors)
        shader.setRasterShaderFunction(color_ramp)
        
        renderer = QgsSingleBandPseudoColorRenderer(layer.dataProvider(), 1, shader)
        layer.setRenderer(renderer)
        layer.setOpacity(0.7)
        layer.triggerRepaint()
        
        # Create distance-based zone rings as vector overlay
        self.create_higuchi_rings(observer_point, max_dist, dem_layer)
    
    def on_higuchi_toggled(self, checked):
        """Suggest parameters suited for Higuchi analysis"""
        if checked:
            # Higuchi zones need at least 2.5km (preferably 5km)
            current_dist = self.spinMaxDistance.value()
            if current_dist < 5000:
                self.spinMaxDistance.setValue(5000)
                self.iface.messageBar().pushMessage(
                    "히구치 분석 안내", 
                    "히구치 거리대 분석을 위해 권장 반경인 5,000m로 자동 조정되었습니다.",
                    level=0
                )
    
    def create_higuchi_rings(self, center_point, max_dist, dem_layer):
        """Create buffer rings showing Higuchi distance zones"""
        
        # Use DEM CRS instead of hardcoded EPSG:5186
        layer = QgsVectorLayer("LineString?crs=" + dem_layer.crs().authid(), "히구치_거리대", "memory")
        pr = layer.dataProvider()
        
        # We need point in DEM CRS for buffer
        center_dem = self.transform_to_dem_crs(center_point, dem_layer)
        zones = [
            (500, "근경 (500m)", QColor(255, 80, 80)),      # Red
            (2500, "중경 (2.5km)", QColor(255, 200, 0)),    # Yellow
        ]
        
        # Add far zone only if max_dist is larger
        if max_dist > 2500:
            zones.append((max_dist, f"원경 ({max_dist/1000:.1f}km)", QColor(50, 200, 50))) # Green
        
        # Create ring features
        for distance, zone_name, color in zones:
            if distance <= max_dist:
                # Create circular buffer
                center_geom = QgsGeometry.fromPointXY(center_dem)
                buffer_geom = center_geom.buffer(distance, 64)
                
                # Robustly get the exterior ring (handling potential MultiPolygon from buffer)
                if buffer_geom.isEmpty():
                    continue
                    
                # buffer_geom of a point should be a Polygon, but let's be safe
                if buffer_geom.isMultipart():
                    parts = buffer_geom.asMultiPolygon()
                    if not parts:
                        continue
                    # Take the exterior ring of the first part
                    rings = parts[0]
                else:
                    rings = buffer_geom.asPolygon()
                
                if rings and len(rings) > 0:
                    exterior_ring = rings[0]
                    ring_geom = QgsGeometry.fromPolylineXY(exterior_ring)
                    feat = QgsFeature(layer.fields())
                    feat.setGeometry(ring_geom)
                    feat.setAttributes([zone_name, int(distance)])
                    pr.addFeature(feat)
        
        layer.updateExtents()
        
        # Apply categorized styling
        categories = []
        for distance, zone_name, color in zones:
            if distance <= max_dist:
                symbol = QgsLineSymbol.createSimple({
                    'color': color.name(),
                    'width': '1.5',
                    'line_style': 'dash'
                })
                category = QgsRendererCategory(zone_name, symbol, zone_name)
                categories.append(category)
        
        if categories:
            renderer = QgsCategorizedSymbolRenderer("zone", categories)
            layer.setRenderer(renderer)
        
        QgsProject.instance().addMapLayers([layer])
    
    def apply_viewshed_style(self, layer):
        """Apply a binary visibility style to viewshed raster
        
        gdal:viewshed output:
        - 0 = Not visible
        - 255 = Visible
        """
        # Set NoData to -9999 so 0 is treated as valid data (Not Visible = Pink)
        layer.dataProvider().setNoDataValue(1, -9999)
        
        shader = QgsRasterShader()
        color_ramp = QgsColorRampShader()
        color_ramp.setColorRampType(QgsColorRampShader.Discrete)
        
        # Get user-defined colors from UI
        visible_color = self.btnVisibleColor.color()
        if visible_color.alpha() == 255:
            visible_color.setAlpha(180)
            
        not_visible_color = self.btnNotVisibleColor.color()
        if not_visible_color.alpha() == 255:
            not_visible_color.setAlpha(180)
            
        # gdal:viewshed outputs 0=not visible, 255=visible
        colors = [
            QgsColorRampShader.ColorRampItem(0, not_visible_color, "보이지 않음"),
            QgsColorRampShader.ColorRampItem(255, visible_color, "보임")
        ]
        color_ramp.setColorRampItemList(colors)
        shader.setRasterShaderFunction(color_ramp)
        
        renderer = QgsSingleBandPseudoColorRenderer(layer.dataProvider(), 1, shader)
        layer.setRenderer(renderer)
        layer.setOpacity(0.7)
        layer.triggerRepaint()

    def link_current_marker_to_layer(self, layer_id, active_points_with_crs=None, annotations=None):
        """Register point markers and annotations to be cleaned up when layer_id is removed.
        Ensures points are transformed to Canvas CRS for visibility.
        """
        result_marker = QgsRubberBand(self.canvas, QgsWkbTypes.PointGeometry)
        result_marker.setColor(QColor(255, 0, 0, 200)) # Semi-transparent red
        # ... (rest of rubberband setup is same, skipping lines for brevity if possible, but replace needs context)
        result_marker.setWidth(2)
        result_marker.setIconSize(4) # Small dots
        result_marker.setIcon(QgsRubberBand.ICON_CIRCLE)
        
        canvas_crs = self.canvas.mapSettings().destinationCrs()
        
        # If specific points with CRS are passed, transform them to canvas
        if active_points_with_crs:
            for pt, p_crs in active_points_with_crs:
                # Transform to canvas CRS for correct display
                pt_canvas = self.transform_point(pt, p_crs, canvas_crs)
                result_marker.addPoint(pt_canvas)
        else:
            # Fallback for manual map clicks (already in Canvas CRS)
            if self.observer_point:
                result_marker.addPoint(self.observer_point)
            for p in self.observer_points:
                result_marker.addPoint(p)
            
        if layer_id not in self.result_marker_map:
            self.result_marker_map[layer_id] = []
        self.result_marker_map[layer_id].append(result_marker)
        result_marker.show()
        
        # [v1.6.02] Store text annotations
        if annotations:
            if layer_id not in self.result_annotation_map:
                self.result_annotation_map[layer_id] = []
            self.result_annotation_map[layer_id].extend(annotations)
    
    def accept(self):
        """Close dialog after successful analysis - keep only result markers visible"""
        # Clear the transient selection markers immediately
        self.point_marker.reset(QgsWkbTypes.PointGeometry)
        
        # Reset state for next use
        self.observer_points = []
        self.observer_point = None
        self.target_point = None
        self.los_click_count = 0
        super().accept()
    
    def reject(self):
        """Clear markers on cancel (no analysis run)"""
        self.point_marker.reset(QgsWkbTypes.PointGeometry)
        self.observer_points = []
        self.observer_point = None
        self.target_point = None
        self.los_click_count = 0
        # Ensure indicator is hidden if tool was active
        if self.map_tool:
            try: self.map_tool.snap_indicator.setMatch(QgsPointLocator.Match())
            except: pass
        super().reject()
    
    def closeEvent(self, event):
        """Clean up when dialog closes via X button"""
        self.point_marker.reset(QgsWkbTypes.PointGeometry)
        if self.original_tool:
            self.canvas.setMapTool(self.original_tool)
        event.accept()


class ViewshedPointTool(QgsMapToolEmitPoint):
    """Map tool for selecting viewshed observer point with snapping support"""
    
    def __init__(self, canvas, dialog):
        super().__init__(canvas)
        self.dialog = dialog
        self.snap_indicator = QgsSnapIndicator(canvas)
    
    def canvasMoveEvent(self, event):
        """Show snapping indicator"""
        res = self.canvas().snappingUtils().snapToMap(event.pos())
        if res.isValid():
            self.snap_indicator.setMatch(res)
        else:
            self.snap_indicator.setMatch(QgsPointLocator.Match())
    
    def canvasReleaseEvent(self, event):
        if event.button() == Qt.RightButton:
            self.finish_selection()
            return
        
        res = self.canvas().snappingUtils().snapToMap(event.pos())
        if res.isValid():
            point = res.point()
        else:
            point = self.toMapCoordinates(event.pos())
        
        self.dialog.set_observer_point(point)
    
    def keyPressEvent(self, event):
        from qgis.PyQt.QtCore import Qt
        if event.key() == Qt.Key_Escape:
            self.finish_selection()
    
    def finish_selection(self):
        """Finish point selection and return to dialog"""
        self.snap_indicator.setMatch(QgsPointLocator.Match())
        if self.dialog.original_tool:
            self.dialog.canvas.setMapTool(self.dialog.original_tool)
        self.dialog.show()
    
    def deactivate(self):
        self.snap_indicator.setMatch(QgsPointLocator.Match())
        super().deactivate()


class ViewshedLineTool(QgsMapToolEmitPoint):
    """Map tool for drawing a polyline on the map. Click to add vertices, right-click to finish."""
    
    def __init__(self, canvas, dialog):
        super().__init__(canvas)
        self.dialog = dialog
        self.snap_indicator = QgsSnapIndicator(canvas)
        self.points = []
        self.rubber_band = QgsRubberBand(canvas, QgsWkbTypes.LineGeometry)
        self.rubber_band.setColor(QColor(0, 100, 255, 180))
        self.rubber_band.setWidth(2)
    
    def canvasMoveEvent(self, event):
        res = self.canvas().snappingUtils().snapToMap(event.pos())
        if res.isValid():
            self.snap_indicator.setMatch(res)
            mouse_pt = res.point()
        else:
            self.snap_indicator.setMatch(QgsPointLocator.Match())
            mouse_pt = self.toMapCoordinates(event.pos())
        
        # UX Enhancement: Visual feedback for line closure
        is_near_start = False
        if len(self.points) >= 2:
            start_px = self.toCanvasCoordinates(self.points[0])
            curr_px = event.pos()
            dist = math.sqrt((start_px.x() - curr_px.x())**2 + (start_px.y() - curr_px.y())**2)
            if dist < 30: # 30px threshold
                mouse_pt = self.points[0] # Snap exactly to start
                is_near_start = True
        
        if self.points:
            self.rubber_band.reset(QgsWkbTypes.LineGeometry)
            if is_near_start:
                self.rubber_band.setColor(QColor(0, 200, 0, 180)) # Green when snapping for closure
                self.rubber_band.setWidth(3)
            else:
                self.rubber_band.setColor(QColor(0, 100, 255, 180)) # Normal blue
                self.rubber_band.setWidth(2)
                
            for pt in self.points:
                self.rubber_band.addPoint(pt)
            self.rubber_band.addPoint(mouse_pt)
    
    def canvasReleaseEvent(self, event):
        from qgis.PyQt.QtCore import Qt
        if event.button() == Qt.RightButton:
            self.finish_line()
            return
        
        res = self.canvas().snappingUtils().snapToMap(event.pos())
        point = res.point() if res.isValid() else self.toMapCoordinates(event.pos())
        
        # Check for snapping to start point (Close Loop)
        if len(self.points) >= 2:
            start_px = self.toCanvasCoordinates(self.points[0])
            curr_px = event.pos()
            # If distance < 30 pixels, close the line
            dist = math.sqrt((start_px.x() - curr_px.x())**2 + (start_px.y() - curr_px.y())**2)
            if dist < 30:
                self.finish_line(close_line=True)
                return
        
        self.points.append(point)
        self.rubber_band.addPoint(point)
    
    def keyPressEvent(self, event):
        from qgis.PyQt.QtCore import Qt
        if event.key() == Qt.Key_Escape:
            self.cleanup()
            self.dialog.show()
        elif event.key() == Qt.Key_C:
            self.finish_line(close_line=True)
        elif event.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.finish_line(close_line=False)
    
    def finish_line(self, close_line=False):
        if len(self.points) >= 2:
            self.dialog.set_line_from_tool(self.points, is_closed=close_line)
            self.cleanup()
            self.dialog.show()
        else:
            self.dialog.iface.messageBar().pushMessage("알림", "최소 2개 점이 필요합니다", level=1)
    
    def cleanup(self):
        self.rubber_band.reset(QgsWkbTypes.LineGeometry)
        self.snap_indicator.setMatch(QgsPointLocator.Match())
        self.points = []
        if self.dialog.original_tool:
            self.dialog.canvas.setMapTool(self.dialog.original_tool)
    
    def deactivate(self):
        self.rubber_band.reset(QgsWkbTypes.LineGeometry)
        self.snap_indicator.setMatch(QgsPointLocator.Match())
        super().deactivate()


class ProfilePlotWidget(QWidget):
    """Custom widget to draw 2D terrain profile for Viewshed Profiler"""
    def __init__(self, profile_data, obs_height, tgt_height, parent=None):
        super().__init__(parent)
        self.profile_data = profile_data
        self.obs_height = obs_height
        self.tgt_height = tgt_height
        self.setMinimumSize(700, 350)
        
    def paintEvent(self, event):
        if not self.profile_data: return
        
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        
        width = self.width()
        height = self.height()
        margin_left = 60
        margin_right = 30
        margin_top = 30
        margin_bottom = 40
        
        plot_w = width - margin_left - margin_right
        plot_h = height - margin_top - margin_bottom
        
        # Data extraction
        distances = [p['distance'] for p in self.profile_data]
        elevations = [p['elevation'] for p in self.profile_data]
        
        max_dist = distances[-1] if distances[-1] > 0 else 1
        obs_elev = elevations[0] + self.obs_height
        tgt_elev = elevations[-1] + self.tgt_height
        
        min_elev = min(elevations) - 5
        max_elev = max(max(elevations), obs_elev, tgt_elev) + 5
        elev_range = max_elev - min_elev if max_elev > min_elev else 10
        
        def to_screen(d, e):
            sx = margin_left + (d / max_dist) * plot_w
            sy = margin_top + plot_h - ((e - min_elev) / elev_range) * plot_h
            return sx, sy

        # --- 1. Draw Axes ---
        painter.setPen(QPen(Qt.black, 1))
        painter.drawLine(margin_left, margin_top + plot_h, margin_left + plot_w, margin_top + plot_h)  # X
        painter.drawLine(margin_left, margin_top, margin_left, margin_top + plot_h)  # Y
        
        # Axis Labels
        painter.setFont(QFont("Arial", 8))
        painter.drawText(margin_left - 5, height - 10, "0")
        painter.drawText(width - margin_right - 40, height - 10, f"{int(max_dist)}m")
        painter.drawText(5, margin_top + plot_h, f"{int(min_elev)}m")
        painter.drawText(5, margin_top + 10, f"{int(max_elev)}m")
        
        # Title
        painter.setFont(QFont("Arial", 10, QFont.Bold))
        painter.drawText(margin_left, 18, "지형 단면 및 가시선 (Terrain Profile & Line of Sight)")
        
        # --- 2. Draw Filled Terrain Polygon ---
        terrain_poly = QPolygonF()
        terrain_poly.append(QPointF(*to_screen(0, min_elev)))  # Bottom-left
        for d, e in zip(distances, elevations):
            terrain_poly.append(QPointF(*to_screen(d, e)))
        terrain_poly.append(QPointF(*to_screen(max_dist, min_elev)))  # Bottom-right
        
        painter.setBrush(QBrush(QColor(139, 119, 101, 200)))  # Brown terrain fill
        painter.setPen(Qt.NoPen)
        painter.drawPolygon(terrain_poly)
        
        # --- 3. Calculate Visibility using Max-Angle Algorithm ---
        # Compute visibility status for each profile point
        visibility = []  # True = Visible, False = Hidden
        max_angle = -float('inf')
        start_elev = elevations[0] + self.obs_height
        
        for i, (d, e) in enumerate(zip(distances, elevations)):
            if d == 0:
                visibility.append(True)  # Observer point is always "visible"
                continue
            
            # Angle from observer to this point's terrain surface
            angle = (e - start_elev) / d
            
            if angle >= max_angle:
                max_angle = angle
                visibility.append(True)
            else:
                visibility.append(False)
        
        # --- 4. Draw Visibility Segments on Terrain Surface ---
        pen_visible = QPen(QColor(0, 200, 0), 1.5)  # Green, thin
        pen_hidden = QPen(QColor(255, 0, 0), 1.5)   # Red, thin
        
        for i in range(len(distances) - 1):
            x1, y1 = to_screen(distances[i], elevations[i])
            x2, y2 = to_screen(distances[i+1], elevations[i+1])
            
            # Use status of the endpoint to determine color
            if visibility[i+1]:
                painter.setPen(pen_visible)
            else:
                painter.setPen(pen_hidden)
            painter.drawLine(QPointF(x1, y1), QPointF(x2, y2))
        
        # --- 5. Draw Shadow Zones (Hidden Dips) ---
        painter.setBrush(QBrush(QColor(80, 80, 80, 60)))  # Very light grey
        painter.setPen(Qt.NoPen)
        
        max_angle_running = -float('inf')
        for i in range(len(distances) - 1):
            d1, e1 = distances[i], elevations[i]
            d2, e2 = distances[i+1], elevations[i+1]
            
            if d1 == 0:
                max_angle_running = (e1 - start_elev + 0.0001) / 0.0001  # Avoid div by zero
                continue
            
            angle1 = (e1 - start_elev) / d1
            angle2 = (e2 - start_elev) / d2 if d2 > 0 else angle1
            
            if angle1 >= max_angle_running:
                max_angle_running = angle1
            
            # If this segment is in shadow
            if angle2 < max_angle_running:
                # Height of the "shadow ceiling" at d2
                shadow_h = start_elev + max_angle_running * d2
                
                # Draw polygon from terrain to shadow ceiling
                p1 = QPointF(*to_screen(d1, e1))
                p2 = QPointF(*to_screen(d2, e2))
                p3 = QPointF(*to_screen(d2, shadow_h))
                p4 = QPointF(*to_screen(d1, start_elev + max_angle_running * d1))
                
                shadow_poly = QPolygonF([p1, p2, p3, p4])
                painter.drawPolygon(shadow_poly)
        
        # --- 6. Draw Sight Line (Dashed Blue) ---
        obs_screen = to_screen(0, obs_elev)
        tgt_screen = to_screen(max_dist, tgt_elev)
        
        painter.setPen(QPen(QColor(0, 100, 255, 150), 1, Qt.DashLine))
        painter.drawLine(QPointF(*obs_screen), QPointF(*tgt_screen))
        
        # --- 7. Draw Start (S) and End (E) Markers ---
        painter.setFont(QFont("Arial", 9, QFont.Bold))
        
        # Observer (Blue Circle with S)
        painter.setBrush(QBrush(QColor(0, 100, 255)))
        painter.setPen(QPen(Qt.white, 1))
        painter.drawEllipse(QPointF(*obs_screen), 8, 8)
        painter.setPen(Qt.white)
        painter.drawText(int(obs_screen[0]) - 4, int(obs_screen[1]) + 4, "S")
        
        # Target (Orange Circle with E)
        painter.setBrush(QBrush(QColor(255, 140, 0)))
        painter.setPen(QPen(Qt.white, 1))
        painter.drawEllipse(QPointF(*tgt_screen), 8, 8)
        painter.setPen(Qt.white)
        painter.drawText(int(tgt_screen[0]) - 4, int(tgt_screen[1]) + 4, "E")
        
        # --- 8. Draw Legend ---
        legend_x = margin_left + 10
        legend_y = margin_top + 10
        painter.setFont(QFont("Arial", 8))
        
        painter.setPen(pen_visible)
        painter.drawLine(legend_x, legend_y, legend_x + 20, legend_y)
        painter.setPen(Qt.black)
        painter.drawText(legend_x + 25, legend_y + 4, "보임 (Visible)")
        
        painter.setPen(pen_hidden)
        painter.drawLine(legend_x, legend_y + 15, legend_x + 20, legend_y + 15)
        painter.setPen(Qt.black)
        painter.drawText(legend_x + 25, legend_y + 19, "안보임 (Hidden)")


class ViewshedProfilerDialog(QDialog):
    """Dialog to show 2D Viewshed Profile chart"""
    def __init__(self, iface, profile_data, obs_height, tgt_height, total_dist, parent=None):
        super().__init__(parent)
        self.setWindowTitle("가시권 프로파일러 (Viewshed Profiler)")
        self.setMinimumSize(800, 500)
        
        layout = QVBoxLayout()
        
        # Info Header
        header = QLabel(f"<b>거리:</b> {total_dist:.1f}m | <b>관측고:</b> {obs_height}m | <b>대상고:</b> {tgt_height}m")
        header.setStyleSheet("font-size: 14px; padding: 10px; background: #f0f0f0; border-radius: 5px;")
        layout.addWidget(header)
        
        # Plot area
        self.plot = ProfilePlotWidget(profile_data, obs_height, tgt_height)
        layout.addWidget(self.plot)
        
        # Footer buttons
        btn_layout = QHBoxLayout()
        btn_save = QPushButton("🖼️ 이미지로 저장 (.png)")
        btn_save.clicked.connect(self.save_image)
        btn_close = QPushButton("닫기")
        btn_close.clicked.connect(self.close)
        
        btn_save.setStyleSheet("padding: 8px 15px; font-weight: bold; background: #4CAF50; color: white; border: none;")
        btn_close.setStyleSheet("padding: 8px 15px;")
        
        btn_layout.addStretch()
        btn_layout.addWidget(btn_save)
        btn_layout.addWidget(btn_close)
        layout.addLayout(btn_layout)
        
        self.setLayout(layout)
        
    def save_image(self):
        filename, _ = QFileDialog.getSaveFileName(self, "이미지 저장", "viewshed_profile.png", "PNG (*.png)")
        if filename:
            # Render the widget to image
            image = QImage(self.plot.size(), QImage.Format_ARGB32)
            # Fill with white background
            image.fill(Qt.white)
            painter = QPainter(image)
            self.plot.render(painter)
            painter.end()
            image.save(filename)
            print(f"Profile saved to {filename}")

