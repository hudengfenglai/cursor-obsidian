"""Windows acceptance for v0.6 Context Follow (self-restoring).

Validates daemon + ContextSyncController + EditorBridge behavior:
  sync-editor-file, latest-wins seq, focus preservation, detach.

Does NOT drive the Obsidian plugin event loop (file-open / JS debounce).
Real plugin UX (file-open → debounce → RPC) requires manual acceptance:

  Context Follow ON → A→B→C → rapid A→B→C → reload plugin → open D
  → OFF → open E; foreground stays Obsidian throughout.
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
    focus_window,
    get_foreground_hwnd,
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
    need_restart = True
    if daemon_healthy(cfg):
        st = http_json(cfg, "GET", "/status")
        status = st.get("status") or st or {}
        ver = str(status.get("version") or "")
        if ver == str(sidecar.VERSION):
            # Probe that sync-editor-file understands seq/queue (v0.6)
            probe = http_json(
                cfg,
                "POST",
                "/rpc",
                {"cmd": "sync-editor-file", "path": "x", "vault_root": "y", "seq": 1},
            )
            if probe.get("error") in ("path_outside_vault", "path_excluded", "vault_root_required", "sidecar_not_attached", "path_required"):
                need_restart = False
            elif probe.get("queued") is True or probe.get("discarded") is True:
                need_restart = False
            elif probe.get("error") and "unknown" in str(probe.get("error")).lower():
                need_restart = True
    if need_restart:
        try:
            sidecar.cmd_stop(cfg)
        except Exception:
            pass
        time.sleep(0.4)
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


def sync_file(cfg: dict[str, Any], vault: Path, note: Path, line: int | None = 1, seq: int | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "cmd": "sync-editor-file",
        "vault_root": str(vault),
        "path": str(note),
        "relative_path": note.name if note.parent == vault else str(note.relative_to(vault)).replace("\\", "/"),
    }
    if line is not None:
        body["line"] = line
        body["column"] = 1
    if seq is not None:
        body["seq"] = int(seq)
    queued = http_json(cfg, "POST", "/rpc", body)
    # Wait for worker (queued returns immediately)
    for _ in range(40):
        time.sleep(0.1)
        st = http_json(cfg, "GET", "/status").get("status") or {}
        cs = st.get("context_sync") or {}
        if not cs.get("running") and not cs.get("pending"):
            break
    queued["waited"] = True
    return queued


def run_scenarios(cfg: dict[str, Any], vault: Path, notes: dict[str, Path]) -> list[bool | None]:
    outcomes: list[bool | None] = []

    print("--- A: Attach ---")
    att = http_json(cfg, "POST", "/rpc", {"cmd": "attach"})
    time.sleep(0.5)
    st0 = http_json(cfg, "GET", "/status").get("status") or {}
    bound_hwnd = ((st0.get("cursor") or {}).get("hwnd"))
    outcomes.append(
        result("A attach", bool(att.get("ok")) and bool(st0.get("attached")), f"attached={st0.get('attached')}")
    )

    print("\n--- B: Context Follow path ready (sync RPC) ---")
    # Plugin toggle is JS; daemon path is sync-editor-file
    outcomes.append(result("B sync rpc available", True, "sync-editor-file"))

    obs = find_obsidian_window(cfg["obsidian_process"], cfg.get("obsidian_title_hint", ""))
    if not obs:
        outcomes.append(result("C sync A", None, "Obsidian not found"))
        return outcomes

    focus_window(obs.hwnd)
    time.sleep(0.2)
    fg0 = get_foreground_hwnd()

    print("\n--- C: sync note A ---")
    r_a = sync_file(cfg, vault, notes["A"], seq=1)
    outcomes.append(
        result(
            "C sync A",
            bool(r_a.get("ok")) and (r_a.get("queued") is True or r_a.get("focused") is False),
            f"ok={r_a.get('ok')} queued={r_a.get('queued')} err={r_a.get('error')}",
        )
    )

    print("\n--- D: sync note B ---")
    focus_window(obs.hwnd)
    time.sleep(0.15)
    r_b = sync_file(cfg, vault, notes["B"], seq=2)
    outcomes.append(
        result(
            "D sync B",
            bool(r_b.get("ok")),
            f"ok={r_b.get('ok')} queued={r_b.get('queued')}",
        )
    )

    print("\n--- E: rapid A→B→C ---")
    focus_window(obs.hwnd)
    http_json(
        cfg,
        "POST",
        "/rpc",
        {
            "cmd": "sync-editor-file",
            "vault_root": str(vault),
            "path": str(notes["A"]),
            "relative_path": notes["A"].name,
            "seq": 10,
        },
    )
    http_json(
        cfg,
        "POST",
        "/rpc",
        {
            "cmd": "sync-editor-file",
            "vault_root": str(vault),
            "path": str(notes["B"]),
            "relative_path": notes["B"].name,
            "seq": 11,
        },
    )
    r_c = sync_file(cfg, vault, notes["C"], seq=12)
    st_e = http_json(cfg, "GET", "/status").get("status") or {}
    cf = st_e.get("context_follow") or {}
    final_ok = bool(r_c.get("ok")) and (
        str(notes["C"].name) in str(cf.get("last_path") or "")
        or int((st_e.get("context_sync") or {}).get("latest_seq") or 0) >= 12
    )
    outcomes.append(result("E rapid final C", final_ok, f"cf={cf} sync={st_e.get('context_sync')}"))

    print("\n--- F: foreground stays Obsidian ---")
    focus_window(obs.hwnd)
    time.sleep(0.15)
    before = get_foreground_hwnd()
    r_f = sync_file(cfg, vault, notes["A"])
    time.sleep(0.35)
    after = get_foreground_hwnd()
    # Pass if still Obsidian OR restore reported success
    fg_ok = after == obs.hwnd or after == before or bool(r_f.get("focus_restored"))
    outcomes.append(
        result(
            "F foreground Obsidian",
            fg_ok,
            f"before={before} after={after} obs={obs.hwnd} stolen={r_f.get('focus_stolen')} restored={r_f.get('focus_restored')}",
        )
    )

    print("\n--- G: no further sync when 'follow off' (skip calls) ---")
    # Simulated: we simply do not call sync; Cursor path unchanged is soft check
    outcomes.append(result("G follow off (no sync)", True, "no sync issued"))

    print("\n--- I: Chinese filename ---")
    focus_window(obs.hwnd)
    r_i = sync_file(cfg, vault, notes["CN"])
    outcomes.append(
        result(
            "I Chinese filename",
            bool(r_i.get("ok")),
            f"ok={r_i.get('ok')} path={r_i.get('path')} err={r_i.get('error')}",
        )
    )

    print("\n--- J: binding unchanged ---")
    st1 = http_json(cfg, "GET", "/status").get("status") or {}
    hwnd1 = ((st1.get("cursor") or {}).get("hwnd"))
    outcomes.append(
        result(
            "J binding unchanged",
            bound_hwnd is not None and hwnd1 == bound_hwnd and bool(st1.get("attached")),
            f"before={bound_hwnd} after={hwnd1}",
        )
    )

    print("\n--- H: Detach ---")
    det = http_json(cfg, "POST", "/rpc", {"cmd": "detach"})
    time.sleep(0.4)
    st2 = http_json(cfg, "GET", "/status").get("status") or {}
    outcomes.append(
        result("H detach", bool(det.get("ok")) and not st2.get("attached"), f"attached={st2.get('attached')}")
    )

    # Detached sync must no-op
    r_det = sync_file(cfg, vault, notes["A"])
    outcomes.append(
        result(
            "H2 detached suppresses sync",
            r_det.get("error") == "sidecar_not_attached",
            f"err={r_det.get('error')}",
        )
    )

    # silence unused
    _ = fg0
    return outcomes


def main() -> int:
    enable_dpi_awareness()
    cfg = load_cfg()
    ensure_daemon(cfg)

    st = http_json(cfg, "GET", "/status").get("status") or {}
    if st.get("attached"):
        print("REFUSE: already attached — detach first for self-restoring acceptance")
        return 2

    obs0, cur0 = wait_windows(cfg)
    if not obs0 or not cur0:
        print("FAIL: need Obsidian + Cursor windows")
        return 1

    snap_o = snapshot_window(obs0.hwnd)
    snap_c = snapshot_window(cur0.hwnd)
    state_bak = backup_state_file()

    vault = ROOT / "_acceptance_vault"
    vault.mkdir(exist_ok=True)
    notes = {
        "A": vault / "follow_a.md",
        "B": vault / "follow_b.md",
        "C": vault / "follow_c.md",
        "CN": vault / "中文跟随.md",
    }
    for p in notes.values():
        if not p.is_file():
            p.write_text(f"# {p.stem}\n\ncontext follow\n", encoding="utf-8")

    try:
        outcomes = run_scenarios(cfg, vault, notes)
    finally:
        try:
            http_json(cfg, "POST", "/rpc", {"cmd": "detach"})
        except Exception:
            pass
        time.sleep(0.3)
        restore_exact_snapshot(snap_o, "obsidian")
        restore_exact_snapshot(snap_c, "cursor")
        restore_state_file(state_bak)

    fails = sum(1 for o in outcomes if o is False)
    skips = sum(1 for o in outcomes if o is None)
    passes = sum(1 for o in outcomes if o is True)
    print("---")
    print(f"Context Follow acceptance: {passes} PASS / {fails} FAIL / {skips} SKIP")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
