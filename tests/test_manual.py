import time
"""The manual tick path: one turn at a time, with a working MCP socket."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mcp import control, harness as harness_module, issues, manual, pause  # noqa: E402
from mcp.config import ProjectConfig  # noqa: E402
from mcp.harness import TurnResult  # noqa: E402


def make_project() -> ProjectConfig:
    # A short root: AF_UNIX paths are limited to ~104 bytes and pytest's
    # tmp_path is far longer than that.
    base = Path(tempfile.mkdtemp(prefix="bc", dir="/tmp"))
    root = base / "i"
    (root / "agents").mkdir(parents=True)
    for _role in ("builder", "reviewer", "pm", "researcher"):
        _f = root / "agents" / f"{_role}.md"
        if not _f.exists():
            _f.write_text(f"---\nname: {_role}\n---\n{_role}")
    (root / "agents" / "builder.md").write_text("---\nname: builder\n---\nbuild")
    (root / "agents" / "pm.md").write_text("---\nname: pm\n---\npm")
    public = base / "public"
    public.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=public, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
                    "--allow-empty", "-m", "init"], cwd=public, check=True)
    (root / "project.yml").write_text(
        f"public_repo: ../public\n"
        "allowed_tools:\n  builder: [get_issue]\n  pm: [get_issue]\n")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return ProjectConfig.load(root)


def test_a_manual_tick_provides_the_control_socket_its_turn_calls_through():
    """A turn's MCP server proxies every tool call to the control socket.
    The daemon normally owns it; a manual tick must start it, or the turn
    runs with a dead MCP connection (found on a real pm turn)."""
    cfg = make_project()
    created = issues.create_issue(cfg, "build it", "body", None, 0, "pm", "pm",
                                  assignee="builder")
    seen = {}

    class H:
        def start(self, prompt, cwd, settings):
            mcp = settings["mcp"]
            seen["issue"] = control.call(mcp["project_root"], mcp["task_id"],
                                         "get_issue", {"id": created["id"]})
            return TurnResult("s1", "Done.\nNEXT: return", {}, True)

        def resume(self, *a, **k):
            raise AssertionError

        def interrupt(self, *a, **k):
            pass

    harness_module._REGISTRY["fake"] = H

    result = manual.run_one(cfg)

    assert "started 1 turn" in result
    assert seen["issue"]["id"] == created["id"], \
        "the turn's tool call reached the daemon through the control socket"
    assert issues.get_issue(cfg, created["id"])["assignee"] == "pm"


def test_a_manual_tick_refuses_while_the_daemon_runs():
    cfg = make_project()
    (cfg.project_root / ".agents-daemon").mkdir(exist_ok=True)
    (cfg.project_root / ".agents-daemon" / "daemon.pid").write_text(str(os.getpid()))

    with pytest.raises(manual.DaemonRunning, match="daemon is running"):
        manual.run_one(cfg)


def test_a_dry_run_starts_nothing():
    cfg = make_project()
    created = issues.create_issue(cfg, "build it", "body", None, 0, "pm", "pm",
                                  assignee="builder")

    line = manual.run_one(cfg, dry_run=True)

    assert line.startswith(f"start #{created['id']} → builder")
    assert not (cfg.project_root / ".agents-daemon" / "control.sock").exists()
    assert issues.get_issue(cfg, created["id"])["assignee"] == "builder"


def test_force_clears_a_pause():
    cfg = make_project()
    pause.record_failure(cfg.project_root, "a usage limit")
    assert "paused" in manual.run_one(cfg)
    assert pause.active(cfg.project_root), "a plain tick respects the pause"
    manual.run_one(cfg, force=True)
    assert pause.active(cfg.project_root) is None


def test_force_goes_past_a_spent_budget():
    cfg = make_project()
    created = issues.create_issue(cfg, "build it", "body", None, 0, "pm", "pm",
                                  assignee="builder")
    (cfg.project_root / "project.yml").write_text(
        "public_repo: ../public\nbudget: {usd: 1}\nallowed_tools:\n  builder: [get_issue]\n")
    cfg = ProjectConfig.load(cfg.project_root)
    issues.add_comment(cfg, created["id"], "spent", "builder", "builder",
                       turns=[{"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "role": "builder",
                               "model": "m", "usd": 5.0, "input": 0, "output": 0, "cache_read": 0,
                               "cache_write": 0, "seconds": 0}])

    assert "budget reached" in manual.run_one(cfg, dry_run=True)
    assert "start" in manual.run_one(cfg, dry_run=True, force=True)

    class H:
        def start(self, prompt, cwd, settings):
            return TurnResult("s1", "Done.\nNEXT: return", {}, True)

        def resume(self, *a, **k):
            raise AssertionError

        def interrupt(self, *a, **k):
            pass

    harness_module._REGISTRY["fake"] = H
    result = manual.run_one(cfg, force=True)

    assert "started 1 turn" in result
    assert issues.get_issue(cfg, created["id"])["assignee"] == "pm"


