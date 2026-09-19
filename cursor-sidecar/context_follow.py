"""Context Follow helpers (v0.6) — pure logic for debounce / suppression / filters.

Plugin mirrors these rules in JS; tests exercise this module directly.
Does not touch Cursor Agent / ACP / selection.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_DEBOUNCE_MS = 150
DEFAULT_SUPPRESS_S = 0.4


@dataclass
class ContextFollowGate:
    """Tracks last successful sync for duplicate suppression."""

    enabled: bool = False
    last_path: str | None = None
    last_line: int | None = None
    last_sync_at: float = 0.0
    last_relative_path: str | None = None

    def record(
        self,
        path: str,
        line: int | None,
        *,
        relative_path: str | None = None,
        now: float | None = None,
    ) -> None:
        self.last_path = path
        self.last_line = line
        self.last_relative_path = relative_path
        self.last_sync_at = float(time.time() if now is None else now)

    def status_dict(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.enabled),
            "last_path": self.last_relative_path or self.last_path,
            "last_sync_at": self.last_sync_at or None,
        }


def is_markdown_extension(name_or_ext: str) -> bool:
    ext = name_or_ext.lower().lstrip(".")
    if "." in ext:
        ext = Path(ext).suffix.lstrip(".").lower()
    return ext in ("md", "markdown")


def should_sync_line_column(*, extension: str | None, is_markdown_editor: bool) -> bool:
    """Only Markdown editor views sync line/column at file switch."""
    if not is_markdown_editor:
        return False
    return is_markdown_extension(extension or "md")


def is_excluded_rel_path(rel_path: str, *, config_dir: str = ".obsidian") -> bool:
    """Reject vault config / hidden system paths (vault-relative)."""
    raw = (rel_path or "").replace("\\", "/").lstrip("/")
    if not raw:
        return True
    cfg = (config_dir or ".obsidian").replace("\\", "/").strip("/")
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if not parts:
        return True
    if parts[0] == cfg:
        return True
    if parts[0] == ".obsidian":
        return True
    if parts[0].startswith(".") and parts[0] not in (".", ".."):
        return True
    return False


def is_path_inside_vault(vault_root: str | Path, file_path: str | Path) -> bool:
    try:
        vault = Path(vault_root).resolve()
        target = Path(file_path).resolve()
        target.relative_to(vault)
        return True
    except (OSError, ValueError):
        return False


def should_accept_sync(
    *,
    enabled: bool,
    attached: bool,
    path: str | None,
    line: int | None = None,
    last_path: str | None = None,
    last_line: int | None = None,
    last_sync_at: float = 0.0,
    now: float | None = None,
    suppress_window_s: float = DEFAULT_SUPPRESS_S,
    binding_ok: bool = True,
) -> tuple[bool, str]:
    """Return (accept, reason). Pure decision for Context Follow."""
    if not enabled:
        return False, "disabled"
    if not attached:
        return False, "detached"
    if not binding_ok:
        return False, "stale_binding"
    if not path:
        return False, "no_path"
    ts = float(time.time() if now is None else now)
    if last_path and _norm_path(last_path) == _norm_path(path):
        if last_line == line and (ts - float(last_sync_at or 0.0)) < float(suppress_window_s):
            return False, "duplicate"
        if line is None and last_line is None and (ts - float(last_sync_at or 0.0)) < float(
            suppress_window_s
        ):
            return False, "duplicate"
    return True, "ok"


def _norm_path(p: str) -> str:
    return Path(p).as_posix().lower()


def debounce_should_fire(pending_generation: int, fired_generation: int) -> bool:
    """Only the latest scheduled debounce callback should run."""
    return int(pending_generation) == int(fired_generation)


@dataclass
class DebounceBox:
    """Simple generation counter for debounce cancellation."""

    generation: int = 0
    delay_ms: int = DEFAULT_DEBOUNCE_MS

    def schedule(self) -> int:
        self.generation += 1
        return self.generation

    def is_current(self, gen: int) -> bool:
        return debounce_should_fire(self.generation, gen)
