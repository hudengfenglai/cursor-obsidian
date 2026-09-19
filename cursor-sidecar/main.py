#!/usr/bin/env python3
"""Cursor Sidecar v0.3 — Attach/Detach + Live Sidecar (WinEventHook follow)."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from geometry import (
    atomic_write_text,
    can_restore_bound_window,
    cursor_width_from_work,
    evaluate_attachment,
    ratios_for_preset,
    recover_state_dict,
    resolve_preset,
)
from window import (
    apply_window_placement,
    arrange_bound_windows,
    enable_dpi_awareness,
    find_cursor_window,
    find_obsidian_window,
    focus_window,
    get_work_area_for_hwnd,
    get_window_rect,
    is_process_running,
    launch_process,
    resolve_bound_window,
    resolve_cursor_exe,
    snapshot_window,
    toggle_cursor_visibility,
    validate_window_binding,
    wait_for_window,
    window_info_from_hwnd,
)
from win_events import LiveFollowService

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config.json"
PID_FILE = ROOT / ".sidecar.pid"
DAEMON_PID_FILE = ROOT / ".sidecar.daemon.pid"
TOKEN_FILE = ROOT / ".sidecar.daemon.token"
STATE_FILE = ROOT / ".sidecar.state.json"

VERSION = "0.3.0"

LIFECYCLE_LOCK = threading.RLock()
_FOLLOW: LiveFollowService | None = None
_DAEMON_MODE = False
_STOP_DAEMON = threading.Event()

LOCALHOST_BIND_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def normalize_daemon_bind_host(host: str | None) -> str:
    """Daemon must bind localhost only (never 0.0.0.0)."""
    raw = (host or "127.0.0.1").strip()
    key = raw.lower().strip("[]")
    if key in ("0.0.0.0", "::", "*"):
        raise ValueError("daemon must bind localhost only (got all-interfaces bind)")
    if key not in LOCALHOST_BIND_HOSTS and not key.startswith("127."):
        raise ValueError(f"daemon host must be localhost, got {host!r}")
    return raw


def daemon_endpoint_args(host: str, port: int) -> list[str]:
    """CLI fragments so daemon-start and plugin probe the same endpoint."""
    return ["--host", normalize_daemon_bind_host(host), "--port", str(int(port))]


def load_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"config not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        cfg = json.load(f)
    # Preset drives ratios (v0.3). Legacy explicit ratios still accepted if no preset.
    preset = str(cfg.get("preset") or "normal")
    try:
        preset = resolve_preset(preset)
        o, c = ratios_for_preset(preset)
    except ValueError:
        preset = "normal"
        o, c = ratios_for_preset(preset)
    # Allow explicit ratio override only when preset key absent in file... always use preset.
    cfg["preset"] = preset
    cfg["obsidian_ratio"] = o
    cfg["cursor_ratio"] = c
    cfg["gap"] = int(cfg.get("gap", 0))
    cfg["monitor"] = cfg.get("monitor", None)
    # Deprecated polling knobs (kept for compat; Live Follow ignores them)
    cfg["poll_ms"] = int(cfg.get("poll_ms", 500))
    cfg["follow_obsidian"] = bool(cfg.get("follow_obsidian", False))
    cfg["launch_cursor_if_missing"] = bool(cfg.get("launch_cursor_if_missing", True))
    cfg["daemon_host"] = normalize_daemon_bind_host(str(cfg.get("daemon_host", "127.0.0.1")))
    cfg["daemon_port"] = int(cfg.get("daemon_port", 27845))
    cfg["live_follow"] = bool(cfg.get("live_follow", True))
    cfg["follow_debounce_ms"] = int(cfg.get("follow_debounce_ms", 40))
    cfg["debug"] = bool(cfg.get("debug", False))
    return cfg


def update_config_file(updates: dict[str, Any]) -> None:
    path = ROOT / "config.json"
    raw: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                raw = loaded
        except (OSError, json.JSONDecodeError):
            raw = {}
    raw.update(updates)
    atomic_write_text(path, json.dumps(raw, indent=2, ensure_ascii=False) + "\n")


def read_state() -> dict[str, Any]:
    if not STATE_FILE.is_file():
        return {}
    try:
        raw = STATE_FILE.read_text(encoding="utf-8")
    except OSError:
        return {}
    data = recover_state_dict(raw)
    if not data and raw.strip():
        # Malformed leftover — quarantine
        try:
            bad = STATE_FILE.with_suffix(".state.corrupt")
            os.replace(STATE_FILE, bad)
        except OSError:
            pass
    return data


def write_state(data: dict[str, Any]) -> None:
    atomic_write_text(
        STATE_FILE,
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
    )


def clear_attached_state(keep_file: bool = True) -> None:
    if not keep_file:
        if STATE_FILE.exists():
            STATE_FILE.unlink()
        return
    state = read_state()
    state["attached"] = False
    state["timestamp"] = time.time()
    write_state(state)


def read_pid(path: Path = PID_FILE) -> int | None:
    if not path.is_file():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except ValueError:
        return None


def write_pid(pid: int, path: Path = PID_FILE) -> None:
    atomic_write_text(path, str(pid) + "\n")


def clear_pid(path: Path = PID_FILE) -> None:
    if path.exists():
        path.unlink()


def ensure_daemon_token() -> str:
    """Load or create daemon auth token (never log the full value)."""
    import secrets

    if TOKEN_FILE.is_file():
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if token:
            return token
    token = secrets.token_urlsafe(32)
    atomic_write_text(TOKEN_FILE, token + "\n")
    return token


def binding_live(record: dict[str, Any] | None) -> bool:
    return bool(validate_window_binding(record)["ok"])


def binding_report(record: dict[str, Any] | None) -> dict[str, Any]:
    return validate_window_binding(record)


def mark_stale(reason: str) -> None:
    with LIFECYCLE_LOCK:
        state = read_state()
        state["attached"] = False
        state["stale_reason"] = reason
        state["timestamp"] = time.time()
        write_state(state)
        stop_live_follow()


def stop_live_follow() -> None:
    global _FOLLOW
    if _FOLLOW is not None:
        try:
            _FOLLOW.stop()
        except Exception:
            pass


def ensure_live_follow_service(cfg: dict[str, Any]) -> LiveFollowService:
    global _FOLLOW
    if _FOLLOW is None:
        _FOLLOW = LiveFollowService(
            lifecycle_lock=LIFECYCLE_LOCK,
            get_attached_state=read_state,
            mark_stale=mark_stale,
            gap=int(cfg.get("gap", 0)),
            debounce_ms=int(cfg.get("follow_debounce_ms", 40)),
            debug=bool(cfg.get("debug", False)),
            enabled=bool(cfg.get("live_follow", True)),
        )
    else:
        _FOLLOW.enabled = bool(cfg.get("live_follow", True))
        _FOLLOW.gap = int(cfg.get("gap", 0))
        _FOLLOW.debug = bool(cfg.get("debug", False))
    return _FOLLOW


def start_live_follow_if_daemon(cfg: dict[str, Any], state: dict[str, Any]) -> None:
    """WinEventHook only runs inside the long-lived daemon process."""
    if not _DAEMON_MODE:
        return
    if not cfg.get("live_follow", True):
        stop_live_follow()
        return
    if not state.get("attached"):
        stop_live_follow()
        return
    obs = state.get("obsidian") or {}
    cur = state.get("cursor") or {}
    if not binding_live(obs) or not binding_live(cur):
        return
    right = state.get("right_rect")
    if right and len(right) == 4:
        width = max(int(right[2]) - int(right[0]), 1)
    else:
        work = get_work_area_for_hwnd(int(obs["hwnd"])).as_tuple()
        width = cursor_width_from_work(work, float(cfg["cursor_ratio"]), int(cfg.get("gap", 0)))
    svc = ensure_live_follow_service(cfg)
    svc.start(int(obs["hwnd"]), int(cur["hwnd"]), width)


def apply_preset_to_cfg(cfg: dict[str, Any], preset: str) -> None:
    preset = resolve_preset(preset)
    o, c = ratios_for_preset(preset)
    cfg["preset"] = preset
    cfg["obsidian_ratio"] = o
    cfg["cursor_ratio"] = c


def cmd_set_live_follow(cfg: dict[str, Any], enabled: bool, **_: Any) -> int:
    """Enable/disable Live Follow without Detach. Persists config.json live_follow."""
    with LIFECYCLE_LOCK:
        cfg["live_follow"] = bool(enabled)
        try:
            update_config_file({"live_follow": bool(enabled)})
        except OSError as exc:
            print(f"[sidecar] could not persist live_follow: {exc}")
        if not enabled:
            stop_live_follow()
            if _FOLLOW is not None:
                _FOLLOW.enabled = False
            print("[sidecar] live_follow=false (hook stopped; still attached if previously attached)")
            return 0
        svc = ensure_live_follow_service(cfg)
        svc.enabled = True
        truth = refresh_attachment_truth()
        if truth["attached"]:
            start_live_follow_if_daemon(cfg, truth["state"])
            print("[sidecar] live_follow=true (follow started for attached session)")
        else:
            print("[sidecar] live_follow=true (will follow on next Attach)")
        return 0


def refresh_attachment_truth(state: dict[str, Any] | None = None) -> dict[str, Any]:
    """Recompute attached/state_valid; persist correction if stale."""
    with LIFECYCLE_LOCK:
        state = dict(state if state is not None else read_state())
        obs = state.get("obsidian") or {}
        cur = state.get("cursor") or {}
        verdict = evaluate_attachment(
            state,
            obsidian_live=binding_live(obs),
            cursor_live=binding_live(cur),
        )
        if state.get("attached") and not verdict["attached"]:
            state["attached"] = False
            state["stale_reason"] = verdict["reason"]
            state["timestamp"] = time.time()
            write_state(state)
            stop_live_follow()
            return {**verdict, "state": state}

        # After auto-detach, keep reporting the stale cause until next Attach
        if (
            not verdict["attached"]
            and state.get("stale_reason")
            and verdict.get("reason") == "detached"
        ):
            return {
                "attached": False,
                "state_valid": False,
                "reason": str(state["stale_reason"]),
                "state": state,
            }
        return {**verdict, "state": state}


def ensure_cursor(
    cfg: dict[str, Any],
    workspace: str | None = None,
    file_path: str | None = None,
) -> None:
    proc = cfg["cursor_process"]
    exe = resolve_cursor_exe(cfg.get("cursor_exe_candidates", []))
    args: list[str] = []
    if file_path:
        args.append(file_path)
    elif workspace:
        args.append(workspace)

    if is_process_running(proc):
        if args and exe:
            print(f"[sidecar] opening in Cursor: {' '.join(args)}")
            launch_process(exe, args=args)
        return

    if not cfg["launch_cursor_if_missing"]:
        raise RuntimeError(f"{proc} is not running and launch_cursor_if_missing=false")
    if not exe:
        raise RuntimeError("Cursor.exe not found. Add path to config.cursor_exe_candidates")
    print(f"[sidecar] launching {exe}" + (f" {' '.join(args)}" if args else ""))
    launch_process(exe, args=args or None)
    found = wait_for_window(
        lambda: find_cursor_window(proc, cfg.get("cursor_title_hint", "")),
        timeout_s=45.0,
    )
    if not found:
        raise RuntimeError("Cursor launched but window not found in time")


def _monitor_arg(cfg: dict[str, Any]) -> int | None:
    monitor = cfg.get("monitor")
    if monitor in (None, "auto"):
        return None
    return int(monitor)


def cmd_attach(
    cfg: dict[str, Any],
    workspace: str | None = None,
    file_path: str | None = None,
) -> int:
    """Attach Sidecar: save originals once, bind HWNDs, arrange by preset. Idempotent."""
    with LIFECYCLE_LOCK:
        # Prefer preset stored in state if present
        state0 = read_state()
        if state0.get("preset"):
            try:
                apply_preset_to_cfg(cfg, str(state0["preset"]))
            except ValueError:
                pass
        ensure_cursor(cfg, workspace=workspace, file_path=file_path)

        obs = wait_for_window(
            lambda: find_obsidian_window(
                cfg["obsidian_process"], cfg.get("obsidian_title_hint", "")
            ),
            timeout_s=5.0,
        )
        if not obs:
            print("[sidecar] Obsidian window not found")
            return 1

        truth = refresh_attachment_truth()
        state = truth["state"]

        if truth["attached"]:
            obs_b = resolve_bound_window(
                state.get("obsidian"),
                cfg["obsidian_process"],
                cfg.get("obsidian_title_hint", ""),
            )
            cur_b = resolve_bound_window(
                state.get("cursor"),
                cfg["cursor_process"],
                cfg.get("cursor_title_hint", ""),
                allow_hidden=True,
            )
            if obs_b and cur_b:
                left, right = arrange_bound_windows(
                    obs_b,
                    cur_b,
                    obsidian_ratio=cfg["obsidian_ratio"],
                    cursor_ratio=cfg["cursor_ratio"],
                    gap=cfg["gap"],
                    monitor=_monitor_arg(cfg),
                )
                state["left_rect"] = list(left.as_tuple())
                state["right_rect"] = list(right.as_tuple())
                state["attached"] = True
                state["preset"] = cfg.get("preset", "normal")
                state["timestamp"] = time.time()
                state["stale_reason"] = None
                write_state(state)
                start_live_follow_if_daemon(cfg, state)
                if _FOLLOW:
                    _FOLLOW.update_cursor_width(right.width)
                    _FOLLOW.follow_now()
                print("[sidecar] attach (idempotent rearrange)")
                return 0

        cur = resolve_bound_window(
            state.get("cursor") if binding_live(state.get("cursor")) else None,
            cfg["cursor_process"],
            cfg.get("cursor_title_hint", ""),
            allow_hidden=True,
        )
        if not cur:
            cur = find_cursor_window(cfg["cursor_process"], cfg.get("cursor_title_hint", ""))
        if not cur:
            print("[sidecar] Cursor window not found")
            return 1

        obs_snap = snapshot_window(obs.hwnd)
        cur_snap = snapshot_window(cur.hwnd)
        obs_snap["process"] = cfg["obsidian_process"]
        cur_snap["process"] = cfg["cursor_process"]

        left, right = arrange_bound_windows(
            obs,
            cur,
            obsidian_ratio=cfg["obsidian_ratio"],
            cursor_ratio=cfg["cursor_ratio"],
            gap=cfg["gap"],
            monitor=_monitor_arg(cfg),
        )

        new_state = {
            "attached": True,
            "timestamp": time.time(),
            "version": VERSION,
            "preset": cfg.get("preset", "normal"),
            "obsidian": obs_snap,
            "cursor": cur_snap,
            "left_rect": list(left.as_tuple()),
            "right_rect": list(right.as_tuple()),
            "bound": {
                "obsidian_hwnd": obs.hwnd,
                "obsidian_pid": obs.pid,
                "cursor_hwnd": cur.hwnd,
                "cursor_pid": cur.pid,
            },
            "stale_reason": None,
        }
        write_state(new_state)
        start_live_follow_if_daemon(cfg, new_state)
        print(
            f"[sidecar] attached\n"
            f"  Obsidian hwnd={obs.hwnd} pid={obs.pid}\n"
            f"  Cursor   hwnd={cur.hwnd} pid={cur.pid}\n"
            f"  preset={cfg.get('preset')} L={left.as_tuple()} R={right.as_tuple()}"
        )
        return 0


def cmd_detach(cfg: dict[str, Any], **_: Any) -> int:
    """Detach Sidecar and restore original WindowPlacement for bound windows only."""
    with LIFECYCLE_LOCK:
        stop_live_follow()
        truth = refresh_attachment_truth()
        state = truth["state"]
        preset = state.get("preset") or cfg.get("preset", "normal")
        if not state.get("obsidian") and not state.get("cursor"):
            print("[sidecar] no saved state; already detached")
            write_state(
                {
                    "attached": False,
                    "timestamp": time.time(),
                    "version": VERSION,
                    "preset": preset,
                }
            )
            return 0

        follow = read_pid(PID_FILE)
        if follow:
            try:
                os.kill(follow, signal.SIGTERM)
            except OSError:
                pass
            clear_pid(PID_FILE)

        obs_snap = state.get("obsidian") or {}
        cur_snap = state.get("cursor") or {}

        def _restore(snap: dict[str, Any]) -> str:
            check = validate_window_binding(snap if snap else None)
            decision = can_restore_bound_window(snap, binding_ok=bool(check.get("ok")))
            if decision == "skip":
                return "skip"
            if decision == "gone":
                return "gone"
            hwnd = int(snap["hwnd"])
            ok = apply_window_placement(hwnd, snap)
            return "ok" if ok else "fail"

        r_obs = _restore(obs_snap)
        r_cur = _restore(cur_snap)

        write_state(
            {
                "attached": False,
                "timestamp": time.time(),
                "version": VERSION,
                "preset": preset,
                "last_detach": {
                    "obsidian": r_obs,
                    "cursor": r_cur,
                },
                "obsidian": obs_snap,
                "cursor": cur_snap,
            }
        )
        print(f"[sidecar] detached (obsidian={r_obs}, cursor={r_cur})")
        return 0


def cmd_click(
    cfg: dict[str, Any],
    workspace: str | None = None,
    file_path: str | None = None,
) -> int:
    """Ribbon: Attach if detached, Detach if attached. NOT show/hide."""
    truth = refresh_attachment_truth()
    if truth["attached"]:
        return cmd_detach(cfg)
    return cmd_attach(cfg, workspace=workspace, file_path=file_path)


def cmd_arrange(cfg: dict[str, Any], **_: Any) -> int:
    """Re-arrange bound windows by preset. Does not rediscover a new Cursor if unbound."""
    with LIFECYCLE_LOCK:
        truth = refresh_attachment_truth()
        if not truth["attached"]:
            print("[sidecar] arrange requires attached session (use attach)")
            return 1
        state = truth["state"]
        if state.get("preset"):
            try:
                apply_preset_to_cfg(cfg, str(state["preset"]))
            except ValueError:
                pass
        obs = resolve_bound_window(
            state.get("obsidian"),
            cfg["obsidian_process"],
            cfg.get("obsidian_title_hint", ""),
            allow_rediscovery=False,
        )
        cur = resolve_bound_window(
            state.get("cursor"),
            cfg["cursor_process"],
            cfg.get("cursor_title_hint", ""),
            allow_hidden=True,
            allow_rediscovery=False,
        )
        if not obs or not cur:
            print("[sidecar] arrange skipped: bound window missing (not rediscovering)")
            return 1
        left, right = arrange_bound_windows(
            obs,
            cur,
            obsidian_ratio=cfg["obsidian_ratio"],
            cursor_ratio=cfg["cursor_ratio"],
            gap=cfg["gap"],
            monitor=_monitor_arg(cfg),
        )
        state["left_rect"] = list(left.as_tuple())
        state["right_rect"] = list(right.as_tuple())
        state["preset"] = cfg.get("preset", "normal")
        state["timestamp"] = time.time()
        write_state(state)
        start_live_follow_if_daemon(cfg, state)
        if _FOLLOW:
            _FOLLOW.update_cursor_width(right.width)
            _FOLLOW.follow_now()
        print(f"[sidecar] arranged preset={cfg.get('preset')} L={left.as_tuple()} R={right.as_tuple()}")
        return 0


def cmd_preset(cfg: dict[str, Any], preset: str | None = None, **_: Any) -> int:
    """Set Compact/Normal/Wide. Arrange immediately if attached."""
    name = preset or cfg.get("preset") or "normal"
    with LIFECYCLE_LOCK:
        try:
            apply_preset_to_cfg(cfg, str(name))
        except ValueError as exc:
            print(f"[sidecar] {exc}")
            return 1
        state = read_state()
        state["preset"] = cfg["preset"]
        state["timestamp"] = time.time()
        write_state(state)
        try:
            update_config_file({"preset": cfg["preset"]})
        except OSError:
            pass
        print(f"[sidecar] preset={cfg['preset']} ratios={cfg['obsidian_ratio']}/{cfg['cursor_ratio']}")
        truth = refresh_attachment_truth()
        if truth["attached"]:
            return cmd_arrange(cfg)
        return 0


def cmd_focus(cfg: dict[str, Any], **_: Any) -> int:
    truth = refresh_attachment_truth()
    state = truth["state"]
    cur = resolve_bound_window(
        state.get("cursor") if truth["attached"] else None,
        cfg["cursor_process"],
        cfg.get("cursor_title_hint", ""),
        allow_hidden=True,
    )
    if not cur:
        print("[sidecar] Cursor window not found")
        return 1
    focus_window(cur.hwnd)
    print(f"[sidecar] focused Cursor hwnd={cur.hwnd}")
    return 0


def cmd_toggle(cfg: dict[str, Any], **_: Any) -> int:
    truth = refresh_attachment_truth()
    binding = (truth["state"].get("cursor") if truth["attached"] else None)
    state = toggle_cursor_visibility(
        cfg["cursor_process"],
        cfg.get("cursor_title_hint", ""),
        binding=binding,
    )
    print(f"[sidecar] Cursor {state}")
    return 0


def cmd_show(cfg: dict[str, Any], **_: Any) -> int:
    truth = refresh_attachment_truth()
    cur = resolve_bound_window(
        truth["state"].get("cursor") if truth["attached"] else None,
        cfg["cursor_process"],
        cfg.get("cursor_title_hint", ""),
        allow_hidden=True,
    )
    if not cur:
        print("[sidecar] Cursor window not found")
        return 1
    focus_window(cur.hwnd)
    print("[sidecar] Cursor shown")
    return 0


def cmd_hide(cfg: dict[str, Any], **_: Any) -> int:
    truth = refresh_attachment_truth()
    cur = resolve_bound_window(
        truth["state"].get("cursor") if truth["attached"] else None,
        cfg["cursor_process"],
        cfg.get("cursor_title_hint", ""),
        allow_hidden=True,
    )
    if not cur:
        print("[sidecar] Cursor window not found")
        return 1
    from window import hide_window

    hide_window(cur.hwnd)
    print("[sidecar] Cursor hidden")
    return 0


def cmd_open(
    cfg: dict[str, Any],
    workspace: str | None = None,
    file_path: str | None = None,
) -> int:
    if not file_path and not workspace:
        print("[sidecar] open requires --file or --workspace")
        return 1
    ensure_cursor(cfg, workspace=workspace, file_path=file_path)
    print(f"[sidecar] opened {file_path or workspace}")
    return 0


def build_status(cfg: dict[str, Any]) -> dict[str, Any]:
    truth = refresh_attachment_truth()
    state = truth["state"]
    obs_rep = binding_report(state.get("obsidian"))
    cur_rep = binding_report(state.get("cursor"))
    obs_live = bool(obs_rep.get("ok"))
    cur_live = bool(cur_rep.get("ok"))

    legacy = bool(obs_rep.get("legacy_binding") or cur_rep.get("legacy_binding"))
    # Prefer cursor binding mode when attached; else best available
    if cur_rep.get("binding_mode") and cur_rep.get("binding_mode") not in ("none", "invalid"):
        binding_mode = cur_rep.get("binding_mode")
    else:
        binding_mode = obs_rep.get("binding_mode") or "none"

    restore_safe = bool(
        (not state.get("attached") and truth["reason"] == "detached")
        or (
            obs_rep.get("restore_safe")
            and cur_rep.get("restore_safe")
            and truth["attached"]
        )
        or (
            # Detach path: restore is safe only for windows still bound
            (not truth["attached"])
            and truth["reason"] != "detached"
        )
    )
    # Clearer: restore_safe means "if you detach now, you will only touch exact HWNDs"
    if truth["attached"]:
        restore_safe = bool(obs_rep.get("ok") and cur_rep.get("ok"))
    else:
        restore_safe = True  # nothing to restore / already detached

    obs_info = None
    if obs_live:
        info = window_info_from_hwnd(int(state["obsidian"]["hwnd"]))
        if info:
            obs_info = {
                "hwnd": info.hwnd,
                "pid": info.pid,
                "title": info.title,
                "rect": list(info.rect.as_tuple()),
                "bound": True,
            }
    if obs_info is None:
        found = find_obsidian_window(
            cfg["obsidian_process"], cfg.get("obsidian_title_hint", "")
        )
        if found:
            obs_info = {
                "hwnd": found.hwnd,
                "pid": found.pid,
                "title": found.title,
                "rect": list(found.rect.as_tuple()),
                "bound": False,
            }

    cur_info = None
    if cur_live:
        info = window_info_from_hwnd(int(state["cursor"]["hwnd"]))
        if info:
            cur_info = {
                "hwnd": info.hwnd,
                "pid": info.pid,
                "title": info.title,
                "rect": list(info.rect.as_tuple()),
                "bound": True,
                "visible": bool(__import__("win32gui").IsWindowVisible(info.hwnd)),
            }
    if cur_info is None:
        found = find_cursor_window(
            cfg["cursor_process"], cfg.get("cursor_title_hint", "")
        )
        if found:
            cur_info = {
                "hwnd": found.hwnd,
                "pid": found.pid,
                "title": found.title,
                "rect": list(found.rect.as_tuple()),
                "bound": False,
                "visible": bool(__import__("win32gui").IsWindowVisible(found.hwnd)),
            }

    return {
        "version": VERSION,
        "attached": truth["attached"],
        "state_valid": truth["state_valid"],
        "reason": truth["reason"],
        "preset": state.get("preset") or cfg.get("preset", "normal"),
        "binding_mode": binding_mode,
        "legacy_binding": legacy,
        "restore_safe": restore_safe,
        "live_follow": (
            _FOLLOW.status()
            if _FOLLOW is not None
            else {
                "enabled": bool(cfg.get("live_follow", True)),
                "running": False,
                "hook_installed": False,
                "moving": False,
                "last_event": "",
                "last_follow_at": None,
            }
        ),
        "obsidian_binding": {
            "ok": obs_live,
            "legacy_binding": bool(obs_rep.get("legacy_binding")),
            "reason": obs_rep.get("reason"),
        },
        "cursor_binding": {
            "ok": cur_live,
            "legacy_binding": bool(cur_rep.get("legacy_binding")),
            "reason": cur_rep.get("reason"),
        },
        "obsidian": obs_info,
        "cursor": cur_info,
        "cursor_running": is_process_running(cfg["cursor_process"]),
        "follow_pid": read_pid(PID_FILE),
        "daemon_pid": read_pid(DAEMON_PID_FILE),
        "left_rect": state.get("left_rect"),
        "right_rect": state.get("right_rect"),
    }


def cmd_status(cfg: dict[str, Any], as_json: bool = False, **_: Any) -> int:
    payload = build_status(cfg)
    if as_json:
        print(json.dumps(payload, ensure_ascii=False))
        return 0
    print("[sidecar] status")
    print(f"  attached: {payload['attached']}  state_valid: {payload['state_valid']}  ({payload['reason']})")
    if payload["obsidian"]:
        o = payload["obsidian"]
        print(f"  Obsidian: hwnd={o['hwnd']} pid={o['pid']} bound={o['bound']} \"{o['title']}\"")
    else:
        print("  Obsidian: not found")
    if payload["cursor"]:
        c = payload["cursor"]
        print(f"  Cursor:   hwnd={c['hwnd']} pid={c['pid']} bound={c.get('bound')} \"{c['title']}\"")
    else:
        print("  Cursor:   not found")
    print(f"  cursor_running: {payload['cursor_running']}")
    print(f"  follow_pid: {payload['follow_pid'] or 'none'}")
    print(f"  daemon_pid: {payload['daemon_pid'] or 'none'}")
    return 0


def cmd_stop(cfg: dict[str, Any], **_: Any) -> int:
    del cfg
    stop_live_follow()
    stopped = False
    for label, path in (("daemon", DAEMON_PID_FILE), ("follow", PID_FILE)):
        pid = read_pid(path)
        if not pid:
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            print(f"[sidecar] stopped {label} pid={pid}")
            stopped = True
        except OSError as exc:
            print(f"[sidecar] could not stop {label}: {exc}")
        clear_pid(path)
    if not stopped:
        print("[sidecar] nothing to stop")
    return 0


# ---- Optional thin daemon (kept for plugin; lifecycle is attach/detach) ----

def handle_rpc(cfg: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    cmd = str(body.get("cmd") or body.get("command") or "").strip().lower()
    aliases = {"dock": "attach", "undock": "detach", "restore": "detach"}
    cmd = aliases.get(cmd, cmd)
    workspace = body.get("workspace")
    file_path = body.get("file") or body.get("file_path")
    preset = body.get("preset")
    try:
        if cmd == "attach":
            code = cmd_attach(cfg, workspace=workspace, file_path=file_path)
        elif cmd == "detach":
            code = cmd_detach(cfg)
        elif cmd == "click":
            code = cmd_click(cfg, workspace=workspace, file_path=file_path)
        elif cmd == "arrange":
            code = cmd_arrange(cfg)
        elif cmd in ("preset", "compact", "normal", "wide"):
            name = preset or (cmd if cmd != "preset" else cfg.get("preset"))
            code = cmd_preset(cfg, preset=str(name))
        elif cmd in ("set-live-follow", "set_live_follow", "live-follow"):
            if "enabled" not in body:
                return {"ok": False, "error": "enabled required"}
            code = cmd_set_live_follow(cfg, enabled=bool(body.get("enabled")))
        elif cmd == "toggle":
            code = cmd_toggle(cfg)
        elif cmd == "show":
            code = cmd_show(cfg)
        elif cmd == "hide":
            code = cmd_hide(cfg)
        elif cmd == "focus":
            code = cmd_focus(cfg)
        elif cmd == "open":
            code = cmd_open(cfg, workspace=workspace, file_path=file_path)
        elif cmd == "status":
            return {"ok": True, "cmd": cmd, "status": build_status(cfg)}
        else:
            return {"ok": False, "error": f"unknown cmd: {cmd}"}
        truth = refresh_attachment_truth()
        return {
            "ok": code == 0,
            "cmd": cmd,
            "code": code,
            "attached": truth["attached"],
            "state_valid": truth["state_valid"],
            "preset": cfg.get("preset"),
            "live_follow": _FOLLOW.status() if _FOLLOW else None,
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "cmd": cmd, "error": str(exc)}


def run_daemon(cfg: dict[str, Any]) -> int:
    global _DAEMON_MODE
    _DAEMON_MODE = True
    if cfg.get("debug"):
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    host, port = cfg["daemon_host"], cfg["daemon_port"]
    expected_token = ensure_daemon_token()
    print(f"[sidecar] daemon http://{host}:{port} (token auth; live_follow={cfg.get('live_follow')})")

    ensure_live_follow_service(cfg)
    # Resume follow if already attached when daemon starts
    st = read_state()
    if st.get("attached"):
        start_live_follow_if_daemon(cfg, st)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            print(f"[daemon] {self.command} {self.path}")

        def _authorized(self) -> bool:
            got = self.headers.get("X-Cursor-Sidecar-Token") or ""
            return bool(got) and got == expected_token

        def _send(self, code: int, payload: dict[str, Any]) -> None:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _forbid(self) -> None:
            self._send(403, {"ok": False, "error": "forbidden"})

        def do_OPTIONS(self) -> None:  # noqa: N802
            self.send_response(405)
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            if not self._authorized():
                self._forbid()
                return
            path = urlparse(self.path).path
            if path in ("/", "/health"):
                self._send(
                    200,
                    {"ok": True, "service": "cursor-sidecar", "version": VERSION},
                )
                return
            if path == "/status":
                self._send(200, {"ok": True, "status": build_status(cfg)})
                return
            self._send(404, {"ok": False, "error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if not self._authorized():
                self._forbid()
                return
            if urlparse(self.path).path not in ("/rpc", "/"):
                self._send(404, {"ok": False, "error": "not found"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                self._send(400, {"ok": False, "error": "invalid json"})
                return
            result = handle_rpc(cfg, body if isinstance(body, dict) else {})
            self._send(200 if result.get("ok") else 500, result)

    server = ThreadingHTTPServer((host, port), Handler)
    write_pid(os.getpid(), DAEMON_PID_FILE)
    http_thread = threading.Thread(target=server.serve_forever, name="sidecar-http", daemon=True)
    http_thread.start()
    _STOP_DAEMON.clear()

    def _stop(_sig: int, _frame: object) -> None:
        print("\n[sidecar] daemon stopping")
        _STOP_DAEMON.set()
        stop_live_follow()
        server.shutdown()
        clear_pid(DAEMON_PID_FILE)

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    try:
        while not _STOP_DAEMON.is_set():
            time.sleep(0.25)
    finally:
        stop_live_follow()
        clear_pid(DAEMON_PID_FILE)
        _DAEMON_MODE = False
    return 0


def cmd_daemon(cfg: dict[str, Any], **_: Any) -> int:
    return run_daemon(cfg)


def cmd_daemon_start(cfg: dict[str, Any], **_: Any) -> int:
    ensure_daemon_token()  # create token file before spawn
    existing = read_pid(DAEMON_PID_FILE)
    if existing:
        try:
            os.kill(existing, 0)
            print(f"[sidecar] daemon already running pid={existing}")
            return 0
        except OSError:
            clear_pid(DAEMON_PID_FILE)
    host = normalize_daemon_bind_host(str(cfg["daemon_host"]))
    port = int(cfg["daemon_port"])
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
    subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "main.py"),
            "-c",
            str(DEFAULT_CONFIG),
            "daemon",
            *daemon_endpoint_args(host, port),
        ],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
        close_fds=True,
    )
    time.sleep(0.5)
    print(f"[sidecar] daemon-start http://{host}:{port} (auth token file ready)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cursor-sidecar", description="Cursor Sidecar v0.3 Live Sidecar")
    p.add_argument("-c", "--config", default=str(DEFAULT_CONFIG))
    p.add_argument("--workspace", default=None)
    p.add_argument("--file", default=None)
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("attach", help="Attach Sidecar (save originals + arrange)")
    sub.add_parser("detach", help="Detach and restore original window placements")
    sub.add_parser("click", help="Attach if detached, Detach if attached")
    sub.add_parser("arrange", help="Re-arrange bound windows by preset")
    preset_p = sub.add_parser("preset", help="Set width preset: compact|normal|wide")
    preset_p.add_argument("name", nargs="?", default="normal")
    sub.add_parser("compact", help="preset compact")
    sub.add_parser("normal", help="preset normal")
    sub.add_parser("wide", help="preset wide")
    live_p = sub.add_parser("set-live-follow", help="Enable/disable Live Follow without Detach")
    live_p.add_argument("--enabled", choices=("true", "false", "1", "0", "on", "off"), required=True)
    sub.add_parser("focus", help="Focus bound Cursor window")
    sub.add_parser("toggle", help="Show/hide Cursor (advanced)")
    sub.add_parser("show", help="Show Cursor")
    sub.add_parser("hide", help="Hide Cursor")
    sub.add_parser("open", help="Open --file/--workspace in Cursor Desktop")
    status_p = sub.add_parser("status", help="Attachment + window status")
    status_p.add_argument("--json", action="store_true")
    sub.add_parser("stop", help="Stop follow/daemon pids")
    daemon_p = sub.add_parser("daemon", help="Foreground daemon (HTTP + Live Follow)")
    daemon_p.add_argument("--host", default=None, help="Bind host (localhost only)")
    daemon_p.add_argument("--port", type=int, default=None)
    daemon_start_p = sub.add_parser("daemon-start", help="Background daemon")
    daemon_start_p.add_argument("--host", default=None, help="Bind host (localhost only)")
    daemon_start_p.add_argument("--port", type=int, default=None)

    sub.add_parser("dock", help="alias of attach")
    sub.add_parser("undock", help="alias of detach")
    sub.add_parser("restore", help="alias of detach")
    return p


def main(argv: list[str] | None = None) -> int:
    dpi = enable_dpi_awareness()
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = load_config(Path(args.config))
    common = {"workspace": args.workspace, "file_path": args.file}

    # Apply CLI host/port overrides before daemon commands (same endpoint as plugin)
    if getattr(args, "host", None):
        cfg["daemon_host"] = normalize_daemon_bind_host(str(args.host))
    if getattr(args, "port", None) is not None:
        cfg["daemon_port"] = int(args.port)

    aliases = {"dock": "attach", "undock": "detach", "restore": "detach"}
    command = aliases.get(args.command, args.command)

    if command != "status":
        print(f"[sidecar] dpi={dpi}")

    if command in ("compact", "normal", "wide"):
        return cmd_preset(cfg, preset=command, **common)
    if command == "preset":
        return cmd_preset(cfg, preset=getattr(args, "name", None) or "normal", **common)
    if command == "set-live-follow":
        flag = str(getattr(args, "enabled", "true")).lower()
        enabled = flag in ("true", "1", "on", "yes")
        return cmd_set_live_follow(cfg, enabled=enabled, **common)

    dispatch = {
        "attach": cmd_attach,
        "detach": cmd_detach,
        "click": cmd_click,
        "arrange": cmd_arrange,
        "focus": cmd_focus,
        "toggle": cmd_toggle,
        "show": cmd_show,
        "hide": cmd_hide,
        "open": cmd_open,
        "stop": cmd_stop,
        "daemon": cmd_daemon,
        "daemon-start": cmd_daemon_start,
    }
    if command == "status":
        return cmd_status(cfg, as_json=bool(args.json), **common)
    if command in dispatch:
        return dispatch[command](cfg, **common)
    parser.error(f"unknown command: {command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
