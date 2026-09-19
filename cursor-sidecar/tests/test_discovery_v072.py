"""v0.7.2 discovery / binding / classifier tests."""

from __future__ import annotations

from agents_auto import ensure_agents_window_bound, find_existing_agent, wait_for_agent_classified
from cursor_exe import resolve_cursor_executable
from agents_menu import normalize_menu_label
from cursor_windows import (
    ROLE_AGENT,
    ROLE_EDITOR,
    ROLE_UNKNOWN,
    agent_candidates,
    clear_agent_binding,
    clear_editor_binding,
    editor_candidates,
    refresh_cursor_bindings,
    select_editor_window,
)


def test_agent_never_in_editor_candidates():
    windows = [
        {"hwnd": 1, "role": ROLE_AGENT, "title": "Agents"},
        {"hwnd": 2, "role": ROLE_EDITOR, "title": "Editor"},
        {"hwnd": 3, "role": ROLE_UNKNOWN, "title": "x"},
    ]
    eds = editor_candidates(windows)
    assert all(w["role"] == ROLE_EDITOR for w in eds)
    assert 1 not in [w["hwnd"] for w in eds]


def test_select_editor_ambiguous(monkeypatch):
    classified = [
        {"hwnd": 10, "role": ROLE_EDITOR},
        {"hwnd": 11, "role": ROLE_EDITOR},
        {"hwnd": 12, "role": ROLE_AGENT},
    ]
    monkeypatch.setattr(
        "cursor_windows.list_cursor_windows_classified",
        lambda *_a, **_k: classified,
    )
    r = select_editor_window("Cursor.exe")
    assert r["ok"] is False
    assert r["error"] == "editor_window_ambiguous"


def test_select_editor_refuses_only_agent(monkeypatch):
    classified = [{"hwnd": 12, "role": ROLE_AGENT}]
    monkeypatch.setattr(
        "cursor_windows.list_cursor_windows_classified",
        lambda *_a, **_k: classified,
    )
    r = select_editor_window("Cursor.exe")
    assert r["ok"] is False
    assert r["error"] == "editor_window_not_found"


def test_stale_agent_atomic_clear_keeps_editor(monkeypatch):
    state = {
        "cursor_editor": {"hwnd": 1, "pid": 1, "process": "Cursor.exe", "process_create_time": 1.0},
        "cursor_agent": {"hwnd": 2, "pid": 2, "process": "Cursor.exe", "process_create_time": 1.0},
        "embedded": True,
        "native_child": True,
        "embed_mode": "pane",
    }

    def fake_validate(rec):
        if rec and int(rec.get("hwnd") or 0) == 2:
            return {"ok": False, "reason": "hwnd_gone"}
        return {"ok": True}

    monkeypatch.setattr("cursor_windows.validate_window_binding", fake_validate)
    monkeypatch.setattr("cursor_windows.classify_cursor_window", lambda *_a, **_k: ROLE_EDITOR)
    r = refresh_cursor_bindings(state)
    assert r["cleared_agent"] is True
    assert r["editor_ok"] is True
    assert state["cursor_agent"] is None
    assert state["embedded"] is False
    assert state["native_child"] is False
    assert state["cursor_editor"]["hwnd"] == 1


def test_stale_editor_does_not_clear_agent(monkeypatch):
    state = {
        "cursor_editor": {"hwnd": 1, "pid": 1, "process": "Cursor.exe"},
        "cursor_agent": {"hwnd": 2, "pid": 2, "process": "Cursor.exe"},
        "embedded": False,
    }

    def fake_validate(rec):
        if rec and int(rec.get("hwnd") or 0) == 1:
            return {"ok": False, "reason": "hwnd_gone"}
        return {"ok": True}

    monkeypatch.setattr("cursor_windows.validate_window_binding", fake_validate)
    monkeypatch.setattr("cursor_windows.classify_cursor_window", lambda h: ROLE_AGENT if h == 2 else ROLE_UNKNOWN)
    r = refresh_cursor_bindings(state)
    assert r["cleared_editor"] is True
    assert r["agent_ok"] is True
    assert state["cursor_editor"] is None
    assert state["cursor_agent"]["hwnd"] == 2


def test_reuse_hidden_agent():
    classified = [
        {"hwnd": 10, "role": ROLE_EDITOR},
        {"hwnd": 20, "role": ROLE_AGENT, "minimized": True, "visible": False},
    ]
    r = find_existing_agent(
        process_name="Cursor.exe",
        editor_hwnd_i=10,
        classify_list_fn=lambda _p: classified,
    )
    assert r["ok"] is True
    assert r["candidate"]["hwnd"] == 20


def test_menu_disabled_then_reuse(monkeypatch):
    state = {
        "cursor_editor": {
            "hwnd": 10,
            "pid": 1,
            "process": "Cursor.exe",
            "process_create_time": 1.0,
        },
        "cursor_agent": None,
    }
    classified = [
        {"hwnd": 10, "role": ROLE_EDITOR},
        {"hwnd": 30, "role": ROLE_AGENT},
    ]

    monkeypatch.setattr(
        "agents_auto.refresh_cursor_bindings",
        lambda st: {"editor_ok": True, "agent_ok": False},
    )
    monkeypatch.setattr("agents_auto.agent_binding_ok", lambda st: {"ok": False})
    monkeypatch.setattr("agents_auto.editor_hwnd", lambda st: 10)
    monkeypatch.setattr("agents_auto.win32gui.IsWindow", lambda _h: True)
    monkeypatch.setattr("agents_auto.get_foreground_hwnd", lambda: 1)
    monkeypatch.setattr("agents_auto.restore_foreground_hwnd", lambda _h: True)
    monkeypatch.setattr("agents_auto.show_window_noactivate", lambda _h: None)

    def classify(_p):
        return classified

    def trigger(_eh):
        return {"ok": False, "error": "menu_item_disabled", "should_rescan": True}

    def bind(hwnd, process_name="Cursor.exe"):
        return {"hwnd": hwnd, "pid": 1, "role": "agent"}

    r = ensure_agents_window_bound(
        {"cursor_process": "Cursor.exe"},
        state,
        classify_list_fn=classify,
        list_fn=lambda _p: [{"hwnd": 10}, {"hwnd": 30}],
        bind_fn=bind,
        trigger_fn=trigger,
    )
    assert r["ok"] is True
    assert r["source"] in ("reuse_after_menu_disabled", "reuse_existing_agent")
    assert r["agent_hwnd"] == 30


def test_wait_allows_auxiliary_hwnds():
    before = [{"hwnd": 1}, {"hwnd": 2}]

    def classify(_p):
        return [
            {"hwnd": 1, "role": ROLE_EDITOR},
            {"hwnd": 2, "role": ROLE_UNKNOWN},
            {"hwnd": 99, "role": ROLE_UNKNOWN},  # aux
            {"hwnd": 55, "role": ROLE_AGENT},
        ]

    r = wait_for_agent_classified(
        process_name="Cursor.exe",
        editor_hwnd_i=1,
        before=before,
        timeout_s=1.0,
        poll_s=0.01,
        classify_list_fn=classify,
    )
    assert r["ok"] is True
    assert r["candidate"]["hwnd"] == 55


def test_menu_label_normalization():
    assert normalize_menu_label("New &Agents Window\tCtrl+...") == "new agents window"


def test_persisted_verified_exe(monkeypatch, tmp_path_factory):
    try:
        root = tmp_path_factory.mktemp("curexe")
    except Exception:
        import tempfile
        from pathlib import Path

        root = Path(tempfile.mkdtemp())
    exe = root / "Cursor.exe"
    exe.write_bytes(b"mz")
    cfg = {"cursor_exe_verified": str(exe)}
    monkeypatch.setattr("cursor_exe._registry_candidates", lambda: [])
    monkeypatch.setattr("cursor_exe._path_which", lambda: None)
    monkeypatch.setattr("cursor_exe._persist_verified", lambda *_a, **_k: None)
    r = resolve_cursor_executable(cfg, persist=False)
    assert r["ok"] is True
    assert r["source"] == "persisted_verified"


def test_electron_editor_title_classifies_editor(monkeypatch):
    monkeypatch.setattr("cursor_windows.win32gui.IsWindow", lambda _h: True)
    monkeypatch.setattr("cursor_windows._menu_labels", lambda _h: [])
    monkeypatch.setattr("cursor_windows._uia_name_hits", lambda _h: {})
    from cursor_windows import score_cursor_window, ROLE_EDITOR

    r = score_cursor_window(1, title="init_skill.py - cursor-obsidian - Cursor")
    assert r["role"] == ROLE_EDITOR
    assert r["editor_score"] >= 2
    assert "title_electron_editor" in r["signals"]


def test_cursor_agents_title_classifies_agent(monkeypatch):
    monkeypatch.setattr("cursor_windows.win32gui.IsWindow", lambda _h: True)
    monkeypatch.setattr("cursor_windows._menu_labels", lambda _h: [])
    monkeypatch.setattr("cursor_windows._uia_name_hits", lambda _h: {})
    from cursor_windows import score_cursor_window, ROLE_AGENT

    r = score_cursor_window(1, title="Cursor Agents")
    assert r["role"] == ROLE_AGENT


def test_wait_sole_newcomer_non_editor():
    before = [{"hwnd": 10}]
    n = {"i": 0}

    def classify(_p):
        n["i"] += 1
        if n["i"] < 2:
            return [{"hwnd": 10, "role": ROLE_EDITOR}]
        return [
            {"hwnd": 10, "role": ROLE_EDITOR},
            {"hwnd": 99, "role": "UNKNOWN", "title": "Cursor"},
        ]

    r = wait_for_agent_classified(
        process_name="Cursor.exe",
        editor_hwnd_i=10,
        before=before,
        timeout_s=1.0,
        poll_s=0.01,
        classify_list_fn=classify,
    )
    assert r["ok"] is True
    assert r["candidate"]["hwnd"] == 99
    assert r["match"] == "sole_newcomer_non_editor"
