"""The spending limit: counted from recorded turn costs, checked before a turn
starts, reset by a person."""

import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mcp import budget, control, dispatch, issues, server  # noqa: E402
from mcp import cost  # noqa: E402
from mcp import harness as harness_module  # noqa: E402
from mcp.config import ProjectConfig  # noqa: E402
from mcp.harness import TurnResult  # noqa: E402

HOUR = 3600


def make_project(tmp_path: Path, budget_conf=None, allowed_tools=None) -> ProjectConfig:
    root = tmp_path / "instance"
    (root / "agents").mkdir(parents=True)
    for role in ("builder", "reviewer", "pm"):
        (root / "agents" / f"{role}.md").write_text(f"---\nname: {role}\ndescription: {role}\n---\n{role}")
    public = tmp_path / "public"
    public.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=public)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
                    "--allow-empty", "-m", "init"], cwd=public)
    data = {"public_repo": "../public"}
    if budget_conf is not None:
        data["budget"] = budget_conf
    if allowed_tools is not None:
        data["allowed_tools"] = allowed_tools
    (root / "project.yml").write_text(yaml.safe_dump(data))
    subprocess.run(["git", "init", "-q"], cwd=root)
    return ProjectConfig.load(root)


def spend(cfg, issue_id, usd, at=None):
    """Record a cost on an issue as a real turn would."""
    stamp = at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    turn = {"at": stamp, "role": "builder", "model": "m", "usd": usd, "input": 0, "output": 0,
            "cache_read": 0, "cache_write": 0, "seconds": 0}
    issues.add_comment(cfg, issue_id, "a report", "builder", "builder", turns=[turn])


def test_the_limit_is_validated(tmp_path):
    for bad in ({"usd": 0}, {"usd": -1}, {"usd": True}, {"usd": 3, "per": "5 hours"},
                {"usd": 3, "per": "0h"}, {"per": "5h"}):
        with pytest.raises(ValueError):
            make_project(tmp_path / str(abs(hash(str(bad)))), budget_conf=bad)
    assert make_project(tmp_path / "ok", budget_conf={"usd": 30, "per": "5h"}).budget == {
        "usd": 30.0, "per": "5h", "per_seconds": 5 * HOUR}


def test_no_budget_means_no_status(tmp_path):
    assert budget.status(make_project(tmp_path)) is None


def test_spending_is_summed_from_the_issues(tmp_path):
    cfg = make_project(tmp_path, {"usd": 10})
    a = issues.create_issue(cfg, "a", "b", None, 0, "pm", "pm", assignee="builder")
    b = issues.create_issue(cfg, "b", "b", None, 0, "pm", "pm", assignee="builder")
    spend(cfg, a["id"], 1.5)
    spend(cfg, b["id"], 2.0)
    state = budget.status(cfg)
    assert state["spent"] == 3.5 and state["limit"] == 10 and state["over"] is False


def test_reaching_the_limit_is_over(tmp_path):
    cfg = make_project(tmp_path, {"usd": 3})
    a = issues.create_issue(cfg, "a", "b", None, 0, "pm", "pm", assignee="builder")
    spend(cfg, a["id"], 3.0)
    assert budget.status(cfg)["over"] is True


def test_spending_older_than_the_window_stops_counting(tmp_path):
    cfg = make_project(tmp_path, {"usd": 3, "per": "5h"})
    a = issues.create_issue(cfg, "a", "b", None, 0, "pm", "pm", assignee="builder")
    old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 6 * HOUR))
    spend(cfg, a["id"], 5.0, at=old)
    assert budget.status(cfg)["over"] is False, "six hours ago is outside a 5h window"


def test_without_a_window_stays_stopped_until_reset(tmp_path):
    cfg = make_project(tmp_path, {"usd": 3})
    a = issues.create_issue(cfg, "a", "b", None, 0, "pm", "pm", assignee="builder")
    old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 100 * HOUR))
    spend(cfg, a["id"], 5.0, at=old)
    assert budget.status(cfg)["over"] is True, "no window: old spending still counts"


def test_a_reset_counts_only_spending_after_it(tmp_path):
    cfg = make_project(tmp_path, {"usd": 3})
    a = issues.create_issue(cfg, "a", "b", None, 0, "pm", "pm", assignee="builder")
    now = time.time()
    spend(cfg, a["id"], 5.0, at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 100)))
    budget.reset(cfg.project_root, now=now - 50)
    state = budget.status(cfg, now=now)
    assert state["spent"] == 0 and state["over"] is False, "spending before the reset is ignored"
    spend(cfg, a["id"], 3.0)
    assert budget.status(cfg)["over"] is True


def test_the_reset_is_refused_over_the_control_socket(tmp_path):
    cfg = make_project(tmp_path, {"usd": 3})
    cfg.allowed_tools["pm"] = ["reset_budget", "get_issue"]
    from mcp import holders
    holders.claim(cfg.project_root, 1, "task-b", __import__("os").getpid(), "pm")
    with pytest.raises(PermissionError, match="coordinator-only"):
        control._execute(cfg, "task-b", "reset_budget", {})


def test_the_coordinator_can_reset_and_see_the_status(tmp_path):
    cfg = make_project(tmp_path, {"usd": 3})
    a = issues.create_issue(cfg, "a", "b", None, 0, "pm", "pm", assignee="builder")
    spend(cfg, a["id"], 5.0)
    assert server.dispatch(cfg, "reset_budget", {}, ("coordinator", "coordinator"))["over"] is False


def test_a_full_budget_starts_no_turn_and_changes_no_issue(tmp_path):
    started = []

    class H:
        def start(self, prompt, cwd, settings):
            started.append(prompt)
            return TurnResult("s1", "Done.\nNEXT: return", {}, True)

        def resume(self, *a, **k):
            raise AssertionError

        def interrupt(self, *a, **k):
            pass

    harness_module._REGISTRY["fake"] = H
    cfg = make_project(tmp_path, {"usd": 3})
    a = issues.create_issue(cfg, "a", "b", None, 0, "pm", "pm", assignee="builder")
    spend(cfg, a["id"], 3.0)
    before = issues.get_issue(cfg, a["id"])

    assert dispatch.tick(cfg) == []
    assert started == []
    assert issues.get_issue(cfg, a["id"]) == before
    assert "budget" in dispatch.next_action(cfg)


def test_a_turn_stopped_mid_issue_resumes_with_the_same_role_after_a_reset(tmp_path):
    class H:
        def start(self, prompt, cwd, settings):
            return TurnResult("s1", "Built.\nNEXT: assign reviewer", {}, True,
                              usage=[cost.record("m", usd=2.0)])

        def resume(self, *a, **k):
            raise AssertionError

        def interrupt(self, *a, **k):
            pass

    harness_module._REGISTRY["fake"] = H
    cfg = make_project(tmp_path, {"usd": 2}, allowed_tools={"builder": ["assign_issue"]})
    a = issues.create_issue(cfg, "a", "b", None, 0, "pm", "pm", assignee="builder")
    for t in dispatch.tick(cfg):
        t.join(10)
    assert issues.get_issue(cfg, a["id"])["assignee"] == "reviewer"
    assert dispatch.tick(cfg) == [], "the reviewer waits: the budget is spent"
    assert issues.get_issue(cfg, a["id"])["assignee"] == "reviewer"

    budget.reset(cfg.project_root, now=time.time() + 1)
    harness_module._REGISTRY["fake"] = lambda: type("R", (), {
        "start": lambda self, *x, **k: TurnResult("s2", "Reviewed.\nNEXT: return", {}, True),
        "resume": lambda self, *x, **k: None, "interrupt": lambda self, *x, **k: None})()
    for t in dispatch.tick(cfg):
        t.join(10)
    assert issues.get_issue(cfg, a["id"])["assignee"] == "builder", \
        "the reviewer carried on and handed back to whoever pushed it"
