#!/usr/bin/env python3
"""Cursor Sidecar v0.2 — Attach/Detach lifecycle for real Cursor Desktop."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from geometry import (
    atomic_write_text,
    can_restore_bound_window,
    evaluate_attachment,
    normalize_ratios,
    recover_state_dict,
)
from window import (
    apply_window_placement,
    arrange_bound_windows,
    enable_dpi_awareness,
    find_cursor_window,
    find_obsidian_window,
    focus_window,
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

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config.json"
PID_FILE = ROOT / ".sidecar.pid"
DAEMON_PID_FILE = ROOT / ".sidecar.daemon.pid"
TOKEN_FILE = ROOT / ".sidecar.daemon.token"
STATE_FILE = ROOT / ".sidecar.state.json"

VERSION = "0.2.1"


def load_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"config not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        cfg = json.load(f)
    o, c = normalize_ratios(cfg.get("obsidian_ratio", 0.7), cfg.get("cursor_ratio", 0.3))
    cfg["obsidian_ratio"] = o
    cfg["cursor_ratio"] = c
    cfg["gap"] = int(cfg.get("gap", 0))
    cfg["monitor"] = cfg.get("monitor", None)
    cfg["poll_ms"] = int(cfg.get("poll_ms", 500))
    cfg["follow_obsidian"] = bool(cfg.get("follow_obsidian", False))
    cfg["launch_cursor_if_missing"] = bool(cfg.get("launch_cursor_if_missing", True))
    cfg["daemon_host"] = str(cfg.get("daemon_host", "127.0.0.1"))
    cfg["daemon_port"] = int(cfg.get("daemon_port", 27845))
    return cfg


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


def refresh_attachment_truth(state: dict[str, Any] | None = None) -> dict[str, Any]:
    """Recompute attached/state_valid; persist correction if stale."""
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
    """Attach Sidecar: save originals once, bind HWNDs, arrange 70/30. Idempotent."""
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

    # Reuse bound windows when still attached & valid
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
            state["timestamp"] = time.time()
            write_state(state)
            print("[sidecar] attach (idempotent rearrange)")
            return 0

    # Fresh attach — discover Cursor (prefer existing binding only if live)
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

    # Capture ORIGINAL layout only when transitioning into attached
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
    }
    write_state(new_state)
    print(
        f"[sidecar] attached\n"
        f"  Obsidian hwnd={obs.hwnd} pid={obs.pid}\n"
        f"  Cursor   hwnd={cur.hwnd} pid={cur.pid}\n"
        f"  L={left.as_tuple()} R={right.as_tuple()}"
    )
    return 0


def cmd_detach(cfg: dict[str, Any], **_: Any) -> int:
    """Detach Sidecar and restore original WindowPlacement for bound windows only."""
    del cfg  # process names unused — never rediscover by process
    truth = refresh_attachment_truth()
    state = truth["state"]
    if not state.get("obsidian") and not state.get("cursor"):
        print("[sidecar] no saved state; already detached")
        write_state({"attached": False, "timestamp": time.time(), "version": VERSION})
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
        """Restore only the exact bound HWND. Never soft-rediscover another window."""
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
    """Re-arrange while keeping originals; attaches if needed."""
    truth = refresh_attachment_truth()
    if not truth["attached"]:
        return cmd_attach(cfg)
    state = truth["state"]
    obs = resolve_bound_window(
        state.get("obsidian"),
        cfg["obsidian_process"],
        cfg.get("obsidian_title_hint", ""),
    )
    cur = resolve_bound_window(
        state.get("cursor"),
        cfg["cursor_process"],
        cfg.get("cursor_title_hint", ""),
        allow_hidden=True,
    )
    if not obs or not cur:
        return cmd_attach(cfg)
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
    state["timestamp"] = time.time()
    write_state(state)
    print(f"[sidecar] arranged L={left.as_tuple()} R={right.as_tuple()}")
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
        "binding_mode": binding_mode,
        "legacy_binding": legacy,
        "restore_safe": restore_safe,
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
    # Aliases from older plugin
    aliases = {"dock": "attach", "undock": "detach", "restore": "detach"}
    cmd = aliases.get(cmd, cmd)
    workspace = body.get("workspace")
    file_path = body.get("file") or body.get("file_path")
    try:
        if cmd == "attach":
            code = cmd_attach(cfg, workspace=workspace, file_path=file_path)
        elif cmd == "detach":
            code = cmd_detach(cfg)
        elif cmd == "click":
            code = cmd_click(cfg, workspace=workspace, file_path=file_path)
        elif cmd == "arrange":
            code = cmd_arrange(cfg)
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
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "cmd": cmd, "error": str(exc)}


def run_daemon(cfg: dict[str, Any]) -> int:
    host, port = cfg["daemon_host"], cfg["daemon_port"]
    expected_token = ensure_daemon_token()
    print(f"[sidecar] daemon http://{host}:{port} (token file present; auth required)")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            # Avoid logging Authorization headers / tokens
            print(f"[daemon] {self.command} {self.path} →")
            try:
                print(f"[daemon]   {fmt % args}")
            except Exception:
                pass

        def _authorized(self) -> bool:
            got = self.headers.get("X-Cursor-Sidecar-Token") or ""
            return bool(got) and got == expected_token

        def _send(self, code: int, payload: dict[str, Any]) -> None:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            # No Access-Control-Allow-Origin — localhost RPC is not for browsers
            self.end_headers()
            self.wfile.write(raw)

        def _forbid(self) -> None:
            self._send(403, {"ok": False, "error": "forbidden"})

        def do_OPTIONS(self) -> None:  # noqa: N802
            # Do not advertise CORS wildcard
            self.send_response(405)
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            if not self._authorized():
                self._forbid()
                return
            path = urlparse(self.path).path
            if path in ("/", "/health"):
                self._send(200, {"ok": True, "service": "cursor-sidecar", "version": VERSION})
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

    def _stop(_sig: int, _frame: object) -> None:
        clear_pid(DAEMON_PID_FILE)
        server.shutdown()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    try:
        server.serve_forever()
    finally:
        clear_pid(DAEMON_PID_FILE)
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
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
    subprocess.Popen(
        [sys.executable, str(ROOT / "main.py"), "-c", str(DEFAULT_CONFIG), "daemon"],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
        close_fds=True,
    )
    time.sleep(0.5)
    print(f"[sidecar] daemon-start port={cfg['daemon_port']} (auth token file ready)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cursor-sidecar", description="Cursor Sidecar v0.2 lifecycle")
    p.add_argument("-c", "--config", default=str(DEFAULT_CONFIG))
    p.add_argument("--workspace", default=None)
    p.add_argument("--file", default=None)
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("attach", help="Attach Sidecar (save originals + arrange)")
    sub.add_parser("detach", help="Detach and restore original window placements")
    sub.add_parser("click", help="Attach if detached, Detach if attached")
    sub.add_parser("arrange", help="Re-arrange bound windows")
    sub.add_parser("focus", help="Focus bound Cursor window")
    sub.add_parser("toggle", help="Show/hide Cursor (advanced)")
    sub.add_parser("show", help="Show Cursor")
    sub.add_parser("hide", help="Hide Cursor")
    sub.add_parser("open", help="Open --file/--workspace in Cursor Desktop")
    status_p = sub.add_parser("status", help="Attachment + window status")
    status_p.add_argument("--json", action="store_true")
    sub.add_parser("stop", help="Stop follow/daemon pids")
    sub.add_parser("daemon", help="Foreground HTTP helper (optional)")
    sub.add_parser("daemon-start", help="Background HTTP helper (optional)")

    # Back-compat aliases
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

    aliases = {"dock": "attach", "undock": "detach", "restore": "detach"}
    command = aliases.get(args.command, args.command)

    if command != "status":
        print(f"[sidecar] dpi={dpi}")

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
