# cursor-obsidian

**v0.5.0** — Zero-config Windows packaging (bundled `cursor-sidecar.exe`).

Repo: https://github.com/hudengfenglai/cursor-obsidian

## Install (Windows x64 — end users)

1. Download `cursor-sidecar-v0.5.0-windows-x64.zip`
2. Unzip into your Obsidian vault plugin folder  
   (e.g. `<vault>/<configDir>/plugins/cursor-sidecar/`)
3. Enable **Cursor Sidecar** in Obsidian Community Plugins / Installed plugins
4. Click **Attach** (ribbon or command)

No Python, pip, venv, or pywin32 install required.

> **Note:** The helper binary is currently **unsigned**. Windows SmartScreen may warn about an unknown publisher. This is expected for v0.5; do not bypass security software — acknowledge the publisher warning if you trust the release source.

## Developer setup (source mode)

See `cursor-sidecar/README.md`. Source mode (`python main.py …`) remains fully supported for development, pytest, and acceptance scripts.

## Scope

Live Sidecar + Context Bridge. No Cursor Agent CLI / ACP / API. Cursor remains the real Desktop Editor.
