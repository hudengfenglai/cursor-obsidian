"""Unit tests for pure Sidecar geometry / state logic (no Win32)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from geometry import (
    atomic_write_text,
    can_restore_bound_window,
    compute_split_rects,
    deserialize_state,
    evaluate_attachment,
    normalize_ratios,
    placement_dict,
    placement_tuple,
    recover_state_dict,
    serialize_state,
    validate_binding_identity,
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
    assert json.loads(raw)["cursor"]["pid"] == 44


def test_create_time_mismatch_rejects():
    record = {
        "hwnd": 100,
        "pid": 200,
        "process_name": "Cursor.exe",
        "process_create_time": 1000.0,
    }
    v = validate_binding_identity(
        record,
        hwnd_exists=True,
        live_pid=200,
        live_process_name="Cursor.exe",
        live_create_time=9999.0,
    )
    assert v["ok"] is False
    assert v["reason"] == "create_time_mismatch"
    assert v["restore_safe"] is False


def test_legacy_binding_without_create_time():
    record = {"hwnd": 100, "pid": 200, "process_name": "Cursor.exe"}
    v = validate_binding_identity(
        record,
        hwnd_exists=True,
        live_pid=200,
        live_process_name="Cursor.exe",
        live_create_time=1234.5,
    )
    assert v["ok"] is True
    assert v["legacy_binding"] is True
    assert v["binding_mode"] == "hwnd_pid_process"


def test_full_identity_ok():
    record = {
        "hwnd": 100,
        "pid": 200,
        "process_name": "Cursor.exe",
        "process_create_time": 50.0,
    }
    v = validate_binding_identity(
        record,
        hwnd_exists=True,
        live_pid=200,
        live_process_name="Cursor.exe",
        live_create_time=50.2,
    )
    assert v["ok"] is True
    assert v["legacy_binding"] is False
    assert v["binding_mode"] == "hwnd_pid_process_start"
    assert v["restore_safe"] is True


def test_process_name_mismatch():
    record = {
        "hwnd": 1,
        "pid": 2,
        "process_name": "Cursor.exe",
        "process_create_time": 1.0,
    }
    v = validate_binding_identity(
        record,
        hwnd_exists=True,
        live_pid=2,
        live_process_name="notepad.exe",
        live_create_time=1.0,
    )
    assert v["ok"] is False
    assert v["reason"] == "process_name_mismatch"


def test_cursor_a_gone_no_fallback_restore():
    """Cursor A binding dead → restore policy is gone (never touch Cursor B)."""
    snap_a = {
        "hwnd": 111,
        "pid": 222,
        "process_name": "Cursor.exe",
        "process_create_time": 10.0,
        "original_window_placement": {},
    }
    # Binding identity fails (hwnd gone) — even if Cursor B exists elsewhere
    identity = validate_binding_identity(
        snap_a,
        hwnd_exists=False,
        live_pid=None,
    )
    assert identity["ok"] is False
    assert can_restore_bound_window(snap_a, binding_ok=False) == "gone"
    assert can_restore_bound_window(snap_a, binding_ok=True) == "restore"
    assert can_restore_bound_window(None, binding_ok=False) == "skip"


def test_atomic_write_text():
    import tempfile
    import shutil

    root = Path(__file__).resolve().parent / "_tmp_atomic"
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    try:
        target = root / "state.json"
        atomic_write_text(target, '{"attached": false}\n')
        assert target.read_text(encoding="utf-8") == '{"attached": false}\n'
        assert not (root / "state.json.tmp").exists()
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_malformed_state_recovery():
    assert recover_state_dict("") == {}
    assert recover_state_dict("{not json") == {}
    assert recover_state_dict("[1,2,3]") == {}
    assert recover_state_dict('{"attached": true}')["attached"] is True
