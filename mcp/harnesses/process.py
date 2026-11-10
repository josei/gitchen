"""One harness turn as a subprocess, and nothing it started outliving it.

Shared by every adapter: a turn's completion is its CLI process exiting, and
a dev server or Playwright webServer the agent left running must not keep
its port bound into the next turn ("port in use", or worse, a test suite
running against stale code nobody thinks to look for).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

# Process groups of the turns this process is running. A turn's processes are
# in their own sessions, so they don't die with the daemon that started them:
# on SIGTERM the daemon must end them itself, or they run on with nothing left
# to report to (reproduced: a daemon stop left a turn's process running).
_LIVE_GROUPS: set[int] = set()
_LIVE_LOCK = threading.Lock()


def reap_all() -> None:
    """Ends every turn this process still has running, then forgets them."""
    with _LIVE_LOCK:
        groups = list(_LIVE_GROUPS)
        _LIVE_GROUPS.clear()
    for pgid in groups:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    for pgid in groups:
        deadline = time.monotonic() + 5
        while _group_alive(pgid) and time.monotonic() < deadline:
            time.sleep(0.2)
        if _group_alive(pgid):
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


def install_term_handler() -> None:
    """On SIGTERM, end the turns still running, then exit."""
    def handle(signum, frame):  # noqa: ARG001
        reap_all()
        sys.exit(0)
    signal.signal(signal.SIGTERM, handle)


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Something in the group can't be signalled by us. It still exists,
        # so it counts as alive; the worktree sweep below is the backstop.
        return True


def is_linked_worktree(cwd: str) -> bool:
    """Only a linked worktree may be swept by working directory. The main
    checkout of a repo is someone's real working copy, and a process there
    may be the user's own, so it is never a sweep target."""
    out = subprocess.run(["git", "-C", cwd, "rev-parse", "--git-dir", "--git-common-dir"],
                         capture_output=True, text=True)
    if out.returncode != 0:
        return False
    git_dir, common_dir = out.stdout.split()
    return Path(cwd, git_dir).resolve() != Path(cwd, common_dir).resolve()


def pids_with_cwd_under(root: str) -> list[int]:
    """Processes whose working directory is inside `root`. A process that
    detaches (setsid, a double-forked server) loses its process group and
    its parent, but keeps its working directory, so this still finds it.
    macOS does not expose other processes' environments to `ps`, so the
    working directory is the handle that survives detaching."""
    root = os.path.realpath(root)
    out = subprocess.run(["lsof", "-w", "-a", "-d", "cwd", "-Fpn"], capture_output=True, text=True).stdout
    pids, pid = [], None
    for line in out.splitlines():
        if line.startswith("p"):
            pid = int(line[1:])
        elif line.startswith("n") and pid is not None:
            path = line[1:]
            if pid != os.getpid() and (path == root or path.startswith(root.rstrip("/") + "/")):
                pids.append(pid)
    return pids


def _signal_all(pids: list[int], sig: int) -> None:
    for pid in pids:
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass


def reap_turn(pgid: int, cwd: str) -> None:
    """Nothing an agent starts (a dev server, a Playwright webServer) may
    outlive its turn: an orphan keeps its port bound, so the next turn's
    `npm run test:e2e` fails with "port in use" or runs against stale code,
    and nobody thinks to look for it. Asks politely first so servers can
    shut themselves down cleanly, then force-kills stragglers."""
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    deadline = time.monotonic() + 5
    while _group_alive(pgid) and time.monotonic() < deadline:
        time.sleep(0.2)
    if _group_alive(pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    if not is_linked_worktree(cwd):
        return
    stragglers = pids_with_cwd_under(cwd)
    _signal_all(stragglers, signal.SIGTERM)
    if stragglers:
        time.sleep(2)
        _signal_all(pids_with_cwd_under(cwd), signal.SIGKILL)


def run(args: list[str], cwd: str, env: dict | None = None,
        stdin_text: str | None = None) -> tuple[int, str, str]:
    """Runs `args` in `cwd` to completion. Returns (returncode, stdout,
    stderr). `env`, when given, is the whole environment the process gets.
    `stdin_text` is what it reads from stdin; with none, stdin is empty."""
    # Temp files, not pipes: a server the agent left running would keep an
    # inherited pipe open and block the read forever, hanging the whole pool
    # slot. The turn's own exit is what we wait for; output is read after.
    with tempfile.TemporaryFile("w+") as out_f, tempfile.TemporaryFile("w+") as err_f:
        proc = subprocess.Popen(args, cwd=cwd, stdout=out_f, stderr=err_f, text=True,
                                start_new_session=True, env=env, stdin=subprocess.PIPE)
        try:
            proc.stdin.write(stdin_text or "")
            proc.stdin.close()
        except BrokenPipeError:
            pass  # the process exited without reading its input
        with _LIVE_LOCK:
            _LIVE_GROUPS.add(proc.pid)
        try:
            proc.wait()
        finally:
            reap_turn(proc.pid, cwd)
            with _LIVE_LOCK:
                _LIVE_GROUPS.discard(proc.pid)
        out_f.seek(0)
        err_f.seek(0)
        return proc.returncode, out_f.read().strip(), err_f.read()
