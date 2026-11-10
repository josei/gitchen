"""Role skills, the Claude Code way: skills live in the instance's
`skills/<name>/SKILL.md`, a role lists the ones it preloads in its
frontmatter (`skills:`), and the daemon injects them into its prompt."""

from pathlib import Path

import pytest

from mcp import dispatch
from mcp.config import ProjectConfig
from tests.test_dispatch import make_project

SHARED = {"dir": "/tmp/shared/7/abc", "url": "/shared/7/abc"}
ISSUE = {"id": 7, "title": "t", "base_branch": "main", "lines": [], "assignment_history": []}


def _skill(root: Path, name: str, body: str, **front) -> None:
    folder = root / "skills" / name
    folder.mkdir(parents=True, exist_ok=True)
    head = "".join(f"{k}: {v}\n" for k, v in {"name": name, **front}.items())
    (folder / "SKILL.md").write_text(f"---\n{head}---\n{body}\n")


def _role(root: Path, role: str, skills: str) -> None:
    (root / "agents" / f"{role}.md").write_text(
        f"---\nname: {role}\ndescription: d\nskills: {skills}\n---\nYou are the {role}.")


def test_a_role_gets_exactly_the_skills_it_lists_in_full(tmp_path):
    root = make_project(tmp_path).project_root
    _skill(root, "screenshots", "Take one per feature.", description="How to capture the UI")
    _skill(root, "release-notes", "Write them short.", description="Release notes")
    _role(root, "reviewer", "[screenshots]")
    cfg = ProjectConfig.load(root)

    prompt = dispatch._build_prompt(cfg, "reviewer", ISSUE, SHARED)
    assert "## Skill: screenshots" in prompt
    assert "How to capture the UI" in prompt and "Take one per feature." in prompt
    assert str(root / "skills" / "screenshots") in prompt, "the skill's folder is named for its files"
    assert "release-notes" not in prompt, "a skill no one listed is not loaded"
    assert prompt.index("You are the reviewer.") < prompt.index("## Skill: screenshots") \
        < prompt.index("## Issue #7")

    builder = dispatch._build_prompt(cfg, "builder", ISSUE, SHARED)
    assert "## Skill:" not in builder, "skills are per role, never inherited"


def test_skills_load_in_the_order_listed(tmp_path):
    root = make_project(tmp_path).project_root
    _skill(root, "b", "second")
    _skill(root, "a", "first")
    _role(root, "reviewer", "[b, a]")
    prompt = dispatch._build_prompt(ProjectConfig.load(root), "reviewer", ISSUE, SHARED)
    assert prompt.index("## Skill: b") < prompt.index("## Skill: a")


def test_a_missing_skill_is_refused_at_load(tmp_path):
    root = make_project(tmp_path).project_root
    _role(root, "reviewer", "[nope]")
    with pytest.raises(ValueError, match="role reviewer: skill nope: there is no skills/nope/SKILL.md"):
        ProjectConfig.load(root)


def test_a_person_only_skill_cannot_be_preloaded(tmp_path):
    root = make_project(tmp_path).project_root
    _skill(root, "publish", "Push it.", **{"disable-model-invocation": "true"})
    _role(root, "reviewer", "[publish]")
    with pytest.raises(ValueError, match="disable-model-invocation"):
        ProjectConfig.load(root)


def test_a_person_only_skill_that_no_role_lists_is_fine(tmp_path):
    root = make_project(tmp_path).project_root
    _skill(root, "publish", "Push it.", **{"disable-model-invocation": "true"})
    ProjectConfig.load(root)


def test_a_skill_name_cannot_point_outside_skills(tmp_path):
    root = make_project(tmp_path).project_root
    _role(root, "reviewer", "['../agents']")
    with pytest.raises(ValueError, match="named by its folder"):
        ProjectConfig.load(root)
