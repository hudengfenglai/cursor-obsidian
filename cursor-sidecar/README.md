# Cursor Sidecar v0.4.1 — Live Sidecar + Context Bridge

Dock the **real Cursor Desktop** beside Obsidian on Windows, keep Cursor
stuck to Obsidian’s right edge while attached, and open the current note
in that same bound Editor.

## Interaction

```text
Attach  → initial split by preset (default Normal = 70/30)
Then    → Cursor follows Obsidian live via WinEventHook
Open    → current Obsidian note opens in bound Cursor Desktop Editor
Detach  → restore exact bound windows to Attach-time placements
```

## Context Bridge

While Sidecar is **attached**:

```text
Obsidian current note (+ line/column)
        ↓
Open in real Cursor Desktop Editor
```

- Uses the **bound** Cursor PID → real `Cursor.exe` (not a hardcoded path)
- Prefers Desktop Editor flags: `--classic --reuse-window --goto`
- Falls back to `cursor://file/...` deep link
- **Does not use Cursor Agent CLI / ACP / `agent`**

Must Attach first (`sidecar_not_attached` otherwise).

Multi-Cursor routing relies on focusing the bound editor before
`--reuse-window` invocation because Cursor does not expose a public
HWND-targeting argument. If a new Cursor window appears, Sidecar binding
is unchanged and a `routing_warning` is returned.

## Width presets

| Preset  | Obsidian | Cursor |
|---------|----------|--------|
| Compact | 78%      | 22%    |
| Normal  | 70%      | 30%    |
| Wide    | 58%      | 42%    |

## Live Sidecar (daemon)

Authenticated localhost daemon (`X-Cursor-Sidecar-Token`).

Obsidian **Live Sidecar** toggle maps to `set-live-follow` (stops Hook without Detach).

## Commands

```powershell
python main.py attach
python main.py detach
python main.py open-editor-file --vault-root <vault> --path <file> --line 10 --column 1
python main.py open-editor-vault --path <vault>
python main.py status --json
```

## Tests

```powershell
pytest
python -m py_compile main.py window.py geometry.py win_events.py editor_bridge.py acceptance_run.py acceptance_live_follow.py acceptance_context_bridge.py
node --check ../obsidian-plugin/main.js
```

## Acceptance testing

```powershell
python acceptance_run.py
python acceptance_live_follow.py
python acceptance_context_bridge.py
```

## Remaining limitations

- When no usable space remains to the right of Obsidian, follow placement is constrained by the monitor work area.
- Context Bridge cannot 100% force a specific HWND; it focuses the bound window then uses `--reuse-window`.
- Detached “Open in Cursor” is intentionally unsupported in v0.4.x.
- Advanced daemon host/port changes are migrated automatically on next `ensureDaemon()` / `daemon-start` (v0.4.1).

## Out of scope

Agent prompt / selection→Agent / bidirectional cursor sync / PyInstaller / SetParent / embedding
