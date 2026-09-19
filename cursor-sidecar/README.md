# Cursor Sidecar v0.3 — Live Sidecar

Dock the **real Cursor Desktop** beside Obsidian on Windows, then keep Cursor
stuck to Obsidian’s right edge while attached.

## Interaction

```text
Attach  → initial split by preset (default Normal = 70/30)
Then    → Cursor follows Obsidian live via WinEventHook
Detach  → restore exact bound windows to Attach-time placements
```

Live Follow only moves **Cursor**. It never snaps Obsidian back to 70/30 while you drag.

## Width presets

| Preset  | Obsidian | Cursor |
|---------|----------|--------|
| Compact | 78%      | 22%    |
| Normal  | 70%      | 30%    |
| Wide    | 58%      | 42%    |

Presets apply on Attach, Arrange, and when you switch preset while attached.

## Live Sidecar (daemon)

v0.3 Live Follow uses the existing authenticated localhost daemon
(`X-Cursor-Sidecar-Token`). No second background service.

Obsidian setting **Live Sidecar** (replaces “Use daemon (experimental)”):

- ON → ensure daemon; Cursor follows while attached
- OFF → one-shot Attach / Detach via CLI still works

`poll_ms` / `follow_obsidian` in `config.json` are **deprecated**. Live Follow does not use polling.

## Commands

```powershell
python main.py attach
python main.py detach
python main.py click
python main.py arrange
python main.py compact|normal|wide
python main.py status --json
python main.py daemon
```

## Safety (unchanged from v0.2.1)

- Binding: `hwnd + pid + process_name + process_create_time`
- No rediscovery of another Cursor on Detach / binding loss
- Atomic state write
- Daemon token auth (no CORS `*`)

## Tests

```powershell
pytest
python -m py_compile main.py window.py geometry.py win_events.py acceptance_run.py acceptance_live_follow.py
node --check ../obsidian-plugin/main.js
```

## Acceptance testing

Windows-only harnesses (not CI). Refuse to run if Sidecar is attached. Self-restoring (`try`/`finally`, exact HWND).

```powershell
python acceptance_run.py          # v0.2.1 lifecycle (6 scenarios)
python acceptance_live_follow.py  # v0.3 live follow (A–F)
```

## Out of scope (v0.4+)

Note sync / Open current file / PyInstaller / SetParent / embedding / Cursor CLI / ACP
