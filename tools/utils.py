# -*- coding: utf-8 -*-
import os
import tempfile
import traceback
from datetime import datetime

from qgis.core import (
    QgsCoordinateTransform,
    QgsMessageLog,
    QgsProject,
    QgsUnitTypes,
    Qgis,
)

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

def _log_file_path():
    """Return a writable log file path (best-effort)."""
    try:
        from qgis.core import QgsApplication

        base = QgsApplication.qgisSettingsDirPath() or ""
    except Exception:
        base = ""

    if not base:
        base = tempfile.gettempdir()

    log_dir = os.path.join(base, "ArchToolkit", "logs")
    try:
        os.makedirs(log_dir, exist_ok=True)
    except Exception:
        log_dir = tempfile.gettempdir()

    return os.path.join(log_dir, "archtoolkit.log")


def get_log_path():
    """Public helper to retrieve the current log file path."""
    return _log_file_path()


def _write_log_line(level_name: str, message: str):
    """Append a timestamped line to the plugin log file (best-effort, thread-safe enough)."""
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] [{level_name}] {message}\n"
        with open(_log_file_path(), "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def _is_main_thread():
    """Best-effort check to avoid calling Qt/QGIS UI APIs from worker threads."""
    try:
        from qgis.PyQt.QtCore import QCoreApplication, QThread

        app = QCoreApplication.instance()
        if app is None:
            return True
        return QThread.currentThread() == app.thread()
    except Exception:
        return True


def log_message(message, level=Qgis.Info):
    """Log to file + QGIS Message Log (file is always attempted; QGIS log only on main thread)."""
    try:
        level_name = "INFO"
        if level == Qgis.Warning:
            level_name = "WARN"
        elif level == Qgis.Critical:
            level_name = "ERROR"
        _write_log_line(level_name, str(message))
    except Exception:
        pass

    # QgsMessageLog may not be safe off the main thread on some setups.
    if not _is_main_thread():
        return

    try:
        QgsMessageLog.logMessage(str(message), "ArchToolkit", level)
    except Exception:
        # Never crash due to logging
        pass


def log_exception(context: str, exc: Exception = None, level=Qgis.Critical):
    """Log a stack trace to file + (main thread only) QGIS log."""
    try:
        msg = f"{context}: {exc}" if exc is not None else str(context)
        if exc is not None and getattr(exc, "__traceback__", None) is not None:
            tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        else:
            tb = traceback.format_exc()
        log_message(msg, level=level)
        if tb and "Traceback" in tb:
            log_message(tb, level=level)
    except Exception:
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
    try:
        lvl = Qgis.Info
        if level == 1:
            lvl = Qgis.Warning
        elif level == 2:
            lvl = Qgis.Critical
        log_message(f"{title}: {text}", level=lvl)
    except Exception:
        pass
    iface.messageBar().pushMessage(title, text, level=level, duration=duration)
