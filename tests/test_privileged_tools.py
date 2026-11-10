"""server.dispatch()'s privileged tier: assign_issue/return_issue/
comment_issue. These exist for the coordinator's own cron or
interactive turns, which never pass through dispatch.py's automatic
end-of-turn handling — unlike a pool-spawned worker/reviewer, the
coordinator's connection is already unrestricted, so there's no forgery
concern in giving it these directly."""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mcp import harness as harness_module  # noqa: E402
from mcp import issues, server  # noqa: E402
from mcp.config import ProjectConfig  # noqa: E402
from mcp.harness import TurnResult  # noqa: E402

IDENTITY = ("coordinator", "coordinator")


def make_project(tmp_path: Path) -> ProjectConfig:
    root = tmp_path / "instance"
    (root / "agents").mkdir(parents=True)
    for _role in ("builder", "reviewer", "pm", "researcher"):
        _f = root / "agents" / f"{_role}.md"
        if not _f.exists():
            _f.write_text(f"---\nname: {_role}\n---\n{_role}")
    (root / "agents" / "researcher.md").write_text(
        "---\nname: researcher\ndescription: researches things\n---\nYou are the researcher.")
    (tmp_path / "public").mkdir()
    (root / "project.yml").write_text("public_repo: ../public\n")
    subprocess.run(["git", "init", "-q"], cwd=root)
    return ProjectConfig.load(root)


def test_assign_issue_pushes(tmp_path):
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "title", "body", None, 0, *IDENTITY, assignee="builder")

    result = server.dispatch(cfg, "assign_issue", {"id": created["id"], "to": "builder"}, IDENTITY)

    assert result["assignee"] == "builder"


def test_return_issue_pops(tmp_path):
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "title", "body", None, 0, "pm", "pm")
    server.dispatch(cfg, "assign_issue", {"id": created["id"], "to": "builder"}, ("pm", "pm"))

    result = server.dispatch(cfg, "return_issue", {"id": created["id"]},
                              ("builder (task x)", "builder"))

    assert result["assignee"] == "pm"


def test_return_issue_with_nothing_above_blocks_the_issue(tmp_path):
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "title", "body", None, 0, *IDENTITY, assignee="builder")

    result = server.dispatch(cfg, "return_issue", {"id": created["id"]}, ("builder (task x)", "builder"))

    assert result["blocked"] is True
    assert issues.get_issue(cfg, created["id"])["state"] == "blocked"


def test_assign_issue_refuses_the_coordinator(tmp_path):
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "title", "body", None, 0, *IDENTITY, assignee="builder")
    try:
        server.dispatch(cfg, "assign_issue", {"id": created["id"], "to": "coordinator"}, IDENTITY)
        assert False, "expected a refusal"
    except ValueError:
        pass
    assert issues.get_issue(cfg, created["id"])["assignee"] == "builder"


def test_comment_issue_appends(tmp_path):
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "title", "body", None, 0, *IDENTITY, assignee="builder")

    result = server.dispatch(cfg, "comment_issue", {"id": created["id"], "text": "noted"}, IDENTITY)

    body_text = "\n".join(line["text"] for line in result["lines"])
    assert "noted" in body_text




def test_move_issue_is_a_tool_and_takes_fractional_priorities(tmp_path):
    cfg = make_project(tmp_path)
    a = issues.create_issue(cfg, "a", "b", None, 10, *IDENTITY, assignee="builder")
    b = issues.create_issue(cfg, "b", "b", None, 20, *IDENTITY, assignee="builder")
    server.dispatch(cfg, "move_issue", {"id": b["id"], "before": a["id"]}, IDENTITY)
    assert [i["id"] for i in issues.list_issues(cfg, state="open")] == [b["id"], a["id"]]
    server.dispatch(cfg, "update_issue", {"id": a["id"], "hash": issues.get_issue(cfg, a["id"])["hash"],
                                          "priority": 12.5}, IDENTITY)
    assert issues.get_issue(cfg, a["id"])["priority"] == 12.5
