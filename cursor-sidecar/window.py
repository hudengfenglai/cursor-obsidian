"""Windows window discovery and layout helpers for Cursor Sidecar."""

from __future__ import annotations

import ctypes
import os
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional

import psutil
import win32api
import win32con
import win32gui
import win32process

from geometry import compute_split_rects as _compute_split_rects_pure
from geometry import placement_dict, placement_tuple

user32 = ctypes.windll.user32
_DPI_READY = False

# DPI awareness context: DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)


def enable_dpi_awareness() -> str:
    """Enable Per-Monitor DPI Awareness V2 before any coordinate Win32 calls."""
    global _DPI_READY
    if _DPI_READY:
        return "already"

    # Prefer V2 context (Win10 1703+)
    try:
        if hasattr(user32, "SetProcessDpiAwarenessContext"):
            ok = user32.SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
            if ok:
                _DPI_READY = True
                return "per_monitor_v2"
    except Exception:
        pass

    try:
        # PROCESS_PER_MONITOR_DPI_AWARE = 2
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        _DPI_READY = True
        return "per_monitor_v1"
    except Exception:
        pass

    try:
        user32.SetProcessDPIAware()
        _DPI_READY = True
        return "system"
    except Exception:
        return "none"


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

    @classmethod
    def from_tuple(cls, t: tuple[int, int, int, int] | list[int]) -> "Rect":
        return cls(int(t[0]), int(t[1]), int(t[2]), int(t[3]))


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


def is_same_window(
    hwnd: int,
    pid: int,
    process_name: str | None = None,
    process_create_time: float | None = None,
) -> bool:
    """Validate HWND still belongs to the expected process identity."""
    record = {
        "hwnd": hwnd,
        "pid": pid,
        "process_name": process_name,
        "process_create_time": process_create_time,
    }
    # Drop None identity fields so legacy path works when omitted
    if process_name is None:
        record.pop("process_name")
    if process_create_time is None:
        record.pop("process_create_time")
    return bool(validate_window_binding(record)["ok"])


def get_process_identity(pid: int) -> dict[str, Any]:
    try:
        proc = psutil.Process(int(pid))
        return {
            "process_name": proc.name(),
            "process_create_time": float(proc.create_time()),
        }
    except (psutil.Error, ValueError, TypeError):
        return {"process_name": "", "process_create_time": None}


def validate_window_binding(record: dict[str, Any] | None) -> dict[str, Any]:
    """Platform binding check: HWND + PID + process name + create_time."""
    from geometry import validate_binding_identity

    if not record:
        return validate_binding_identity(
            None, hwnd_exists=False, live_pid=None
        )

    try:
        hwnd_i = int(record.get("hwnd") or 0)
        pid_i = int(record.get("pid") or 0)
    except (TypeError, ValueError):
        return validate_binding_identity(
            {"hwnd": "x", "pid": "y"}, hwnd_exists=False, live_pid=None
        )

    hwnd_exists = bool(hwnd_i and win32gui.IsWindow(hwnd_i))
    live_pid: int | None = None
    live_name: str | None = None
    live_ctime: float | None = None

    if hwnd_exists:
        try:
            _, live_pid = win32process.GetWindowThreadProcessId(hwnd_i)
            live_pid = int(live_pid)
            ident = get_process_identity(live_pid)
            live_name = ident.get("process_name") or None
            live_ctime = ident.get("process_create_time")
        except Exception:
            hwnd_exists = False
            live_pid = None

    return validate_binding_identity(
        record,
        hwnd_exists=hwnd_exists,
        live_pid=live_pid,
        live_process_name=live_name,
        live_create_time=live_ctime,
    )


def window_info_from_hwnd(hwnd: int) -> Optional[WindowInfo]:
    if not hwnd or not win32gui.IsWindow(hwnd):
        return None
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        return WindowInfo(
            hwnd=int(hwnd),
            title=win32gui.GetWindowText(hwnd),
            pid=int(pid),
            process_name=_process_name(pid),
            rect=get_window_rect(hwnd),
        )
    except Exception:
        return None


def is_visible_top_level(hwnd: int, *, allow_minimized: bool = False) -> bool:
    if not win32gui.IsWindow(hwnd):
        return False
    if not win32gui.IsWindowVisible(hwnd) and not allow_minimized:
        return False
    if win32gui.GetParent(hwnd):
        return False
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
        info = window_info_from_hwnd(hwnd)
        if info:
            results.append(info)
        return True

    win32gui.EnumWindows(_callback, None)
    return results


def find_windows_by_process(
    process_name: str,
    *,
    allow_minimized: bool = False,
) -> list[WindowInfo]:
    target = _normalize(process_name)
    return [
        info
        for info in enum_top_level_windows(allow_minimized=allow_minimized)
        if _normalize(info.process_name) == target
    ]


def pick_best_window(
    windows: Iterable[WindowInfo],
    title_hint: str = "",
) -> Optional[WindowInfo]:
    windows = list(windows)
    if not windows:
        return None
    hint = title_hint.lower().strip()

    def _score(w: WindowInfo) -> tuple[int, int, int, int]:
        minimized = 1 if win32gui.IsIconic(w.hwnd) else 0
        title_boost = 1 if hint and hint in w.title.lower() else 0
        title_l = w.title.lower()
        penalty = 1 if "settings" in title_l else 0
        area = abs(w.rect.width * w.rect.height)
        return (-minimized, -penalty, title_boost, area)

    return max(windows, key=_score)


def find_obsidian_window(process_name: str, title_hint: str = "") -> Optional[WindowInfo]:
    return pick_best_window(find_windows_by_process(process_name), title_hint)


def find_cursor_window(process_name: str, title_hint: str = "") -> Optional[WindowInfo]:
    return pick_best_window(find_windows_by_process(process_name), title_hint)


def find_cursor_hwnd_any(process_name: str, title_hint: str = "") -> Optional[WindowInfo]:
    found: list[WindowInfo] = []
    target = _normalize(process_name)

    def _callback(hwnd: int, _: object) -> bool:
        if not win32gui.IsWindow(hwnd) or win32gui.GetParent(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd)
        if not title.strip():
            return True
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if _normalize(_process_name(pid)) != target:
                return True
            found.append(
                WindowInfo(
                    hwnd=hwnd,
                    title=title,
                    pid=pid,
                    process_name=_process_name(pid),
                    rect=get_window_rect(hwnd),
                )
            )
        except Exception:
            pass
        return True

    win32gui.EnumWindows(_callback, None)
    return pick_best_window(found, title_hint)


def resolve_bound_window(
    binding: dict[str, Any] | None,
    process_name: str,
    title_hint: str = "",
    *,
    allow_hidden: bool = False,
    allow_rediscovery: bool = True,
) -> Optional[WindowInfo]:
    """Prefer previously bound HWND+PID(+identity); rediscover only if allowed."""
    if binding:
        check = validate_window_binding(binding)
        if check["ok"]:
            info = window_info_from_hwnd(int(binding["hwnd"]))
            if info:
                return info
        if not allow_rediscovery:
            return None
    if not allow_rediscovery:
        return None
    if allow_hidden and _normalize(process_name) == "cursor":
        return find_cursor_hwnd_any(process_name, title_hint)
    return pick_best_window(find_windows_by_process(process_name), title_hint)


def get_work_area_for_hwnd(hwnd: int) -> Rect:
    """Monitor work area for a window — no monitor-index round trip."""
    hmon = win32api.MonitorFromWindow(hwnd, win32con.MONITOR_DEFAULTTONEAREST)
    info = win32api.GetMonitorInfo(hmon)
    work = info["Work"]
    return Rect(work[0], work[1], work[2], work[3])


def list_monitor_work_areas() -> list[Rect]:
    """All monitor work areas (enumeration order). Supports negative coordinates."""
    monitors: list[Rect] = []
    for hmonitor, _hdc, _rect in win32api.EnumDisplayMonitors(None, None):
        info = win32api.GetMonitorInfo(hmonitor)
        work = info["Work"]
        monitors.append(Rect(work[0], work[1], work[2], work[3]))
    return monitors


def get_monitor_work_area(monitor_index: int = 0) -> Rect:
    """Advanced: fixed monitor index (enumeration order). Prefer get_work_area_for_hwnd."""
    monitors = list_monitor_work_areas()
    if not monitors:
        w = win32api.GetSystemMetrics(win32con.SM_CXSCREEN)
        h = win32api.GetSystemMetrics(win32con.SM_CYSCREEN)
        return Rect(0, 0, w, h)
    if monitor_index < 0 or monitor_index >= len(monitors):
        raise ValueError(
            f"monitor index {monitor_index} out of range (found {len(monitors)} monitors)"
        )
    return monitors[monitor_index]


def monitor_snapshot_for_hwnd(hwnd: int) -> dict[str, Any]:
    hmon = win32api.MonitorFromWindow(hwnd, win32con.MONITOR_DEFAULTTONEAREST)
    info = win32api.GetMonitorInfo(hmon)
    work = info["Work"]
    monitor = info["Monitor"]
    return {
        "work": [int(work[0]), int(work[1]), int(work[2]), int(work[3])],
        "monitor": [int(monitor[0]), int(monitor[1]), int(monitor[2]), int(monitor[3])],
        "device": str(info.get("Device", "")),
    }


def snapshot_window(hwnd: int) -> dict[str, Any]:
    """Full original state for Attach → Detach restore."""
    placement = win32gui.GetWindowPlacement(hwnd)
    flags, show_cmd, min_pos, max_pos, normal = placement
    pid = int(win32process.GetWindowThreadProcessId(hwnd)[1])
    ident = get_process_identity(pid)
    return {
        "hwnd": int(hwnd),
        "pid": pid,
        "process_name": ident.get("process_name") or _process_name(pid),
        "process_create_time": ident.get("process_create_time"),
        "title": win32gui.GetWindowText(hwnd),
        "original_rect": list(get_window_rect(hwnd).as_tuple()),
        "original_window_placement": placement_dict(
            flags, show_cmd, min_pos, max_pos, normal
        ),
        "original_monitor": monitor_snapshot_for_hwnd(hwnd),
        "original_visibility": bool(win32gui.IsWindowVisible(hwnd)),
        "maximized": show_cmd == win32con.SW_SHOWMAXIMIZED,
        "minimized": show_cmd == win32con.SW_SHOWMINIMIZED or bool(win32gui.IsIconic(hwnd)),
    }


def apply_window_placement(hwnd: int, snap: dict[str, Any]) -> bool:
    """Restore via SetWindowPlacement; Rect fallback if needed."""
    if not hwnd or not win32gui.IsWindow(hwnd):
        return False

    placement = snap.get("original_window_placement")
    if placement:
        try:
            # SetWindowPlacement wants (flags, showCmd, ptMin, ptMax, rcNormal)
            tup = placement_tuple(placement)
            win32gui.SetWindowPlacement(hwnd, tup)
            # Re-apply visibility preference if explicitly hidden before attach
            if snap.get("original_visibility") is False:
                win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
            return True
        except Exception:
            pass

    # Fallback: SetWindowPos from original_rect / normal_position
    rect_src = snap.get("original_rect")
    if not rect_src and placement:
        rect_src = placement.get("normal_position")
    if not rect_src or len(rect_src) != 4:
        return False
    rect = Rect.from_tuple(rect_src)
    restore_if_maximized_or_minimized(hwnd)
    set_window_rect(hwnd, rect, activate=False)
    if snap.get("maximized"):
        win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)
    if snap.get("original_visibility") is False:
        win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
    return True


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
    left_t, right_t = _compute_split_rects_pure(
        work.as_tuple(), obsidian_ratio, cursor_ratio, gap
    )
    return Rect.from_tuple(left_t), Rect.from_tuple(right_t)


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


def launch_process(
    exe_path: str,
    args: Optional[list[str]] = None,
    cwd: Optional[str] = None,
) -> None:
    cmd = [exe_path, *(args or [])]
    subprocess.Popen(
        cmd,
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


def arrange_bound_windows(
    obsidian: WindowInfo,
    cursor: WindowInfo,
    *,
    obsidian_ratio: float,
    cursor_ratio: float,
    gap: int,
    monitor: Optional[int] = None,
) -> tuple[Rect, Rect]:
    if monitor is None or monitor == "auto":
        work = get_work_area_for_hwnd(obsidian.hwnd)
    else:
        work = get_monitor_work_area(int(monitor))
    left, right = compute_split_rects(work, obsidian_ratio, cursor_ratio, gap)
    set_window_rect(obsidian.hwnd, left, activate=False)
    set_window_rect(cursor.hwnd, right, activate=False)
    return left, right


def show_window(hwnd: int) -> None:
    win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
    try:
        user32.SetForegroundWindow(hwnd)
    except Exception:
        pass


def hide_window(hwnd: int) -> None:
    win32gui.ShowWindow(hwnd, win32con.SW_HIDE)


def focus_window(hwnd: int) -> None:
    show_window(hwnd)


def toggle_cursor_visibility(
    process_name: str,
    title_hint: str = "",
    binding: dict[str, Any] | None = None,
) -> str:
    win_info = resolve_bound_window(
        binding, process_name, title_hint, allow_hidden=True
    )
    if not win_info:
        raise RuntimeError(f"Cursor window not found ({process_name})")
    if win32gui.IsWindowVisible(win_info.hwnd):
        hide_window(win_info.hwnd)
        return "hidden"
    show_window(win_info.hwnd)
    return "shown"


# Back-compat alias used by older call sites
def arrange_sidecar(**kwargs: Any) -> tuple[WindowInfo, WindowInfo, Rect, Rect]:
    obs = find_obsidian_window(kwargs["obsidian_process"], kwargs.get("obsidian_title_hint", ""))
    cur = find_cursor_window(kwargs["cursor_process"], kwargs.get("cursor_title_hint", ""))
    if not obs:
        raise RuntimeError("Obsidian window not found")
    if not cur:
        raise RuntimeError("Cursor window not found")
    left, right = arrange_bound_windows(
        obs,
        cur,
        obsidian_ratio=kwargs["obsidian_ratio"],
        cursor_ratio=kwargs["cursor_ratio"],
        gap=kwargs["gap"],
        monitor=kwargs.get("monitor"),
    )
    obs = window_info_from_hwnd(obs.hwnd) or obs
    cur = window_info_from_hwnd(cur.hwnd) or cur
    return obs, cur, left, right
