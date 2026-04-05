"""Real-time log capture and streaming via WebSocket.

Intercepts stdout to capture print() output, stores it in a circular buffer,
and broadcasts to connected WebSocket subscribers.
"""

from __future__ import annotations

import asyncio
import re
import sys
from collections import deque
from datetime import datetime, timezone
from typing import Any, TextIO


# Known source prefixes extracted from print() output
_SOURCE_PATTERN = re.compile(
    r"^\[("
    r"WORKER|CONVERT|WATCHER|PLEX|METADATA|SCANNER|CLEANUP|QUEUE|API"
    r")\]\s*",
    re.IGNORECASE,
)

# Map level keywords found in messages
_LEVEL_KEYWORDS = {
    "error": "error",
    "fail": "error",
    "exception": "error",
    "traceback": "error",
    "warn": "warn",
    "warning": "warn",
}


def _detect_level(message: str) -> str:
    lower = message.lower()
    for keyword, level in _LEVEL_KEYWORDS.items():
        if keyword in lower:
            return level
    return "info"


class LogEntry(dict):
    """A single log entry stored as a dict for easy JSON serialization."""

    __slots__ = ()

    def __init__(self, timestamp: str, level: str, source: str, message: str):
        super().__init__(
            timestamp=timestamp,
            level=level,
            source=source,
            message=message,
        )


class LogBuffer:
    """Thread-safe circular buffer that stores log entries and broadcasts to subscribers."""

    def __init__(self, maxlen: int = 2000):
        self._buffer: deque[LogEntry] = deque(maxlen=maxlen)
        self._subscribers: list[asyncio.Queue] = []
        self._loop: asyncio.AbstractEventLoop | None = None

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def add_subscriber(self, queue: asyncio.Queue) -> None:
        self._subscribers.append(queue)

    def remove_subscriber(self, queue: asyncio.Queue) -> None:
        try:
            self._subscribers.remove(queue)
        except ValueError:
            pass

    def append(self, entry: LogEntry) -> None:
        """Append an entry to the buffer and broadcast to subscribers."""
        self._buffer.append(entry)
        if self._loop and self._subscribers:
            for q in self._subscribers:
                try:
                    self._loop.call_soon_threadsafe(q.put_nowait, entry)
                except (RuntimeError, asyncio.QueueFull):
                    pass

    def get_recent(
        self,
        limit: int = 200,
        source: str = "",
        search: str = "",
    ) -> list[dict[str, Any]]:
        """Return recent log entries, optionally filtered by source and search text."""
        entries = list(self._buffer)

        if source:
            source_upper = source.upper()
            entries = [e for e in entries if e["source"] == source_upper]

        if search:
            search_lower = search.lower()
            entries = [e for e in entries if search_lower in e["message"].lower()]

        if limit > 0:
            entries = entries[-limit:]

        return entries


class _LogInterceptor:
    """A stdout wrapper that captures lines for the log buffer."""

    def __init__(self, original: TextIO, buffer: LogBuffer):
        self._original = original
        self._buffer = buffer
        self._partial = ""

    def write(self, text: str) -> int:
        result = self._original.write(text)

        self._partial += text
        while "\n" in self._partial:
            line, self._partial = self._partial.split("\n", 1)
            line = line.strip()
            if not line:
                continue
            self._ingest(line)

        return result

    def _ingest(self, line: str) -> None:
        match = _SOURCE_PATTERN.match(line)
        if match:
            source = match.group(1).upper()
            message = line[match.end():]
        else:
            source = "SYSTEM"
            message = line

        level = _detect_level(message)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

        entry = LogEntry(
            timestamp=ts,
            level=level,
            source=source,
            message=message,
        )
        self._buffer.append(entry)

    def flush(self) -> None:
        self._original.flush()

    def fileno(self) -> int:
        return self._original.fileno()

    def isatty(self) -> bool:
        return self._original.isatty()

    # Forward any other attribute access to the original stream
    def __getattr__(self, name: str) -> Any:
        return getattr(self._original, name)


# Module-level singleton
log_buffer = LogBuffer()


def init_logstream(loop: asyncio.AbstractEventLoop | None = None) -> None:
    """Install the stdout interceptor and configure the event loop for broadcasting."""
    if loop is None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

    if loop:
        log_buffer.set_loop(loop)

    # Only install once
    if not isinstance(sys.stdout, _LogInterceptor):
        sys.stdout = _LogInterceptor(sys.stdout, log_buffer)  # type: ignore[assignment]
