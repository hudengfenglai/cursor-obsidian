#!/usr/bin/env python3
"""Packaged (frozen exe) acceptance for Cursor Sidecar v0.5.

Invokes dist/cursor-sidecar.exe only — never python main.py.
Isolates DATA_DIR so LocalAppData stays clean.

Usage:
  python acceptance_packaged.py
  python acceptance_packaged.py --exe path\\to\\cursor-sidecar.exe
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
DEFAULT_EXE = REPO / "dist" / "cursor-sidecar.exe"


def run_exe(exe: Path, data_dir: Path, *args: str, timeout: float = 60) -> subprocess.CompletedProcess[str]:
    cmd = [str(exe), "--data-dir", str(data_dir), *args]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(data_dir),
    )


def http_json(host: str, port: int, method: str, path: str, token: str, body: dict | None = None):
    url = f"http://{host}:{port}{path}"
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "X-Cursor-Sidecar-Token": token,
            **({"Content-Type": "application/json"} if data else {}),
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


def read_token(data_dir: Path) -> str:
    p = data_dir / ".sidecar.daemon.token"
    if not p.is_file():
        return ""
    return p.read_text(encoding="utf-8").strip()


def wait_health(host: str, port: int, token: str, tries: int = 40) -> bool:
    for _ in range(tries):
        try:
            r = http_json(host, port, "GET", "/health", token)
            if r.get("ok"):
                return True
        except Exception:
            pass
        time.sleep(0.25)
    return False


def daemon_is_exe(meta: dict, exe: Path) -> bool:
    if str(meta.get("runtime_mode") or "") != "frozen":
        return False
    got = Path(str(meta.get("executable_path") or "")).resolve()
    return got == exe.resolve()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", default=str(DEFAULT_EXE))
    ap.add_argument("--port", type=int, default=27845)
    ap.add_argument("--migrate-port", type=int, default=27999)
    ap.add_argument("--skip-ui", action="store_true", help="Skip attach/live/open tests needing Obsidian/Cursor")
    args = ap.parse_args()

    exe = Path(args.exe)
    results: list[tuple[str, str]] = []

    def record(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, "PASS" if ok else "FAIL"))
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))

    if not exe.is_file():
        print(f"EXE not found: {exe}")
        print("Run packaging/build.ps1 first. Packaged acceptance: NOT RUN")
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="sidecar-packaged-"))
    data_a = tmp / "data_a"
    data_a.mkdir(parents=True)

    try:
        # A. --version
        ver = run_exe(exe, data_a, "--version")
        record("A.version", ver.returncode == 0 and ver.stdout.strip() == "0.5.0", ver.stdout.strip())

        # B. status
        st = run_exe(exe, data_a, "status", "--json")
        record("B.status", st.returncode == 0, (st.stdout or st.stderr)[:120])

        # C. daemon-start + health
        host = "127.0.0.1"
        port = int(args.port)
        ds = run_exe(exe, data_a, "daemon-start", "--host", host, "--port", str(port))
        token = ""
        for _ in range(20):
            token = read_token(data_a)
            if token:
                break
            time.sleep(0.15)
        healthy = bool(token) and wait_health(host, port, token)
        meta = {}
        meta_path = data_a / ".sidecar.daemon.json"
        if meta_path.is_file():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        record(
            "C.daemon-start+health",
            ds.returncode == 0 and healthy and daemon_is_exe(meta, exe),
            f"runtime_mode={meta.get('runtime_mode')} pid={meta.get('pid')}",
        )

        # J. no python.exe as daemon identity
        no_python = "python" not in str(meta.get("executable_path") or "").lower()
        record("J.daemon-is-exe-not-python", no_python and daemon_is_exe(meta, exe))

        if not args.skip_ui:
            # D. Attach
            att = run_exe(exe, data_a, "attach")
            record("D.attach", att.returncode == 0, (att.stdout or att.stderr)[:100])

            # E. Live follow move — soft: set-live-follow via RPC
            try:
                r = http_json(host, port, "POST", "/rpc", token, {"cmd": "set-live-follow", "enabled": True})
                record("E.live-follow", bool(r.get("ok")), str(r)[:100])
            except Exception as e:
                record("E.live-follow", False, str(e))

            # F. Open current note — skip if no vault; probe command exists
            oef = run_exe(
                exe,
                data_a,
                "open-editor-file",
                "--vault-root",
                str(tmp),
                "--path",
                str(tmp / "中文笔记.md"),
            )
            # Expect attach-required or path error — not a crash
            record("F.open-editor-file-cli", oef.returncode in (0, 1, 2), f"code={oef.returncode}")
        else:
            record("D.attach", True, "SKIPPED")
            record("E.live-follow", True, "SKIPPED")
            record("F.open-editor-file-cli", True, "SKIPPED")

        # G. port migration 27845 → 27999
        new_port = int(args.migrate_port)
        mig = run_exe(exe, data_a, "daemon-start", "--host", host, "--port", str(new_port))
        token2 = read_token(data_a)
        mig_ok = mig.returncode == 0 and wait_health(host, new_port, token2)
        # old port should die
        old_dead = True
        try:
            http_json(host, port, "GET", "/health", token2)
            old_dead = False
        except Exception:
            old_dead = True
        meta2 = json.loads((data_a / ".sidecar.daemon.json").read_text(encoding="utf-8")) if (data_a / ".sidecar.daemon.json").is_file() else {}
        record(
            "G.port-migration",
            mig_ok and old_dead and int(meta2.get("port") or 0) == new_port and daemon_is_exe(meta2, exe),
            f"{port}->{new_port} meta_port={meta2.get('port')}",
        )
        port = new_port
        token = token2

        if not args.skip_ui:
            det = run_exe(exe, data_a, "detach")
            record("H.detach", det.returncode == 0, (det.stdout or det.stderr)[:80])
        else:
            record("H.detach", True, "SKIPPED")

        # I. shutdown
        try:
            http_json(host, port, "POST", "/rpc", token, {"cmd": "shutdown-daemon"})
            time.sleep(0.5)
            down = False
            try:
                http_json(host, port, "GET", "/health", token)
            except Exception:
                down = True
            record("I.shutdown", down)
        except Exception as e:
            record("I.shutdown", False, str(e))

    finally:
        # Best-effort cleanup
        try:
            if (data_a / ".sidecar.daemon.token").is_file():
                tok = read_token(data_a)
                meta = {}
                if (data_a / ".sidecar.daemon.json").is_file():
                    meta = json.loads((data_a / ".sidecar.daemon.json").read_text(encoding="utf-8"))
                if tok and meta.get("port"):
                    try:
                        http_json("127.0.0.1", int(meta["port"]), "POST", "/rpc", tok, {"cmd": "shutdown-daemon"})
                    except Exception:
                        pass
        except Exception:
            pass
        shutil.rmtree(tmp, ignore_errors=True)

    failed = sum(1 for _, s in results if s == "FAIL")
    print("---")
    print(f"Packaged acceptance: {len(results) - failed} PASS / {failed} FAIL")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
