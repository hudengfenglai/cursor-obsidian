"""Context Follow latest-wins sync queue (v0.6).

Single worker + one pending slot. Newer jobs replace older pending jobs.
HTTP handlers return immediately after queueing — they do not wait for Cursor.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional


@dataclass(frozen=True)
class SyncJob:
    seq: int
    vault_root: str
    path: str
    line: int | None = None
    column: int | None = None
    relative_path: str | None = None


ExecuteFn = Callable[[SyncJob], dict[str, Any]]


class ContextSyncController:
    """Single-channel context sync: pending max 1, latest wins."""

    def __init__(self, execute: ExecuteFn | None = None) -> None:
        self._execute = execute
        self._lock = threading.RLock()
        self._pending: SyncJob | None = None
        self._running = False
        self._latest_seq = 0
        self._last_submitted_path: str | None = None
        self._last_result: dict[str, Any] | None = None
        self._worker: threading.Thread | None = None

    def set_execute(self, execute: ExecuteFn) -> None:
        self._execute = execute

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": bool(self._running),
                "pending": self._pending is not None,
                "latest_seq": int(self._latest_seq),
            }

    def clear_pending(self) -> None:
        with self._lock:
            self._pending = None

    def submit(self, job: SyncJob) -> dict[str, Any]:
        """Queue job. Stale seq discarded. Pending older job replaced."""
        with self._lock:
            seq = int(job.seq)
            if seq < self._latest_seq:
                return {
                    "ok": True,
                    "queued": False,
                    "discarded": True,
                    "reason": "stale_seq",
                    "seq": seq,
                    "latest_seq": self._latest_seq,
                    "cmd": "sync-editor-file",
                }
            self._latest_seq = seq
            self._pending = job
            self._last_submitted_path = job.path
            start_worker = not self._running
            if start_worker:
                self._running = True
        if start_worker:
            t = threading.Thread(
                target=self._worker_loop,
                name="context-sync-worker",
                daemon=True,
            )
            self._worker = t
            t.start()
        return {
            "ok": True,
            "queued": True,
            "seq": int(job.seq),
            "latest_seq": seq,
            "cmd": "sync-editor-file",
        }

    def _worker_loop(self) -> None:
        while True:
            with self._lock:
                job = self._pending
                self._pending = None
                if job is None:
                    self._running = False
                    return
                latest = self._latest_seq
            # If superseded before start, skip (newer is/was pending)
            if int(job.seq) < int(latest):
                continue
            with self._lock:
                if self._pending is not None and int(self._pending.seq) > int(job.seq):
                    continue
            result: dict[str, Any]
            try:
                if self._execute is None:
                    result = {"ok": False, "error": "no_executor", "seq": job.seq}
                else:
                    result = dict(self._execute(job) or {})
                    result.setdefault("seq", job.seq)
            except Exception as exc:  # noqa: BLE001
                result = {"ok": False, "error": str(exc), "seq": job.seq}
            with self._lock:
                # Ignore result if a newer job already queued/started tracking
                if int(job.seq) >= int(self._latest_seq) or self._pending is None:
                    self._last_result = result

    def last_result(self) -> dict[str, Any] | None:
        with self._lock:
            return dict(self._last_result) if self._last_result else None


def should_restore_obsidian_focus(
    *,
    foreground_before: int,
    foreground_after: int,
    obsidian_hwnd: int,
    cursor_hwnd: int,
) -> bool:
    """
    Restore only when focus moved Obsidian → bound Cursor.
    If user switched to Chrome/etc., do nothing.
    """
    try:
        before = int(foreground_before or 0)
        after = int(foreground_after or 0)
        obs = int(obsidian_hwnd or 0)
        cur = int(cursor_hwnd or 0)
    except (TypeError, ValueError):
        return False
    if not before or not after or not obs or not cur:
        return False
    if before != obs:
        return False
    if after != cur:
        return False
    return True
