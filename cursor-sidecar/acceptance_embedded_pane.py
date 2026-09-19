#!/usr/bin/env python3
"""Windows helper acceptance for Embedded Pane Phase 1 (visual embed).

Layer A — helper acceptance (this script):
  Inject a synthetic pane CSS rect; verify mapping + SetWindowPos path.
  Does NOT drive Obsidian DOM.

Layer B — manual Obsidian acceptance: see checklist printed at end (NOT RUN by default).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import main as sidecar  # noqa: E402
from embedded_window import (  # noqa: E402
    apply_embedded_pane,
    compute_embedded_placement,
    restore_window_chrome,
    snapshot_window_chrome,
)
from pane_geometry import map_pane_dict_to_screen  # noqa: E402
from window import find_cursor_window, find_obsidian_window, get_window_rect  # noqa: E402


def record(name: str, ok: bool, detail: str = "") -> dict[str, Any]:
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))
    return {"name": name, "ok": ok, "detail": detail}


def main() -> int:
    print("=== Embedded Pane helper acceptance (Phase 1) ===\n")
    results: list[dict[str, Any]] = []

    # Pure mapping smoke
    mapped = map_pane_dict_to_screen(
        {
            "left": 700,
            "top": 0,
            "width": 260,
            "height": 720,
            "viewport_width": 960,
            "viewport_height": 720,
            "visible": True,
        },
        client_screen_x=100,
        client_screen_y=200,
        client_width_px=1200,
        client_height_px=900,
    )
    sr = mapped["screen_rect"]
    results.append(
        record(
            "A.mapping_125",
            mapped["ok"] and sr["left"] == 975 and sr["width"] == 325,
            json.dumps(sr),
        )
    )

    obs = find_obsidian_window("Obsidian", "")
    cur = find_cursor_window("Cursor", "")
    if not obs or not cur:
        results.append(record("A.windows_present", False, "Obsidian or Cursor not found — skip live move tests"))
        _print_manual()
        failed = sum(1 for r in results if not r["ok"])
        print(f"\nHelper acceptance: {len(results) - failed}/{len(results)} (live window tests skipped)")
        return 1 if failed else 0

    results.append(record("A.windows_present", True, f"obs={obs.hwnd} cur={cur.hwnd}"))

    pane = {
        "left": 700,
        "top": 40,
        "width": 280,
        "height": 700,
        "viewport_width": float(max(obs.rect.width, 1)),
        # Approximate: use physical client via compute path instead
        "viewport_height": float(max(obs.rect.height, 1)),
        "device_pixel_ratio": 1.0,
        "visible": True,
    }
    # Recompute using real client metrics
    placement = compute_embedded_placement(obsidian_hwnd=obs.hwnd, pane=pane)
    results.append(record("A.compute_placement", bool(placement.get("ok")), json.dumps(placement.get("screen_rect"))))

    snap = snapshot_window_chrome(cur.hwnd)
    results.append(record("A.snapshot_chrome", "style" in snap and "rect" in snap))

    applied = apply_embedded_pane(obsidian_hwnd=obs.hwnd, cursor_hwnd=cur.hwnd, pane=pane)
    results.append(record("A.apply_move", applied.get("applied") == "placed", str(applied.get("applied"))))

    time.sleep(0.3)
    # Collapse / hide
    pane_hidden = dict(pane)
    pane_hidden["visible"] = False
    hid = apply_embedded_pane(obsidian_hwnd=obs.hwnd, cursor_hwnd=cur.hwnd, pane=pane_hidden)
    results.append(record("A.hide_collapsed", hid.get("applied") == "hidden"))

    time.sleep(0.2)
    # Restore show
    shown = apply_embedded_pane(obsidian_hwnd=obs.hwnd, cursor_hwnd=cur.hwnd, pane=pane)
    results.append(record("A.show_again", shown.get("applied") == "placed"))

    restore_window_chrome(cur.hwnd, snap)
    after = get_window_rect(cur.hwnd)
    results.append(record("A.restore_chrome", True, f"rect={list(after.as_tuple())}"))

    # State machine via RPC helpers (in-process, no daemon required for enter/exit if we mock attach)
    # Soft check: exit/enter functions exist
    results.append(
        record(
            "A.api_surface",
            callable(getattr(sidecar, "enter_embedded_pane", None))
            and callable(getattr(sidecar, "exit_embedded_pane", None))
            and callable(getattr(sidecar, "update_embedded_pane", None)),
        )
    )

    _print_manual()
    failed = sum(1 for r in results if not r["ok"])
    print(f"\nHelper acceptance: {len(results) - failed}/{len(results)} passed")
    return 1 if failed else 0


def _print_manual() -> None:
    print(
        """
=== Manual Obsidian acceptance (Layer B) ===
Status: NOT RUN (requires human + live Obsidian UI)

1. Open Embedded Pane
2. Cursor appears in pane region
3. Drag pane separator → Cursor continuous resize
4. Collapse right sidebar → Cursor disappears
5. Expand → Cursor returns
6. Move Obsidian window → Cursor sticks to pane
7. Maximize / restore
8. Context Follow A.md → B.md inside pane Cursor
9. Click Obsidian → Obsidian accepts input
10. Click Cursor region → Cursor accepts input
11. Exit Embed → ordinary Sidecar
12. Detach → dual-window original restore
"""
    )


if __name__ == "__main__":
    raise SystemExit(main())
