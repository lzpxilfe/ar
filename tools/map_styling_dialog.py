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
Map Styling Tool for ArchToolkit
Applies professional cartographic styles to South Korean Digital Topographic Map layers.
"""
import os
from qgis.PyQt import uic
from qgis.PyQt import QtWidgets
from qgis.PyQt.QtCore import Qt, QVariant, QPointF
from qgis.PyQt.QtGui import QColor, QPainter
from qgis.core import (
    QgsProject, QgsVectorLayer, QgsRasterLayer, QgsMapLayerProxyModel,
    QgsSymbol, QgsLineSymbol, QgsFillSymbol,
    QgsRuleBasedRenderer, QgsSingleSymbolRenderer,
    QgsSimpleLineSymbolLayer, QgsSimpleFillSymbolLayer,
    QgsUnitTypes, QgsField, QgsFeature, QgsGeometry,
    QgsWkbTypes, QgsFeatureRequest,
    QgsSingleBandPseudoColorRenderer, QgsRasterShader, QgsColorRampShader,
    QgsSingleBandGrayRenderer, QgsHillshadeRenderer, QgsContrastEnhancement,
    QgsRasterBandStats, QgsLayerTreeLayer
)

FORM_CLASS, _ = uic.loadUiType(os.path.join(
    os.path.dirname(__file__), 'map_styling_dialog_base.ui'))

class MapStylingDialog(QtWidgets.QDialog, FORM_CLASS):
    
    def __init__(self, iface, parent=None):
        super(MapStylingDialog, self).__init__(parent)
        self.setupUi(self)
        self.iface = iface
        
        # Setup
        self.populate_layers()
        self.cmbDemLayer.setFilters(QgsMapLayerProxyModel.RasterLayer)
        
        # Connect signals
        self.btnSelectAll.clicked.connect(lambda: self.set_all_checks(True))
        self.btnDeselectAll.clicked.connect(lambda: self.set_all_checks(False))
        self.btnApply.clicked.connect(self.apply_styling)
        self.btnClose.clicked.connect(self.close)

    def populate_layers(self):
        """Fill the list widget with vector layers from the project"""
        self.lstLayers.clear()
        layers = QgsProject.instance().mapLayers().values()
        for layer in layers:
            if isinstance(layer, QgsVectorLayer):
                item = QtWidgets.QListWidgetItem(layer.name())
                item.setData(Qt.UserRole, layer.id())
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Unchecked)
                self.lstLayers.addItem(item)

    def set_all_checks(self, state):
        for i in range(self.lstLayers.count()):
            self.lstLayers.item(i).setCheckState(Qt.Checked if state else Qt.Unchecked)

    def get_selected_layers(self):
        selected = []
        for i in range(self.lstLayers.count()):
            item = self.lstLayers.item(i)
            if item.checkState() == Qt.Checked:
                lid = item.data(Qt.UserRole)
                layer = QgsProject.instance().mapLayer(lid)
                if layer:
                    selected.append(layer)
        return selected

    def apply_styling(self):
        source_layers = self.get_selected_layers()
        dem_layer = self.cmbDemLayer.currentLayer()

        if not source_layers and not (self.chkDemStyling.isChecked() and dem_layer):
            QtWidgets.QMessageBox.warning(self, "오류", "시각화를 적용할 레이어를 선택해주세요.")
            return

        try:
            results = []
            
            # 1. Raster Background Styling
            if self.chkDemStyling.isChecked() and isinstance(dem_layer, QgsRasterLayer):
                self.style_dem_background(dem_layer)
                results.append("배경 지형")

            # 2. Vector Styling
            if source_layers:
                tasks = []
                if self.chkRoads.isChecked():
                    tasks.append({
                        'name': "Style: 도로",
                        'codes': ['A0023210','A0023211','A0023212','A0023213','A0023214','A0023215','A0023216','A0023217'],
                        'style_func': self.style_road_layer
                    })
                if self.chkRivers.isChecked():
                    tasks.append({
                        'name': "Style: 하천",
                        'codes': ['E0022110','E0022115','E0022112','E0022113'],
                        'style_func': self.style_river_layer
                    })
                if self.chkBuildings.isChecked():
                    tasks.append({
                        'name': "Style: 건물",
                        'codes': ['B0014110','B0014111','B0014112','B0014113','B0014115'],
                        'style_func': self.style_building_layer
                    })

                # 2.1 Create Vector Group
                vector_group_name = "Style: 도면 데이터"
                root = QgsProject.instance().layerTreeRoot()
                vec_group = root.findGroup(vector_group_name)
                if vec_group:
                    root.removeChildNode(vec_group)
                vec_group = root.insertGroup(0, vector_group_name) # Always top for vector data

                for task in tasks:
                    aggregated_layer = self.aggregate_features(source_layers, task['codes'], task['name'])
                    if aggregated_layer:
                        # Add directly to group (layer was added with addMapLayer(False))
                        layer_node = QgsLayerTreeLayer(aggregated_layer)
                        vec_group.insertChildNode(0, layer_node)  # Insert at top
                        
                        # Apply style
                        task['style_func'](aggregated_layer, 'Layer')
                        results.append(task['name'].replace("Style: ", ""))


                # 3. Move source layers into a hidden sub-group for unified control
                source_group_name = "원본 레이어 (숨김)"
                source_sub_group = vec_group.addGroup(source_group_name)
                
                for sl in source_layers:
                    sl_node = root.findLayer(sl.id())
                    if sl_node:
                        # Clone and move to sub-group
                        new_node = QgsLayerTreeLayer(sl)
                        source_sub_group.addChildNode(new_node)
                        # Remove from original location
                        parent = sl_node.parent()
                        if parent:
                            parent.removeChildNode(sl_node)
                
                # Hide the source sub-group
                source_sub_group.setItemVisibilityChecked(False)

            # Final message
            if results:
                self.iface.messageBar().pushMessage(
                    "시각화 완료", f"통합 레이어가 생성되었습니다: {', '.join(results)}", level=0
                )
                self.accept()
            else:
                QtWidgets.QMessageBox.information(self, "정보", "선택한 레이어들에서 해당하는 데이터를 찾을 수 없습니다.")
                
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "오류", f"스타일 적용 중 오류: {str(e)}")

    def style_dem_background(self, source_raster):
        """Create a 3-layer styled background group from a single DEM"""
        
        group_name = f"Style: 배경 지형 ({source_raster.name()})"
        root = QgsProject.instance().layerTreeRoot()
        
        # Remove existing group if it exists
        existing_group = root.findGroup(group_name)
        if existing_group:
            root.removeChildNode(existing_group)
        
        group = root.addGroup(group_name)
        
        # We want: Color (Top), Gray (Mid), Hillshade (Bottom)
        # Strategy: Add all with addLayer (appends at bottom), then reorder manually.
        # Or: Add in reverse order. Let's add in reverse order so last added is at top.
        
        # 1. Hillshade (should be at bottom, add first)
        hillshade_layer = source_raster.clone()
        hillshade_layer.setName(f"{source_raster.name()}_음영기복")
        hillshade_layer.setRenderer(QgsHillshadeRenderer(hillshade_layer.dataProvider(), 1, 315, 45))
        QgsProject.instance().addMapLayer(hillshade_layer, False)
        group.addLayer(hillshade_layer) 
        
        # 2. Gray Layer (should be in middle, add second - will be on top of hillshade)
        gray_layer = source_raster.clone()
        gray_layer.setName(f"{source_raster.name()}_그레이")
        gray_layer.setRenderer(QgsSingleBandGrayRenderer(gray_layer.dataProvider(), 1))
        gray_layer.setOpacity(0.4)
        gray_layer.setBlendMode(QPainter.CompositionMode_Multiply) 
        QgsProject.instance().addMapLayer(gray_layer, False)
        gray_node = QgsLayerTreeLayer(gray_layer)
        group.insertChildNode(0, gray_node) # Insert at top of group
        
        # 3. Color Layer (should be at top, add last)
        color_layer = source_raster.clone()
        color_layer.setName(f"{source_raster.name()}_고도색상")
        
        stats = color_layer.dataProvider().bandStatistics(1, QgsRasterBandStats.All)
        min_val, max_val = stats.minimumValue, stats.maximumValue
        shader = QgsRasterShader()
        color_ramp = QgsColorRampShader(min_val, max_val)
        color_ramp.setColorRampType(QgsColorRampShader.Discrete)
        items = [
            QgsColorRampShader.ColorRampItem(min_val + (max_val-min_val)*0.0, QColor("#ffffcc"), "<= Min"),
            QgsColorRampShader.ColorRampItem(min_val + (max_val-min_val)*0.25, QColor("#c2e699"), "Low"),
            QgsColorRampShader.ColorRampItem(min_val + (max_val-min_val)*0.5, QColor("#78c679"), "Mid"),
            QgsColorRampShader.ColorRampItem(min_val + (max_val-min_val)*0.75, QColor("#31a354"), "High"),
            QgsColorRampShader.ColorRampItem(max_val, QColor("#006837"), "Max")
        ]
        color_ramp.setColorRampItemList(items)
        shader.setRasterShaderFunction(color_ramp)
        color_layer.setRenderer(QgsSingleBandPseudoColorRenderer(color_layer.dataProvider(), 1, shader))
        color_layer.setOpacity(0.7)
        QgsProject.instance().addMapLayer(color_layer, False)
        color_node = QgsLayerTreeLayer(color_layer)
        group.insertChildNode(0, color_node) # Insert at very top of group


    def detect_code_field(self, layer):
        """Identify which field contains the layer codes"""
        possible_names = ['Layer', 'layer', 'RefName', 'LayerName', 'LAYER']
        fields = [f.name() for f in layer.fields()]
        for name in possible_names:
            if name in fields:
                return name
        return None

    def aggregate_features(self, source_layers, codes, name):
        """Combine matching features from multiple layers into one memory layer"""
        is_building = "건물" in name
        crs = source_layers[0].crs().authid()
        
        dest_geom_type = "MultiPolygon" if is_building else "LineString"
        dest_layer = QgsVectorLayer(f"{dest_geom_type}?crs={crs}", name, "memory")
        pr = dest_layer.dataProvider()
        pr.addAttributes([QgsField("Layer", QVariant.String)])
        dest_layer.updateFields()
        
        all_features = []
        
        for sl in source_layers:
            field_name = self.detect_code_field(sl)
            if not field_name: continue
            
            query = f"\"{field_name}\" IN ({', '.join([f'\'{c}\'' for c in codes])})"
            request = QgsFeatureRequest().setFilterExpression(query)
            
            for feat in sl.getFeatures(request):
                new_feat = QgsFeature(dest_layer.fields())
                code_val = feat.attribute(field_name)
                new_feat.setAttributes([code_val])
                
                geom = feat.geometry()
                if is_building:
                    # Robust polygonization for buildings
                    poly_geom = None
                    if geom.type() == QgsWkbTypes.LineGeometry:
                        try:
                            # Try to create polygon from points
                            if geom.isMultipart():
                                lines = geom.asMultiPolyline()
                                ring = [p for line in lines for p in line]
                                poly_geom = QgsGeometry.fromPolygonXY([ring])
                            else:
                                poly_geom = QgsGeometry.fromPolygonXY([geom.asPolyline()])
                        except:
                            pass
                    
                    if poly_geom and not poly_geom.isNull() and not poly_geom.isEmpty():
                        new_feat.setGeometry(poly_geom)
                    else:
                        # Fallback: buffer line to make polygon
                        try:
                            buffered = geom.buffer(0.01, 2)
                            if buffered and not buffered.isEmpty():
                                new_feat.setGeometry(buffered)
                            else:
                                new_feat.setGeometry(geom)
                        except:
                            new_feat.setGeometry(geom)
                else:
                    new_feat.setGeometry(geom)

                
                all_features.append(new_feat)
        
        if not all_features:
            return None
            
        pr.addFeatures(all_features)
        QgsProject.instance().addMapLayer(dest_layer, False)  # Add to project but NOT to layer tree
        return dest_layer

    def style_road_layer(self, layer, field_name):
        color = QColor("#ff9501")
        road_configs = {
            'A0023211': (1.2, "고속국도"),
            'A0023212': (1.0, "일반국도"),
            'A0023213': (0.8, "지방도"),
            'A0023214': (0.7, "시/군도"),
            'A0023215': (0.5, "면도"),
            'A0023216': (0.4, "소로"),
            'A0023217': (0.3, "도보/길"),
            'A0023210': (0.4, "기타도로")
        }
        
        # Create invisible root rule (ELSE filter catches nothing)
        root_rule = QgsRuleBasedRenderer.Rule(None)  # No symbol for root
        
        for code, (width, label) in road_configs.items():
            sym = QgsLineSymbol.createSimple({'color': color.name(), 'width': str(width)})
            rule = QgsRuleBasedRenderer.Rule(sym, 0, 0, f"\"{field_name}\" = '{code}'", label)
            root_rule.appendChild(rule)
            
        layer.setRenderer(QgsRuleBasedRenderer(root_rule))
        layer.triggerRepaint()

    def style_river_layer(self, layer, field_name):
        color = QColor("#1ea1ff")
        river_configs = {
            'E0022110': (1.0, "하천"),
            'E0022115': (0.4, "수로"),
            'E0022112': (0.7, "소하천"),
            'E0022113': (0.3, "세천")
        }
        
        # Create invisible root rule (ELSE filter catches nothing)
        root_rule = QgsRuleBasedRenderer.Rule(None)  # No symbol for root
        
        for code, (width, label) in river_configs.items():
            sym = QgsLineSymbol.createSimple({'color': color.name(), 'width': str(width)})
            rule = QgsRuleBasedRenderer.Rule(sym, 0, 0, f"\"{field_name}\" = '{code}'", label)
            root_rule.appendChild(rule)
            
        layer.setRenderer(QgsRuleBasedRenderer(root_rule))
        layer.triggerRepaint()

    def style_building_layer(self, layer, field_name):
        offset_val = self.spinOffset.value()
        
        if layer.geometryType() == QgsWkbTypes.PolygonGeometry:
            symbol = QgsFillSymbol.createSimple({
                'color': '#ffffff',
                'outline_color': '#666666',
                'outline_width': '0.1'
            })
            shadow_layer = QgsSimpleFillSymbolLayer()
            shadow_layer.setFillColor(QColor(0, 0, 0, 100))
            shadow_layer.setStrokeColor(Qt.transparent)
            shadow_layer.setOffset(QPointF(offset_val, offset_val))
            shadow_layer.setOffsetUnit(QgsUnitTypes.RenderMillimeters)
            symbol.insertSymbolLayer(0, shadow_layer)
        else:
            symbol = QgsLineSymbol.createSimple({'color': '#ffffff', 'width': '0.3'})
            shadow_layer = QgsSimpleLineSymbolLayer()
            shadow_layer.setColor(QColor(0, 0, 0, 100))
            shadow_layer.setWidth(0.3)
            shadow_layer.setOffset(offset_val) 
            symbol.insertSymbolLayer(0, shadow_layer)

        layer.setRenderer(QgsSingleSymbolRenderer(symbol))
        layer.triggerRepaint()


