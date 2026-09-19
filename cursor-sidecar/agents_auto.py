"""Auto-discover / auto-bind Cursor Agents Window (v0.7.2).

Order:
  1) refresh + existing valid binding
  2) enumerate+classify → unique AGENT (restore if hidden)
  3) trigger: win32_menu → alt_file_menu → uia_menu → command_palette
     (ok only after new Agent HWND confirmed)
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
VK_MENU = 0x12  # Alt
VK_RETURN = 0x0D
VK_ESCAPE = 0x1B
VK_DOWN = 0x28
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

# Prefer exact "New Agents Window" — "Open/Agents Window" often switches layout
# in the SAME Editor HWND (no new window). Never query bare "Agents Window".
_PALETTE_QUERIES = (
    "New Agents Window",
    "File: New Agents Window",
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
    """Send palette keystrokes. ok=True only means keys were sent — caller must confirm HWND.

    Do NOT press Down before Enter: the top match is already highlighted; Down would
    select the second row (often "Open Agents Window" / layout switch, same HWND).
    """
    eh = int(editor_hwnd_i or 0)
    if not eh or not win32gui.IsWindow(eh):
        return {"ok": False, "error": "editor_hwnd_invalid"}
    q = (query or _PALETTE_QUERIES[0]).strip()
    prev = get_foreground_hwnd()
    try:
        focus_window(eh)
        time.sleep(0.25)
        dismiss_palette()
        time.sleep(0.1)
        open_command_palette()
        time.sleep(0.5)  # let palette UI mount
        chord([VK_CONTROL, ord("A")], pause=0.05)
        # Clipboard paste is more reliable than per-char Unicode SendInput in Electron
        pasted = False
        try:
            import win32clipboard

            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardText(q)
            finally:
                win32clipboard.CloseClipboard()
            chord([VK_CONTROL, ord("V")], pause=0.08)
            pasted = True
        except Exception:
            type_text(q)
        time.sleep(0.65)  # let fuzzy filter settle on exact top hit
        tap_vk(VK_RETURN, pause=0.2)
        time.sleep(0.45)
        return {
            "ok": True,
            "keys_sent": True,
            "trigger_method": "command_palette",
            "query": q,
            "pasted": pasted,
            "prev_foreground": prev,
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "trigger_method": "command_palette",
            "query": q,
        }


def trigger_agents_window_via_alt_file_menu(editor_hwnd_i: int) -> dict[str, Any]:
    """Keyboard: Alt, F → type 'New Agents Window' → Enter (Electron menu bar)."""
    eh = int(editor_hwnd_i or 0)
    if not eh or not win32gui.IsWindow(eh):
        return {"ok": False, "error": "editor_hwnd_invalid"}
    try:
        focus_window(eh)
        time.sleep(0.2)
        dismiss_palette()
        # Sequential Alt then F (more reliable than Alt+F chord on Electron)
        tap_vk(VK_MENU, pause=0.15)
        tap_vk(ord("F"), pause=0.45)
        type_text("New Agents Window", pause=0.025)
        time.sleep(0.4)
        tap_vk(VK_RETURN, pause=0.2)
        time.sleep(0.4)
        return {
            "ok": True,
            "keys_sent": True,
            "trigger_method": "alt_file_menu",
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc), "trigger_method": "alt_file_menu"}


def _confirm_agent_appeared(
    *,
    before: list[dict[str, Any]],
    process_name: str,
    editor_hwnd_i: int,
    timeout_s: float = 3.0,
    classify_list_fn: Callable[[str], list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    return wait_for_agent_classified(
        process_name=process_name,
        editor_hwnd_i=editor_hwnd_i,
        before=before,
        timeout_s=timeout_s,
        poll_s=0.15,
        classify_list_fn=classify_list_fn,
    )


def trigger_new_agents_window(
    editor_hwnd_i: int,
    *,
    process_name: str = "Cursor.exe",
    classify_list_fn: Callable[[str], list[dict[str, Any]]] | None = None,
    list_fn: Callable[[str], list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """
    Try triggers in order; only return ok=True when a new Agent HWND is confirmed.
    Priority: win32_menu → alt_file_menu → uia_menu → command_palette
    """
    list_fn = list_fn or list_cursor_top_level
    classify_list_fn = classify_list_fn or list_cursor_windows_classified
    eh = int(editor_hwnd_i)
    before = list_fn(process_name)
    attempts: list[dict[str, Any]] = []

    def _try(label: str, result: dict[str, Any], confirm_s: float = 3.2) -> dict[str, Any] | None:
        attempts.append({"step": label, **{k: result.get(k) for k in (
            "ok", "error", "trigger_method", "query", "keys_sent"
        )}})
        if result.get("error") == "menu_item_disabled":
            return {**result, "should_rescan": True, "attempts": attempts}
        if not result.get("ok") and not result.get("keys_sent"):
            return None
        confirmed = _confirm_agent_appeared(
            before=before,
            process_name=process_name,
            editor_hwnd_i=eh,
            timeout_s=confirm_s,
            classify_list_fn=classify_list_fn,
        )
        if confirmed.get("ok"):
            return {
                **result,
                "ok": True,
                "confirmed": True,
                "confirm": confirmed,
                "attempts": attempts,
                "trigger_method": result.get("trigger_method") or label,
            }
        attempts[-1]["confirm_error"] = confirmed.get("error") or "no_new_window"
        return None

    # A: Win32 GetMenu
    hit = _try("win32_menu", trigger_new_agents_via_win32_menu(eh))
    if hit:
        return hit

    # B: Alt → File → New Agents Window (Electron)
    hit = _try("alt_file_menu", trigger_agents_window_via_alt_file_menu(eh))
    if hit:
        return hit

    # C: UIA
    try:
        from agents_uia import invoke_file_new_agents_window

        uia = invoke_file_new_agents_window(eh)
    except Exception as exc:
        uia = {"ok": False, "error": str(exc), "trigger_method": "uia_menu"}
    hit = _try("uia_menu", uia)
    if hit:
        return hit

    # D: Command palette — exact New Agents Window only (avoid layout-switch commands)
    for query in _PALETTE_QUERIES:
        pal = trigger_agents_window_via_palette(eh, query=query)
        hit = _try(f"command_palette:{query}", pal, confirm_s=4.0)
        if hit:
            return hit

    return {
        "ok": False,
        "error": "trigger_failed_no_new_window",
        "trigger_method": None,
        "attempts": attempts,
        "hint": "Palette/menu keys sent but no new Cursor HWND appeared",
        "before_count": len(before),
    }


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
    """
    Success:
      A) unique ROLE_AGENT, or
      B) exactly one new non-EDITOR HWND (Electron Agents often stays UNKNOWN briefly)
    Aux HWNDs that are not the sole newcomer are ignored.
    """
    classify_list_fn = classify_list_fn or list_cursor_windows_classified
    before_ids = {int(w.get("hwnd") or 0) for w in before}
    eh = int(editor_hwnd_i or 0)
    deadline = time.time() + float(timeout_s)
    last: dict[str, Any] = {}
    while time.time() < deadline:
        classified = classify_list_fn(process_name)
        agents = agent_candidates(classified, exclude_hwnd=eh)
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
                "match": "classified_agent",
                **last,
            }
        if len(agents) > 1:
            return {
                "ok": False,
                "error": "agent_window_ambiguous",
                "candidates": agents,
                **last,
            }

        # Soft path: sole newcomer that is not EDITOR (and not the bound editor hwnd)
        newcomers = [
            w
            for w in classified
            if int(w.get("hwnd") or 0) not in before_ids
            and int(w.get("hwnd") or 0) != eh
            and str(w.get("role") or "") != "EDITOR"
        ]
        if len(newcomers) == 1:
            cand = newcomers[0]
            return {
                "ok": True,
                "candidate": cand,
                "new_hwnd": True,
                "match": "sole_newcomer_non_editor",
                **last,
            }
        time.sleep(poll_s)
    last["error"] = "agent_window_not_found"
    last["ok"] = False
    return last


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
        try:
            trig = trigger_fn(
                eh,
                process_name=proc,
                classify_list_fn=classify_list_fn,
                list_fn=list_fn,
            )
        except TypeError:
            trig = trigger_fn(eh)
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

    # Trigger already confirmed a new Agent HWND during method loop
    if trig.get("confirmed") and isinstance(trig.get("confirm"), dict) and trig["confirm"].get("ok"):
        cand = trig["confirm"]["candidate"]
        hwnd = int(cand["hwnd"])
        snap = bind_fn(hwnd, process_name=proc)
        restore_foreground_hwnd(prev_fg)
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
            "source": "auto_launch_confirmed",
            "agent_hwnd": hwnd,
            "trigger": trig,
            "trigger_method": trig.get("trigger_method"),
            "before_cursor_windows": before_snapshot,
            "after_cursor_windows": trig["confirm"].get("after_cursor_windows"),
            "refresh": refresh,
            "match": trig["confirm"].get("match"),
            "classified_editor_count": trig["confirm"].get("classified_editor_count"),
            "classified_agent_count": trig["confirm"].get("classified_agent_count"),
            "unknown_count": trig["confirm"].get("unknown_count"),
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
            "error": trig.get("error") or "agent_window_not_found",
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
