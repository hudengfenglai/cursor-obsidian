"""v0.3 RC unit tests: runtime toggle, event routing, daemon endpoint, follow edge cases."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from geometry import compute_follow_cursor_rect
from main import (
    daemon_endpoint_args,
    normalize_daemon_bind_host,
)
from win_events import (
    EVENT_OBJECT_DESTROY,
    EVENT_OBJECT_LOCATIONCHANGE,
    LiveFollowService,
)


def test_normalize_daemon_localhost_ok():
    assert normalize_daemon_bind_host("127.0.0.1") == "127.0.0.1"
    assert normalize_daemon_bind_host("localhost") == "localhost"
    assert normalize_daemon_bind_host("::1") == "::1"


def test_normalize_daemon_rejects_all_interfaces():
    with pytest.raises(ValueError):
        normalize_daemon_bind_host("0.0.0.0")
    with pytest.raises(ValueError):
        normalize_daemon_bind_host("::")


def test_normalize_daemon_rejects_lan_bind():
    with pytest.raises(ValueError):
        normalize_daemon_bind_host("192.168.1.5")


def test_daemon_endpoint_args_consistency():
    assert daemon_endpoint_args("127.0.0.1", 30000) == [
        "--host",
        "127.0.0.1",
        "--port",
        "30000",
    ]


def test_no_room_right_current_behavior():
    """Documented limitation: when Obsidian fills work area, Cursor parks at right edge."""
    work = (0, 0, 1000, 800)
    obs = (0, 0, 1000, 800)  # no space to the right
    cur = compute_follow_cursor_rect(obs, cursor_width=300, gap=0, work=work)
    assert cur[2] <= work[2]
    assert cur[0] >= work[0]
    # May overlap Obsidian — recorded limitation for v0.3
    assert cur[2] - cur[0] >= 1


def test_cursor_destroy_routing_marks_stale():
    marks: list[str] = []
    lock = threading.RLock()
    svc = LiveFollowService(
        lifecycle_lock=lock,
        get_attached_state=lambda: {
            "attached": True,
            "obsidian": {"hwnd": 10, "pid": 1},
            "cursor": {"hwnd": 20, "pid": 2},
        },
        mark_stale=lambda reason: marks.append(reason),
        enabled=True,
    )
    svc.obsidian_hwnd = 10
    svc.cursor_hwnd = 20
    svc.stop = MagicMock()  # type: ignore[method-assign]

    assert svc.dispatch_win_event(EVENT_OBJECT_DESTROY, 20) == "CURSOR_DESTROY"
    assert marks == ["cursor_gone"]
    assert svc.last_event == "CURSOR_DESTROY"
    svc.stop.assert_called()


def test_cursor_locationchange_ignored():
    marks: list[str] = []
    svc = LiveFollowService(
        lifecycle_lock=threading.RLock(),
        get_attached_state=lambda: {"attached": True},
        mark_stale=lambda reason: marks.append(reason),
        enabled=True,
    )
    svc.obsidian_hwnd = 10
    svc.cursor_hwnd = 20
    assert svc.dispatch_win_event(EVENT_OBJECT_LOCATIONCHANGE, 20) == "ignore_hwnd"
    assert marks == []


def test_obsidian_destroy_routing():
    marks: list[str] = []
    svc = LiveFollowService(
        lifecycle_lock=threading.RLock(),
        get_attached_state=lambda: {"attached": True},
        mark_stale=lambda reason: marks.append(reason),
        enabled=True,
    )
    svc.obsidian_hwnd = 10
    svc.cursor_hwnd = 20
    svc.stop = MagicMock()  # type: ignore[method-assign]
    assert svc.dispatch_win_event(EVENT_OBJECT_DESTROY, 10) == "DESTROY"
    assert marks == ["obsidian_gone"]


def test_set_live_follow_false_stops_without_detach(monkeypatch):
    import shutil
    import main as sidecar

    root = Path(__file__).resolve().parent / "_tmp_rc_live_off"
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    try:
        cfg_path = root / "config.json"
        cfg_path.write_text(
            json.dumps(
                {
                    "preset": "normal",
                    "gap": 0,
                    "daemon_host": "127.0.0.1",
                    "daemon_port": 27845,
                    "live_follow": True,
                    "obsidian_process": "Obsidian.exe",
                    "cursor_process": "Cursor.exe",
                }
            ),
            encoding="utf-8",
        )
        state_path = root / ".sidecar.state.json"
        state_path.write_text(
            json.dumps(
                {
                    "attached": True,
                    "preset": "normal",
                    "obsidian": {"hwnd": 1, "pid": 1},
                    "cursor": {"hwnd": 2, "pid": 2},
                }
            ),
            encoding="utf-8",
        )

        monkeypatch.setattr(sidecar, "ROOT", root)
        monkeypatch.setattr(sidecar, "STATE_FILE", state_path)
        monkeypatch.setattr(sidecar, "DEFAULT_CONFIG", cfg_path)
        monkeypatch.setattr(sidecar, "_DAEMON_MODE", True)

        stopped = {"n": 0}

        class FakeFollow:
            enabled = True

            def stop(self):
                stopped["n"] += 1

            def status(self):
                return {"enabled": self.enabled, "running": False, "hook_installed": False}

        fake = FakeFollow()
        monkeypatch.setattr(sidecar, "_FOLLOW", fake)

        cfg = sidecar.load_config(cfg_path)
        code = sidecar.cmd_set_live_follow(cfg, enabled=False)
        assert code == 0
        assert stopped["n"] == 1
        assert cfg["live_follow"] is False
        state = json.loads(state_path.read_text(encoding="utf-8"))
        assert state.get("attached") is True
        persisted = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert persisted.get("live_follow") is False
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_set_live_follow_true_starts_when_attached(monkeypatch):
    import shutil
    import main as sidecar

    root = Path(__file__).resolve().parent / "_tmp_rc_live_on"
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    try:
        cfg_path = root / "config.json"
        cfg_path.write_text(
            json.dumps(
                {
                    "preset": "normal",
                    "gap": 0,
                    "daemon_host": "127.0.0.1",
                    "daemon_port": 27845,
                    "live_follow": False,
                    "obsidian_process": "Obsidian.exe",
                    "cursor_process": "Cursor.exe",
                    "cursor_ratio": 0.3,
                }
            ),
            encoding="utf-8",
        )
        state_path = root / ".sidecar.state.json"
        state_path.write_text(
            json.dumps(
                {
                    "attached": True,
                    "obsidian": {"hwnd": 11, "pid": 1, "process_name": "Obsidian.exe"},
                    "cursor": {"hwnd": 22, "pid": 2, "process_name": "Cursor.exe"},
                    "right_rect": [700, 0, 1000, 800],
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(sidecar, "ROOT", root)
        monkeypatch.setattr(sidecar, "STATE_FILE", state_path)
        monkeypatch.setattr(sidecar, "DEFAULT_CONFIG", cfg_path)
        monkeypatch.setattr(sidecar, "_DAEMON_MODE", True)
        monkeypatch.setattr(sidecar, "_FOLLOW", None)
        monkeypatch.setattr(sidecar, "binding_live", lambda _r: True)
        monkeypatch.setattr(
            sidecar,
            "refresh_attachment_truth",
            lambda state=None: {
                "attached": True,
                "state_valid": True,
                "reason": "ok",
                "state": json.loads(state_path.read_text(encoding="utf-8")),
            },
        )

        started: list[tuple] = []

        class FakeSvc:
            enabled = False

            def __init__(self, **kwargs):
                pass

            def start(self, oh, ch, width):
                started.append((oh, ch, width))
                self.enabled = True

            def stop(self):
                pass

            def status(self):
                return {"enabled": True, "running": True, "hook_installed": True}

        monkeypatch.setattr(sidecar, "LiveFollowService", FakeSvc)

        cfg = sidecar.load_config(cfg_path)
        code = sidecar.cmd_set_live_follow(cfg, enabled=True)
        assert code == 0
        assert cfg["live_follow"] is True
        assert started and started[0][0] == 11 and started[0][1] == 22
        persisted = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert persisted.get("live_follow") is True
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_strict_acceptance_forbids_rescue():
    import acceptance_live_follow as acc

    assert acc.STRICT_ACCEPTANCE is True
    with pytest.raises(RuntimeError, match="forbids rescue"):
        acc.forbid_rescue("follow_now")


def test_refresh_attachment_truth_uses_lock(monkeypatch):
    import shutil
    import main as sidecar

    root = Path(__file__).resolve().parent / "_tmp_rc_lock"
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    try:
        state_path = root / "state.json"
        state_path.write_text(json.dumps({"attached": False}), encoding="utf-8")
        monkeypatch.setattr(sidecar, "STATE_FILE", state_path)

        held = {"ok": False}

        def wrapped_read():
            held["ok"] = sidecar.LIFECYCLE_LOCK._is_owned()  # type: ignore[attr-defined]
            return json.loads(state_path.read_text(encoding="utf-8"))

        monkeypatch.setattr(sidecar, "read_state", wrapped_read)
        truth = sidecar.refresh_attachment_truth()
        assert truth["attached"] is False
        assert held["ok"] is True
    finally:
        shutil.rmtree(root, ignore_errors=True)
