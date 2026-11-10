"""Periodic issue creation — the only thing cron is for. A schedule never
fires an agent directly: on each cron match it creates an issue with a
configured assignee, and the normal dispatch mechanism (see dispatch.py)
picks it up exactly like any other issue — the same end-of-turn contract,
the same worktree, the same circuit breaker. No separate harness-invocation
path, no agent-prompt cron-firing. Time is just another way an issue is
born into the system; everything downstream of that is identical to any
other issue's lifecycle.

Reads an instance repo's schedules/*.md — one file per job, frontmatter
(`cron`, `assignee`, optionally `title`/`labels`/`priority`, and `folder` when
 the project sets no `default_folder`) + the
body as the issue's own body. A schedule does not file a new issue while the
one it filed last is still `open` or `blocked`: a round that is still running
(or waiting on its own work) is not stacked with another. Standard 5-field cron, matched at most once per
matching minute (state tracked the same way the old agent-firing scheduler
used to, just for issue creation instead).
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import frontmatter, issues
from .config import ProjectConfig

_SYSTEM_IDENTITY = ("daemon", "daemon")


@dataclass
class Schedule:
    name: str
    cron: str
    assignee: str | None
    title: str
    body: str
    labels: list[str]
    priority: int
    run_role: str | None = None  # a program role to run instead of filing an issue
    folder: str | None = None    # required when folders are declared and there is no default


def load_schedules(project_root: Path) -> list[Schedule]:
    schedules_dir = Path(project_root) / "schedules"
    if not schedules_dir.exists():
        return []
    out = []
    for path in sorted(schedules_dir.glob("*.md")):
        front, body = frontmatter.parse(path.read_text())
        if front.get("run_role"):
            # A program run: no issue is created, so no assignee is needed.
            out.append(Schedule(
                name=path.stem, cron=front["cron"], assignee=None, run_role=str(front["run_role"]),
                title=front.get("title", path.stem), body=body.strip(), labels=[], priority=0,
            ))
            continue
        out.append(Schedule(
            name=path.stem, cron=front["cron"], assignee=front["assignee"],
            title=front.get("title", path.stem), body=body.strip(),
            labels=front.get("labels", []), priority=int(front.get("priority", 0)),
            folder=front.get("folder"),
        ))
    return out


def _match_field(field: str, value: int, min_v: int, max_v: int) -> bool:
    if field == "*":
        return True
    for part in field.split(","):
        if "/" in part:
            base, step_s = part.split("/")
            step = int(step_s)
            start, end = (min_v, max_v) if base == "*" else (
                tuple(map(int, base.split("-"))) if "-" in base else (int(base), max_v)
            )
            if start <= value <= end and (value - start) % step == 0:
                return True
        elif "-" in part:
            start, end = map(int, part.split("-"))
            if start <= value <= end:
                return True
        elif int(part) == value:
            return True
    return False


def cron_matches(expr: str, dt: datetime) -> bool:
    """Standard 5-field cron: minute hour day-of-month month day-of-week.
    day-of-week: 0=Sunday..6=Saturday (7 also accepted as Sunday)."""
    minute, hour, dom, month, dow = expr.split()
    dow = re.sub(r"(?<![0-9])7(?![0-9])", "0", dow)
    cron_weekday = (dt.weekday() + 1) % 7  # python Monday=0 -> cron Sunday=0
    return (
        _match_field(minute, dt.minute, 0, 59)
        and _match_field(hour, dt.hour, 0, 23)
        and _match_field(dom, dt.day, 1, 31)
        and _match_field(month, dt.month, 1, 12)
        and _match_field(dow, cron_weekday, 0, 6)
    )


def _state_path(project_root: Path) -> Path:
    d = Path(project_root) / ".agents-daemon"
    d.mkdir(exist_ok=True)
    return d / "schedule_state.json"


def read_state(project_root: Path) -> dict:
    """The last-fired minute per schedule name, without creating anything —
    the web view reads this too, and a read-only process must not make dirs."""
    path = Path(project_root) / ".agents-daemon" / "schedule_state.json"
    return json.loads(path.read_text()) if path.exists() else {}


def _load_state(project_root: Path) -> dict:
    return read_state(project_root)


def _save_state(project_root: Path, state: dict) -> None:
    _state_path(project_root).write_text(json.dumps(state))


def fire(cfg: ProjectConfig, schedule: Schedule) -> dict:
    return issues.create_issue(
        cfg, schedule.title, schedule.body, schedule.labels, schedule.priority,
        *_SYSTEM_IDENTITY, assignee=schedule.assignee, folder=schedule.folder,
    )


UNFINISHED = ("open", "blocked")


def _still_running(cfg: ProjectConfig, state: dict, schedule: Schedule) -> bool:
    """Whether the issue this schedule filed last is still open or blocked."""
    last = state.get(f"issue:{schedule.name}")
    if last is None:
        return False
    try:
        return issues.get_issue(cfg, int(last))["state"] in UNFINISHED
    except (FileNotFoundError, ValueError, KeyError):
        return False  # gone: nothing is running


def tick(cfg: ProjectConfig, now: datetime | None = None) -> list[dict]:
    """Fires at most once per matching minute per schedule, same as a real
    cron would — the daemon ticks every ~20s, so without this a single
    matching minute would otherwise create the same issue two or three
    times over."""
    now = now or datetime.now()
    minute_key = now.strftime("%Y-%m-%d %H:%M")
    state = _load_state(cfg.project_root)
    fired = []
    changed = False
    for schedule in load_schedules(cfg.project_root):
        if schedule.run_role:
            continue  # program runs are started by jobs.start_scheduled, not by issue creation
        if cron_matches(schedule.cron, now) and state.get(schedule.name) != minute_key:
            if _still_running(cfg, state, schedule):
                continue  # the last round is unfinished; the next match tries again
            try:
                created = fire(cfg, schedule)
                fired.append(created)
                state[f"issue:{schedule.name}"] = created["id"]
            except ValueError as exc:
                # e.g. the schedule names an assignee that is not a role. That is
                # a config mistake to report, never a reason to stop the daemon's
                # loop; the minute is still marked so it is not retried every tick.
                print(f"[schedules] {schedule.name}: not fired: {exc}", file=sys.stderr, flush=True)
            state[schedule.name] = minute_key
            changed = True
    if changed:
        _save_state(cfg.project_root, state)
    return fired
