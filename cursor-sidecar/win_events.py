"""WinEventHook-based Live Sidecar follow (v0.3).

Only moves/resizes the bound Cursor window to stick to Obsidian's right edge.
Never snaps Obsidian back to a preset during user drag/resize.
"""

from __future__ import annotations

import ctypes
import logging
import threading
import time
from ctypes import wintypes
from typing import Any, Callable, Optional

import win32con
import win32gui

from geometry import LatestOnlyDebouncer, compute_follow_cursor_rect
from window import (
    Rect,
    get_work_area_for_hwnd,
    get_window_rect,
    hide_window,
    set_window_rect,
    show_window,
    validate_window_binding,
)

log = logging.getLogger("cursor_sidecar.live")

# WinEvent constants
EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_SYSTEM_CAPTURESTART = 0x0008
EVENT_SYSTEM_CAPTUREEND = 0x0009
EVENT_SYSTEM_MOVESIZESTART = 0x000A
EVENT_SYSTEM_MOVESIZEEND = 0x000B
EVENT_SYSTEM_MINIMIZESTART = 0x0016
EVENT_SYSTEM_MINIMIZEEND = 0x0017
EVENT_OBJECT_DESTROY = 0x8001
EVENT_OBJECT_LOCATIONCHANGE = 0x800B

# Native-child hit-test keepalive (Electron SetParent quirk)
_NATIVE_NUDGE_INTERVAL_S = 0.35
_NATIVE_NUDGE_DEBOUNCE_S = 0.05

WINEVENT_OUTOFCONTEXT = 0x0000
WINEVENT_SKIPOWNPROCESS = 0x0002

WM_QUIT = 0x0012

user32 = ctypes.windll.user32

WinEventProcType = ctypes.WINFUNCTYPE(
    None,
    wintypes.HANDLE,  # hWinEventHook
    wintypes.DWORD,  # event
    wintypes.HWND,  # hwnd
    wintypes.LONG,  # idObject
    wintypes.LONG,  # idChild
    wintypes.DWORD,  # dwEventThread
    wintypes.DWORD,  # dwmsEventTime
)


class LiveFollowService:
    """Long-lived WinEventHook follower tied to exact Obsidian/Cursor bindings."""

    def __init__(
        self,
        *,
        lifecycle_lock: threading.RLock,
        get_attached_state: Callable[[], dict[str, Any]],
        mark_stale: Callable[[str], None],
        gap: int = 0,
        debounce_ms: int = 40,
        debug: bool = False,
        enabled: bool = True,
    ) -> None:
        self._lock = lifecycle_lock
        self._get_attached_state = get_attached_state
        self._mark_stale = mark_stale
        self.gap = int(gap)
        self.debounce_ms = int(debounce_ms)
        self.debug = bool(debug)
        self.enabled = bool(enabled)

        self._thread: Optional[threading.Thread] = None
        self._thread_id: Optional[int] = None
        self._stop = threading.Event()
        self._hook = None
        self._hooks: list = []
        self._callback_ref = None  # keep alive

        self.obsidian_hwnd = 0
        self.cursor_hwnd = 0
        self.cursor_width = 0
        self.obsidian_user_moving = False
        self._self_applying = False
        self._suppress_until = 0.0
        self._cursor_hidden_for_minimize = False

        # "sidecar" = right-edge follow (editor); "pane" = visual embed (agent HWND)
        self.follow_mode: str = "sidecar"
        self.last_pane_dom: dict[str, Any] | None = None
        self.last_embed_apply: dict[str, Any] | None = None
        self.agent_hwnd: int = 0
        self.embed_backend: str = "visual"

        self.last_event = ""
        self.last_follow_at = 0.0
        self.hook_installed = False
        self.running = False

        self._debouncer = LatestOnlyDebouncer(
            max(self.debounce_ms, 1) / 1000.0,
            lambda: self._follow_cursor(reason="debounced"),
        )
        self._nudge_thread: Optional[threading.Thread] = None
        self._nudge_timer: Optional[threading.Timer] = None
        self._nudge_lock = threading.Lock()

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "running": self.running and not self._stop.is_set(),
            "hook_installed": self.hook_installed,
            "moving": self.obsidian_user_moving,
            "last_event": self.last_event,
            "last_follow_at": self.last_follow_at or None,
            "obsidian_hwnd": self.obsidian_hwnd or None,
            "cursor_hwnd": self.cursor_hwnd or None,
            "agent_hwnd": self.agent_hwnd or None,
            "follow_mode": self.follow_mode,
            "embed_backend": self.embed_backend,
            "has_pane_dom": bool(self.last_pane_dom),
        }

    def set_follow_mode(self, mode: str) -> None:
        m = str(mode or "sidecar").strip().lower()
        self.follow_mode = "pane" if m == "pane" else "sidecar"

    def set_last_pane_dom(self, pane: dict[str, Any] | None) -> None:
        self.last_pane_dom = dict(pane) if isinstance(pane, dict) else None

    def set_agent_hwnd(self, hwnd: int) -> None:
        self.agent_hwnd = int(hwnd or 0)

    def set_embed_backend(self, backend: str) -> None:
        b = str(backend or "visual").strip().lower()
        self.embed_backend = "native_child" if b in ("native_child", "native") else "visual"

    def start(self, obsidian_hwnd: int, cursor_hwnd: int, cursor_width: int) -> None:
        if not self.enabled:
            return
        self.stop()
        self.obsidian_hwnd = int(obsidian_hwnd)
        self.cursor_hwnd = int(cursor_hwnd)
        self.cursor_width = max(int(cursor_width), 1)
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._thread_main,
            name="sidecar-winevent",
            daemon=True,
        )
        self._thread.start()
        self._nudge_thread = threading.Thread(
            target=self._native_nudge_loop,
            name="sidecar-native-nudge",
            daemon=True,
        )
        self._nudge_thread.start()
        # brief wait for hook install
        for _ in range(50):
            if self.hook_installed:
                break
            time.sleep(0.02)
        log.info("live follow started obs=%s cur=%s", self.obsidian_hwnd, self.cursor_hwnd)

    def stop(self) -> None:
        """Stop hook thread. Safe to call from the hook callback (no self-join)."""
        self._debouncer.cancel()
        self._cancel_nudge_timer()
        self._stop.set()
        tid = self._thread_id
        on_hook_thread = tid is not None and threading.get_ident() == tid
        if tid:
            try:
                user32.PostThreadMessageW(tid, WM_QUIT, 0, 0)
            except Exception:
                pass
        if on_hook_thread:
            # Message loop + UnhookWinEvent finish in _thread_main finally.
            log.info("live follow stop requested (from hook thread)")
            return
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=2.0)
        nt = self._nudge_thread
        if nt and nt.is_alive() and threading.get_ident() != nt.ident:
            nt.join(timeout=1.0)
        self._thread = None
        self._nudge_thread = None
        self._thread_id = None
        self.running = False
        self.hook_installed = False
        self.obsidian_user_moving = False
        log.info("live follow stopped")

    def update_cursor_width(self, width: int) -> None:
        self.cursor_width = max(int(width), 1)

    def follow_now(self) -> None:
        self._debouncer.flush()

    def _thread_main(self) -> None:
        self._thread_id = threading.get_ident()
        self.running = True
        try:
            self._callback_ref = WinEventProcType(self._on_event)
            flags = WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS
            ranges = (
                (EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_CAPTUREEND),
                (EVENT_SYSTEM_MOVESIZESTART, EVENT_SYSTEM_MINIMIZEEND),
                (EVENT_OBJECT_DESTROY, EVENT_OBJECT_LOCATIONCHANGE),
            )
            self._hooks = []
            for emin, emax in ranges:
                h = user32.SetWinEventHook(
                    emin,
                    emax,
                    0,
                    self._callback_ref,
                    0,
                    0,
                    flags,
                )
                if h:
                    self._hooks.append(h)
            if not self._hooks:
                log.error("SetWinEventHook failed")
                self.running = False
                return
            self.hook_installed = True
            log.info("WinEventHook installed (%d hooks)", len(self._hooks))

            msg = wintypes.MSG()
            while not self._stop.is_set():
                r = user32.GetMessageW(ctypes.byref(msg), 0, 0, 0)
                if r == 0 or r == -1:
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            for h in self._hooks:
                try:
                    user32.UnhookWinEvent(h)
                except Exception:
                    pass
            self._hooks = []
            log.info("WinEventHook removed")
            self.hook_installed = False
            self.running = False

    def _on_event(
        self,
        _hWinEventHook,
        event,
        hwnd,
        idObject,
        idChild,
        _dwEventThread,
        _dwmsEventTime,
    ) -> None:
        if self._stop.is_set():
            return
        # Only top-level object interest
        if idObject != 0 or idChild != 0:
            if idObject not in (0, win32con.OBJID_WINDOW):
                return
        hwnd_i = int(hwnd or 0)
        if not hwnd_i:
            return
        self.dispatch_win_event(int(event), hwnd_i)

    def _is_agent_tree(self, hwnd_i: int) -> bool:
        """True if hwnd is agent_hwnd or a descendant (Electron chrome lives in children)."""
        ah = int(self.agent_hwnd or 0)
        if not ah or not hwnd_i:
            return False
        h = int(hwnd_i)
        for _ in range(40):
            if h == ah:
                return True
            if h == int(self.obsidian_hwnd or 0):
                return False
            try:
                h = int(win32gui.GetParent(h) or 0)
            except Exception:
                return False
            if not h:
                return False
        return False

    def _mouse_buttons_up(self) -> bool:
        try:
            import win32api

            # High bit set => currently down
            return (win32api.GetAsyncKeyState(0x01) & 0x8000) == 0 and (
                win32api.GetAsyncKeyState(0x02) & 0x8000
            ) == 0
        except Exception:
            return True

    def _cancel_nudge_timer(self) -> None:
        with self._nudge_lock:
            t = self._nudge_timer
            self._nudge_timer = None
        if t is not None:
            try:
                t.cancel()
            except Exception:
                pass

    def schedule_native_nudge(self, reason: str = "") -> None:
        """Debounced hit-test refresh for native_child Agents pane."""
        if self.embed_backend != "native_child" or self.follow_mode != "pane":
            return
        if not self.agent_hwnd:
            return

        def _fire() -> None:
            with self._nudge_lock:
                self._nudge_timer = None
            self._nudge_native_child(reason=reason or "debounced")

        with self._nudge_lock:
            if self._nudge_timer is not None:
                try:
                    self._nudge_timer.cancel()
                except Exception:
                    pass
            self._nudge_timer = threading.Timer(_NATIVE_NUDGE_DEBOUNCE_S, _fire)
            self._nudge_timer.daemon = True
            self._nudge_timer.start()

    def _nudge_native_child(self, reason: str = "") -> None:
        if self._stop.is_set():
            return
        if self.embed_backend != "native_child" or self.follow_mode != "pane":
            return
        if self.obsidian_user_moving or self._self_applying:
            return
        if not self._mouse_buttons_up():
            return
        ah = int(self.agent_hwnd or 0)
        if not ah:
            return
        try:
            from native_embed import nudge_native_child

            self._self_applying = True
            self._suppress_until = time.time() + 0.15
            result = nudge_native_child(ah)
            if self.debug:
                log.debug("native-nudge[%s] -> %s", reason, result)
        except Exception as exc:
            log.error("native nudge error: %s", exc)
        finally:
            self._self_applying = False

    def _native_nudge_loop(self) -> None:
        """Periodic safety net: same SetWindowPos refresh that Obsidian drag triggers."""
        while not self._stop.wait(_NATIVE_NUDGE_INTERVAL_S):
            if not self.enabled or not self.running:
                continue
            self._nudge_native_child(reason="keepalive")

    def dispatch_win_event(self, event: int, hwnd_i: int) -> str:
        """Route a WinEvent. Public for unit tests; does not require a live OS hook."""
        # Cursor destroy must be handled even though we ignore other Cursor events
        if hwnd_i == self.cursor_hwnd and event == EVENT_OBJECT_DESTROY:
            self.last_event = "CURSOR_DESTROY"
            self._handle_cursor_gone()
            return "CURSOR_DESTROY"

        # Ignore our own SetWindowPos echo (defense in depth; we filter to Obsidian)
        if hwnd_i == self.cursor_hwnd and time.time() < self._suppress_until:
            return "ignore_suppress"

        # Agent / Electron chrome: restore hit-test after click/focus churn
        if self.embed_backend == "native_child" and self._is_agent_tree(hwnd_i):
            if event in (EVENT_SYSTEM_CAPTUREEND, EVENT_SYSTEM_FOREGROUND):
                self.last_event = (
                    "AGENT_CAPTUREEND" if event == EVENT_SYSTEM_CAPTUREEND else "AGENT_FOREGROUND"
                )
                self.schedule_native_nudge(reason=self.last_event)
                return self.last_event
            if event == EVENT_OBJECT_DESTROY and hwnd_i == int(self.agent_hwnd or 0):
                self.last_event = "AGENT_DESTROY"
                self.agent_hwnd = 0
                return "AGENT_DESTROY"
            return "ignore_agent"

        if hwnd_i != self.obsidian_hwnd:
            return "ignore_hwnd"

        if self.debug:
            log.debug("event=%s hwnd=%s", event, hwnd_i)

        if event == EVENT_SYSTEM_MOVESIZESTART:
            self.last_event = "MOVESIZESTART"
            self.obsidian_user_moving = True
            return "MOVESIZESTART"

        if event == EVENT_SYSTEM_MOVESIZEEND:
            self.last_event = "MOVESIZEEND"
            self.obsidian_user_moving = False
            self._debouncer.flush()
            return "MOVESIZEEND"

        if event == EVENT_SYSTEM_MINIMIZESTART:
            self.last_event = "MINIMIZESTART"
            self._on_obsidian_minimize()
            return "MINIMIZESTART"

        if event == EVENT_SYSTEM_MINIMIZEEND:
            self.last_event = "MINIMIZEEND"
            self._on_obsidian_restore()
            return "MINIMIZEEND"

        if event == EVENT_OBJECT_DESTROY:
            self.last_event = "DESTROY"
            self._handle_obsidian_gone()
            return "DESTROY"

        if event == EVENT_OBJECT_LOCATIONCHANGE:
            self.last_event = "LOCATIONCHANGE"
            if self._self_applying:
                return "ignore_self_applying"
            self._debouncer.trigger()
            return "LOCATIONCHANGE"

        return "ignore_event"

    def _bindings_ok(self) -> tuple[bool, str]:
        state = self._get_attached_state()
        if not state.get("attached"):
            return False, "detached"
        obs = dict(state.get("obsidian") or {})
        cur = dict(state.get("cursor") or {})
        if not obs or not cur:
            return False, "incomplete_bindings"
        obs["hwnd"] = self.obsidian_hwnd
        cur["hwnd"] = self.cursor_hwnd
        if not validate_window_binding(obs).get("ok"):
            return False, "obsidian_gone"
        if not validate_window_binding(cur).get("ok"):
            return False, "cursor_gone"
        return True, "ok"

    def _handle_obsidian_gone(self) -> None:
        log.info("bound Obsidian destroyed — stopping live follow")
        # Best-effort: if agent was native-child, detach before parent dies
        if self.embed_backend == "native_child" and self.agent_hwnd:
            try:
                from native_embed import exit_native_child

                # Snapshot may live in process state; try NULL parent restore
                exit_native_child(agent_hwnd=self.agent_hwnd, snapshot=None)
            except Exception as exc:
                log.warning("native child emergency detach: %s", exc)
        try:
            self._mark_stale("obsidian_gone")
        except Exception as exc:
            log.error("mark_stale failed: %s", exc)
        self.stop()

    def _handle_cursor_gone(self) -> None:
        log.info("bound Cursor gone — stopping live follow")
        try:
            self._mark_stale("cursor_gone")
        except Exception as exc:
            log.error("mark_stale failed: %s", exc)
        self.stop()

    def _on_obsidian_minimize(self) -> None:
        with self._lock:
            ok, reason = self._bindings_ok()
            if not ok:
                if reason == "cursor_gone":
                    self._handle_cursor_gone()
                elif reason == "obsidian_gone":
                    self._handle_obsidian_gone()
                return
            try:
                hide_window(self.cursor_hwnd)
                self._cursor_hidden_for_minimize = True
            except Exception as exc:
                log.error("minimize sync failed: %s", exc)

    def _on_obsidian_restore(self) -> None:
        with self._lock:
            ok, reason = self._bindings_ok()
            if not ok:
                if reason == "cursor_gone":
                    self._handle_cursor_gone()
                elif reason == "obsidian_gone":
                    self._handle_obsidian_gone()
                return
            try:
                # Show without forcing activation steal beyond ShowWindow
                win32gui.ShowWindow(self.cursor_hwnd, win32con.SW_SHOWNOACTIVATE)
                self._cursor_hidden_for_minimize = False
            except Exception as exc:
                log.error("restore show failed: %s", exc)
        self._follow_cursor(reason="restore")

    def _follow_cursor(self, reason: str = "") -> None:
        if self._stop.is_set() or not self.enabled:
            return
        if self.follow_mode == "pane":
            self._follow_embedded_pane(reason=reason)
            return
        with self._lock:
            ok, why = self._bindings_ok()
            if not ok:
                if why == "cursor_gone":
                    self._handle_cursor_gone()
                elif why == "obsidian_gone":
                    self._handle_obsidian_gone()
                return
            try:
                if win32gui.IsIconic(self.obsidian_hwnd):
                    return
                obs_rect = get_window_rect(self.obsidian_hwnd).as_tuple()
                work = get_work_area_for_hwnd(self.obsidian_hwnd).as_tuple()
                width = self.cursor_width
                if width <= 0:
                    # fallback: keep existing cursor width
                    width = max(get_window_rect(self.cursor_hwnd).width, 1)
                    self.cursor_width = width
                target = compute_follow_cursor_rect(
                    obs_rect,
                    cursor_width=width,
                    gap=self.gap,
                    work=work,
                )
                self._self_applying = True
                self._suppress_until = time.time() + 0.2
                set_window_rect(
                    self.cursor_hwnd,
                    Rect.from_tuple(target),
                    activate=False,
                )
                self.last_follow_at = time.time()
                if self.debug:
                    log.debug("follow[%s] -> %s", reason, target)
            except Exception as exc:
                log.error("follow error: %s", exc)
            finally:
                self._self_applying = False

    def _follow_embedded_pane(self, reason: str = "") -> None:
        """Recompute pane screen rect; move agent_hwnd only (never editor)."""
        with self._lock:
            ok, why = self._bindings_ok()
            if not ok:
                if why == "cursor_gone":
                    self._handle_cursor_gone()
                elif why == "obsidian_gone":
                    self._handle_obsidian_gone()
                return
            pane = self.last_pane_dom
            if not isinstance(pane, dict):
                return
            target = int(self.agent_hwnd or 0)
            if not target:
                # No agent bound — do not fall back to editor HWND
                return
            try:
                if win32gui.IsIconic(self.obsidian_hwnd):
                    return
                if not win32gui.IsWindow(target):
                    log.info("agent hwnd gone during embed follow")
                    self.agent_hwnd = 0
                    return
                self._self_applying = True
                self._suppress_until = time.time() + 0.2
                if self.embed_backend == "native_child":
                    from native_embed import update_native_child

                    mapped = update_native_child(
                        agent_hwnd=target,
                        obsidian_hwnd=self.obsidian_hwnd,
                        pane=pane,
                    )
                else:
                    from embedded_window import apply_embedded_pane

                    mapped = apply_embedded_pane(
                        obsidian_hwnd=self.obsidian_hwnd,
                        cursor_hwnd=target,
                        pane=pane,
                    )
                self.last_embed_apply = mapped
                self.last_follow_at = time.time()
                if self.debug:
                    log.debug(
                        "embed-follow[%s] backend=%s agent=%s -> %s",
                        reason,
                        self.embed_backend,
                        target,
                        mapped.get("client_rect") or mapped.get("screen_rect"),
                    )
            except Exception as exc:
                log.error("embed follow error: %s", exc)
            finally:
                self._self_applying = False
