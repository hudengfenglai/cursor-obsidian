"""Optional UI Automation helpers for Cursor window classification / File menu.

Best-effort only. Missing COM/UIA → soft fail (callers fall through).
Does not reverse-engineer Cursor command IDs.
"""

from __future__ import annotations

import logging
import re
from typing import Any

log = logging.getLogger("cursor_sidecar.agents_uia")

_AGENT_MARKERS = re.compile(r"new agents? window|agents window", re.I)
_EDITOR_MARKERS = re.compile(r"open editor window|^editor window$", re.I)


def scan_window_name_markers(hwnd: int, *, max_elements: int = 80) -> dict[str, int]:
    """Walk a shallow UIA tree for known public name markers."""
    hits = {"editor_window": 0, "agents_window": 0, "new_agents": 0}
    try:
        import comtypes  # noqa: F401
        import comtypes.client
    except Exception:
        return hits

    try:
        uia = comtypes.client.CreateObject(
            "{ff48dba4-60ef-4201-aa87-54103eef594e}",
            interface=None,
        )
        # Prefer IUIAutomation via GetModule
        mod = comtypes.client.GetModule("UIAutomationCore.dll")
        IUIAutomation = mod.IUIAutomation
        uia = comtypes.client.CreateObject(
            "{ff48dba4-60ef-4201-aa87-54103eef594e}",
            interface=IUIAutomation,
        )
        element = uia.ElementFromHandle(int(hwnd))
        if not element:
            return hits
        condition = uia.CreateTrueCondition()
        walker = uia.CreateTreeWalker(condition)
        # Breadth-ish: children of root + one level
        stack = [element]
        seen = 0
        while stack and seen < max_elements:
            el = stack.pop(0)
            seen += 1
            try:
                name = str(el.CurrentName or "")
            except Exception:
                name = ""
            if name:
                if _EDITOR_MARKERS.search(name.strip()):
                    hits["editor_window"] += 1
                if re.search(r"new agents? window", name, re.I):
                    hits["new_agents"] += 1
                elif re.search(r"agents window", name, re.I):
                    hits["agents_window"] += 1
            try:
                child = walker.GetFirstChildElement(el)
                while child and seen < max_elements:
                    stack.append(child)
                    try:
                        child = walker.GetNextSiblingElement(child)
                    except Exception:
                        break
            except Exception:
                pass
    except Exception as exc:
        log.debug("UIA scan failed: %s", exc)
    return hits


def invoke_file_new_agents_window(hwnd: int) -> dict[str, Any]:
    """UIA: File → New Agents Window → InvokePattern."""
    try:
        import comtypes.client
    except Exception as exc:
        return {"ok": False, "error": "uia_unavailable", "detail": str(exc)}

    try:
        mod = comtypes.client.GetModule("UIAutomationCore.dll")
        IUIAutomation = mod.IUIAutomation
        uia = comtypes.client.CreateObject(
            "{ff48dba4-60ef-4201-aa87-54103eef594e}",
            interface=IUIAutomation,
        )
        root = uia.ElementFromHandle(int(hwnd))
        if not root:
            return {"ok": False, "error": "uia_no_element"}

        # Expand File menu then find New Agents Window
        name_prop = 30005  # UIA_NamePropertyId
        control_type = 30003  # UIA_ControlTypePropertyId
        # MenuItem = 50011, Menu = 50009, Button = 50000 — keep loose
        def _find_by_name(parent, needle: str, depth: int = 0):
            if depth > 6 or parent is None:
                return None
            try:
                cond = uia.CreatePropertyCondition(name_prop, needle)
                found = parent.FindFirst(4, cond)  # TreeScope_Descendants=4
                if found:
                    return found
            except Exception:
                pass
            # Fuzzy walk
            try:
                walker = uia.CreateTreeWalker(uia.CreateTrueCondition())
                child = walker.GetFirstChildElement(parent)
                n = 0
                while child and n < 40:
                    n += 1
                    try:
                        nm = str(child.CurrentName or "")
                    except Exception:
                        nm = ""
                    if needle.lower() in nm.replace("&", "").lower():
                        return child
                    deeper = _find_by_name(child, needle, depth + 1)
                    if deeper:
                        return deeper
                    try:
                        child = walker.GetNextSiblingElement(child)
                    except Exception:
                        break
            except Exception:
                pass
            return None

        # Try direct descendant first
        target = None
        for label in ("New Agents Window", "New Agent Window", "Agents Window"):
            target = _find_by_name(root, label)
            if target:
                break
        if not target:
            file_el = _find_by_name(root, "File")
            if file_el:
                try:
                    # Expand pattern 10005 = ExpandCollapse
                    iid = comtypes.GUID("{d5137a7b-5aae-4751-8e58-2e2c0dbf7b8b}")
                    # Fall through to Invoke on File then re-find
                    from comtypes import COMError

                    try:
                        expand = file_el.GetCurrentPattern(30015)  # ExpandCollapsePatternId often 30015
                    except Exception:
                        expand = None
                except Exception:
                    pass
                for label in ("New Agents Window", "New Agent Window", "Agents Window"):
                    target = _find_by_name(root, label)
                    if target:
                        break

        if not target:
            return {"ok": False, "error": "uia_menu_item_not_found"}

        # InvokePatternId = 10000
        try:
            pattern = target.GetCurrentPattern(10000)
            if pattern is None:
                return {"ok": False, "error": "uia_no_invoke"}
            pattern.Invoke()
            return {"ok": True, "trigger_method": "uia_menu"}
        except Exception as exc:
            return {"ok": False, "error": "uia_invoke_failed", "detail": str(exc)}
    except Exception as exc:
        log.debug("UIA invoke failed: %s", exc)
        return {"ok": False, "error": "uia_failed", "detail": str(exc)}
