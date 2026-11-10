import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mcp import holders  # noqa: E402


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=5)
    return proc.pid


def test_claim_then_current_round_trips(tmp_path):
    assert holders.current(tmp_path, 1) is None
    ok = holders.claim(tmp_path, 1, "task-a", os.getpid(), "builder")
    assert ok is True
    assert holders.current(tmp_path, 1) == {"task_id": "task-a", "pid": os.getpid(), "role": "builder"}


def test_second_claim_on_a_live_holder_fails(tmp_path):
    holders.claim(tmp_path, 1, "task-a", os.getpid(), "builder")
    assert holders.claim(tmp_path, 1, "task-b", os.getpid(), "builder") is False


def test_release_clears_only_if_task_id_matches(tmp_path):
    holders.claim(tmp_path, 1, "task-a", os.getpid(), "builder")
    holders.release(tmp_path, 1, "task-b")  # some other, non-matching task id
    assert holders.current(tmp_path, 1) is not None, "a release from the wrong task_id must not clear it"

    holders.release(tmp_path, 1, "task-a")
    assert holders.current(tmp_path, 1) is None


def test_stale_holder_is_cleared_and_claimable(tmp_path):
    dead_pid = _dead_pid()
    holders.claim(tmp_path, 1, "task-a", dead_pid, "builder")
    assert holders.is_held(tmp_path, 1) is False, "a holder whose pid is no longer running is stale"

    assert holders.claim(tmp_path, 1, "task-b", os.getpid(), "builder") is True


def test_different_issues_are_independent(tmp_path):
    assert holders.claim(tmp_path, 1, "task-a", os.getpid(), "builder") is True
    assert holders.claim(tmp_path, 2, "task-b", os.getpid(), "builder") is True
    assert holders.is_held(tmp_path, 1) and holders.is_held(tmp_path, 2)
