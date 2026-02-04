# -*- coding: utf-8 -*-
"""
Gemini API helpers for ArchToolkit.

Goals
- No external Python dependencies (use Qt/QGIS network + stdlib only).
- Store API key in QGIS AuthManager (preferred) and keep only authcfg id in settings.
- Best-effort: never crash the plugin if AuthManager is unavailable/misconfigured.
"""

from __future__ import annotations

import json
from typing import Optional, Tuple

from qgis.PyQt import QtWidgets
from qgis.PyQt.QtCore import QEventLoop, QSettings, QTimer, QUrl
from qgis.PyQt.QtNetwork import QNetworkRequest

from qgis.core import Qgis

from .utils import log_message, push_message


_SETTINGS_PREFIX = "ArchToolkit/ai/gemini"


def _settings_get(key: str, default=None):
    try:
        return QSettings().value(f"{_SETTINGS_PREFIX}/{key}", default)
    except Exception:
        return default


def _settings_set(key: str, value) -> None:
    try:
        QSettings().setValue(f"{_SETTINGS_PREFIX}/{key}", value)
    except Exception:
        pass


def get_configured_model(default: str = "gemini-1.5-flash") -> str:
    v = str(_settings_get("model", default) or "").strip()
    return v or default


def set_configured_model(model: str) -> None:
    _settings_set("model", str(model or "").strip())


def _auth_manager():
    try:
        from qgis.core import QgsApplication

        return QgsApplication.authManager()
    except Exception:
        return None


def _new_auth_config():
    try:
        from qgis.core import QgsAuthMethodConfig

        return QgsAuthMethodConfig()
    except Exception:
        return None


def _store_auth_config(auth_cfg) -> bool:
    authm = _auth_manager()
    if authm is None or auth_cfg is None:
        return False

    for name in ("storeAuthenticationConfig", "storeAuthMethodConfig"):
        fn = getattr(authm, name, None)
        if fn is None:
            continue
        try:
            return bool(fn(auth_cfg))
        except Exception:
            continue
    return False


def _update_auth_config(auth_cfg) -> bool:
    authm = _auth_manager()
    if authm is None or auth_cfg is None:
        return False

    for name in ("updateAuthenticationConfig", "updateAuthMethodConfig"):
        fn = getattr(authm, name, None)
        if fn is None:
            continue
        try:
            return bool(fn(auth_cfg))
        except Exception:
            continue
    return False


def _load_auth_config(authcfg_id: str):
    authm = _auth_manager()
    auth_cfg = _new_auth_config()
    if authm is None or auth_cfg is None:
        return None

    if not authcfg_id:
        return None

    for name in ("loadAuthenticationConfig", "loadAuthMethodConfig"):
        fn = getattr(authm, name, None)
        if fn is None:
            continue
        try:
            # Signature is typically (authcfg, config, full=True)
            ok = fn(str(authcfg_id), auth_cfg, True)
            if ok:
                return auth_cfg
        except TypeError:
            try:
                ok = fn(str(authcfg_id), auth_cfg)
                if ok:
                    return auth_cfg
            except Exception:
                continue
        except Exception:
            continue

    return None


def get_api_key() -> Optional[str]:
    """Return Gemini API key stored in QGIS AuthManager, if configured."""
    authcfg_id = str(_settings_get("authcfg", "") or "").strip()
    if not authcfg_id:
        return None

    auth_cfg = _load_auth_config(authcfg_id)
    if auth_cfg is None:
        return None

    try:
        # "Basic" method: password field holds the secret.
        key = str(auth_cfg.config("password") or "").strip()
        return key or None
    except Exception:
        return None


def configure_api_key(parent: QtWidgets.QWidget, *, iface=None) -> Optional[str]:
    """Prompt user for an API key and store it in AuthManager (best-effort).

    Returns the configured key (in-memory) on success, else None.
    """
    try:
        key, ok = QtWidgets.QInputDialog.getText(
            parent,
            "Gemini API Key",
            "Gemini API 키를 입력하세요 (QGIS 인증 저장소에 저장됩니다):",
            QtWidgets.QLineEdit.Password,
        )
    except Exception:
        return None

    if not ok:
        return None

    api_key = str(key or "").strip()
    if not api_key:
        return None

    authm = _auth_manager()
    auth_cfg = _new_auth_config()
    if authm is None or auth_cfg is None:
        if iface is not None:
            push_message(iface, "경고", "QGIS AuthManager를 사용할 수 없습니다. 키 저장에 실패했습니다.", level=1, duration=6)
        return None

    # Reuse existing config if present; otherwise create a new one.
    existing_id = str(_settings_get("authcfg", "") or "").strip()
    if existing_id:
        loaded = _load_auth_config(existing_id)
        if loaded is not None:
            auth_cfg = loaded

    try:
        auth_cfg.setName("ArchToolkit Gemini")
        auth_cfg.setMethod("Basic")
        auth_cfg.setConfig("username", "apikey")
        auth_cfg.setConfig("password", api_key)
    except Exception:
        if iface is not None:
            push_message(iface, "오류", "AuthManager 설정 구성 중 오류가 발생했습니다.", level=2, duration=6)
        return None

    ok = False
    try:
        if existing_id and getattr(auth_cfg, "id", lambda: "")() == existing_id:
            ok = _update_auth_config(auth_cfg)
        if not ok:
            ok = _store_auth_config(auth_cfg)
    except Exception:
        ok = False

    if not ok:
        if iface is not None:
            push_message(
                iface,
                "오류",
                "Gemini API 키를 AuthManager에 저장하지 못했습니다. (마스터 비밀번호 설정이 필요할 수 있습니다)",
                level=2,
                duration=8,
            )
        return None

    try:
        authcfg_id = str(auth_cfg.id() or "").strip()
    except Exception:
        authcfg_id = ""

    if authcfg_id:
        _settings_set("authcfg", authcfg_id)
        if iface is not None:
            push_message(iface, "완료", "Gemini API 키를 저장했습니다.", level=0, duration=5)

    return api_key


def generate_text(
    *,
    api_key: str,
    model: str,
    prompt: str,
    temperature: float = 0.2,
    max_output_tokens: int = 1024,
    timeout_ms: int = 45000,
) -> Tuple[Optional[str], Optional[str]]:
    """Call Gemini generateContent and return (text, error_message)."""
    api_key = str(api_key or "").strip()
    if not api_key:
        return None, "API key is missing"

    model = str(model or "").strip() or "gemini-1.5-flash"

    # API endpoint (Generative Language API)
    url = QUrl(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    )

    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": str(prompt or "")}],
            }
        ],
        "generationConfig": {
            "temperature": float(temperature),
            "maxOutputTokens": int(max(64, min(8192, int(max_output_tokens)))),
        },
    }

    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    try:
        from qgis.core import QgsNetworkAccessManager

        nam = QgsNetworkAccessManager.instance()
    except Exception:
        try:
            from qgis.PyQt.QtNetwork import QNetworkAccessManager

            nam = QNetworkAccessManager()
        except Exception:
            nam = None

    if nam is None:
        return None, "Network manager is unavailable"

    req = QNetworkRequest(url)
    req.setHeader(QNetworkRequest.ContentTypeHeader, "application/json; charset=utf-8")

    try:
        reply = nam.post(req, data)
    except Exception as e:
        return None, f"Request failed: {e}"

    loop = QEventLoop()
    timer = QTimer()
    timer.setSingleShot(True)

    def _on_timeout():
        try:
            reply.abort()
        except Exception:
            pass
        try:
            loop.quit()
        except Exception:
            pass

    def _on_finished():
        try:
            loop.quit()
        except Exception:
            pass

    try:
        timer.timeout.connect(_on_timeout)
        reply.finished.connect(_on_finished)
        timer.start(int(timeout_ms))
        loop.exec_()
    except Exception:
        pass

    try:
        if timer.isActive():
            timer.stop()
    except Exception:
        pass

    try:
        if reply.error():
            err = reply.errorString()
            try:
                body = bytes(reply.readAll()).decode("utf-8", "ignore")
            except Exception:
                body = ""
            return None, f"{err}\n{body}".strip()
    except Exception:
        pass

    try:
        raw = bytes(reply.readAll()).decode("utf-8", "ignore")
        obj = json.loads(raw) if raw else {}
    except Exception as e:
        return None, f"Invalid JSON response: {e}"

    # candidates[0].content.parts[0].text
    try:
        candidates = obj.get("candidates") or []
        if not candidates:
            return None, f"No candidates in response: {raw[:500]}"
        content = candidates[0].get("content") or {}
        parts = content.get("parts") or []
        texts = []
        for p in parts:
            t = p.get("text")
            if isinstance(t, str) and t.strip():
                texts.append(t.strip())
        if texts:
            return "\n".join(texts).strip(), None
        return None, f"No text parts in response: {raw[:500]}"
    except Exception as e:
        return None, f"Failed to parse response: {e}"


def explain_auth_manager_once() -> str:
    """User-facing short explanation about AuthManager persistence."""
    return (
        "네. 한 번 저장해두면(동일 QGIS 프로필 내) 다음부터는 authcfg id만 참조하므로 보통 다시 입력할 필요가 없습니다. "
        "다만 QGIS 인증 저장소(마스터 비밀번호)를 초기화/삭제하거나 다른 프로필을 쓰면 다시 설정해야 할 수 있습니다."
    )

