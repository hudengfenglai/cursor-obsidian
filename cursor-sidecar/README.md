# Cursor Sidecar v0.2 — Lifecycle & Window Restore

Dock the **real Cursor Desktop** beside Obsidian on Windows.

This is **not** an embed. Cursor stays `Cursor.exe`. Obsidian only triggers a Win32 helper.

## Interaction (core of v0.2)

```text
First click  → Attach
               save original WindowPlacement for both windows
               Obsidian 70% | Cursor 30%

Second click → Detach
               restore both windows to pre-Attach state
```

**Acceptance:** If Obsidian was maximized before Attach, Detach must maximize it again.

Ribbon = **Attach / Detach**, not show/hide.

Show/hide remains a separate command for debugging.

## Why Attach ≠ Cursor running

If Cursor is already open but not attached, the first click must **Attach**, never hide Cursor.

```text
Cursor process running  ≠  Sidecar attached
```

## Commands

```powershell
cd cursor-sidecar
.\.venv\Scripts\Activate.ps1

python main.py attach          # save originals + arrange
python main.py detach          # restore WindowPlacement
python main.py click           # attach ↔ detach
python main.py arrange         # re-split bound windows
python main.py focus           # focus bound Cursor HWND
python main.py toggle          # show/hide (advanced)
python main.py show
python main.py hide
python main.py status --json
```

Aliases: `dock`→`attach`, `undock`/`restore`→`detach`.

## State (`.sidecar.state.json`)

On Attach (only when previously detached):

- `attached: true`
- Obsidian / Cursor: `hwnd`, `pid`, `original_rect`, `original_window_placement`, `original_monitor`, visibility
- Bound HWNDs for subsequent ops
- Current `left_rect` / `right_rect`

On Detach: restore via `SetWindowPlacement`, then `attached: false`.

Stale HWND/PID → `status` reports `attached=false`, `state_valid=false`.

## DPI

Startup calls `enable_dpi_awareness()`:

1. `SetProcessDpiAwarenessContext(PER_MONITOR_AWARE_V2)`
2. else `SetProcessDpiAwareness(2)`
3. else `SetProcessDPIAware()`

Work area comes from `MonitorFromWindow(obsidian) → GetMonitorInfo`, not monitor-index round-trips (unless config forces an index).

## Multi-Cursor

After Attach, operations use the **bound** Cursor HWND+PID. Rediscovery only if that window dies.

## Install

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pytest
python -m py_compile main.py window.py geometry.py
```

Obsidian plugin: copy `obsidian-plugin/*` → `<vault>/.obsidian/plugins/cursor-sidecar/`, set **Sidecar directory** in plugin settings.

## Out of scope for v0.2

Daemon follow / WinEventHook, note sync, PyInstaller, SetParent, Cursor CLI — those are v0.3+.

## Roadmap

```text
v0.2  Attach/Detach + restore + DPI + HWND bind   ← you are here
v0.3  Daemon + live follow + multi-monitor polish
v0.4  Open current note / focus / context
v1.0  sidecar.exe, zero Python for end users
```
