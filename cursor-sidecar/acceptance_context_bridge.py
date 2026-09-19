"""Windows acceptance for v0.4 Context Bridge (self-restoring).

Does not modify note content. Does not touch Cursor accounts.
Requires Sidecar detached at start; attaches for the run then restores.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from window import (  # noqa: E402
    apply_window_placement,
    enable_dpi_awareness,
    find_cursor_window,
    find_obsidian_window,
    snapshot_window,
    validate_window_binding,
)
import main as sidecar  # noqa: E402


def load_cfg() -> dict[str, Any]:
    return sidecar.load_config(ROOT / "config.json")


def result(name: str, ok: bool | None, detail: str) -> bool | None:
    if ok is None:
        print(f"[SKIP] {name}: {detail}")
        return None
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    return ok


def backup_state_file() -> bytes | None:
    path = sidecar.STATE_FILE
    if not path.is_file():
        return None
    return path.read_bytes()


def restore_state_file(backup: bytes | None) -> None:
    path = sidecar.STATE_FILE
    if backup is None:
        sidecar.write_state(
            {"attached": False, "timestamp": time.time(), "version": sidecar.VERSION}
        )
        return
    tmp = path.with_name(path.name + ".acceptance.bak.tmp")
    tmp.write_bytes(backup)
    tmp.replace(path)


def restore_exact_snapshot(snap: dict[str, Any] | None, label: str) -> str:
    if not snap:
        return "skip"
    if not validate_window_binding(snap).get("ok"):
        print(f"[restore] {label}: gone")
        return "gone"
    ok = apply_window_placement(int(snap["hwnd"]), snap)
    print(f"[restore] {label}: {'ok' if ok else 'fail'}")
    return "ok" if ok else "fail"


def daemon_token() -> str:
    if sidecar.TOKEN_FILE.is_file():
        return sidecar.TOKEN_FILE.read_text(encoding="utf-8").strip()
    return sidecar.ensure_daemon_token()


def http_json(cfg: dict[str, Any], method: str, path: str, body: dict | None = None) -> dict[str, Any]:
    url = f"http://{cfg['daemon_host']}:{int(cfg['daemon_port'])}{path}"
    data = None
    headers = {"X-Cursor-Sidecar-Token": daemon_token()}
    if body is not None:
        raw = json.dumps(body).encode("utf-8")
        data = raw
        headers["Content-Type"] = "application/json"
        headers["Content-Length"] = str(len(raw))
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        raw_err = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw_err or "{}")
        except json.JSONDecodeError:
            payload = {"ok": False, "error": raw_err or str(exc)}
        payload.setdefault("ok", False)
        return payload


def daemon_healthy(cfg: dict[str, Any]) -> bool:
    try:
        return bool(http_json(cfg, "GET", "/health").get("ok"))
    except Exception:
        return False


def ensure_daemon(cfg: dict[str, Any]) -> bool:
    if daemon_healthy(cfg):
        probe = http_json(cfg, "POST", "/rpc", {"cmd": "open-editor-file", "path": "x"})
        # Old daemon → unknown cmd; restart
        if probe.get("error") == "sidecar_not_attached" or probe.get("error") == "path_required" or probe.get("error") == "vault_root_required":
            return False
        if probe.get("error") and "unknown" in str(probe.get("error")):
            sidecar.cmd_stop(cfg)
            time.sleep(0.4)
        else:
            # Healthy enough if status works
            if daemon_healthy(cfg):
                st = http_json(cfg, "GET", "/status")
                if st.get("ok"):
                    return False
    sidecar.cmd_daemon_start(cfg)
    for _ in range(24):
        time.sleep(0.25)
        if daemon_healthy(cfg):
            return True
    raise RuntimeError("daemon not healthy")


def wait_windows(cfg: dict[str, Any], timeout: float = 20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        obs = find_obsidian_window(cfg["obsidian_process"], cfg.get("obsidian_title_hint", ""))
        cur = find_cursor_window(cfg["cursor_process"], cfg.get("cursor_title_hint", ""))
        if obs and cur:
            return obs, cur
        time.sleep(0.4)
    return (
        find_obsidian_window(cfg["obsidian_process"], cfg.get("obsidian_title_hint", "")),
        find_cursor_window(cfg["cursor_process"], cfg.get("cursor_title_hint", "")),
    )


def run_scenarios(cfg: dict[str, Any], vault_file: Path) -> list[bool | None]:
    outcomes: list[bool | None] = []

    print("--- Attach for Context Bridge ---")
    att = http_json(cfg, "POST", "/rpc", {"cmd": "attach"})
    time.sleep(0.5)
    st0 = http_json(cfg, "GET", "/status").get("status") or {}
    bound_hwnd = ((st0.get("cursor") or {}).get("hwnd"))
    live0 = (st0.get("live_follow") or {}).get("running")

    print("\n--- A: open-editor-file ---")
    r_a = http_json(
        cfg,
        "POST",
        "/rpc",
        {
            "cmd": "open-editor-file",
            "vault_root": str(vault_file.parent),
            "path": str(vault_file),
            "line": 2,
            "column": 1,
            "focus": True,
        },
    )
    outcomes.append(
        result(
            "A open file",
            bool(r_a.get("ok")) and r_a.get("method") in ("exe_goto", "exe_file", "deeplink"),
            f"ok={r_a.get('ok')} method={r_a.get('method')} err={r_a.get('error')}",
        )
    )

    print("\n--- B: line/column echoed ---")
    outcomes.append(
        result(
            "B line/column",
            r_a.get("line") == 2 and r_a.get("column") == 1,
            f"line={r_a.get('line')} column={r_a.get('column')}",
        )
    )

    print("\n--- C: Chinese path (synthetic under vault parent if needed) ---")
    cn_dir = vault_file.parent / "03 知识"
    cn_dir.mkdir(exist_ok=True)
    cn_file = cn_dir / "一致连续.md"
    cn_file.write_text("# test\nline2\n", encoding="utf-8")
    try:
        r_c = http_json(
            cfg,
            "POST",
            "/rpc",
            {
                "cmd": "open-editor-file",
                "vault_root": str(vault_file.parent),
                "path": str(cn_file),
                "line": 1,
                "column": 1,
                "focus": True,
            },
        )
        outcomes.append(
            result(
                "C chinese path",
                bool(r_c.get("ok")),
                f"method={r_c.get('method')} path={r_c.get('path')}",
            )
        )
    finally:
        try:
            cn_file.unlink()
            cn_dir.rmdir()
        except OSError:
            pass

    print("\n--- D: binding HWND unchanged ---")
    st1 = http_json(cfg, "GET", "/status").get("status") or {}
    bound1 = ((st1.get("cursor") or {}).get("hwnd"))
    outcomes.append(
        result(
            "D binding stable",
            bound_hwnd is not None and bound_hwnd == bound1 and st1.get("attached") is True,
            f"before={bound_hwnd} after={bound1}",
        )
    )

    print("\n--- E: Live Follow still running (if enabled) ---")
    live1 = (st1.get("live_follow") or {}).get("running")
    if cfg.get("live_follow", True):
        outcomes.append(
            result(
                "E live follow",
                bool(live1) or bool((st1.get("live_follow") or {}).get("hook_installed")),
                f"before={live0} after={live1}",
            )
        )
    else:
        outcomes.append(result("E live follow", None, "live_follow disabled in config"))

    print("\n--- F: Detach restores ---")
    r_f = http_json(cfg, "POST", "/rpc", {"cmd": "detach"})
    time.sleep(0.4)
    st_f = http_json(cfg, "GET", "/status").get("status") or {}
    outcomes.append(
        result(
            "F detach after bridge",
            bool(r_f.get("ok")) and st_f.get("attached") is False,
            f"attached={st_f.get('attached')}",
        )
    )

    print("\n--- Multi-Cursor routing ---")
    outcomes.append(result("multi-Cursor routing", None, "single Cursor assumed; not auto-tested"))

    return outcomes


def main() -> int:
    dpi = enable_dpi_awareness()
    print(f"[acceptance-bridge] dpi={dpi} version={sidecar.VERSION}")
    cfg = load_cfg()

    if sidecar.refresh_attachment_truth().get("attached"):
        print("[acceptance-bridge] REFUSED: Sidecar attached — detach first")
        return 2

    obs, cur = wait_windows(cfg)
    if not obs or not cur:
        print("[acceptance-bridge] need Obsidian + Cursor open")
        return 2

    # Use a temp note under a temp "vault" directory next to sidecar (self-contained)
    vault = ROOT / "_acceptance_vault"
    vault.mkdir(exist_ok=True)
    note = vault / "bridge_note.md"
    note.write_text("# bridge\nsecond line here\n", encoding="utf-8")

    state_backup = backup_state_file()
    obs_snap = snapshot_window(obs.hwnd)
    cur_snap = snapshot_window(cur.hwnd)
    started = False
    outcomes: list[bool | None] = []

    try:
        started = ensure_daemon(cfg)
        print(f"[acceptance-bridge] daemon ready started_by_us={started}")
        outcomes = run_scenarios(cfg, note)
    finally:
        print("\n[acceptance-bridge] cleanup…")
        try:
            if daemon_healthy(cfg):
                st = http_json(cfg, "GET", "/status").get("status") or {}
                if st.get("attached"):
                    http_json(cfg, "POST", "/rpc", {"cmd": "detach"})
        except Exception as exc:
            print(f"[acceptance-bridge] detach: {exc}")
            try:
                sidecar.cmd_detach(cfg)
            except Exception:
                pass
        restore_exact_snapshot(obs_snap, "Obsidian")
        restore_exact_snapshot(cur_snap, "Cursor")
        restore_state_file(state_backup)
        if started:
            try:
                sidecar.cmd_stop(cfg)
            except Exception:
                pass
        try:
            note.unlink(missing_ok=True)
        except OSError:
            pass

    passed = sum(1 for x in outcomes if x is True)
    failed = sum(1 for x in outcomes if x is False)
    skipped = sum(1 for x in outcomes if x is None)
    print(f"\n[acceptance-bridge] {passed} PASS, {failed} FAIL, {skipped} SKIP")
    return 0 if failed == 0 and passed > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
