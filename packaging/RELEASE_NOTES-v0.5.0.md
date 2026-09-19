# Cursor Sidecar v0.5.0

First zero-config Windows release.

## Highlights

- Real Cursor Desktop Editor beside Obsidian
- Attach / Detach with exact window restore
- Live Follow via WinEventHook
- Compact / Normal / Wide layouts
- Open current Obsidian note in Cursor
- Line / column handoff
- Chinese path support
- Packaged Windows x64 helper
- No Python installation required

## Installation

1. Download:
   `cursor-sidecar-v0.5.0-windows-x64.zip`

2. Extract the plugin folder into the Obsidian plugins directory.

3. Enable Cursor Sidecar.

4. Open Cursor Desktop Editor.

5. Run:
   Attach Cursor Sidecar

## Checksums (SHA256)

```
bc70ed4442080cd1f6510e9cc5befecf73ba69b6325391a9ddbdb511df46218f  cursor-sidecar.exe
cb7d73565746c7c80314380c05c355306aa92b37a9166d7069d3420ecbcff348  cursor-sidecar-v0.5.0-windows-x64.zip
```

Helper size: 10,034,250 bytes  
ZIP size: 9,822,690 bytes

## Limitations

- Windows x64 only
- unsigned helper
- SmartScreen may warn
- multi-Cursor routing cannot target a HWND with 100% guarantee
- no macOS / ARM64
- no auto-update

This is a **sidecar / desktop integration**, not a native embedded Cursor.
