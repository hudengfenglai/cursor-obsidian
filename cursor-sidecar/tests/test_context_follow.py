"""v0.6 Context Follow — pure logic + silent sync contract tests."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock

import context_follow as cf
import main as sidecar
from editor_bridge import EditorBridge


def test_debounce_only_latest_fires():
    box = cf.DebounceBox(delay_ms=150)
    g1 = box.schedule()
    g2 = box.schedule()
    assert not box.is_current(g1)
    assert box.is_current(g2)


def test_same_file_suppression():
    ok, reason = cf.should_accept_sync(
        enabled=True,
        attached=True,
        path=r"D:\vault\a.md",
        line=1,
        last_path=r"D:\vault\a.md",
        last_line=1,
        last_sync_at=100.0,
        now=100.2,
        suppress_window_s=0.4,
    )
    assert ok is False
    assert reason == "duplicate"


def test_context_follow_disabled():
    ok, reason = cf.should_accept_sync(
        enabled=False, attached=True, path=r"D:\vault\a.md"
    )
    assert ok is False
    assert reason == "disabled"


def test_detached_suppression():
    ok, reason = cf.should_accept_sync(
        enabled=True, attached=False, path=r"D:\vault\a.md"
    )
    assert ok is False
    assert reason == "detached"


def test_stale_binding_suppression():
    ok, reason = cf.should_accept_sync(
        enabled=True, attached=True, path=r"D:\vault\a.md", binding_ok=False
    )
    assert ok is False
    assert reason == "stale_binding"


def test_non_file_empty_path():
    ok, reason = cf.should_accept_sync(enabled=True, attached=True, path=None)
    assert ok is False
    assert reason == "no_path"


def test_excluded_obsidian_config_paths():
    assert cf.is_excluded_rel_path(".obsidian/app.json") is True
    assert cf.is_excluded_rel_path(".obsidian/plugins/x/data.json") is True
    assert cf.is_excluded_rel_path("notes/a.md") is False
    assert cf.is_excluded_rel_path("中文/笔记.md") is False
    assert cf.is_excluded_rel_path(".trash/x.md") is True


def test_vault_path_validation():
    import shutil

    base = Path(__file__).resolve().parent / "_tmp_cf_vault"
    if base.exists():
        shutil.rmtree(base, ignore_errors=True)
    vault = base / "vault"
    vault.mkdir(parents=True)
    try:
        f = vault / "ok.md"
        f.write_text("x", encoding="utf-8")
        assert cf.is_path_inside_vault(vault, f) is True
        assert cf.is_path_inside_vault(vault, base / "outside.md") is False
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_markdown_line_column_gate():
    assert cf.should_sync_line_column(extension="md", is_markdown_editor=True) is True
    assert cf.should_sync_line_column(extension="pdf", is_markdown_editor=False) is False
    assert cf.should_sync_line_column(extension="md", is_markdown_editor=False) is False


def test_explicit_open_focus_true(monkeypatch):
    calls = {"focus": 0}

    def fake_focus(_h):
        calls["focus"] += 1

    bridge = EditorBridge(
        validate_binding=lambda b: {"ok": True, "hwnd": 1, "pid": 2},
        focus_hwnd=fake_focus,
        list_cursor_hwnds=lambda _n: [1],
        focus_settle_s=0,
        routing_wait_s=0,
    )
    monkeypatch.setattr(
        "editor_bridge.get_bound_cursor_executable",
        lambda binding, validate_fn=None: {
            "ok": True,
            "exe": r"C:\Cursor\Cursor.exe",
            "hwnd": 1,
            "pid": 2,
        },
    )
    monkeypatch.setattr(
        "editor_bridge.resolve_vault_file",
        lambda vault, path: Path(path),
    )
    monkeypatch.setattr(
        bridge,
        "_invoke_with_fallback",
        lambda *a, **k: {"ok": True, "method": "exe_goto"},
    )
    r = bridge.open_file(
        binding={"hwnd": 1, "pid": 2},
        vault_root=r"D:\vault",
        path=r"D:\vault\a.md",
        line=2,
        focus=True,
        preserve_foreground=False,
    )
    assert r["ok"] is True
    assert r["focused"] is True
    assert calls["focus"] == 1


def test_automatic_sync_focus_false(monkeypatch):
    calls = {"focus": 0, "restore": 0}

    def fake_focus(_h):
        calls["focus"] += 1

    bridge = EditorBridge(
        validate_binding=lambda b: {"ok": True, "hwnd": 1, "pid": 2},
        focus_hwnd=fake_focus,
        list_cursor_hwnds=lambda _n: [1],
        focus_settle_s=0,
        routing_wait_s=0,
    )
    monkeypatch.setattr(
        "editor_bridge.get_bound_cursor_executable",
        lambda binding, validate_fn=None: {
            "ok": True,
            "exe": r"C:\Cursor\Cursor.exe",
            "hwnd": 1,
            "pid": 2,
        },
    )
    monkeypatch.setattr(
        "editor_bridge.resolve_vault_file",
        lambda vault, path: Path(path),
    )
    monkeypatch.setattr(
        bridge,
        "_invoke_with_fallback",
        lambda *a, **k: {"ok": True, "method": "exe_file"},
    )

    def restore(hwnd):
        calls["restore"] += 1
        return {"stolen": False, "restored": False, "foreground_after": hwnd, "original": hwnd}

    r = bridge.open_file(
        binding={"hwnd": 1, "pid": 2},
        vault_root=r"D:\vault",
        path=r"D:\vault\a.md",
        focus=False,
        preserve_foreground=True,
        get_foreground_hwnd=lambda: 99,
        restore_if_stolen=restore,
    )
    assert r["ok"] is True
    assert r["focused"] is False
    assert calls["focus"] == 0
    assert calls["restore"] == 1


def test_sync_rpc_requires_attached(monkeypatch):
    monkeypatch.setattr(
        sidecar,
        "refresh_attachment_truth",
        lambda: {"attached": False, "state_valid": True, "reason": "detached", "state": {}},
    )
    r = sidecar.sync_editor_file_result(
        {}, vault_root=r"D:\vault", path=r"D:\vault\a.md"
    )
    assert r["ok"] is False
    assert r["error"] == "sidecar_not_attached"
    assert r["cmd"] == "sync-editor-file"


def test_sync_rpc_excludes_obsidian_config(monkeypatch):
    monkeypatch.setattr(
        sidecar,
        "refresh_attachment_truth",
        lambda: {"attached": True, "state_valid": True, "reason": "ok", "state": {"cursor": {"hwnd": 1}}},
    )
    r = sidecar.sync_editor_file_result(
        {},
        vault_root=r"D:\vault",
        path=r"D:\vault\.obsidian\app.json",
        relative_path=".obsidian/app.json",
    )
    assert r["ok"] is False
    assert r["error"] == "path_excluded"


def test_sync_rpc_stale_binding(monkeypatch):
    monkeypatch.setattr(
        sidecar,
        "refresh_attachment_truth",
        lambda: {
            "attached": True,
            "state_valid": True,
            "reason": "ok",
            "state": {"cursor": {"hwnd": 1, "pid": 2}},
        },
    )
    monkeypatch.setattr(sidecar, "validate_window_binding", lambda b: {"ok": False, "reason": "gone"})
    monkeypatch.setattr(sidecar, "is_path_inside_vault", lambda v, p: True)
    r = sidecar.sync_editor_file_result(
        {},
        vault_root=r"D:\vault",
        path=r"D:\vault\a.md",
        relative_path="a.md",
    )
    assert r["ok"] is False
    assert r["error"] == "stale_binding"


def test_gate_record_and_status():
    gate = cf.ContextFollowGate(enabled=True)
    gate.record(r"D:\vault\a.md", 3, relative_path="a.md", now=123.0)
    st = gate.status_dict()
    assert st["enabled"] is True
    assert st["last_path"] == "a.md"
    assert st["last_sync_at"] == 123.0


def test_handle_rpc_sync_ignores_focus_true(monkeypatch):
    captured = {}

    def fake_sync(cfg, **kwargs):
        captured.update(kwargs)
        return {"ok": True, "cmd": "sync-editor-file", "focused": False}

    monkeypatch.setattr(sidecar, "sync_editor_file_result", fake_sync)
    r = sidecar.handle_rpc(
        {},
        {
            "cmd": "sync-editor-file",
            "vault_root": r"D:\vault",
            "path": r"D:\vault\a.md",
            "focus": True,  # must be ignored by RPC path
        },
    )
    assert r["ok"] is True
    assert "focus" not in captured  # sync_editor_file_result has no focus param
