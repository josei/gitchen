"""Draining the daemon: stop taking new turns, let the running ones finish, then
exit on its own. A completed turn must not undo the request, unlike a pause."""

import os
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mcp import daemon, dispatch, drain, harness as harness_module, holders, issues, pause  # noqa: E402
from mcp.config import ProjectConfig  # noqa: E402
from mcp.harness import TurnResult  # noqa: E402


def make_project() -> ProjectConfig:
    base = Path(tempfile.mkdtemp(prefix="dr", dir="/tmp"))
    root = base / "i"
    (root / "agents").mkdir(parents=True)
    for role in ("builder", "pm"):
        (root / "agents" / f"{role}.md").write_text(f"---\nname: {role}\ndescription: {role}\n---\n{role}")
    public = base / "public"
    public.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=public, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
                    "--allow-empty", "-m", "init"], cwd=public, check=True)
    (root / "project.yml").write_text(yaml.safe_dump({"public_repo": "../public"}))
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return ProjectConfig.load(root)


def test_a_drain_request_starts_no_new_turn(tmp_path):
    cfg = make_project()
    started = []

    class H:
        def start(self, prompt, cwd, settings):
            started.append(cwd)
            return TurnResult("s", "Done.\nNEXT: return", {}, True)

        def resume(self, *a, **k):
            raise AssertionError

        def interrupt(self, *a, **k):
            pass

    harness_module._REGISTRY["fake"] = lambda: H()
    issues.create_issue(cfg, "open work", "b", None, 0, "pm", "pm", assignee="builder")
    drain.request(cfg.project_root)
    assert dispatch.tick(cfg) == []
    assert started == []
    assert "draining" in dispatch.next_action(cfg)


def test_a_completed_turn_does_not_undo_a_drain(tmp_path):
    cfg = make_project()

    class H:
        def start(self, prompt, cwd, settings):
            return TurnResult("s", "Done.\nNEXT: return", {}, True)

        def resume(self, *a, **k):
            raise AssertionError

        def interrupt(self, *a, **k):
            pass

    harness_module._REGISTRY["fake"] = lambda: H()
    issues.create_issue(cfg, "one", "b", None, 0, "pm", "pm", assignee="builder")
    issues.create_issue(cfg, "two", "b", None, 0, "pm", "pm", assignee="builder")
    for t in dispatch.tick(cfg):
        t.join(10)
    drain.request(cfg.project_root)
    assert pause.active(cfg.project_root) is None
    assert drain.requested(cfg.project_root), "the request outlives the turn that finished"
    assert dispatch.tick(cfg) == []


def test_the_daemon_leaves_once_nothing_is_running(tmp_path):
    cfg = make_project()
    assert daemon.drain_step(cfg) is False, "not draining: keep going"
    drain.request(cfg.project_root)
    assert daemon.drain_step(cfg) is True, "draining and idle: time to exit"
    assert holders.claim(cfg.project_root, 9, "task-busy", os.getpid(), "builder")
    try:
        assert daemon.drain_step(cfg) is False, "a turn is still running: wait for it"
    finally:
        holders.release(cfg.project_root, 9, "task-busy")


def test_starting_the_daemon_clears_an_old_drain(tmp_path):
    cfg = make_project()
    drain.request(cfg.project_root)
    drain.clear(cfg.project_root)
    assert not drain.requested(cfg.project_root)


def test_stop_with_drain_waits_for_the_daemon_to_leave_by_itself(tmp_path):
    """A stand-in daemon that leaves once it sees the drain request: the stop
    must wait for it and not kill it."""
    cfg = make_project()
    child = tmp_path / "child.py"
    child.write_text(textwrap.dedent(f"""\
        import sys, time, os
        sys.path.insert(0, {str(Path(__file__).resolve().parent.parent)!r})
        from mcp import drain
        root = {str(cfg.project_root)!r}
        while not drain.requested(root):
            time.sleep(0.05)
        time.sleep(0.2)
        sys.exit(0)
    """))
    proc = subprocess.Popen([sys.executable, str(child)])
    # A real daemon is reaped by init once it leaves. Here the test is its parent,
    # so reap it in the background, or it would stay a zombie that still answers signals.
    import threading
    threading.Thread(target=proc.wait, daemon=True).start()
    pid_path = cfg.project_root / ".agents-daemon" / "daemon.pid"
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(proc.pid))
    result = daemon.stop_daemon(cfg.project_root, drain_first=True, timeout=20)
    assert result == "drained" and proc.poll() == 0, "it left on its own, with exit 0"
    assert not pid_path.exists()
