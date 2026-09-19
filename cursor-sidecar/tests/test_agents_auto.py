"""v0.7.1/0.7.2 auto-bind tests — inject classify / trigger (no live SendInput)."""

from __future__ import annotations

from agents_auto import ensure_agents_window_bound, wait_for_agent_classified
from cursor_windows import ROLE_AGENT, ROLE_EDITOR, ROLE_UNKNOWN


def test_ensure_auto_select_existing(monkeypatch):
    state = {
        "cursor_editor": {"hwnd": 10, "pid": 1, "process": "Cursor.exe", "process_create_time": 1.0},
        "cursor_agent": None,
    }
    monkeypatch.setattr(
        "agents_auto.refresh_cursor_bindings",
        lambda st: {"editor_ok": True, "agent_ok": False},
    )
    monkeypatch.setattr("agents_auto.agent_binding_ok", lambda st: {"ok": False})
    monkeypatch.setattr("agents_auto.editor_hwnd", lambda st: 10)
    monkeypatch.setattr("agents_auto.show_window_noactivate", lambda _h: None)

    classified = [
        {"hwnd": 10, "role": ROLE_EDITOR},
        {"hwnd": 20, "role": ROLE_AGENT, "title": "Agents"},
    ]

    def bind_fn(hwnd, process_name="Cursor.exe"):
        return {"hwnd": hwnd, "pid": 1, "title": "Agents", "role": "agent"}

    r = ensure_agents_window_bound(
        {"cursor_process": "Cursor.exe"},
        state,
        classify_list_fn=lambda _p: classified,
        list_fn=lambda _p: [{"hwnd": 10}, {"hwnd": 20}],
        bind_fn=bind_fn,
        trigger_fn=lambda *_a, **_k: {"ok": False},
    )
    assert r["ok"] is True
    assert r["source"] == "reuse_existing_agent"
    assert state["cursor_agent"]["hwnd"] == 20


def test_ensure_auto_launch_classified(monkeypatch):
    monkeypatch.setattr("agents_auto.win32gui.IsWindow", lambda _h: True)
    monkeypatch.setattr("agents_auto.get_foreground_hwnd", lambda: 1)
    monkeypatch.setattr("agents_auto.restore_foreground_hwnd", lambda _h: True)
    monkeypatch.setattr(
        "agents_auto.refresh_cursor_bindings",
        lambda st: {"editor_ok": True, "agent_ok": False},
    )
    monkeypatch.setattr("agents_auto.agent_binding_ok", lambda st: {"ok": False})
    monkeypatch.setattr("agents_auto.editor_hwnd", lambda st: 10)

    state = {"cursor_editor": {"hwnd": 10}, "cursor_agent": None}
    windows = [{"hwnd": 10, "role": ROLE_EDITOR}]

    def classify(_p):
        return list(windows)

    def bind_fn(hwnd, process_name="Cursor.exe"):
        return {"hwnd": hwnd, "pid": 7, "title": "New", "role": "agent"}

    def trigger(eh):
        windows.append({"hwnd": 55, "role": ROLE_AGENT, "title": "Agents"})
        return {"ok": True, "trigger_method": "win32_menu"}

    r = ensure_agents_window_bound(
        {"cursor_process": "Cursor.exe"},
        state,
        timeout_s=2.0,
        classify_list_fn=classify,
        list_fn=lambda _p: [{"hwnd": w["hwnd"]} for w in windows],
        bind_fn=bind_fn,
        trigger_fn=trigger,
    )
    assert r["ok"] is True
    assert r["source"] == "auto_launch_classified"
    assert r["agent_hwnd"] == 55
    assert r["trigger_method"] == "win32_menu"


def test_ensure_not_found(monkeypatch):
    monkeypatch.setattr("agents_auto.win32gui.IsWindow", lambda _h: True)
    monkeypatch.setattr("agents_auto.get_foreground_hwnd", lambda: 1)
    monkeypatch.setattr("agents_auto.restore_foreground_hwnd", lambda _h: True)
    monkeypatch.setattr(
        "agents_auto.refresh_cursor_bindings",
        lambda st: {"editor_ok": True, "agent_ok": False},
    )
    monkeypatch.setattr("agents_auto.agent_binding_ok", lambda st: {"ok": False})
    monkeypatch.setattr("agents_auto.editor_hwnd", lambda st: 10)

    state = {"cursor_editor": {"hwnd": 10}, "cursor_agent": None}
    only_editor = [{"hwnd": 10, "role": ROLE_EDITOR}]

    r = ensure_agents_window_bound(
        {"cursor_process": "Cursor.exe"},
        state,
        timeout_s=0.35,
        classify_list_fn=lambda _p: only_editor,
        list_fn=lambda _p: [{"hwnd": 10}],
        bind_fn=lambda *_a, **_k: None,
        trigger_fn=lambda *_a, **_k: {"ok": True, "trigger_method": "command_palette"},
    )
    assert r["ok"] is False
    assert r["error"] == "agent_window_not_found"


def test_wait_for_agent_classified_unique():
    before = [{"hwnd": 1}]
    n = {"i": 0}

    def classify(_p):
        n["i"] += 1
        if n["i"] < 2:
            return [{"hwnd": 1, "role": ROLE_EDITOR}]
        return [
            {"hwnd": 1, "role": ROLE_EDITOR},
            {"hwnd": 9, "role": ROLE_UNKNOWN},
            {"hwnd": 8, "role": ROLE_AGENT},
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
    assert r["candidate"]["hwnd"] == 8


def test_ensure_auto_launch_confirmed(monkeypatch):
    """Trigger that already confirmed HWND binds without a second wait."""
    monkeypatch.setattr("agents_auto.win32gui.IsWindow", lambda _h: True)
    monkeypatch.setattr("agents_auto.get_foreground_hwnd", lambda: 1)
    monkeypatch.setattr("agents_auto.restore_foreground_hwnd", lambda _h: True)
    monkeypatch.setattr(
        "agents_auto.refresh_cursor_bindings",
        lambda st: {"editor_ok": True, "agent_ok": False},
    )
    monkeypatch.setattr("agents_auto.agent_binding_ok", lambda st: {"ok": False})
    monkeypatch.setattr("agents_auto.editor_hwnd", lambda st: 10)

    state = {"cursor_editor": {"hwnd": 10}, "cursor_agent": None}

    def bind_fn(hwnd, process_name="Cursor.exe"):
        return {"hwnd": hwnd, "pid": 3, "title": "Agents", "role": "agent"}

    def trigger(eh, **_k):
        return {
            "ok": True,
            "confirmed": True,
            "trigger_method": "alt_file_menu",
            "confirm": {
                "ok": True,
                "candidate": {"hwnd": 77, "role": ROLE_AGENT},
                "match": "role_agent",
                "classified_editor_count": 1,
                "classified_agent_count": 1,
                "unknown_count": 0,
            },
        }

    r = ensure_agents_window_bound(
        {"cursor_process": "Cursor.exe"},
        state,
        classify_list_fn=lambda _p: [{"hwnd": 10, "role": ROLE_EDITOR}],
        list_fn=lambda _p: [{"hwnd": 10}],
        bind_fn=bind_fn,
        trigger_fn=trigger,
    )
    assert r["ok"] is True
    assert r["source"] == "auto_launch_confirmed"
    assert r["agent_hwnd"] == 77
    assert r["trigger_method"] == "alt_file_menu"


def test_ensure_propagates_trigger_failed_no_new_window(monkeypatch):
    monkeypatch.setattr("agents_auto.win32gui.IsWindow", lambda _h: True)
    monkeypatch.setattr("agents_auto.get_foreground_hwnd", lambda: 1)
    monkeypatch.setattr("agents_auto.restore_foreground_hwnd", lambda _h: True)
    monkeypatch.setattr(
        "agents_auto.refresh_cursor_bindings",
        lambda st: {"editor_ok": True, "agent_ok": False},
    )
    monkeypatch.setattr("agents_auto.agent_binding_ok", lambda st: {"ok": False})
    monkeypatch.setattr("agents_auto.editor_hwnd", lambda st: 10)

    state = {"cursor_editor": {"hwnd": 10}, "cursor_agent": None}
    only = [{"hwnd": 10, "role": ROLE_EDITOR}]

    r = ensure_agents_window_bound(
        {"cursor_process": "Cursor.exe"},
        state,
        timeout_s=0.2,
        classify_list_fn=lambda _p: only,
        list_fn=lambda _p: [{"hwnd": 10}],
        bind_fn=lambda *_a, **_k: None,
        trigger_fn=lambda *_a, **_k: {
            "ok": False,
            "error": "trigger_failed_no_new_window",
            "trigger_method": None,
            "attempts": [{"step": "command_palette:New Agents Window", "ok": True}],
        },
    )
    assert r["ok"] is False
    assert r["error"] == "trigger_failed_no_new_window"