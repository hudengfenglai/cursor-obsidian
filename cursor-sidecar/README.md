# Cursor Sidecar (MVP)

Windows helper that docks the **real Cursor Desktop** window to the right of Obsidian.

This does **not** embed, inject, or modify Cursor / Obsidian. Both remain separate processes; we only call Windows window APIs (`SetWindowPos`).

```text
Obsidian.exe  |  Cursor.exe
   70%        |     30%
```

## Why

Keep Cursor Desktop features intact:

- login / account switching (Editor mode)
- Agent, Diff, Chat
- Rules / Extensions

Obsidian stays your knowledge OS; Cursor stays your commercial IDE client.

## Requirements

- Windows 10/11
- Python 3.10+
- Obsidian and Cursor Desktop installed

```powershell
cd cursor-sidecar
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### Windows permissions

Usually **no admin** is required.

- Uses public Win32 APIs: `EnumWindows`, `GetWindowRect`, `SetWindowPos`, `ShowWindow`
- Does **not** inject DLLs, hook input, or attach as a debugger
- If antivirus blocks `python.exe` launching `Cursor.exe`, allowlist the script folder
- If Cursor was started elevated and the sidecar is not (or vice versa), window moves may fail — run both at the same integrity level

## Config (`config.json`)

| Key | Meaning | Default |
|-----|---------|---------|
| `obsidian_ratio` | Left share | `0.7` |
| `cursor_ratio` | Right share | `0.3` |
| `gap` | Pixel gap between panes | `0` |
| `monitor` | Monitor index, or `null`/`"auto"` to follow Obsidian | `0` |
| `poll_ms` | Follow-loop interval | `500` |
| `follow_obsidian` | `start` keeps re-arranging when Obsidian moves | `true` |
| `launch_cursor_if_missing` | `start` launches Cursor | `true` |
| `cursor_exe_candidates` | Paths searched when launching | see file |

Edit ratios after you validate that a ~30% wide Cursor still works for Agent / Diff / account switch.

## Commands

```powershell
# Discover windows
python main.py status

# One-shot arrange (Obsidian + Cursor must already be open)
python main.py arrange

# Launch Cursor if needed, arrange, then follow Obsidian moves
python main.py start

# Stop follow loop
python main.py stop

# Show / hide Cursor
python main.py toggle
```

Optional config path:

```powershell
python main.py -c .\config.json arrange
```

## Suggested first test (before relying on sidecar)

1. Open Cursor Editor (not maximized).
2. Manually drag it to ~30% width on the right.
3. Confirm: Agent, Diff, chat, account switch all still work.
4. If yes → run `python main.py arrange`.

## File layout

```text
cursor-sidecar/
├── main.py              # CLI: start / arrange / stop / toggle / status
├── window.py            # Find windows + SetWindowPos layout
├── config.json          # Ratios, gap, monitor, process names
├── requirements.txt     # pywin32, psutil
└── README.md
```

## Phase 2 — Obsidian plugin

Plugin source: [`../obsidian-plugin`](../obsidian-plugin)

Installed into vault as:

```text
<vault>/.obsidian/plugins/cursor-sidecar/
```

Ribbon icon runs:

```text
python main.py --workspace <vault> click
```

Extra CLI commands:

```powershell
python main.py dock                 # launch + arrange once (no follow)
python main.py click                # dock if down, toggle if up
python main.py start --no-follow
python main.py --workspace "D:\vault" dock
python main.py status --json
```

## Phase 3 — Context (optional)

- “Open current note in Cursor”
- Path sync / cursor position (harder; still external)

## Explicitly out of scope

- Modifying Cursor / Electron
- Injection, hooks, overlays into Cursor
- Cursor CLI / ACP / API as the editor surface
- Fake “embedded” iframe of Cursor

## Troubleshooting

| Symptom | Check |
|---------|--------|
| `Obsidian window not found` | Open a vault; run `status`; adjust `obsidian_title_hint` |
| `Cursor window not found` | Open Editor window (not only tray); adjust `cursor_title_hint` |
| Wrong monitor | Set `"monitor": null` or the correct index from multi-monitor setup |
| Layout fights maximize | Sidecar restores maximized windows before resizing — avoid leaving Obsidian maximized if you want stable split |
| Follow loop won’t stop | `python main.py stop` or end the `python main.py start` terminal with Ctrl+C |
