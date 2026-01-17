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

from .tools.dem_generator_dialog import DemGeneratorDialog
from .tools.contour_extractor_dialog import ContourExtractorDialog
from .tools.terrain_analysis_dialog import TerrainAnalysisDialog
from .tools.terrain_profile_dialog import TerrainProfileDialog
from .tools.map_styling_dialog import MapStylingDialog
from .tools.viewshed_dialog import ViewshedDialog
from .tools.cost_surface_dialog import CostSurfaceDialog
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

    def initGui(self):
        try:
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
            
            # Terrain Analysis
            terrain_icon = os.path.join(plugin_dir, 'terrain_icon.png')
            self.terrain_action = QAction(QIcon(terrain_icon), u"지형 분석 (Terrain Analysis)", self.iface.mainWindow())
            self.terrain_action.triggered.connect(self.run_terrain_tool)
            
            # Terrain Profile
            profile_icon = os.path.join(plugin_dir, 'profile_icon.png')
            self.profile_action = QAction(QIcon(profile_icon), u"지형 단면 (Terrain Profile)", self.iface.mainWindow())
            self.profile_action.triggered.connect(self.run_profile_tool)

            # 비용표면/최소비용경로 (Cost Surface / LCP)
            cost_icon = os.path.join(plugin_dir, 'cost_icon.png')
            self.cost_action = QAction(QIcon(cost_icon), u"비용표면/최소비용경로 (Cost Surface / LCP)", self.iface.mainWindow())
            self.cost_action.triggered.connect(self.run_cost_tool)

            # Map Styling
            style_icon = os.path.join(plugin_dir, 'style_icon.png')
            self.style_action = QAction(QIcon(style_icon), u"도면 시각화 (Map Styling)", self.iface.mainWindow())
            self.style_action.triggered.connect(self.run_styling_tool)

            # Viewshed Analysis
            viewshed_icon = os.path.join(plugin_dir, 'viewshed_icon.png')
            self.viewshed_action = QAction(QIcon(viewshed_icon), u"가시권 분석 (Viewshed Analysis)", self.iface.mainWindow())
            self.viewshed_action.triggered.connect(self.run_viewshed_tool)

            # 2. Add to Plugin Menu
            self.iface.addPluginToMenu(self.menu_name, self.dem_action)
            self.iface.addPluginToMenu(self.menu_name, self.contour_action)
            self.iface.addPluginToMenu(self.menu_name, self.terrain_action)
            self.iface.addPluginToMenu(self.menu_name, self.profile_action)
            self.iface.addPluginToMenu(self.menu_name, self.cost_action)
            self.iface.addPluginToMenu(self.menu_name, self.style_action)
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
            self.tool_menu.addSeparator()
            self.tool_menu.addAction(self.terrain_action)
            self.tool_menu.addAction(self.profile_action)
            self.tool_menu.addAction(self.viewshed_action)
            self.tool_menu.addAction(self.cost_action)
            self.tool_menu.addSeparator()
            self.tool_menu.addAction(self.style_action)
            
            self.main_action.setMenu(self.tool_menu)
            
            # Add QToolButton to toolbar for instant popup support
            tool_button = QToolButton()
            tool_button.setDefaultAction(self.main_action)
            tool_button.setMenu(self.tool_menu)
            tool_button.setPopupMode(QToolButton.InstantPopup)
            
            self.toolbar.addWidget(tool_button)
            
            # Keep references for cleanup
            self.actions = [
                self.dem_action, self.contour_action, self.terrain_action,
                self.profile_action, self.cost_action, self.style_action, self.viewshed_action,
                self.main_action
            ]
        except Exception as e:
            QMessageBox.critical(self.iface.mainWindow(), "ArchToolkit 로드 오류", f"플러그인을 초기화하는 중 오류가 발생했습니다: {str(e)}")

    def unload(self):
        # Remove from menu
        for action in self.actions:
            self.iface.removePluginMenu(self.menu_name, action)

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
             
        # Remove toolbar cleanly from mainWindow
        if self.toolbar:
            self.iface.mainWindow().removeToolBar(self.toolbar)
            self.toolbar.deleteLater()
            self.toolbar = None

    def run_dem_tool(self):
        try:
            dlg = DemGeneratorDialog(self.iface)
            dlg.exec_()
        except Exception as e:
            QMessageBox.critical(self.iface.mainWindow(), "오류", f"도구를 여는 중 오류가 발생했습니다: {str(e)}")

    def run_contour_tool(self):
        try:
            dlg = ContourExtractorDialog(self.iface)
            dlg.exec_()
        except Exception as e:
            QMessageBox.critical(self.iface.mainWindow(), "오류", f"도구를 여는 중 오류가 발생했습니다: {str(e)}")

    def run_terrain_tool(self):
        try:
            dlg = TerrainAnalysisDialog(self.iface)
            dlg.exec_()
        except Exception as e:
            QMessageBox.critical(self.iface.mainWindow(), "오류", f"도구를 여는 중 오류가 발생했습니다: {str(e)}")

    def run_profile_tool(self):
        try:
            dlg = TerrainProfileDialog(self.iface)
            dlg.exec_()
        except Exception as e:
            QMessageBox.critical(self.iface.mainWindow(), "오류", f"도구를 여는 중 오류가 발생했습니다: {str(e)}")

    def run_styling_tool(self):
        try:
            dlg = MapStylingDialog(self.iface)
            dlg.exec_()
        except Exception as e:
            QMessageBox.critical(self.iface.mainWindow(), "오류", f"도구를 여는 중 오류가 발생했습니다: {str(e)}")

    def run_cost_tool(self):
        try:
            dlg = CostSurfaceDialog(self.iface)
            dlg.exec_()
        except Exception as e:
            QMessageBox.critical(self.iface.mainWindow(), "오류", f"도구를 여는 중 오류가 발생했습니다: {str(e)}")

    def run_viewshed_tool(self):
        try:
            # [v1.6.20] Maintain persistent dialog instance so layersRemoved signal persists
            if self.viewshed_dlg is None:
                self.viewshed_dlg = ViewshedDialog(self.iface)
            
            # Show the dialog. exec_() is modal and blocks until closed.
            # In v1.7.0 we might switch to .show() for non-modal interaction.
            self.viewshed_dlg.exec_()
        except Exception as e:
            QMessageBox.critical(self.iface.mainWindow(), "오류", f"도구를 여는 중 오류가 발생했습니다: {str(e)}")
