"""Pure geometry tests: DOM CSS rect → Win32 screen rect (v0.7 Embedded Pane)."""

from __future__ import annotations

import pytest

from pane_geometry import (
    DomPaneRect,
    compute_client_scales,
    is_pane_collapsed,
    map_dom_rect_to_screen,
    map_pane_dict_to_screen,
)


def test_scale_125_example():
    """Spec §26: client 1200x900, viewport 960x720 → scale 1.25."""
    sx, sy = compute_client_scales(1200, 900, 960, 720)
    assert sx == pytest.approx(1.25)
    assert sy == pytest.approx(1.25)

    screen, out_sx, out_sy = map_dom_rect_to_screen(
        client_screen_x=100,
        client_screen_y=200,
        client_width_px=1200,
        client_height_px=900,
        viewport_width_css=960,
        viewport_height_css=720,
        dom_left=700,
        dom_top=0,
        dom_width=260,
        dom_height=720,
    )
    assert out_sx == pytest.approx(1.25)
    assert out_sy == pytest.approx(1.25)
    assert screen.left == 100 + round(700 * 1.25)  # 975
    assert screen.top == 200
    assert screen.width == round(260 * 1.25)  # 325
    assert screen.height == round(720 * 1.25)  # 900
    assert screen.left == 975
    assert screen.width == 325
    assert screen.height == 900


def test_scale_100():
    screen, sx, sy = map_dom_rect_to_screen(
        client_screen_x=0,
        client_screen_y=0,
        client_width_px=1920,
        client_height_px=1080,
        viewport_width_css=1920,
        viewport_height_css=1080,
        dom_left=1400,
        dom_top=40,
        dom_width=500,
        dom_height=1000,
    )
    assert sx == pytest.approx(1.0)
    assert sy == pytest.approx(1.0)
    assert screen.left == 1400
    assert screen.top == 40
    assert screen.width == 500
    assert screen.height == 1000


def test_scale_150():
    sx, sy = compute_client_scales(1800, 1200, 1200, 800)
    assert sx == pytest.approx(1.5)
    assert sy == pytest.approx(1.5)


def test_negative_monitor_coordinates():
    screen, _, _ = map_dom_rect_to_screen(
        client_screen_x=-1920,
        client_screen_y=100,
        client_width_px=1920,
        client_height_px=1080,
        viewport_width_css=1920,
        viewport_height_css=1080,
        dom_left=1600,
        dom_top=0,
        dom_width=300,
        dom_height=1080,
    )
    assert screen.left == -320
    assert screen.top == 100
    assert screen.width == 300


def test_fractional_css_rect_rounds():
    screen, _, _ = map_dom_rect_to_screen(
        client_screen_x=10.4,
        client_screen_y=20.6,
        client_width_px=1000,
        client_height_px=800,
        viewport_width_css=1000,
        viewport_height_css=800,
        dom_left=100.4,
        dom_top=50.6,
        dom_width=200.4,
        dom_height=300.6,
    )
    assert screen.left == round(10.4 + 100.4)
    assert screen.top == round(20.6 + 50.6)
    assert screen.width == round(200.4)
    assert screen.height == round(300.6)


def test_zero_size_and_collapsed():
    assert is_pane_collapsed(width=0, height=100, visible=True) is True
    assert is_pane_collapsed(width=100, height=0, visible=True) is True
    assert is_pane_collapsed(width=100, height=100, visible=False) is True
    assert is_pane_collapsed(width=100, height=100, visible=True) is False

    mapped = map_pane_dict_to_screen(
        {
            "left": 0,
            "top": 0,
            "width": 0,
            "height": 720,
            "viewport_width": 960,
            "viewport_height": 720,
            "visible": True,
        },
        client_screen_x=0,
        client_screen_y=0,
        client_width_px=960,
        client_height_px=720,
    )
    assert mapped["ok"] is True
    assert mapped["collapsed"] is True
    assert mapped["visible"] is False


def test_dom_pane_roundtrip():
    d = DomPaneRect.from_dict(
        {
            "left": 1,
            "top": 2,
            "width": 3,
            "height": 4,
            "viewport_width": 5,
            "viewport_height": 6,
            "device_pixel_ratio": 1.25,
            "visible": False,
        }
    )
    assert d is not None
    assert d.to_dict()["device_pixel_ratio"] == 1.25
    assert DomPaneRect.from_dict(None) is None
    assert DomPaneRect.from_dict({"left": "bad"}) is None


def test_dpr_is_diagnostic_only():
    """devicePixelRatio must not replace client/viewport scale."""
    mapped = map_pane_dict_to_screen(
        {
            "left": 0,
            "top": 0,
            "width": 100,
            "height": 100,
            "viewport_width": 200,
            "viewport_height": 200,
            "device_pixel_ratio": 2.0,
            "visible": True,
        },
        client_screen_x=0,
        client_screen_y=0,
        client_width_px=200,
        client_height_px=200,
    )
    assert mapped["scale_x"] == pytest.approx(1.0)
    assert mapped["device_pixel_ratio"] == 2.0
