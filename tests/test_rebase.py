"""When the base has moved, the branch is rebased in its worktree and goes back
for review, instead of being refused. A conflict goes back with the files named.
The daemon merges nothing that wasn't checked against the base it lands on."""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mcp import issues, merge, worktrees  # noqa: E402
from tests.test_worktrees import make_project_with_real_public_repo  # noqa: E402

IDENT = ["-c", "user.email=t@t", "-c", "user.name=t"]


def git(cwd, *args):
    return subprocess.run(["git", *IDENT, *args], cwd=cwd, capture_output=True, text=True)


def commit_file(cwd, name, text, message):
    (Path(cwd) / name).write_text(text)
    git(cwd, "add", name)
    git(cwd, "commit", "-qm", message)


def issue_with_commit(cfg, name="work.txt", text="work\n"):
    created = issues.create_issue(cfg, "title", "body", None, 0, "coordinator", "coordinator",
                                  assignee="builder")
    worktree = worktrees.create(cfg, created["id"], created["base_branch"])
    commit_file(worktree, name, text, "the builder's work")
    return created["id"], worktree


def test_a_moved_base_rebases_the_branch_and_sends_it_back_unmerged(tmp_path):
    cfg = make_project_with_real_public_repo(tmp_path)
    issue_id, worktree = issue_with_commit(cfg)
    commit_file(cfg.public_repo, "other.txt", "elsewhere\n", "main moved on")
    branch_before = git(worktree, "rev-parse", "HEAD").stdout.strip()

    with pytest.raises(merge.Rebased, match="Nothing is merged yet"):
        issues.update_issue(cfg, issue_id, issues.get_issue(cfg, issue_id)["hash"],
                            {"state": "merged"}, "reviewer", "reviewer")

    issue = issues.get_issue(cfg, issue_id)
    assert issue["state"] == "open", "nothing was merged"
    assert git(worktree, "rev-parse", "HEAD").stdout.strip() != branch_before, "the branch was rebased"
    assert (worktree / "other.txt").exists(), "the branch now carries main's new work"


def test_after_the_recheck_the_rebased_branch_merges(tmp_path):
    cfg = make_project_with_real_public_repo(tmp_path)
    issue_id, _worktree = issue_with_commit(cfg)
    commit_file(cfg.public_repo, "other.txt", "elsewhere\n", "main moved on")
    with pytest.raises(merge.Rebased):
        issues.update_issue(cfg, issue_id, issues.get_issue(cfg, issue_id)["hash"],
                            {"state": "merged"}, "reviewer", "reviewer")

    merged = issues.update_issue(cfg, issue_id, issues.get_issue(cfg, issue_id)["hash"],
                                 {"state": "merged"}, "reviewer", "reviewer")
    assert merged["state"] == "merged"
    head = git(cfg.public_repo, "rev-parse", "HEAD").stdout.strip()
    branch = git(cfg.public_repo, "rev-parse", f"issue/{issue_id}").stdout.strip()
    assert head == branch, "main now holds exactly the reviewed branch"


def test_a_conflict_is_refused_with_the_files_and_the_branch_is_left_as_it_was(tmp_path):
    cfg = make_project_with_real_public_repo(tmp_path)
    issue_id, worktree = issue_with_commit(cfg, name="README.md", text="the builder's line\n")
    commit_file(cfg.public_repo, "README.md", "main's line\n", "main changed the same file")
    branch_before = git(worktree, "rev-parse", "HEAD").stdout.strip()

    with pytest.raises(merge.Conflict, match="README.md"):
        issues.update_issue(cfg, issue_id, issues.get_issue(cfg, issue_id)["hash"],
                            {"state": "merged"}, "reviewer", "reviewer")

    assert git(worktree, "rev-parse", "HEAD").stdout.strip() == branch_before
    assert git(worktree, "status", "--porcelain").stdout.strip() == "", "the rebase was aborted cleanly"
    assert issues.get_issue(cfg, issue_id)["state"] == "open"


def test_uncommitted_work_is_never_rebased_over(tmp_path):
    cfg = make_project_with_real_public_repo(tmp_path)
    issue_id, worktree = issue_with_commit(cfg)
    commit_file(cfg.public_repo, "other.txt", "elsewhere\n", "main moved on")
    (worktree / "unsaved.txt").write_text("not committed\n")
    (worktree / "README.md").write_text("changed but not committed\n")
    with pytest.raises(ValueError, match="uncommitted"):
        issues.update_issue(cfg, issue_id, issues.get_issue(cfg, issue_id)["hash"],
                            {"state": "merged"}, "reviewer", "reviewer")
    assert (worktree / "README.md").read_text() == "changed but not committed\n"
