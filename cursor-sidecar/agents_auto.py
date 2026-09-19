"""Auto-launch / auto-bind Cursor Agents Window (v0.7.1).

Does not use titles to identify Agents Window — HWND snapshot + diff only.
Triggers creation via Editor command palette keystrokes (no SDK / Agent CLI).
"""

from __future__ import annotations

import logging
import time
from ctypes import POINTER, Structure, Union, byref, c_ulong, c_ushort, c_void_p, sizeof, windll
from ctypes.wintypes import DWORD, WORD
from typing import Any, Callable

import win32con
import win32gui

from cursor_windows import (
    agent_binding_ok,
    bind_agent_from_hwnd,
    candidates_excluding_editor,
    diff_new_hwnds,
    editor_hwnd,
    list_cursor_top_level,
)
from window import focus_window, get_foreground_hwnd, restore_foreground_hwnd

log = logging.getLogger("cursor_sidecar.agents_auto")

# Virtual-key codes
VK_CONTROL = 0x11
VK_SHIFT = 0x10
VK_RETURN = 0x0D
VK_ESCAPE = 0x1B

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

# Command palette queries tried in order (Cursor 3 naming varies)
_PALETTE_QUERIES = (
    "New Agents Window",
    "Open Agents Window",
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
    sent = windll.user32.SendInput(n, byref(arr), sizeof(INPUT))
    if sent != n:
        log.warning("SendInput partial: %s/%s", sent, n)


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
            wVk=0,
            wScan=ord(ch),
            dwFlags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP,
            time=0,
            dwExtraInfo=None,
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
    """Focus Editor and run one command-palette query that opens Agents Window."""
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
        time.sleep(0.4)
        return {
            "ok": True,
            "method": "command_palette",
            "query": q,
            "prev_foreground": prev,
        }
    except Exception as exc:
        log.exception("palette trigger failed")
        return {"ok": False, "error": str(exc), "prev_foreground": prev, "query": q}


def wait_for_new_agent_hwnd(
    *,
    before: list[dict[str, Any]],
    process_name: str,
    editor_hwnd_i: int,
    timeout_s: float = 10.0,
    poll_s: float = 0.25,
    list_fn: Callable[[str], list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Poll until exactly one new non-editor Cursor top-level appears."""
    list_fn = list_fn or list_cursor_top_level
    deadline = time.time() + float(timeout_s)
    last_after: list[dict[str, Any]] = []
    while time.time() < deadline:
        after = list_fn(process_name)
        last_after = after
        newcomers = [
            w
            for w in diff_new_hwnds(before, after)
            if int(w.get("hwnd") or 0) != int(editor_hwnd_i or 0)
        ]
        if len(newcomers) == 1:
            return {"ok": True, "candidate": newcomers[0], "after": after}
        if len(newcomers) > 1:
            return {
                "ok": False,
                "error": "agent_window_ambiguous",
                "candidates": newcomers,
                "after": after,
            }
        time.sleep(poll_s)
    return {
        "ok": False,
        "error": "agent_window_not_found",
        "after": last_after,
        "after_count": len(last_after),
    }


def ensure_agents_window_bound(
    cfg: dict[str, Any],
    state: dict[str, Any],
    *,
    timeout_s: float = 10.0,
    trigger_fn: Callable[[int], dict[str, Any]] | None = None,
    list_fn: Callable[[str], list[dict[str, Any]]] | None = None,
    bind_fn: Callable[..., dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """
    Ensure state has a live agent binding.

    Order:
      1) existing valid binding
      2) sole existing non-editor Cursor HWND → auto-bind
      3) snapshot → trigger New Agents Window → wait → bind newcomer
    Never falls back to visual embed.
    """
    proc = str(cfg.get("cursor_process") or "Cursor.exe")
    list_fn = list_fn or list_cursor_top_level
    bind_fn = bind_fn or bind_agent_from_hwnd
    trigger_fn = trigger_fn or trigger_agents_window_via_palette

    ok = agent_binding_ok(state)
    if ok.get("ok"):
        return {
            "ok": True,
            "source": "existing_binding",
            "agent_hwnd": int((state.get("cursor_agent") or {}).get("hwnd") or 0),
        }

    eh = editor_hwnd(state)
    windows = list_fn(proc)
    cands = candidates_excluding_editor(windows, eh)
    if len(cands) == 1:
        snap = bind_fn(int(cands[0]["hwnd"]), process_name=proc)
        if not snap:
            return {"ok": False, "error": "agent_snapshot_failed"}
        state["cursor_agent"] = snap
        state["agent_stale_reason"] = None
        return {
            "ok": True,
            "source": "auto_select_existing",
            "agent_hwnd": int(snap["hwnd"]),
            "agent": {"hwnd": snap["hwnd"], "pid": snap["pid"], "title": snap.get("title")},
        }
    if len(cands) > 1:
        return {
            "ok": False,
            "error": "agent_window_ambiguous",
            "candidates": cands,
            "hint": "Close extra Cursor windows or use debug Bind Agents",
        }

    if not eh or not win32gui.IsWindow(eh):
        return {"ok": False, "error": "editor_hwnd_invalid"}

    prev_fg = get_foreground_hwnd()
    last_trig: dict[str, Any] = {}
    last_wait: dict[str, Any] = {}
    per_query_timeout = max(3.0, float(timeout_s) / max(len(_PALETTE_QUERIES), 1))

    for query in _PALETTE_QUERIES:
        before = list_fn(proc)
        # Prefer injectable trigger_fn(hwnd); fall back to palette with query=
        try:
            trig = trigger_fn(eh, query=query)  # type: ignore[call-arg]
        except TypeError:
            trig = trigger_fn(eh)
        last_trig = trig if isinstance(trig, dict) else {"ok": bool(trig)}
        if not last_trig.get("ok"):
            continue
        waited = wait_for_new_agent_hwnd(
            before=before,
            process_name=proc,
            editor_hwnd_i=eh,
            timeout_s=per_query_timeout,
            list_fn=list_fn,
        )
        last_wait = waited
        if waited.get("ok"):
            cand = waited["candidate"]
            snap = bind_fn(int(cand["hwnd"]), process_name=proc)
            if not snap:
                restore_foreground_hwnd(prev_fg)
                return {"ok": False, "error": "agent_snapshot_failed", "trigger": last_trig}
            state["cursor_agent"] = snap
            state["agent_stale_reason"] = None
            state["agent_bind_before"] = None
            restore_foreground_hwnd(prev_fg)
            return {
                "ok": True,
                "source": "auto_launch_diff",
                "agent_hwnd": int(snap["hwnd"]),
                "trigger": last_trig,
                "agent": {
                    "hwnd": snap["hwnd"],
                    "pid": snap["pid"],
                    "title": snap.get("title"),
                },
            }

    restore_foreground_hwnd(prev_fg)
    # Agents may already have been open — recheck sole non-editor
    windows2 = list_fn(proc)
    cands2 = candidates_excluding_editor(windows2, eh)
    if len(cands2) == 1:
        snap = bind_fn(int(cands2[0]["hwnd"]), process_name=proc)
        if snap:
            state["cursor_agent"] = snap
            state["agent_stale_reason"] = None
            return {
                "ok": True,
                "source": "auto_select_after_trigger",
                "agent_hwnd": int(snap["hwnd"]),
                "trigger": last_trig,
                "agent": {
                    "hwnd": snap["hwnd"],
                    "pid": snap["pid"],
                    "title": snap.get("title"),
                },
            }
    return {
        "ok": False,
        "error": last_wait.get("error") or "agent_window_not_found",
        "trigger": last_trig,
        "wait": last_wait,
    }
