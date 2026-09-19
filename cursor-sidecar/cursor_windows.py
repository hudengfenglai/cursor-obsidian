"""Score-based Cursor Editor vs Agents Window classifier + binding refresh (v0.7.2).

Title is a weak signal only. Prefer menu / UIA / structure markers.
Agent HWND must NEVER become editor binding.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable, Optional

import win32con
import win32gui

from window import (
    find_windows_by_process,
    snapshot_window,
    validate_window_binding,
    window_info_from_hwnd,
)

log = logging.getLogger("cursor_sidecar.cursor_windows")

ROLE_EDITOR = "EDITOR"
ROLE_AGENT = "AGENT"
ROLE_UNKNOWN = "UNKNOWN"

# Minimum absolute score to classify (otherwise UNKNOWN)
CLASSIFY_THRESHOLD = 2

_TITLE_AGENT_RE = re.compile(r"\bagents?\b", re.I)
_TITLE_EDITOR_RE = re.compile(
    r"\b(editor|welcome|untitled|\.md|\.py|\.ts|\.tsx|\.js|\.jsx|\.json|\.rs|\.go)\b",
    re.I,
)
# Classic VS Code / Cursor title: "file - folder - Cursor"
_TITLE_ELECTRON_EDITOR_RE = re.compile(r".+\s+-\s+.+\s+-\s+Cursor\s*$", re.I)
_TITLE_CURSOR_ONLY_RE = re.compile(r"^Cursor\s*$", re.I)
_TITLE_AGENTS_PRODUCT_RE = re.compile(r"^Cursor\s+Agents?\b|\bAgents?\s+Window\b", re.I)


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


def _normalize_menu_label(text: str) -> str:
    t = (text or "").replace("&", "")
    t = re.sub(r"\t.*$", "", t)
    t = re.sub(r"\s+", " ", t).strip().lower()
    return t


def _menu_labels(hwnd: int, *, max_depth: int = 3) -> list[str]:
    """Collect Win32 native menu item labels (best-effort)."""
    labels: list[str] = []
    try:
        menu = win32gui.GetMenu(int(hwnd))
    except Exception:
        return labels
    if not menu:
        return labels

    def _walk(hmenu: int, depth: int) -> None:
        if not hmenu or depth > max_depth:
            return
        try:
            count = int(win32gui.GetMenuItemCount(hmenu))
        except Exception:
            return
        for i in range(max(count, 0)):
            try:
                # MF_BYPOSITION | MIIM_STRING via GetMenuString
                text = win32gui.GetMenuString(hmenu, i, win32con.MF_BYPOSITION)
            except Exception:
                text = ""
            if text:
                labels.append(str(text))
            try:
                sub = win32gui.GetSubMenu(hmenu, i)
            except Exception:
                sub = 0
            if sub:
                _walk(int(sub), depth + 1)

    _walk(int(menu), 0)
    return labels


def _uia_name_hits(hwnd: int) -> dict[str, int]:
    """Optional UIA name scan. Returns marker hit counts. Never raises."""
    hits = {"editor_window": 0, "agents_window": 0, "new_agents": 0}
    try:
        from agents_uia import scan_window_name_markers

        scanned = scan_window_name_markers(int(hwnd))
        if isinstance(scanned, dict):
            for k in hits:
                hits[k] = int(scanned.get(k) or 0)
    except Exception:
        pass
    return hits


def score_cursor_window(hwnd: int, *, title: str | None = None) -> dict[str, Any]:
    """Compute editor/agent scores for a top-level Cursor HWND."""
    hwnd = int(hwnd or 0)
    editor_score = 0
    agent_score = 0
    signals: list[str] = []

    if not hwnd or not win32gui.IsWindow(hwnd):
        return {
            "hwnd": hwnd or None,
            "role": ROLE_UNKNOWN,
            "editor_score": 0,
            "agent_score": 0,
            "signals": ["invalid_hwnd"],
        }

    if title is None:
        try:
            title = win32gui.GetWindowText(hwnd) or ""
        except Exception:
            title = ""
    title_l = (title or "").lower().strip()

    # --- Title signals (Electron often has no Win32 menu; titles must carry weight) ---
    if _TITLE_AGENTS_PRODUCT_RE.search(title or "") or (
        _TITLE_AGENT_RE.search(title_l) and " - " not in title_l
    ):
        agent_score += 2
        signals.append("title_agents_product")
    elif _TITLE_AGENT_RE.search(title_l) and "editor" not in title_l:
        agent_score += 1
        signals.append("title_agents")

    if _TITLE_ELECTRON_EDITOR_RE.match(title or ""):
        # e.g. "init_skill.py - cursor-obsidian - Cursor"
        editor_score += 2
        signals.append("title_electron_editor")
    if _TITLE_EDITOR_RE.search(title_l) and "agent" not in title_l:
        editor_score += 1
        signals.append("title_editorish")
    if _TITLE_CURSOR_ONLY_RE.match(title or ""):
        # Bare "Cursor" is ambiguous (Agents Window often uses this title).
        # Do not bias toward EDITOR — leave UNKNOWN unless other signals fire.
        signals.append("title_cursor_only_ambiguous")

    # Native menu markers (strong ±2) — often empty on Electron
    labels = [_normalize_menu_label(x) for x in _menu_labels(hwnd)]
    joined = " | ".join(labels)
    if any("new agents window" in x for x in labels) or "new agents window" in joined:
        editor_score += 2
        signals.append("menu_new_agents")
    if any(x == "agents window" or x.endswith("agents window") for x in labels):
        if not any("new agents" in x for x in labels):
            editor_score += 1
            signals.append("menu_agents_entry")
    if any("open editor window" in x or x == "editor window" for x in labels):
        agent_score += 2
        signals.append("menu_open_editor")
    if any("new agent" == x or x.startswith("new agent ") for x in labels):
        agent_score += 1
        signals.append("menu_new_agent")

    # UIA markers (strong ±2)
    uia = _uia_name_hits(hwnd)
    if uia.get("editor_window"):
        agent_score += 2
        signals.append("uia_editor_window")
    if uia.get("agents_window") or uia.get("new_agents"):
        editor_score += 2
        signals.append("uia_agents_entry")

    role = ROLE_UNKNOWN
    if editor_score >= CLASSIFY_THRESHOLD and editor_score > agent_score:
        role = ROLE_EDITOR
    elif agent_score >= CLASSIFY_THRESHOLD and agent_score > editor_score:
        role = ROLE_AGENT
    elif editor_score >= CLASSIFY_THRESHOLD and agent_score >= CLASSIFY_THRESHOLD:
        role = ROLE_UNKNOWN
        signals.append("score_tie")

    return {
        "hwnd": hwnd,
        "title": title,
        "role": role,
        "editor_score": editor_score,
        "agent_score": agent_score,
        "signals": signals,
    }


def classify_cursor_window(hwnd: int, *, title: str | None = None) -> str:
    """Return EDITOR | AGENT | UNKNOWN."""
    return str(score_cursor_window(hwnd, title=title).get("role") or ROLE_UNKNOWN)


def list_cursor_top_level(process_name: str = "Cursor.exe") -> list[dict[str, Any]]:
    """Visible + minimized top-level Cursor windows."""
    out: list[dict[str, Any]] = []
    for info in find_windows_by_process(process_name, allow_minimized=True):
        out.append(
            {
                "hwnd": int(info.hwnd),
                "pid": int(info.pid),
                "title": info.title,
                "rect": list(info.rect.as_tuple()),
                "process_name": info.process_name,
                "minimized": bool(win32gui.IsIconic(int(info.hwnd))),
                "visible": bool(win32gui.IsWindowVisible(int(info.hwnd))),
            }
        )
    return out


def apply_companion_agent_heuristic(
    classified: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """If exactly one EDITOR and exactly one other top-level Cursor window, that
    companion is the Agents Window (product: at most one Agents Window).

    Electron Agents titles are often just "Cursor" → UNKNOWN without this.
    """
    if not classified:
        return classified
    editors = [w for w in classified if str(w.get("role") or "") == ROLE_EDITOR]
    others = [w for w in classified if str(w.get("role") or "") != ROLE_EDITOR]
    if len(editors) != 1 or len(others) != 1:
        return classified
    companion = others[0]
    if str(companion.get("role") or "") == ROLE_AGENT:
        return classified
    promoted = dict(companion)
    promoted["role"] = ROLE_AGENT
    promoted["agent_score"] = max(int(promoted.get("agent_score") or 0), CLASSIFY_THRESHOLD)
    sigs = list(promoted.get("signals") or [])
    if "sole_companion_of_editor" not in sigs:
        sigs.append("sole_companion_of_editor")
    promoted["signals"] = sigs
    out: list[dict[str, Any]] = []
    for w in classified:
        if int(w.get("hwnd") or 0) == int(promoted.get("hwnd") or 0):
            out.append(promoted)
        else:
            out.append(w)
    return out


def list_cursor_windows_classified(
    process_name: str = "Cursor.exe",
    *,
    classify_fn: Callable[[int], dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    classify_fn = classify_fn or (lambda h: score_cursor_window(h))
    out: list[dict[str, Any]] = []
    for w in list_cursor_top_level(process_name):
        scored = classify_fn(int(w["hwnd"]))
        merged = dict(w)
        merged.update(
            {
                "role": scored.get("role"),
                "editor_score": scored.get("editor_score"),
                "agent_score": scored.get("agent_score"),
                "signals": scored.get("signals"),
            }
        )
        out.append(merged)
    return apply_companion_agent_heuristic(out)


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


def editor_candidates(
    windows: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Only ROLE_EDITOR — Agents can never be editor candidates."""
    return [w for w in (windows or []) if str(w.get("role") or "") == ROLE_EDITOR]


def agent_candidates(
    windows: list[dict[str, Any]] | None,
    *,
    exclude_hwnd: int = 0,
) -> list[dict[str, Any]]:
    eh = int(exclude_hwnd or 0)
    out = []
    for w in windows or []:
        if str(w.get("role") or "") != ROLE_AGENT:
            continue
        if eh and int(w.get("hwnd") or 0) == eh:
            continue
        out.append(w)
    return out


def select_editor_window(
    process_name: str = "Cursor.exe",
    *,
    classify_fn: Callable[[int], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Pick exactly one EDITOR window for Attach.
    Never returns an AGENT hwnd.
    """
    classified = list_cursor_windows_classified(process_name, classify_fn=classify_fn)
    editors = editor_candidates(classified)
    agents = agent_candidates(classified)
    unknowns = [w for w in classified if str(w.get("role")) == ROLE_UNKNOWN]
    if len(editors) == 1:
        return {
            "ok": True,
            "window": editors[0],
            "classified": classified,
            "editor_count": 1,
            "agent_count": len(agents),
            "unknown_count": len(unknowns),
        }
    if len(editors) > 1:
        return {
            "ok": False,
            "error": "editor_window_ambiguous",
            "candidates": editors,
            "classified": classified,
            "editor_count": len(editors),
            "agent_count": len(agents),
            "unknown_count": len(unknowns),
        }
    return {
        "ok": False,
        "error": "editor_window_not_found",
        "classified": classified,
        "editor_count": 0,
        "agent_count": len(agents),
        "unknown_count": len(unknowns),
        "hint": "Agents Window cannot become Editor binding",
    }


def bind_agent_from_hwnd(hwnd: int, process_name: str = "Cursor.exe") -> dict[str, Any] | None:
    info = window_info_from_hwnd(int(hwnd))
    if not info:
        return None
    snap = snapshot_window(int(hwnd))
    snap["process"] = process_name
    snap["role"] = "agent"
    return snap


def bind_editor_from_hwnd(hwnd: int, process_name: str = "Cursor.exe") -> dict[str, Any] | None:
    # Hard gate: refuse AGENT classification
    role = classify_cursor_window(int(hwnd))
    if role == ROLE_AGENT:
        return None
    info = window_info_from_hwnd(int(hwnd))
    if not info:
        return None
    snap = snapshot_window(int(hwnd))
    snap["process"] = process_name
    snap["role"] = "editor"
    snap["classified_role"] = role
    return snap


def agent_binding_ok(state: dict[str, Any]) -> dict[str, Any]:
    ag = get_agent_binding(state)
    if not ag:
        return {"ok": False, "reason": "no_agent_binding"}
    check = validate_window_binding(ag)
    if not check.get("ok"):
        return {"ok": False, "reason": "stale_agent", "detail": check}
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
    # Live role: refuse if HWND is now classified AGENT
    try:
        role = classify_cursor_window(int(ed.get("hwnd") or 0))
        if role == ROLE_AGENT:
            return {"ok": False, "reason": "editor_is_agent", "role": role}
    except Exception:
        pass
    return {"ok": True, "binding": ed}


def clear_agent_binding(state: dict[str, Any], *, reason: str = "cleared") -> dict[str, Any]:
    """Atomic clear of agent + embed claims. Does not touch editor."""
    state["cursor_agent"] = None
    state["agent_bound"] = False
    state["agent_stale_reason"] = reason
    state["agent_bind_before"] = None
    state["embedded"] = False
    state["native_child"] = False
    state["native_child_pending"] = False
    state["embed_mode"] = "sidecar"
    return state


def clear_editor_binding(state: dict[str, Any], *, reason: str = "cleared") -> dict[str, Any]:
    """Clear editor only — never touches agent."""
    state["cursor_editor"] = None
    state["cursor"] = None
    state["editor_stale_reason"] = reason
    return state


def refresh_cursor_bindings(state: dict[str, Any]) -> dict[str, Any]:
    """
    Live-validate editor and agent independently.
    Invalid agent → atomic clear agent/embed.
    Invalid editor → clear editor only.
    Never fallback agent→editor.
    """
    migrate_cursor_roles(state)
    result: dict[str, Any] = {
        "editor_ok": False,
        "agent_ok": False,
        "cleared_editor": False,
        "cleared_agent": False,
    }

    ed_ok = editor_binding_ok(state)
    if ed_ok.get("ok"):
        result["editor_ok"] = True
    else:
        if get_editor_binding(state):
            clear_editor_binding(state, reason=str(ed_ok.get("reason") or "stale_editor"))
            result["cleared_editor"] = True
        result["editor_reason"] = ed_ok.get("reason")

    ag_ok = agent_binding_ok(state)
    if ag_ok.get("ok"):
        result["agent_ok"] = True
    else:
        if get_agent_binding(state):
            clear_agent_binding(state, reason=str(ag_ok.get("reason") or "stale_agent"))
            result["cleared_agent"] = True
        result["agent_reason"] = ag_ok.get("reason")

    # Re-sync legacy cursor key from editor if present
    if isinstance(state.get("cursor_editor"), dict) and state["cursor_editor"].get("hwnd"):
        state["cursor"] = state["cursor_editor"]

    result["agent_bound"] = bool(result["agent_ok"])
    result["editor_hwnd"] = editor_hwnd(state) or None
    result["agent_hwnd"] = agent_hwnd(state) or None
    return result


def independent_bindings_intact(state: dict[str, Any]) -> dict[str, bool]:
    ed = editor_binding_ok(state)
    ag = agent_binding_ok(state)
    return {
        "editor_ok": bool(ed.get("ok")),
        "agent_ok": bool(ag.get("ok")),
        "independent": True,
    }


def show_window_noactivate(hwnd: int) -> None:
    """Restore/show without forcing foreground steal when possible."""
    hwnd = int(hwnd)
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_SHOWNOACTIVATE)
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        else:
            win32gui.ShowWindow(hwnd, win32con.SW_SHOWNOACTIVATE)
    except Exception:
        pass
