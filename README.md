# cursor-obsidian

Dock **real Cursor Desktop** beside Obsidian on Windows — keep Cursor login, Agent, Diff, Rules, Extensions.

## Layout

```text
cursor-obsidian/
├── cursor-sidecar/     # Phase 1: Python window manager (pywin32)
└── obsidian-plugin/    # Phase 2: Obsidian ribbon + commands
```

## Quick start

1. Sidecar venv (once):

```powershell
cd cursor-sidecar
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python main.py status
```

2. Plugin is installed to vault `胡梦浩obsidian` as `cursor-sidecar`. Reload Obsidian (or toggle the plugin) and click the ribbon circle.

See `cursor-sidecar/README.md` and `obsidian-plugin/README.md`.
