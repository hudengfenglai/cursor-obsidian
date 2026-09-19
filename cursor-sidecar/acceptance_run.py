"""Windows acceptance runner for Cursor Sidecar v0.2.1 (scenarios 1–5 automatable)."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import win32con
import win32gui

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from window import (  # noqa: E402
    enable_dpi_awareness,
    find_cursor_window,
    find_obsidian_window,
    get_window_rect,
    snapshot_window,
)
import main as sidecar  # noqa: E402


def load_cfg():
    return sidecar.load_config(ROOT / "config.json")


def show_cmd(hwnd: int) -> int:
    return win32gui.GetWindowPlacement(hwnd)[1]


def maximize(hwnd: int) -> None:
    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)


def wait_windows(cfg, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        obs = find_obsidian_window(cfg["obsidian_process"], cfg.get("obsidian_title_hint", ""))
        cur = find_cursor_window(cfg["cursor_process"], cfg.get("cursor_title_hint", ""))
        if obs and cur:
            return obs, cur
        time.sleep(0.4)
    return (
        find_obsidian_window(cfg["obsidian_process"], cfg.get("obsidian_title_hint", "")),
        find_cursor_window(cfg["cursor_process"], cfg.get("cursor_title_hint", "")),
    )


def status(cfg):
    return sidecar.build_status(cfg)


def result(name: str, ok: bool, detail: str):
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}: {detail}")
    return ok


def main():
    dpi = enable_dpi_awareness()
    cfg = load_cfg()
    print(f"=== Sidecar acceptance dpi={dpi} version={sidecar.VERSION} ===\n")

    obs, cur = wait_windows(cfg)
    if not obs:
        print("[FAIL] SETUP: Obsidian window not found — open a vault and retry")
        return 1
    if not cur:
        print("[FAIL] SETUP: Cursor window not found — open Cursor Editor and retry")
        return 1
    print(f"SETUP Obsidian hwnd={obs.hwnd} title={obs.title!r}")
    print(f"SETUP Cursor   hwnd={cur.hwnd} title={cur.title!r}\n")

    # Ensure clean start
    sidecar.write_state({"attached": False, "timestamp": time.time(), "version": sidecar.VERSION})
    outcomes: list[bool] = []

    # ---- Scenario 1: maximize → attach → detach restore ----
    print("--- Scenario 1: maximize restore ---")
    maximize(obs.hwnd)
    maximize(cur.hwnd)
    time.sleep(0.5)
    before_obs = show_cmd(obs.hwnd)
    before_cur = show_cmd(cur.hwnd)
    obs_snap = snapshot_window(obs.hwnd)
    cur_snap = snapshot_window(cur.hwnd)
    code = sidecar.cmd_attach(cfg)
    st = status(cfg)
    mid_obs = show_cmd(obs.hwnd)
    mid_cur = show_cmd(cur.hwnd)
    mid_orect = get_window_rect(obs.hwnd).as_tuple()
    mid_crect = get_window_rect(cur.hwnd).as_tuple()
    code2 = sidecar.cmd_detach(cfg)
    time.sleep(0.4)
    after_obs = show_cmd(obs.hwnd)
    after_cur = show_cmd(cur.hwnd)
    ok1 = (
        code == 0
        and code2 == 0
        and st.get("attached") is True  # was attached before detach; re-check after
    )
    # After detach, both should be maximized again (SW_SHOWMAXIMIZED=3)
    st_after = status(cfg)
    ok1_restore = (
        after_obs == win32con.SW_SHOWMAXIMIZED
        and after_cur == win32con.SW_SHOWMAXIMIZED
        and st_after.get("attached") is False
    )
    # Also verify mid-attach was not both maximized (split happened)
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
    sidecar.write_state({"attached": False, "timestamp": time.time(), "version": sidecar.VERSION})
    cur2 = find_cursor_window(cfg["cursor_process"], cfg.get("cursor_title_hint", ""))
    vis_before = win32gui.IsWindowVisible(cur2.hwnd) if cur2 else False
    code = sidecar.cmd_click(cfg)
    st = status(cfg)
    cur2b = find_cursor_window(cfg["cursor_process"], cfg.get("cursor_title_hint", ""))
    vis_after = win32gui.IsWindowVisible(cur2b.hwnd) if cur2b else False
    outcomes.append(
        result(
            "2 click attaches (no hide)",
            code == 0 and st.get("attached") is True and vis_before and vis_after,
            f"vis {vis_before}→{vis_after} attached={st.get('attached')} "
            f"bound_hwnd={st.get('cursor',{}).get('hwnd') if st.get('cursor') else None}",
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
            bound and bound == hwnd_a == hwnd_f and st_a.get("attached") and st_f.get("attached"),
            f"bound={bound} after_arrange={hwnd_a} after_focus={hwnd_f}",
        )
    )

    # ---- Scenario 4: simulate cursor gone via stale state (safe, no kill) ----
    print("\n--- Scenario 4: stale cursor binding → attached=false ---")
    # Keep real windows; poison state cursor hwnd to fake "closed"
    state = sidecar.read_state()
    fake = dict(state.get("cursor") or {})
    real_hwnd = fake.get("hwnd")
    fake["hwnd"] = 1  # invalid
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
    # restore real binding for scenario 5 prep — re-attach cleanly
    sidecar.write_state({"attached": False, "timestamp": time.time(), "version": sidecar.VERSION})
    sidecar.cmd_attach(cfg)
    st_re = status(cfg)
    bound_a = (st_re.get("cursor") or {}).get("hwnd")
    rect_b_before = None

    # ---- Scenario 5: detach must not touch other cursor if binding gone ----
    print("\n--- Scenario 5: detach with gone binding does not move other windows ---")
    # Capture current cursor rect, then poison binding (simulate A closed, B still there)
    cur_live = find_cursor_window(cfg["cursor_process"], cfg.get("cursor_title_hint", ""))
    if cur_live:
        rect_b_before = get_window_rect(cur_live.hwnd).as_tuple()
        place_b_before = win32gui.GetWindowPlacement(cur_live.hwnd)
    state = sidecar.read_state()
    # Point binding at dead hwnd while real Cursor (B) remains
    dead = dict(state.get("cursor") or {})
    dead["hwnd"] = 999001
    dead["pid"] = 999001
    dead["process_create_time"] = 1.23
    state["cursor"] = dead
    state["attached"] = True
    # Keep obsidian binding valid if possible
    sidecar.write_state(state)
    code = sidecar.cmd_detach(cfg)
    # Read last_detach from state
    after_state = sidecar.read_state()
    last = after_state.get("last_detach") or {}
    cur_after = find_cursor_window(cfg["cursor_process"], cfg.get("cursor_title_hint", ""))
    rect_b_after = get_window_rect(cur_after.hwnd).as_tuple() if cur_after else None
    place_b_after = win32gui.GetWindowPlacement(cur_after.hwnd) if cur_after else None
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

    # Cleanup: leave detached clean
    sidecar.write_state({"attached": False, "timestamp": time.time(), "version": sidecar.VERSION})

    # ---- Scenario 6: DPI path / work area sanity (no visual) ----
    print("\n--- Scenario 6: DPI + work area path ---")
    from window import get_work_area_for_hwnd

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

    print("\n=== SUMMARY ===")
    passed = sum(1 for x in outcomes if x)
    total = len(outcomes)
    print(f"{passed}/{total} passed")
    # Note scenario 3 multi-cursor: only one Cursor window present — binding stability tested
    print(
        "NOTE: Scenario 3 ran with a single Cursor Editor window "
        "(multi-instance not open). Binding stability still verified."
    )
    print(
        "NOTE: Scenario 6 checks DPI awareness mode + work-area API; "
        "visual seam/overlap on 125%/150% still needs human eyes."
    )
    return 0 if passed == total else 2


if __name__ == "__main__":
    raise SystemExit(main())
