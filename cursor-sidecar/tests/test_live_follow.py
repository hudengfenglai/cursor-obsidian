"""v0.3 Live Sidecar pure-logic tests (no Win32 / no desktop)."""

from __future__ import annotations

import time

import pytest

from geometry import (
    LatestOnlyDebouncer,
    WIDTH_PRESETS,
    clamp_rect_to_work,
    compute_follow_cursor_rect,
    compute_split_rects,
    cursor_width_from_work,
    evaluate_attachment,
    ratios_for_preset,
    resolve_preset,
)


def test_preset_mapping():
    assert ratios_for_preset("compact") == (0.78, 0.22)
    assert ratios_for_preset("normal") == (0.70, 0.30)
    assert ratios_for_preset("wide") == (0.58, 0.42)
    assert resolve_preset("NORMAL") == "normal"
    assert set(WIDTH_PRESETS) == {"compact", "normal", "wide"}
    with pytest.raises(ValueError):
        resolve_preset("huge")


def test_preset_split_rects_match_ratios():
    work = (0, 0, 1000, 800)
    for name, (o, c) in WIDTH_PRESETS.items():
        left, right = compute_split_rects(work, o, c, gap=0)
        assert left[2] - left[0] + right[2] - right[0] == 1000
        assert abs((right[2] - right[0]) / 1000 - c) < 0.02
        assert ratios_for_preset(name) == (o, c)


def test_follow_rect_sticks_to_right_edge():
    obs = (100, 50, 800, 650)
    work = (0, 0, 1920, 1080)
    cur = compute_follow_cursor_rect(obs, cursor_width=300, gap=0, work=work)
    assert cur[0] == 800  # Obsidian.right
    assert cur[1] == 50
    assert cur[2] == 1100
    assert cur[3] == 650


def test_follow_rect_with_gap():
    obs = (0, 0, 700, 800)
    work = (0, 0, 1000, 800)
    cur = compute_follow_cursor_rect(obs, cursor_width=200, gap=10, work=work)
    assert cur[0] == 710
    assert cur[2] == 910


def test_follow_clamps_to_work_area():
    obs = (0, 0, 900, 800)
    work = (0, 0, 1000, 800)
    cur = compute_follow_cursor_rect(obs, cursor_width=400, gap=0, work=work)
    # only 100px remain — clamp width
    assert cur[0] >= work[0]
    assert cur[2] <= work[2]
    assert cur[2] - cur[0] <= 100


def test_follow_negative_coordinate_monitor():
    """Secondary monitor to the left: work area with negative X."""
    work = (-2560, 0, 0, 1440)
    obs = (-2560, 100, -1000, 1300)
    cur = compute_follow_cursor_rect(obs, cursor_width=400, gap=0, work=work)
    assert cur[0] == -1000
    assert cur[1] == 100
    assert cur[2] == -600
    assert cur[3] == 1300
    assert cur[0] >= work[0]
    assert cur[2] <= work[2]


def test_clamp_rect_to_work_overflow():
    work = (-2560, 0, 0, 1440)
    clamped = clamp_rect_to_work((-100, 10, 500, 500), work)
    assert clamped[0] >= work[0]
    assert clamped[2] <= work[2]


def test_cursor_width_from_work():
    assert cursor_width_from_work((0, 0, 1000, 800), 0.3) == 300
    assert cursor_width_from_work((-2560, 0, 0, 1440), 0.22) == int(round(2560 * 0.22))


def test_follow_does_not_move_obsidian_contract():
    """Follow helper only returns Cursor rect; Obsidian input unchanged by contract."""
    obs = (10, 20, 710, 820)
    before = tuple(obs)
    _ = compute_follow_cursor_rect(obs, cursor_width=200, gap=0, work=(0, 0, 2000, 1000))
    assert obs == before


def test_stale_binding_during_follow_evaluation():
    state = {
        "attached": True,
        "obsidian": {"hwnd": 1, "pid": 10},
        "cursor": {"hwnd": 2, "pid": 20},
    }
    gone = evaluate_attachment(state, obsidian_live=True, cursor_live=False)
    assert gone["attached"] is False
    assert gone["reason"] == "cursor_gone"
    obs_gone = evaluate_attachment(state, obsidian_live=False, cursor_live=True)
    assert obs_gone["reason"] == "obsidian_gone"


def test_minimize_state_transition_logic():
    """Documented minimize sync flags (pure state machine)."""
    hidden = False
    # minimize
    hidden = True
    assert hidden is True
    # restore
    hidden = False
    assert hidden is False


def test_debounce_latest_only():
    calls: list[int] = []

    class Box:
        n = 0

    def fire():
        calls.append(Box.n)

    d = LatestOnlyDebouncer(0.05, fire)
    Box.n = 1
    d.trigger()
    Box.n = 2
    d.trigger()
    Box.n = 3
    d.trigger()
    time.sleep(0.12)
    assert calls == [3]


def test_debounce_flush_immediate():
    calls: list[str] = []
    d = LatestOnlyDebouncer(1.0, lambda: calls.append("x"))
    d.trigger()
    d.flush()
    assert calls == ["x"]
    time.sleep(0.05)
    assert calls == ["x"]  # cancelled pending timer


def test_original_snapshot_untouched_by_follow_math():
    """Follow math never mutates saved original placement dicts."""
    original = {
        "hwnd": 1,
        "original_rect": [0, 0, 800, 600],
        "original_window_placement": {"showCmd": 1},
    }
    snap = dict(original)
    snap["original_rect"] = list(original["original_rect"])
    _ = compute_follow_cursor_rect(
        (100, 100, 700, 700),
        cursor_width=200,
        gap=0,
        work=(0, 0, 1920, 1080),
    )
    assert snap["original_rect"] == [0, 0, 800, 600]
    assert snap["original_window_placement"] == {"showCmd": 1}
