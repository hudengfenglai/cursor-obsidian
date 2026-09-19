"""Runtime path helpers for source and frozen (PyInstaller) modes.

CODE_DIR  — where the helper lives (source package or exe directory)
DATA_DIR  — writable runtime files (config/state/token/pid/meta)
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

VERSION = "0.5.0"

DEFAULT_CONFIG_VALUES: dict[str, Any] = {
    "preset": "normal",
    "gap": 0,
    "monitor": None,
    "obsidian_process": "Obsidian.exe",
    "cursor_process": "Cursor.exe",
    "obsidian_title_hint": "Obsidian",
    "cursor_title_hint": "Cursor",
    "live_follow": True,
    "follow_debounce_ms": 40,
    "debug": False,
    "poll_ms": 500,
    "follow_obsidian": False,
    "daemon_host": "127.0.0.1",
    "daemon_port": 27845,
    "launch_cursor_if_missing": True,
    "cursor_exe_candidates": [
        r"%LOCALAPPDATA%\Programs\cursor\Cursor.exe",
        r"%LOCALAPPDATA%\cursor\Cursor.exe",
    ],
}


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False)) or hasattr(sys, "_MEIPASS")


def runtime_mode() -> str:
    return "frozen" if is_frozen() else "source"


def code_dir() -> Path:
    """Directory of the helper binary or source package."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def default_frozen_data_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "CursorSidecar"
    return Path.home() / "AppData" / "Local" / "CursorSidecar"


def resolve_data_dir(
    *,
    cli_override: str | None = None,
    env: Optional[dict[str, str]] = None,
    frozen: bool | None = None,
    source_fallback: Path | None = None,
) -> Path:
    """
    Priority:
      --data-dir / cli_override
      → CURSOR_SIDECAR_DATA_DIR
      → frozen: %LOCALAPPDATA%\\CursorSidecar
      → source: source package directory
    """
    if cli_override:
        return Path(cli_override).expanduser().resolve()
    environ = env if env is not None else os.environ
    env_dir = (environ.get("CURSOR_SIDECAR_DATA_DIR") or "").strip()
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    use_frozen = is_frozen() if frozen is None else bool(frozen)
    if use_frozen:
        local = environ.get("LOCALAPPDATA")
        if local:
            return (Path(local) / "CursorSidecar").resolve()
        return default_frozen_data_dir().resolve()
    return (source_fallback or code_dir()).resolve()


@dataclass(frozen=True)
class RuntimePaths:
    code_dir: Path
    data_dir: Path
    config_path: Path
    state_path: Path
    daemon_pid_path: Path
    daemon_meta_path: Path
    daemon_token_path: Path
    follow_pid_path: Path
    helper_executable: Path
    main_py: Path
    runtime_mode: str


_ACTIVE: RuntimePaths | None = None


def helper_executable() -> Path:
    if is_frozen():
        return Path(sys.executable).resolve()
    # Source mode: no single helper exe; callers use build_self_command()
    return Path(sys.executable).resolve()


def build_self_command(*args: str, python: str | None = None) -> list[str]:
    """Build argv to re-invoke this helper (daemon spawn / restart)."""
    extra = [str(a) for a in args]
    if is_frozen():
        return [str(Path(sys.executable).resolve()), *extra]
    py = python or sys.executable
    return [str(py), str(code_dir() / "main.py"), *extra]


def ensure_data_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def build_paths(*, data_dir_override: str | None = None) -> RuntimePaths:
    cdir = code_dir()
    ddir = ensure_data_dir(resolve_data_dir(cli_override=data_dir_override))
    return RuntimePaths(
        code_dir=cdir,
        data_dir=ddir,
        config_path=ddir / "config.json",
        state_path=ddir / ".sidecar.state.json",
        daemon_pid_path=ddir / ".sidecar.daemon.pid",
        daemon_meta_path=ddir / ".sidecar.daemon.json",
        daemon_token_path=ddir / ".sidecar.daemon.token",
        follow_pid_path=ddir / ".sidecar.pid",
        helper_executable=helper_executable(),
        main_py=cdir / "main.py",
        runtime_mode=runtime_mode(),
    )


def init_runtime(*, data_dir_override: str | None = None) -> RuntimePaths:
    """Initialize (or re-initialize) process-global runtime paths."""
    global _ACTIVE
    _ACTIVE = build_paths(data_dir_override=data_dir_override)
    return _ACTIVE


def get_paths() -> RuntimePaths:
    global _ACTIVE
    if _ACTIVE is None:
        _ACTIVE = build_paths()
    return _ACTIVE


def application_dir() -> Path:
    return get_paths().code_dir


def data_dir() -> Path:
    return get_paths().data_dir


def config_path() -> Path:
    return get_paths().config_path


def state_path() -> Path:
    return get_paths().state_path


def daemon_meta_path() -> Path:
    return get_paths().daemon_meta_path


def daemon_pid_path() -> Path:
    return get_paths().daemon_pid_path


def daemon_token_path() -> Path:
    return get_paths().daemon_token_path
