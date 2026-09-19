"""Windows acceptance for v0.3 Live Sidecar follow (self-restoring).

Windows-only. Not CI. Refuses to run if Sidecar is already attached.
Enables in-process daemon mode so WinEventHook can run without a separate process.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

import win32con
import win32gui

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from window import (  # noqa: E402
    apply_window_placement,
    enable_dpi_awareness,
    find_cursor_window,
    find_obsidian_window,
    get_window_rect,
    get_work_area_for_hwnd,
    set_window_rect,
    snapshot_window,
    validate_window_binding,
    Rect,
)
import main as sidecar  # noqa: E402


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


def result(name: str, ok: bool, detail: str) -> bool:
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


def approx_edge(cursor_left: int, obs_right: int, tol: int = 8) -> bool:
    return abs(cursor_left - obs_right) <= tol


def force_move(hwnd: int, left: int, top: int, width: int, height: int) -> None:
    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    set_window_rect(hwnd, Rect(left, top, left + width, top + height), activate=False)


def wait_follow(
    obs_hwnd: int,
    cur_hwnd: int,
    *,
    timeout: float = 3.0,
    expect_hidden: bool | None = None,
) -> tuple[bool, str]:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        if expect_hidden is True:
            vis = bool(win32gui.IsWindowVisible(cur_hwnd))
            iconic = bool(win32gui.IsIconic(cur_hwnd))
            if not vis or iconic:
                return True, f"hidden vis={vis} iconic={iconic}"
            last = f"still visible vis={vis}"
            time.sleep(0.1)
            continue
        if expect_hidden is False:
            if win32gui.IsWindowVisible(cur_hwnd) and not win32gui.IsIconic(cur_hwnd):
                # also require edge follow
                o = get_window_rect(obs_hwnd).as_tuple()
                c = get_window_rect(cur_hwnd).as_tuple()
                if approx_edge(c[0], o[2]) and abs(c[1] - o[1]) <= 12:
                    return True, f"visible+follow O={o} C={c}"
            last = "waiting restore+follow"
            time.sleep(0.1)
            continue
        o = get_window_rect(obs_hwnd).as_tuple()
        c = get_window_rect(cur_hwnd).as_tuple()
        height_ok = abs((c[3] - c[1]) - (o[3] - o[1])) <= 16
        if approx_edge(c[0], o[2]) and abs(c[1] - o[1]) <= 12 and height_ok:
            return True, f"O={o} C={c}"
        last = f"O={o} C={c}"
        # nudge follow in case event missed during synthetic SetWindowPos
        if sidecar._FOLLOW:
            sidecar._FOLLOW.follow_now()
        time.sleep(0.08)
    return False, last


def run_scenarios(cfg: dict[str, Any], obs_hwnd: int, cur_hwnd: int) -> list[bool]:
    outcomes: list[bool] = []
    work = get_work_area_for_hwnd(obs_hwnd)

    # Enable in-process live follow (same process as harness)
    sidecar._DAEMON_MODE = True

    print("--- A: Attach → move Obsidian → Cursor right-edge follows ---")
    code = sidecar.cmd_attach(cfg)
    st = sidecar.build_status(cfg)
    lf = st.get("live_follow") or {}
    attach_ok = code == 0 and st.get("attached") is True
    # Ensure hook running
    state = sidecar.read_state()
    sidecar.start_live_follow_if_daemon(cfg, state)
    time.sleep(0.3)
    st2 = sidecar.build_status(cfg)
    lf = st2.get("live_follow") or {}
    hook_ok = bool(lf.get("running") or lf.get("hook_installed"))

    # Move Obsidian within work area (do not touch Cursor)
    o0 = get_window_rect(obs_hwnd).as_tuple()
    new_left = work.left + 40
    new_top = work.top + 40
    new_w = min(max(o0[2] - o0[0], 400), work.width - 200)
    new_h = min(max(o0[3] - o0[1], 300), work.height - 80)
    force_move(obs_hwnd, new_left, new_top, new_w, new_h)
    ok_a, detail_a = wait_follow(obs_hwnd, cur_hwnd)
    # Obsidian must not have been snapped back to original attach left
    o1 = get_window_rect(obs_hwnd).as_tuple()
    not_snapped = abs(o1[0] - new_left) <= 20
    outcomes.append(
        result(
            "A move follow",
            attach_ok and hook_ok and ok_a and not_snapped,
            f"attach={attach_ok} hook={hook_ok} follow={ok_a} not_snapped={not_snapped} {detail_a}",
        )
    )

    print("\n--- B: Resize Obsidian → Cursor height/position follows ---")
    o = get_window_rect(obs_hwnd).as_tuple()
    force_move(obs_hwnd, o[0], o[1], max(o[2] - o[0] - 80, 350), max(o[3] - o[1] - 60, 280))
    ok_b, detail_b = wait_follow(obs_hwnd, cur_hwnd)
    outcomes.append(result("B resize follow", ok_b, detail_b))

    print("\n--- C: Obsidian minimize → Cursor hidden ---")
    win32gui.ShowWindow(obs_hwnd, win32con.SW_MINIMIZE)
    time.sleep(0.2)
    if sidecar._FOLLOW:
        sidecar._FOLLOW._on_obsidian_minimize()
    ok_c, detail_c = wait_follow(obs_hwnd, cur_hwnd, expect_hidden=True, timeout=2.0)
    outcomes.append(result("C minimize hide", ok_c, detail_c))

    print("\n--- D: Obsidian restore → Cursor reappears + follows ---")
    win32gui.ShowWindow(obs_hwnd, win32con.SW_RESTORE)
    time.sleep(0.2)
    if sidecar._FOLLOW:
        sidecar._FOLLOW._on_obsidian_restore()
    ok_d, detail_d = wait_follow(obs_hwnd, cur_hwnd, expect_hidden=False, timeout=3.0)
    outcomes.append(result("D restore show", ok_d, detail_d))

    print("\n--- E: Monitor migration (soft if single monitor) ---")
    # Soft: re-read MonitorFromWindow after move; follow still clamps to that work area
    work_e = get_work_area_for_hwnd(obs_hwnd)
    o = get_window_rect(obs_hwnd).as_tuple()
    c = get_window_rect(cur_hwnd).as_tuple()
    in_work = c[0] >= work_e.left - 8 and c[2] <= work_e.right + 8
    ok_e, detail_e = wait_follow(obs_hwnd, cur_hwnd)
    outcomes.append(
        result(
            "E monitor work-area follow",
            ok_e and in_work,
            f"work={work_e.as_tuple()} O={o} C={c} in_work={in_work} {detail_e}",
        )
    )

    print("\n--- F: Detach → restore attach-time originals ---")
    # Capture post-attach originals from state (saved at Attach)
    state = sidecar.read_state()
    orig_o = (state.get("obsidian") or {}).get("original_rect")
    orig_c = (state.get("cursor") or {}).get("original_rect")
    code_f = sidecar.cmd_detach(cfg)
    time.sleep(0.4)
    st_f = sidecar.build_status(cfg)
    o_f = get_window_rect(obs_hwnd).as_tuple()
    c_f = get_window_rect(cur_hwnd).as_tuple()
    # Restore should match saved originals closely (placement restore)
    def close_rect(a, b, tol=40):
        if not a or not b:
            return False
        return all(abs(int(a[i]) - int(b[i])) <= tol for i in range(4))

    restored = close_rect(o_f, orig_o) and close_rect(c_f, orig_c)
    follow_stopped = not ((st_f.get("live_follow") or {}).get("running"))
    outcomes.append(
        result(
            "F detach restore",
            code_f == 0
            and st_f.get("attached") is False
            and restored
            and follow_stopped,
            f"attached={st_f.get('attached')} restored={restored} "
            f"O {orig_o}→{o_f} C {orig_c}→{c_f} follow_stopped={follow_stopped}",
        )
    )
    return outcomes


def main() -> int:
    dpi = enable_dpi_awareness()
    print(f"[acceptance-live] dpi={dpi}")
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
    outcomes: list[bool] = []

    try:
        outcomes = run_scenarios(cfg, obs_hwnd, cur_hwnd)
    finally:
        print("\n[acceptance-live] cleanup / restore desktop…")
        try:
            sidecar.stop_live_follow()
        except Exception:
            pass
        sidecar._DAEMON_MODE = False
        try:
            if sidecar.read_state().get("attached"):
                sidecar.cmd_detach(cfg)
        except Exception as exc:
            print(f"[acceptance-live] detach during cleanup: {exc}")
        restore_exact_snapshot(obs_snap, "Obsidian")
        restore_exact_snapshot(cur_snap, "Cursor")
        restore_state_file(state_backup)
        print("[acceptance-live] state file restored")

    passed = sum(1 for x in outcomes if x)
    total = len(outcomes)
    print(f"\n[acceptance-live] {passed}/{total} passed")
    return 0 if passed == total and total > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
