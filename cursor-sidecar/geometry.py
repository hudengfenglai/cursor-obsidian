"""Pure geometry / state helpers (no Win32) — safe for any-platform unit tests."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def normalize_ratios(obsidian_ratio: float, cursor_ratio: float) -> tuple[float, float]:
    o = float(obsidian_ratio)
    c = float(cursor_ratio)
    if o <= 0 or c <= 0:
        raise ValueError("obsidian_ratio and cursor_ratio must be > 0")
    return o, c


def compute_split_rects(
    work: tuple[int, int, int, int],
    obsidian_ratio: float,
    cursor_ratio: float,
    gap: int = 0,
) -> tuple[tuple[int, int, int, int], tuple[int, int, int, int]]:
    """Split a work-area rect into left (Obsidian) and right (Cursor)."""
    o, c = normalize_ratios(obsidian_ratio, cursor_ratio)
    left0, top, right0, bottom = work
    width = right0 - left0
    gap = max(int(gap), 0)
    usable = max(width - gap, 1)
    left_w = int(round(usable * (o / (o + c))))
    right_w = usable - left_w
    left = (left0, top, left0 + left_w, bottom)
    right = (left0 + left_w + gap, top, left0 + left_w + gap + right_w, bottom)
    return left, right


def placement_dict(
    flags: int,
    show_cmd: int,
    min_pos: tuple[int, int] | list[int],
    max_pos: tuple[int, int] | list[int],
    normal_rect: tuple[int, int, int, int] | list[int],
) -> dict[str, Any]:
    return {
        "flags": int(flags),
        "show_cmd": int(show_cmd),
        "min_position": [int(min_pos[0]), int(min_pos[1])],
        "max_position": [int(max_pos[0]), int(max_pos[1])],
        "normal_position": [
            int(normal_rect[0]),
            int(normal_rect[1]),
            int(normal_rect[2]),
            int(normal_rect[3]),
        ],
    }


def placement_tuple(data: dict[str, Any]) -> tuple:
    """Rebuild argument for SetWindowPlacement-style restore."""
    return (
        int(data.get("flags", 0)),
        int(data["show_cmd"]),
        tuple(data["min_position"]),
        tuple(data["max_position"]),
        tuple(data["normal_position"]),
    )


def is_maximized_show_cmd(show_cmd: int) -> bool:
    return int(show_cmd) == 3


def is_minimized_show_cmd(show_cmd: int) -> bool:
    return int(show_cmd) in (2, 6)


def _norm_proc(name: str | None) -> str:
    if not name:
        return ""
    return name.lower().removesuffix(".exe")


def validate_binding_identity(
    record: dict[str, Any] | None,
    *,
    hwnd_exists: bool,
    live_pid: int | None,
    live_process_name: str | None = None,
    live_create_time: float | None = None,
    create_time_tolerance: float = 1.0,
) -> dict[str, Any]:
    """
    Pure binding check given live process facts from the platform layer.

    Returns ok / legacy_binding / restore_safe / binding_mode / reason.
    """
    if not record:
        return {
            "ok": False,
            "legacy_binding": False,
            "restore_safe": False,
            "binding_mode": "none",
            "reason": "missing_record",
        }

    try:
        expected_hwnd = int(record["hwnd"])
        expected_pid = int(record["pid"])
    except (KeyError, TypeError, ValueError):
        return {
            "ok": False,
            "legacy_binding": False,
            "restore_safe": False,
            "binding_mode": "invalid",
            "reason": "bad_hwnd_pid",
        }

    if not hwnd_exists:
        return {
            "ok": False,
            "legacy_binding": "process_create_time" not in record,
            "restore_safe": False,
            "binding_mode": "hwnd_gone",
            "reason": "hwnd_gone",
        }

    if live_pid is None or int(live_pid) != expected_pid:
        return {
            "ok": False,
            "legacy_binding": "process_create_time" not in record,
            "restore_safe": False,
            "binding_mode": "pid_mismatch",
            "reason": "pid_mismatch",
        }

    has_name = bool(record.get("process_name"))
    has_ctime = record.get("process_create_time") is not None
    legacy = not has_ctime

    if has_name and live_process_name is not None:
        if _norm_proc(str(record["process_name"])) != _norm_proc(live_process_name):
            return {
                "ok": False,
                "legacy_binding": legacy,
                "restore_safe": False,
                "binding_mode": "process_name_mismatch",
                "reason": "process_name_mismatch",
            }

    if has_ctime:
        if live_create_time is None:
            return {
                "ok": False,
                "legacy_binding": False,
                "restore_safe": False,
                "binding_mode": "hwnd_pid_process_start",
                "reason": "create_time_unavailable",
            }
        expected_ct = float(record["process_create_time"])
        if abs(float(live_create_time) - expected_ct) > create_time_tolerance:
            return {
                "ok": False,
                "legacy_binding": False,
                "restore_safe": False,
                "binding_mode": "hwnd_pid_process_start",
                "reason": "create_time_mismatch",
            }
        mode = "hwnd_pid_process_start"
    elif has_name:
        mode = "hwnd_pid_process"
    else:
        mode = "hwnd_pid"

    return {
        "ok": True,
        "legacy_binding": legacy,
        "restore_safe": True,  # exact HWND match — never fallback to another window
        "binding_mode": mode,
        "reason": "ok",
        "hwnd": expected_hwnd,
        "pid": expected_pid,
    }


def can_restore_bound_window(
    snap: dict[str, Any] | None,
    *,
    binding_ok: bool,
) -> str:
    """
    Detach restore policy (no soft rediscovery).

    Returns: skip | gone | restore
    """
    if not snap:
        return "skip"
    if not binding_ok:
        return "gone"
    return "restore"


def evaluate_attachment(
    state: dict[str, Any],
    *,
    obsidian_live: bool,
    cursor_live: bool,
) -> dict[str, Any]:
    claimed = bool(state.get("attached"))
    if not claimed:
        return {
            "attached": False,
            "state_valid": True,
            "reason": "detached",
        }

    obs = state.get("obsidian") or {}
    cur = state.get("cursor") or {}
    has_shape = bool(obs.get("hwnd") and obs.get("pid") and cur.get("hwnd") and cur.get("pid"))
    if not has_shape:
        return {
            "attached": False,
            "state_valid": False,
            "reason": "incomplete_bindings",
        }

    if not obsidian_live and not cursor_live:
        return {
            "attached": False,
            "state_valid": False,
            "reason": "both_windows_gone",
        }

    if not cursor_live:
        return {
            "attached": False,
            "state_valid": False,
            "reason": "cursor_gone",
        }

    if not obsidian_live:
        return {
            "attached": False,
            "state_valid": False,
            "reason": "obsidian_gone",
        }

    return {
        "attached": True,
        "state_valid": True,
        "reason": "ok",
    }


def window_binding_valid(record: dict[str, Any] | None, *, live_hwnd_ok: bool) -> bool:
    if not record:
        return False
    if "hwnd" not in record or "pid" not in record:
        return False
    try:
        int(record["hwnd"])
        int(record["pid"])
    except (TypeError, ValueError):
        return False
    return bool(live_hwnd_ok)


def serialize_state(state: dict[str, Any]) -> str:
    return json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True)


def deserialize_state(raw: str) -> dict[str, Any]:
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("state must be an object")
    return data


def atomic_write_text(path: Path | str, text: str) -> None:
    """Write via temp file + os.replace to avoid truncated JSON on crash."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, target)


def recover_state_dict(raw: str | None) -> dict[str, Any]:
    """Malformed / empty state → empty dict (safe detached)."""
    if not raw or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return data
