"""The library's role prompts say what a role is for. The framework supplies
the mechanics (how a report is posted, how a picture is embedded, how the
outcome is written), so a prompt that spells them out would go stale the day
the framework changes, and would disagree with the daemon's own wording."""

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mcp import frontmatter  # noqa: E402

LIBRARY = Path(__file__).resolve().parent.parent / "library" / "agents"
PROMPTS = sorted(LIBRARY.glob("*.md"))
MECHANICS = [
    (re.compile(r"NEXT:"), "a NEXT: directive, which the daemon supplies"),
    (re.compile(r"!\[[^\]]*\]\("), "markdown image syntax, which the framework explains"),
    (re.compile(r"artifacts? folder", re.IGNORECASE), "the artifacts folder, which the framework names"),
    (re.compile(r"`[^`\n]*\.(png|jpe?g|gif|webp)`"), "an image path in backticks, which shows nothing"),
]


def test_the_library_has_the_roles_the_docs_name():
    names = {p.stem for p in PROMPTS}
    assert {"builder", "reviewer", "researcher", "pm", "retro", "strategist"} <= names


@pytest.mark.parametrize("path", PROMPTS, ids=lambda p: p.stem)
def test_a_prompt_describes_its_role_and_nothing_about_mechanics(path):
    front, body = frontmatter.parse(path.read_text())
    assert front.get("name") == path.stem
    assert str(front.get("description", "")).strip(), "every role needs a description"
    for pattern, what in MECHANICS:
        hit = pattern.search(path.read_text())
        assert hit is None, f"{path.name} spells out {what}: {hit.group(0)!r}"


@pytest.mark.parametrize("name", ["builder", "reviewer", "researcher"])
def test_a_wired_role_declares_its_outcomes(name):
    front, _body = frontmatter.parse((LIBRARY / f"{name}.md").read_text())
    outcomes = front.get("outcomes") or {}
    assert outcomes, f"{name} must say what outcomes it can report"
    assert all(str(v).strip() for v in outcomes.values()), "each outcome says what it means"


def test_pm_leaves_the_directive_wording_to_the_framework():
    body = (LIBRARY / "pm.md").read_text()
    assert "NEXT" not in body
    assert "flag it for a human" not in body.lower(), "the pm makes product calls rather than waiting"
    assert "revisit" in body, "a call a person may want to revisit is named in the report"
