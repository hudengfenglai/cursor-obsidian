#!/usr/bin/env python3
"""Native child SetParent acceptance helpers (Layer A).

Does NOT drive Obsidian DOM. Manual UX checklist printed as NOT RUN by default.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from native_embed import style_for_native_child, style_has_child, style_has_popup  # noqa: E402
from pane_geometry import map_pane_dict_to_client  # noqa: E402


def record(name: str, ok: bool, detail: str = "") -> dict:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return {"name": name, "ok": ok, "detail": detail}


def main() -> int:
    print("=== Native Child helper acceptance (spike) ===\n")
    results = []

    style = style_for_native_child(0x80000000 | 0x00C00000 | 0x10000000)
    results.append(record("A.style_ws_child", style_has_child(style) and not style_has_popup(style), hex(style)))

    mapped = map_pane_dict_to_client(
        {
            "left": 700,
            "top": 0,
            "width": 260,
            "height": 720,
            "viewport_width": 960,
            "viewport_height": 720,
            "visible": True,
        },
        client_width_px=1200,
        client_height_px=900,
    )
    cr = mapped["client_rect"]
    results.append(
        record(
            "A.client_mapping",
            mapped["ok"] and cr["left"] == 875 and cr["width"] == 325,
            json.dumps(cr),
        )
    )

    print(
        """
=== Manual UX acceptance (Layer B) ===
Status: NOT RUN

A. Attach Editor  B. Bind New Agents Window  C. Open Agents Pane
D. Enter Native Agents Embed
E. GetParent(agent)==obsidian  F. WS_CHILD set / WS_POPUP clear
G-H. Resize follows  I-J. Exit restores parent/styles/rect
1-15. Focus/IME/palette/modals/popups/DPI/recover — see project notes
"""
    )
    failed = sum(1 for r in results if not r["ok"])
    print(f"Helper acceptance: {len(results) - failed}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
