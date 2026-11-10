"""Issues may sit in folders, and a role may be limited to the folders it
edits. A role can always file an issue anywhere, which is how it asks for a
change it can't make itself."""

import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mcp import control, dispatch, harness as harness_module, holders, issues, server  # noqa: E402
from mcp.config import ProjectConfig  # noqa: E402
from mcp.harness import TurnResult  # noqa: E402

IDENT = ("coordinator", "coordinator")


def make_project(extra=None) -> ProjectConfig:
    base = Path(tempfile.mkdtemp(prefix="fo", dir="/tmp"))
    root = base / "i"
    (root / "agents").mkdir(parents=True)
    for role in ("builder", "pm", "strategist"):
        (root / "agents" / f"{role}.md").write_text(f"---\nname: {role}\ndescription: {role}\n---\n{role}")
    public = base / "public"
    public.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=public, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
                    "--allow-empty", "-m", "init"], cwd=public, check=True)
    data = {"public_repo": "../public", "folders": ["company", "product"], "allowed_tools": {
        "pm": ["list_issues", "get_issue", "update_issue", "create_issue", "assign_issue", "move_issue"],
        "strategist": ["list_issues", "get_issue", "update_issue", "create_issue"]}}
    data.update(extra or {})
    (root / "project.yml").write_text(yaml.safe_dump(data))
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return ProjectConfig.load(root)


def make_folder_issue(cfg, folder, title="t", assignee="builder"):
    return issues.create_issue(cfg, title, "b", None, 0, *IDENT, assignee=assignee, folder=folder)


def test_an_issue_is_filed_in_its_folder_and_found_there(tmp_path):
    cfg = make_project({"default_folder": "product"})
    created = make_folder_issue(cfg, "company", title="mission")
    assert (cfg.issues_dir() / "company" / f"{created['id']}.md").exists()
    assert issues.get_issue(cfg, created["id"])["title"] == "mission"
    assert issues.list_issues(cfg)[0]["folder"] == "company"


def test_list_issues_can_filter_by_folder(tmp_path):
    cfg = make_project()
    product = make_folder_issue(cfg, "product", title="p")
    company = make_folder_issue(cfg, "company", title="c")
    assert [i["id"] for i in issues.list_issues(cfg, folder="product")] == [product["id"]]
    assert [i["id"] for i in issues.list_issues(cfg, folder="company")] == [company["id"]]
    assert {i["id"] for i in issues.list_issues(cfg)} == {product["id"], company["id"]}


def test_ids_are_unique_across_folders(tmp_path):
    cfg = make_project()
    a = make_folder_issue(cfg, "product")
    b = make_folder_issue(cfg, "company")
    assert [a["id"], b["id"]] == [1, 2]


def test_a_folder_is_required_when_the_project_has_no_default(tmp_path):
    cfg = make_project()
    with pytest.raises(ValueError, match="folder"):
        issues.create_issue(cfg, "t", "b", None, 0, *IDENT, assignee="builder")
    created = make_folder_issue(cfg, "product")
    assert created["folder"] == "product"


def test_an_unknown_folder_is_refused(tmp_path):
    cfg = make_project()
    with pytest.raises(ValueError, match="folder"):
        make_folder_issue(cfg, "Not A Folder!")
    with pytest.raises(ValueError, match="not declared"):
        make_folder_issue(cfg, "never-declared")


def test_the_default_folder_is_used_when_none_is_given(tmp_path):
    cfg = make_project({"default_folder": "product"})
    created = issues.create_issue(cfg, "t", "b", None, 0, *IDENT, assignee="builder")
    assert created["folder"] == "product"


def test_update_move_and_comment_work_on_a_nested_issue(tmp_path):
    cfg = make_project()
    created = make_folder_issue(cfg, "product")
    issues.add_comment(cfg, created["id"], "a note", "pm", "pm")
    current = issues.get_issue(cfg, created["id"])
    issues.update_issue(cfg, created["id"], current["hash"], {"title": "renamed"}, *IDENT)
    assert issues.get_issue(cfg, created["id"])["title"] == "renamed"
    other = make_folder_issue(cfg, "product", title="other")
    issues.move_issue(cfg, created["id"], after=other["id"], identity=IDENT[0], role_for_email=IDENT[1])
    order = [i["id"] for i in issues.list_issues(cfg, state="open")]
    assert order.index(created["id"]) == order.index(other["id"]) + 1, "the move works in a folder too"


def _live_turn(cfg, task, role):
    import os
    assert holders.claim(cfg.project_root, 1, task, os.getpid(), role)


def test_a_scoped_role_may_not_edit_outside_its_folders(tmp_path):
    cfg = make_project({"edit_scopes": {"pm": ["product"]}})
    company = make_folder_issue(cfg, "company", title="mission")
    _live_turn(cfg, "task-scope", "pm")
    current = issues.get_issue(cfg, company["id"])
    with pytest.raises(PermissionError, match="cannot edit issues in company"):
        control._execute(cfg, "task-scope", "update_issue",
                         {"id": company["id"], "hash": current["hash"], "title": "x"})


def test_a_scoped_role_may_edit_its_own_folder(tmp_path):
    cfg = make_project({"edit_scopes": {"pm": ["product"]}})
    product = make_folder_issue(cfg, "product", title="roadmap item")
    _live_turn(cfg, "task-ok", "pm")
    current = issues.get_issue(cfg, product["id"])
    result = control._execute(cfg, "task-ok", "update_issue",
                              {"id": product["id"], "hash": current["hash"], "title": "better"})
    assert result["ok"] is True


def test_a_scoped_role_can_file_an_issue_in_any_folder(tmp_path):
    cfg = make_project({"edit_scopes": {"pm": ["product"]}})
    make_folder_issue(cfg, "company", title="mission")
    _live_turn(cfg, "task-file", "pm")
    created = control._execute(cfg, "task-file", "create_issue",
                               {"title": "please fix the mission", "body": "b", "folder": "company",
                                "assignee": "strategist"})
    assert created["folder"] == "company", "asking for a change is always allowed"


def test_a_scoped_role_may_not_move_an_issue_it_cannot_edit(tmp_path):
    cfg = make_project({"edit_scopes": {"pm": ["product"]}})
    company = make_folder_issue(cfg, "company")
    product = make_folder_issue(cfg, "product")
    _live_turn(cfg, "task-move", "pm")
    with pytest.raises(PermissionError, match="cannot edit"):
        control._execute(cfg, "task-move", "move_issue", {"id": company["id"], "after": product["id"]})


def test_the_coordinator_is_never_scoped(tmp_path):
    cfg = make_project({"edit_scopes": {"pm": ["product"]}})
    company = make_folder_issue(cfg, "company", title="mission")
    current = issues.get_issue(cfg, company["id"])
    result = server.dispatch(cfg, "update_issue", {"id": company["id"], "hash": current["hash"], "title": "ok"}, IDENT)
    assert result["ok"] is True


def test_a_scoped_role_is_told_where_it_may_edit(tmp_path):
    cfg = make_project({"edit_scopes": {"pm": ["product"]}})
    issue = {"id": 7, "title": "t", "base_branch": "main", "lines": [{"text": "body"}],
             "assignment_history": []}
    prompt = dispatch._build_prompt(cfg, "pm", issue, {"dir": str(tmp_path), "url": "/s"})
    assert "You can edit issues in: product" in prompt


def test_an_edit_scope_must_name_declared_folders(tmp_path):
    with pytest.raises(ValueError, match="not declared"):
        make_project({"edit_scopes": {"pm": ["nowhere"]}})


def test_folder_names_are_simple(tmp_path):
    with pytest.raises(ValueError, match="lowercase"):
        make_project({"folders": ["Bad Name"]})
