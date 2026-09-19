"""Cursor Editor vs Agents Window role helpers (v0.7 Agents Pane).

Editor binding: Context Follow / Open note / Sidecar arrange.
Agent binding: Embedded Agents Pane visual embed only.

Does not use window titles to identify Agents Window — explicit HWND bind/diff only.
"""

from __future__ import annotations

from typing import Any

from window import (
    find_windows_by_process,
    snapshot_window,
    validate_window_binding,
    window_info_from_hwnd,
)


def migrate_cursor_roles(state: dict[str, Any]) -> dict[str, Any]:
    """Ensure cursor_editor / cursor_agent keys exist; keep legacy state['cursor']."""
    if not isinstance(state, dict):
        return {}
    editor = state.get("cursor_editor")
    if not isinstance(editor, dict) or not editor.get("hwnd"):
        legacy = state.get("cursor")
        if isinstance(legacy, dict) and legacy.get("hwnd"):
            state["cursor_editor"] = dict(legacy)
    if "cursor_agent" not in state:
        state["cursor_agent"] = None
    # Keep legacy cursor == editor for v0.6 consumers
    if isinstance(state.get("cursor_editor"), dict):
        state["cursor"] = state["cursor_editor"]
    return state


def get_editor_binding(state: dict[str, Any]) -> dict[str, Any] | None:
    migrate_cursor_roles(state)
    ed = state.get("cursor_editor") or state.get("cursor")
    return ed if isinstance(ed, dict) and ed.get("hwnd") else None


def get_agent_binding(state: dict[str, Any]) -> dict[str, Any] | None:
    migrate_cursor_roles(state)
    ag = state.get("cursor_agent")
    return ag if isinstance(ag, dict) and ag.get("hwnd") else None


def editor_hwnd(state: dict[str, Any]) -> int:
    ed = get_editor_binding(state)
    try:
        return int((ed or {}).get("hwnd") or 0)
    except (TypeError, ValueError):
        return 0


def agent_hwnd(state: dict[str, Any]) -> int:
    ag = get_agent_binding(state)
    try:
        return int((ag or {}).get("hwnd") or 0)
    except (TypeError, ValueError):
        return 0


def list_cursor_top_level(process_name: str = "Cursor.exe") -> list[dict[str, Any]]:
    """Visible top-level Cursor windows (no title heuristics)."""
    out: list[dict[str, Any]] = []
    for info in find_windows_by_process(process_name, allow_minimized=True):
        out.append(
            {
                "hwnd": int(info.hwnd),
                "pid": int(info.pid),
                "title": info.title,
                "rect": list(info.rect.as_tuple()),
                "process_name": info.process_name,
            }
        )
    return out


def hwnd_set(windows: list[dict[str, Any]] | None) -> set[int]:
    s: set[int] = set()
    for w in windows or []:
        try:
            h = int(w.get("hwnd") or 0)
        except (TypeError, ValueError):
            continue
        if h:
            s.add(h)
    return s


def diff_new_hwnds(
    before: list[dict[str, Any]] | None,
    after: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    before_ids = hwnd_set(before)
    return [w for w in (after or []) if int(w.get("hwnd") or 0) not in before_ids]


def candidates_excluding_editor(
    windows: list[dict[str, Any]],
    editor_hwnd_i: int,
) -> list[dict[str, Any]]:
    eh = int(editor_hwnd_i or 0)
    return [w for w in windows if int(w.get("hwnd") or 0) != eh]


def bind_agent_from_hwnd(hwnd: int, process_name: str = "Cursor.exe") -> dict[str, Any] | None:
    info = window_info_from_hwnd(int(hwnd))
    if not info:
        return None
    snap = snapshot_window(int(hwnd))
    snap["process"] = process_name
    snap["role"] = "agent"
    return snap


def agent_binding_ok(state: dict[str, Any]) -> dict[str, Any]:
    ag = get_agent_binding(state)
    if not ag:
        return {"ok": False, "reason": "no_agent_binding"}
    check = validate_window_binding(ag)
    if not check.get("ok"):
        return {"ok": False, "reason": "stale_agent", "detail": check}
    # Must not be the same HWND as editor
    eh = editor_hwnd(state)
    ah = agent_hwnd(state)
    if eh and ah and eh == ah:
        return {"ok": False, "reason": "agent_equals_editor"}
    return {"ok": True, "binding": ag}


def editor_binding_ok(state: dict[str, Any]) -> dict[str, Any]:
    ed = get_editor_binding(state)
    if not ed:
        return {"ok": False, "reason": "no_editor_binding"}
    check = validate_window_binding(ed)
    if not check.get("ok"):
        return {"ok": False, "reason": "stale_editor", "detail": check}
    return {"ok": True, "binding": ed}


def clear_agent_binding(state: dict[str, Any], *, reason: str = "cleared") -> dict[str, Any]:
    state["cursor_agent"] = None
    state["agent_stale_reason"] = reason
    state["agent_bind_before"] = None
    return state


def independent_bindings_intact(state: dict[str, Any]) -> dict[str, bool]:
    """Stale editor must not clear agent, and vice versa (logic check helper)."""
    ed = editor_binding_ok(state)
    ag = agent_binding_ok(state)
    return {
        "editor_ok": bool(ed.get("ok")),
        "agent_ok": bool(ag.get("ok")),
        "independent": True,
    }
