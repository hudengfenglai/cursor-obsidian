# Obsidian plugin — Cursor Sidecar

Ribbon button + commands that call the local Python helper in `../cursor-sidecar`.

## Install into a vault

Copy (or junction) this folder to:

```text
<vault>/.obsidian/plugins/cursor-sidecar/
```

Required files:

- `manifest.json`
- `main.js`
- `styles.css`

Then in Obsidian: **Settings → Community plugins → enable “Cursor Sidecar”** (turn off Safe mode if needed).

## Settings

| Setting | Purpose |
|---------|---------|
| Sidecar directory | Path to `cursor-sidecar` (contains `main.py`) |
| Python path | Prefer `.venv\Scripts\python.exe` |
| Open vault in Cursor | Pass vault path on dock/click |
| Show notices | Toast feedback |

## Ribbon

Click the filled-circle icon:

- Cursor not running → launch Cursor (open vault) + arrange 70/30
- Cursor running → show/hide Cursor window

## Commands (Command Palette)

- Toggle / dock Cursor
- Dock Cursor (launch + arrange)
- Arrange windows
- Show / hide Cursor
- Sidecar status
