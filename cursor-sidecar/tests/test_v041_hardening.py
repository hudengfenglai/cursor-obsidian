"""v0.4.1 hardening: capability probe + daemon endpoint migration."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest.mock import MagicMock

from editor_bridge import (
    KNOWN_EDITOR_DEFAULTS,
    _capability_cache_key,
    clear_capability_cache,
    probe_editor_capabilities,
)


def _local_tmp(name: str) -> Path:
    root = Path(__file__).resolve().parent / name
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    return root


def test_probe_help_contains_all_flags(monkeypatch):
    clear_capability_cache()

    def fake_run(cmd, **kwargs):
        m = MagicMock()
        m.stdout = "Options:\n  --reuse-window\n  --goto\n  --classic\n  --new-window\n"
        m.stderr = ""
        return m

    monkeypatch.setattr("editor_bridge.subprocess.run", fake_run)
    caps = probe_editor_capabilities(r"C:\Apps\Cursor.exe", force=True)
    assert caps["source"] == "help"
    assert caps["probed"] is True
    assert caps["reuse_window"] is True
    assert caps["goto"] is True
    assert caps["classic"] is True
    assert caps["new_window"] is True


def test_probe_help_omits_classic(monkeypatch):
    clear_capability_cache()

    def fake_run(cmd, **kwargs):
        m = MagicMock()
        m.stdout = "Usage: Cursor.exe [--reuse-window] [--goto] [--new-window]"
        m.stderr = ""
        return m

    monkeypatch.setattr("editor_bridge.subprocess.run", fake_run)
    caps = probe_editor_capabilities(r"C:\Apps\Cursor.exe", force=True)
    assert caps["classic"] is False
    assert caps["goto"] is True
    assert caps["reuse_window"] is True
    assert caps["source"] == "help"


def test_probe_help_omits_goto(monkeypatch):
    clear_capability_cache()

    def fake_run(cmd, **kwargs):
        m = MagicMock()
        m.stdout = "Usage: Cursor.exe [--reuse-window] [--classic]"
        m.stderr = ""
        return m

    monkeypatch.setattr("editor_bridge.subprocess.run", fake_run)
    caps = probe_editor_capabilities(r"C:\Apps\Cursor.exe", force=True)
    assert caps["goto"] is False
    assert caps["classic"] is True


def test_probe_empty_help_uses_fallback(monkeypatch):
    clear_capability_cache()

    def fake_run(cmd, **kwargs):
        m = MagicMock()
        m.stdout = "   "
        m.stderr = ""
        return m

    monkeypatch.setattr("editor_bridge.subprocess.run", fake_run)
    caps = probe_editor_capabilities(r"C:\Apps\Cursor.exe", force=True)
    assert caps["source"] == "fallback"
    assert caps["probed"] is False
    assert caps["classic"] == KNOWN_EDITOR_DEFAULTS["classic"]


def test_probe_failure_uses_fallback(monkeypatch):
    clear_capability_cache()

    def fake_run(cmd, **kwargs):
        raise TimeoutError("boom")

    monkeypatch.setattr("editor_bridge.subprocess.run", fake_run)
    caps = probe_editor_capabilities(r"C:\Apps\Cursor.exe", force=True)
    assert caps["source"] == "fallback"
    assert caps["probed"] is False


def test_capability_cache_invalidates_on_mtime_change(monkeypatch):
    clear_capability_cache()
    root = _local_tmp("_tmp_v041_mtime")
    try:
        exe = root / "Cursor.exe"
        exe.write_bytes(b"fake")
        calls = {"n": 0}

        def fake_run(cmd, **kwargs):
            calls["n"] += 1
            m = MagicMock()
            m.stdout = f"--reuse-window --goto call{calls['n']}"
            m.stderr = ""
            return m

        monkeypatch.setattr("editor_bridge.subprocess.run", fake_run)
        c1 = probe_editor_capabilities(str(exe))
        c2 = probe_editor_capabilities(str(exe))
        assert calls["n"] == 1
        assert c1 == c2

        import os
        import time

        time.sleep(0.05)
        os.utime(exe, None)
        c3 = probe_editor_capabilities(str(exe))
        assert calls["n"] == 2
        assert c3["source"] == "help"
        assert _capability_cache_key(str(exe)).endswith(
            str(int(exe.stat().st_mtime_ns))
        ) or "|" in _capability_cache_key(str(exe))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_probe_never_invokes_agent(monkeypatch):
    clear_capability_cache()
    seen: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        seen.append(list(cmd))
        assert "agent" not in cmd[0].lower()
        assert kwargs.get("shell") is False
        m = MagicMock()
        m.stdout = "--reuse-window"
        m.stderr = ""
        return m

    monkeypatch.setattr("editor_bridge.subprocess.run", fake_run)
    probe_editor_capabilities(r"C:\Cursor\Cursor.exe", force=True)
    assert seen == [[r"C:\Cursor\Cursor.exe", "--help"]]


def test_daemon_meta_no_token(monkeypatch):
    import main as sidecar

    root = _local_tmp("_tmp_v041_meta")
    try:
        monkeypatch.setattr(sidecar, "ROOT", root)
        monkeypatch.setattr(sidecar, "DAEMON_META_FILE", root / ".sidecar.daemon.json")
        sidecar.write_daemon_meta(
            pid=12345, host="127.0.0.1", port=27845, process_create_time=1.0
        )
        raw = (root / ".sidecar.daemon.json").read_text(encoding="utf-8")
        data = json.loads(raw)
        assert "token" not in data
        assert "pid" in data and data["port"] == 27845
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_verify_rejects_unrelated_process(monkeypatch):
    import main as sidecar

    class FakeProc:
        def create_time(self):
            return 100.0

        def cmdline(self):
            return ["python.exe", "unrelated.py"]

    monkeypatch.setattr(sidecar.psutil, "Process", lambda pid: FakeProc())
    meta = {"pid": 1, "process_create_time": 100.0, "host": "127.0.0.1", "port": 27845}
    v = sidecar.verify_sidecar_daemon_process(meta)
    assert v["ok"] is False
    assert v["reason"] == "not_sidecar_daemon"


def test_verify_create_time_mismatch(monkeypatch):
    import main as sidecar

    class FakeProc:
        def create_time(self):
            return 999.0

        def cmdline(self):
            return ["python.exe", "main.py", "daemon"]

    monkeypatch.setattr(sidecar.psutil, "Process", lambda pid: FakeProc())
    meta = {"pid": 1, "process_create_time": 100.0, "host": "127.0.0.1", "port": 27845}
    v = sidecar.verify_sidecar_daemon_process(meta)
    assert v["ok"] is False
    assert v["reason"] == "create_time_mismatch"


def test_verify_accepts_sidecar_daemon(monkeypatch):
    import main as sidecar

    class FakeProc:
        def create_time(self):
            return 100.0

        def cmdline(self):
            return ["python.exe", str(Path("main.py")), "daemon", "--port", "27845"]

    monkeypatch.setattr(sidecar.psutil, "Process", lambda pid: FakeProc())
    meta = {"pid": 42, "process_create_time": 100.0, "host": "127.0.0.1", "port": 27845}
    v = sidecar.verify_sidecar_daemon_process(meta)
    assert v["ok"] is True
    assert v["pid"] == 42


def test_malformed_meta_safe():
    import main as sidecar

    assert sidecar.verify_sidecar_daemon_process({})["ok"] is False
    assert sidecar.verify_sidecar_daemon_process({"pid": "x"})["ok"] is False


def test_daemon_start_reuses_same_endpoint(monkeypatch):
    import main as sidecar

    root = _local_tmp("_tmp_v041_reuse")
    try:
        monkeypatch.setattr(sidecar, "ROOT", root)
        monkeypatch.setattr(sidecar, "DAEMON_META_FILE", root / ".sidecar.daemon.json")
        monkeypatch.setattr(sidecar, "DAEMON_PID_FILE", root / ".sidecar.daemon.pid")
        monkeypatch.setattr(sidecar, "TOKEN_FILE", root / ".sidecar.daemon.token")
        monkeypatch.setattr(sidecar, "DEFAULT_CONFIG", root / "config.json")
        (root / "config.json").write_text("{}", encoding="utf-8")
        (root / ".sidecar.daemon.token").write_text("tok\n", encoding="utf-8")
        sidecar.write_daemon_meta(
            pid=7, host="127.0.0.1", port=27845, process_create_time=50.0
        )
        monkeypatch.setattr(
            sidecar,
            "verify_sidecar_daemon_process",
            lambda m: {
                "ok": True,
                "pid": 7,
                "host": "127.0.0.1",
                "port": 27845,
                "process_create_time": 50.0,
            },
        )
        spawned = {"n": 0}

        def fake_popen(*a, **k):
            spawned["n"] += 1
            return MagicMock()

        monkeypatch.setattr(sidecar.subprocess, "Popen", fake_popen)
        cfg = {"daemon_host": "127.0.0.1", "daemon_port": 27845}
        assert sidecar.cmd_daemon_start(cfg) == 0
        assert spawned["n"] == 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_daemon_start_migrates_different_port(monkeypatch):
    import main as sidecar

    root = _local_tmp("_tmp_v041_migrate")
    try:
        monkeypatch.setattr(sidecar, "ROOT", root)
        monkeypatch.setattr(sidecar, "DAEMON_META_FILE", root / ".sidecar.daemon.json")
        monkeypatch.setattr(sidecar, "DAEMON_PID_FILE", root / ".sidecar.daemon.pid")
        monkeypatch.setattr(sidecar, "TOKEN_FILE", root / ".sidecar.daemon.token")
        monkeypatch.setattr(sidecar, "DEFAULT_CONFIG", root / "config.json")
        (root / "config.json").write_text("{}", encoding="utf-8")
        (root / ".sidecar.daemon.token").write_text("tok\n", encoding="utf-8")
        sidecar.write_daemon_meta(
            pid=7, host="127.0.0.1", port=27845, process_create_time=50.0
        )
        monkeypatch.setattr(
            sidecar,
            "verify_sidecar_daemon_process",
            lambda m: {
                "ok": True,
                "pid": 7,
                "host": "127.0.0.1",
                "port": 27845,
                "process_create_time": 50.0,
            },
        )
        stopped = {"n": 0}
        monkeypatch.setattr(
            sidecar,
            "stop_existing_daemon_safe",
            lambda meta: stopped.__setitem__("n", stopped["n"] + 1) or "stopped_via_rpc",
        )
        spawned = {"args": None}

        def fake_popen(args, **k):
            spawned["args"] = list(args)
            return MagicMock()

        monkeypatch.setattr(sidecar.subprocess, "Popen", fake_popen)
        monkeypatch.setattr(sidecar.time, "sleep", lambda _s: None)
        cfg = {"daemon_host": "127.0.0.1", "daemon_port": 30000}
        assert sidecar.cmd_daemon_start(cfg) == 0
        assert stopped["n"] == 1
        assert spawned["args"] is not None
        assert "--port" in spawned["args"]
        assert "30000" in spawned["args"]
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_stale_pid_starts_new(monkeypatch):
    import main as sidecar

    root = _local_tmp("_tmp_v041_stale")
    try:
        monkeypatch.setattr(sidecar, "ROOT", root)
        monkeypatch.setattr(sidecar, "DAEMON_META_FILE", root / ".sidecar.daemon.json")
        monkeypatch.setattr(sidecar, "DAEMON_PID_FILE", root / ".sidecar.daemon.pid")
        monkeypatch.setattr(sidecar, "TOKEN_FILE", root / ".sidecar.daemon.token")
        monkeypatch.setattr(sidecar, "DEFAULT_CONFIG", root / "config.json")
        (root / "config.json").write_text("{}", encoding="utf-8")
        (root / ".sidecar.daemon.token").write_text("tok\n", encoding="utf-8")
        sidecar.write_daemon_meta(
            pid=999001, host="127.0.0.1", port=27845, process_create_time=1.0
        )
        monkeypatch.setattr(
            sidecar,
            "verify_sidecar_daemon_process",
            lambda m: {"ok": False, "reason": "gone"},
        )
        spawned = {"n": 0}
        monkeypatch.setattr(
            sidecar.subprocess,
            "Popen",
            lambda *a, **k: spawned.__setitem__("n", spawned["n"] + 1) or MagicMock(),
        )
        monkeypatch.setattr(sidecar.time, "sleep", lambda _s: None)
        cfg = {"daemon_host": "127.0.0.1", "daemon_port": 27845}
        assert sidecar.cmd_daemon_start(cfg) == 0
        assert spawned["n"] == 1
    finally:
        shutil.rmtree(root, ignore_errors=True)
