"""Visual embed helpers for Cursor HWND over an Obsidian pane (Phase 1 — no SetParent)."""

from __future__ import annotations

import logging
from typing import Any

import win32con
import win32gui

from pane_geometry import DomPaneRect, map_pane_dict_to_screen
from window import (
    Rect,
    get_window_rect,
    hide_window,
    restore_if_maximized_or_minimized,
    show_window,
)

log = logging.getLogger("cursor_sidecar.embedded")

# Style bits we may strip for borderless experiment (keep WS_SYSMENU)
_BORDERLESS_REMOVE = (
    win32con.WS_CAPTION
    | win32con.WS_THICKFRAME
    | win32con.WS_MINIMIZEBOX
    | win32con.WS_MAXIMIZEBOX
)


def get_client_screen_metrics(hwnd: int) -> dict[str, float]:
    """Obsidian HWND → client origin (screen) + client size (physical px)."""
    _left, _top, right, bottom = win32gui.GetClientRect(int(hwnd))
    width = float(right - _left)
    height = float(bottom - _top)
    sx, sy = win32gui.ClientToScreen(int(hwnd), (0, 0))
    return {
        "client_screen_x": float(sx),
        "client_screen_y": float(sy),
        "client_width_px": width,
        "client_height_px": height,
    }


def snapshot_window_chrome(hwnd: int) -> dict[str, Any]:
    style = int(win32gui.GetWindowLong(int(hwnd), win32con.GWL_STYLE))
    exstyle = int(win32gui.GetWindowLong(int(hwnd), win32con.GWL_EXSTYLE))
    try:
        owner = int(win32gui.GetWindowLongPtr(int(hwnd), win32con.GWLP_HWNDPARENT) or 0)
    except AttributeError:
        owner = int(win32gui.GetWindowLong(int(hwnd), win32con.GWL_HWNDPARENT) or 0)
    rect = get_window_rect(int(hwnd))
    visible = bool(win32gui.IsWindowVisible(int(hwnd)))
    return {
        "style": style,
        "exstyle": exstyle,
        "owner": owner,
        "rect": list(rect.as_tuple()),
        "visible": visible,
    }


def restore_window_chrome(hwnd: int, snap: dict[str, Any] | None) -> None:
    if not snap or not hwnd:
        return
    try:
        if "style" in snap:
            win32gui.SetWindowLong(int(hwnd), win32con.GWL_STYLE, int(snap["style"]))
        if "exstyle" in snap:
            win32gui.SetWindowLong(int(hwnd), win32con.GWL_EXSTYLE, int(snap["exstyle"]))
        if "owner" in snap:
            try:
                win32gui.SetWindowLongPtr(
                    int(hwnd), win32con.GWLP_HWNDPARENT, int(snap["owner"] or 0)
                )
            except AttributeError:
                win32gui.SetWindowLong(
                    int(hwnd), win32con.GWL_HWNDPARENT, int(snap["owner"] or 0)
                )
        win32gui.SetWindowPos(
            int(hwnd),
            0,
            0,
            0,
            0,
            0,
            win32con.SWP_NOMOVE
            | win32con.SWP_NOSIZE
            | win32con.SWP_NOZORDER
            | win32con.SWP_NOACTIVATE
            | win32con.SWP_FRAMECHANGED,
        )
        if snap.get("rect") and len(snap["rect"]) == 4:
            from window import set_window_rect

            set_window_rect(int(hwnd), Rect.from_tuple(tuple(snap["rect"])), activate=False)
        if snap.get("visible"):
            show_window(int(hwnd))
        else:
            hide_window(int(hwnd))
    except Exception as exc:
        log.warning("restore_window_chrome failed: %s", exc)


def apply_borderless(hwnd: int) -> None:
    style = int(win32gui.GetWindowLong(int(hwnd), win32con.GWL_STYLE))
    style = style & ~_BORDERLESS_REMOVE
    win32gui.SetWindowLong(int(hwnd), win32con.GWL_STYLE, style)
    win32gui.SetWindowPos(
        int(hwnd),
        0,
        0,
        0,
        0,
        0,
        win32con.SWP_NOMOVE
        | win32con.SWP_NOSIZE
        | win32con.SWP_NOZORDER
        | win32con.SWP_NOACTIVATE
        | win32con.SWP_FRAMECHANGED,
    )


def set_owner_experimental(cursor_hwnd: int, obsidian_hwnd: int) -> None:
    """Optional owned-window z-order hint (NOT SetParent / NOT WS_CHILD)."""
    try:
        win32gui.SetWindowLongPtr(int(cursor_hwnd), win32con.GWLP_HWNDPARENT, int(obsidian_hwnd))
    except AttributeError:
        win32gui.SetWindowLong(int(cursor_hwnd), win32con.GWL_HWNDPARENT, int(obsidian_hwnd))
    raise_embedded_zorder(int(cursor_hwnd))


def raise_embedded_zorder(cursor_hwnd: int) -> None:
    """Place Cursor above peers without HWND_TOPMOST (not system-wide topmost)."""
    win32gui.SetWindowPos(
        int(cursor_hwnd),
        win32con.HWND_TOP,
        0,
        0,
        0,
        0,
        win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE,
    )


def place_cursor_on_screen_rect(cursor_hwnd: int, screen: dict[str, int]) -> None:
    left = int(screen["left"])
    top = int(screen["top"])
    width = max(int(screen["width"]), 1)
    height = max(int(screen["height"]), 1)
    restore_if_maximized_or_minimized(int(cursor_hwnd))
    # Intentionally NO SWP_NOZORDER — HWND_TOP (not TOPMOST) for visual embed stacking.
    win32gui.SetWindowPos(
        int(cursor_hwnd),
        win32con.HWND_TOP,
        left,
        top,
        width,
        height,
        win32con.SWP_SHOWWINDOW | win32con.SWP_NOACTIVATE,
    )


def compute_embedded_placement(
    *,
    obsidian_hwnd: int,
    pane: dict[str, Any] | DomPaneRect,
) -> dict[str, Any]:
    metrics = get_client_screen_metrics(int(obsidian_hwnd))
    mapped = map_pane_dict_to_screen(
        pane if isinstance(pane, dict) else pane.to_dict(),
        client_screen_x=metrics["client_screen_x"],
        client_screen_y=metrics["client_screen_y"],
        client_width_px=metrics["client_width_px"],
        client_height_px=metrics["client_height_px"],
    )
    mapped["client"] = metrics
    return mapped


def apply_embedded_pane(
    *,
    obsidian_hwnd: int,
    cursor_hwnd: int,
    pane: dict[str, Any],
    hide_when_collapsed: bool = True,
) -> dict[str, Any]:
    mapped = compute_embedded_placement(obsidian_hwnd=obsidian_hwnd, pane=pane)
    if not mapped.get("ok"):
        return mapped
    if not mapped.get("visible"):
        if hide_when_collapsed:
            try:
                hide_window(int(cursor_hwnd))
            except Exception as exc:
                log.info("hide for collapsed pane: %s", exc)
        mapped["applied"] = "hidden"
        return mapped
    try:
        win32gui.ShowWindow(int(cursor_hwnd), win32con.SW_SHOWNOACTIVATE)
        place_cursor_on_screen_rect(int(cursor_hwnd), mapped["screen_rect"])
        mapped["applied"] = "placed"
    except Exception as exc:
        mapped["ok"] = False
        mapped["error"] = str(exc)
        mapped["applied"] = "error"
    return mapped
