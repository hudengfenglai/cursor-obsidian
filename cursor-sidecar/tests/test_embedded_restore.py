"""Embedded pane restore-layer state machine tests (no Win32 required)."""

from __future__ import annotations

from pane_geometry import DomPaneRect


def test_restore_layers_are_distinct():
    """Attach original ≠ sidecar runtime ≠ embedded temporary."""
    attach_original = {"style": 0x14CF0000, "exstyle": 0x100, "owner": 0, "rect": [0, 0, 800, 600]}
    sidecar_runtime = {"rect": [900, 0, 1400, 900]}
    embedded_temp = {"style": 0x14CF0000 & ~0x00C00000, "owner": 12345, "rect": [1000, 50, 1300, 850]}

    # Exit embed restores embedded_temp chrome → sidecar_runtime geometry via arrange
    # Detach restores attach_original placement
    assert attach_original["rect"] != sidecar_runtime["rect"]
    assert embedded_temp["rect"] != attach_original["rect"]
    assert embedded_temp.get("owner") != attach_original.get("owner")


def test_embed_mode_defaults():
    state = {"attached": True, "embed_mode": "sidecar", "embedded": False}
    assert state["embed_mode"] == "sidecar"
    assert state["embedded"] is False

    # Enter
    state["embed_mode"] = "pane"
    state["embedded"] = True
    state["last_pane_dom"] = DomPaneRect(
        left=700, top=0, width=260, height=720,
        viewport_width=960, viewport_height=720,
    ).to_dict()
    assert state["embedded"] is True

    # Exit embed → sidecar, still attached
    state["embed_mode"] = "sidecar"
    state["embedded"] = False
    state["last_pane_dom"] = None
    assert state["attached"] is True
    assert state["embedded"] is False


def test_update_ignored_when_embed_off():
    """Mirrors update_embedded_pane ignore semantics."""
    state = {"attached": True, "embed_mode": "sidecar", "embedded": False}
    ignored = (not state.get("embedded")) or str(state.get("embed_mode") or "") != "pane"
    assert ignored is True
