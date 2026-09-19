# Cursor Sidecar v0.2.1 — Lifecycle + Safety Hardening

Dock the **real Cursor Desktop** beside Obsidian on Windows.

## Interaction

```text
First click  → Attach (save originals + 70/30)
Second click → Detach (restore exact bound windows only)
```

If the bound Cursor HWND is gone, Detach returns `gone` and **does not** resize another Cursor window.

## Safety (v0.2.1)

- Binding: `hwnd + pid + process_name + process_create_time`
- Legacy states without create_time still work; `status` marks `legacy_binding`
- Atomic state write (`.tmp` → `os.replace`)
- Daemon is **experimental**, default off; requires `X-Cursor-Sidecar-Token` (no CORS `*`)

## Commands

```powershell
python main.py attach
python main.py detach
python main.py click
python main.py status --json
```

## Tests

```powershell
pytest
python -m py_compile main.py window.py geometry.py acceptance_run.py
node --check ../obsidian-plugin/main.js
```

## Acceptance testing

`cursor-sidecar/acceptance_run.py` is a **Windows-only** acceptance harness (not a CI unit test).

It refuses to run if Sidecar is currently **attached** (will not overwrite live restore state).  
On exit it restores the exact Obsidian/Cursor placements captured at start, and restores `.sidecar.state.json`.

It checks:

- maximize → attach → detach restore
- existing Cursor → attach (not hide)
- bound HWND stability across arrange/focus
- stale binding detection (`cursor_gone`, etc.)
- do not restore another Cursor window when the bound one is gone
- DPI / monitor work area via `MonitorFromWindow`

```powershell
python acceptance_run.py
```

Requires Obsidian and Cursor Desktop windows to be open, with Sidecar detached.

## Out of scope

Live follow / WinEventHook / note sync / PyInstaller → v0.3+
