"""Pure geometry / state helpers (no Win32) — safe for any-platform unit tests."""

from __future__ import annotations

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
    # SW_SHOWMAXIMIZED = 3
    return int(show_cmd) == 3


def is_minimized_show_cmd(show_cmd: int) -> bool:
    # SW_SHOWMINIMIZED = 2, SW_MINIMIZE = 6
    return int(show_cmd) in (2, 6)


def window_binding_valid(record: dict[str, Any] | None, *, live_hwnd_ok: bool) -> bool:
    """Pure check: binding record shape + optional live hwnd flag from platform layer."""
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


def evaluate_attachment(
    state: dict[str, Any],
    *,
    obsidian_live: bool,
    cursor_live: bool,
) -> dict[str, Any]:
    """
    Decide whether state should still count as attached.

    Returns:
      attached: effective attachment
      state_valid: bindings look coherent
      reason: short code
    """
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

    # Spec D: if Cursor manually closed, must not stay attached=true
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


def serialize_state(state: dict[str, Any]) -> str:
    import json

    return json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True)


def deserialize_state(raw: str) -> dict[str, Any]:
    import json

    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("state must be an object")
    return data
