"""v0.6 Context Follow — latest-wins, focus policy, silent sync tests."""

from __future__ import annotations

import shutil
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import context_follow as cf
import context_sync as cs
import main as sidecar
from context_sync import ContextSyncController, SyncJob, should_restore_obsidian_focus
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
    ok, reason = cf.should_accept_sync(enabled=False, attached=True, path=r"D:\vault\a.md")
    assert ok is False
    assert reason == "disabled"


def test_detached_suppression():
    ok, reason = cf.should_accept_sync(enabled=True, attached=False, path=r"D:\vault\a.md")
    assert ok is False
    assert reason == "detached"


def test_stale_binding_suppression():
    ok, reason = cf.should_accept_sync(
        enabled=True, attached=True, path=r"D:\vault\a.md", binding_ok=False
    )
    assert ok is False
    assert reason == "stale_binding"


def test_excluded_obsidian_config_paths():
    assert cf.is_excluded_rel_path(".obsidian/app.json") is True
    assert cf.is_excluded_rel_path("notes/a.md") is False
    assert cf.is_excluded_rel_path("中文/笔记.md") is False


def test_vault_path_validation():
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


def test_focus_restore_obsidian_to_cursor():
    assert (
        should_restore_obsidian_focus(
            foreground_before=10,
            foreground_after=20,
            obsidian_hwnd=10,
            cursor_hwnd=20,
        )
        is True
    )


def test_focus_restore_obsidian_to_chrome_noop():
    assert (
        should_restore_obsidian_focus(
            foreground_before=10,
            foreground_after=99,
            obsidian_hwnd=10,
            cursor_hwnd=20,
        )
        is False
    )


def test_next_context_sync_seq_rebases_after_plugin_reload():
    # local reset to 0, daemon already at 80
    nxt = cs.next_context_sync_seq(0, 80, now_ms=1_700_000_000_000)
    assert nxt > 80
    assert nxt == max(0, 80, 1_700_000_000_000 * 1000) + 1


def test_next_context_sync_seq_prefers_local_when_ahead():
    # clock below both counters
    nxt = cs.next_context_sync_seq(500, 80, now_ms=0)
    assert nxt == 501


def test_stale_discarded_does_not_update_gate():
    gate = cf.ContextFollowGate(enabled=True)
    gate.record(r"D:\vault\a.md", 1, relative_path="a.md", now=10.0)
    # Simulate plugin: discarded response must not overwrite last sync
    discarded = {"ok": True, "queued": False, "discarded": True, "reason": "stale_seq"}
    if discarded.get("discarded") is True or discarded.get("queued") is False:
        pass  # no gate.record
    else:
        gate.record(r"D:\vault\b.md", 1, relative_path="b.md", now=20.0)
    assert gate.last_relative_path == "a.md"
    assert gate.last_sync_at == 10.0


def test_stale_seq_retry_once_logic():
    # First response discarded; rebase then second succeeds
    daemon_latest = 100
    local = 0
    seq1 = cs.next_context_sync_seq(local, daemon_latest, now_ms=2)
    assert seq1 > daemon_latest
    # If somehow still stale (daemon jumped), rebase once more from returned latest
    returned_latest = 200
    seq2 = cs.next_context_sync_seq(seq1, returned_latest, now_ms=2)
    assert seq2 > returned_latest
    # Only one retry in plugin — third not automatic
    assert seq2 != seq1


def test_python_fallback_seq_no_modulo():
    import inspect

    src = inspect.getsource(sidecar.sync_editor_file_result)
    assert "% 2_000_000_000" not in src
    assert "next_context_sync_seq" in src


def test_python_fallback_seq_not_stale_vs_plugin_scale(monkeypatch):
    """RPC without seq must still queue after plugin-scale latest_seq."""
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
    monkeypatch.setattr(sidecar, "validate_window_binding", lambda b: {"ok": True})
    monkeypatch.setattr(sidecar, "is_path_inside_vault", lambda v, p: True)
    monkeypatch.setattr(sidecar, "_execute_context_sync_job", lambda job: {"ok": True, "seq": job.seq})

    ctrl = ContextSyncController(execute=lambda job: {"ok": True, "seq": job.seq})
    # Simulate prior plugin sync at Date.now()*1000 scale
    plugin_scale = 1_700_000_000_000_001
    ctrl.submit(SyncJob(seq=plugin_scale, vault_root="v", path="prior.md"))
    deadline = time.time() + 1.0
    while time.time() < deadline and ctrl.status()["running"]:
        time.sleep(0.01)
    sidecar._CONTEXT_SYNC = ctrl

    r = sidecar.sync_editor_file_result(
        {},
        vault_root=r"D:\vault",
        path=r"D:\vault\a.md",
        relative_path="a.md",
        seq=None,
    )
    assert r.get("ok") is True
    assert r.get("queued") is True
    assert r.get("discarded") is not True
    assert int(r.get("seq") or 0) > plugin_scale


def test_pending_b_replaced_by_c():
    executed: list[int] = []
    barrier = threading.Event()

    def execute(job: SyncJob):
        if job.seq == 1:
            barrier.wait(timeout=2.0)
        executed.append(job.seq)
        return {"ok": True, "seq": job.seq}

    ctrl = ContextSyncController(execute=execute)
    assert ctrl.submit(SyncJob(seq=1, vault_root="v", path="A")).get("queued") is True
    time.sleep(0.05)
    assert ctrl.submit(SyncJob(seq=2, vault_root="v", path="B")).get("queued") is True
    assert ctrl.submit(SyncJob(seq=3, vault_root="v", path="C")).get("queued") is True
    barrier.set()
    deadline = time.time() + 2.0
    while time.time() < deadline and ctrl.status()["running"]:
        time.sleep(0.02)
    assert 2 not in executed
    assert 3 in executed
    assert executed[-1] == 3


def test_stale_seq_discarded():
    executed: list[int] = []

    def execute(job: SyncJob):
        executed.append(job.seq)
        return {"ok": True}

    ctrl = ContextSyncController(execute=execute)
    ctrl.submit(SyncJob(seq=10, vault_root="v", path="A"))
    time.sleep(0.05)
    out = ctrl.submit(SyncJob(seq=5, vault_root="v", path="old"))
    assert out.get("discarded") is True
    deadline = time.time() + 1.0
    while time.time() < deadline and ctrl.status()["running"]:
        time.sleep(0.02)
    assert 5 not in executed


def test_clear_pending():
    ctrl = ContextSyncController(execute=lambda j: {"ok": True})
    # Force pending without starting by setting state carefully
    ctrl.submit(SyncJob(seq=1, vault_root="v", path="A"))
    ctrl.clear_pending()
    st = ctrl.status()
    # running may still be true briefly; pending must be false
    assert st["pending"] is False


def test_background_routing_check_false(monkeypatch):
    calls = {"focus": 0, "list": 0}

    bridge = EditorBridge(
        validate_binding=lambda b: {"ok": True, "hwnd": 1, "pid": 2},
        focus_hwnd=lambda _h: calls.__setitem__("focus", calls["focus"] + 1),
        list_cursor_hwnds=lambda _n: calls.__setitem__("list", calls["list"] + 1) or [1],
        focus_settle_s=0,
        routing_wait_s=0.5,
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
    monkeypatch.setattr("editor_bridge.resolve_vault_file", lambda vault, path: Path(path))
    monkeypatch.setattr(bridge, "_invoke_with_fallback", lambda *a, **k: {"ok": True, "method": "exe_file"})

    r = bridge.open_file(
        binding={"hwnd": 1, "pid": 2},
        vault_root=r"D:\vault",
        path=r"D:\vault\a.md",
        focus=False,
        routing_check=False,
        preserve_foreground=False,
    )
    assert r["ok"] is True
    assert r["routing_check"] is False
    assert calls["focus"] == 0
    assert calls["list"] == 0  # no before/after enumeration


def test_explicit_open_focus_true(monkeypatch):
    calls = {"focus": 0}

    bridge = EditorBridge(
        validate_binding=lambda b: {"ok": True, "hwnd": 1, "pid": 2},
        focus_hwnd=lambda _h: calls.__setitem__("focus", calls["focus"] + 1),
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
    monkeypatch.setattr("editor_bridge.resolve_vault_file", lambda vault, path: Path(path))
    monkeypatch.setattr(bridge, "_invoke_with_fallback", lambda *a, **k: {"ok": True, "method": "exe_goto"})
    r = bridge.open_file(
        binding={"hwnd": 1},
        vault_root=r"D:\v",
        path=r"D:\v\a.md",
        focus=True,
        routing_check=True,
    )
    assert r["focused"] is True
    assert calls["focus"] == 1


def test_sync_rpc_requires_attached(monkeypatch):
    monkeypatch.setattr(
        sidecar,
        "refresh_attachment_truth",
        lambda: {"attached": False, "state_valid": True, "reason": "detached", "state": {}},
    )
    r = sidecar.sync_editor_file_result({}, vault_root=r"D:\vault", path=r"D:\vault\a.md", seq=1)
    assert r["ok"] is False
    assert r["error"] == "sidecar_not_attached"


def test_sync_rpc_queues(monkeypatch):
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
    monkeypatch.setattr(sidecar, "validate_window_binding", lambda b: {"ok": True})
    monkeypatch.setattr(sidecar, "is_path_inside_vault", lambda v, p: True)
    monkeypatch.setattr(sidecar, "_execute_context_sync_job", lambda job: {"ok": True, "seq": job.seq})
    # reset controller
    sidecar._CONTEXT_SYNC = ContextSyncController(execute=lambda job: {"ok": True, "seq": job.seq})
    r = sidecar.sync_editor_file_result(
        {},
        vault_root=r"D:\vault",
        path=r"D:\vault\a.md",
        relative_path="a.md",
        seq=42,
    )
    assert r.get("ok") is True
    assert r.get("queued") is True
    assert r.get("seq") == 42


def test_handle_rpc_sync_passes_seq(monkeypatch):
    captured = {}

    def fake_sync(cfg, **kwargs):
        captured.update(kwargs)
        return {"ok": True, "queued": True, "seq": kwargs.get("seq")}

    monkeypatch.setattr(sidecar, "sync_editor_file_result", fake_sync)
    r = sidecar.handle_rpc(
        {},
        {
            "cmd": "sync-editor-file",
            "vault_root": r"D:\vault",
            "path": r"D:\vault\a.md",
            "seq": 7,
            "focus": True,
        },
    )
    assert r["ok"] is True
    assert captured.get("seq") == 7
