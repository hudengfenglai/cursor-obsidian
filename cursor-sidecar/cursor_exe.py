"""Resolve Cursor.exe across custom / user / system installs (v0.7.2)."""

from __future__ import annotations

import logging
import os
import shutil
import winreg
from pathlib import Path
from typing import Any, Iterable, Optional

import psutil

from window import expand_path

log = logging.getLogger("cursor_sidecar.cursor_exe")

_STANDARD_CANDIDATES = (
    r"%LOCALAPPDATA%\Programs\cursor\Cursor.exe",
    r"%LOCALAPPDATA%\Programs\Cursor\Cursor.exe",
    r"%LOCALAPPDATA%\cursor\Cursor.exe",
    r"C:\Program Files\Cursor\Cursor.exe",
    r"C:\Program Files (x86)\Cursor\Cursor.exe",
    # Common portable / custom installs (user machines)
    r"E:\cursor\Cursor.exe",
    r"D:\cursor\Cursor.exe",
    r"C:\cursor\Cursor.exe",
)


def _is_cursor_exe(path: str | None) -> bool:
    if not path:
        return False
    p = expand_path(path)
    if not os.path.isfile(p):
        return False
    return Path(p).name.lower() in ("cursor.exe", "cursor")


def exe_from_pid(pid: int) -> Optional[str]:
    try:
        path = psutil.Process(int(pid)).exe()
    except Exception:
        return None
    return path if _is_cursor_exe(path) else None


def exe_from_binding(binding: dict[str, Any] | None) -> Optional[str]:
    if not isinstance(binding, dict):
        return None
    # Prefer live PID path
    try:
        pid = int(binding.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    if pid:
        found = exe_from_pid(pid)
        if found:
            return found
    for key in ("exe", "executable", "path"):
        raw = binding.get(key)
        if _is_cursor_exe(str(raw) if raw else None):
            return expand_path(str(raw))
    return None


def _reg_query_values(root, subkey: str) -> list[str]:
    out: list[str] = []
    try:
        with winreg.OpenKey(root, subkey) as key:
            i = 0
            while True:
                try:
                    name, value, _ = winreg.EnumValue(key, i)
                except OSError:
                    break
                i += 1
                if isinstance(value, str) and value.strip():
                    out.append(value.strip())
                    # DisplayIcon often "path,0"
                    if "," in value:
                        out.append(value.split(",", 1)[0].strip())
    except OSError:
        pass
    return out


def _registry_candidates() -> list[str]:
    found: list[str] = []
    uninstall_roots = [
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
        ),
    ]
    for root, base in uninstall_roots:
        try:
            with winreg.OpenKey(root, base) as key:
                n = winreg.QueryInfoKey(key)[0]
                for i in range(n):
                    try:
                        sub = winreg.EnumKey(key, i)
                    except OSError:
                        continue
                    path = f"{base}\\{sub}"
                    vals = _reg_query_values(root, path)
                    display = " ".join(vals).lower()
                    if "cursor" not in display:
                        continue
                    for v in vals:
                        if v.lower().endswith("cursor.exe") or "cursor.exe" in v.lower():
                            found.append(v.split(",", 1)[0].strip())
                        # InstallLocation + Cursor.exe
                        if os.path.isdir(expand_path(v)):
                            cand = os.path.join(expand_path(v), "Cursor.exe")
                            found.append(cand)
        except OSError:
            continue

    app_paths = [
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\App Paths\Cursor.exe"),
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\App Paths\Cursor.exe"),
    ]
    for root, path in app_paths:
        try:
            with winreg.OpenKey(root, path) as key:
                val, _ = winreg.QueryValueEx(key, None)
                if isinstance(val, str):
                    found.append(val)
        except OSError:
            pass
    return found


def _path_which() -> Optional[str]:
    for name in ("Cursor.exe", "cursor.exe", "cursor"):
        hit = shutil.which(name)
        if _is_cursor_exe(hit):
            return expand_path(hit)
    return None


def _persist_verified(path: str, cfg: dict[str, Any]) -> None:
    cfg["cursor_exe_verified"] = path
    try:
        from runtime_paths import get_paths
        import json

        cfg_path = get_paths().config_path
        data: dict[str, Any] = {}
        if cfg_path.is_file():
            try:
                data = json.loads(cfg_path.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        if not isinstance(data, dict):
            data = {}
        data["cursor_exe_verified"] = path
        cfg_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except Exception as exc:
        log.debug("persist verified exe failed: %s", exc)


def resolve_cursor_executable(
    cfg: dict[str, Any] | None = None,
    *,
    binding: dict[str, Any] | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """
    Priority:
      1) bound process exe
      2) persisted verified path in config
      3) registry
      4) standard install locations
      5) PATH
      6) config cursor_exe_candidates
    """
    cfg = dict(cfg or {})
    tried: list[str] = []

    def _accept(path: str | None, source: str) -> dict[str, Any] | None:
        if not _is_cursor_exe(path):
            if path:
                tried.append(str(path))
            return None
        resolved = str(Path(expand_path(path)).resolve())
        if persist:
            _persist_verified(resolved, cfg)
        return {"ok": True, "path": resolved, "source": source, "tried": tried}

    hit = _accept(exe_from_binding(binding), "bound_process")
    if hit:
        return hit

    verified = cfg.get("cursor_exe_verified") or cfg.get("verified_cursor_exe")
    hit = _accept(str(verified) if verified else None, "persisted_verified")
    if hit:
        return hit

    for reg in _registry_candidates():
        hit = _accept(reg, "registry")
        if hit:
            return hit

    for std in _STANDARD_CANDIDATES:
        hit = _accept(std, "standard_path")
        if hit:
            return hit

    hit = _accept(_path_which(), "path_which")
    if hit:
        return hit

    for raw in cfg.get("cursor_exe_candidates") or []:
        hit = _accept(str(raw), "config_candidates")
        if hit:
            return hit

    return {
        "ok": False,
        "error": "cursor_exe_not_found",
        "tried": tried,
        "hint": "Set Cursor executable path in plugin Advanced settings",
    }
