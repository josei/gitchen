"""A pause in dispatch after a turn that never ran: a usage limit, an outage, a
CLI that won't start. It says nothing about any issue, so no issue changes, and
every issue would fail the same way, so nothing starts until the wait is over.

The wait doubles with each failure, up to half an hour, and a turn that
completes clears it. It never gives up: a provider coming back is the normal
ending. A bad config pauses forever too, which is correct, since every turn
would fail; the reason is in the daemon log and in this file.

State is runtime, under `.agents-daemon/pause.json`: never committed, and
losing it only means one more attempt.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

_BASE_SECONDS = 60
_MAX_SECONDS = 1800


def _path(root: Path) -> Path:
    return Path(root) / ".agents-daemon" / "pause.json"


def _load(root: Path) -> dict:
    try:
        return json.loads(_path(root).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def record_failure(root: Path, reason: str) -> dict:
    failures = int(_load(root).get("failures", 0)) + 1
    wait = min(_MAX_SECONDS, _BASE_SECONDS * 2 ** min(failures - 1, 16))
    state = {"failures": failures, "until": time.time() + wait, "reason": reason}
    path = _path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, path)
    return state


def active(root: Path) -> dict | None:
    """The pause, while it is in force. After that the failure count stays on
    disk, so that the next failure waits longer."""
    state = _load(root)
    return state if state and time.time() < state.get("until", 0) else None


def clear(root: Path) -> None:
    _path(root).unlink(missing_ok=True)
