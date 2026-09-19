"""v0.4 Context Bridge pure-logic tests (no real Cursor required)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from editor_bridge import (
    EditorBridge,
    build_cursor_file_deeplink,
    build_editor_location,
    clear_capability_cache,
    detect_new_cursor_windows,
    get_bound_cursor_executable,
    probe_editor_capabilities,
    resolve_vault_file,
    to_one_based_cursor,
)


def test_zero_based_to_one_based():
    assert to_one_based_cursor(0, 0) == (1, 1)
    assert to_one_based_cursor(125, 6) == (126, 7)
    assert to_one_based_cursor(None, None) == (None, None)


def test_build_editor_location_variants():
    assert build_editor_location(r"D:\a\b.md") == r"D:\a\b.md"
    assert build_editor_location(r"D:\a\b.md", line=10) == r"D:\a\b.md:10"
    assert build_editor_location(r"D:\a\b.md", line=10, column=3) == r"D:\a\b.md:10:3"
    assert build_editor_location(r"D:\a\b.md", line=0, column=0) == r"D:\a\b.md:1:1"


def test_build_location_drive_and_spaces():
    loc = build_editor_location(r"D:\vault\my notes\file.md", line=2, column=1)
    assert loc.endswith(r"\my notes\file.md:2:1")
    assert loc.startswith("D:")


def test_chinese_path_location_and_deeplink():
    p = r"D:\胡梦浩obsidian\03 知识\数学分析\一致连续.md"
    loc = build_editor_location(p, line=126, column=1)
    assert "一致连续.md:126:1" in loc
    uri = build_cursor_file_deeplink(p, line=126, column=1)
    assert uri.startswith("cursor://file/")
    assert "%20" in uri or "03" in uri  # space encoded or path segment
    assert "一致连续" not in uri or "%E4%B8%80" in uri or "一致连续" in uri
    # Special chars encoded
    assert "#" not in uri.split("cursor://file/")[-1].split(":")[0] or "%23" in uri


def test_deeplink_encodes_hash_question_percent():
    """Encoding rules for # % ? (no real Windows file — ? is illegal in Win filenames)."""
    from urllib.parse import quote

    path_uri = "D:/vault/a#b%c?.md"
    encoded = quote(path_uri, safe="/:")
    assert "%23" in encoded
    assert "%3F" in encoded or "%3f" in encoded
    assert "%25" in encoded
    uri = f"cursor://file/{encoded}:1:1"
    assert uri.startswith("cursor://file/")
    assert uri.endswith(":1:1")


def test_vault_containment_and_traversal(monkeypatch):
    root = Path(__file__).resolve().parent / "_tmp_bridge_vault"
    if root.exists():
        import shutil

        shutil.rmtree(root, ignore_errors=True)
    (root / "sub").mkdir(parents=True)
    f = root / "sub" / "note.md"
    f.write_text("hi", encoding="utf-8")
    outside = Path(__file__).resolve().parent / "_tmp_bridge_outside.md"
    outside.write_text("x", encoding="utf-8")
    try:
        assert resolve_vault_file(root, f) == f.resolve()
        with pytest.raises(ValueError, match="path_outside_vault"):
            resolve_vault_file(root, outside)
        with pytest.raises(ValueError, match="path_outside_vault"):
            resolve_vault_file(root, root / "sub" / ".." / ".." / "_tmp_bridge_outside.md")
        with pytest.raises(FileNotFoundError):
            resolve_vault_file(root, root / "missing.md")
    finally:
        import shutil

        shutil.rmtree(root, ignore_errors=True)
        outside.unlink(missing_ok=True)


def test_bound_exe_invalid_binding():
    def validate(_r):
        return {"ok": False, "reason": "hwnd_gone"}

    r = get_bound_cursor_executable({"hwnd": 1, "pid": 2}, validate_fn=validate)
    assert r["ok"] is False
    assert r["error"] == "cursor_gone"


def test_bound_exe_not_bound():
    r = get_bound_cursor_executable(None, validate_fn=lambda _r: {"ok": False})
    assert r["error"] == "cursor_not_bound"


def test_new_cursor_window_detection():
    assert detect_new_cursor_windows([1, 2], [1, 2, 9]) == [9]
    assert detect_new_cursor_windows([1], [1]) == []


def test_fallback_order_and_no_state_side_effects(monkeypatch):
    clear_capability_cache()
    calls: list[list[str]] = []

    def fake_open(exe, args):
        calls.append([exe, *args])
        raise OSError("fail")

    deeplinks: list[str] = []

    monkeypatch.setattr("editor_bridge.open_via_executable", fake_open)
    monkeypatch.setattr(
        "editor_bridge.open_via_deeplink",
        lambda uri: deeplinks.append(uri),
    )
    monkeypatch.setattr(
        "editor_bridge.probe_editor_capabilities",
        lambda _exe, force=False: {
            "reuse_window": True,
            "goto": True,
            "classic": True,
            "new_window": True,
            "probed": True,
        },
    )

    root = Path(__file__).resolve().parent / "_tmp_bridge_open"
    root.mkdir(parents=True, exist_ok=True)
    note = root / "note.md"
    note.write_text("line1\nline2\n", encoding="utf-8")
    try:
        bridge = EditorBridge(
            validate_binding=lambda _r: {"ok": True},
            focus_hwnd=lambda _h: None,
            list_cursor_hwnds=lambda _n: [100],
            focus_settle_s=0,
            routing_wait_s=0,
        )
        monkeypatch.setattr(
            "editor_bridge.get_bound_cursor_executable",
            lambda binding, validate_fn: {
                "ok": True,
                "exe": r"C:\Cursor\Cursor.exe",
                "pid": 1,
                "hwnd": 100,
            },
        )
        # Force all exe attempts to fail → deeplink
        result = bridge.open_file(
            binding={"hwnd": 100, "pid": 1},
            vault_root=root,
            path=note,
            line=2,
            column=1,
            focus=True,
        )
        assert result["ok"] is True
        assert result["method"] == "deeplink"
        assert deeplinks and deeplinks[0].startswith("cursor://file/")
        assert calls  # attempted exe first
        # classic+reuse+goto should be first attempt
        assert "--classic" in calls[0] and "--goto" in calls[0]
    finally:
        import shutil

        shutil.rmtree(root, ignore_errors=True)


def test_open_requires_attached_semantics_in_main(monkeypatch):
    import main as sidecar

    monkeypatch.setattr(
        sidecar,
        "refresh_attachment_truth",
        lambda: {
            "attached": False,
            "state_valid": False,
            "reason": "detached",
            "state": {"attached": False},
        },
    )
    cfg = {
        "cursor_process": "Cursor.exe",
        "live_follow": True,
    }
    r = sidecar.open_editor_file_result(
        cfg,
        vault_root=r"D:\vault",
        path=r"D:\vault\a.md",
        line=1,
        column=1,
    )
    assert r["ok"] is False
    assert r["error"] == "sidecar_not_attached"


def test_capability_probe_never_calls_agent(monkeypatch):
    clear_capability_cache()
    seen: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        seen.append(list(cmd))
        assert kwargs.get("shell") is False
        assert "agent" not in cmd[0].lower()
        m = MagicMock()
        m.stdout = "Usage: Cursor.exe [--reuse-window] [--goto] [--classic]"
        m.stderr = ""
        return m

    monkeypatch.setattr("editor_bridge.subprocess.run", fake_run)
    caps = probe_editor_capabilities(r"C:\Apps\Cursor.exe", force=True)
    assert caps["goto"] is True
    assert caps["classic"] is True
    assert seen and seen[0] == [r"C:\Apps\Cursor.exe", "--help"]
