"""Cursor Desktop Editor Context Bridge (v0.4).

Opens vault files in the *bound* Cursor Desktop Editor.
Does NOT use Cursor Agent CLI / ACP / agent.exe.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import quote

import psutil

log = logging.getLogger("cursor_sidecar.editor_bridge")

# Process-local capability cache: "exe|mtime" -> caps
_CAP_CACHE: dict[str, dict[str, Any]] = {}

KNOWN_EDITOR_DEFAULTS: dict[str, Any] = {
    "reuse_window": True,
    "goto": True,
    "classic": True,
    "new_window": True,
    "probed": False,
    "source": "fallback",
}


def to_one_based_cursor(line_0: int | None, ch_0: int | None) -> tuple[int | None, int | None]:
    """Convert Obsidian 0-based editor cursor to 1-based editor location."""
    line = None if line_0 is None else max(int(line_0) + 1, 1)
    col = None if ch_0 is None else max(int(ch_0) + 1, 1)
    if line is None:
        return None, None
    return line, col


def build_editor_location(
    path: str | Path,
    line: int | None = None,
    column: int | None = None,
) -> str:
    """Build Cursor/VS Code style location: path[:line[:column]]."""
    p = str(path)
    if line is None:
        return p
    line_i = max(int(line), 1)
    if column is None:
        return f"{p}:{line_i}"
    col_i = max(int(column), 1)
    return f"{p}:{line_i}:{col_i}"


def resolve_vault_file(vault_root: str | Path, file_path: str | Path) -> Path:
    """Resolve file under vault; reject path traversal / missing files."""
    vault = Path(vault_root).resolve()
    target = Path(file_path).resolve()
    if not vault.is_dir():
        raise ValueError("vault_root_invalid")
    try:
        target.relative_to(vault)
    except ValueError as exc:
        raise ValueError("path_outside_vault") from exc
    if not target.is_file():
        raise FileNotFoundError(f"file_not_found:{target}")
    return target


def resolve_vault_folder(folder_path: str | Path) -> Path:
    target = Path(folder_path).resolve()
    if not target.is_dir():
        raise NotADirectoryError(f"folder_not_found:{target}")
    return target


def build_cursor_file_deeplink(
    path: str | Path,
    line: int | None = None,
    column: int | None = None,
) -> str:
    """
    cursor://file/<abs-path>[:line[:column]]

    Encodes spaces, Chinese, #, ?, % while keeping drive letters and slashes.
    """
    resolved = Path(path).resolve()
    path_uri = resolved.as_posix()  # D:/foo/bar.md
    encoded = quote(path_uri, safe="/:")
    loc = encoded
    if line is not None:
        loc = f"{encoded}:{max(int(line), 1)}"
        if column is not None:
            loc = f"{loc}:{max(int(column), 1)}"
    return f"cursor://file/{loc}"


def _capability_cache_key(cursor_exe: str) -> str:
    p = Path(cursor_exe)
    try:
        resolved = str(p.resolve())
        mtime = int(p.stat().st_mtime_ns)
    except OSError:
        resolved = str(p)
        mtime = 0
    return f"{resolved}|{mtime}"


def probe_editor_capabilities(cursor_exe: str, *, force: bool = False) -> dict[str, Any]:
    """
    Probe Desktop Editor launcher flags via `Cursor.exe --help`.
    Never calls agent / cursor-agent.

    If --help succeeds with non-empty output: set flags strictly from that text.
    If --help fails / empty: use known Desktop Editor fallback defaults.
    """
    key = _capability_cache_key(cursor_exe)
    if not force and key in _CAP_CACHE:
        return dict(_CAP_CACHE[key])

    caps = dict(KNOWN_EDITOR_DEFAULTS)
    try:
        proc = subprocess.run(
            [cursor_exe, "--help"],
            capture_output=True,
            text=True,
            timeout=5,
            shell=False,
            check=False,
        )
        text = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        if text:
            lower = text.lower()
            caps = {
                "reuse_window": "--reuse-window" in lower,
                "goto": "--goto" in lower,
                "classic": "--classic" in lower,
                "new_window": "--new-window" in lower,
                "probed": True,
                "source": "help",
            }
        else:
            log.info("editor --help empty — using fallback defaults")
    except (OSError, subprocess.SubprocessError, TimeoutError) as exc:
        log.info("editor capability probe failed: %s — using fallback defaults", exc)

    _CAP_CACHE[key] = dict(caps)
    return dict(caps)


def clear_capability_cache() -> None:
    _CAP_CACHE.clear()


def get_bound_cursor_executable(
    binding: dict[str, Any] | None,
    *,
    validate_fn: Callable[[dict[str, Any] | None], dict[str, Any]],
) -> dict[str, Any]:
    """Resolve Cursor.exe from exact Sidecar-bound PID. No rediscovery."""
    if not binding:
        return {"ok": False, "error": "cursor_not_bound"}
    report = validate_fn(binding)
    if not report.get("ok"):
        reason = str(report.get("reason") or "cursor_gone")
        if reason in ("hwnd_gone", "pid_mismatch", "create_time_mismatch", "process_name_mismatch"):
            return {"ok": False, "error": "cursor_gone", "reason": reason}
        return {"ok": False, "error": "cursor_gone", "reason": reason}
    try:
        pid = int(binding["pid"])
        hwnd = int(binding["hwnd"])
    except (KeyError, TypeError, ValueError):
        return {"ok": False, "error": "cursor_not_bound"}
    try:
        exe = psutil.Process(pid).exe()
    except (psutil.Error, OSError) as exc:
        return {"ok": False, "error": "cursor_gone", "detail": str(exc)}
    if not exe or not Path(exe).is_file():
        return {"ok": False, "error": "cursor_exe_missing"}
    return {"ok": True, "exe": exe, "pid": pid, "hwnd": hwnd}


def list_cursor_top_level_hwnds(process_name: str = "Cursor.exe") -> list[int]:
    """Enumerate current top-level Cursor HWNDs (for routing warning)."""
    import win32gui
    import win32process

    want = process_name.lower().replace(".exe", "")
    found: list[int] = []

    def _cb(hwnd: int, _: object) -> None:
        if not win32gui.IsWindow(hwnd):
            return
        if not win32gui.GetWindowText(hwnd):
            return
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            name = psutil.Process(int(pid)).name()
        except (psutil.Error, OSError, ValueError):
            return
        if name.lower().replace(".exe", "") == want:
            found.append(int(hwnd))

    win32gui.EnumWindows(_cb, None)
    return found


def detect_new_cursor_windows(before: list[int], after: list[int]) -> list[int]:
    before_set = set(int(x) for x in before)
    return [h for h in after if int(h) not in before_set]


def open_via_deeplink(uri: str) -> None:
    """Open cursor:// URI without browser HTTP. No shell=True."""
    if os.name == "nt":
        try:
            os.startfile(uri)  # noqa: S606 — intentional URI protocol launch
            return
        except OSError:
            import ctypes

            rc = ctypes.windll.shell32.ShellExecuteW(None, "open", uri, None, None, 1)
            if int(rc) <= 32:
                raise OSError(f"ShellExecuteW failed for deeplink rc={rc}")
            return
    # Non-Windows fallback (not primary target)
    subprocess.Popen(["xdg-open", uri], shell=False)  # noqa: S603


def open_via_executable(cursor_exe: str, args: list[str]) -> subprocess.Popen:
    """Launch Desktop Editor with argv array (never shell=True)."""
    cmd = [cursor_exe, *args]
    log.info("editor invoke: %s", cmd)
    return subprocess.Popen(  # noqa: S603
        cmd,
        shell=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )


class EditorBridge:
    """Context Bridge: open files/folders in bound Cursor Desktop Editor."""

    def __init__(
        self,
        *,
        validate_binding: Callable[[dict[str, Any] | None], dict[str, Any]],
        focus_hwnd: Callable[[int], None],
        list_cursor_hwnds: Optional[Callable[[str], list[int]]] = None,
        process_name: str = "Cursor.exe",
        focus_settle_s: float = 0.08,
        routing_wait_s: float = 0.5,
    ) -> None:
        self.validate_binding = validate_binding
        self.focus_hwnd = focus_hwnd
        self.list_cursor_hwnds = list_cursor_hwnds or list_cursor_top_level_hwnds
        self.process_name = process_name
        self.focus_settle_s = focus_settle_s
        self.routing_wait_s = routing_wait_s

    def resolve_cursor_executable(self, binding: dict[str, Any] | None) -> dict[str, Any]:
        return get_bound_cursor_executable(binding, validate_fn=self.validate_binding)

    def build_location(
        self,
        path: str | Path,
        line: int | None = None,
        column: int | None = None,
    ) -> str:
        return build_editor_location(path, line=line, column=column)

    def _invoke_with_fallback(
        self,
        cursor_exe: str,
        abs_path: Path,
        *,
        line: int | None,
        column: int | None,
        is_folder: bool = False,
    ) -> dict[str, Any]:
        caps = probe_editor_capabilities(cursor_exe)
        attempts: list[tuple[str, list[str]]] = []
        last_err: Exception | None = None

        if is_folder:
            folder = str(abs_path)
            if caps.get("classic") and caps.get("reuse_window"):
                attempts.append(("exe_folder_classic", ["--classic", "--reuse-window", folder]))
            if caps.get("reuse_window"):
                attempts.append(("exe_folder", ["--reuse-window", folder]))
            attempts.append(("exe_folder_plain", [folder]))
        else:
            location = self.build_location(abs_path, line=line, column=column)
            file_only = str(abs_path)
            if caps.get("classic") and caps.get("reuse_window") and caps.get("goto") and line is not None:
                attempts.append(
                    (
                        "exe_goto",
                        ["--classic", "--reuse-window", "--goto", location],
                    )
                )
            if caps.get("reuse_window") and caps.get("goto") and line is not None:
                attempts.append(("exe_goto", ["--reuse-window", "--goto", location]))
            # When --goto is unsupported but a line is requested, prefer deeplink
            # for location fidelity before plain file opens.
            if line is not None and not caps.get("goto"):
                uri = build_cursor_file_deeplink(abs_path, line=line, column=column)
                try:
                    open_via_deeplink(uri)
                    return {"ok": True, "method": "deeplink", "uri": uri}
                except OSError as exc:
                    last_err = exc
                    log.info("deeplink (no-goto path) failed: %s", exc)
            if caps.get("classic") and caps.get("reuse_window"):
                attempts.append(("exe_file", ["--classic", "--reuse-window", file_only]))
            if caps.get("reuse_window"):
                attempts.append(("exe_file", ["--reuse-window", file_only]))
            attempts.append(("exe_file", [file_only]))

        for method, args in attempts:
            try:
                open_via_executable(cursor_exe, args)
                return {"ok": True, "method": method, "args": args}
            except OSError as exc:
                last_err = exc
                log.info("editor invoke failed (%s): %s", method, exc)

        # Deep link only for files
        if not is_folder:
            uri = build_cursor_file_deeplink(abs_path, line=line, column=column)
            try:
                open_via_deeplink(uri)
                return {"ok": True, "method": "deeplink", "uri": uri}
            except OSError as exc:
                last_err = exc

        return {
            "ok": False,
            "error": "editor_open_failed",
            "detail": str(last_err) if last_err else "unknown",
        }

    def open_file(
        self,
        *,
        binding: dict[str, Any] | None,
        vault_root: str | Path,
        path: str | Path,
        line: int | None = None,
        column: int | None = None,
        focus: bool = True,
    ) -> dict[str, Any]:
        resolved = get_bound_cursor_executable(binding, validate_fn=self.validate_binding)
        if not resolved.get("ok"):
            return {"ok": False, "error": resolved.get("error"), "reason": resolved.get("reason")}

        try:
            abs_file = resolve_vault_file(vault_root, path)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        except FileNotFoundError as exc:
            return {"ok": False, "error": "file_not_found", "detail": str(exc)}

        line_i = max(int(line), 1) if line is not None else None
        col_i = max(int(column), 1) if column is not None else None

        before = self.list_cursor_hwnds(self.process_name)
        hwnd = int(resolved["hwnd"])
        if focus:
            try:
                self.focus_hwnd(hwnd)
                time.sleep(self.focus_settle_s)
            except Exception as exc:  # noqa: BLE001
                log.info("focus bound cursor failed: %s", exc)

        result = self._invoke_with_fallback(
            str(resolved["exe"]),
            abs_file,
            line=line_i,
            column=col_i,
            is_folder=False,
        )
        time.sleep(self.routing_wait_s)
        after = self.list_cursor_hwnds(self.process_name)
        new_hwnds = detect_new_cursor_windows(before, after)

        out: dict[str, Any] = {
            "ok": bool(result.get("ok")),
            "method": result.get("method"),
            "path": str(abs_file),
            "line": line_i,
            "column": col_i,
            "cursor_exe": resolved["exe"],
            "bound_hwnd": hwnd,
            "bound_pid": resolved["pid"],
        }
        if new_hwnds:
            out["routing_warning"] = "new_cursor_window_created"
            out["new_hwnds"] = new_hwnds
        if not result.get("ok"):
            out["error"] = result.get("error")
            out["detail"] = result.get("detail")
        return out

    def open_folder(
        self,
        *,
        binding: dict[str, Any] | None,
        path: str | Path,
        focus: bool = True,
    ) -> dict[str, Any]:
        resolved = get_bound_cursor_executable(binding, validate_fn=self.validate_binding)
        if not resolved.get("ok"):
            return {"ok": False, "error": resolved.get("error"), "reason": resolved.get("reason")}

        try:
            folder = resolve_vault_folder(path)
        except NotADirectoryError as exc:
            return {"ok": False, "error": "folder_not_found", "detail": str(exc)}

        before = self.list_cursor_hwnds(self.process_name)
        hwnd = int(resolved["hwnd"])
        if focus:
            try:
                self.focus_hwnd(hwnd)
                time.sleep(self.focus_settle_s)
            except Exception as exc:  # noqa: BLE001
                log.info("focus bound cursor failed: %s", exc)

        result = self._invoke_with_fallback(
            str(resolved["exe"]),
            folder,
            line=None,
            column=None,
            is_folder=True,
        )
        time.sleep(self.routing_wait_s)
        after = self.list_cursor_hwnds(self.process_name)
        new_hwnds = detect_new_cursor_windows(before, after)

        out: dict[str, Any] = {
            "ok": bool(result.get("ok")),
            "method": result.get("method"),
            "path": str(folder),
            "cursor_exe": resolved["exe"],
            "bound_hwnd": hwnd,
            "bound_pid": resolved["pid"],
        }
        if new_hwnds:
            out["routing_warning"] = "new_cursor_window_created"
            out["new_hwnds"] = new_hwnds
        if not result.get("ok"):
            out["error"] = result.get("error")
            out["detail"] = result.get("detail")
        return out
