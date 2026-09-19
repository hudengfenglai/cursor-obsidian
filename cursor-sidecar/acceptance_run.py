"""Windows acceptance runner for Cursor Sidecar (self-restoring harness).

Windows-only. Does not run in CI. Never auto-detach a live Sidecar session.
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
    snapshot_window,
    validate_window_binding,
)
import main as sidecar  # noqa: E402


def load_cfg() -> dict[str, Any]:
    return sidecar.load_config(ROOT / "config.json")


def show_cmd(hwnd: int) -> int:
    return win32gui.GetWindowPlacement(hwnd)[1]


def maximize(hwnd: int) -> None:
    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)


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


def status(cfg: dict[str, Any]) -> dict[str, Any]:
    return sidecar.build_status(cfg)


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
    # Write bytes atomically via temp (preserve exact prior JSON)
    tmp = path.with_name(path.name + ".acceptance.bak.tmp")
    tmp.write_bytes(backup)
    tmp.replace(path)


def restore_exact_snapshot(snap: dict[str, Any] | None, label: str) -> str:
    """Restore only the exact HWND captured at harness start. No rediscovery."""
    if not snap:
        return "skip"
    check = validate_window_binding(snap)
    if not check.get("ok"):
        print(f"[restore] {label}: gone (exact HWND no longer valid)")
        return "gone"
    ok = apply_window_placement(int(snap["hwnd"]), snap)
    print(f"[restore] {label}: {'ok' if ok else 'fail'} hwnd={snap.get('hwnd')}")
    return "ok" if ok else "fail"


def write_detached_scratch() -> None:
    """Scratch detached state for scenarios — never used to wipe a live Attach."""
    sidecar.write_state(
        {
            "attached": False,
            "timestamp": time.time(),
            "version": sidecar.VERSION,
        }
    )


def run_scenarios(
    cfg: dict[str, Any],
    dpi: str,
    obs_hwnd: int,
    cur_hwnd: int,
) -> list[bool]:
    outcomes: list[bool] = []

    # ---- Scenario 1: maximize → attach → detach restore ----
    print("--- Scenario 1: maximize restore ---")
    maximize(obs_hwnd)
    maximize(cur_hwnd)
    time.sleep(0.5)
    before_obs = show_cmd(obs_hwnd)
    before_cur = show_cmd(cur_hwnd)
    obs_snap = snapshot_window(obs_hwnd)
    cur_snap = snapshot_window(cur_hwnd)
    code = sidecar.cmd_attach(cfg)
    st = status(cfg)
    mid_obs = show_cmd(obs_hwnd)
    mid_cur = show_cmd(cur_hwnd)
    mid_orect = get_window_rect(obs_hwnd).as_tuple()
    mid_crect = get_window_rect(cur_hwnd).as_tuple()
    code2 = sidecar.cmd_detach(cfg)
    time.sleep(0.4)
    after_obs = show_cmd(obs_hwnd)
    after_cur = show_cmd(cur_hwnd)
    st_after = status(cfg)
    ok1_restore = (
        after_obs == win32con.SW_SHOWMAXIMIZED
        and after_cur == win32con.SW_SHOWMAXIMIZED
        and st_after.get("attached") is False
    )
    split_ok = mid_orect != obs_snap["original_rect"] or mid_crect != cur_snap["original_rect"]
    outcomes.append(
        result(
            "1 maximize→attach→detach",
            ok1_restore and split_ok and code == 0 and code2 == 0,
            f"before=({before_obs},{before_cur}) mid_show=({mid_obs},{mid_cur}) "
            f"after=({after_obs},{after_cur}) mid_rects O={mid_orect} C={mid_crect} "
            f"attached_after={st_after.get('attached')} split_ok={split_ok}",
        )
    )

    # ---- Scenario 2: Cursor already open → click must Attach not hide ----
    print("\n--- Scenario 2: click attaches when Cursor already open ---")
    write_detached_scratch()
    cur2 = find_cursor_window(cfg["cursor_process"], cfg.get("cursor_title_hint", ""))
    vis_before = bool(cur2 and win32gui.IsWindowVisible(cur2.hwnd))
    code = sidecar.cmd_click(cfg)
    st = status(cfg)
    cur2b = find_cursor_window(cfg["cursor_process"], cfg.get("cursor_title_hint", ""))
    vis_after = bool(cur2b and win32gui.IsWindowVisible(cur2b.hwnd))
    outcomes.append(
        result(
            "2 click attaches (no hide)",
            code == 0 and st.get("attached") is True and vis_before and vis_after,
            f"vis {vis_before}→{vis_after} attached={st.get('attached')} "
            f"bound_hwnd={(st.get('cursor') or {}).get('hwnd')}",
        )
    )

    # ---- Scenario 3: binding sticks across arrange/focus/status ----
    print("\n--- Scenario 3: bound HWND stable across arrange/focus ---")
    bound = (st.get("cursor") or {}).get("hwnd")
    sidecar.cmd_arrange(cfg)
    st_a = status(cfg)
    sidecar.cmd_focus(cfg)
    st_f = status(cfg)
    hwnd_a = (st_a.get("cursor") or {}).get("hwnd")
    hwnd_f = (st_f.get("cursor") or {}).get("hwnd")
    outcomes.append(
        result(
            "3 binding stable",
            bool(
                bound
                and bound == hwnd_a == hwnd_f
                and st_a.get("attached")
                and st_f.get("attached")
            ),
            f"bound={bound} after_arrange={hwnd_a} after_focus={hwnd_f}",
        )
    )

    # ---- Scenario 4: simulate cursor gone via stale state (safe, no kill) ----
    print("\n--- Scenario 4: stale cursor binding → attached=false ---")
    state = sidecar.read_state()
    fake = dict(state.get("cursor") or {})
    fake["hwnd"] = 1
    fake["pid"] = 1
    fake["process_create_time"] = 0.1
    state["cursor"] = fake
    state["attached"] = True
    sidecar.write_state(state)
    truth = sidecar.refresh_attachment_truth()
    st4 = status(cfg)
    outcomes.append(
        result(
            "4 cursor_gone stale status",
            truth.get("attached") is False
            and st4.get("attached") is False
            and st4.get("reason") == "cursor_gone",
            f"reason={st4.get('reason')} attached={st4.get('attached')} "
            f"state_valid={st4.get('state_valid')}",
        )
    )
    write_detached_scratch()
    sidecar.cmd_attach(cfg)

    # ---- Scenario 5: detach must not touch other cursor if binding gone ----
    print("\n--- Scenario 5: detach with gone binding does not move other windows ---")
    cur_live = find_cursor_window(cfg["cursor_process"], cfg.get("cursor_title_hint", ""))
    rect_b_before = None
    place_b_before = None
    if cur_live:
        rect_b_before = get_window_rect(cur_live.hwnd).as_tuple()
        place_b_before = win32gui.GetWindowPlacement(cur_live.hwnd)
    state = sidecar.read_state()
    dead = dict(state.get("cursor") or {})
    dead["hwnd"] = 999001
    dead["pid"] = 999001
    dead["process_create_time"] = 1.23
    state["cursor"] = dead
    state["attached"] = True
    sidecar.write_state(state)
    code = sidecar.cmd_detach(cfg)
    after_state = sidecar.read_state()
    last = after_state.get("last_detach") or {}
    cur_after = find_cursor_window(cfg["cursor_process"], cfg.get("cursor_title_hint", ""))
    rect_b_after = get_window_rect(cur_after.hwnd).as_tuple() if cur_after else None
    place_b_after = (
        win32gui.GetWindowPlacement(cur_after.hwnd) if cur_after else None
    )
    untouched = rect_b_before == rect_b_after and (
        place_b_before is None or place_b_before == place_b_after
    )
    outcomes.append(
        result(
            "5 detach gone → Cursor B untouched",
            last.get("cursor") == "gone" and untouched and code == 0,
            f"last_detach.cursor={last.get('cursor')} rect {rect_b_before}→{rect_b_after} "
            f"untouched={untouched}",
        )
    )

    # ---- Scenario 6: DPI path / work area sanity (no visual) ----
    print("\n--- Scenario 6: DPI + work area path ---")
    obs6, _ = wait_windows(cfg)
    work = get_work_area_for_hwnd(obs6.hwnd)
    ok6 = work.width > 100 and work.height > 100
    outcomes.append(
        result(
            "6 work area via MonitorFromWindow",
            ok6 and dpi in ("per_monitor_v2", "per_monitor_v1", "system", "already"),
            f"dpi={dpi} work={work.as_tuple()} w={work.width} h={work.height}",
        )
    )

    return outcomes


def main() -> int:
    dpi = enable_dpi_awareness()
    cfg = load_cfg()
    print(f"=== Sidecar acceptance dpi={dpi} version={sidecar.VERSION} ===\n")

    st0 = status(cfg)
    if st0.get("attached") is True:
        print(
            "[FAIL] SETUP: Acceptance test requires Sidecar to be detached first.\n"
            "       Run: python main.py detach\n"
            "       Then re-run acceptance_run.py (will not overwrite a live Attach)."
        )
        return 3

    obs, cur = wait_windows(cfg)
    if not obs:
        print("[FAIL] SETUP: Obsidian window not found — open a vault and retry")
        return 1
    if not cur:
        print("[FAIL] SETUP: Cursor window not found — open Cursor Editor and retry")
        return 1
    print(f"SETUP Obsidian hwnd={obs.hwnd} title={obs.title!r}")
    print(f"SETUP Cursor   hwnd={cur.hwnd} title={cur.title!r}\n")

    # Capture desktop + state BEFORE any maximize / attach / poison
    state_backup = backup_state_file()
    initial_obsidian = snapshot_window(obs.hwnd)
    initial_cursor = snapshot_window(cur.hwnd)
    print(
        f"CAPTURED initial placements "
        f"O={initial_obsidian.get('original_rect')} "
        f"C={initial_cursor.get('original_rect')}\n"
    )

    outcomes: list[bool] = []
    exit_code = 2
    try:
        write_detached_scratch()
        outcomes = run_scenarios(cfg, dpi, obs.hwnd, cur.hwnd)
        passed = sum(1 for x in outcomes if x)
        total = len(outcomes)
        print("\n=== SUMMARY ===")
        print(f"{passed}/{total} passed")
        print(
            "NOTE: Scenario 3 ran with whatever Cursor Editor windows were open "
            "(multi-instance still needs manual coverage)."
        )
        print(
            "NOTE: Scenario 6 checks DPI awareness mode + work-area API; "
            "visual seam/overlap on 125%/150% still needs human eyes."
        )
        exit_code = 0 if passed == total else 2
    except KeyboardInterrupt:
        print("\n[ABORT] KeyboardInterrupt")
        exit_code = 130
        raise
    except Exception as exc:  # noqa: BLE001 — harness must restore in finally
        print(f"\n[ERROR] {exc}")
        exit_code = 1
    finally:
        print("\n=== RESTORE environment ===")
        try:
            restore_exact_snapshot(initial_obsidian, "Obsidian")
            restore_exact_snapshot(initial_cursor, "Cursor")
        except Exception as exc:  # noqa: BLE001
            print(f"[restore] window restore error: {exc}")
        try:
            restore_state_file(state_backup)
            print("[restore] .sidecar.state.json restored from harness backup")
        except Exception as exc:  # noqa: BLE001
            print(f"[restore] state restore error: {exc}")
            try:
                write_detached_scratch()
                print("[restore] fell back to clean detached state")
            except Exception:
                pass

    return exit_code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
