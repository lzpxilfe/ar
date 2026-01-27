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
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction, QMenu, QToolButton, QMessageBox

from .tools.utils import log_exception, start_ui_log_pump, stop_ui_log_pump
import os.path

class ArchToolkit:
    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.actions = []
        self.menu_name = u'Archaeology Toolkit'
        self.toolbar = None
        self.main_action = None
        self.viewshed_dlg = None # [v1.6.20] Persistent reference for marker cleanup
        self.cost_dlg = None  # Persistent reference for temp/preview cleanup
        self.profile_dlg = None  # Persistent reference for multi-profile selection/view
        self.geochem_dlg = None  # Optional: keep reference if we later add temp cleanup

    def initGui(self):
        try:
            # Enable real-time logs in the QGIS "Log Messages" panel.
            try:
                start_ui_log_pump()
            except Exception:
                pass

            plugin_dir = os.path.dirname(__file__)
            
            # 1. Create Actions for all tools
            
            # DEM Generation
            dem_icon = os.path.join(plugin_dir, 'dem_icon.png')
            self.dem_action = QAction(QIcon(dem_icon), u"DEM 생성 (Generate DEM)", self.iface.mainWindow())
            self.dem_action.triggered.connect(self.run_dem_tool)
            
            # Contour Extraction
            contour_icon = os.path.join(plugin_dir, 'contour_icon.png')
            self.contour_action = QAction(QIcon(contour_icon), u"등고선 추출 (Extract Contours)", self.iface.mainWindow())
            self.contour_action.triggered.connect(self.run_contour_tool)

            # Cadastral overlap table (Survey area vs Parcels)
            cad_icon = None
            for icon_name in ("jijuk.png", "jijuk.jpg", "jijuk.jpeg", "style_icon.png"):
                icon_path = os.path.join(plugin_dir, icon_name)
                if os.path.exists(icon_path):
                    cad_icon = icon_path
                    break
            self.cad_overlap_action = QAction(
                QIcon(cad_icon or ""),
                u"지적도 중첩 면적표 (Cadastral Overlap)",
                self.iface.mainWindow(),
            )
            self.cad_overlap_action.triggered.connect(self.run_cadastral_overlap_tool)
            
            # Terrain Analysis
            terrain_icon = os.path.join(plugin_dir, 'terrain_icon.png')
            self.terrain_action = QAction(QIcon(terrain_icon), u"지형 분석 (Terrain Analysis)", self.iface.mainWindow())
            self.terrain_action.triggered.connect(self.run_terrain_tool)

            # GeoChem (WMS RGB -> class polygons)
            geochem_icon = None
            for icon_path in (
                os.path.join(plugin_dir, "tools", "geochem.png"),
                os.path.join(plugin_dir, "geochem.png"),
                os.path.join(plugin_dir, "terrain_icon.png"),
            ):
                if os.path.exists(icon_path):
                    geochem_icon = icon_path
                    break
            self.geochem_action = QAction(
                QIcon(geochem_icon or ""),
                u"지구화학도 폴리곤화 (GeoChem WMS → Polygons)",
                self.iface.mainWindow(),
            )
            self.geochem_action.triggered.connect(self.run_geochem_tool)
             
            # Terrain Profile
            profile_icon = os.path.join(plugin_dir, 'profile_icon.png')
            self.profile_action = QAction(QIcon(profile_icon), u"지형 단면 (Terrain Profile)", self.iface.mainWindow())
            self.profile_action.triggered.connect(self.run_profile_tool)

            # 비용표면/최소비용경로 (Cost Surface / LCP)
            cost_icon = os.path.join(plugin_dir, 'cost_icon.png')
            self.cost_action = QAction(QIcon(cost_icon), u"비용표면/최소비용경로 (Cost Surface / LCP)", self.iface.mainWindow())
            self.cost_action.triggered.connect(self.run_cost_tool)

            # 최소비용 네트워크 (Least-cost Network)
            network_icon = None
            for icon_name in ("network_icon.png", "network_icon.jpg", "network_icon.jpeg"):
                icon_path = os.path.join(plugin_dir, icon_name)
                if os.path.exists(icon_path):
                    network_icon = icon_path
                    break
            self.network_action = QAction(
                QIcon(network_icon or cost_icon),
                u"최소비용 네트워크 (Least-cost Network)",
                self.iface.mainWindow(),
            )
            self.network_action.triggered.connect(self.run_network_tool)

            # Spatial / Visibility Network (PPA / LOS)
            spatial_network_icon = None
            for icon_name in (
                "spatial_network.png",
                "spatial_network.jpg",
                "spatial_network.jpeg",
                "network_visibility.png",
                "network_visibility.jpg",
                "network_visibility.jpeg",
            ):
                icon_path = os.path.join(plugin_dir, icon_name)
                if os.path.exists(icon_path):
                    spatial_network_icon = icon_path
                    break
            self.spatial_network_action = QAction(
                QIcon(spatial_network_icon or network_icon or cost_icon),
                u"근접/가시성 네트워크 (PPA / Visibility)",
                self.iface.mainWindow(),
            )
            self.spatial_network_action.triggered.connect(self.run_spatial_network_tool)

            # Map Styling
            style_icon = os.path.join(plugin_dir, 'style_icon.png')
            self.style_action = QAction(QIcon(style_icon), u"도면 시각화 (Map Styling)", self.iface.mainWindow())
            self.style_action.triggered.connect(self.run_styling_tool)

            # Slope/Aspect Drafting (Cartographic)
            drafting_icon = None
            for icon_name in ("slope_aspect.png",):
                icon_path = os.path.join(plugin_dir, icon_name)
                if os.path.exists(icon_path):
                    drafting_icon = icon_path
                    break
            self.drafting_action = QAction(
                QIcon(drafting_icon or style_icon),
                u"경사도/사면방향 도면화 (Slope/Aspect Drafting)",
                self.iface.mainWindow(),
            )
            self.drafting_action.triggered.connect(self.run_drafting_tool)

            # Viewshed Analysis
            viewshed_icon = os.path.join(plugin_dir, 'viewshed_icon.png')
            self.viewshed_action = QAction(QIcon(viewshed_icon), u"가시권 분석 (Viewshed Analysis)", self.iface.mainWindow())
            self.viewshed_action.triggered.connect(self.run_viewshed_tool)

            # 2. Add to Plugin Menu
            self.iface.addPluginToMenu(self.menu_name, self.dem_action)
            self.iface.addPluginToMenu(self.menu_name, self.contour_action)
            self.iface.addPluginToMenu(self.menu_name, self.cad_overlap_action)
            self.iface.addPluginToMenu(self.menu_name, self.terrain_action)
            self.iface.addPluginToMenu(self.menu_name, self.geochem_action)
            self.iface.addPluginToMenu(self.menu_name, self.profile_action)
            self.iface.addPluginToMenu(self.menu_name, self.cost_action)
            self.iface.addPluginToMenu(self.menu_name, self.network_action)
            self.iface.addPluginToMenu(self.menu_name, self.spatial_network_action)
            self.iface.addPluginToMenu(self.menu_name, self.style_action)
            self.iface.addPluginToMenu(self.menu_name, self.drafting_action)
            self.iface.addPluginToMenu(self.menu_name, self.viewshed_action)

            # 3. Create Dedicated Toolbar for Visibility
            self.toolbar = self.iface.addToolBar(u"ArchToolkit")
            self.toolbar.setObjectName("ArchToolkit")

            # 4. Create Unified Toolkit Button
            main_icon_path = os.path.join(plugin_dir, 'icon.png')
            self.main_action = QAction(QIcon(main_icon_path), u"ArchToolkit", self.iface.mainWindow())
            
            # Create Dropdown Menu
            self.tool_menu = QMenu(self.iface.mainWindow())
            self.tool_menu.addAction(self.dem_action)
            self.tool_menu.addAction(self.contour_action)
            self.tool_menu.addAction(self.cad_overlap_action)
            self.tool_menu.addSeparator()
            self.tool_menu.addAction(self.terrain_action)
            self.tool_menu.addAction(self.geochem_action)
            self.tool_menu.addAction(self.profile_action)
            self.tool_menu.addAction(self.viewshed_action)
            self.tool_menu.addAction(self.cost_action)
            self.tool_menu.addAction(self.network_action)
            self.tool_menu.addAction(self.spatial_network_action)
            self.tool_menu.addSeparator()
            self.tool_menu.addAction(self.style_action)
            self.tool_menu.addAction(self.drafting_action)
             
            self.main_action.setMenu(self.tool_menu)
            
            # Add QToolButton to toolbar for instant popup support
            tool_button = QToolButton()
            tool_button.setDefaultAction(self.main_action)
            tool_button.setMenu(self.tool_menu)
            tool_button.setPopupMode(QToolButton.InstantPopup)
            
            self.toolbar.addWidget(tool_button)
            
            # Keep references for cleanup
            self.actions = [
                self.dem_action, self.contour_action, self.cad_overlap_action, self.terrain_action, self.geochem_action,
                self.profile_action, self.cost_action, self.network_action, self.spatial_network_action, self.style_action, self.drafting_action, self.viewshed_action,
                self.main_action
            ]
        except Exception as e:
            log_exception("ArchToolkit initGui error", e)
            QMessageBox.critical(self.iface.mainWindow(), "ArchToolkit 로드 오류", f"플러그인을 초기화하는 중 오류가 발생했습니다: {str(e)}")

    def unload(self):
        # Remove from menu
        for action in self.actions:
            self.iface.removePluginMenu(self.menu_name, action)

        try:
            stop_ui_log_pump()
        except Exception:
            pass

        # Close persistent dialogs and disconnect long-lived signals (prevents stale callbacks after reload)
        if self.viewshed_dlg is not None:
            try:
                if hasattr(self.viewshed_dlg, "cleanup_for_unload"):
                    self.viewshed_dlg.cleanup_for_unload()
            except Exception:
                pass
            try:
                self.viewshed_dlg.close()
            except Exception:
                pass
            try:
                self.viewshed_dlg.deleteLater()
            except Exception:
                pass
            self.viewshed_dlg = None

        if self.cost_dlg is not None:
            try:
                if hasattr(self.cost_dlg, "cleanup_for_unload"):
                    self.cost_dlg.cleanup_for_unload()
            except Exception:
                pass
            try:
                self.cost_dlg.close()
            except Exception:
                pass
            try:
                self.cost_dlg.deleteLater()
            except Exception:
                pass
            self.cost_dlg = None

        if self.profile_dlg is not None:
            try:
                if hasattr(self.profile_dlg, "cleanup_for_unload"):
                    self.profile_dlg.cleanup_for_unload()
            except Exception:
                pass
            try:
                self.profile_dlg.close()
            except Exception:
                pass
            try:
                self.profile_dlg.deleteLater()
            except Exception:
                pass
            self.profile_dlg = None
             
        # Remove toolbar cleanly from mainWindow
        if self.toolbar:
            self.iface.mainWindow().removeToolBar(self.toolbar)
            self.toolbar.deleteLater()
            self.toolbar = None

    def run_dem_tool(self):
        try:
            from .tools.dem_generator_dialog import DemGeneratorDialog
            dlg = DemGeneratorDialog(self.iface)
            dlg.exec_()
        except Exception as e:
            log_exception("DEM tool error", e)
            QMessageBox.critical(self.iface.mainWindow(), "오류", f"도구를 여는 중 오류가 발생했습니다: {str(e)}")

    def run_contour_tool(self):
        try:
            from .tools.contour_extractor_dialog import ContourExtractorDialog
            dlg = ContourExtractorDialog(self.iface)
            dlg.exec_()
        except Exception as e:
            log_exception("Contour tool error", e)
            QMessageBox.critical(self.iface.mainWindow(), "오류", f"도구를 여는 중 오류가 발생했습니다: {str(e)}")

    def run_cadastral_overlap_tool(self):
        try:
            from .tools.cadastral_overlap_dialog import CadastralOverlapDialog

            dlg = CadastralOverlapDialog(self.iface)
            dlg.exec_()
        except Exception as e:
            log_exception("Cadastral overlap tool error", e)
            QMessageBox.critical(self.iface.mainWindow(), "오류", f"도구를 여는 중 오류가 발생했습니다: {str(e)}")

    def run_terrain_tool(self):
        try:
            from .tools.terrain_analysis_dialog import TerrainAnalysisDialog
            dlg = TerrainAnalysisDialog(self.iface)
            dlg.exec_()
        except Exception as e:
            log_exception("Terrain tool error", e)
            QMessageBox.critical(self.iface.mainWindow(), "오류", f"도구를 여는 중 오류가 발생했습니다: {str(e)}")

    def run_profile_tool(self):
        try:
            from .tools.terrain_profile_dialog import TerrainProfileDialog
            if self.profile_dlg is None:
                self.profile_dlg = TerrainProfileDialog(self.iface)
            # Non-modal: lets users click/choose saved profile lines on the map.
            self.profile_dlg.show()
            self.profile_dlg.raise_()
            self.profile_dlg.activateWindow()
        except Exception as e:
            log_exception("Terrain profile tool error", e)
            QMessageBox.critical(self.iface.mainWindow(), "오류", f"도구를 여는 중 오류가 발생했습니다: {str(e)}")

    def run_geochem_tool(self):
        try:
            from .tools.geochem_polygonize_dialog import GeoChemPolygonizeDialog

            dlg = GeoChemPolygonizeDialog(self.iface)
            dlg.exec_()
        except Exception as e:
            log_exception("GeoChem tool error", e)
            QMessageBox.critical(self.iface.mainWindow(), "오류", f"도구를 여는 중 오류가 발생했습니다: {str(e)}")

    def run_styling_tool(self):
        try:
            from .tools.map_styling_dialog import MapStylingDialog
            dlg = MapStylingDialog(self.iface)
            dlg.exec_()
        except Exception as e:
            log_exception("Map styling tool error", e)
            QMessageBox.critical(self.iface.mainWindow(), "오류", f"도구를 여는 중 오류가 발생했습니다: {str(e)}")

    def run_drafting_tool(self):
        try:
            from .tools.slope_aspect_drafting_dialog import SlopeAspectDraftingDialog
            dlg = SlopeAspectDraftingDialog(self.iface)
            dlg.exec_()
        except Exception as e:
            log_exception("Slope/aspect drafting tool error", e)
            QMessageBox.critical(self.iface.mainWindow(), "오류", f"도구를 여는 중 오류가 발생했습니다: {str(e)}")

    def run_cost_tool(self):
        try:
            if self.cost_dlg is None:
                from .tools.cost_surface_dialog import CostSurfaceDialog
                self.cost_dlg = CostSurfaceDialog(self.iface)
            self.cost_dlg.exec_()
        except Exception as e:
            log_exception("Cost surface tool error", e)
            QMessageBox.critical(self.iface.mainWindow(), "오류", f"도구를 여는 중 오류가 발생했습니다: {str(e)}")

    def run_network_tool(self):
        try:
            from .tools.cost_network_dialog import CostNetworkDialog
            dlg = CostNetworkDialog(self.iface)
            dlg.exec_()
        except Exception as e:
            log_exception("Least-cost network tool error", e)
            QMessageBox.critical(self.iface.mainWindow(), "오류", f"도구 실행 중 오류가 발생했습니다: {str(e)}")

    def run_spatial_network_tool(self):
        try:
            from .tools.spatial_network_dialog import SpatialNetworkDialog
            dlg = SpatialNetworkDialog(self.iface)
            dlg.exec_()
        except Exception as e:
            log_exception("Spatial network tool error", e)
            QMessageBox.critical(
                self.iface.mainWindow(),
                "오류",
                f"도구 실행 중 오류가 발생했습니다: {str(e)}",
            )

    def run_viewshed_tool(self):
        try:
            # [v1.6.20] Maintain persistent dialog instance so layersRemoved signal persists
            if self.viewshed_dlg is None:
                from .tools.viewshed_dialog import ViewshedDialog
                self.viewshed_dlg = ViewshedDialog(self.iface)
            
            # Show the dialog. exec_() is modal and blocks until closed.
            # In v1.7.0 we might switch to .show() for non-modal interaction.
            self.viewshed_dlg.exec_()
        except Exception as e:
            log_exception("Viewshed tool error", e)
            QMessageBox.critical(self.iface.mainWindow(), "오류", f"도구를 여는 중 오류가 발생했습니다: {str(e)}")
