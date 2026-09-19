"""Windows STRICT acceptance for v0.3 Live Sidecar (self-restoring).

Windows-only. Not CI.
Default mode is STRICT: no follow_now() rescue, no private handler calls.
Drives a real out-of-process daemon so WinEventHook (WINEVENT_SKIPOWNPROCESS)
can observe moves performed by this harness process.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import win32con
import win32gui

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from window import (  # noqa: E402
    Rect,
    apply_window_placement,
    enable_dpi_awareness,
    find_cursor_window,
    find_obsidian_window,
    get_window_rect,
    get_work_area_for_hwnd,
    list_monitor_work_areas,
    set_window_rect,
    snapshot_window,
    validate_window_binding,
)
import main as sidecar  # noqa: E402

# STRICT: forbids rescue helpers that mask missing WinEvents
STRICT_ACCEPTANCE = True


def forbid_rescue(name: str) -> None:
    if STRICT_ACCEPTANCE:
        raise RuntimeError(f"strict acceptance forbids rescue: {name}")


def load_cfg() -> dict[str, Any]:
    return sidecar.load_config(ROOT / "config.json")


def wait_windows(cfg: dict[str, Any], timeout: float = 20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        obs = find_obsidian_window(
            cfg["obsidian_process"], cfg.get("obsidian_title_hint", "")
        )
        cur = find_cursor_window(
            cfg["cursor_process"], cfg.get("cursor_title_hint", "")
        )
        if obs and cur:
            return obs, cur
        time.sleep(0.4)
    return (
        find_obsidian_window(
            cfg["obsidian_process"], cfg.get("obsidian_title_hint", "")
        ),
        find_cursor_window(cfg["cursor_process"], cfg.get("cursor_title_hint", "")),
    )


def result(name: str, ok: bool | None, detail: str) -> bool | None:
    if ok is None:
        print(f"[SKIP] {name}: {detail}")
        return None
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}: {detail}")
    return ok


def backup_state_file() -> bytes | None:
    path = sidecar.STATE_FILE
    if not path.is_file():
        return None
    return path.read_bytes()


def restore_state_file(backup: bytes | None) -> None:
    path = sidecar.STATE_FILE
    if backup is None:
        sidecar.write_state(
            {
                "attached": False,
                "timestamp": time.time(),
                "version": sidecar.VERSION,
            }
        )
        return
    tmp = path.with_name(path.name + ".acceptance.bak.tmp")
    tmp.write_bytes(backup)
    tmp.replace(path)


def restore_exact_snapshot(snap: dict[str, Any] | None, label: str) -> str:
    if not snap:
        return "skip"
    check = validate_window_binding(snap)
    if not check.get("ok"):
        print(f"[restore] {label}: gone (exact HWND no longer valid)")
        return "gone"
    ok = apply_window_placement(int(snap["hwnd"]), snap)
    print(f"[restore] {label}: {'ok' if ok else 'fail'} hwnd={snap.get('hwnd')}")
    return "ok" if ok else "fail"


def approx_edge(cursor_left: int, obs_right: int, tol: int = 12) -> bool:
    return abs(cursor_left - obs_right) <= tol


def force_move(hwnd: int, left: int, top: int, width: int, height: int) -> None:
    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    set_window_rect(hwnd, Rect(left, top, left + width, top + height), activate=False)


def daemon_token() -> str:
    if sidecar.TOKEN_FILE.is_file():
        return sidecar.TOKEN_FILE.read_text(encoding="utf-8").strip()
    return sidecar.ensure_daemon_token()


def http_json(cfg: dict[str, Any], method: str, path: str, body: dict | None = None) -> dict[str, Any]:
    host = cfg["daemon_host"]
    port = int(cfg["daemon_port"])
    url = f"http://{host}:{port}{path}"
    data = None
    headers = {"X-Cursor-Sidecar-Token": daemon_token()}
    if body is not None:
        raw = json.dumps(body).encode("utf-8")
        data = raw
        headers["Content-Type"] = "application/json"
        headers["Content-Length"] = str(len(raw))
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        raw_err = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw_err or "{}")
        except json.JSONDecodeError:
            payload = {"ok": False, "error": raw_err or str(exc)}
        payload.setdefault("ok", False)
        payload["_http_status"] = exc.code
        return payload


def daemon_healthy(cfg: dict[str, Any]) -> bool:
    try:
        r = http_json(cfg, "GET", "/health")
        return bool(r.get("ok"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return False


def rpc(cfg: dict[str, Any], cmd: str, **extra: Any) -> dict[str, Any]:
    body = {"cmd": cmd, **extra}
    return http_json(cfg, "POST", "/rpc", body)


def status_remote(cfg: dict[str, Any]) -> dict[str, Any]:
    payload = http_json(cfg, "GET", "/status")
    return payload.get("status") or payload


def ensure_daemon(cfg: dict[str, Any]) -> bool:
    """Ensure out-of-process daemon runs RC code. Returns True if we (re)started it."""
    def _wait_up() -> bool:
        for _ in range(24):
            time.sleep(0.25)
            if daemon_healthy(cfg):
                return True
        return False

    if daemon_healthy(cfg):
        probe = rpc(cfg, "set-live-follow", enabled=True)
        if probe.get("ok"):
            return False
        # Old daemon without set-live-follow — restart to load current code
        print(
            f"[acceptance-live] restarting daemon (RC probe failed: "
            f"{probe.get('error') or probe.get('_http_status')})"
        )
        try:
            sidecar.cmd_stop(cfg)
        except Exception:
            pass
        time.sleep(0.4)

    sidecar.cmd_daemon_start(cfg)
    if not _wait_up():
        raise RuntimeError("daemon failed to become healthy")
    probe = rpc(cfg, "set-live-follow", enabled=True)
    if not probe.get("ok"):
        raise RuntimeError(f"set-live-follow unsupported after restart: {probe}")
    return True


def wait_hook_follow(
    cfg: dict[str, Any],
    obs_hwnd: int,
    cur_hwnd: int,
    *,
    baseline_follow_at: float | None,
    allowed_events: set[str],
    timeout: float = 5.0,
    expect_hidden: bool | None = None,
) -> tuple[bool, str]:
    """Wait for daemon WinEventHook to update follow — never calls follow_now()."""
    if not STRICT_ACCEPTANCE:
        forbid_rescue("non-strict")
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        st = status_remote(cfg)
        lf = st.get("live_follow") or {}
        last_at = lf.get("last_follow_at")
        last_event = str(lf.get("last_event") or "")
        if expect_hidden is True:
            vis = bool(win32gui.IsWindowVisible(cur_hwnd))
            iconic = bool(win32gui.IsIconic(cur_hwnd))
            event_ok = (not allowed_events) or (last_event in allowed_events)
            if (not vis or iconic) and event_ok:
                return True, f"hidden vis={vis} iconic={iconic} event={last_event}"
            last = f"vis={vis} iconic={iconic} event={last_event}"
            time.sleep(0.1)
            continue
        if expect_hidden is False:
            if win32gui.IsWindowVisible(cur_hwnd) and not win32gui.IsIconic(cur_hwnd):
                o = get_window_rect(obs_hwnd).as_tuple()
                c = get_window_rect(cur_hwnd).as_tuple()
                follow_ok = approx_edge(c[0], o[2]) and abs(c[1] - o[1]) <= 16
                event_ok = (not allowed_events) or (last_event in allowed_events)
                at_ok = last_at is not None and (
                    baseline_follow_at is None or float(last_at) > float(baseline_follow_at)
                )
                if follow_ok and event_ok and (at_ok or last_event in allowed_events):
                    return True, f"visible+follow event={last_event} O={o} C={c}"
            last = f"waiting restore event={last_event}"
            time.sleep(0.1)
            continue

        o = get_window_rect(obs_hwnd).as_tuple()
        c = get_window_rect(cur_hwnd).as_tuple()
        height_ok = abs((c[3] - c[1]) - (o[3] - o[1])) <= 20
        rect_ok = approx_edge(c[0], o[2]) and abs(c[1] - o[1]) <= 16 and height_ok
        event_ok = last_event in allowed_events
        at_ok = last_at is not None and (
            baseline_follow_at is None or float(last_at) > float(baseline_follow_at or 0)
        )
        if rect_ok and event_ok and at_ok:
            return True, f"event={last_event} at={last_at} O={o} C={c}"
        last = f"event={last_event} at={last_at} O={o} C={c} rect_ok={rect_ok}"
        # STRICT: never sidecar._FOLLOW.follow_now() here
        time.sleep(0.1)
    return False, last


def run_scenarios(cfg: dict[str, Any], obs_hwnd: int, cur_hwnd: int) -> list[bool | None]:
    outcomes: list[bool | None] = []
    work = get_work_area_for_hwnd(obs_hwnd)

    print("--- A: Attach → move Obsidian → Cursor follows via WinEventHook ---")
    r = rpc(cfg, "attach")
    time.sleep(0.5)
    st = status_remote(cfg)
    lf = st.get("live_follow") or {}
    attach_ok = bool(r.get("ok")) and st.get("attached") is True
    hook_ok = bool(lf.get("running") or lf.get("hook_installed"))
    baseline_at = lf.get("last_follow_at")
    baseline_event = lf.get("last_event")

    new_left = work.left + 40
    new_top = work.top + 40
    o0 = get_window_rect(obs_hwnd).as_tuple()
    new_w = min(max(o0[2] - o0[0], 400), work.width - 200)
    new_h = min(max(o0[3] - o0[1], 300), work.height - 80)
    force_move(obs_hwnd, new_left, new_top, new_w, new_h)
    ok_a, detail_a = wait_hook_follow(
        cfg,
        obs_hwnd,
        cur_hwnd,
        baseline_follow_at=float(baseline_at) if baseline_at else 0.0,
        allowed_events={"LOCATIONCHANGE", "MOVESIZEEND"},
    )
    o1 = get_window_rect(obs_hwnd).as_tuple()
    not_snapped = abs(o1[0] - new_left) <= 20
    outcomes.append(
        result(
            "A move follow (strict)",
            attach_ok and hook_ok and ok_a and not_snapped,
            f"attach={attach_ok} hook={hook_ok} baseline_event={baseline_event} "
            f"not_snapped={not_snapped} {detail_a}",
        )
    )

    print("\n--- B: Resize Obsidian → Cursor follows via WinEventHook ---")
    st_b0 = status_remote(cfg)
    lf_b0 = st_b0.get("live_follow") or {}
    baseline_b = lf_b0.get("last_follow_at")
    o = get_window_rect(obs_hwnd).as_tuple()
    force_move(
        obs_hwnd,
        o[0],
        o[1],
        max(o[2] - o[0] - 80, 350),
        max(o[3] - o[1] - 60, 280),
    )
    ok_b, detail_b = wait_hook_follow(
        cfg,
        obs_hwnd,
        cur_hwnd,
        baseline_follow_at=float(baseline_b) if baseline_b else 0.0,
        allowed_events={"LOCATIONCHANGE", "MOVESIZEEND"},
    )
    outcomes.append(result("B resize follow (strict)", ok_b, detail_b))

    print("\n--- C: Obsidian minimize → Cursor hidden (WinEvent) ---")
    st_c0 = status_remote(cfg)
    win32gui.ShowWindow(obs_hwnd, win32con.SW_MINIMIZE)
    # STRICT: do not call _on_obsidian_minimize()
    ok_c, detail_c = wait_hook_follow(
        cfg,
        obs_hwnd,
        cur_hwnd,
        baseline_follow_at=None,
        allowed_events={"MINIMIZESTART"},
        expect_hidden=True,
        timeout=4.0,
    )
    outcomes.append(result("C minimize hide (strict)", ok_c, detail_c))

    print("\n--- D: Obsidian restore → Cursor reappears (WinEvent) ---")
    win32gui.ShowWindow(obs_hwnd, win32con.SW_RESTORE)
    # STRICT: do not call _on_obsidian_restore()
    ok_d, detail_d = wait_hook_follow(
        cfg,
        obs_hwnd,
        cur_hwnd,
        baseline_follow_at=None,
        allowed_events={"MINIMIZEEND", "LOCATIONCHANGE", "MOVESIZEEND"},
        expect_hidden=False,
        timeout=5.0,
    )
    outcomes.append(result("D restore show (strict)", ok_d, detail_d))

    print("\n--- E: Multi-monitor migration ---")
    monitors = list_monitor_work_areas()
    if len(monitors) < 2:
        outcomes.append(result("E monitor migration", None, "single monitor"))
    else:
        cur_work = get_work_area_for_hwnd(obs_hwnd)
        other = next(
            (m for m in monitors if m.as_tuple() != cur_work.as_tuple()),
            None,
        )
        if other is None:
            outcomes.append(result("E monitor migration", None, "no distinct other monitor"))
        else:
            st_e0 = status_remote(cfg)
            baseline_e = (st_e0.get("live_follow") or {}).get("last_follow_at")
            target_w = min(900, other.width - 120)
            target_h = min(700, other.height - 80)
            force_move(
                obs_hwnd,
                other.left + 40,
                other.top + 40,
                target_w,
                target_h,
            )
            ok_e, detail_e = wait_hook_follow(
                cfg,
                obs_hwnd,
                cur_hwnd,
                baseline_follow_at=float(baseline_e) if baseline_e else 0.0,
                allowed_events={"LOCATIONCHANGE", "MOVESIZEEND"},
                timeout=6.0,
            )
            c = get_window_rect(cur_hwnd).as_tuple()
            new_work = get_work_area_for_hwnd(obs_hwnd)
            in_new = c[0] >= new_work.left - 12 and c[2] <= new_work.right + 12
            outcomes.append(
                result(
                    "E monitor migration (strict)",
                    ok_e and in_new and new_work.as_tuple() == other.as_tuple(),
                    f"other={other.as_tuple()} new_work={new_work.as_tuple()} "
                    f"in_new={in_new} {detail_e}",
                )
            )

    print("\n--- F: Detach → restore attach-time originals ---")
    # Read originals via local state file (daemon writes same file)
    time.sleep(0.2)
    state = sidecar.read_state()
    orig_o = (state.get("obsidian") or {}).get("original_rect")
    orig_c = (state.get("cursor") or {}).get("original_rect")
    r_f = rpc(cfg, "detach")
    time.sleep(0.5)
    st_f = status_remote(cfg)
    o_f = get_window_rect(obs_hwnd).as_tuple()
    c_f = get_window_rect(cur_hwnd).as_tuple()

    def close_rect(a, b, tol=40):
        if not a or not b:
            return False
        return all(abs(int(a[i]) - int(b[i])) <= tol for i in range(4))

    restored = close_rect(o_f, orig_o) and close_rect(c_f, orig_c)
    follow_stopped = not ((st_f.get("live_follow") or {}).get("running"))
    outcomes.append(
        result(
            "F detach restore",
            bool(r_f.get("ok"))
            and st_f.get("attached") is False
            and restored
            and follow_stopped,
            f"attached={st_f.get('attached')} restored={restored} "
            f"O {orig_o}→{o_f} C {orig_c}→{c_f} follow_stopped={follow_stopped}",
        )
    )

    print("\n--- G: Cursor destroy routing (unit path; no user Cursor kill) ---")
    # Covered by pytest mock of dispatch_win_event; document manual check.
    print(
        "[INFO] G: do not auto-close user Cursor. "
        "Unit test covers CURSOR_DESTROY → cursor_gone. "
        "Manual: Attach, close bound Cursor, confirm status.reason=cursor_gone."
    )
    outcomes.append(result("G cursor destroy", None, "manual / unit-tested; not auto-closing user Cursor"))

    return outcomes


def main() -> int:
    dpi = enable_dpi_awareness()
    print(f"[acceptance-live] STRICT={STRICT_ACCEPTANCE} dpi={dpi}")
    cfg = load_cfg()

    truth = sidecar.refresh_attachment_truth()
    if truth.get("attached"):
        print(
            "[acceptance-live] REFUSED: Sidecar is currently attached. "
            "Detach first so this harness cannot overwrite live restore state."
        )
        return 2

    obs, cur = wait_windows(cfg)
    if not obs or not cur:
        print("[acceptance-live] need both Obsidian and Cursor windows open")
        return 2

    obs_hwnd, cur_hwnd = obs.hwnd, cur.hwnd
    print(f"[acceptance-live] Obsidian hwnd={obs_hwnd} Cursor hwnd={cur_hwnd}")

    state_backup = backup_state_file()
    obs_snap = snapshot_window(obs_hwnd)
    cur_snap = snapshot_window(cur_hwnd)
    outcomes: list[bool | None] = []
    started_daemon = False

    try:
        started_daemon = ensure_daemon(cfg)
        print(f"[acceptance-live] daemon ready (started_by_us={started_daemon})")
        outcomes = run_scenarios(cfg, obs_hwnd, cur_hwnd)
    finally:
        print("\n[acceptance-live] cleanup / restore desktop…")
        try:
            if daemon_healthy(cfg):
                st = status_remote(cfg)
                if st.get("attached"):
                    rpc(cfg, "detach")
        except Exception as exc:
            print(f"[acceptance-live] detach during cleanup: {exc}")
            try:
                sidecar.cmd_detach(cfg)
            except Exception:
                pass
        restore_exact_snapshot(obs_snap, "Obsidian")
        restore_exact_snapshot(cur_snap, "Cursor")
        restore_state_file(state_backup)
        print("[acceptance-live] state file restored")
        # Leave a pre-existing daemon running; only stop if we started it for this run
        if started_daemon:
            try:
                sidecar.cmd_stop(cfg)
            except Exception as exc:
                print(f"[acceptance-live] stop daemon: {exc}")

    passed = sum(1 for x in outcomes if x is True)
    failed = sum(1 for x in outcomes if x is False)
    skipped = sum(1 for x in outcomes if x is None)
    print(f"\n[acceptance-live] {passed} PASS, {failed} FAIL, {skipped} SKIP")
    return 0 if failed == 0 and passed > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
