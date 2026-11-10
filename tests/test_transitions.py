"""Outcomes and transitions: a role reports an outcome, and the instance's
`transitions` table says what that outcome does. Assign pushes onto the
assignment stack; return pops; the closing verbs close the issue."""

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mcp import dispatch, harness as harness_module, issues  # noqa: E402
from mcp.config import ProjectConfig  # noqa: E402
from mcp.harness import TurnResult  # noqa: E402

ROLES = {
    "pm": "---\nname: pm\ndescription: plans\n---\nYou are the pm.",
    "builder": "---\nname: builder\ndescription: builds\noutcomes: {built: the work is committed, "
               "stuck: it cannot be done}\n---\nYou are the builder.",
    "reviewer": "---\nname: reviewer\ndescription: reviews\noutcomes: {approved: all criteria met, "
                "rejected: something must change}\n---\nYou are the reviewer.",
}
TRANSITIONS = {"builder": {"built": "assign reviewer", "stuck": "return"},
               "reviewer": {"approved": "merge", "rejected": "return"}}


def make_project(tmp_path: Path, transitions=TRANSITIONS, roles=ROLES, allowed=None) -> ProjectConfig:
    root = tmp_path / "instance"
    (root / "agents").mkdir(parents=True)
    for name, text in roles.items():
        (root / "agents" / f"{name}.md").write_text(text)
    public = tmp_path / "public"
    public.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=public, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
                    "--allow-empty", "-m", "init"], cwd=public, check=True)
    data = {"public_repo": "../public"}
    if transitions is not None:
        data["transitions"] = transitions
    if allowed is not None:
        data["allowed_tools"] = allowed
    (root / "project.yml").write_text(yaml.safe_dump(data))
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return ProjectConfig.load(root)


def which_role(prompt: str) -> str:
    return next(r for r in ROLES if f"You are the {r}." in prompt)


def commit_work(cwd):
    (Path(cwd) / "work.txt").write_text("x")
    subprocess.run(["git", "add", "work.txt"], cwd=cwd, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "work"],
                   cwd=cwd, check=True)


class Script:
    """A fake harness: each role answers from its own queue of replies. A reply
    can be a function of the worktree, so it can commit work first."""

    def __init__(self, replies):
        self.replies = {role: list(items) for role, items in replies.items()}
        self.seen = []
        self.current = None

    def _answer(self, cwd):
        reply = self.replies[self.current].pop(0)
        return reply(cwd) if callable(reply) else reply

    def start(self, prompt, cwd, settings):
        self.current = which_role(prompt)
        self.seen.append(self.current)
        return TurnResult("s", self._answer(cwd), {}, True)

    def resume(self, session_id, prompt, cwd, settings):
        return TurnResult(session_id, self._answer(cwd), {}, True)

    def interrupt(self, *a, **k):
        pass


# --- the table is validated when the project loads --------------------------

def test_an_outcome_the_role_does_not_declare_is_refused(tmp_path):
    with pytest.raises(ValueError, match="does not declare that outcome"):
        make_project(tmp_path, transitions={"builder": {"finished": "return"}})


def test_a_role_without_outcomes_cannot_be_wired(tmp_path):
    with pytest.raises(ValueError, match="declares no outcomes"):
        make_project(tmp_path, transitions={"pm": {"done": "return"}})


def test_a_verb_that_is_not_known_is_refused(tmp_path):
    with pytest.raises(ValueError, match="verb"):
        make_project(tmp_path, transitions={"builder": {"built": "fly"}})


def test_assign_must_name_a_role(tmp_path):
    with pytest.raises(ValueError, match="assign"):
        make_project(tmp_path, transitions={"builder": {"built": "assign nobody"}})


# --- the walk -----------------------------------------------------------------

def test_build_review_reject_rebuild_approve_merges_with_no_pm_turn(tmp_path):
    cfg = make_project(tmp_path)
    script = Script({
        "pm": [],
        "builder": [
            lambda cwd: (commit_work(cwd), "Built it.\nNEXT: built")[1],
            lambda cwd: "Fixed it.\nNEXT: built",
        ],
        "reviewer": [
            "Not quite.\nNEXT: rejected",
            lambda cwd: "Good now.\nNEXT: approved",
        ],
    })
    harness_module._REGISTRY["fake"] = lambda: script
    created = issues.create_issue(cfg, "build it", "b", None, 0, "pm", "pm", assignee="builder")
    for _ in range(4):
        for t in dispatch.tick(cfg):
            t.join(10)
    final = issues.get_issue(cfg, created["id"])
    assert final["state"] == "merged"
    assert script.seen == ["builder", "reviewer", "builder", "reviewer"], "no pm turn in between"
    assert "pm" not in script.seen


def test_a_stuck_builder_hands_back_to_whoever_pushed_it(tmp_path):
    cfg = make_project(tmp_path)
    script = Script({"pm": [], "builder": ["Cannot do it.\nNEXT: stuck"], "reviewer": []})
    harness_module._REGISTRY["fake"] = lambda: script
    created = issues.create_issue(cfg, "x", "b", None, 0, "pm", "pm", assignee="builder")
    for t in dispatch.tick(cfg):
        t.join(10)
    assert issues.get_issue(cfg, created["id"])["assignee"] == "pm"


def test_an_outcome_the_table_does_not_list_is_corrected_not_obeyed(tmp_path):
    cfg = make_project(tmp_path)
    script = Script({"pm": [], "builder": ["Done.\nNEXT: merge", "Fine.\nNEXT: stuck"], "reviewer": []})
    harness_module._REGISTRY["fake"] = lambda: script
    created = issues.create_issue(cfg, "x", "b", None, 0, "pm", "pm", assignee="builder")
    for t in dispatch.tick(cfg):
        t.join(10)
    final = issues.get_issue(cfg, created["id"])
    assert final["assignee"] == "pm" and final["state"] == "open", "merge was not a builder outcome"


def test_a_role_outside_the_table_keeps_the_plain_directives(tmp_path):
    roles = dict(ROLES, researcher="---\nname: researcher\ndescription: looks\n---\nYou are the researcher.")
    cfg = make_project(tmp_path, roles=roles, transitions={"builder": {"built": "assign reviewer"}})
    line = dispatch._directive_line(cfg, "researcher")
    assert "NEXT: return" in line and "NEXT: built" not in line


def test_a_wired_role_is_offered_only_its_outcomes(tmp_path):
    cfg = make_project(tmp_path)
    line = dispatch._directive_line(cfg, "builder")
    assert "NEXT: built" in line and "NEXT: stuck" in line
    assert "NEXT: return" not in line and "NEXT: done" not in line


def test_a_turns_mcp_server_loads_without_reading_the_role_prompts(tmp_path):
    """The turn's MCP server is sandboxed without read access to agents/, so
    loading the project there must not read them. The daemon still checks."""
    cfg = make_project(tmp_path)
    (cfg.project_root / "project.yml").write_text(
        "public_repo: ../public\ntransitions:\n  builder: {finished: return}\n")  # undeclared outcome
    with pytest.raises(ValueError, match="does not declare"):
        ProjectConfig.load(cfg.project_root)
    (cfg.project_root / "agents" / "builder.md").unlink()  # and the prompt can't be read at all
    proxy_view = ProjectConfig.load(cfg.project_root, check_outcomes=False)
    assert proxy_view.transitions == {} and proxy_view.public_repo.name == "public"


def test_an_approval_of_work_with_no_commits_merges_and_does_not_loop(tmp_path):
    # A reviewer may approve an issue whose builder changed nothing: the branch
    # is at base, so the merge is a no-op. It must close, not send the issue back.
    cfg = make_project(tmp_path)
    script = Script({
        "pm": [],
        "reviewer": ["Nothing to change; every criterion already holds.\nNEXT: approved"],
    })
    harness_module._REGISTRY["fake"] = lambda: script
    created = issues.create_issue(cfg, "check it", "b", None, 0, "pm", "pm", assignee="reviewer")
    for t in dispatch.tick(cfg):
        t.join(10)
    final = issues.get_issue(cfg, created["id"])
    assert final["state"] == "merged"
    assert script.seen == ["reviewer"]
