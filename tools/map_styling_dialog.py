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
import copy
import json
import os
from datetime import datetime

from qgis.PyQt import uic
from qgis.PyQt import QtWidgets
from qgis.PyQt.QtCore import Qt, QVariant, QPointF, QUrl
from qgis.PyQt.QtGui import QColor, QPainter, QDesktopServices
from qgis.core import (
    QgsProject, QgsVectorLayer, QgsRasterLayer, QgsMapLayerProxyModel,
    QgsLineSymbol, QgsFillSymbol,
    QgsRuleBasedRenderer, QgsSingleSymbolRenderer,
    QgsSimpleLineSymbolLayer, QgsSimpleFillSymbolLayer,
    QgsUnitTypes, QgsField, QgsFeature, QgsGeometry,
    QgsWkbTypes, QgsFeatureRequest,
    QgsSingleBandPseudoColorRenderer, QgsRasterShader, QgsColorRampShader,
    QgsSingleBandGrayRenderer, QgsHillshadeRenderer,
    QgsRasterBandStats, QgsLayerTreeLayer
)
from .utils import new_run_id, restore_ui_focus, push_message, set_archtoolkit_layer_metadata

FORM_CLASS, _ = uic.loadUiType(os.path.join(
    os.path.dirname(__file__), 'map_styling_dialog_base.ui'))

DEFAULT_CODE_CONFIG = {
    "roads": {
        "name": "Style: 도로",
        "color": "#ff9501",
        "rules": [
            {"code": "A0023211", "width_mm": 1.2, "label": "고속국도"},
            {"code": "A0023212", "width_mm": 1.0, "label": "일반국도"},
            {"code": "A0023213", "width_mm": 0.8, "label": "지방도"},
            {"code": "A0023214", "width_mm": 0.7, "label": "시/군도"},
            {"code": "A0023215", "width_mm": 0.5, "label": "면도"},
            {"code": "A0023216", "width_mm": 0.4, "label": "소로"},
            {"code": "A0023217", "width_mm": 0.3, "label": "도보/길"},
            {"code": "A0023210", "width_mm": 0.4, "label": "기타도로"},
        ],
    },
    "rivers": {
        "name": "Style: 하천",
        "color": "#1ea1ff",
        "rules": [
            {"code": "E0022110", "width_mm": 1.0, "label": "하천"},
            {"code": "E0022115", "width_mm": 0.4, "label": "수로"},
            {"code": "E0022112", "width_mm": 0.7, "label": "소하천"},
            {"code": "E0022113", "width_mm": 0.3, "label": "세천"},
        ],
    },
    "buildings": {
        "name": "Style: 건물",
        "codes": ["B0014110", "B0014111", "B0014112", "B0014113", "B0014115"],
        "fill_color": "#ffffff",
        "outline_color": "#666666",
        "outline_width_mm": 0.1,
        "shadow_alpha": 100,
    },
}

class MapStylingDialog(QtWidgets.QDialog, FORM_CLASS):
    
    def __init__(self, iface, parent=None):
        super(MapStylingDialog, self).__init__(parent)
        self.setupUi(self)
        self.iface = iface
        self._style_run_id = None
        
        # Setup
        self.populate_layers()
        self.cmbDemLayer.setFilters(QgsMapLayerProxyModel.RasterLayer)
        self.code_config = self._load_code_config()
        self._sync_code_config_ui()
        
        # Connect signals
        self.btnSelectAll.clicked.connect(lambda: self.set_all_checks(True))
        self.btnDeselectAll.clicked.connect(lambda: self.set_all_checks(False))
        self.btnApply.clicked.connect(self.apply_styling)
        self.btnClose.clicked.connect(self.close)
        if hasattr(self, "btnOpenCodeConfig"):
            self.btnOpenCodeConfig.clicked.connect(self.open_code_config_file)
        if hasattr(self, "btnReloadCodeConfig"):
            self.btnReloadCodeConfig.clicked.connect(self.reload_code_config)
        if hasattr(self, "btnExportPreset"):
            self.btnExportPreset.clicked.connect(self.export_qml_preset)

    def _code_config_path(self):
        return os.path.join(os.path.dirname(__file__), "map_styling_codes.json")

    def _load_code_config(self):
        """Load DXF code/style mapping from JSON (fallback to built-in defaults)."""
        self._code_config_load_error = None
        config = copy.deepcopy(DEFAULT_CODE_CONFIG)
        path = self._code_config_path()

        if not os.path.exists(path):
            self._code_config_load_error = f"매핑 파일이 없습니다: {path}"
            return config

        try:
            with open(path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
        except Exception as e:
            self._code_config_load_error = f"매핑 파일을 읽는 중 오류: {e}"
            return config

        if not isinstance(loaded, dict):
            self._code_config_load_error = "매핑 파일 형식이 올바르지 않습니다(JSON object 필요)."
            return config

        for key in ("roads", "rivers", "buildings"):
            if not isinstance(loaded.get(key), dict):
                continue
            cat = loaded[key]
            if key in ("roads", "rivers"):
                if isinstance(cat.get("name"), str):
                    config[key]["name"] = cat["name"]
                if isinstance(cat.get("color"), str):
                    config[key]["color"] = cat["color"]
                if isinstance(cat.get("rules"), list):
                    rules = []
                    for item in cat["rules"]:
                        if not isinstance(item, dict):
                            continue
                        code = item.get("code")
                        width = item.get("width_mm", item.get("width"))
                        label = item.get("label", "")
                        if not (isinstance(code, str) and code.strip()):
                            continue
                        try:
                            width_f = float(width)
                        except Exception:
                            continue
                        if not isinstance(label, str):
                            label = str(label)
                        rules.append({"code": code.strip(), "width_mm": width_f, "label": label})
                    config[key]["rules"] = rules
            else:
                if isinstance(cat.get("name"), str):
                    config[key]["name"] = cat["name"]
                if isinstance(cat.get("codes"), list):
                    config[key]["codes"] = [str(c) for c in cat["codes"] if str(c).strip()]
                if isinstance(cat.get("fill_color"), str):
                    config[key]["fill_color"] = cat["fill_color"]
                if isinstance(cat.get("outline_color"), str):
                    config[key]["outline_color"] = cat["outline_color"]
                if cat.get("outline_width_mm") is not None:
                    try:
                        config[key]["outline_width_mm"] = float(cat["outline_width_mm"])
                    except Exception:
                        pass
                if cat.get("shadow_alpha") is not None:
                    try:
                        config[key]["shadow_alpha"] = int(cat["shadow_alpha"])
                    except Exception:
                        pass

        return config

    def _sync_code_config_ui(self):
        try:
            if hasattr(self, "lblCodeConfigPath"):
                self.lblCodeConfigPath.setText(self._code_config_path())
        except Exception:
            pass

    def open_code_config_file(self):
        path = self._code_config_path()
        if not os.path.exists(path):
            push_message(self.iface, "정보", f"매핑 파일이 없습니다: {path}", level=1)
            return
        try:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        except Exception:
            push_message(self.iface, "오류", "매핑 파일을 여는 중 오류가 발생했습니다.", level=2)

    def reload_code_config(self):
        self.code_config = self._load_code_config()
        self._sync_code_config_ui()
        if getattr(self, "_code_config_load_error", None):
            push_message(self.iface, "경고", f"기본 매핑으로 대체했습니다: {self._code_config_load_error}", level=1)
        else:
            push_message(self.iface, "완료", "DXF 코드 매핑을 다시 불러왔습니다.", level=0)

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
            push_message(self.iface, "오류", "시각화를 적용할 레이어를 선택해주세요.", level=2)
            restore_ui_focus(self)
            return


        try:
            self._style_run_id = new_run_id("map_styling")
            results = []
            
            # 1. Raster Background Styling
            if self.chkDemStyling.isChecked() and isinstance(dem_layer, QgsRasterLayer):
                self.style_dem_background(dem_layer)
                results.append("배경 지형")

            # 2. Vector Styling
            if source_layers:
                tasks = []
                roads_cfg = self.code_config.get("roads", {})
                rivers_cfg = self.code_config.get("rivers", {})
                buildings_cfg = self.code_config.get("buildings", {})
                if self.chkRoads.isChecked():
                    tasks.append({
                        'name': roads_cfg.get("name", "Style: 도로"),
                        'codes': [r.get("code") for r in roads_cfg.get("rules", []) if isinstance(r, dict) and r.get("code")],
                        'dest_geom': "line",
                        'style_func': self.style_road_layer,
                    })
                if self.chkRivers.isChecked():
                    tasks.append({
                        'name': rivers_cfg.get("name", "Style: 하천"),
                        'codes': [r.get("code") for r in rivers_cfg.get("rules", []) if isinstance(r, dict) and r.get("code")],
                        'dest_geom': "line",
                        'style_func': self.style_river_layer,
                    })
                if self.chkBuildings.isChecked():
                    tasks.append({
                        'name': buildings_cfg.get("name", "Style: 건물"),
                        'codes': buildings_cfg.get("codes", []) if isinstance(buildings_cfg.get("codes"), list) else [],
                        'dest_geom': "polygon",
                        'style_func': self.style_building_layer,
                    })

                # 2.1 Create Vector Group
                vector_group_name = "Style: 도면 데이터"
                root = QgsProject.instance().layerTreeRoot()
                vec_group = root.findGroup(vector_group_name)
                if vec_group:
                    root.removeChildNode(vec_group)
                vec_group = root.insertGroup(0, vector_group_name) # Always top for vector data

                for task in tasks:
                    aggregated_layer = self.aggregate_features(source_layers, task.get('codes', []), task['name'], task.get("dest_geom", "line"))
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
                push_message(self.iface, "시각화 완료", f"통합 레이어가 생성되었습니다: {', '.join(results)}", level=0)
                self.accept()
            else:
                push_message(self.iface, "정보", "선택한 레이어들에서 해당하는 데이터를 찾을 수 없습니다.", level=1)
                restore_ui_focus(self)
                
        except Exception as e:
            push_message(self.iface, "오류", f"스타일 적용 중 오류: {str(e)}", level=2)
            restore_ui_focus(self)


    def style_dem_background(self, source_raster):
        """Create a 3-layer styled background group from a single DEM"""
        run_id = str(getattr(self, "_style_run_id", "") or "").strip() or new_run_id("map_styling")
        
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
        try:
            set_archtoolkit_layer_metadata(
                hillshade_layer,
                tool_id="map_styling",
                run_id=run_id,
                kind="dem_hillshade",
                units="m",
                params={"source": str(source_raster.name() or "")},
            )
        except Exception:
            pass
        QgsProject.instance().addMapLayer(hillshade_layer, False)
        group.addLayer(hillshade_layer) 
        
        # 2. Gray Layer (should be in middle, add second - will be on top of hillshade)
        gray_layer = source_raster.clone()
        gray_layer.setName(f"{source_raster.name()}_그레이")
        gray_layer.setRenderer(QgsSingleBandGrayRenderer(gray_layer.dataProvider(), 1))
        gray_layer.setOpacity(0.4)
        gray_layer.setBlendMode(QPainter.CompositionMode_Multiply) 
        try:
            set_archtoolkit_layer_metadata(
                gray_layer,
                tool_id="map_styling",
                run_id=run_id,
                kind="dem_gray",
                units="m",
                params={"source": str(source_raster.name() or "")},
            )
        except Exception:
            pass
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
        try:
            set_archtoolkit_layer_metadata(
                color_layer,
                tool_id="map_styling",
                run_id=run_id,
                kind="dem_color",
                units="m",
                params={"source": str(source_raster.name() or "")},
            )
        except Exception:
            pass
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

    def aggregate_features(self, source_layers, codes, name, dest_geom="line"):
        """Combine matching features from multiple layers into one memory layer"""
        run_id = str(getattr(self, "_style_run_id", "") or "").strip() or new_run_id("map_styling")
        if not codes:
            return None
        is_building = dest_geom == "polygon"
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
                        except Exception:
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
                        except Exception:
                            new_feat.setGeometry(geom)
                else:
                    new_feat.setGeometry(geom)

                
                all_features.append(new_feat)
        
        if not all_features:
            return None
            
        pr.addFeatures(all_features)
        try:
            set_archtoolkit_layer_metadata(
                dest_layer,
                tool_id="map_styling",
                run_id=run_id,
                kind="styled_vector",
                units="",
                params={"name": str(name or ""), "dest_geom": str(dest_geom or "")},
            )
        except Exception:
            pass
        QgsProject.instance().addMapLayer(dest_layer, False)  # Add to project but NOT to layer tree
        return dest_layer

    def style_road_layer(self, layer, field_name):
        cfg = self.code_config.get("roads", {})
        color = QColor(cfg.get("color", "#ff9501"))
        road_rules = cfg.get("rules", [])
        
        # Create invisible root rule (ELSE filter catches nothing)
        root_rule = QgsRuleBasedRenderer.Rule(None)  # No symbol for root
        
        for item in road_rules:
            if not isinstance(item, dict):
                continue
            code = item.get("code")
            width = item.get("width_mm", item.get("width"))
            label = item.get("label", "")
            if not (isinstance(code, str) and code):
                continue
            try:
                width_f = float(width)
            except Exception:
                continue
            sym = QgsLineSymbol.createSimple({'color': color.name(), 'width': str(width_f)})
            rule = QgsRuleBasedRenderer.Rule(sym, 0, 0, f"\"{field_name}\" = '{code}'", str(label))
            root_rule.appendChild(rule)
            
        layer.setRenderer(QgsRuleBasedRenderer(root_rule))
        layer.triggerRepaint()

    def style_river_layer(self, layer, field_name):
        cfg = self.code_config.get("rivers", {})
        color = QColor(cfg.get("color", "#1ea1ff"))
        river_rules = cfg.get("rules", [])
        
        # Create invisible root rule (ELSE filter catches nothing)
        root_rule = QgsRuleBasedRenderer.Rule(None)  # No symbol for root
        
        for item in river_rules:
            if not isinstance(item, dict):
                continue
            code = item.get("code")
            width = item.get("width_mm", item.get("width"))
            label = item.get("label", "")
            if not (isinstance(code, str) and code):
                continue
            try:
                width_f = float(width)
            except Exception:
                continue
            sym = QgsLineSymbol.createSimple({'color': color.name(), 'width': str(width_f)})
            rule = QgsRuleBasedRenderer.Rule(sym, 0, 0, f"\"{field_name}\" = '{code}'", str(label))
            root_rule.appendChild(rule)
            
        layer.setRenderer(QgsRuleBasedRenderer(root_rule))
        layer.triggerRepaint()

    def style_building_layer(self, layer, field_name):
        offset_val = self.spinOffset.value()
        cfg = self.code_config.get("buildings", {})
        fill_color = cfg.get("fill_color", "#ffffff")
        outline_color = cfg.get("outline_color", "#666666")
        outline_width = cfg.get("outline_width_mm", 0.1)
        try:
            shadow_alpha = int(cfg.get("shadow_alpha", 100))
        except Exception:
            shadow_alpha = 100
        shadow_alpha = max(0, min(255, shadow_alpha))
        
        if layer.geometryType() == QgsWkbTypes.PolygonGeometry:
            symbol = QgsFillSymbol.createSimple({
                'color': str(fill_color),
                'outline_color': str(outline_color),
                'outline_width': str(outline_width),
            })
            shadow_layer = QgsSimpleFillSymbolLayer()
            shadow_layer.setFillColor(QColor(0, 0, 0, shadow_alpha))
            shadow_layer.setStrokeColor(Qt.transparent)
            shadow_layer.setOffset(QPointF(offset_val, offset_val))
            shadow_layer.setOffsetUnit(QgsUnitTypes.RenderMillimeters)
            symbol.insertSymbolLayer(0, shadow_layer)
        else:
            symbol = QgsLineSymbol.createSimple({'color': '#ffffff', 'width': '0.3'})
            shadow_layer = QgsSimpleLineSymbolLayer()
            shadow_layer.setColor(QColor(0, 0, 0, shadow_alpha))
            shadow_layer.setWidth(0.3)
            shadow_layer.setOffset(offset_val) 
            symbol.insertSymbolLayer(0, shadow_layer)

        layer.setRenderer(QgsSingleSymbolRenderer(symbol))
        layer.triggerRepaint()

    @staticmethod
    def _save_named_style(layer, path):
        try:
            res = layer.saveNamedStyle(path)
            if isinstance(res, (tuple, list)):
                return bool(res[0])
            return bool(res)
        except Exception:
            return False

    def export_qml_preset(self):
        """Export QML styles + current mapping config for reuse."""
        base_dir = QtWidgets.QFileDialog.getExistingDirectory(self, "프리셋 저장 폴더 선택")
        if not base_dir:
            return

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        preset_dir = os.path.join(base_dir, f"ArchToolkit_MapStyling_Preset_{ts}")
        try:
            os.makedirs(preset_dir, exist_ok=False)
        except Exception:
            preset_dir = base_dir

        project_crs = QgsProject.instance().crs().authid() or "EPSG:4326"
        exported = []

        # Vector styles (templates)
        try:
            roads_layer = QgsVectorLayer(f"LineString?crs={project_crs}", "roads_style_template", "memory")
            roads_layer.dataProvider().addAttributes([QgsField("Layer", QVariant.String)])
            roads_layer.updateFields()
            self.style_road_layer(roads_layer, "Layer")
            if self._save_named_style(roads_layer, os.path.join(preset_dir, "roads.qml")):
                exported.append("roads.qml")
        except Exception:
            pass

        try:
            rivers_layer = QgsVectorLayer(f"LineString?crs={project_crs}", "rivers_style_template", "memory")
            rivers_layer.dataProvider().addAttributes([QgsField("Layer", QVariant.String)])
            rivers_layer.updateFields()
            self.style_river_layer(rivers_layer, "Layer")
            if self._save_named_style(rivers_layer, os.path.join(preset_dir, "rivers.qml")):
                exported.append("rivers.qml")
        except Exception:
            pass

        try:
            buildings_layer = QgsVectorLayer(f"MultiPolygon?crs={project_crs}", "buildings_style_template", "memory")
            buildings_layer.dataProvider().addAttributes([QgsField("Layer", QVariant.String)])
            buildings_layer.updateFields()
            self.style_building_layer(buildings_layer, "Layer")
            if self._save_named_style(buildings_layer, os.path.join(preset_dir, "buildings.qml")):
                exported.append("buildings.qml")
        except Exception:
            pass

        # DEM styles (export only when DEM styling is enabled and a DEM is selected)
        dem_layer = self.cmbDemLayer.currentLayer()
        if self.chkDemStyling.isChecked() and isinstance(dem_layer, QgsRasterLayer):
            try:
                hillshade_layer = dem_layer.clone()
                hillshade_layer.setRenderer(QgsHillshadeRenderer(hillshade_layer.dataProvider(), 1, 315, 45))
                if self._save_named_style(hillshade_layer, os.path.join(preset_dir, "dem_hillshade.qml")):
                    exported.append("dem_hillshade.qml")
            except Exception:
                pass

            try:
                gray_layer = dem_layer.clone()
                gray_layer.setRenderer(QgsSingleBandGrayRenderer(gray_layer.dataProvider(), 1))
                gray_layer.setOpacity(0.4)
                gray_layer.setBlendMode(QPainter.CompositionMode_Multiply)
                if self._save_named_style(gray_layer, os.path.join(preset_dir, "dem_gray.qml")):
                    exported.append("dem_gray.qml")
            except Exception:
                pass

            try:
                color_layer = dem_layer.clone()
                stats = color_layer.dataProvider().bandStatistics(1, QgsRasterBandStats.All)
                min_val, max_val = stats.minimumValue, stats.maximumValue
                shader = QgsRasterShader()
                color_ramp = QgsColorRampShader(min_val, max_val)
                color_ramp.setColorRampType(QgsColorRampShader.Discrete)
                items = [
                    QgsColorRampShader.ColorRampItem(min_val + (max_val - min_val) * 0.0, QColor("#ffffcc"), "<= Min"),
                    QgsColorRampShader.ColorRampItem(min_val + (max_val - min_val) * 0.25, QColor("#c2e699"), "Low"),
                    QgsColorRampShader.ColorRampItem(min_val + (max_val - min_val) * 0.5, QColor("#78c679"), "Mid"),
                    QgsColorRampShader.ColorRampItem(min_val + (max_val - min_val) * 0.75, QColor("#31a354"), "High"),
                    QgsColorRampShader.ColorRampItem(max_val, QColor("#006837"), "Max"),
                ]
                color_ramp.setColorRampItemList(items)
                shader.setRasterShaderFunction(color_ramp)
                color_layer.setRenderer(QgsSingleBandPseudoColorRenderer(color_layer.dataProvider(), 1, shader))
                color_layer.setOpacity(0.7)
                if self._save_named_style(color_layer, os.path.join(preset_dir, "dem_color.qml")):
                    exported.append("dem_color.qml")
            except Exception:
                pass

        # Mapping config snapshot
        try:
            with open(os.path.join(preset_dir, "map_styling_codes.json"), "w", encoding="utf-8") as f:
                json.dump(self.code_config, f, ensure_ascii=False, indent=2)
            exported.append("map_styling_codes.json")
        except Exception:
            pass

        # Minimal manifest
        try:
            manifest = {
                "schema": 1,
                "tool": "ArchToolkit Map Styling",
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "exported_files": exported,
                "options": {
                    "roads": bool(self.chkRoads.isChecked()),
                    "rivers": bool(self.chkRivers.isChecked()),
                    "buildings": bool(self.chkBuildings.isChecked()),
                    "dem_styling": bool(self.chkDemStyling.isChecked()),
                    "building_shadow_offset_mm": float(self.spinOffset.value()),
                },
            }
            with open(os.path.join(preset_dir, "preset_manifest.json"), "w", encoding="utf-8") as f:
                json.dump(manifest, f, ensure_ascii=False, indent=2)
            exported.append("preset_manifest.json")
        except Exception:
            pass

        push_message(self.iface, "완료", f"프리셋을 저장했습니다: {preset_dir}", level=0)


