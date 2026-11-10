"""The runtime (never committed) mutex over "which live instance may act on
this issue right now". Separate from `issues.py`'s `assignee`/
`assignment_history` on purpose: assignee is durable, meaningful fact
(who's responsible) that belongs in git; a holder only means anything while
its process is alive, same bucket as the existing MCP-connection singleton
pidfile under `.agents-daemon/` — gitignored, local, disposable.

A stale holder (its pid no longer running, because the agent crashed or was
killed without releasing) is indistinguishable from "never spawned" once
cleared — that's the whole crash-recovery story: no special case, the next
dispatch tick just spawns a fresh attempt for the same still-assigned role.
"""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path


def _dir(project_root: Path) -> Path:
    d = Path(project_root) / ".agents-daemon" / "holders"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(project_root: Path, issue_id: int) -> Path:
    return _dir(project_root) / f"{issue_id}.json"


def _lock_path(project_root: Path, issue_id: int) -> Path:
    return _dir(project_root) / f".{issue_id}.lock"


def _is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def current(project_root: Path, issue_id: int) -> dict | None:
    """Returns the live holder record, or None — clearing it first if its
    pid is no longer running (stale, treated as released)."""
    path = _path(project_root, issue_id)
    if not path.exists():
        return None
    record = json.loads(path.read_text())
    if not _is_running(record["pid"]):
        path.unlink(missing_ok=True)
        return None
    return record


def any_live(project_root: Path) -> bool:
    """Is any issue held by a live turn right now? The queue runs one issue
    at a time, so this is also "is a turn in flight"."""
    for path in _dir(project_root).glob("*.json"):
        try:
            issue_id = int(path.stem)
        except ValueError:
            continue
        if current(project_root, issue_id) is not None:
            return True
    return False


def claim(project_root: Path, issue_id: int, task_id: str, pid: int, role: str) -> bool:
    """Atomically claim the mutex for this issue, or fail if something else
    already holds it live. True means the caller may now act; false means
    try a different issue this tick."""
    lock_path = _lock_path(project_root, issue_id)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            if current(project_root, issue_id) is not None:
                return False
            # Written whole, then renamed into place: a reader on another thread
            # (another slot's tick counting live turns) never sees a half record.
            target = _path(project_root, issue_id)
            scratch = target.with_suffix(f".{task_id}.tmp")
            scratch.write_text(json.dumps({"task_id": task_id, "pid": pid, "role": role}))
            os.replace(scratch, target)
            return True
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def release(project_root: Path, issue_id: int, task_id: str) -> None:
    """Only clears the record if it still matches this task_id — a holder
    that was already cleared (e.g. detected stale and reclaimed by someone
    else) must not be stomped by a late release from the original."""
    path = _path(project_root, issue_id)
    if not path.exists():
        return
    record = json.loads(path.read_text())
    if record.get("task_id") == task_id:
        path.unlink(missing_ok=True)


def is_held(project_root: Path, issue_id: int) -> bool:
    return current(project_root, issue_id) is not None


def find_by_task(project_root: Path, task_id: str) -> dict | None:
    """The live holder record for this task, with its issue id added. The
    control socket uses it to authorize a call: the task is only as good as
    the live claim behind it."""
    for path in _dir(project_root).glob("*.json"):
        record = json.loads(path.read_text())
        if record.get("task_id") == task_id and _is_running(record["pid"]):
            # A job (a scheduled program run) holds under its own name, not an issue id.
            key = int(path.stem) if path.stem.isdigit() else path.stem
            return {**record, "issue_id": key if isinstance(key, int) else None, "key": key}
    return None
