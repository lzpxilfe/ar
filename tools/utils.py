# -*- coding: utf-8 -*-
import os
from qgis.core import QgsCoordinateTransform, QgsProject
from qgis.PyQt import QtWidgets

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

def restore_ui_focus(dialog):
    """Ensure the dialog is visible and has focus"""
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()

def push_message(iface, title, text, level=0, duration=3):
    """Helper to push message to QGIS message bar"""
    iface.messageBar().pushMessage(title, text, level=level, duration=duration)
