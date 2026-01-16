# -*- coding: utf-8 -*-
import os
from qgis.core import QgsCoordinateTransform, QgsMessageLog, QgsProject, QgsUnitTypes, Qgis

def transform_point(point, src_crs, dest_crs):
    """Transform point from source CRS to destination CRS"""
    if src_crs == dest_crs:
        return point
    transform = QgsCoordinateTransform(src_crs, dest_crs, QgsProject.instance())
    return transform.transform(point)

def cleanup_files(file_paths):
    """Safely remove a list of file paths"""
    for path in file_paths:
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass

def log_message(message, level=Qgis.Info):
    """Log to QGIS Message Log (not the transient message bar)."""
    try:
        QgsMessageLog.logMessage(str(message), "ArchToolkit", level)
    except Exception:
        # Never crash due to logging
        pass

def is_metric_crs(crs):
    """Return True if CRS map units are meters (recommended for distance-based tools)."""
    try:
        return (not crs.isGeographic()) and crs.mapUnits() == QgsUnitTypes.DistanceMeters
    except Exception:
        return False

def restore_ui_focus(dialog):
    """Ensure the dialog is visible and has focus"""
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()

def push_message(iface, title, text, level=0, duration=3):
    """Helper to push message to QGIS message bar"""
    iface.messageBar().pushMessage(title, text, level=level, duration=duration)
