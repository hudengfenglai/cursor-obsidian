"""Unit tests for pure Sidecar geometry / state logic (no Win32)."""

from __future__ import annotations

import json

import pytest

from geometry import (
    compute_split_rects,
    deserialize_state,
    evaluate_attachment,
    normalize_ratios,
    placement_dict,
    placement_tuple,
    serialize_state,
    window_binding_valid,
)


def test_normalize_ratios_ok():
    assert normalize_ratios(0.7, 0.3) == (0.7, 0.3)


def test_normalize_ratios_rejects_non_positive():
    with pytest.raises(ValueError):
        normalize_ratios(0, 0.3)
    with pytest.raises(ValueError):
        normalize_ratios(0.5, -1)


def test_compute_split_rects_basic():
    left, right = compute_split_rects((0, 0, 1000, 800), 0.7, 0.3, gap=0)
    assert left == (0, 0, 700, 800)
    assert right == (700, 0, 1000, 800)


def test_compute_split_rects_with_gap():
    left, right = compute_split_rects((100, 50, 1100, 850), 0.5, 0.5, gap=10)
    # usable = 1000 - 10 = 990 → 495 / 495
    assert left[0] == 100
    assert left[2] - left[0] == 495
    assert right[0] == left[2] + 10
    assert right[2] == 1100


def test_placement_roundtrip():
    d = placement_dict(0, 3, (0, 0), (-1, -1), (10, 20, 800, 600))
    t = placement_tuple(d)
    assert t[1] == 3
    assert t[4] == (10, 20, 800, 600)


def test_window_binding_valid():
    assert window_binding_valid({"hwnd": 1, "pid": 2}, live_hwnd_ok=True)
    assert not window_binding_valid({"hwnd": 1, "pid": 2}, live_hwnd_ok=False)
    assert not window_binding_valid({"hwnd": 1}, live_hwnd_ok=True)
    assert not window_binding_valid(None, live_hwnd_ok=True)


def test_evaluate_attachment_detached():
    v = evaluate_attachment({}, obsidian_live=True, cursor_live=True)
    assert v["attached"] is False
    assert v["reason"] == "detached"


def test_evaluate_attachment_cursor_gone():
    state = {
        "attached": True,
        "obsidian": {"hwnd": 1, "pid": 10},
        "cursor": {"hwnd": 2, "pid": 20},
    }
    v = evaluate_attachment(state, obsidian_live=True, cursor_live=False)
    assert v["attached"] is False
    assert v["state_valid"] is False
    assert v["reason"] == "cursor_gone"


def test_evaluate_attachment_ok():
    state = {
        "attached": True,
        "obsidian": {"hwnd": 1, "pid": 10},
        "cursor": {"hwnd": 2, "pid": 20},
    }
    v = evaluate_attachment(state, obsidian_live=True, cursor_live=True)
    assert v["attached"] is True
    assert v["state_valid"] is True


def test_evaluate_attachment_incomplete():
    state = {"attached": True, "obsidian": {"hwnd": 1}}
    v = evaluate_attachment(state, obsidian_live=False, cursor_live=False)
    assert v["attached"] is False
    assert v["reason"] == "incomplete_bindings"


def test_state_serialization_roundtrip():
    state = {
        "attached": True,
        "obsidian": {"hwnd": 11, "pid": 22, "original_rect": [0, 0, 1, 1]},
        "cursor": {"hwnd": 33, "pid": 44},
    }
    raw = serialize_state(state)
    back = deserialize_state(raw)
    assert back["attached"] is True
    assert back["obsidian"]["hwnd"] == 11
    # stable json
    assert json.loads(raw)["cursor"]["pid"] == 44
