#!/usr/bin/env python3
"""Cursor Sidecar v0.7-dev — Live Sidecar + Context Follow + Embedded Pane experiment."""

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

import psutil

from editor_bridge import EditorBridge
from geometry import (
    atomic_write_text,
    can_restore_bound_window,
    cursor_width_from_work,
    evaluate_attachment,
    ratios_for_preset,
    recover_state_dict,
    resolve_preset,
)
from context_follow import ContextFollowGate, is_excluded_rel_path, is_path_inside_vault
from context_sync import ContextSyncController, SyncJob, next_context_sync_seq
from cursor_windows import (
    agent_binding_ok,
    agent_hwnd,
    bind_agent_from_hwnd,
    bind_editor_from_hwnd,
    candidates_excluding_editor,
    clear_agent_binding,
    diff_new_hwnds,
    editor_binding_ok,
    editor_hwnd,
    get_agent_binding,
    get_editor_binding,
    list_cursor_top_level,
    migrate_cursor_roles,
    refresh_cursor_bindings,
    select_editor_window,
)
from cursor_exe import resolve_cursor_executable
from agents_auto import ensure_agents_window_bound
from embedded_window import (
    apply_borderless,
    apply_embedded_pane,
    restore_window_chrome,
    set_owner_experimental,
    snapshot_window_chrome,
)
from native_embed import (
    enter_native_child,
    exit_native_child,
    probe_hwnd_win32,
    recover_native_child,
    update_native_child,
)
from pane_geometry import DomPaneRect
from runtime_paths import (
    DEFAULT_CONFIG_VALUES,
    VERSION,
    build_self_command,
    get_paths,
    init_runtime,
    is_frozen,
    runtime_mode,
)
from window import (
    apply_window_placement,
    arrange_bound_windows,
    enable_dpi_awareness,
    find_cursor_window,
    find_obsidian_window,
    focus_window,
    get_foreground_hwnd,
    get_work_area_for_hwnd,
    get_window_rect,
    is_process_running,
    launch_process,
    resolve_bound_window,
    resolve_cursor_exe,
    restore_foreground_if_cursor_stole_focus,
    snapshot_window,
    toggle_cursor_visibility,
    validate_window_binding,
    wait_for_window,
    window_info_from_hwnd,
)
from win_events import LiveFollowService

# Path globals — refreshed by sync_path_globals() / init_runtime()
_paths = init_runtime()
ROOT = _paths.code_dir  # code/package dir (compat alias)
CODE_DIR = _paths.code_dir
DATA_DIR = _paths.data_dir
DEFAULT_CONFIG = _paths.config_path
PID_FILE = _paths.follow_pid_path
DAEMON_PID_FILE = _paths.daemon_pid_path
DAEMON_META_FILE = _paths.daemon_meta_path
TOKEN_FILE = _paths.daemon_token_path
STATE_FILE = _paths.state_path

LIFECYCLE_LOCK = threading.RLock()
_FOLLOW: LiveFollowService | None = None
_DAEMON_MODE = False
_STOP_DAEMON = threading.Event()
_DAEMON_HTTP_SERVER: ThreadingHTTPServer | None = None
_CONTEXT_FOLLOW_GATE = ContextFollowGate()
_CONTEXT_SYNC: ContextSyncController | None = None


def _get_context_sync() -> ContextSyncController:
    global _CONTEXT_SYNC
    if _CONTEXT_SYNC is None:
        _CONTEXT_SYNC = ContextSyncController(execute=_execute_context_sync_job)
    else:
        _CONTEXT_SYNC.set_execute(_execute_context_sync_job)
    return _CONTEXT_SYNC

LOCALHOST_BIND_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def sync_path_globals(data_dir_override: str | None = None) -> None:
    """Rebind module path globals after --data-dir / env override."""
    global ROOT, CODE_DIR, DATA_DIR, DEFAULT_CONFIG, PID_FILE
    global DAEMON_PID_FILE, DAEMON_META_FILE, TOKEN_FILE, STATE_FILE, _paths
    _paths = init_runtime(data_dir_override=data_dir_override)
    ROOT = _paths.code_dir
    CODE_DIR = _paths.code_dir
    DATA_DIR = _paths.data_dir
    DEFAULT_CONFIG = _paths.config_path
    PID_FILE = _paths.follow_pid_path
    DAEMON_PID_FILE = _paths.daemon_pid_path
    DAEMON_META_FILE = _paths.daemon_meta_path
    TOKEN_FILE = _paths.daemon_token_path
    STATE_FILE = _paths.state_path


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
    path = Path(path)
    disk: dict[str, Any] = {}
    if path.is_file():
        with path.open("r", encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            disk = loaded
    else:
        # Frozen / first-run: bootstrap defaults into DATA_DIR (never fail "config not found").
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        bootstrap = dict(DEFAULT_CONFIG_VALUES)
        atomic_write_text(
            path,
            json.dumps(bootstrap, indent=2, ensure_ascii=False) + "\n",
        )
        disk = bootstrap

    cfg: dict[str, Any] = {**DEFAULT_CONFIG_VALUES, **disk}
    # Preset drives ratios (v0.3). Legacy explicit ratios still accepted if no preset.
    preset = str(cfg.get("preset") or "normal")
    try:
        preset = resolve_preset(preset)
        o, c = ratios_for_preset(preset)
    except ValueError:
        preset = "normal"
        o, c = ratios_for_preset(preset)
    cfg["preset"] = preset
    cfg["obsidian_ratio"] = o
    cfg["cursor_ratio"] = c
    cfg["gap"] = int(cfg.get("gap", 0))
    cfg["monitor"] = cfg.get("monitor", None)
    cfg["poll_ms"] = int(cfg.get("poll_ms", 500))
    cfg["follow_obsidian"] = bool(cfg.get("follow_obsidian", False))
    cfg["launch_cursor_if_missing"] = bool(cfg.get("launch_cursor_if_missing", True))
    cfg["daemon_host"] = normalize_daemon_bind_host(str(cfg.get("daemon_host", "127.0.0.1")))
    cfg["daemon_port"] = int(cfg.get("daemon_port", 27845))
    cfg["live_follow"] = bool(cfg.get("live_follow", True))
    cfg["follow_debounce_ms"] = int(cfg.get("follow_debounce_ms", 40))
    cfg["debug"] = bool(cfg.get("debug", False))
    # Embedded Pane experiment (Phase 1 visual embed — defaults OFF)
    cfg["embed_borderless"] = bool(cfg.get("embed_borderless", False))
    cfg["experimental_owned_window"] = bool(cfg.get("experimental_owned_window", False))
    cfg["native_child_experiment"] = bool(cfg.get("native_child_experiment", False))
    backend = str(cfg.get("embed_backend") or "visual").strip().lower()
    cfg["embed_backend"] = "native_child" if backend in ("native_child", "native") else "visual"
    return cfg


def update_config_file(updates: dict[str, Any]) -> None:
    path = Path(DEFAULT_CONFIG)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    raw: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                raw = loaded
        except (OSError, json.JSONDecodeError):
            raw = {}
    if not raw:
        raw = dict(DEFAULT_CONFIG_VALUES)
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
    if data:
        migrate_cursor_roles(data)
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


def read_daemon_meta() -> dict[str, Any]:
    if not DAEMON_META_FILE.is_file():
        return {}
    try:
        data = json.loads(DAEMON_META_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _norm_exe_path(value: str) -> str:
    return os.path.normcase(os.path.abspath(os.path.expandvars(str(value))))


def write_daemon_meta(
    *,
    pid: int,
    host: str,
    port: int,
    process_create_time: float | None = None,
) -> None:
    if process_create_time is None:
        try:
            process_create_time = float(psutil.Process(int(pid)).create_time())
        except (psutil.Error, ValueError, TypeError):
            process_create_time = 0.0
    mode = runtime_mode()
    exe = str(Path(sys.executable).resolve())
    payload: dict[str, Any] = {
        "pid": int(pid),
        "process_create_time": float(process_create_time),
        "host": normalize_daemon_bind_host(host),
        "port": int(port),
        "started_at": time.time(),
        "runtime_mode": mode,
        "executable_path": exe,
        "entrypoint": "frozen" if mode == "frozen" else "main.py",
        "version": VERSION,
    }
    if mode == "source":
        payload["main_py"] = str(CODE_DIR / "main.py")
    atomic_write_text(
        DAEMON_META_FILE,
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
    )


def clear_daemon_meta() -> None:
    if DAEMON_META_FILE.exists():
        try:
            DAEMON_META_FILE.unlink()
        except OSError:
            pass


def verify_sidecar_daemon_process(meta: dict[str, Any]) -> dict[str, Any]:
    """Confirm metadata still points at our helper process (anti PID-reuse)."""
    if not meta:
        return {"ok": False, "reason": "no_meta"}
    try:
        pid = int(meta.get("pid") or 0)
        expected_ctime = float(meta.get("process_create_time") or 0.0)
    except (TypeError, ValueError):
        return {"ok": False, "reason": "malformed_meta"}
    if pid <= 0:
        return {"ok": False, "reason": "malformed_meta"}
    try:
        proc = psutil.Process(pid)
        live_ctime = float(proc.create_time())
        if expected_ctime and abs(live_ctime - expected_ctime) > 1.5:
            return {"ok": False, "reason": "create_time_mismatch", "pid": pid}
        try:
            cmdline = " ".join(proc.cmdline()).lower()
        except (psutil.Error, OSError):
            cmdline = ""
        try:
            live_exe = proc.exe() or ""
        except (psutil.Error, OSError, AttributeError):
            live_exe = ""

        mode = str(meta.get("runtime_mode") or "").strip().lower()
        if not mode:
            mode = "source" if "main.py" in cmdline else "frozen"
        expected_exe = str(meta.get("executable_path") or "").strip()

        if "daemon" not in cmdline:
            return {"ok": False, "reason": "not_sidecar_daemon", "pid": pid}

        if mode == "frozen":
            if expected_exe and live_exe:
                if _norm_exe_path(live_exe) != _norm_exe_path(expected_exe):
                    return {"ok": False, "reason": "exe_mismatch", "pid": pid}
            else:
                base = os.path.basename(live_exe).lower()
                if "cursor-sidecar" not in base:
                    return {"ok": False, "reason": "not_sidecar_daemon", "pid": pid}
        else:
            # SOURCE (and legacy meta): python + main.py + daemon
            if "main.py" not in cmdline:
                return {"ok": False, "reason": "not_sidecar_daemon", "pid": pid}
            if expected_exe and live_exe:
                if _norm_exe_path(live_exe) != _norm_exe_path(expected_exe):
                    return {"ok": False, "reason": "exe_mismatch", "pid": pid}

        return {
            "ok": True,
            "pid": pid,
            "host": str(meta.get("host") or ""),
            "port": int(meta.get("port") or 0),
            "process_create_time": live_ctime,
            "runtime_mode": mode,
            "executable_path": live_exe or expected_exe,
        }
    except psutil.Error:
        return {"ok": False, "reason": "gone", "pid": pid}


def request_daemon_shutdown() -> None:
    """Signal the in-process daemon loop to stop (authenticated RPC path)."""
    global _DAEMON_HTTP_SERVER
    _STOP_DAEMON.set()
    stop_live_follow()
    srv = _DAEMON_HTTP_SERVER
    if srv is not None:
        threading.Thread(target=srv.shutdown, name="sidecar-http-shutdown", daemon=True).start()


def stop_daemon_via_rpc(host: str, port: int, token: str, timeout: float = 3.0) -> bool:
    """Authenticated shutdown against a running daemon endpoint. Returns True if accepted."""
    import urllib.error
    import urllib.request

    url = f"http://{host}:{int(port)}/rpc"
    body = json.dumps({"cmd": "shutdown-daemon"}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
            "X-Cursor-Sidecar-Token": token,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8") or "{}")
            return bool(payload.get("ok"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return False


def stop_existing_daemon_safe(meta: dict[str, Any]) -> str:
    """
    Stop old helper only when identity verifies.
    Prefer authenticated RPC; fall back to terminate if still our process.
    """
    check = verify_sidecar_daemon_process(meta)
    if not check.get("ok"):
        clear_pid(DAEMON_PID_FILE)
        clear_daemon_meta()
        return f"stale ({check.get('reason')})"

    host = str(check.get("host") or meta.get("host") or "127.0.0.1")
    port = int(check.get("port") or meta.get("port") or 0)
    pid = int(check["pid"])
    token = ""
    if TOKEN_FILE.is_file():
        try:
            token = TOKEN_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            token = ""

    if token and port > 0:
        stop_daemon_via_rpc(host, port, token)
        deadline = time.time() + 3.0
        while time.time() < deadline:
            if not verify_sidecar_daemon_process(meta).get("ok"):
                clear_pid(DAEMON_PID_FILE)
                clear_daemon_meta()
                return "stopped_via_rpc"
            time.sleep(0.1)

    # Re-verify before kill — never terminate unrelated Python
    check2 = verify_sidecar_daemon_process(meta)
    if not check2.get("ok"):
        clear_pid(DAEMON_PID_FILE)
        clear_daemon_meta()
        return "exited"
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        clear_pid(DAEMON_PID_FILE)
        clear_daemon_meta()
        return f"kill_failed:{exc}"
    deadline = time.time() + 3.0
    while time.time() < deadline:
        if not verify_sidecar_daemon_process(meta).get("ok"):
            break
        time.sleep(0.1)
    clear_pid(DAEMON_PID_FILE)
    clear_daemon_meta()
    return "terminated"


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
    prev_pane = getattr(svc, "last_pane_dom", None)
    svc.start(int(obs["hwnd"]), int(cur["hwnd"]), width)
    if state.get("embedded") and str(state.get("embed_mode") or "") == "pane":
        svc.set_follow_mode("pane")
        pane = state.get("last_pane_dom")
        if isinstance(pane, dict):
            svc.set_last_pane_dom(pane)
        elif isinstance(prev_pane, dict):
            svc.set_last_pane_dom(prev_pane)
        if hasattr(svc, "set_agent_hwnd"):
            svc.set_agent_hwnd(agent_hwnd(state))
        if hasattr(svc, "set_embed_backend"):
            svc.set_embed_backend(
                "native_child" if state.get("native_child") else "visual"
            )
    else:
        if hasattr(svc, "set_follow_mode"):
            svc.set_follow_mode("sidecar")
        if not state.get("embedded"):
            if hasattr(svc, "set_last_pane_dom"):
                svc.set_last_pane_dom(None)
            if hasattr(svc, "set_agent_hwnd"):
                svc.set_agent_hwnd(0)
            if hasattr(svc, "set_embed_backend"):
                svc.set_embed_backend("visual")


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
    state = read_state()
    migrate_cursor_roles(state)
    refresh_cursor_bindings(state)
    resolved = resolve_cursor_executable(
        cfg,
        binding=get_editor_binding(state) or state.get("cursor"),
        persist=True,
    )
    exe = resolved.get("path") if resolved.get("ok") else None
    if exe:
        cfg["cursor_exe_verified"] = exe
    args: list[str] = []
    if file_path:
        args.extend(["--reuse-window", file_path])
    elif workspace:
        # Force vault into an existing Editor window when Cursor is already up
        args.extend(["--reuse-window", workspace])

    if is_process_running(proc):
        if args and exe:
            print(f"[sidecar] opening in Cursor: {' '.join(args)}")
            launch_process(exe, args=args)
        return

    if not cfg["launch_cursor_if_missing"]:
        raise RuntimeError(f"{proc} is not running and launch_cursor_if_missing=false")
    if not exe:
        raise RuntimeError(
            "Cursor.exe not found. Set path in settings (Advanced) or config.cursor_exe_verified"
        )
    # Cold start: open the vault folder directly (no --reuse-window needed)
    cold_args = [file_path or workspace] if (file_path or workspace) else None
    print(f"[sidecar] launching {exe}" + (f" {' '.join(cold_args)}" if cold_args else ""))
    launch_process(exe, args=cold_args)

    def _find_editor():
        sel = select_editor_window(proc)
        if not sel.get("ok"):
            return None
        w = sel["window"]
        from window import window_info_from_hwnd

        return window_info_from_hwnd(int(w["hwnd"]))

    found = wait_for_window(_find_editor, timeout_s=45.0)
    if not found:
        raise RuntimeError("Cursor launched but Editor window not classified in time")


def _open_workspace_in_bound_editor(
    cfg: dict[str, Any],
    state: dict[str, Any],
    workspace: str | None,
) -> dict[str, Any] | None:
    """After Attach: pin Obsidian vault into the bound Editor (--reuse-window)."""
    if not workspace:
        return None
    binding = get_editor_binding(state) or state.get("cursor")
    if not isinstance(binding, dict) or not binding.get("hwnd"):
        return None
    try:
        result = _editor_bridge(cfg).open_folder(
            binding=binding,
            path=str(workspace),
            focus=False,
        )
        if result.get("ok"):
            print(f"[sidecar] bound Editor workspace -> {workspace}")
        else:
            print(
                f"[sidecar] bound Editor workspace failed: "
                f"{result.get('error') or result}"
            )
        return result
    except Exception as exc:
        print(f"[sidecar] bound Editor workspace error: {exc}")
        return {"ok": False, "error": str(exc)}


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
        migrate_cursor_roles(state)
        refresh_cursor_bindings(state)
        write_state(state)

        if truth["attached"]:
            obs_b = resolve_bound_window(
                state.get("obsidian"),
                cfg["obsidian_process"],
                cfg.get("obsidian_title_hint", ""),
            )
            # Prefer live editor binding — never rediscover via largest window
            cur_b = None
            if editor_binding_ok(state).get("ok"):
                from window import window_info_from_hwnd

                cur_b = window_info_from_hwnd(editor_hwnd(state))
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
                _open_workspace_in_bound_editor(cfg, state, workspace)
                print("[sidecar] attach (idempotent rearrange)")
                return 0

        # Fresh editor selection: EDITOR only (Agents never bind as editor)
        sel = select_editor_window(cfg["cursor_process"])
        if not sel.get("ok"):
            # Wait briefly for Editor if only Agents present / still launching
            def _wait_editor():
                s2 = select_editor_window(cfg["cursor_process"])
                return s2 if s2.get("ok") else None

            waited = wait_for_window(_wait_editor, timeout_s=8.0, poll_s=0.35)
            if waited and waited.get("ok"):
                sel = waited
            else:
                err = sel.get("error") or "editor_window_not_found"
                print(f"[sidecar] Cursor Editor not found ({err})")
                if err == "editor_window_ambiguous":
                    print("[sidecar] multiple Editor candidates — refusing to guess")
                return 1

        from window import window_info_from_hwnd

        cur = window_info_from_hwnd(int(sel["window"]["hwnd"]))
        if not cur:
            print("[sidecar] Cursor Editor hwnd invalid")
            return 1
        # Double-check bind refuses AGENT
        cur_snap = bind_editor_from_hwnd(cur.hwnd, process_name=cfg["cursor_process"])
        if not cur_snap:
            print("[sidecar] refused to bind Agents Window as Editor")
            return 1

        obs_snap = snapshot_window(obs.hwnd)
        obs_snap["process"] = cfg["obsidian_process"]

        left, right = arrange_bound_windows(
            obs,
            cur,
            obsidian_ratio=cfg["obsidian_ratio"],
            cursor_ratio=cfg["cursor_ratio"],
            gap=cfg["gap"],
            monitor=_monitor_arg(cfg),
        )

        prev = read_state()
        migrate_cursor_roles(prev)
        refresh_cursor_bindings(prev)
        prev_agent = prev.get("cursor_agent") if isinstance(prev.get("cursor_agent"), dict) else None

        new_state = {
            "attached": True,
            "timestamp": time.time(),
            "version": VERSION,
            "preset": cfg.get("preset", "normal"),
            "obsidian": obs_snap,
            "cursor": cur_snap,
            "cursor_editor": cur_snap,
            "cursor_agent": prev_agent,
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
        _open_workspace_in_bound_editor(cfg, new_state, workspace)
        print(
            f"[sidecar] attached\n"
            f"  Obsidian hwnd={obs.hwnd} pid={obs.pid}\n"
            f"  Cursor Editor hwnd={cur.hwnd} pid={cur.pid}\n"
            f"  preset={cfg.get('preset')} L={left.as_tuple()} R={right.as_tuple()}"
        )
        return 0


def cmd_detach(cfg: dict[str, Any], **_: Any) -> int:
    """Detach Sidecar and restore original WindowPlacement for bound windows only."""
    with LIFECYCLE_LOCK:
        stop_live_follow()
        truth = refresh_attachment_truth()
        state = truth["state"]
        migrate_cursor_roles(state)
        # Exit Agents Pane chrome first (agent HWND only — not editor)
        if state.get("embedded") or state.get("native_child"):
            if state.get("native_child"):
                _force_exit_native_child(state)
            else:
                ah = agent_hwnd(state)
                if ah:
                    restore_window_chrome(ah, state.get("embedded_snapshot"))
            state["embedded"] = False
            state["embed_mode"] = "sidecar"
            state["embedded_snapshot"] = None
            state["last_pane_dom"] = None
            state["native_child"] = False
        preset = state.get("preset") or cfg.get("preset", "normal")
        if not state.get("obsidian") and not state.get("cursor"):
            print("[sidecar] no saved state; already detached")
            write_state(
                {
                    "attached": False,
                    "timestamp": time.time(),
                    "version": VERSION,
                    "preset": preset,
                    "embed_mode": "sidecar",
                    "embedded": False,
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
        cur_snap = get_editor_binding(state) or state.get("cursor") or {}
        agent_snap = get_agent_binding(state)

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
        r_agent = _restore(agent_snap) if agent_snap else "skip"

        write_state(
            {
                "attached": False,
                "timestamp": time.time(),
                "version": VERSION,
                "preset": preset,
                "embed_mode": "sidecar",
                "embedded": False,
                "last_detach": {
                    "obsidian": r_obs,
                    "cursor": r_cur,
                    "agent": r_agent,
                },
                "obsidian": obs_snap,
                "cursor": cur_snap,
                "cursor_editor": cur_snap,
                "cursor_agent": agent_snap,
            }
        )
        print(f"[sidecar] detached (obsidian={r_obs}, cursor={r_cur}, agent={r_agent})")
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


def _editor_bridge(cfg: dict[str, Any]) -> EditorBridge:
    return EditorBridge(
        validate_binding=validate_window_binding,
        focus_hwnd=focus_window,
        process_name=str(cfg.get("cursor_process") or "Cursor.exe"),
    )


def open_editor_file_result(
    cfg: dict[str, Any],
    *,
    vault_root: str | None,
    path: str | None,
    line: int | None = None,
    column: int | None = None,
    focus: bool = True,
) -> dict[str, Any]:
    """Context Bridge: open vault file in bound Cursor Desktop Editor. No state mutation."""
    truth = refresh_attachment_truth()
    if not truth["attached"]:
        return {
            "ok": False,
            "error": "sidecar_not_attached",
            "message": "Attach Cursor Sidecar first.",
        }
    if not path:
        return {"ok": False, "error": "path_required"}
    if not vault_root:
        return {"ok": False, "error": "vault_root_required"}

    state_before = read_state()
    binding = dict(state_before.get("cursor") or {})
    result = _editor_bridge(cfg).open_file(
        binding=binding,
        vault_root=vault_root,
        path=path,
        line=line,
        column=column,
        focus=bool(focus),
        preserve_foreground=False,
        routing_check=True,
    )
    # Guarantee Sidecar lifecycle fields untouched
    state_after = read_state()
    for key in ("attached", "preset", "obsidian", "cursor", "left_rect", "right_rect"):
        if state_before.get(key) != state_after.get(key):
            result["state_mutation_warning"] = key
            break
    result["cmd"] = "open-editor-file"
    return result


def _execute_context_sync_job(job: SyncJob) -> dict[str, Any]:
    """Worker body for Context Follow — silent, no routing wait, focus-safe."""
    cfg = load_config(Path(DEFAULT_CONFIG))
    truth = refresh_attachment_truth()
    if not truth["attached"]:
        return {"ok": False, "error": "sidecar_not_attached", "seq": job.seq}
    if not validate_window_binding(dict((truth["state"] or {}).get("cursor") or {})).get("ok"):
        return {"ok": False, "error": "stale_binding", "seq": job.seq}

    state = truth["state"] or {}
    binding = dict(state.get("cursor") or {})
    obs_hwnd = 0
    try:
        obs_hwnd = int((state.get("obsidian") or {}).get("hwnd") or 0)
    except (TypeError, ValueError):
        obs_hwnd = 0

    result = _editor_bridge(cfg).open_file(
        binding=binding,
        vault_root=job.vault_root,
        path=job.path,
        line=job.line,
        column=job.column,
        focus=False,
        preserve_foreground=True,
        routing_check=False,
        obsidian_hwnd=obs_hwnd or None,
        get_foreground_hwnd=get_foreground_hwnd,
        restore_if_stolen=restore_foreground_if_cursor_stole_focus,
    )
    result["cmd"] = "sync-editor-file"
    result["focused"] = False
    result["seq"] = job.seq
    if result.get("ok"):
        rel = job.relative_path
        _CONTEXT_FOLLOW_GATE.record(
            str(result.get("path") or job.path),
            result.get("line") if job.line is not None else None,
            relative_path=rel,
        )
    return result


def sync_editor_file_result(
    cfg: dict[str, Any],
    *,
    vault_root: str | None,
    path: str | None,
    line: int | None = None,
    column: int | None = None,
    relative_path: str | None = None,
    seq: int | None = None,
) -> dict[str, Any]:
    """Queue Context Follow job (latest-wins). Returns immediately with queued=true."""
    truth = refresh_attachment_truth()
    if not truth["attached"]:
        return {
            "ok": False,
            "error": "sidecar_not_attached",
            "message": "Attach Cursor Sidecar first.",
            "cmd": "sync-editor-file",
        }
    if not path:
        return {"ok": False, "error": "path_required", "cmd": "sync-editor-file"}
    if not vault_root:
        return {"ok": False, "error": "vault_root_required", "cmd": "sync-editor-file"}

    rel = relative_path
    if not rel:
        try:
            rel = str(Path(path).resolve().relative_to(Path(vault_root).resolve())).replace("\\", "/")
        except Exception:
            rel = None
    if rel and is_excluded_rel_path(rel):
        return {"ok": False, "error": "path_excluded", "cmd": "sync-editor-file", "path": rel}
    if not is_path_inside_vault(vault_root, path):
        return {"ok": False, "error": "path_outside_vault", "cmd": "sync-editor-file"}

    binding = dict((truth["state"] or {}).get("cursor") or {})
    if not validate_window_binding(binding).get("ok"):
        return {
            "ok": False,
            "error": "stale_binding",
            "message": "Bound Cursor gone; Context Follow idle.",
            "cmd": "sync-editor-file",
        }

    # seq: client-provided, else rebase against controller so fallback never goes stale
    # vs plugin clock-scale seqs (Date.now()*1000 ≈ epoch µs).
    if seq is None:
        ctrl = _get_context_sync()
        latest = int((ctrl.status() or {}).get("latest_seq") or 0)
        seq = next_context_sync_seq(0, latest, int(time.time_ns() // 1_000_000))
    job = SyncJob(
        seq=int(seq),
        vault_root=str(vault_root),
        path=str(path),
        line=line,
        column=column,
        relative_path=rel,
    )
    ctrl = _get_context_sync()
    # Ensure executor closed over current cfg path via DEFAULT_CONFIG
    ctrl.set_execute(_execute_context_sync_job)
    out = ctrl.submit(job)
    out["context_sync"] = ctrl.status()
    return out


def clear_context_sync() -> dict[str, Any]:
    ctrl = _get_context_sync()
    ctrl.clear_pending()
    return {"ok": True, "cmd": "context-sync-clear", "context_sync": ctrl.status()}


def open_editor_vault_result(
    cfg: dict[str, Any],
    *,
    path: str | None,
    focus: bool = True,
) -> dict[str, Any]:
    """Context Bridge: open vault folder in bound Cursor Desktop Editor. No state mutation."""
    truth = refresh_attachment_truth()
    if not truth["attached"]:
        return {
            "ok": False,
            "error": "sidecar_not_attached",
            "message": "Attach Cursor Sidecar first.",
        }
    if not path:
        return {"ok": False, "error": "path_required"}

    state_before = read_state()
    binding = dict(state_before.get("cursor") or {})
    result = _editor_bridge(cfg).open_folder(
        binding=binding,
        path=path,
        focus=bool(focus),
    )
    state_after = read_state()
    for key in ("attached", "preset", "obsidian", "cursor", "left_rect", "right_rect"):
        if state_before.get(key) != state_after.get(key):
            result["state_mutation_warning"] = key
            break
    result["cmd"] = "open-editor-vault"
    return result


def cmd_open_editor_file(
    cfg: dict[str, Any],
    *,
    vault_root: str | None = None,
    path: str | None = None,
    line: int | None = None,
    column: int | None = None,
    focus: bool = True,
    **_: Any,
) -> int:
    result = open_editor_file_result(
        cfg,
        vault_root=vault_root,
        path=path,
        line=line,
        column=column,
        focus=focus,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


def cmd_sync_editor_file(
    cfg: dict[str, Any],
    *,
    vault_root: str | None = None,
    path: str | None = None,
    line: int | None = None,
    column: int | None = None,
    relative_path: str | None = None,
    seq: int | None = None,
    **_: Any,
) -> int:
    result = sync_editor_file_result(
        cfg,
        vault_root=vault_root,
        path=path,
        line=line,
        column=column,
        relative_path=relative_path,
        seq=seq,
    )
    # Wait briefly so CLI users see worker outcome in status (optional)
    if result.get("queued"):
        time.sleep(0.35)
        last = _get_context_sync().last_result()
        if last:
            result["result"] = last
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


def cmd_open_editor_vault(
    cfg: dict[str, Any],
    *,
    path: str | None = None,
    focus: bool = True,
    **_: Any,
) -> int:
    result = open_editor_vault_result(cfg, path=path, focus=focus)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


def build_status(cfg: dict[str, Any]) -> dict[str, Any]:
    truth = refresh_attachment_truth()
    state = truth["state"]
    migrate_cursor_roles(state)
    refresh = refresh_cursor_bindings(state)
    if refresh.get("cleared_editor") or refresh.get("cleared_agent"):
        write_state(state)
    obs_rep = binding_report(state.get("obsidian"))
    cur_rep = binding_report(state.get("cursor_editor") or state.get("cursor"))
    obs_live = bool(obs_rep.get("ok"))
    cur_live = bool(cur_rep.get("ok")) and bool(refresh.get("editor_ok"))

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
        "context_follow": {
            "last_path": _CONTEXT_FOLLOW_GATE.last_relative_path or _CONTEXT_FOLLOW_GATE.last_path,
            "last_sync_at": _CONTEXT_FOLLOW_GATE.last_sync_at or None,
        },
        "context_sync": _get_context_sync().status(),
        "embedded": build_embedded_status(state),
        "cursor_editor": {
            "ok": bool(editor_binding_ok(state).get("ok")),
            "hwnd": editor_hwnd(state) or None,
        },
        "cursor_agent": {
            "ok": bool(agent_binding_ok(state).get("ok")),
            "hwnd": agent_hwnd(state) or None,
            "bound": bool(agent_binding_ok(state).get("ok")),
        },
        "bindings_refresh": refresh,
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


def build_runtime_info(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Canonical runtime identity — never includes token content."""
    paths = get_paths()
    cfg = cfg or {}
    host = str(cfg.get("daemon_host") or "127.0.0.1")
    port = int(cfg.get("daemon_port") or 27845)
    daemon_running = False
    try:
        meta_path = paths.daemon_meta_path
        if meta_path.is_file():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            pid = int(meta.get("pid") or 0)
            if pid and psutil.pid_exists(pid):
                daemon_running = True
    except Exception:
        daemon_running = False
    return {
        "ok": True,
        "version": VERSION,
        "runtime_mode": runtime_mode(),
        "binary_path": str(paths.helper_executable),
        "code_dir": str(paths.code_dir),
        "data_dir": str(paths.data_dir),
        "config_path": str(paths.config_path),
        "state_path": str(paths.state_path),
        "daemon_meta_path": str(paths.daemon_meta_path),
        "daemon_token_path": str(paths.daemon_token_path),
        "daemon_pid_path": str(paths.daemon_pid_path),
        "daemon_running": daemon_running,
        "daemon_host": host,
        "daemon_port": port,
        "cursor_exe_verified": cfg.get("cursor_exe_verified"),
    }


def cmd_runtime_info(cfg: dict[str, Any], as_json: bool = True, **_: Any) -> int:
    payload = build_runtime_info(cfg)
    if as_json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        for k, v in payload.items():
            print(f"  {k}: {v}")
    return 0


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


# ---- Agents Pane (visual embed of Agents Window; editor untouched) --------


def _obsidian_hwnd(state: dict[str, Any]) -> int:
    try:
        return int((state.get("obsidian") or {}).get("hwnd") or 0)
    except (TypeError, ValueError):
        return 0


def build_embedded_status(state: dict[str, Any] | None = None) -> dict[str, Any]:
    st = state if state is not None else read_state()
    migrate_cursor_roles(st)
    refresh = refresh_cursor_bindings(st)
    if refresh.get("cleared_editor") or refresh.get("cleared_agent"):
        write_state(st)
    ag = get_agent_binding(st)
    oh = _obsidian_hwnd(st)
    ah = agent_hwnd(st)
    claimed_backend = st.get("embed_backend") or "visual"
    claimed_native = bool(st.get("native_child"))
    agent_live_ok = bool(refresh.get("agent_ok"))

    live: dict[str, Any] = {}
    verified = False
    if ah and oh:
        try:
            live = probe_hwnd_win32(ah, expected_parent=oh)
            verified = bool(live.get("is_native_child_verified"))
        except Exception as exc:
            live = {"ok": False, "error": str(exc)}

    # Hard rule: native_child=true ONLY when Win32 verifies
    out: dict[str, Any] = {
        "mode": st.get("embed_mode") or "sidecar",
        "embedded": bool(st.get("embedded")) and agent_live_ok,
        "embed_backend": claimed_backend,
        "backend": claimed_backend,
        "native_child": bool(verified),  # live only
        "native_child_claimed": claimed_native,
        "is_native_child": bool(verified),
        "target": "agent",
        "agent_bound": agent_live_ok,  # live validate only
        "obsidian_hwnd": oh or None,
        "agent_hwnd": ah or None,
        "editor_hwnd": editor_hwnd(st) or None,
        "agent_parent_hwnd": live.get("parent_hwnd"),
        "expected_parent_hwnd": oh or None,
        "WS_CHILD": live.get("WS_CHILD"),
        "WS_POPUP": live.get("WS_POPUP"),
        "WS_CAPTION": live.get("WS_CAPTION"),
        "WS_THICKFRAME": live.get("WS_THICKFRAME"),
        "agent_style": live.get("style"),
        "style_after": live.get("style"),
        "refresh": refresh,
        "obsidian_dpi": None,
        "agent_dpi": live.get("dpi"),
        "visible": None,
        "dom_rect": st.get("last_pane_dom"),
        "screen_rect": None,
        "client_rect": None,
        "scale_x": None,
        "scale_y": None,
        "agent_stale_reason": st.get("agent_stale_reason"),
        "win32": live,
    }
    try:
        if oh:
            from native_embed import read_dpi

            out["obsidian_dpi"] = read_dpi(oh)
    except Exception:
        pass
    if _FOLLOW is not None and isinstance(_FOLLOW.last_embed_apply, dict):
        mapped = _FOLLOW.last_embed_apply
        out["visible"] = mapped.get("visible")
        out["screen_rect"] = mapped.get("screen_rect")
        out["client_rect"] = mapped.get("client_rect")
        out["scale_x"] = mapped.get("scale_x")
        out["scale_y"] = mapped.get("scale_y")
        if mapped.get("dom_rect"):
            out["dom_rect"] = mapped.get("dom_rect")
    return out


def begin_bind_agents_window(cfg: dict[str, Any]) -> dict[str, Any]:
    """Snapshot current Cursor HWNDs before user opens New Agents Window."""
    proc = str(cfg.get("cursor_process") or "Cursor.exe")
    with LIFECYCLE_LOCK:
        state = read_state()
        before = list_cursor_top_level(proc)
        state["agent_bind_before"] = before
        state["timestamp"] = time.time()
        write_state(state)
        return {
            "ok": True,
            "cmd": "begin-bind-agents-window",
            "before_count": len(before),
            "hint": "Open Cursor → File → New Agents Window, then run complete-bind-agents-window",
        }


def complete_bind_agents_window(cfg: dict[str, Any]) -> dict[str, Any]:
    """Diff HWND set; bind exactly one new Cursor top-level as agent."""
    proc = str(cfg.get("cursor_process") or "Cursor.exe")
    with LIFECYCLE_LOCK:
        state = read_state()
        migrate_cursor_roles(state)
        before = state.get("agent_bind_before")
        if not isinstance(before, list):
            return {
                "ok": False,
                "error": "bind_not_started",
                "hint": "Run begin-bind-agents-window first",
            }
        after = list_cursor_top_level(proc)
        # Exclude editor HWND from "new" candidates if it somehow appears
        eh = editor_hwnd(state)
        newcomers = [
            w for w in diff_new_hwnds(before, after) if int(w.get("hwnd") or 0) != eh
        ]
        if len(newcomers) == 0:
            return {"ok": False, "error": "agent_window_not_found", "after_count": len(after)}
        if len(newcomers) > 1:
            return {
                "ok": False,
                "error": "agent_window_ambiguous",
                "candidates": newcomers,
            }
        hwnd = int(newcomers[0]["hwnd"])
        snap = bind_agent_from_hwnd(hwnd, process_name=proc)
        if not snap:
            return {"ok": False, "error": "agent_snapshot_failed"}
        state["cursor_agent"] = snap
        state["agent_bind_before"] = None
        state["agent_stale_reason"] = None
        state["timestamp"] = time.time()
        write_state(state)
        return {
            "ok": True,
            "cmd": "complete-bind-agents-window",
            "agent": {"hwnd": snap["hwnd"], "pid": snap["pid"], "title": snap.get("title")},
            "embedded_status": build_embedded_status(state),
        }


def select_agents_window(cfg: dict[str, Any], *, confirm: bool = False, hwnd: int | None = None) -> dict[str, Any]:
    """Bind an already-open Agents window (excludes editor). No silent auto-bind."""
    proc = str(cfg.get("cursor_process") or "Cursor.exe")
    with LIFECYCLE_LOCK:
        state = read_state()
        migrate_cursor_roles(state)
        eh = editor_hwnd(state)
        windows = list_cursor_top_level(proc)
        cands = candidates_excluding_editor(windows, eh)
        if hwnd is not None:
            hwnd_i = int(hwnd)
            match = [w for w in cands if int(w.get("hwnd") or 0) == hwnd_i]
            if not match:
                return {"ok": False, "error": "hwnd_not_candidate", "candidates": cands}
            if not confirm:
                return {
                    "ok": False,
                    "error": "confirm_required",
                    "candidate": match[0],
                    "hint": "Pass confirm=true to bind this HWND",
                }
            snap = bind_agent_from_hwnd(hwnd_i, process_name=proc)
            if not snap:
                return {"ok": False, "error": "agent_snapshot_failed"}
            state["cursor_agent"] = snap
            state["agent_stale_reason"] = None
            write_state(state)
            return {
                "ok": True,
                "cmd": "select-agents-window",
                "agent": {"hwnd": snap["hwnd"], "pid": snap["pid"], "title": snap.get("title")},
            }
        if len(cands) == 0:
            return {"ok": False, "error": "agent_window_not_found", "candidates": []}
        if len(cands) > 1:
            return {"ok": False, "error": "agent_window_ambiguous", "candidates": cands}
        if not confirm:
            return {
                "ok": False,
                "error": "confirm_required",
                "candidate": cands[0],
                "hint": "Pass confirm=true to bind the sole non-editor Cursor window",
            }
        snap = bind_agent_from_hwnd(int(cands[0]["hwnd"]), process_name=proc)
        if not snap:
            return {"ok": False, "error": "agent_snapshot_failed"}
        state["cursor_agent"] = snap
        state["agent_stale_reason"] = None
        write_state(state)
        return {
            "ok": True,
            "cmd": "select-agents-window",
            "agent": {"hwnd": snap["hwnd"], "pid": snap["pid"], "title": snap.get("title")},
        }


def enter_embedded_pane(cfg: dict[str, Any], pane: dict[str, Any]) -> dict[str, Any]:
    """Enter Agents Pane mode. Moves agent_hwnd only. Requires Attach + agent bind."""
    with LIFECYCLE_LOCK:
        truth = refresh_attachment_truth()
        if not truth["attached"]:
            return {"ok": False, "error": "sidecar_not_attached"}
        state = truth["state"]
        migrate_cursor_roles(state)
        refresh_cursor_bindings(state)
        write_state(state)
        obs_hwnd = _obsidian_hwnd(state)
        if not obs_hwnd or not validate_window_binding(state.get("obsidian")).get("ok"):
            return {"ok": False, "error": "stale_binding", "which": "obsidian"}
        # Editor must remain valid for Context Follow — but we do NOT move it
        ed_ok = editor_binding_ok(state)
        if not ed_ok.get("ok"):
            return {"ok": False, "error": "stale_binding", "which": "editor"}

        ag_ok = agent_binding_ok(state)
        backend = str(
            (pane.get("backend") if isinstance(pane, dict) else None)
            or state.get("embed_backend")
            or cfg.get("embed_backend")
            or "visual"
        ).strip().lower()
        use_native = backend in ("native_child", "native")

        auto_bind: dict[str, Any] | None = None
        if not ag_ok.get("ok"):
            if use_native:
                auto_bind = ensure_agents_window_bound(cfg, state)
                if not auto_bind.get("ok"):
                    write_state(state)
                    return {
                        "ok": False,
                        "error": auto_bind.get("error") or "agent_window_not_found",
                        "auto_bind": auto_bind,
                        "visual_fallback": False,
                        "hint": "Could not open/bind Agents Window automatically",
                        "embedded_status": build_embedded_status(state),
                    }
                write_state(state)
            else:
                return {
                    "ok": False,
                    "error": "agent_not_bound",
                    "reason": ag_ok.get("reason"),
                    "hint": "Open Cursor → File → New Agents Window, then Bind Agents Window",
                }
        ah = agent_hwnd(state)
        if not ah:
            return {
                "ok": False,
                "error": "agent_window_not_found" if use_native else "agent_not_bound",
                "visual_fallback": False,
            }

        dom = DomPaneRect.from_dict(pane)
        if dom is None:
            return {"ok": False, "error": "invalid_pane"}
        pane_dict = dom.to_dict()
        if isinstance(pane, dict):
            for k in ("backend", "chrome_level", "borderless"):
                if k in pane:
                    pane_dict[k] = pane[k]

        if use_native:
            result = _enter_native_agents_embed(cfg, state, obs_hwnd, ah, pane_dict)
            if auto_bind:
                result = dict(result)
                result["auto_bind"] = auto_bind
            return result

        if state.get("native_child"):
            _force_exit_native_child(state)

        if not state.get("embedded"):
            # Embedded temporary layer on AGENT only — never touch editor chrome
            state["embedded_snapshot"] = snapshot_window_chrome(ah)
            state["agent_runtime_rect"] = list(get_window_rect(ah).as_tuple())

        borderless = bool(cfg.get("embed_borderless")) or bool(pane.get("borderless"))
        if borderless:
            apply_borderless(ah)

        # Owned-window only for visual backend (never mix with SetParent)
        if bool(cfg.get("experimental_owned_window")):
            set_owner_experimental(ah, obs_hwnd)

        state["embed_mode"] = "pane"
        state["embedded"] = True
        state["embed_backend"] = "visual"
        state["native_child"] = False
        state["embed_target"] = "agent"
        state["last_pane_dom"] = pane_dict
        state["timestamp"] = time.time()
        write_state(state)

        if _FOLLOW:
            _FOLLOW.set_follow_mode("pane")
            _FOLLOW.set_last_pane_dom(pane_dict)
            _FOLLOW.set_agent_hwnd(ah)
            if hasattr(_FOLLOW, "set_embed_backend"):
                _FOLLOW.set_embed_backend("visual")

        mapped = apply_embedded_pane(
            obsidian_hwnd=obs_hwnd,
            cursor_hwnd=ah,
            pane=pane_dict,
        )
        if _FOLLOW:
            _FOLLOW.last_embed_apply = mapped
        return {
            "ok": bool(mapped.get("ok")),
            "cmd": "enter-embedded-pane",
            "embedded": True,
            "embed_mode": "pane",
            "embed_backend": "visual",
            "embed_target": "agent",
            "agent_hwnd": ah,
            "editor_hwnd": editor_hwnd(state),
            "placement": mapped,
            "embedded_status": build_embedded_status(state),
            "error": mapped.get("error"),
        }


def open_native_agents_pane(cfg: dict[str, Any], pane: dict[str, Any]) -> dict[str, Any]:
    """One-shot UX: auto-bind Agents Window + enter native_child. No visual fallback."""
    pane = dict(pane or {})
    pane["backend"] = "native_child"
    result = enter_embedded_pane(cfg, pane)
    out = dict(result) if isinstance(result, dict) else {"ok": False, "error": "enter_failed"}
    out["cmd"] = "open-native-agents-pane"
    out["visual_fallback"] = False
    return out


def _force_exit_native_child(state: dict[str, Any]) -> dict[str, Any]:
    ah = agent_hwnd(state)
    snap = state.get("native_restore_snapshot")
    result: dict[str, Any] = {"ok": True, "noop": not state.get("native_child")}
    if ah and state.get("native_child"):
        result = exit_native_child(agent_hwnd=ah, snapshot=snap if isinstance(snap, dict) else None)
    state["native_child"] = False
    state["native_restore_snapshot"] = None
    state["native_parent_hwnd"] = None
    state["native_dpi"] = None
    return result


def _enter_native_agents_embed(
    cfg: dict[str, Any],
    state: dict[str, Any],
    obs_hwnd: int,
    ah: int,
    pane_dict: dict[str, Any],
) -> dict[str, Any]:
    del cfg

    def _persist(snap: dict[str, Any]) -> None:
        # Persist recovery snapshot BEFORE SetParent; do NOT claim success yet
        state["native_restore_snapshot"] = snap
        state["native_child_pending"] = True
        state["embed_backend"] = "native_child"
        state["last_pane_dom"] = pane_dict
        state["timestamp"] = time.time()
        write_state(state)

    result = enter_native_child(
        agent_hwnd=ah,
        obsidian_hwnd=obs_hwnd,
        pane=pane_dict,
        persist_snapshot=_persist,
    )
    if not result.get("ok") or not result.get("is_native_child_verified"):
        # Clear pending claim; keep snapshot for recover if half-applied
        state["native_child"] = False
        state["native_child_pending"] = False
        state["embedded"] = False
        state["embed_mode"] = "sidecar"
        write_state(state)
        return {
            "ok": False,
            "cmd": "enter-native-agents-embed",
            "error": result.get("error") or "native_child_enter_failed",
            "win32_error": result.get("win32_error"),
            "placement": result,
            "embedded_status": build_embedded_status(state),
            # Explicit: never fall back to visual
            "visual_fallback": False,
        }

    state["native_child"] = True
    state["native_child_pending"] = False
    state["embed_backend"] = "native_child"
    state["embed_mode"] = "pane"
    state["embedded"] = True
    state["embed_target"] = "agent"
    state["native_parent_hwnd"] = result.get("parent_hwnd")
    state["native_dpi"] = result.get("dpi")
    state["last_pane_dom"] = pane_dict
    state["timestamp"] = time.time()
    write_state(state)

    if _FOLLOW:
        _FOLLOW.set_follow_mode("pane")
        _FOLLOW.set_last_pane_dom(pane_dict)
        _FOLLOW.set_agent_hwnd(ah)
        if hasattr(_FOLLOW, "set_embed_backend"):
            _FOLLOW.set_embed_backend("native_child")
        _FOLLOW.last_embed_apply = result

    return {
        "ok": True,
        "cmd": "enter-native-agents-embed",
        "embedded": True,
        "embed_mode": "pane",
        "embed_backend": "native_child",
        "is_native_child_verified": True,
        "embed_target": "agent",
        "agent_hwnd": ah,
        "obsidian_hwnd": obs_hwnd,
        "agent_parent_hwnd": result.get("parent_hwnd"),
        "style": result.get("style"),
        "style_after": result.get("style_after") or result.get("style"),
        "WS_CHILD": True,
        "WS_POPUP": False,
        "WS_CAPTION": result.get("WS_CAPTION"),
        "WS_THICKFRAME": result.get("WS_THICKFRAME"),
        "chrome": result.get("chrome"),
        "win32_error": result.get("win32_error"),
        "editor_hwnd": editor_hwnd(state),
        "placement": result,
        "embedded_status": build_embedded_status(state),
    }


def update_embedded_pane(cfg: dict[str, Any], pane: dict[str, Any]) -> dict[str, Any]:
    """Update cached DOM pane rect; reposition agent_hwnd only."""
    del cfg
    with LIFECYCLE_LOCK:
        truth = refresh_attachment_truth()
        state = truth["state"]
        migrate_cursor_roles(state)
        if not state.get("embedded") or str(state.get("embed_mode") or "") != "pane":
            return {
                "ok": True,
                "cmd": "update-embedded-pane",
                "ignored": True,
                "reason": "embed_mode_off",
            }
        if not truth["attached"]:
            return {"ok": False, "error": "sidecar_not_attached"}
        obs_hwnd = _obsidian_hwnd(state)
        if not obs_hwnd or not validate_window_binding(state.get("obsidian")).get("ok"):
            return {"ok": False, "error": "stale_binding", "which": "obsidian"}

        ag_ok = agent_binding_ok(state)
        if not ag_ok.get("ok"):
            # Agent closed — leave embed, do NOT bind editor
            if state.get("native_child"):
                _force_exit_native_child(state)
            clear_agent_binding(state, reason=str(ag_ok.get("reason") or "stale_agent"))
            state["embedded"] = False
            state["embed_mode"] = "sidecar"
            state["embedded_snapshot"] = None
            state["last_pane_dom"] = None
            write_state(state)
            if _FOLLOW:
                _FOLLOW.set_follow_mode("sidecar")
                _FOLLOW.set_agent_hwnd(0)
                _FOLLOW.set_last_pane_dom(None)
            return {
                "ok": False,
                "error": "agent_window_closed",
                "reason": ag_ok.get("reason"),
                "embedded": False,
            }

        ah = agent_hwnd(state)
        dom = DomPaneRect.from_dict(pane)
        if dom is None:
            return {"ok": False, "error": "invalid_pane"}
        pane_dict = dom.to_dict()
        state["last_pane_dom"] = pane_dict
        state["timestamp"] = time.time()
        write_state(state)

        backend = "native_child" if state.get("native_child") else "visual"
        if _FOLLOW:
            _FOLLOW.set_follow_mode("pane")
            _FOLLOW.set_last_pane_dom(pane_dict)
            _FOLLOW.set_agent_hwnd(ah)
            if hasattr(_FOLLOW, "set_embed_backend"):
                _FOLLOW.set_embed_backend(backend)

        if backend == "native_child":
            mapped = update_native_child(
                agent_hwnd=ah, obsidian_hwnd=obs_hwnd, pane=pane_dict
            )
        else:
            mapped = apply_embedded_pane(
                obsidian_hwnd=obs_hwnd,
                cursor_hwnd=ah,
                pane=pane_dict,
            )
        if _FOLLOW:
            _FOLLOW.last_embed_apply = mapped
        return {
            "ok": bool(mapped.get("ok")),
            "cmd": "update-embedded-pane",
            "ignored": False,
            "embed_backend": backend,
            "embed_target": "agent",
            "placement": mapped,
            "embedded_status": build_embedded_status(state),
            "error": mapped.get("error"),
        }


def exit_embedded_pane(cfg: dict[str, Any]) -> dict[str, Any]:
    """Leave Agents Pane → restore agent (visual chrome or native SetParent reverse)."""
    del cfg
    with LIFECYCLE_LOCK:
        truth = refresh_attachment_truth()
        state = truth["state"]
        migrate_cursor_roles(state)
        if not state.get("embedded") and not state.get("native_child"):
            return {
                "ok": True,
                "cmd": "exit-embedded-pane",
                "embedded": False,
                "embed_mode": "sidecar",
                "noop": True,
            }
        native_result = None
        if state.get("native_child"):
            native_result = _force_exit_native_child(state)
        else:
            ah = agent_hwnd(state)
            if ah:
                restore_window_chrome(ah, state.get("embedded_snapshot"))

        state["embedded"] = False
        state["embed_mode"] = "sidecar"
        state["embed_target"] = None
        state["embedded_snapshot"] = None
        state["last_pane_dom"] = None
        state["native_child"] = False
        state["timestamp"] = time.time()
        write_state(state)

        if _FOLLOW:
            _FOLLOW.set_follow_mode("sidecar")
            _FOLLOW.set_last_pane_dom(None)
            _FOLLOW.set_agent_hwnd(0)
            _FOLLOW.last_embed_apply = None
            if hasattr(_FOLLOW, "set_embed_backend"):
                _FOLLOW.set_embed_backend("visual")

        return {
            "ok": True,
            "cmd": "exit-embedded-pane",
            "embedded": False,
            "embed_mode": "sidecar",
            "arranged": False,
            "attached": truth["attached"],
            "editor_untouched": True,
            "native_restore": native_result,
            "embedded_status": build_embedded_status(state),
        }


def recover_native_agents_window(cfg: dict[str, Any]) -> dict[str, Any]:
    del cfg
    with LIFECYCLE_LOCK:
        state = read_state()
        snap = state.get("native_restore_snapshot")
        result = recover_native_child(snap if isinstance(snap, dict) else None)
        state["native_child"] = False
        state["embedded"] = False
        state["embed_mode"] = "sidecar"
        state["native_restore_snapshot"] = None
        state["native_parent_hwnd"] = None
        state["timestamp"] = time.time()
        write_state(state)
        if _FOLLOW:
            _FOLLOW.set_follow_mode("sidecar")
            _FOLLOW.set_agent_hwnd(0)
            if hasattr(_FOLLOW, "set_embed_backend"):
                _FOLLOW.set_embed_backend("visual")
        return {
            "ok": bool(result.get("ok")),
            "cmd": "recover-native-child",
            "result": result,
            "embedded_status": build_embedded_status(state),
        }


def set_embed_backend(cfg: dict[str, Any], backend: str) -> dict[str, Any]:
    b = "native_child" if str(backend).strip().lower() in ("native_child", "native") else "visual"
    with LIFECYCLE_LOCK:
        cfg["embed_backend"] = b
        try:
            update_config_file({"embed_backend": b})
        except OSError:
            pass
        state = read_state()
        if state.get("native_child") and b == "visual":
            return {"ok": False, "error": "exit_native_first"}
        if state.get("embedded") and not state.get("native_child") and b == "native_child":
            return {"ok": False, "error": "exit_visual_first"}
        state["embed_backend"] = b
        write_state(state)
        return {"ok": True, "cmd": "set-embed-backend", "embed_backend": b}


def focus_agents_window(cfg: dict[str, Any]) -> dict[str, Any]:
    """Focus bound Agents Window only (never editor)."""
    del cfg
    with LIFECYCLE_LOCK:
        state = read_state()
        migrate_cursor_roles(state)
        ag_ok = agent_binding_ok(state)
        if not ag_ok.get("ok"):
            return {"ok": False, "error": "agent_not_bound", "reason": ag_ok.get("reason")}
        ah = agent_hwnd(state)
        try:
            focus_window(ah)
            return {"ok": True, "cmd": "focus-agents-window", "agent_hwnd": ah}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}


def cmd_stop(cfg: dict[str, Any], **_: Any) -> int:
    del cfg
    stop_live_follow()
    meta = read_daemon_meta()
    if meta:
        how = stop_existing_daemon_safe(meta)
        print(f"[sidecar] daemon: {how}")
    stopped = False
    for label, path in (("daemon", DAEMON_PID_FILE), ("follow", PID_FILE)):
        pid = read_pid(path)
        if not pid:
            continue
        # Daemon already handled via metadata when possible
        if label == "daemon" and meta:
            clear_pid(path)
            stopped = True
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            print(f"[sidecar] stopped {label} pid={pid}")
            stopped = True
        except OSError as exc:
            print(f"[sidecar] could not stop {label}: {exc}")
        clear_pid(path)
    clear_daemon_meta()
    if not stopped and not meta:
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
        elif cmd in ("open-editor-file", "open_editor_file"):
            line = body.get("line")
            column = body.get("column")
            return open_editor_file_result(
                cfg,
                vault_root=body.get("vault_root") or workspace,
                path=body.get("path") or file_path,
                line=int(line) if line is not None else None,
                column=int(column) if column is not None else None,
                focus=bool(body.get("focus", True)),
            )
        elif cmd in ("sync-editor-file", "sync_editor_file"):
            line = body.get("line")
            column = body.get("column")
            seq_raw = body.get("seq")
            seq = int(seq_raw) if seq_raw is not None else None
            return sync_editor_file_result(
                cfg,
                vault_root=body.get("vault_root") or workspace,
                path=body.get("path") or file_path,
                line=int(line) if line is not None else None,
                column=int(column) if column is not None else None,
                relative_path=body.get("relative_path") or body.get("rel_path"),
                seq=seq,
            )
        elif cmd in ("context-sync-clear", "context_sync_clear", "clear-context-sync"):
            return clear_context_sync()
        elif cmd in ("open-editor-vault", "open_editor_vault"):
            return open_editor_vault_result(
                cfg,
                path=body.get("path") or workspace or file_path,
                focus=bool(body.get("focus", True)),
            )
        elif cmd in ("shutdown-daemon", "shutdown_daemon"):
            if not _DAEMON_MODE:
                return {"ok": False, "cmd": cmd, "error": "not_daemon_process"}
            # Respond first; shutdown asynchronously so the HTTP response can flush.
            threading.Thread(
                target=request_daemon_shutdown,
                name="sidecar-shutdown-rpc",
                daemon=True,
            ).start()
            return {"ok": True, "cmd": cmd, "shutting_down": True}
        elif cmd in ("focus-agents-window", "focus_agents_window"):
            return focus_agents_window(cfg)
        elif cmd in ("begin-bind-agents-window", "begin_bind_agents_window"):
            return begin_bind_agents_window(cfg)
        elif cmd in ("complete-bind-agents-window", "complete_bind_agents_window"):
            return complete_bind_agents_window(cfg)
        elif cmd in ("select-agents-window", "select_agents_window", "bind-agents-window"):
            # bind-agents-window alias: if no before snapshot, treat as select with confirm
            if "confirm" in body or body.get("hwnd") is not None:
                return select_agents_window(
                    cfg,
                    confirm=bool(body.get("confirm")),
                    hwnd=int(body["hwnd"]) if body.get("hwnd") is not None else None,
                )
            # two-step: begin if no snapshot, else complete
            st = read_state()
            if isinstance(st.get("agent_bind_before"), list):
                return complete_bind_agents_window(cfg)
            return begin_bind_agents_window(cfg)
        elif cmd in (
            "enter-embedded-pane",
            "enter_embedded_pane",
            "enter-agents-pane",
            "enter-native-agents-embed",
            "open-native-agents-pane",
            "open_native_agents_pane",
        ):
            pane = body.get("pane")
            if not isinstance(pane, dict):
                return {"ok": False, "error": "pane required"}
            if cmd in (
                "enter-native-agents-embed",
                "open-native-agents-pane",
                "open_native_agents_pane",
            ) or body.get("backend") == "native_child":
                pane = dict(pane)
                pane["backend"] = "native_child"
            if cmd in ("open-native-agents-pane", "open_native_agents_pane"):
                return open_native_agents_pane(cfg, pane)
            return enter_embedded_pane(cfg, pane)
        elif cmd in ("update-embedded-pane", "update_embedded_pane", "update-native-child-pane"):
            pane = body.get("pane")
            if not isinstance(pane, dict):
                return {"ok": False, "error": "pane required"}
            return update_embedded_pane(cfg, pane)
        elif cmd in (
            "exit-embedded-pane",
            "exit_embedded_pane",
            "exit-agents-pane",
            "exit-native-agents-embed",
        ):
            return exit_embedded_pane(cfg)
        elif cmd in ("recover-native-child", "recover_native_child", "recover-native-agents-window"):
            return recover_native_agents_window(cfg)
        elif cmd in ("set-embed-backend", "set_embed_backend"):
            if "backend" not in body and "embed_backend" not in body:
                return {"ok": False, "error": "backend required"}
            return set_embed_backend(cfg, str(body.get("backend") or body.get("embed_backend")))
        elif cmd in ("set-cursor-exe", "set_cursor_exe"):
            path = str(body.get("path") or body.get("cursor_exe") or "").strip()
            if not path:
                return {"ok": False, "error": "path required"}
            update_config_file({"cursor_exe_verified": path, "cursor_exe_candidates": [path]})
            cfg["cursor_exe_verified"] = path
            return {"ok": True, "cmd": "set-cursor-exe", "path": path}
        elif cmd in ("runtime-info", "runtime_info"):
            return build_runtime_info(cfg)
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
            "embedded": build_embedded_status(),
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "cmd": cmd, "error": str(exc)}


def run_daemon(cfg: dict[str, Any]) -> int:
    global _DAEMON_MODE, _DAEMON_HTTP_SERVER
    _DAEMON_MODE = True
    if cfg.get("debug"):
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    host, port = cfg["daemon_host"], cfg["daemon_port"]
    expected_token = ensure_daemon_token()
    print(f"[sidecar] daemon http://{host}:{port} (token auth; live_follow={cfg.get('live_follow')})")

    # Crash recovery: agent left as Obsidian child
    st0 = read_state()
    if st0.get("native_child") and st0.get("native_restore_snapshot"):
        print("[sidecar] recovering native_child leftover from previous session…")
        try:
            recover_native_agents_window(cfg)
        except Exception as exc:
            print(f"[sidecar] native recover failed: {exc}")

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
    _DAEMON_HTTP_SERVER = server
    write_pid(os.getpid(), DAEMON_PID_FILE)
    write_daemon_meta(pid=os.getpid(), host=host, port=port)
    http_thread = threading.Thread(target=server.serve_forever, name="sidecar-http", daemon=True)
    http_thread.start()
    _STOP_DAEMON.clear()

    def _stop(_sig: int, _frame: object) -> None:
        print("\n[sidecar] daemon stopping")
        request_daemon_shutdown()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    try:
        while not _STOP_DAEMON.is_set():
            time.sleep(0.25)
    finally:
        stop_live_follow()
        try:
            server.shutdown()
        except Exception:
            pass
        clear_pid(DAEMON_PID_FILE)
        clear_daemon_meta()
        _DAEMON_HTTP_SERVER = None
        _DAEMON_MODE = False
    return 0


def cmd_daemon(cfg: dict[str, Any], **_: Any) -> int:
    return run_daemon(cfg)


def cmd_daemon_start(cfg: dict[str, Any], **_: Any) -> int:
    ensure_daemon_token()  # create token file before spawn
    host = normalize_daemon_bind_host(str(cfg["daemon_host"]))
    port = int(cfg["daemon_port"])

    meta = read_daemon_meta()
    if not meta:
        # Legacy: PID file only
        existing = read_pid(DAEMON_PID_FILE)
        if existing:
            meta = {"pid": existing, "process_create_time": 0.0, "host": "", "port": 0}

    if meta:
        check = verify_sidecar_daemon_process(meta)
        if check.get("ok"):
            same_ep = (
                normalize_daemon_bind_host(str(check.get("host") or meta.get("host") or ""))
                == host
                and int(check.get("port") or meta.get("port") or 0) == port
            )
            if same_ep:
                print(f"[sidecar] daemon already running at requested endpoint http://{host}:{port}")
                # Refresh meta if legacy pid-only
                write_daemon_meta(
                    pid=int(check["pid"]),
                    host=host,
                    port=port,
                    process_create_time=float(check.get("process_create_time") or 0.0) or None,
                )
                write_pid(int(check["pid"]), DAEMON_PID_FILE)
                return 0
            print(
                f"[sidecar] daemon endpoint mismatch "
                f"(running {check.get('host')}:{check.get('port')} → requested {host}:{port}); migrating"
            )
            how = stop_existing_daemon_safe(meta)
            print(f"[sidecar] old daemon: {how}")
        else:
            # Stale / PID reused by unrelated process — never kill; just clear our files
            print(f"[sidecar] clearing stale daemon metadata ({check.get('reason')})")
            clear_pid(DAEMON_PID_FILE)
            clear_daemon_meta()

    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    argv = build_self_command(
        "--data-dir",
        str(DATA_DIR),
        "-c",
        str(DEFAULT_CONFIG),
        "daemon",
        *daemon_endpoint_args(host, port),
    )
    subprocess.Popen(
        argv,
        cwd=str(DATA_DIR if is_frozen() else CODE_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
        close_fds=True,
    )
    # Wait briefly for child to write meta
    for _ in range(20):
        time.sleep(0.15)
        m = read_daemon_meta()
        if m and int(m.get("port") or 0) == port:
            print(f"[sidecar] daemon-start http://{host}:{port} (auth token file ready)")
            return 0
    print(f"[sidecar] daemon-start http://{host}:{port} (auth token file ready; meta pending)")
    return 0


def _peek_data_dir(argv: list[str]) -> str | None:
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--data-dir" and i + 1 < len(argv):
            return argv[i + 1]
        if tok.startswith("--data-dir="):
            return tok.split("=", 1)[1]
        i += 1
    return None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cursor-sidecar", description="Cursor Sidecar v0.6 Context Follow")
    p.add_argument("-c", "--config", default=str(DEFAULT_CONFIG))
    p.add_argument("--data-dir", default=None, help="Override writable DATA_DIR")
    p.add_argument("--version", action="store_true", help="Print helper version and exit")
    p.add_argument("--workspace", default=None)
    p.add_argument("--file", default=None)
    sub = p.add_subparsers(dest="command", required=False)

    sub.add_parser("version", help="Print helper version")
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
    oef = sub.add_parser("open-editor-file", help="Context Bridge: open vault file in bound Cursor Editor")
    oef.add_argument("--path", required=True)
    oef.add_argument("--vault-root", required=True)
    oef.add_argument("--line", type=int, default=None)
    oef.add_argument("--column", type=int, default=None)
    oef.add_argument("--no-focus", action="store_true")
    sef = sub.add_parser("sync-editor-file", help="Context Follow: silent open (never steals focus intentionally)")
    sef.add_argument("--path", required=True)
    sef.add_argument("--vault-root", required=True)
    sef.add_argument("--line", type=int, default=None)
    sef.add_argument("--column", type=int, default=None)
    sef.add_argument("--relative-path", default=None)
    oev = sub.add_parser("open-editor-vault", help="Context Bridge: open vault folder in bound Cursor Editor")
    oev.add_argument("--path", required=True)
    oev.add_argument("--no-focus", action="store_true")
    status_p = sub.add_parser("status", help="Attachment + window status")
    status_p.add_argument("--json", action="store_true")
    ri = sub.add_parser("runtime-info", help="Print canonical runtime paths (no token)")
    ri.add_argument("--json", action="store_true", default=True)
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
    raw = list(argv) if argv is not None else sys.argv[1:]
    data_override = _peek_data_dir(raw)
    if data_override:
        sync_path_globals(data_override)

    dpi = enable_dpi_awareness()
    parser = build_parser()
    args = parser.parse_args(raw)
    if getattr(args, "data_dir", None):
        sync_path_globals(str(args.data_dir))

    if bool(getattr(args, "version", False)) or args.command == "version":
        print(VERSION)
        return 0
    if not args.command:
        parser.error("command required (or pass --version)")

    cfg = load_config(Path(args.config))
    common = {"workspace": args.workspace, "file_path": args.file}

    # Apply CLI host/port overrides before daemon commands (same endpoint as plugin)
    if getattr(args, "host", None):
        cfg["daemon_host"] = normalize_daemon_bind_host(str(args.host))
    if getattr(args, "port", None) is not None:
        cfg["daemon_port"] = int(args.port)

    aliases = {"dock": "attach", "undock": "detach", "restore": "detach"}
    command = aliases.get(args.command, args.command)

    if command == "runtime-info":
        return cmd_runtime_info(cfg, as_json=True)

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
    if command == "open-editor-file":
        return cmd_open_editor_file(
            cfg,
            vault_root=getattr(args, "vault_root", None),
            path=getattr(args, "path", None),
            line=getattr(args, "line", None),
            column=getattr(args, "column", None),
            focus=not bool(getattr(args, "no_focus", False)),
        )
    if command == "sync-editor-file":
        return cmd_sync_editor_file(
            cfg,
            vault_root=getattr(args, "vault_root", None),
            path=getattr(args, "path", None),
            line=getattr(args, "line", None),
            column=getattr(args, "column", None),
            relative_path=getattr(args, "relative_path", None),
        )
    if command == "open-editor-vault":
        return cmd_open_editor_vault(
            cfg,
            path=getattr(args, "path", None),
            focus=not bool(getattr(args, "no_focus", False)),
        )

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
