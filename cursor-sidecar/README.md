# Cursor Sidecar v0.7.0-dev — Embedded Agents Pane

Sidecar Mode + Context Follow (Editor Window) preserved.

**New:** embed Cursor **New Agents Window** into an Obsidian ItemView (visual
`SetWindowPos` only — no SetParent). Full Editor stays for file context.

## End-user install

1. Download `cursor-sidecar-v0.5.0-windows-x64.zip`
2. Unzip to `<vault>/<configDir>/plugins/cursor-sidecar/`
3. Enable the plugin → **Attach**

Bundled helper: `bin/cursor-sidecar.exe`

Writable runtime files live in `%LOCALAPPDATA%\CursorSidecar\`
(`config.json`, daemon token/meta/pid, state).

## Interaction

```text
Attach  → initial split by preset (default Normal = 70/30)
Then    → Cursor follows Obsidian live via WinEventHook
Follow  → (optional) Cursor file follows Obsidian active note — silent
Open    → explicit command opens note and focuses Cursor
Detach  → restore exact bound windows to Attach-time placements
```

## Context Follow

Settings → **Context Follow** (default OFF).

When Sidecar is **attached** and Context Follow is on, Obsidian `file-open`
events debounce (~150ms) and call `sync-editor-file` (`focus=false`).
Manual **Open current note in Cursor** still uses `open-editor-file` with focus.

## Developer / source mode

```powershell
cd cursor-sidecar
python -m venv .venv
.\.venv\Scripts\pip install -r requirements-dev.txt
python main.py attach
pytest -q
```

Optional isolated data dir:

```powershell
python main.py --data-dir D:\tmp\sidecar-data status --json
```

## Build packaged helper (Windows x64)

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build.ps1
```

Outputs:

- `dist/cursor-sidecar.exe`
- `release/cursor-sidecar/` (plugin layout + `bin/`)
- `release/cursor-sidecar-v0.5.0-windows-x64.zip`

## Packaged acceptance

```powershell
python acceptance_packaged.py
python acceptance_packaged.py --skip-ui
```

## Tests / static checks

```powershell
pytest -q
python -m py_compile main.py runtime_paths.py context_follow.py window.py geometry.py win_events.py editor_bridge.py acceptance_run.py acceptance_live_follow.py acceptance_context_bridge.py acceptance_context_follow.py acceptance_packaged.py
node --check ../obsidian-plugin/main.js
python acceptance_context_follow.py
```

## Remaining limitations

- Windows x64 only (no macOS / Linux / ARM64)
- Helper binary is **unsigned** (SmartScreen / unknown publisher possible)
- No code signing, auto-update, or Community Plugin store listing yet
- Context Follow cannot guarantee Cursor never activates; focus is restored to Obsidian when stolen
- When no usable space remains to the right of Obsidian, follow placement is constrained by the monitor work area
- Multi-Cursor routing relies on focusing the bound editor before `--reuse-window` (explicit open only)
