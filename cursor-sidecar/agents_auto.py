"""Auto-discover / auto-bind Cursor Agents Window (v0.7.2).

Order:
  1) refresh + existing valid binding
  2) enumerate+classify → unique AGENT (restore if hidden)
  3) trigger: win32_menu → uia_menu → command_palette
  4) wait by classification (not strict HWND diff of 1)
Never visual fallback. Never bind AGENT as editor.
"""

from __future__ import annotations

import logging
import time
from ctypes import Structure, Union, byref, c_ulong, c_void_p, sizeof, windll
from ctypes.wintypes import DWORD, WORD
from typing import Any, Callable

import win32con
import win32gui

from agents_menu import trigger_new_agents_via_win32_menu
from cursor_windows import (
    ROLE_AGENT,
    agent_binding_ok,
    agent_candidates,
    agent_hwnd,
    bind_agent_from_hwnd,
    editor_hwnd,
    list_cursor_top_level,
    list_cursor_windows_classified,
    refresh_cursor_bindings,
    show_window_noactivate,
)
from window import focus_window, get_foreground_hwnd, restore_foreground_hwnd

log = logging.getLogger("cursor_sidecar.agents_auto")

VK_CONTROL = 0x11
VK_SHIFT = 0x10
VK_RETURN = 0x0D
VK_ESCAPE = 0x1B
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

_PALETTE_QUERIES = (
    "New Agents Window",
    "Agents Window",
)


class KEYBDINPUT(Structure):
    _fields_ = [
        ("wVk", WORD),
        ("wScan", WORD),
        ("dwFlags", DWORD),
        ("time", DWORD),
        ("dwExtraInfo", c_void_p),
    ]


class _INPUTUNION(Union):
    _fields_ = [("ki", KEYBDINPUT)]


class INPUT(Structure):
    _anonymous_ = ("union",)
    _fields_ = [("type", c_ulong), ("union", _INPUTUNION)]


def _send_input(*inputs: INPUT) -> None:
    n = len(inputs)
    if n == 0:
        return
    arr = (INPUT * n)(*inputs)
    windll.user32.SendInput(n, byref(arr), sizeof(INPUT))


def _key_down(vk: int) -> INPUT:
    return INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(wVk=vk & 0xFFFF, wScan=0, dwFlags=0, time=0, dwExtraInfo=None))


def _key_up(vk: int) -> INPUT:
    return INPUT(
        type=INPUT_KEYBOARD,
        ki=KEYBDINPUT(wVk=vk & 0xFFFF, wScan=0, dwFlags=KEYEVENTF_KEYUP, time=0, dwExtraInfo=None),
    )


def _unicode_down(ch: str) -> INPUT:
    return INPUT(
        type=INPUT_KEYBOARD,
        ki=KEYBDINPUT(wVk=0, wScan=ord(ch), dwFlags=KEYEVENTF_UNICODE, time=0, dwExtraInfo=None),
    )


def _unicode_up(ch: str) -> INPUT:
    return INPUT(
        type=INPUT_KEYBOARD,
        ki=KEYBDINPUT(
            wVk=0, wScan=ord(ch), dwFlags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, time=0, dwExtraInfo=None
        ),
    )


def tap_vk(vk: int, *, pause: float = 0.04) -> None:
    _send_input(_key_down(vk), _key_up(vk))
    time.sleep(pause)


def chord(vks: list[int], *, pause: float = 0.08) -> None:
    downs = [_key_down(v) for v in vks]
    ups = [_key_up(v) for v in reversed(vks)]
    _send_input(*downs, *ups)
    time.sleep(pause)


def type_text(text: str, *, pause: float = 0.012) -> None:
    for ch in text:
        _send_input(_unicode_down(ch), _unicode_up(ch))
        time.sleep(pause)


def open_command_palette() -> None:
    chord([VK_CONTROL, VK_SHIFT, ord("P")])
    time.sleep(0.35)


def dismiss_palette() -> None:
    tap_vk(VK_ESCAPE, pause=0.08)
    tap_vk(VK_ESCAPE, pause=0.05)


def trigger_agents_window_via_palette(
    editor_hwnd_i: int,
    *,
    query: str | None = None,
) -> dict[str, Any]:
    eh = int(editor_hwnd_i or 0)
    if not eh or not win32gui.IsWindow(eh):
        return {"ok": False, "error": "editor_hwnd_invalid"}
    q = (query or _PALETTE_QUERIES[0]).strip()
    prev = get_foreground_hwnd()
    try:
        focus_window(eh)
        time.sleep(0.2)
        dismiss_palette()
        open_command_palette()
        type_text(q)
        time.sleep(0.28)
        tap_vk(VK_RETURN, pause=0.15)
        time.sleep(0.35)
        return {
            "ok": True,
            "trigger_method": "command_palette",
            "query": q,
            "prev_foreground": prev,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc), "trigger_method": "command_palette", "query": q}


def _diag_counts(classified: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "classified_editor_count": sum(1 for w in classified if w.get("role") == "EDITOR"),
        "classified_agent_count": sum(1 for w in classified if w.get("role") == ROLE_AGENT),
        "unknown_count": sum(1 for w in classified if w.get("role") == "UNKNOWN"),
    }


def find_existing_agent(
    *,
    process_name: str,
    editor_hwnd_i: int,
    classify_list_fn: Callable[[str], list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    classify_list_fn = classify_list_fn or list_cursor_windows_classified
    classified = classify_list_fn(process_name)
    agents = agent_candidates(classified, exclude_hwnd=int(editor_hwnd_i or 0))
    counts = _diag_counts(classified)
    if len(agents) == 1:
        return {"ok": True, "candidate": agents[0], "classified": classified, **counts}
    if len(agents) > 1:
        return {
            "ok": False,
            "error": "agent_window_ambiguous",
            "candidates": agents,
            "classified": classified,
            **counts,
        }
    return {"ok": False, "error": "agent_window_not_found", "classified": classified, **counts}


def wait_for_agent_classified(
    *,
    process_name: str,
    editor_hwnd_i: int,
    before: list[dict[str, Any]],
    timeout_s: float = 7.0,
    poll_s: float = 0.15,
    classify_list_fn: Callable[[str], list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Success: unique AGENT, optionally new HWND or newly classifiable/visible."""
    classify_list_fn = classify_list_fn or list_cursor_windows_classified
    before_ids = {int(w.get("hwnd") or 0) for w in before}
    deadline = time.time() + float(timeout_s)
    last: dict[str, Any] = {}
    while time.time() < deadline:
        classified = classify_list_fn(process_name)
        agents = agent_candidates(classified, exclude_hwnd=int(editor_hwnd_i or 0))
        counts = _diag_counts(classified)
        last = {
            "after_cursor_windows": classified,
            **counts,
        }
        if len(agents) == 1:
            cand = agents[0]
            return {
                "ok": True,
                "candidate": cand,
                "new_hwnd": int(cand.get("hwnd") or 0) not in before_ids,
                **last,
            }
        if len(agents) > 1:
            return {
                "ok": False,
                "error": "agent_window_ambiguous",
                "candidates": agents,
                **last,
            }
        time.sleep(poll_s)
    last["error"] = "agent_window_not_found"
    last["ok"] = False
    return last


def trigger_new_agents_window(editor_hwnd_i: int) -> dict[str, Any]:
    """Priority: win32_menu → uia_menu → command_palette."""
    eh = int(editor_hwnd_i)
    # A: Win32 menu
    menu = trigger_new_agents_via_win32_menu(eh)
    if menu.get("ok"):
        return menu
    if menu.get("error") == "menu_item_disabled":
        return {**menu, "should_rescan": True}

    # B: UIA
    try:
        from agents_uia import invoke_file_new_agents_window

        uia = invoke_file_new_agents_window(eh)
        if uia.get("ok"):
            return uia
        menu["uia"] = uia
    except Exception as exc:
        menu["uia"] = {"ok": False, "error": str(exc)}

    # C: palette fallback
    for query in _PALETTE_QUERIES:
        pal = trigger_agents_window_via_palette(eh, query=query)
        if pal.get("ok"):
            return pal
        menu.setdefault("palette_attempts", []).append(pal)
    return {
        "ok": False,
        "error": "trigger_failed",
        "win32_menu": menu,
        "trigger_method": None,
    }


def ensure_agents_window_bound(
    cfg: dict[str, Any],
    state: dict[str, Any],
    *,
    timeout_s: float = 7.0,
    trigger_fn: Callable[[int], dict[str, Any]] | None = None,
    list_fn: Callable[[str], list[dict[str, Any]]] | None = None,
    classify_list_fn: Callable[[str], list[dict[str, Any]]] | None = None,
    bind_fn: Callable[..., dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    proc = str(cfg.get("cursor_process") or "Cursor.exe")
    bind_fn = bind_fn or bind_agent_from_hwnd
    classify_list_fn = classify_list_fn or list_cursor_windows_classified
    list_fn = list_fn or list_cursor_top_level
    trigger_fn = trigger_fn or trigger_new_agents_window

    refresh = refresh_cursor_bindings(state)
    before_snapshot = list_fn(proc)

    if agent_binding_ok(state).get("ok"):
        return {
            "ok": True,
            "source": "existing_binding",
            "agent_hwnd": agent_hwnd(state),
            "refresh": refresh,
            "before_cursor_windows": before_snapshot,
            "trigger_method": None,
        }

    eh = editor_hwnd(state)
    existing = find_existing_agent(
        process_name=proc,
        editor_hwnd_i=eh,
        classify_list_fn=classify_list_fn,
    )
    if existing.get("ok"):
        cand = existing["candidate"]
        hwnd = int(cand["hwnd"])
        show_window_noactivate(hwnd)
        snap = bind_fn(hwnd, process_name=proc)
        if not snap:
            return {"ok": False, "error": "agent_snapshot_failed", **existing}
        state["cursor_agent"] = snap
        state["agent_stale_reason"] = None
        state["agent_bound"] = True
        return {
            "ok": True,
            "source": "reuse_existing_agent",
            "agent_hwnd": hwnd,
            "trigger_method": None,
            "before_cursor_windows": before_snapshot,
            "after_cursor_windows": existing.get("classified"),
            "refresh": refresh,
            "classified_editor_count": existing.get("classified_editor_count"),
            "classified_agent_count": existing.get("classified_agent_count"),
            "unknown_count": existing.get("unknown_count"),
        }

    if not eh or not win32gui.IsWindow(eh):
        return {
            "ok": False,
            "error": "editor_hwnd_invalid",
            "refresh": refresh,
            "before_cursor_windows": before_snapshot,
            **{k: existing.get(k) for k in (
                "classified_editor_count",
                "classified_agent_count",
                "unknown_count",
            )},
        }

    prev_fg = get_foreground_hwnd()
    try:
        trig = trigger_fn(eh)
    except TypeError:
        trig = trigger_fn(eh)  # type: ignore[misc]
    except Exception as exc:
        trig = {"ok": False, "error": str(exc)}

    # Disabled menu → rescan (Agents likely exists)
    if trig.get("should_rescan") or trig.get("error") == "menu_item_disabled":
        existing2 = find_existing_agent(
            process_name=proc,
            editor_hwnd_i=eh,
            classify_list_fn=classify_list_fn,
        )
        if existing2.get("ok"):
            cand = existing2["candidate"]
            hwnd = int(cand["hwnd"])
            show_window_noactivate(hwnd)
            snap = bind_fn(hwnd, process_name=proc)
            if snap:
                state["cursor_agent"] = snap
                state["agent_stale_reason"] = None
                state["agent_bound"] = True
                restore_foreground_hwnd(prev_fg)
                return {
                    "ok": True,
                    "source": "reuse_after_menu_disabled",
                    "agent_hwnd": hwnd,
                    "trigger_method": trig.get("trigger_method"),
                    "trigger": trig,
                    "before_cursor_windows": before_snapshot,
                    "after_cursor_windows": existing2.get("classified"),
                    "refresh": refresh,
                    **{k: existing2.get(k) for k in (
                        "classified_editor_count",
                        "classified_agent_count",
                        "unknown_count",
                    )},
                }

    if not trig.get("ok"):
        restore_foreground_hwnd(prev_fg)
        # Final rescan
        existing3 = find_existing_agent(
            process_name=proc, editor_hwnd_i=eh, classify_list_fn=classify_list_fn
        )
        if existing3.get("ok"):
            cand = existing3["candidate"]
            hwnd = int(cand["hwnd"])
            snap = bind_fn(hwnd, process_name=proc)
            if snap:
                state["cursor_agent"] = snap
                state["agent_bound"] = True
                return {
                    "ok": True,
                    "source": "reuse_after_trigger_fail",
                    "agent_hwnd": hwnd,
                    "trigger": trig,
                    "before_cursor_windows": before_snapshot,
                    "refresh": refresh,
                }
        return {
            "ok": False,
            "error": "agent_window_not_found",
            "trigger": trig,
            "trigger_method": trig.get("trigger_method"),
            "before_cursor_windows": before_snapshot,
            "after_cursor_windows": existing3.get("classified"),
            "refresh": refresh,
            **{k: existing3.get(k) for k in (
                "classified_editor_count",
                "classified_agent_count",
                "unknown_count",
            )},
        }

    waited = wait_for_agent_classified(
        process_name=proc,
        editor_hwnd_i=eh,
        before=before_snapshot,
        timeout_s=timeout_s,
        classify_list_fn=classify_list_fn,
    )
    restore_foreground_hwnd(prev_fg)

    if not waited.get("ok"):
        return {
            "ok": False,
            "error": waited.get("error") or "agent_window_not_found",
            "trigger": trig,
            "trigger_method": trig.get("trigger_method"),
            "before_cursor_windows": before_snapshot,
            "after_cursor_windows": waited.get("after_cursor_windows"),
            "refresh": refresh,
            "classified_editor_count": waited.get("classified_editor_count"),
            "classified_agent_count": waited.get("classified_agent_count"),
            "unknown_count": waited.get("unknown_count"),
        }

    cand = waited["candidate"]
    hwnd = int(cand["hwnd"])
    snap = bind_fn(hwnd, process_name=proc)
    if not snap:
        return {
            "ok": False,
            "error": "agent_snapshot_failed",
            "trigger": trig,
            "trigger_method": trig.get("trigger_method"),
        }
    state["cursor_agent"] = snap
    state["agent_stale_reason"] = None
    state["agent_bound"] = True
    state["agent_bind_before"] = None
    return {
        "ok": True,
        "source": "auto_launch_classified",
        "agent_hwnd": hwnd,
        "trigger": trig,
        "trigger_method": trig.get("trigger_method"),
        "before_cursor_windows": before_snapshot,
        "after_cursor_windows": waited.get("after_cursor_windows"),
        "refresh": refresh,
        "classified_editor_count": waited.get("classified_editor_count"),
        "classified_agent_count": waited.get("classified_agent_count"),
        "unknown_count": waited.get("unknown_count"),
    }
