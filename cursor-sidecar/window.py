"""Windows window discovery and layout helpers for Cursor Sidecar."""

from __future__ import annotations

import ctypes
import os
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

import psutil
import win32api
import win32con
import win32gui
import win32process


user32 = ctypes.windll.user32


@dataclass
class Rect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    def as_tuple(self) -> tuple[int, int, int, int]:
        return self.left, self.top, self.right, self.bottom


@dataclass
class WindowInfo:
    hwnd: int
    title: str
    pid: int
    process_name: str
    rect: Rect


def _normalize(name: str) -> str:
    return name.lower().removesuffix(".exe")


def _process_name(pid: int) -> str:
    try:
        return psutil.Process(pid).name()
    except (psutil.Error, ValueError):
        return ""


def get_window_rect(hwnd: int) -> Rect:
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    return Rect(left, top, right, bottom)


def is_visible_top_level(hwnd: int, *, allow_minimized: bool = False) -> bool:
    if not win32gui.IsWindow(hwnd):
        return False
    if not win32gui.IsWindowVisible(hwnd) and not allow_minimized:
        return False
    if win32gui.GetParent(hwnd):
        return False
    # Skip tool windows / owned popups that are not main frames.
    ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
    if ex_style & win32con.WS_EX_TOOLWINDOW:
        return False
    title = win32gui.GetWindowText(hwnd)
    if not title.strip():
        return False
    return True


def enum_top_level_windows(*, allow_minimized: bool = False) -> list[WindowInfo]:
    results: list[WindowInfo] = []

    def _callback(hwnd: int, _: object) -> bool:
        if not is_visible_top_level(hwnd, allow_minimized=allow_minimized):
            return True
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            name = _process_name(pid)
            title = win32gui.GetWindowText(hwnd)
            results.append(
                WindowInfo(
                    hwnd=hwnd,
                    title=title,
                    pid=pid,
                    process_name=name,
                    rect=get_window_rect(hwnd),
                )
            )
        except Exception:
            pass
        return True

    win32gui.EnumWindows(_callback, None)
    return results


def find_windows_by_process(
    process_name: str,
    title_hint: str = "",
    *,
    allow_minimized: bool = False,
) -> list[WindowInfo]:
    target = _normalize(process_name)
    hint = title_hint.lower().strip()
    matched: list[WindowInfo] = []
    for info in enum_top_level_windows(allow_minimized=allow_minimized):
        if _normalize(info.process_name) != target:
            continue
        if hint and hint not in info.title.lower():
            # Soft filter: keep if no better match later.
            continue
        matched.append(info)

    if matched:
        return matched

    # Fallback without title hint if hint filtered everything out.
    if hint:
        return [
            info
            for info in enum_top_level_windows(allow_minimized=allow_minimized)
            if _normalize(info.process_name) == target
        ]
    return []


def pick_best_window(windows: Iterable[WindowInfo]) -> Optional[WindowInfo]:
    windows = list(windows)
    if not windows:
        return None

    def _score(w: WindowInfo) -> tuple[int, int]:
        minimized = 1 if win32gui.IsIconic(w.hwnd) else 0
        # Prefer non-minimized, then largest area (main editor over tiny dialogs).
        area = abs(w.rect.width * w.rect.height)
        return (-minimized, area)

    return max(windows, key=_score)


def find_obsidian_window(process_name: str, title_hint: str) -> Optional[WindowInfo]:
    return pick_best_window(find_windows_by_process(process_name, title_hint))


def find_cursor_window(process_name: str, title_hint: str) -> Optional[WindowInfo]:
    return pick_best_window(find_windows_by_process(process_name, title_hint))

def get_monitor_work_area(monitor_index: int = 0) -> Rect:
    monitors: list[Rect] = []

    def _enum(hmonitor: int, _hdc: int, _lprect: object, _data: object) -> int:
        info = win32api.GetMonitorInfo(hmonitor)
        work = info["Work"]
        monitors.append(Rect(work[0], work[1], work[2], work[3]))
        return 1

    win32api.EnumDisplayMonitors(None, None, _enum, None)
    if not monitors:
        # Primary screen fallback.
        w = win32api.GetSystemMetrics(win32con.SM_CXSCREEN)
        h = win32api.GetSystemMetrics(win32con.SM_CYSCREEN)
        return Rect(0, 0, w, h)

    if monitor_index < 0 or monitor_index >= len(monitors):
        raise ValueError(
            f"monitor index {monitor_index} out of range (found {len(monitors)} monitors)"
        )
    return monitors[monitor_index]


def monitor_index_for_window(hwnd: int) -> int:
    hmonitor = win32api.MonitorFromWindow(hwnd, win32con.MONITOR_DEFAULTTONEAREST)
    monitors: list[int] = []

    def _enum(hmon: int, _hdc: int, _lprect: object, _data: object) -> int:
        monitors.append(hmon)
        return 1

    win32api.EnumDisplayMonitors(None, None, _enum, None)
    try:
        return monitors.index(hmonitor)
    except ValueError:
        return 0


def restore_if_maximized_or_minimized(hwnd: int) -> None:
    placement = win32gui.GetWindowPlacement(hwnd)
    show_cmd = placement[1]
    if show_cmd in (win32con.SW_SHOWMAXIMIZED, win32con.SW_SHOWMINIMIZED):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    elif win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)


def set_window_rect(hwnd: int, rect: Rect, activate: bool = False) -> None:
    restore_if_maximized_or_minimized(hwnd)
    flags = (
        win32con.SWP_NOZORDER
        | win32con.SWP_NOOWNERZORDER
        | win32con.SWP_SHOWWINDOW
    )
    if not activate:
        flags |= win32con.SWP_NOACTIVATE

    win32gui.SetWindowPos(
        hwnd,
        win32con.HWND_TOP,
        rect.left,
        rect.top,
        rect.width,
        rect.height,
        flags,
    )


def compute_split_rects(
    work: Rect,
    obsidian_ratio: float,
    cursor_ratio: float,
    gap: int,
) -> tuple[Rect, Rect]:
    total = obsidian_ratio + cursor_ratio
    if total <= 0:
        raise ValueError("obsidian_ratio + cursor_ratio must be > 0")

    usable = max(work.width - max(gap, 0), 1)
    left_w = int(round(usable * (obsidian_ratio / total)))
    right_w = usable - left_w

    left = Rect(work.left, work.top, work.left + left_w, work.bottom)
    right = Rect(
        work.left + left_w + max(gap, 0),
        work.top,
        work.left + left_w + max(gap, 0) + right_w,
        work.bottom,
    )
    return left, right


def expand_path(path: str) -> str:
    return os.path.expandvars(os.path.expanduser(path))


def resolve_cursor_exe(candidates: Iterable[str]) -> Optional[str]:
    for raw in candidates:
        path = expand_path(raw)
        if os.path.isfile(path):
            return path
    return None


def is_process_running(process_name: str) -> bool:
    target = _normalize(process_name)
    for proc in psutil.process_iter(["name"]):
        name = proc.info.get("name") or ""
        if _normalize(name) == target:
            return True
    return False


def launch_process(exe_path: str, cwd: Optional[str] = None) -> None:
    subprocess.Popen(
        [exe_path],
        cwd=cwd or os.path.dirname(exe_path),
        shell=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def wait_for_window(
    finder: Callable[[], Optional[WindowInfo]],
    timeout_s: float = 30.0,
    poll_s: float = 0.4,
) -> Optional[WindowInfo]:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        info = finder()
        if info:
            return info
        time.sleep(poll_s)
    return None


def arrange_sidecar(
    *,
    obsidian_process: str,
    cursor_process: str,
    obsidian_title_hint: str,
    cursor_title_hint: str,
    obsidian_ratio: float,
    cursor_ratio: float,
    gap: int,
    monitor: Optional[int] = None,
) -> tuple[WindowInfo, WindowInfo, Rect, Rect]:
    obsidian = find_obsidian_window(obsidian_process, obsidian_title_hint)
    cursor = find_cursor_window(cursor_process, cursor_title_hint)
    if not obsidian:
        raise RuntimeError(f"Obsidian window not found ({obsidian_process})")
    if not cursor:
        raise RuntimeError(f"Cursor window not found ({cursor_process})")

    if monitor is None:
        monitor = monitor_index_for_window(obsidian.hwnd)
    work = get_monitor_work_area(monitor)
    left, right = compute_split_rects(work, obsidian_ratio, cursor_ratio, gap)

    set_window_rect(obsidian.hwnd, left, activate=False)
    set_window_rect(cursor.hwnd, right, activate=False)

    # Refresh rects after move.
    obsidian = WindowInfo(
        hwnd=obsidian.hwnd,
        title=obsidian.title,
        pid=obsidian.pid,
        process_name=obsidian.process_name,
        rect=get_window_rect(obsidian.hwnd),
    )
    cursor = WindowInfo(
        hwnd=cursor.hwnd,
        title=cursor.title,
        pid=cursor.pid,
        process_name=cursor.process_name,
        rect=get_window_rect(cursor.hwnd),
    )
    return obsidian, cursor, left, right


def show_window(hwnd: int) -> None:
    win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
    try:
        user32.SetForegroundWindow(hwnd)
    except Exception:
        pass


def hide_window(hwnd: int) -> None:
    win32gui.ShowWindow(hwnd, win32con.SW_HIDE)


def toggle_cursor_visibility(process_name: str, title_hint: str) -> str:
    """Toggle Cursor main window. Returns 'shown' or 'hidden'."""
    # Include hidden windows for toggle-back.
    found: list[WindowInfo] = []
    target = _normalize(process_name)
    hint = title_hint.lower().strip()

    def _callback(hwnd: int, _: object) -> bool:
        if not win32gui.IsWindow(hwnd):
            return True
        if win32gui.GetParent(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd)
        if not title.strip():
            return True
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            name = _process_name(pid)
            if _normalize(name) != target:
                return True
            if hint and hint not in title.lower():
                return True
            found.append(
                WindowInfo(
                    hwnd=hwnd,
                    title=title,
                    pid=pid,
                    process_name=name,
                    rect=get_window_rect(hwnd),
                )
            )
        except Exception:
            pass
        return True

    win32gui.EnumWindows(_callback, None)
    win_info = pick_best_window(found)
    if not win_info:
        raise RuntimeError(f"Cursor window not found ({process_name})")

    if win32gui.IsWindowVisible(win_info.hwnd):
        hide_window(win_info.hwnd)
        return "hidden"
    show_window(win_info.hwnd)
    return "shown"
