#!/usr/bin/env python3
"""Cursor Sidecar — dock real Cursor Desktop beside Obsidian (Windows).

Commands:
  python main.py start     Launch Cursor if needed, arrange, optionally follow
  python main.py arrange   One-shot split layout
  python main.py stop      Stop follow loop (if running) / no-op for windows
  python main.py toggle    Show/hide Cursor window
  python main.py status    Print discovered windows
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

from window import (
    arrange_sidecar,
    find_cursor_window,
    find_obsidian_window,
    get_window_rect,
    is_process_running,
    launch_process,
    resolve_cursor_exe,
    toggle_cursor_visibility,
    wait_for_window,
)

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config.json"
PID_FILE = ROOT / ".sidecar.pid"
STATE_FILE = ROOT / ".sidecar.state.json"


def load_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"config not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        cfg = json.load(f)
    # Normalize ratios if only one provided.
    o = float(cfg.get("obsidian_ratio", 0.7))
    c = float(cfg.get("cursor_ratio", 0.3))
    if o <= 0 or c <= 0:
        raise ValueError("obsidian_ratio and cursor_ratio must be > 0")
    cfg["obsidian_ratio"] = o
    cfg["cursor_ratio"] = c
    cfg["gap"] = int(cfg.get("gap", 0))
    cfg["monitor"] = cfg.get("monitor", 0)
    cfg["poll_ms"] = int(cfg.get("poll_ms", 500))
    cfg["follow_obsidian"] = bool(cfg.get("follow_obsidian", True))
    cfg["launch_cursor_if_missing"] = bool(cfg.get("launch_cursor_if_missing", True))
    return cfg


def write_state(data: dict[str, Any]) -> None:
    STATE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def read_pid() -> int | None:
    if not PID_FILE.is_file():
        return None
    try:
        return int(PID_FILE.read_text(encoding="utf-8").strip())
    except ValueError:
        return None


def write_pid(pid: int) -> None:
    PID_FILE.write_text(str(pid), encoding="utf-8")


def clear_pid() -> None:
    if PID_FILE.exists():
        PID_FILE.unlink()


def ensure_cursor(cfg: dict[str, Any], workspace: str | None = None) -> None:
    proc = cfg["cursor_process"]
    if is_process_running(proc):
        # Already running — optionally open folder in existing instance.
        if workspace:
            exe = resolve_cursor_exe(cfg.get("cursor_exe_candidates", []))
            if exe:
                print(f"[sidecar] opening workspace in Cursor: {workspace}")
                launch_process(exe, args=[workspace])
        return
    if not cfg["launch_cursor_if_missing"]:
        raise RuntimeError(f"{proc} is not running and launch_cursor_if_missing=false")

    exe = resolve_cursor_exe(cfg.get("cursor_exe_candidates", []))
    if not exe:
        raise RuntimeError(
            "Cursor.exe not found. Add path to config.cursor_exe_candidates"
        )
    args = [workspace] if workspace else None
    print(f"[sidecar] launching {exe}" + (f" {workspace}" if workspace else ""))
    launch_process(exe, args=args)

    found = wait_for_window(
        lambda: find_cursor_window(proc, cfg.get("cursor_title_hint", "")),
        timeout_s=45.0,
    )
    if not found:
        raise RuntimeError("Cursor launched but window not found in time")


def do_arrange(cfg: dict[str, Any]) -> None:
    monitor = cfg.get("monitor")
    # monitor: 0 = primary index; null/"auto" = follow Obsidian's monitor
    if monitor in (None, "auto"):
        mon: int | None = None
    else:
        mon = int(monitor)

    obsidian, cursor, left, right = arrange_sidecar(
        obsidian_process=cfg["obsidian_process"],
        cursor_process=cfg["cursor_process"],
        obsidian_title_hint=cfg.get("obsidian_title_hint", ""),
        cursor_title_hint=cfg.get("cursor_title_hint", ""),
        obsidian_ratio=cfg["obsidian_ratio"],
        cursor_ratio=cfg["cursor_ratio"],
        gap=cfg["gap"],
        monitor=mon,
    )
    print(
        f"[sidecar] arranged\n"
        f"  Obsidian hwnd={obsidian.hwnd} {obsidian.rect.as_tuple()}  \"{obsidian.title}\"\n"
        f"  Cursor   hwnd={cursor.hwnd} {cursor.rect.as_tuple()}  \"{cursor.title}\"\n"
        f"  target L={left.as_tuple()} R={right.as_tuple()}"
    )
    write_state(
        {
            "obsidian_hwnd": obsidian.hwnd,
            "cursor_hwnd": cursor.hwnd,
            "left": list(left.as_tuple()),
            "right": list(right.as_tuple()),
            "updated_at": time.time(),
        }
    )


def follow_loop(cfg: dict[str, Any]) -> None:
    """Re-arrange when Obsidian moves / resizes (e.g. after maximize then restore)."""
    poll = max(cfg["poll_ms"], 100) / 1000.0
    last_rect: tuple[int, int, int, int] | None = None
    print(f"[sidecar] follow mode on (poll={cfg['poll_ms']}ms). Ctrl+C to stop.")
    write_pid(os.getpid())

    def _stop(_sig: int, _frame: object) -> None:
        print("\n[sidecar] stopping follow loop")
        clear_pid()
        sys.exit(0)

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    while True:
        obsidian = find_obsidian_window(
            cfg["obsidian_process"], cfg.get("obsidian_title_hint", "")
        )
        if not obsidian:
            time.sleep(poll)
            continue
        rect = get_window_rect(obsidian.hwnd).as_tuple()
        if last_rect is None or rect != last_rect:
            try:
                do_arrange(cfg)
                # After arrange, Obsidian rect changes — update baseline.
                obsidian2 = find_obsidian_window(
                    cfg["obsidian_process"], cfg.get("obsidian_title_hint", "")
                )
                last_rect = (
                    get_window_rect(obsidian2.hwnd).as_tuple()
                    if obsidian2
                    else rect
                )
            except RuntimeError as exc:
                print(f"[sidecar] arrange skipped: {exc}")
                last_rect = rect
        time.sleep(poll)


def cmd_start(cfg: dict[str, Any], workspace: str | None = None, follow: bool | None = None) -> int:
    ensure_cursor(cfg, workspace=workspace)
    # Wait for Obsidian too.
    obs = wait_for_window(
        lambda: find_obsidian_window(
            cfg["obsidian_process"], cfg.get("obsidian_title_hint", "")
        ),
        timeout_s=5.0,
    )
    if not obs:
        print(
            "[sidecar] warning: Obsidian window not found yet. "
            "Open Obsidian, then run: python main.py arrange"
        )
        return 1

    do_arrange(cfg)
    do_follow = cfg.get("follow_obsidian", True) if follow is None else follow
    if do_follow:
        follow_loop(cfg)
    return 0


def cmd_dock(cfg: dict[str, Any], workspace: str | None = None) -> int:
    """Launch Cursor if needed, arrange once — no follow loop (plugin-friendly)."""
    return cmd_start(cfg, workspace=workspace, follow=False)


def cmd_click(cfg: dict[str, Any], workspace: str | None = None) -> int:
    """Ribbon semantics: dock if Cursor down; toggle visibility if up."""
    proc = cfg["cursor_process"]
    if is_process_running(proc):
        # Prefer toggle when a window already exists (visible or hidden).
        try:
            return cmd_toggle(cfg)
        except RuntimeError:
            # Process up but window not ready — wait and dock.
            wait_for_window(
                lambda: find_cursor_window(proc, cfg.get("cursor_title_hint", "")),
                timeout_s=20.0,
            )
            return cmd_dock(cfg, workspace=workspace)
    return cmd_dock(cfg, workspace=workspace)


def cmd_arrange(cfg: dict[str, Any], workspace: str | None = None) -> int:
    del workspace  # unused
    do_arrange(cfg)
    return 0


def cmd_stop(cfg: dict[str, Any], workspace: str | None = None) -> int:
    del cfg, workspace
    pid = read_pid()
    if not pid:
        print("[sidecar] no follow loop pid file")
        return 0
    try:
        os.kill(pid, signal.SIGTERM)
        print(f"[sidecar] sent SIGTERM to pid {pid}")
    except OSError as exc:
        print(f"[sidecar] could not stop pid {pid}: {exc}")
        clear_pid()
        return 1
    clear_pid()
    return 0


def cmd_toggle(cfg: dict[str, Any], workspace: str | None = None) -> int:
    del workspace
    state = toggle_cursor_visibility(
        cfg["cursor_process"], cfg.get("cursor_title_hint", "")
    )
    print(f"[sidecar] Cursor {state}")
    return 0


def cmd_status(cfg: dict[str, Any], workspace: str | None = None, as_json: bool = False) -> int:
    del workspace
    obs = find_obsidian_window(
        cfg["obsidian_process"], cfg.get("obsidian_title_hint", "")
    )
    cur = find_cursor_window(
        cfg["cursor_process"], cfg.get("cursor_title_hint", "")
    )
    payload = {
        "obsidian": None
        if not obs
        else {
            "hwnd": obs.hwnd,
            "pid": obs.pid,
            "title": obs.title,
            "rect": list(obs.rect.as_tuple()),
        },
        "cursor": None
        if not cur
        else {
            "hwnd": cur.hwnd,
            "pid": cur.pid,
            "title": cur.title,
            "rect": list(cur.rect.as_tuple()),
            "visible": bool(__import__("win32gui").IsWindowVisible(cur.hwnd)),
        },
        "cursor_running": is_process_running(cfg["cursor_process"]),
        "follow_pid": read_pid(),
    }
    if as_json:
        print(json.dumps(payload, ensure_ascii=False))
        return 0
    print("[sidecar] status")
    if obs:
        print(f"  Obsidian: hwnd={obs.hwnd} pid={obs.pid} {obs.rect.as_tuple()} \"{obs.title}\"")
    else:
        print("  Obsidian: not found")
    if cur:
        print(f"  Cursor:   hwnd={cur.hwnd} pid={cur.pid} {cur.rect.as_tuple()} \"{cur.title}\"")
    else:
        print("  Cursor:   not found")
    print(f"  cursor running: {payload['cursor_running']}")
    print(f"  follow pid: {payload['follow_pid'] or 'none'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cursor-sidecar",
        description="Dock Cursor Desktop beside Obsidian without modifying either app.",
    )
    p.add_argument(
        "-c",
        "--config",
        default=str(DEFAULT_CONFIG),
        help="path to config.json",
    )
    p.add_argument(
        "--workspace",
        default=None,
        help="folder to open in Cursor (usually the Obsidian vault path)",
    )
    sub = p.add_subparsers(dest="command", required=True)

    start_p = sub.add_parser("start", help="ensure Cursor, arrange, optionally follow Obsidian")
    start_p.add_argument(
        "--no-follow",
        action="store_true",
        help="arrange once without follow loop",
    )
    sub.add_parser("dock", help="launch Cursor if needed + arrange once (no follow)")
    sub.add_parser(
        "click",
        help="Obsidian ribbon action: dock if Cursor down, else show/hide",
    )
    sub.add_parser("arrange", help="one-shot 70/30 (configurable) split")
    sub.add_parser("stop", help="stop follow loop started by start")
    sub.add_parser("toggle", help="show/hide Cursor window")
    status_p = sub.add_parser("status", help="print window discovery info")
    status_p.add_argument("--json", action="store_true", help="machine-readable JSON")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = load_config(Path(args.config))
    workspace = args.workspace

    if args.command == "start":
        return cmd_start(cfg, workspace=workspace, follow=not args.no_follow)
    if args.command == "dock":
        return cmd_dock(cfg, workspace=workspace)
    if args.command == "click":
        return cmd_click(cfg, workspace=workspace)
    if args.command == "arrange":
        return cmd_arrange(cfg, workspace=workspace)
    if args.command == "stop":
        return cmd_stop(cfg, workspace=workspace)
    if args.command == "toggle":
        return cmd_toggle(cfg, workspace=workspace)
    if args.command == "status":
        return cmd_status(cfg, workspace=workspace, as_json=bool(args.json))
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
