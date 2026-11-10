"""Scheduled program runs: a schedule that names a program role (`run_role:`)
runs that program with no issue at all. It holds a slot under its own name, so
its calls are authorized like a role's, and it leaves nothing behind unless it
calls the tools to change something. Its output goes to the daemon log."""

from __future__ import annotations

import os
import shutil
import sys
import threading
import uuid
from datetime import datetime
from pathlib import Path

from . import holders, schedules
from .config import ProjectConfig
from .harness import get_harness


def _settings(cfg: ProjectConfig, role: str, task_id: str, name: str) -> dict:
    settings = cfg.settings_for_role(role)
    if role in cfg.allowed_tools:
        settings["mcp"] = {"role": role, "project_root": str(cfg.project_root), "task_id": task_id,
                           "tools": cfg.allowed_tools[role]}
    settings["env"] = {**os.environ, "GITCHEN_TASK_ID": task_id, "GITCHEN_PROJECT_ROOT": str(cfg.project_root)}
    program = cfg.program_of(role)
    settings["program"] = str(program)
    if settings.get("sandbox"):
        settings["sandbox"] = {**settings["sandbox"],
                               "read": [*(settings["sandbox"].get("read") or []), str(program)]}
    return settings


def _run_job(cfg: ProjectConfig, role: str, name: str, body: str, task_id: str) -> None:
    key = f"job-{name}"
    try:
        scratch = cfg.project_root / ".agents-daemon" / "scratch" / task_id
        scratch.mkdir(parents=True, exist_ok=True)
        settings = _settings(cfg, role, task_id, name)
        turn = get_harness("program").start(body, str(scratch), settings)
        print(f"[job {name}] ok={turn.ok}: {turn.text}", file=sys.stderr, flush=True)
    except Exception as exc:  # noqa: BLE001 - a job never takes the daemon down
        print(f"[job {name}] failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
    finally:
        holders.release(cfg.project_root, key, task_id)
        shutil.rmtree(cfg.project_root / ".agents-daemon" / "scratch" / task_id, ignore_errors=True)


def start_scheduled(cfg: ProjectConfig, now: datetime | None = None) -> list[threading.Thread]:
    """Starts every program-role schedule that matches this minute, once per minute."""
    now = now or datetime.now()
    minute = now.strftime("%Y-%m-%d %H:%M")
    state = schedules._load_state(cfg.project_root)
    threads: list[threading.Thread] = []
    changed = False
    for schedule in schedules.load_schedules(cfg.project_root):
        if not schedule.run_role or not schedules.cron_matches(schedule.cron, now):
            continue
        key = f"job:{schedule.name}"
        if state.get(key) == minute:
            continue
        state[key] = minute
        changed = True
        task_id = str(uuid.uuid4())
        if cfg.program_of(schedule.run_role) is None:
            print(f"[job {schedule.name}] {schedule.run_role} is not a program role", file=sys.stderr, flush=True)
            continue
        if not holders.claim(cfg.project_root, f"job-{schedule.name}", task_id, os.getpid(), schedule.run_role):
            continue
        thread = threading.Thread(target=_run_job,
                                  args=(cfg, schedule.run_role, schedule.name, schedule.body, task_id), daemon=True)
        thread.start()
        threads.append(thread)
    if changed:
        schedules._save_state(cfg.project_root, state)
    return threads
