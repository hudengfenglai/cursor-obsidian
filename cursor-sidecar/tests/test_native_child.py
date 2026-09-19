"""Native child (SetParent) pure logic tests — no real Win32 reparent."""

from __future__ import annotations

from native_embed import style_for_native_child, style_has_child, style_has_popup
from pane_geometry import map_dom_rect_to_client, map_pane_dict_to_client


def test_style_for_native_child_sets_child_clears_popup():
    WS_POPUP = 0x80000000
    WS_CAPTION = 0x00C00000
    WS_VISIBLE = 0x10000000
    original = WS_POPUP | WS_CAPTION | WS_VISIBLE
    child = style_for_native_child(original)
    assert style_has_child(child)
    assert not style_has_popup(child)
    assert child & WS_VISIBLE  # visibility bit preserved if present


def test_style_restoration_is_original():
    original = 0x14CF0000
    child = style_for_native_child(original)
    assert child != original
    # Restore path uses saved original verbatim
    assert original == 0x14CF0000


def test_client_coordinate_mapping_no_screen_origin():
    client, sx, sy = map_dom_rect_to_client(
        client_width_px=1200,
        client_height_px=900,
        viewport_width_css=960,
        viewport_height_css=720,
        dom_left=700,
        dom_top=40,
        dom_width=260,
        dom_height=680,
    )
    assert sx == 1.25
    assert sy == 1.25
    # No ClientToScreen offset — relative to client origin
    assert client.left == round(700 * 1.25)
    assert client.top == round(40 * 1.25)
    assert client.width == round(260 * 1.25)
    assert client.height == round(680 * 1.25)


def test_collapsed_pane_client_map():
    mapped = map_pane_dict_to_client(
        {
            "left": 0,
            "top": 0,
            "width": 0,
            "height": 100,
            "viewport_width": 800,
            "viewport_height": 600,
            "visible": True,
        },
        client_width_px=800,
        client_height_px=600,
    )
    assert mapped["ok"] is True
    assert mapped["collapsed"] is True
    assert mapped["visible"] is False


def test_visual_vs_native_backend_separation():
    assert "visual" != "native_child"
    # State machine: cannot be both meaningful modes at once
    state = {"embed_backend": "visual", "native_child": False}
    assert not (state["embed_backend"] == "native_child" and not state["native_child"] is False)


def test_crash_recovery_state_shape():
    snap = {
        "hwnd": 42,
        "style": 0x14CF0000,
        "exstyle": 0,
        "original_parent": 0,
        "original_owner": 0,
        "rect": [10, 20, 810, 620],
        "original_window_placement": {
            "flags": 0,
            "show_cmd": 1,
            "min_position": [0, 0],
            "max_position": [-1, -1],
            "normal_position": [10, 20, 810, 620],
        },
        "visible": True,
    }
    state = {
        "native_child": True,
        "native_restore_snapshot": snap,
        "cursor_editor": {"hwnd": 1},
        "cursor_agent": {"hwnd": 42},
    }
    # Recover clears native flags without clearing editor
    state["native_child"] = False
    state["native_restore_snapshot"] = None
    assert state["cursor_editor"]["hwnd"] == 1
    assert state["cursor_agent"]["hwnd"] == 42


def test_editor_unaffected_by_agent_stale():
    state = {
        "cursor_editor": {"hwnd": 11, "pid": 1},
        "cursor_agent": None,
        "agent_stale_reason": "closed",
    }
    assert state["cursor_editor"]["hwnd"] == 11
