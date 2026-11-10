"""The instance's spending limit: how much its turns may cost in one window.

The limit is per instance, not per account, so a person's own sessions keep
their headroom. It is counted from the cost each turn recorded on its issue
(see cost.py), so nothing is kept elsewhere to drift out of step. The check
happens before a turn starts, never while one runs, and it doesn't anticipate
what a turn will cost: when spending reaches the limit, nothing more starts,
and an issue in the middle of its work simply waits where it is.

A window is the last `per` (say 5h). Without one, spending counts from the
last reset, and stays stopped until someone resets it. A reset is the human
gate: `gitchen budget reset` says spending starts counting again from now.

State is runtime, under `.agents-daemon/budget.json`, like the pause.
"""

from __future__ import annotations

import calendar
import json
import os
import time
from pathlib import Path

from . import issues
from .config import ProjectConfig


def _path(root: Path) -> Path:
    return Path(root) / ".agents-daemon" / "budget.json"


def _reset_at(root: Path) -> float:
    try:
        return float(json.loads(_path(root).read_text()).get("reset_at", 0.0))
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return 0.0


def _epoch(at: str) -> float:
    return float(calendar.timegm(time.strptime(at, "%Y-%m-%dT%H:%M:%SZ")))


def status(cfg: ProjectConfig, now: float | None = None) -> dict | None:
    """None without a budget. Otherwise the limit, what the current window has
    spent, and whether that has reached the limit."""
    if not cfg.budget:
        return None
    now = time.time() if now is None else now
    reset_at = _reset_at(cfg.project_root)
    per = cfg.budget["per_seconds"]
    start = reset_at if per is None else max(reset_at, now - per)
    spent = sum(t["usd"] for t in issues.all_turns(cfg) if _epoch(t["at"]) >= start)
    return {"limit": cfg.budget["usd"], "per": cfg.budget["per"], "spent": round(spent, 6),
            "over": spent >= cfg.budget["usd"], "window_start": start,
            "reset_at": reset_at or None}


def reset(root: Path, now: float | None = None) -> float:
    """Spending counts from now on. Earlier turns stay on their issues."""
    now = time.time() if now is None else now
    path = _path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"reset_at": now}))
    os.replace(tmp, path)
    return now


def describe(state: dict | None) -> str:
    if state is None:
        return "no budget set (project.yml `budget:`)"
    window = f"per {state['per']}" if state["per"] else "until reset"
    verdict = "stopped: spending has reached the limit" if state["over"] else "running"
    return f"${state['spent']:.2f} of ${state['limit']:.2f} spent ({window}); {verdict}"
