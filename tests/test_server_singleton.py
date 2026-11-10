"""A second `gitchen serve` for the same (project, role) must kill
the first and take over, rather than erroring out or running side by side —
but two *different* profiles for the same project (e.g. the coordinator's
own unrestricted connection and a scoped worker connection, alive at the
same time while a dispatch-spawned worker turn is in flight) must NOT kill
each other."""

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "mcp" / "server.py"

sys.path.insert(0, str(ROOT))
from mcp import daemon  # noqa: E402


@pytest.fixture(autouse=True)
def _stop_any_daemons_this_test_started(tmp_path):
    """Some tests below explicitly start a real daemon process (see
    test_serve_does_not_disturb_an_already_running_daemon) — clean it up
    even on failure, so it doesn't outlive the test and the tmp_path it
    was pointed at. `serve()` itself never starts one (see daemon.py) —
    this is only for tests that call daemon.start_daemon directly."""
    yield
    for pid_file in tmp_path.rglob(".agents-daemon/daemon.pid"):
        daemon.stop_daemon(pid_file.parent.parent)


def _is_running(pid: int) -> bool:
    try:
        import os
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def make_project(tmp_path: Path, allowed_tools: str = "") -> Path:
    root = tmp_path / "instance"
    (root / "agents").mkdir(parents=True)
    for _role in ("builder", "reviewer", "pm", "researcher"):
        _f = root / "agents" / f"{_role}.md"
        if not _f.exists():
            _f.write_text(f"---\nname: {_role}\n---\n{_role}")
    (root / "project.yml").write_text(
        "public_repo: ../public\n" + allowed_tools
    )
    (tmp_path / "public").mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root)
    return root


def spawn(project_root: Path, role: str | None = None) -> subprocess.Popen:
    args = [sys.executable, str(SERVER), "--project-root", str(project_root)]
    if role:
        args += ["--role", role]
    return subprocess.Popen(
        args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def rpc(proc: subprocess.Popen, msg: dict) -> dict:
    proc.stdin.write(json.dumps(msg) + "\n")
    proc.stdin.flush()
    line = proc.stdout.readline()
    return json.loads(line)


def test_second_serve_kills_the_first(tmp_path):
    project_root = make_project(tmp_path)
    pid_path = project_root / ".agents-daemon" / "mcp.pid"

    first = spawn(project_root)
    try:
        for _ in range(50):
            if pid_path.exists():
                break
            time.sleep(0.1)
        assert pid_path.exists(), "first server never wrote a pidfile"
        first_pid = int(pid_path.read_text())
        assert first_pid == first.pid
        assert _is_running(first_pid)

        second = spawn(project_root)
        try:
            for _ in range(50):
                if pid_path.exists() and int(pid_path.read_text()) == second.pid:
                    break
                time.sleep(0.1)
            assert int(pid_path.read_text()) == second.pid

            # `first` is our own child, so check + reap it via poll() rather
            # than os.kill(pid, 0) — a zombie (terminated, not yet reaped by
            # its parent) still answers kill(pid, 0) as "exists".
            for _ in range(50):
                if first.poll() is not None:
                    break
                time.sleep(0.1)
            assert first.poll() is not None, "first server should have been killed"
            assert _is_running(second.pid)
        finally:
            second.terminate()
            second.wait(timeout=5)
    finally:
        if first.poll() is None:
            first.terminate()
            first.wait(timeout=5)


def test_role_filters_list_and_rejects_calls(tmp_path):
    project_root = make_project(tmp_path, allowed_tools=(
        "allowed_tools:\n  reviewer: [get_issue, list_issues]\n"
    ))
    proc = spawn(project_root, role="reviewer")
    try:
        rpc(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})

        listed = rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        names = {t["name"] for t in listed["result"]["tools"]}
        assert names == {"get_issue", "list_issues"}

        denied = rpc(proc, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                             "params": {"name": "create_issue", "arguments": {}}})
        assert "error" in denied
        assert "create_issue" in denied["error"]["message"]
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_unrestricted_connection_sees_every_tool(tmp_path):
    project_root = make_project(tmp_path)
    proc = spawn(project_root)  # no --role: the coordinator's own connection
    try:
        rpc(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        listed = rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        names = {t["name"] for t in listed["result"]["tools"]}
        assert "create_issue" in names and "update_issue" in names
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_different_profiles_coexist_but_same_profile_still_singleton(tmp_path):
    project_root = make_project(tmp_path, allowed_tools=(
        "allowed_tools:\n  builder: [get_issue]\n"
    ))
    coordinator = spawn(project_root)  # unrestricted
    builder_1 = spawn(project_root, role="builder")
    try:
        rpc(coordinator, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        rpc(builder_1, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        time.sleep(0.3)
        assert coordinator.poll() is None, "coordinator's own connection must not be affected"
        assert builder_1.poll() is None

        builder_2 = spawn(project_root, role="builder")
        try:
            rpc(builder_2, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
            for _ in range(50):
                if builder_1.poll() is not None:
                    break
                time.sleep(0.1)
            assert builder_1.poll() is not None, "second builder connection should replace the first"
            assert coordinator.poll() is None, "coordinator connection still must not be touched"
        finally:
            builder_2.terminate()
            builder_2.wait(timeout=5)
    finally:
        coordinator.terminate()
        coordinator.wait(timeout=5)
        if builder_1.poll() is None:
            builder_1.terminate()
            builder_1.wait(timeout=5)


def test_serve_never_starts_the_daemon(tmp_path):
    """serve() is pure MCP tool-call handling — starting the daemon is the
    daemon's own explicit lifecycle (`gitchen daemon start`), never a side
    effect of some agent connection happening to exist. Unrestricted or
    scoped, neither should touch it."""
    project_root = make_project(tmp_path, allowed_tools="allowed_tools:\n  builder: [get_issue]\n")
    coordinator = spawn(project_root)
    builder = spawn(project_root, role="builder")
    try:
        rpc(coordinator, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        rpc(builder, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        time.sleep(0.3)
        assert not (project_root / ".agents-daemon" / "daemon.pid").exists()
    finally:
        coordinator.terminate()
        coordinator.wait(timeout=5)
        builder.terminate()
        builder.wait(timeout=5)


def test_serve_does_not_disturb_an_already_running_daemon(tmp_path):
    project_root = make_project(tmp_path)
    daemon.start_daemon(project_root)
    daemon_pid = daemon.daemon_status(project_root)["pid"]

    proc = spawn(project_root)
    try:
        rpc(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        time.sleep(0.3)
        assert daemon.daemon_status(project_root)["pid"] == daemon_pid, \
            "an already-running daemon must be left exactly as it was"
    finally:
        proc.terminate()
        proc.wait(timeout=5)


