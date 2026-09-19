"""v0.5 frozen-runtime path / identity / config bootstrap tests (pure logic + mocks)."""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import runtime_paths as rp
import main as sidecar_main


def _local_tmp(name: str) -> Path:
    root = Path(__file__).resolve().parent / name
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    return root


def test_resolve_data_dir_priority_cli_over_env(monkeypatch):
    base = _local_tmp("_tmp_v05_prio")
    try:
        cli = base / "cli"
        env = base / "env"
        monkeypatch.setenv("CURSOR_SIDECAR_DATA_DIR", str(env))
        monkeypatch.setenv("LOCALAPPDATA", str(base / "local"))
        got = rp.resolve_data_dir(cli_override=str(cli), frozen=True)
        assert got == cli.resolve()
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_resolve_data_dir_env_over_frozen_default(monkeypatch):
    base = _local_tmp("_tmp_v05_env")
    try:
        env = base / "envdata"
        local = base / "LocalAppData"
        monkeypatch.setenv("CURSOR_SIDECAR_DATA_DIR", str(env))
        monkeypatch.setenv("LOCALAPPDATA", str(local))
        got = rp.resolve_data_dir(env=dict(os.environ), frozen=True)
        assert got == env.resolve()
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_resolve_data_dir_frozen_localappdata(monkeypatch):
    base = _local_tmp("_tmp_v05_local")
    try:
        monkeypatch.delenv("CURSOR_SIDECAR_DATA_DIR", raising=False)
        local = base / "LocalAppData"
        monkeypatch.setenv("LOCALAPPDATA", str(local))
        got = rp.resolve_data_dir(env=dict(os.environ), frozen=True)
        assert got == (local / "CursorSidecar").resolve()
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_resolve_data_dir_source_fallback(monkeypatch):
    base = _local_tmp("_tmp_v05_src")
    try:
        monkeypatch.delenv("CURSOR_SIDECAR_DATA_DIR", raising=False)
        src = base / "src"
        src.mkdir()
        got = rp.resolve_data_dir(frozen=False, source_fallback=src)
        assert got == src.resolve()
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_build_self_command_source(monkeypatch):
    monkeypatch.setattr(rp, "is_frozen", lambda: False)
    monkeypatch.setattr(rp, "code_dir", lambda: Path("C:/repo/cursor-sidecar"))
    cmd = rp.build_self_command("daemon", "--port", "27999", python="C:/Python/python.exe")
    assert cmd[0] == "C:/Python/python.exe"
    assert cmd[1].replace("\\", "/").endswith("cursor-sidecar/main.py")
    assert cmd[2:] == ["daemon", "--port", "27999"]


def test_build_self_command_frozen(monkeypatch):
    monkeypatch.setattr(rp, "is_frozen", lambda: True)
    monkeypatch.setattr(sys, "executable", r"C:\plugin\bin\cursor-sidecar.exe")
    cmd = rp.build_self_command("daemon", "--port", "27999")
    assert cmd == [r"C:\plugin\bin\cursor-sidecar.exe", "daemon", "--port", "27999"]


def test_build_self_command_frozen_endpoint_migration(monkeypatch):
    monkeypatch.setattr(rp, "is_frozen", lambda: True)
    monkeypatch.setattr(sys, "executable", r"D:\release\bin\cursor-sidecar.exe")
    cmd = rp.build_self_command(
        "--data-dir",
        r"C:\Users\u\AppData\Local\CursorSidecar",
        "-c",
        r"C:\Users\u\AppData\Local\CursorSidecar\config.json",
        "daemon",
        "--host",
        "127.0.0.1",
        "--port",
        "27999",
    )
    assert cmd[0].endswith("cursor-sidecar.exe")
    assert "main.py" not in " ".join(cmd)
    assert cmd[-1] == "27999"
    assert "daemon" in cmd


def test_config_bootstrap_creates_file(monkeypatch):
    data = _local_tmp("_tmp_v05_boot")
    try:
        monkeypatch.setattr(sidecar_main, "DATA_DIR", data)
        cfg_path = data / "config.json"
        assert not cfg_path.exists()
        cfg = sidecar_main.load_config(cfg_path)
        assert cfg_path.is_file()
        assert cfg["preset"] == "normal"
        assert cfg["daemon_port"] == 27845
        disk = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert "preset" in disk
    finally:
        shutil.rmtree(data, ignore_errors=True)


def test_config_bootstrap_merges_disk_over_defaults(monkeypatch):
    data = _local_tmp("_tmp_v05_merge")
    try:
        monkeypatch.setattr(sidecar_main, "DATA_DIR", data)
        cfg_path = data / "config.json"
        cfg_path.write_text(json.dumps({"daemon_port": 27999, "preset": "wide"}), encoding="utf-8")
        cfg = sidecar_main.load_config(cfg_path)
        assert cfg["daemon_port"] == 27999
        assert cfg["preset"] == "wide"
        assert cfg["cursor_ratio"] == pytest.approx(0.42)
    finally:
        shutil.rmtree(data, ignore_errors=True)


def test_version_cli(capsys):
    assert sidecar_main.main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == "0.5.0"
    assert sidecar_main.main(["version"]) == 0
    assert capsys.readouterr().out.strip() == "0.5.0"


def test_frozen_mode_does_not_use_repo_root_for_state(monkeypatch):
    base = _local_tmp("_tmp_v05_frozen_state")
    try:
        local = base / "LocalAppData"
        monkeypatch.setenv("LOCALAPPDATA", str(local))
        monkeypatch.delenv("CURSOR_SIDECAR_DATA_DIR", raising=False)
        monkeypatch.setattr(rp, "is_frozen", lambda: True)
        monkeypatch.setattr(rp, "code_dir", lambda: base / "exe_dir")
        paths = rp.build_paths()
        assert paths.data_dir == (local / "CursorSidecar").resolve()
        assert paths.state_path.parent == paths.data_dir
        assert paths.config_path.parent == paths.data_dir
        assert "CursorSidecar" in str(paths.data_dir)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_init_runtime_data_dir_override():
    override = _local_tmp("_tmp_v05_override")
    try:
        paths = rp.init_runtime(data_dir_override=str(override))
        assert paths.data_dir == override.resolve()
        assert paths.daemon_token_path == override.resolve() / ".sidecar.daemon.token"
    finally:
        shutil.rmtree(override, ignore_errors=True)


def _fake_proc(*, ctime, cmdline, exe):
    proc = MagicMock()
    proc.create_time.return_value = ctime
    proc.cmdline.return_value = cmdline
    proc.exe.return_value = exe
    return proc


def test_verify_source_daemon_identity(monkeypatch):
    meta = {
        "pid": 4242,
        "process_create_time": 1000.0,
        "host": "127.0.0.1",
        "port": 27845,
        "runtime_mode": "source",
        "executable_path": r"C:\Python\python.exe",
    }
    proc = _fake_proc(
        ctime=1000.0,
        cmdline=["C:\\Python\\python.exe", "D:\\repo\\main.py", "daemon"],
        exe=r"C:\Python\python.exe",
    )
    monkeypatch.setattr(sidecar_main.psutil, "Process", lambda pid: proc)
    check = sidecar_main.verify_sidecar_daemon_process(meta)
    assert check["ok"] is True


def test_verify_frozen_daemon_identity(monkeypatch):
    exe = r"C:\plugin\bin\cursor-sidecar.exe"
    meta = {
        "pid": 5252,
        "process_create_time": 2000.0,
        "host": "127.0.0.1",
        "port": 27999,
        "runtime_mode": "frozen",
        "executable_path": exe,
    }
    proc = _fake_proc(
        ctime=2000.0,
        cmdline=[exe, "--data-dir", "X", "daemon", "--port", "27999"],
        exe=exe,
    )
    monkeypatch.setattr(sidecar_main.psutil, "Process", lambda pid: proc)
    check = sidecar_main.verify_sidecar_daemon_process(meta)
    assert check["ok"] is True
    assert check["runtime_mode"] == "frozen"


def test_verify_frozen_rejects_wrong_exe(monkeypatch):
    meta = {
        "pid": 5252,
        "process_create_time": 2000.0,
        "host": "127.0.0.1",
        "port": 27999,
        "runtime_mode": "frozen",
        "executable_path": r"C:\plugin\bin\cursor-sidecar.exe",
    }
    proc = _fake_proc(
        ctime=2000.0,
        cmdline=["notepad.exe", "daemon"],
        exe=r"C:\Windows\notepad.exe",
    )
    monkeypatch.setattr(sidecar_main.psutil, "Process", lambda pid: proc)
    check = sidecar_main.verify_sidecar_daemon_process(meta)
    assert check["ok"] is False
    assert check["reason"] == "exe_mismatch"


def test_verify_frozen_rejects_name_only_without_daemon(monkeypatch):
    exe = r"C:\plugin\bin\cursor-sidecar.exe"
    meta = {
        "pid": 1,
        "process_create_time": 1.0,
        "runtime_mode": "frozen",
        "executable_path": exe,
    }
    proc = _fake_proc(ctime=1.0, cmdline=[exe, "status"], exe=exe)
    monkeypatch.setattr(sidecar_main.psutil, "Process", lambda pid: proc)
    check = sidecar_main.verify_sidecar_daemon_process(meta)
    assert check["ok"] is False
    assert check["reason"] == "not_sidecar_daemon"


def test_write_daemon_meta_includes_runtime_fields(monkeypatch):
    base = _local_tmp("_tmp_v05_meta")
    try:
        meta_path = base / ".sidecar.daemon.json"
        monkeypatch.setattr(sidecar_main, "DAEMON_META_FILE", meta_path)
        monkeypatch.setattr(sidecar_main, "CODE_DIR", base)
        monkeypatch.setattr(sidecar_main, "runtime_mode", lambda: "source")
        monkeypatch.setattr(sys, "executable", r"C:\Python\python.exe")

        class P:
            def create_time(self):
                return 42.0

        monkeypatch.setattr(sidecar_main.psutil, "Process", lambda pid: P())
        sidecar_main.write_daemon_meta(pid=99, host="127.0.0.1", port=27845)
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        assert data["runtime_mode"] == "source"
        assert data["executable_path"].endswith("python.exe")
        assert data["entrypoint"] == "main.py"
        assert data["version"] == "0.5.0"
    finally:
        shutil.rmtree(base, ignore_errors=True)
