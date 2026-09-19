"""v0.7.1 auto-bind / chrome pure tests (no live SendInput)."""

from __future__ import annotations

from agents_auto import ensure_agents_window_bound, wait_for_new_agent_hwnd
from native_embed import style_for_chrome_level, style_for_native_child, style_has_caption, style_has_child


def test_chrome_full_clears_caption_and_thickframe_keeps_sysmenu():
    WS_CAPTION = 0x00C00000
    WS_THICKFRAME = 0x00040000
    WS_SYSMENU = 0x00080000
    WS_CHILD = 0x40000000
    style = WS_CHILD | WS_CAPTION | WS_THICKFRAME | WS_SYSMENU
    out = style_for_chrome_level(style, level="full")
    assert style_has_child(out) or (out & WS_CHILD)
    assert not style_has_caption(out)
    assert not (out & WS_THICKFRAME)
    assert out & WS_SYSMENU


def test_chrome_caption_only_keeps_thickframe():
    WS_CAPTION = 0x00C00000
    WS_THICKFRAME = 0x00040000
    WS_SYSMENU = 0x00080000
    style = WS_CAPTION | WS_THICKFRAME
    out = style_for_chrome_level(style, level="caption_only")
    assert not style_has_caption(out)
    assert out & WS_THICKFRAME
    assert out & WS_SYSMENU


def test_native_child_style_still_sets_child():
    WS_POPUP = 0x80000000
    WS_CAPTION = 0x00C00000
    child = style_for_native_child(WS_POPUP | WS_CAPTION)
    assert style_has_child(child)


def test_wait_for_new_agent_hwnd_diff():
    before = [{"hwnd": 1}, {"hwnd": 2}]
    calls = {"n": 0}

    def list_fn(_proc):
        calls["n"] += 1
        if calls["n"] < 2:
            return before
        return before + [{"hwnd": 99, "title": "x"}]

    r = wait_for_new_agent_hwnd(
        before=before,
        process_name="Cursor.exe",
        editor_hwnd_i=1,
        timeout_s=1.0,
        poll_s=0.01,
        list_fn=list_fn,
    )
    assert r["ok"] is True
    assert r["candidate"]["hwnd"] == 99


def test_ensure_auto_select_existing(monkeypatch):
    state = {
        "cursor_editor": {"hwnd": 10},
        "cursor_agent": None,
    }

    def list_fn(_p):
        return [{"hwnd": 10}, {"hwnd": 20, "title": "Agents"}]

    def bind_fn(hwnd, process_name="Cursor.exe"):
        return {"hwnd": hwnd, "pid": 1, "title": "Agents", "role": "agent"}

    r = ensure_agents_window_bound(
        {"cursor_process": "Cursor.exe"},
        state,
        list_fn=list_fn,
        bind_fn=bind_fn,
        trigger_fn=lambda *_a, **_k: {"ok": False},
    )
    assert r["ok"] is True
    assert r["source"] == "auto_select_existing"
    assert state["cursor_agent"]["hwnd"] == 20


def test_ensure_auto_launch_diff(monkeypatch):
    monkeypatch.setattr("agents_auto.win32gui.IsWindow", lambda _h: True)
    monkeypatch.setattr("agents_auto.get_foreground_hwnd", lambda: 1)
    monkeypatch.setattr("agents_auto.restore_foreground_hwnd", lambda _h: True)
    monkeypatch.setattr("agents_auto.focus_window", lambda _h: None)

    state = {
        "cursor_editor": {"hwnd": 10},
        "cursor_agent": None,
    }
    windows = [{"hwnd": 10}]

    def list_fn(_p):
        return list(windows)

    def bind_fn(hwnd, process_name="Cursor.exe"):
        return {"hwnd": hwnd, "pid": 7, "title": "New", "role": "agent"}

    def trigger(eh, query=None):
        windows.append({"hwnd": 55, "title": query or "Agents"})
        return {"ok": True, "query": query}

    r = ensure_agents_window_bound(
        {"cursor_process": "Cursor.exe"},
        state,
        timeout_s=2.0,
        list_fn=list_fn,
        bind_fn=bind_fn,
        trigger_fn=trigger,
    )
    assert r["ok"] is True
    assert r["source"] == "auto_launch_diff"
    assert r["agent_hwnd"] == 55


def test_ensure_not_found(monkeypatch):
    monkeypatch.setattr("agents_auto.win32gui.IsWindow", lambda _h: True)
    monkeypatch.setattr("agents_auto.get_foreground_hwnd", lambda: 1)
    monkeypatch.setattr("agents_auto.restore_foreground_hwnd", lambda _h: True)
    monkeypatch.setattr("agents_auto.focus_window", lambda _h: None)

    state = {"cursor_editor": {"hwnd": 10}, "cursor_agent": None}

    def list_fn(_p):
        return [{"hwnd": 10}]

    r = ensure_agents_window_bound(
        {"cursor_process": "Cursor.exe"},
        state,
        timeout_s=0.3,
        list_fn=list_fn,
        bind_fn=lambda *_a, **_k: None,
        trigger_fn=lambda *_a, **_k: {"ok": True, "query": "x"},
    )
    assert r["ok"] is False
    assert r["error"] == "agent_window_not_found"
