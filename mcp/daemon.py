"""The daemon: a background process that loops two things and nothing
else. No named jobs fire an agent directly, no time-scheduled code path
bypasses the issue system — every bit of this project's automation is
driven by issue state (an assignee with no live holder), not a clock:

- `schedules.tick()`: the *only* thing cron does is create an issue, on a
  schedule, with a configured assignee — a plain mechanical write, no
  agent spawned here. That issue then flows through the exact same
  pipeline as any other.
- `dispatch.tick()`: the circuit breaker (stopping runaway handoff loops),
  then spawning against anything whose assignee has no live holder.
- `origin_sync.tick()`: the daily fetch/push of the public repo to origin
  (see origin_sync.py) — mechanical, unsandboxed, and never forced.

This module's only remaining job beyond that loop is process lifecycle:
start it once per boot (nothing here does that automatically), stop it,
check if it's running. `gitchen serve` never touches any of this — the
MCP server answering tool calls and the daemon driving dispatch are two
separate processes with two separate lifecycles, on purpose.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from . import control, dispatch, drain, holders, jobs, origin_sync, schedules
from .harnesses import process
from .config import ProjectConfig


def run_forever(project_root: Path, poll_seconds: int = 20) -> None:
    cfg = ProjectConfig.load(project_root)
    process.install_term_handler()  # a stop ends the turns it started, not just the loop
    control.serve_in_background(cfg)
    print(f"[{datetime.now().isoformat()}] daemon started, polling every {poll_seconds}s", flush=True)
    while True:
        if drain_step(cfg):
            print(f"[{datetime.now().isoformat()}] drained: no turn is running, leaving", flush=True)
            return
        schedules.tick(cfg)   # mechanical: creates issues, never spawns an agent
        if not drain.requested(cfg.project_root):
            jobs.start_scheduled(cfg)  # scheduled program runs: a script, no issue, no model
        origin_sync.tick(cfg)  # mechanical: fetch/push the public repo, outside every sandbox
        dispatch.tick(cfg)   # non-blocking: spawns its own threads and returns
        time.sleep(poll_seconds)


def drain_step(cfg) -> bool:
    """True when a drain has been requested and nothing is running: the loop leaves."""
    return drain.requested(cfg.project_root) and not holders.any_live(cfg.project_root)


# --- process management for `gitchen daemon start|stop|status` -------------

def _pid_path(project_root: Path) -> Path:
    return project_root / ".agents-daemon" / "daemon.pid"


def _log_path(project_root: Path) -> Path:
    return project_root / ".agents-daemon" / "daemon.log"


def start_daemon(project_root: Path, poll_seconds: int = 20) -> int:
    project_root = Path(project_root).resolve()
    (project_root / ".agents-daemon").mkdir(exist_ok=True)
    drain.clear(project_root)  # a fresh start is not a continuation of an old drain
    pid_path = _pid_path(project_root)
    if pid_path.exists() and _is_running(int(pid_path.read_text())):
        raise RuntimeError(f"daemon already running (pid {pid_path.read_text().strip()})")

    log_file = open(_log_path(project_root), "a")
    proc = subprocess.Popen(
        [sys.executable, "-m", "mcp.daemon_main", "--project-root", str(project_root),
         "--poll-seconds", str(poll_seconds)],
        cwd=str(Path(__file__).resolve().parent.parent),
        stdout=log_file, stderr=log_file, start_new_session=True,
    )
    pid_path.write_text(str(proc.pid))
    return proc.pid


def _is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def stop_daemon(project_root: Path, drain_first: bool = False, timeout: float = 1800) -> str:
    """Stops the daemon. `drain_first` asks it to finish its running turns and leave
    on its own, waiting up to `timeout` seconds; past that, or without a drain, it is
    terminated, and terminating ends the turns it started. Returns what happened:
    "stopped", "drained", "terminated", or "not running"."""
    root = Path(project_root).resolve()
    pid_path = _pid_path(root)
    if not pid_path.exists():
        return "not running"
    pid = int(pid_path.read_text())
    if not _is_running(pid):
        pid_path.unlink(missing_ok=True)
        return "not running"
    if drain_first:
        drain.request(root)
        deadline = time.monotonic() + timeout
        while _is_running(pid) and time.monotonic() < deadline:
            time.sleep(0.5)
        if not _is_running(pid):
            pid_path.unlink(missing_ok=True)
            return "drained"
    os.kill(pid, signal.SIGTERM)
    pid_path.unlink(missing_ok=True)
    return "terminated"


def daemon_status(project_root: Path) -> dict:
    pid_path = _pid_path(Path(project_root).resolve())
    if not pid_path.exists():
        return {"running": False}
    pid = int(pid_path.read_text())
    return {"running": _is_running(pid), "pid": pid}
