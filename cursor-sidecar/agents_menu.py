"""Win32 native menu discovery to trigger File → New Agents Window."""

from __future__ import annotations

import logging
import re
from typing import Any

import win32api
import win32con
import win32gui

log = logging.getLogger("cursor_sidecar.agents_menu")

_TARGET_LABELS = (
    "new agents window",
    "new agent window",
)


def normalize_menu_label(text: str) -> str:
    t = (text or "").replace("&", "")
    t = re.sub(r"\t.*$", "", t)
    t = re.sub(r"\s+", " ", t).strip().lower()
    return t


def _iter_menu_items(hmenu: int, *, depth: int = 0, max_depth: int = 4):
    if not hmenu or depth > max_depth:
        return
    try:
        count = int(win32gui.GetMenuItemCount(hmenu))
    except Exception:
        return
    for i in range(max(count, 0)):
        try:
            text = win32gui.GetMenuString(hmenu, i, win32con.MF_BYPOSITION) or ""
        except Exception:
            text = ""
        try:
            state = int(win32gui.GetMenuState(hmenu, i, win32con.MF_BYPOSITION))
        except Exception:
            state = 0
        try:
            item_id = int(win32gui.GetMenuItemID(hmenu, i))
        except Exception:
            item_id = -1
        try:
            sub = win32gui.GetSubMenu(hmenu, i)
        except Exception:
            sub = 0
        yield {
            "index": i,
            "text": text,
            "norm": normalize_menu_label(text),
            "state": state,
            "id": item_id,
            "submenu": int(sub) if sub else 0,
            "depth": depth,
        }
        if sub:
            yield from _iter_menu_items(int(sub), depth=depth + 1, max_depth=max_depth)


def find_new_agents_menu_item(hwnd: int) -> dict[str, Any] | None:
    hwnd = int(hwnd)
    try:
        menu = win32gui.GetMenu(hwnd)
    except Exception:
        menu = 0
    if not menu:
        return None
    for item in _iter_menu_items(int(menu)):
        norm = item.get("norm") or ""
        if any(t in norm for t in _TARGET_LABELS):
            return item
    return None


def invoke_win32_menu_command(hwnd: int, command_id: int) -> dict[str, Any]:
    hwnd = int(hwnd)
    cmd = int(command_id) & 0xFFFF
    if cmd <= 0:
        return {"ok": False, "error": "invalid_menu_command_id"}
    try:
        win32api.PostMessage(hwnd, win32con.WM_COMMAND, cmd, 0)
        return {"ok": True, "trigger_method": "win32_menu", "command_id": cmd}
    except Exception as exc:
        return {"ok": False, "error": "menu_post_failed", "detail": str(exc)}


def trigger_new_agents_via_win32_menu(hwnd: int) -> dict[str, Any]:
    """
    Discover File→New Agents Window dynamically (no hardcoded command ID).
    If item disabled: ok=False, error=menu_item_disabled (caller should re-scan windows).
    """
    item = find_new_agents_menu_item(hwnd)
    if not item:
        return {"ok": False, "error": "win32_menu_unavailable"}
    state = int(item.get("state") or 0)
    disabled = bool(state & win32con.MF_DISABLED) or bool(state & win32con.MF_GRAYED)
    if disabled:
        return {
            "ok": False,
            "error": "menu_item_disabled",
            "item": {"text": item.get("text"), "id": item.get("id")},
            "hint": "Usually means an Agents Window already exists — rescan",
        }
    cmd_id = int(item.get("id") or 0)
    if cmd_id <= 0:
        return {"ok": False, "error": "menu_item_no_id", "item": item}
    result = invoke_win32_menu_command(hwnd, cmd_id)
    result["item"] = {"text": item.get("text"), "id": cmd_id}
    return result
