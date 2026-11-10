"""When the base has moved, the branch is rebased in its worktree and goes back
for review, instead of being refused. A conflict goes back with the files named.
The daemon merges nothing that wasn't checked against the base it lands on.
While that check runs, the base is kept for the rebased issue."""

import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mcp import dispatch, holders, issues, merge, worktrees  # noqa: E402
from tests.test_worktrees import make_project_with_real_public_repo  # noqa: E402

IDENT = ["-c", "user.email=t@t", "-c", "user.name=t"]


def git(cwd, *args):
    return subprocess.run(["git", *IDENT, *args], cwd=cwd, capture_output=True, text=True)


def commit_file(cwd, name, text, message):
    (Path(cwd) / name).write_text(text)
    git(cwd, "add", name)
    git(cwd, "commit", "-qm", message)


@pytest.fixture(autouse=True)
def no_reservations():
    merge._reservations.clear()
    yield
    merge._reservations.clear()


def in_turn(cfg, issue_id, pid=None):
    """A turn holds the issue, as a dispatched turn in this daemon does."""
    assert holders.claim(cfg.project_root, issue_id, f"task-{issue_id}", pid or os.getpid(), "reviewer")


def merge_it(cfg, issue_id):
    return issues.update_issue(cfg, issue_id, issues.get_issue(cfg, issue_id)["hash"],
                               {"state": "merged"}, "reviewer", "reviewer")


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


def test_the_rebase_message_names_the_base_commit_and_the_new_branch_head(tmp_path):
    cfg = make_project_with_real_public_repo(tmp_path)
    issue_id, worktree = issue_with_commit(cfg)
    commit_file(cfg.public_repo, "other.txt", "elsewhere\n", "main moved on")
    onto = git(cfg.public_repo, "rev-parse", "--short", "main").stdout.strip()
    with pytest.raises(merge.Rebased) as raised:
        merge_it(cfg, issue_id)
    head = git(worktree, "rev-parse", "--short", "HEAD").stdout.strip()
    assert f"onto main at {onto}" in str(raised.value)
    assert f"now at {head}" in str(raised.value)


def test_a_rebase_in_a_turn_keeps_the_base_for_that_issue_until_it_merges(tmp_path):
    cfg = make_project_with_real_public_repo(tmp_path)
    first, _ = issue_with_commit(cfg, name="first.txt")
    second, second_tree = issue_with_commit(cfg, name="second.txt")
    commit_file(cfg.public_repo, "other.txt", "elsewhere\n", "main moved on")
    in_turn(cfg, first)
    with pytest.raises(merge.Rebased):
        merge_it(cfg, first)
    second_before = git(second_tree, "rev-parse", "HEAD").stdout.strip()

    with pytest.raises(merge.Reserved, match=f"#{first}"):
        merge_it(cfg, second)
    assert git(second_tree, "rev-parse", "HEAD").stdout.strip() == second_before, "nothing was done to it"

    assert merge_it(cfg, first)["state"] == "merged", "the checked branch lands on the base it was checked on"
    with pytest.raises(merge.Rebased):
        merge_it(cfg, second)  # free again; it moved, so it is rebased in its turn


def test_the_reservation_ends_with_the_turn_that_holds_it(tmp_path):
    cfg = make_project_with_real_public_repo(tmp_path)
    first, _ = issue_with_commit(cfg, name="first.txt")
    second, _ = issue_with_commit(cfg, name="second.txt")
    commit_file(cfg.public_repo, "other.txt", "elsewhere\n", "main moved on")
    in_turn(cfg, first)
    with pytest.raises(merge.Rebased):
        merge_it(cfg, first)

    merge.release(cfg, first)  # the turn ended some other way: rejected, crashed
    with pytest.raises(merge.Rebased):
        merge_it(cfg, second)


def test_a_merge_with_no_turn_in_this_process_reserves_nothing(tmp_path):
    """Nothing in this process would ever release it: a person's own session,
    or a turn that belongs to another process."""
    cfg = make_project_with_real_public_repo(tmp_path)
    first, _ = issue_with_commit(cfg, name="first.txt")
    second, _ = issue_with_commit(cfg, name="second.txt")
    third, _ = issue_with_commit(cfg, name="third.txt")
    commit_file(cfg.public_repo, "other.txt", "elsewhere\n", "main moved on")
    with pytest.raises(merge.Rebased):
        merge_it(cfg, first)
    in_turn(cfg, second, pid=os.getppid())
    with pytest.raises(merge.Rebased):
        merge_it(cfg, second)
    with pytest.raises(merge.Rebased):
        merge_it(cfg, third)
    assert not merge._reservations


def test_a_merge_directive_waits_for_a_reserved_base_and_the_agent_hears_nothing(tmp_path):
    cfg = make_project_with_real_public_repo(tmp_path)
    first, _ = issue_with_commit(cfg, name="first.txt")
    commit_file(cfg.public_repo, "other.txt", "elsewhere\n", "main moved on")
    second, _ = issue_with_commit(cfg, name="second.txt")  # already on the new base
    in_turn(cfg, first)
    with pytest.raises(merge.Rebased):
        merge_it(cfg, first)

    outcome = []
    waiter = threading.Thread(target=lambda: outcome.append(
        dispatch._try_close(cfg, second, "merged", "reviewer", "reviewer")))
    waiter.start()
    waiter.join(timeout=1)
    assert waiter.is_alive(), "it waits for the base instead of being refused"
    assert issues.get_issue(cfg, second)["state"] == "open"

    merge.release(cfg, first)
    waiter.join(timeout=10)
    assert outcome == [(None, False)], "no refusal, so nothing to tell the agent"
    assert issues.get_issue(cfg, second)["state"] == "merged"
