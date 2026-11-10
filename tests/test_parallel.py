"""max_parallel: how many turns may run at once. Each turn holds its own issue,
so the same role may run on several issues at the same time."""

import subprocess
import sys
import threading
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mcp import dispatch, harness as harness_module, holders, issues, manual  # noqa: E402
from mcp.config import ProjectConfig  # noqa: E402
from mcp.harness import TurnResult  # noqa: E402


def make_project(tmp_path: Path, max_parallel=None) -> ProjectConfig:
    # A short root: AF_UNIX paths are limited to ~104 bytes, and a manual tick
    # serves its control socket inside the project.
    import tempfile
    tmp_path = Path(tempfile.mkdtemp(prefix="pa", dir="/tmp"))
    root = tmp_path / "instance"
    (root / "agents").mkdir(parents=True)
    for role in ("builder", "reviewer", "pm"):
        (root / "agents" / f"{role}.md").write_text(f"---\nname: {role}\ndescription: {role}\n---\n{role}")
    public = tmp_path / "public"
    public.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=public, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
                    "--allow-empty", "-m", "init"], cwd=public, check=True)
    data = {"public_repo": "../public"}
    if max_parallel is not None:
        data["max_parallel"] = max_parallel
    (root / "project.yml").write_text(yaml.safe_dump(data))
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return ProjectConfig.load(root)


class Held:
    """Every turn waits until released, so the test can see what runs together."""

    def __init__(self):
        self.gate = threading.Event()
        self.started = []
        self.lock = threading.Lock()

    def start(self, prompt, cwd, settings):
        with self.lock:
            self.started.append(cwd)
        self.gate.wait(20)
        return TurnResult("s", "Done.\nNEXT: return", {}, True)

    def resume(self, *a, **k):
        raise AssertionError

    def interrupt(self, *a, **k):
        pass


def _three_issues(cfg):
    return [issues.create_issue(cfg, f"issue {n}", "b", None, n, "pm", "pm", assignee="builder")["id"]
            for n in range(3)]


def test_the_default_is_one_turn_at_a_time(tmp_path):
    cfg = make_project(tmp_path)
    assert cfg.max_parallel == 1
    held = Held()
    harness_module._REGISTRY["fake"] = lambda: held
    _three_issues(cfg)
    threads = dispatch.tick(cfg)
    assert len(threads) == 1
    assert dispatch.tick(cfg) == [], "a second turn waits for the first"
    held.gate.set()
    for t in threads:
        t.join(10)


def test_max_parallel_starts_that_many_and_no_more(tmp_path):
    cfg = make_project(tmp_path, max_parallel=2)
    held = Held()
    harness_module._REGISTRY["fake"] = lambda: held
    _three_issues(cfg)
    first = dispatch.tick(cfg)
    assert len(first) == 2
    assert dispatch.tick(cfg) == [], "both slots are full"
    held.gate.set()
    for t in first:
        t.join(10)


def _live_roles(cfg):
    """role of every live holder, read through the holder records."""
    roles = []
    for path in sorted((cfg.project_root / ".agents-daemon" / "holders").glob("*.json")):
        if path.stem.isdigit():
            record = holders.current(cfg.project_root, int(path.stem))
            if record:
                roles.append(record["role"])
    return roles


def test_each_started_turn_has_its_own_issue_and_worktree(tmp_path):
    cfg = make_project(tmp_path, max_parallel=3)
    held = Held()
    harness_module._REGISTRY["fake"] = lambda: held
    _three_issues(cfg)
    threads = dispatch.tick(cfg)
    assert len(threads) == 3
    held.gate.set()
    for t in threads:
        t.join(10)
    assert len(held.started) == 3 and len(set(held.started)) == 3, "one turn per issue"


def test_the_same_role_runs_on_two_issues_at_once(tmp_path):
    cfg = make_project(tmp_path, max_parallel=2)
    held = Held()
    harness_module._REGISTRY["fake"] = lambda: held
    _three_issues(cfg)
    threads = dispatch.tick(cfg)
    assert _live_roles(cfg) == ["builder", "builder"]
    held.gate.set()
    for t in threads:
        t.join(10)


def test_a_manual_tick_starts_one_turn_whatever_the_limit(tmp_path):
    cfg = make_project(tmp_path, max_parallel=3)
    held = Held()
    harness_module._REGISTRY["fake"] = lambda: held
    _three_issues(cfg)
    held.gate.set()
    result = manual.run_one(cfg)
    assert "started 1 turn" in result
