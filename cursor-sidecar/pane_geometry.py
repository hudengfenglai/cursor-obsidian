"""DOM CSS viewport rect → Win32 physical screen rect (v0.7 Embedded Pane).

Pure mapping — no Win32 calls. Helper supplies client origin/size from GetClientRect + ClientToScreen.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DomPaneRect:
    left: float
    top: float
    width: float
    height: float
    viewport_width: float
    viewport_height: float
    device_pixel_ratio: float = 1.0
    visible: bool = True

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "DomPaneRect | None":
        if not isinstance(data, dict):
            return None
        try:
            return cls(
                left=float(data.get("left", 0)),
                top=float(data.get("top", 0)),
                width=float(data.get("width", 0)),
                height=float(data.get("height", 0)),
                viewport_width=float(data.get("viewport_width", 0)),
                viewport_height=float(data.get("viewport_height", 0)),
                device_pixel_ratio=float(data.get("device_pixel_ratio", 1) or 1),
                visible=bool(data.get("visible", True)),
            )
        except (TypeError, ValueError):
            return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "left": self.left,
            "top": self.top,
            "width": self.width,
            "height": self.height,
            "viewport_width": self.viewport_width,
            "viewport_height": self.viewport_height,
            "device_pixel_ratio": self.device_pixel_ratio,
            "visible": self.visible,
        }


@dataclass(frozen=True)
class ClientRect:
    """Physical pixels relative to Obsidian client origin (for WS_CHILD SetWindowPos)."""

    left: int
    top: int
    width: int
    height: int

    def to_dict(self) -> dict[str, int]:
        return {
            "left": self.left,
            "top": self.top,
            "width": self.width,
            "height": self.height,
        }


@dataclass(frozen=True)
class ScreenRect:
    left: int
    top: int
    width: int
    height: int

    def as_tuple(self) -> tuple[int, int, int, int]:
        return (self.left, self.top, self.left + self.width, self.top + self.height)

    def to_dict(self) -> dict[str, int]:
        return {
            "left": self.left,
            "top": self.top,
            "width": self.width,
            "height": self.height,
        }


def map_dom_rect_to_client(
    *,
    client_width_px: float,
    client_height_px: float,
    viewport_width_css: float,
    viewport_height_css: float,
    dom_left: float,
    dom_top: float,
    dom_width: float,
    dom_height: float,
) -> tuple[ClientRect, float, float]:
    """
    Map DOM CSS rect → Obsidian client-area physical rect (no ClientToScreen).
    Used by native_child SetParent mode.
    """
    sx, sy = compute_client_scales(
        client_width_px, client_height_px, viewport_width_css, viewport_height_css
    )
    left = int(round(float(dom_left) * sx))
    top = int(round(float(dom_top) * sy))
    width = max(int(round(float(dom_width) * sx)), 0)
    height = max(int(round(float(dom_height) * sy)), 0)
    return ClientRect(left=left, top=top, width=width, height=height), sx, sy


def map_pane_dict_to_client(
    pane: dict[str, Any] | DomPaneRect,
    *,
    client_width_px: float,
    client_height_px: float,
) -> dict[str, Any]:
    """Convenience for native_child RPC / tests."""
    dom = pane if isinstance(pane, DomPaneRect) else DomPaneRect.from_dict(pane)
    if dom is None:
        return {"ok": False, "error": "invalid_pane"}
    collapsed = is_pane_collapsed(width=dom.width, height=dom.height, visible=dom.visible)
    client, sx, sy = map_dom_rect_to_client(
        client_width_px=client_width_px,
        client_height_px=client_height_px,
        viewport_width_css=dom.viewport_width,
        viewport_height_css=dom.viewport_height,
        dom_left=dom.left,
        dom_top=dom.top,
        dom_width=dom.width,
        dom_height=dom.height,
    )
    return {
        "ok": True,
        "collapsed": collapsed,
        "visible": bool(dom.visible) and not collapsed,
        "scale_x": sx,
        "scale_y": sy,
        "dom_rect": dom.to_dict(),
        "client_rect": client.to_dict(),
        "device_pixel_ratio": dom.device_pixel_ratio,
    }


def compute_client_scales(
    client_width_px: float,
    client_height_px: float,
    viewport_width_css: float,
    viewport_height_css: float,
) -> tuple[float, float]:
    """scale_x/y = physical client size / CSS viewport size."""
    vw = float(viewport_width_css or 0)
    vh = float(viewport_height_css or 0)
    if vw <= 0 or vh <= 0:
        return 1.0, 1.0
    return float(client_width_px) / vw, float(client_height_px) / vh


def is_pane_collapsed(
    *,
    width: float,
    height: float,
    visible: bool,
    min_px: float = 8.0,
) -> bool:
    if not visible:
        return True
    return float(width) < min_px or float(height) < min_px


def map_dom_rect_to_screen(
    *,
    client_screen_x: float,
    client_screen_y: float,
    client_width_px: float,
    client_height_px: float,
    viewport_width_css: float,
    viewport_height_css: float,
    dom_left: float,
    dom_top: float,
    dom_width: float,
    dom_height: float,
) -> tuple[ScreenRect, float, float]:
    """
    Map DOM CSS rect (relative to Obsidian Chromium viewport) to physical screen pixels.
    Returns (screen_rect, scale_x, scale_y).
    """
    sx, sy = compute_client_scales(
        client_width_px, client_height_px, viewport_width_css, viewport_height_css
    )
    left = int(round(float(client_screen_x) + float(dom_left) * sx))
    top = int(round(float(client_screen_y) + float(dom_top) * sy))
    width = max(int(round(float(dom_width) * sx)), 0)
    height = max(int(round(float(dom_height) * sy)), 0)
    return ScreenRect(left=left, top=top, width=width, height=height), sx, sy


def map_pane_dict_to_screen(
    pane: dict[str, Any] | DomPaneRect,
    *,
    client_screen_x: float,
    client_screen_y: float,
    client_width_px: float,
    client_height_px: float,
) -> dict[str, Any]:
    """Convenience for RPC / tests — returns diagnostics + screen_rect."""
    dom = pane if isinstance(pane, DomPaneRect) else DomPaneRect.from_dict(pane)
    if dom is None:
        return {"ok": False, "error": "invalid_pane"}
    collapsed = is_pane_collapsed(width=dom.width, height=dom.height, visible=dom.visible)
    screen, sx, sy = map_dom_rect_to_screen(
        client_screen_x=client_screen_x,
        client_screen_y=client_screen_y,
        client_width_px=client_width_px,
        client_height_px=client_height_px,
        viewport_width_css=dom.viewport_width,
        viewport_height_css=dom.viewport_height,
        dom_left=dom.left,
        dom_top=dom.top,
        dom_width=dom.width,
        dom_height=dom.height,
    )
    return {
        "ok": True,
        "collapsed": collapsed,
        "visible": bool(dom.visible) and not collapsed,
        "scale_x": sx,
        "scale_y": sy,
        "dom_rect": dom.to_dict(),
        "screen_rect": screen.to_dict(),
        "device_pixel_ratio": dom.device_pixel_ratio,
    }
