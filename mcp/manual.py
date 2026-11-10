"""The one-turn-at-a-time path behind `gitchen tick`.

A manual tick is not the daemon: it starts nothing that outlives the command,
it refuses to run while a daemon owns the queue, and — the part that bit a
real pm turn — it has to provide the MCP control socket every dispatched
turn proxies its tool calls to. Without it, the turn runs with a dead MCP
connection ("daemon is not reachable") and can only fall back to bash.

Kept out of `bin/gitchen` so it is testable in-process: a fake harness can
make a tool call through `control.call` during its turn and prove the socket
was listening.
"""

from __future__ import annotations

from . import control, daemon, dispatch, pause
from .config import ProjectConfig


class DaemonRunning(RuntimeError):
    pass


def run_one(cfg: ProjectConfig, force: bool = False, dry_run: bool = False) -> str:
    """Runs at most one dispatch tick and waits for the turn it starts.
    Returns a line describing what happened."""
    if dry_run:
        return dispatch.next_action(cfg, force=force)

    status = daemon.daemon_status(cfg.project_root)
    if status.get("running"):
        raise DaemonRunning(
            f"daemon is running (pid {status.get('pid')}); stop it first — "
            "tick is the manual, one-turn-at-a-time path")

    # The turn's MCP server proxies every tool call to this socket (see
    # control.py). The daemon normally owns the listener; a manual tick has
    # to provide it itself, or the turn runs with a dead MCP connection.
    control.serve_in_background(cfg)
    if force:
        pause.clear(cfg.project_root)

    threads = dispatch.tick(cfg, force=force, limit=1)
    if not threads:
        return f"nothing started: {dispatch.next_action(cfg)}"
    for thread in threads:
        thread.join()
    return f"started {len(threads)} turn(s); turn finished"
