"""Native child embed: SetParent Agents Window under Obsidian (experimental spike).

Visual embed remains the default. This module only reparents agent_hwnd.
No DLL/Electron injection. Style WS_CHILD/WS_POPUP must be set explicitly
(SetParent does not do this automatically — MSDN).
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

import win32api
import win32con
import win32gui
import win32process

from pane_geometry import DomPaneRect, map_pane_dict_to_client

log = logging.getLogger("cursor_sidecar.native_child")

# Style bits we clear / set for native child transition (pure ints for tests too)
WS_CHILD = int(win32con.WS_CHILD)
WS_POPUP = int(win32con.WS_POPUP)
WS_CAPTION = int(win32con.WS_CAPTION)
WS_THICKFRAME = int(win32con.WS_THICKFRAME)
WS_MINIMIZEBOX = int(win32con.WS_MINIMIZEBOX)
WS_MAXIMIZEBOX = int(win32con.WS_MAXIMIZEBOX)
WS_SYSMENU = int(win32con.WS_SYSMENU)
WS_CLIPSIBLINGS = int(win32con.WS_CLIPSIBLINGS)
WS_CLIPCHILDREN = int(win32con.WS_CLIPCHILDREN)
WS_VISIBLE = int(win32con.WS_VISIBLE)

_BORDER_CLEAR = WS_POPUP | WS_CAPTION | WS_THICKFRAME | WS_MINIMIZEBOX | WS_MAXIMIZEBOX
_CHILD_SET = WS_CHILD | WS_CLIPSIBLINGS | WS_CLIPCHILDREN
# Progressive chrome: full clears caption+thickframe; caption_only keeps thickframe
_CHROME_FULL_CLEAR = WS_CAPTION | WS_THICKFRAME | WS_MINIMIZEBOX | WS_MAXIMIZEBOX
_CHROME_CAPTION_CLEAR = WS_CAPTION | WS_MINIMIZEBOX | WS_MAXIMIZEBOX


def style_for_native_child(original_style: int) -> int:
    """Pure: convert top-level style → WS_CHILD child style."""
    style = int(original_style) & ~_BORDER_CLEAR
    style |= _CHILD_SET
    # Keep system menu bit when present on original (requested for UX recovery)
    if int(original_style) & WS_SYSMENU:
        style |= WS_SYSMENU
    return style


def style_for_chrome_level(style: int, *, level: str = "full") -> int:
    """Post-SetParent chrome refine on agent_hwnd only. Preserves WS_CHILD / SYSMENU."""
    style = int(style)
    clear = _CHROME_FULL_CLEAR if level != "caption_only" else _CHROME_CAPTION_CLEAR
    style = style & ~clear
    style |= WS_SYSMENU
    return style


def apply_agent_chrome(
    hwnd: int,
    *,
    level: str = "full",
) -> dict[str, Any]:
    """Strip Win32 title chrome from agent only. Does not touch Editor."""
    hwnd = int(hwnd)
    before = int(win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE))
    after = style_for_chrome_level(before, level=level)
    _set_style(hwnd, after)
    try:
        # Drop native menu bar if any (Electron usually has none)
        win32gui.SetMenu(hwnd, None)
    except Exception:
        pass
    _frame_changed(hwnd)
    live = int(win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE))
    return {
        "ok": True,
        "chrome_level": level,
        "style_before": before,
        "style_after": live,
        "WS_CAPTION": style_has_caption(live),
        "WS_THICKFRAME": style_has_thickframe(live),
        "WS_SYSMENU": bool(live & WS_SYSMENU),
    }


def style_has_child(style: int) -> bool:
    return bool(int(style) & WS_CHILD)


def style_has_popup(style: int) -> bool:
    return bool(int(style) & WS_POPUP)


def snapshot_native_agent(hwnd: int) -> dict[str, Any]:
    """Full restore snapshot before SetParent (does not include Attach originals)."""
    hwnd = int(hwnd)
    style = int(win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE))
    exstyle = int(win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE))
    parent = int(win32gui.GetParent(hwnd) or 0)
    try:
        owner = int(win32gui.GetWindowLongPtr(hwnd, win32con.GWLP_HWNDPARENT) or 0)
    except AttributeError:
        owner = int(win32gui.GetWindowLong(hwnd, win32con.GWL_HWNDPARENT) or 0)
    from geometry import placement_dict

    placement = win32gui.GetWindowPlacement(hwnd)
    flags, show_cmd, min_pos, max_pos, normal = placement
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    dpi = read_dpi(hwnd)
    return {
        "hwnd": hwnd,
        "pid": int(pid),
        "style": style,
        "exstyle": exstyle,
        "original_parent": parent,
        "original_owner": owner,
        "rect": [int(left), int(top), int(right), int(bottom)],
        "original_window_placement": placement_dict(flags, show_cmd, min_pos, max_pos, normal),
        "visible": bool(win32gui.IsWindowVisible(hwnd)),
        "original_visibility": bool(win32gui.IsWindowVisible(hwnd)),
        "was_maximized": int(show_cmd) == int(win32con.SW_SHOWMAXIMIZED),
        "was_minimized": int(show_cmd) == int(win32con.SW_SHOWMINIMIZED)
        or bool(win32gui.IsIconic(hwnd)),
        "dpi_before": dpi,
    }


def get_client_size(hwnd: int) -> tuple[float, float]:
    _l, _t, r, b = win32gui.GetClientRect(int(hwnd))
    return float(r - _l), float(b - _t)


def compute_client_pane(
    *,
    obsidian_hwnd: int,
    pane: dict[str, Any] | DomPaneRect,
) -> dict[str, Any]:
    cw, ch = get_client_size(int(obsidian_hwnd))
    return map_pane_dict_to_client(pane, client_width_px=cw, client_height_px=ch)


def _set_style(hwnd: int, style: int) -> None:
    win32gui.SetWindowLong(int(hwnd), win32con.GWL_STYLE, int(style))


def _set_exstyle(hwnd: int, exstyle: int) -> None:
    win32gui.SetWindowLong(int(hwnd), win32con.GWL_EXSTYLE, int(exstyle))


def _frame_changed(hwnd: int) -> None:
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


def place_child_in_client(hwnd: int, client_rect: dict[str, int]) -> None:
    left = int(client_rect["left"])
    top = int(client_rect["top"])
    width = max(int(client_rect["width"]), 1)
    height = max(int(client_rect["height"]), 1)
    win32gui.SetWindowPos(
        int(hwnd),
        win32con.HWND_TOP,
        left,
        top,
        width,
        height,
        win32con.SWP_NOACTIVATE | win32con.SWP_SHOWWINDOW | win32con.SWP_FRAMECHANGED,
    )


def nudge_native_child(hwnd: int) -> dict[str, Any]:
    """Refresh hit-testing after Electron chrome clicks without moving the child.

    SetParent + Electron often loses mouse hit-test after one in-content click
    (e.g. Agents "Editor Window") until the next SetWindowPos — which is why
    dragging Obsidian temporarily "fixes" clicks. This is that SetWindowPos.
    """
    hwnd = int(hwnd or 0)
    if not hwnd or not win32gui.IsWindow(hwnd):
        return {"ok": False, "error": "agent_gone"}
    try:
        # Re-assert Z-order among Obsidian children, then frame-change.
        win32gui.SetWindowPos(
            hwnd,
            win32con.HWND_TOP,
            0,
            0,
            0,
            0,
            win32con.SWP_NOMOVE
            | win32con.SWP_NOSIZE
            | win32con.SWP_NOACTIVATE
            | win32con.SWP_SHOWWINDOW
            | win32con.SWP_FRAMECHANGED,
        )
        try:
            win32gui.RedrawWindow(
                hwnd,
                None,
                None,
                win32con.RDW_INVALIDATE
                | win32con.RDW_ERASE
                | win32con.RDW_FRAME
                | win32con.RDW_ALLCHILDREN,
            )
        except Exception:
            pass
        return {"ok": True, "applied": "nudge"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def read_dpi(hwnd: int) -> Optional[int]:
    try:
        return int(win32api.GetDpiForWindow(int(hwnd)))
    except Exception:
        return None


def style_has_caption(style: int) -> bool:
    return bool(int(style) & WS_CAPTION)


def style_has_thickframe(style: int) -> bool:
    return bool(int(style) & WS_THICKFRAME)


def probe_hwnd_win32(hwnd: int, *, expected_parent: int | None = None) -> dict[str, Any]:
    """Live Win32 read — never trust in-memory state alone."""
    hwnd = int(hwnd or 0)
    if not hwnd or not win32gui.IsWindow(hwnd):
        return {"ok": False, "error": "invalid_hwnd", "hwnd": hwnd or None}
    style = int(win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE))
    exstyle = int(win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE))
    parent = int(win32gui.GetParent(hwnd) or 0)
    exp = int(expected_parent) if expected_parent else None
    parent_ok = (exp is not None) and (parent == exp)
    child = style_has_child(style)
    popup = style_has_popup(style)
    verified = bool(parent_ok and child and not popup)
    return {
        "ok": True,
        "hwnd": hwnd,
        "parent_hwnd": parent,
        "expected_parent_hwnd": exp,
        "parent_ok": parent_ok,
        "style": style,
        "exstyle": exstyle,
        "WS_CHILD": child,
        "WS_POPUP": popup,
        "WS_CAPTION": style_has_caption(style),
        "WS_THICKFRAME": style_has_thickframe(style),
        "dpi": read_dpi(hwnd),
        "is_native_child_verified": verified,
    }


def enter_native_child(
    *,
    agent_hwnd: int,
    obsidian_hwnd: int,
    pane: dict[str, Any],
    persist_snapshot: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """
    Reparent agent under Obsidian as WS_CHILD.
    Persist restore snapshot BEFORE SetParent.
    Success ONLY if GetParent==obsidian AND WS_CHILD AND not WS_POPUP.
    On failure: rollback; never silently become visual overlay.
    """
    agent_hwnd = int(agent_hwnd)
    obsidian_hwnd = int(obsidian_hwnd)
    if not win32gui.IsWindow(agent_hwnd) or not win32gui.IsWindow(obsidian_hwnd):
        return {"ok": False, "error": "invalid_hwnd", "win32_error": None}

    snap = snapshot_native_agent(agent_hwnd)
    snap["obsidian_hwnd"] = obsidian_hwnd
    snap["obsidian_dpi"] = read_dpi(obsidian_hwnd)
    before = probe_hwnd_win32(agent_hwnd, expected_parent=obsidian_hwnd)

    mapped = compute_client_pane(obsidian_hwnd=obsidian_hwnd, pane=pane)
    if not mapped.get("ok"):
        return {"ok": False, "error": mapped.get("error") or "map_failed", "snapshot": snap}

    if persist_snapshot:
        persist_snapshot(snap)

    win32_error: int | None = None
    try:
        # Hide while flipping chrome / parent
        win32gui.ShowWindow(agent_hwnd, win32con.SW_HIDE)

        new_style = style_for_native_child(int(snap["style"]))
        _set_style(agent_hwnd, new_style)
        # Strip thick-frame related extended styles that keep DWM chrome
        try:
            ex = int(snap.get("exstyle") or 0)
            ex &= ~(
                int(getattr(win32con, "WS_EX_WINDOWEDGE", 0x100))
                | int(getattr(win32con, "WS_EX_CLIENTEDGE", 0x200))
                | int(getattr(win32con, "WS_EX_DLGMODALFRAME", 0x1))
                | int(getattr(win32con, "WS_EX_LAYERED", 0x80000))
            )
            _set_exstyle(agent_hwnd, ex)
        except Exception:
            pass
        _frame_changed(agent_hwnd)

        win32api.SetLastError(0)
        win32gui.SetParent(agent_hwnd, obsidian_hwnd)
        win32_error = int(win32api.GetLastError() or 0)

        parent_now = int(win32gui.GetParent(agent_hwnd) or 0)
        style_now = int(win32gui.GetWindowLong(agent_hwnd, win32con.GWL_STYLE))
        verified = (
            parent_now == obsidian_hwnd
            and style_has_child(style_now)
            and not style_has_popup(style_now)
        )
        if not verified:
            # Hard fail — rollback, do NOT leave a floating "almost" window as success
            try:
                exit_native_child(agent_hwnd=agent_hwnd, snapshot=snap)
            except Exception:
                pass
            after_fail = probe_hwnd_win32(agent_hwnd, expected_parent=obsidian_hwnd)
            return {
                "ok": False,
                "error": "native_child_enter_failed",
                "win32_error": win32_error,
                "parent_hwnd": parent_now,
                "expected_parent_hwnd": obsidian_hwnd,
                "WS_CHILD": style_has_child(style_now),
                "WS_POPUP": style_has_popup(style_now),
                "before": before,
                "after": after_fail,
                "snapshot": snap,
            }

        if mapped.get("visible"):
            place_child_in_client(agent_hwnd, mapped["client_rect"])
            win32gui.ShowWindow(agent_hwnd, win32con.SW_SHOWNOACTIVATE)
            _frame_changed(agent_hwnd)
            place_child_in_client(agent_hwnd, mapped["client_rect"])
        else:
            win32gui.ShowWindow(agent_hwnd, win32con.SW_HIDE)

        chrome_level = "full"
        if isinstance(pane, dict) and pane.get("chrome_level") in ("full", "caption_only"):
            chrome_level = str(pane.get("chrome_level"))
        chrome = apply_agent_chrome(agent_hwnd, level=chrome_level)
        # If full strip somehow left caption, soft-retry caption_only path is N/A;
        # caller may re-enter with chrome_level=caption_only if input breaks.
        snap["chrome_level"] = chrome_level

        after = probe_hwnd_win32(agent_hwnd, expected_parent=obsidian_hwnd)
        dpi_after = after.get("dpi")
        return {
            "ok": True,
            "applied": "native_child" if mapped.get("visible") else "native_child_hidden",
            "win32_error": win32_error,
            "parent_hwnd": after.get("parent_hwnd"),
            "expected_parent_hwnd": obsidian_hwnd,
            "parent_ok": True,
            "is_native_child_verified": True,
            "style": after.get("style"),
            "style_after": after.get("style") or chrome.get("style_after"),
            "WS_CHILD": True,
            "WS_POPUP": False,
            "WS_CAPTION": after.get("WS_CAPTION"),
            "WS_THICKFRAME": after.get("WS_THICKFRAME"),
            "chrome": chrome,
            "client_rect": mapped.get("client_rect"),
            "visible": mapped.get("visible"),
            "scale_x": mapped.get("scale_x"),
            "scale_y": mapped.get("scale_y"),
            "dom_rect": mapped.get("dom_rect"),
            "before": before,
            "after": after,
            "dpi": {
                "obsidian": snap.get("obsidian_dpi"),
                "agent_before": snap.get("dpi_before"),
                "agent_after": dpi_after,
                "changed": (
                    snap.get("dpi_before") is not None
                    and dpi_after is not None
                    and int(snap["dpi_before"]) != int(dpi_after)
                ),
            },
            "snapshot": snap,
        }
    except Exception as exc:
        log.exception("enter_native_child failed")
        try:
            exit_native_child(agent_hwnd=agent_hwnd, snapshot=snap)
        except Exception:
            pass
        err_code = None
        try:
            err_code = int(win32api.GetLastError() or 0)
        except Exception:
            err_code = win32_error
        return {
            "ok": False,
            "error": "native_child_enter_failed",
            "detail": str(exc),
            "win32_error": err_code,
            "snapshot": snap,
            "before": before,
        }


def update_native_child(
    *,
    agent_hwnd: int,
    obsidian_hwnd: int,
    pane: dict[str, Any],
) -> dict[str, Any]:
    mapped = compute_client_pane(obsidian_hwnd=int(obsidian_hwnd), pane=pane)
    if not mapped.get("ok"):
        return mapped
    agent_hwnd = int(agent_hwnd)
    if not win32gui.IsWindow(agent_hwnd):
        return {"ok": False, "error": "agent_gone"}
    try:
        if not mapped.get("visible"):
            win32gui.ShowWindow(agent_hwnd, win32con.SW_HIDE)
            mapped["applied"] = "hidden"
            return mapped
        win32gui.ShowWindow(agent_hwnd, win32con.SW_SHOWNOACTIVATE)
        place_child_in_client(agent_hwnd, mapped["client_rect"])
        mapped["applied"] = "placed"
        mapped["parent_hwnd"] = int(win32gui.GetParent(agent_hwnd) or 0)
        return mapped
    except Exception as exc:
        mapped["ok"] = False
        mapped["error"] = str(exc)
        mapped["applied"] = "error"
        return mapped


def exit_native_child(
    *,
    agent_hwnd: int,
    snapshot: dict[str, Any] | None,
) -> dict[str, Any]:
    """Best-effort restore to top-level. Continues even if intermediate steps fail."""
    agent_hwnd = int(agent_hwnd or 0)
    errors: list[str] = []
    if not agent_hwnd or not win32gui.IsWindow(agent_hwnd):
        return {"ok": False, "error": "agent_gone", "errors": errors}

    snap = snapshot or {}
    try:
        win32gui.ShowWindow(agent_hwnd, win32con.SW_HIDE)
    except Exception as exc:
        errors.append(f"hide:{exc}")

    orig_parent = int(snap.get("original_parent") or 0)
    try:
        # Detach from Obsidian → desktop/original parent
        win32gui.SetParent(agent_hwnd, orig_parent if orig_parent else 0)
    except Exception as exc:
        errors.append(f"setparent:{exc}")
        try:
            win32gui.SetParent(agent_hwnd, 0)
        except Exception as exc2:
            errors.append(f"setparent_null:{exc2}")

    if "style" in snap:
        try:
            _set_style(agent_hwnd, int(snap["style"]))
        except Exception as exc:
            errors.append(f"style:{exc}")
    if "exstyle" in snap:
        try:
            _set_exstyle(agent_hwnd, int(snap["exstyle"]))
        except Exception as exc:
            errors.append(f"exstyle:{exc}")

    try:
        _frame_changed(agent_hwnd)
    except Exception as exc:
        errors.append(f"frame:{exc}")

    # Restore owner if recorded
    if "original_owner" in snap:
        try:
            owner = int(snap.get("original_owner") or 0)
            try:
                win32gui.SetWindowLongPtr(agent_hwnd, win32con.GWLP_HWNDPARENT, owner)
            except AttributeError:
                win32gui.SetWindowLong(agent_hwnd, win32con.GWL_HWNDPARENT, owner)
        except Exception as exc:
            errors.append(f"owner:{exc}")

    # Placement then visibility
    try:
        from window import apply_window_placement

        apply_window_placement(agent_hwnd, snap)
    except Exception as exc:
        errors.append(f"placement:{exc}")
        rect = snap.get("rect")
        if rect and len(rect) == 4:
            try:
                win32gui.SetWindowPos(
                    agent_hwnd,
                    win32con.HWND_TOP,
                    int(rect[0]),
                    int(rect[1]),
                    max(int(rect[2]) - int(rect[0]), 1),
                    max(int(rect[3]) - int(rect[1]), 1),
                    win32con.SWP_NOACTIVATE | win32con.SWP_FRAMECHANGED,
                )
            except Exception as exc2:
                errors.append(f"rect:{exc2}")

    try:
        if snap.get("visible", True) or snap.get("original_visibility", True):
            win32gui.ShowWindow(agent_hwnd, win32con.SW_SHOWNOACTIVATE)
        else:
            win32gui.ShowWindow(agent_hwnd, win32con.SW_HIDE)
    except Exception as exc:
        errors.append(f"show:{exc}")

    parent_now = int(win32gui.GetParent(agent_hwnd) or 0)
    style_now = int(win32gui.GetWindowLong(agent_hwnd, win32con.GWL_STYLE))
    return {
        "ok": len(errors) == 0,
        "errors": errors,
        "parent_hwnd": parent_now,
        "has_ws_child": style_has_child(style_now),
        "has_ws_popup": style_has_popup(style_now),
        "style": style_now,
    }


def recover_native_child(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    """Crash-recovery path: detach child using persisted snapshot."""
    if not isinstance(snapshot, dict):
        return {"ok": False, "error": "no_snapshot"}
    hwnd = int(snapshot.get("hwnd") or 0)
    if not hwnd:
        return {"ok": False, "error": "no_hwnd"}
    if not win32gui.IsWindow(hwnd):
        return {"ok": False, "error": "agent_gone", "hwnd": hwnd}
    return exit_native_child(agent_hwnd=hwnd, snapshot=snapshot)
