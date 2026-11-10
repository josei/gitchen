"""gitchen retro: what the recorded turns cost, by role and model, and the
costliest issues. Read-only, and built only from what is on the issues."""

import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mcp import issues, retro  # noqa: E402
from mcp.config import ProjectConfig  # noqa: E402


def make_project(tmp_path: Path) -> ProjectConfig:
    root = tmp_path / "instance"
    (root / "agents").mkdir(parents=True)
    for role in ("builder", "reviewer", "pm"):
        (root / "agents" / f"{role}.md").write_text(f"---\nname: {role}\n---\n{role}")
    (tmp_path / "public").mkdir()
    (root / "project.yml").write_text("public_repo: ../public\n")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return ProjectConfig.load(root)


def turn(role, model, usd, at, seconds=0, tokens=10):
    return {"at": at, "role": role, "model": model, "usd": usd, "input": tokens, "output": 0,
            "cache_read": 0, "cache_write": 0, "seconds": seconds}


def stamp(ago_seconds=0):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - ago_seconds))


def test_since_is_read_in_hours_or_days():
    assert retro.parse_since("12h") == 12 * 3600
    assert retro.parse_since("7d") == 7 * 86400
    with pytest.raises(ValueError):
        retro.parse_since("a week")


def test_spending_is_grouped_by_role_and_model(tmp_path):
    cfg = make_project(tmp_path)
    a = issues.create_issue(cfg, "a", "b", None, 0, "pm", "pm", assignee="builder")
    issues.add_comment(cfg, a["id"], "r1", "builder", "builder", turns=[
        turn("builder", "small", 0.1, stamp(), seconds=30), turn("builder", "big", 0.5, stamp())])
    issues.add_comment(cfg, a["id"], "r2", "reviewer", "reviewer", turns=[
        turn("reviewer", "small", 0.2, stamp(), seconds=10)])
    summary = retro.summarize(cfg)
    rows = {(r["role"], r["model"]): r for r in summary["by_role_model"]}
    assert rows[("builder", "small")]["usd"] == 0.1 and rows[("builder", "small")]["seconds"] == 30
    assert rows[("builder", "big")]["usd"] == 0.5
    assert rows[("reviewer", "small")]["usd"] == 0.2
    assert summary["total_usd"] == 0.8


def test_the_window_leaves_out_older_turns(tmp_path):
    cfg = make_project(tmp_path)
    a = issues.create_issue(cfg, "a", "b", None, 0, "pm", "pm", assignee="builder")
    issues.add_comment(cfg, a["id"], "old", "builder", "builder",
                       turns=[turn("builder", "small", 9.0, stamp(30 * 86400))])
    issues.add_comment(cfg, a["id"], "new", "builder", "builder",
                       turns=[turn("builder", "small", 1.0, stamp())])
    assert retro.summarize(cfg, since=7 * 86400)["total_usd"] == 1.0
    assert retro.summarize(cfg)["total_usd"] == 10.0


def test_the_costliest_issues_come_first_with_their_titles(tmp_path):
    cfg = make_project(tmp_path)
    cheap = issues.create_issue(cfg, "cheap one", "b", None, 0, "pm", "pm", assignee="builder")
    dear = issues.create_issue(cfg, "dear one", "b", None, 0, "pm", "pm", assignee="builder")
    issues.add_comment(cfg, cheap["id"], "x", "builder", "builder", turns=[turn("builder", "m", 0.1, stamp())])
    issues.add_comment(cfg, dear["id"], "x", "builder", "builder", turns=[turn("builder", "m", 2.0, stamp())])
    top = retro.summarize(cfg, top=1)["costliest"]
    assert top == [{"id": dear["id"], "title": "dear one", "usd": 2.0}]


def test_render_is_a_readable_table(tmp_path):
    cfg = make_project(tmp_path)
    a = issues.create_issue(cfg, "a", "b", None, 0, "pm", "pm", assignee="builder")
    issues.add_comment(cfg, a["id"], "x", "builder", "builder", turns=[turn("builder", "small", 0.25, stamp())])
    text = retro.render(retro.summarize(cfg))
    assert "builder" in text and "small" in text and "$0.25" in text
    assert "costliest" in text.lower()
    assert retro.render(retro.summarize(make_project(tmp_path / "empty"))).strip() == \
        "No turns recorded yet."
