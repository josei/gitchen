"""NEXT: blocked_by: an issue waits for other issues to close, then wakes by
itself. No condition language, no escalation: the wait is on issues, and the
wake-up is the framework's, not a person's."""

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mcp import dispatch, harness as harness_module, issues, worktrees  # noqa: E402
from mcp.config import ProjectConfig  # noqa: E402
from mcp.harness import TurnResult  # noqa: E402


def make_project(tmp_path: Path, allowed=None) -> ProjectConfig:
    root = tmp_path / "instance"
    (root / "agents").mkdir(parents=True)
    for role in ("builder", "reviewer", "pm"):
        (root / "agents" / f"{role}.md").write_text(f"---\nname: {role}\ndescription: {role}\n---\n{role}")
    public = tmp_path / "public"
    public.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=public, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
                    "--allow-empty", "-m", "init"], cwd=public, check=True)
    data = {"public_repo": "../public", "allowed_tools": allowed or {"pm": ["update_issue"]}}
    (root / "project.yml").write_text(yaml.safe_dump(data))
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return ProjectConfig.load(root)


def close(cfg, issue_id, state):
    current = issues.get_issue(cfg, issue_id)
    issues.update_issue(cfg, issue_id, current["hash"], {"state": state}, "coordinator", "coordinator")


def test_parsing_reads_the_ids_and_is_a_closing_verb_only_where_allowed():
    assert dispatch._parse_next("Waiting.\nNEXT: blocked_by #12 #13", True, False) == ("blocked_by", [12, 13])
    assert dispatch._parse_next("Waiting.\nNEXT: blocked_by 7, 8", True, False) == ("blocked_by", [7, 8])
    assert dispatch._parse_next("Waiting.\nNEXT: blocked_by #12", False, False) is None


def test_block_on_waits_and_records_why(tmp_path):
    cfg = make_project(tmp_path)
    a = issues.create_issue(cfg, "a", "b", None, 0, "pm", "pm", assignee="builder")
    b = issues.create_issue(cfg, "b", "b", None, 0, "pm", "pm", assignee="builder")
    issues.block_on(cfg, b["id"], [a["id"]], "pm", "pm")
    waiting = issues.get_issue(cfg, b["id"])
    assert waiting["state"] == "blocked" and waiting["blocked_by"] == [a["id"]]
    assert waiting["assignment_history"][-1]["kind"] == "waiting"
    assert issues.comment_count(waiting) == 0, "waiting is an event, not a comment"


def test_an_issue_wakes_only_when_every_target_has_closed(tmp_path):
    cfg = make_project(tmp_path)
    a = issues.create_issue(cfg, "a", "b", None, 0, "pm", "pm", assignee="builder")
    b = issues.create_issue(cfg, "b", "b", None, 0, "pm", "pm", assignee="builder")
    c = issues.create_issue(cfg, "c", "b", None, 0, "pm", "pm", assignee="builder")
    issues.block_on(cfg, c["id"], [a["id"], b["id"]], "pm", "pm")
    close(cfg, a["id"], "done")
    issues.wake_dependents(cfg)
    assert issues.get_issue(cfg, c["id"])["state"] == "blocked", "b is still open"
    close(cfg, b["id"], "canceled")
    issues.wake_dependents(cfg)
    woken = issues.get_issue(cfg, c["id"])
    assert woken["state"] == "open" and woken["blocked_by"] == []
    assert woken["assignment_history"][-1]["kind"] == "woken"


def test_a_target_that_is_blocked_keeps_the_waiter_waiting(tmp_path):
    """A blocked target may need a person, so the waiter waits with it."""
    cfg = make_project(tmp_path)
    a = issues.create_issue(cfg, "a", "b", None, 0, "pm", "pm", assignee="builder")
    issues.update_issue(cfg, a["id"], issues.get_issue(cfg, a["id"])["hash"], {"state": "blocked"},
                        "coordinator", "coordinator")
    b = issues.create_issue(cfg, "b", "b", None, 0, "pm", "pm", assignee="builder")
    issues.block_on(cfg, b["id"], [a["id"]], "pm", "pm")
    issues.wake_dependents(cfg)
    assert issues.get_issue(cfg, b["id"])["state"] == "blocked"


def test_the_refusals(tmp_path):
    cfg = make_project(tmp_path)
    a = issues.create_issue(cfg, "a", "b", None, 0, "pm", "pm", assignee="builder")
    b = issues.create_issue(cfg, "b", "b", None, 0, "pm", "pm", assignee="builder")
    with pytest.raises(ValueError, match="itself"):
        issues.block_on(cfg, a["id"], [a["id"]], "pm", "pm")
    with pytest.raises(ValueError, match="does not exist"):
        issues.block_on(cfg, a["id"], [999], "pm", "pm")
    close(cfg, b["id"], "done")
    with pytest.raises(ValueError, match="already finished"):
        issues.block_on(cfg, a["id"], [b["id"]], "pm", "pm")


def test_a_cycle_is_refused(tmp_path):
    cfg = make_project(tmp_path)
    a = issues.create_issue(cfg, "a", "b", None, 0, "pm", "pm", assignee="builder")
    b = issues.create_issue(cfg, "b", "b", None, 0, "pm", "pm", assignee="builder")
    issues.block_on(cfg, a["id"], [b["id"]], "pm", "pm")
    with pytest.raises(ValueError, match="cycle"):
        issues.block_on(cfg, b["id"], [a["id"]], "pm", "pm")


def test_a_wake_is_not_a_person_reopening_it(tmp_path):
    """The circuit breaker counts from a person's reopen; a wake must not reset it."""
    cfg = make_project(tmp_path)
    a = issues.create_issue(cfg, "a", "b", None, 0, "pm", "pm", assignee="builder")
    b = issues.create_issue(cfg, "b", "b", None, 0, "pm", "pm", assignee="builder")
    issues.block_on(cfg, b["id"], [a["id"]], "pm", "pm")
    close(cfg, a["id"], "done")
    issues.wake_dependents(cfg)
    assert not [e for e in issues.get_issue(cfg, b["id"])["assignment_history"] if e["type"] == "reopen"]


def test_a_stacked_child_waits_for_its_parent_and_is_retargeted_when_it_merges(tmp_path):
    cfg = make_project(tmp_path)
    (cfg.public_repo / "README.md").write_text("hello\n")
    subprocess.run(["git", "-C", str(cfg.public_repo), "add", "README.md"], check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", str(cfg.public_repo),
                    "commit", "-qm", "readme"], check=True)
    parent = issues.create_issue(cfg, "parent", "b", None, 0, "pm", "pm", assignee="builder")
    worktree = worktrees.create(cfg, parent["id"], parent["base_branch"])
    (worktree / "parent.txt").write_text("work")
    subprocess.run(["git", "add", "parent.txt"], cwd=worktree, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "p"],
                   cwd=worktree, check=True)
    child = issues.create_issue(cfg, "child", "b", None, 0, "pm", "pm", assignee="builder",
                                base_branch=f"issue/{parent['id']}", blocked_by=[parent["id"]])
    assert child["state"] == "blocked" and child["blocked_by"] == [parent["id"]]

    issues.update_issue(cfg, parent["id"], issues.get_issue(cfg, parent["id"])["hash"],
                        {"state": "merged"}, "coordinator", "coordinator")
    issues.wake_dependents(cfg)

    woken = issues.get_issue(cfg, child["id"])
    assert woken["state"] == "open"
    assert woken["base_branch"] == "main", "retargeted onto the parent's own base"


def test_a_directive_that_would_cycle_goes_back_to_the_agent(tmp_path):
    cfg = make_project(tmp_path)
    a = issues.create_issue(cfg, "a", "b", None, 0, "pm", "pm", assignee="pm")
    b = issues.create_issue(cfg, "b", "b", None, 0, "pm", "pm", assignee="pm")
    issues.block_on(cfg, b["id"], [a["id"]], "pm", "pm")  # b waits on a
    seen = []

    class H:
        def start(self, prompt, cwd, settings):
            return TurnResult("s1", f"Waiting.\nNEXT: blocked_by #{b['id']}", {}, True)

        def resume(self, session_id, prompt, cwd, settings):
            seen.append(prompt)
            return TurnResult(session_id, "Fine, returning it.\nNEXT: return", {}, True)

        def interrupt(self, *a, **k):
            pass

    harness_module._REGISTRY["fake"] = H
    for t in dispatch.tick(cfg):  # a is open and runs; its wait on b would close the loop
        t.join(10)
    assert seen and "cycle" in seen[0]
