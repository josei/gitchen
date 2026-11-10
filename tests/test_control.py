import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mcp import control, holders, issues  # noqa: E402
from mcp.config import ProjectConfig  # noqa: E402


@pytest.fixture
def project():
    root = Path(tempfile.mkdtemp(prefix="bc", dir="/tmp")) / "instance"
    root.mkdir()
    (root / "agents").mkdir()
    (root / "agents" / "pm.md").write_text("---\nname: pm\n---\npm")
    (root / "project.yml").write_text(
        "public_repo: ../public\n"
        "allowed_tools:\n  pm: [create_issue, get_issue]\n")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
                    "--allow-empty", "-m", "init"], cwd=root, check=True)
    cfg = ProjectConfig.load(root)
    control.serve_in_background(cfg)
    return cfg


def _live_turn(cfg, task_id, role):
    assert holders.claim(cfg.project_root, 1, task_id, os.getpid(), role)


def test_a_call_from_a_live_turn_runs_the_tool_and_returns_its_result(project):
    _live_turn(project, "task-1", "pm")
    created = control.call(project.project_root, "task-1", "create_issue",
                           {"title": "t", "body": "b"})
    assert issues.get_issue(project, created["id"])["title"] == "t"


def test_a_call_with_no_live_turn_is_refused(project):
    with pytest.raises(control.ControlError, match="no live turn"):
        control.call(project.project_root, "nobody", "get_issue", {"id": 1})


def test_a_tool_outside_the_roles_profile_is_refused(project):
    _live_turn(project, "task-2", "pm")
    with pytest.raises(control.ControlError, match="not permitted for pm"):
        control.call(project.project_root, "task-2", "comment_issue", {"id": 1, "text": "x"})


def test_a_request_still_runs_when_the_caller_disconnects(project):
    _live_turn(project, "task-3", "pm")
    request = json.dumps({"task_id": "task-3", "tool": "create_issue",
                          "args": {"title": "orphaned", "body": "b"}}) + "\n"
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.connect(str(control.socket_path(project.project_root)))
    conn.sendall(request.encode())
    conn.close()  # gone before any reply
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if any(i["title"] == "orphaned" for i in issues.list_issues(project)):
            return
        time.sleep(0.1)
    raise AssertionError("the request did not run after the caller disconnected")
