import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mcp import issues, worktrees  # noqa: E402
from mcp.config import ProjectConfig  # noqa: E402

IDENTITY = ("coordinator", "coordinator")


def make_project_with_real_public_repo(tmp_path: Path) -> ProjectConfig:
    root = tmp_path / "instance"
    (root / "agents").mkdir(parents=True)
    for _role in ("builder", "reviewer", "pm", "researcher"):
        _f = root / "agents" / f"{_role}.md"
        if not _f.exists():
            _f.write_text(f"---\nname: {_role}\n---\n{_role}")
    (root / "project.yml").write_text("public_repo: ../public\n")
    subprocess.run(["git", "init", "-q"], cwd=root)

    public = tmp_path / "public"
    public.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=public)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=public)
    subprocess.run(["git", "config", "user.name", "t"], cwd=public)
    (public / "README.md").write_text("hello\n")
    subprocess.run(["git", "add", "README.md"], cwd=public)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=public)

    return ProjectConfig.load(root)


def make_project_with_empty_public_repo(tmp_path: Path) -> ProjectConfig:
    root = tmp_path / "instance"
    (root / "agents").mkdir(parents=True)
    for _role in ("builder", "reviewer", "pm", "researcher"):
        _f = root / "agents" / f"{_role}.md"
        if not _f.exists():
            _f.write_text(f"---\nname: {_role}\n---\n{_role}")
    (root / "project.yml").write_text("public_repo: ../public\n")
    subprocess.run(["git", "init", "-q"], cwd=root)
    (tmp_path / "public").mkdir()  # not even git-initialized
    return ProjectConfig.load(root)


def test_create_issue_resolves_and_records_base_branch_but_makes_no_worktree(tmp_path):
    """Worktree creation is lazy (dispatch.py's job, on first actual spawn)
    — an issue nobody ever dispatches (a decision issue created already
    closed, say) must never pay for one at all."""
    cfg = make_project_with_real_public_repo(tmp_path)
    created = issues.create_issue(cfg, "title", "body", None, 0, *IDENTITY, assignee="builder")

    assert created["base_branch"] == "main"
    assert not worktrees.path_for(cfg, created["id"]).exists()


def test_worktrees_create_makes_the_branch_and_checkout(tmp_path):
    cfg = make_project_with_real_public_repo(tmp_path)
    created = issues.create_issue(cfg, "title", "body", None, 0, *IDENTITY, assignee="builder")

    path = worktrees.create(cfg, created["id"], created["base_branch"])

    assert path == worktrees.path_for(cfg, created["id"])
    assert path.is_dir()
    assert (path / "README.md").exists()
    branches = subprocess.run(["git", "-C", str(cfg.public_repo), "branch", "--list", "issue/1"],
                               capture_output=True, text=True).stdout
    assert "issue/1" in branches


def test_worktree_creation_is_idempotent(tmp_path):
    cfg = make_project_with_real_public_repo(tmp_path)
    created = issues.create_issue(cfg, "title", "body", None, 0, *IDENTITY, assignee="builder")

    first = worktrees.create(cfg, created["id"], "main")
    second = worktrees.create(cfg, created["id"], "main")  # must not error or recreate
    assert first == second == worktrees.path_for(cfg, created["id"])


def test_stacking_on_another_issues_branch_sees_its_in_progress_commit(tmp_path):
    cfg = make_project_with_real_public_repo(tmp_path)
    parent = issues.create_issue(cfg, "parent", "body", None, 0, *IDENTITY, assignee="builder")

    # simulate the parent's builder having been dispatched and committed something
    parent_wt = worktrees.create(cfg, parent["id"], parent["base_branch"])
    (parent_wt / "feature.txt").write_text("wip\n")
    subprocess.run(["git", "add", "feature.txt"], cwd=parent_wt)
    subprocess.run(["git", "-c", "user.email=t@t.com", "-c", "user.name=t",
                     "commit", "-q", "-m", "wip"], cwd=parent_wt)

    child = issues.create_issue(cfg, "security review", "body", None, 0, *IDENTITY,
                                 assignee="builder", base_branch=f"issue/{parent['id']}")
    child_wt = worktrees.create(cfg, child["id"], child["base_branch"])

    assert child["base_branch"] == f"issue/{parent['id']}"
    assert (child_wt / "feature.txt").exists(), "stacked issue should see the parent's in-progress commit"


def test_worktrees_create_fails_soft_when_public_repo_has_nothing_to_branch_from(tmp_path):
    cfg = make_project_with_empty_public_repo(tmp_path)
    created = issues.create_issue(cfg, "title", "body", None, 0, *IDENTITY, assignee="builder")

    assert created["base_branch"] == "main"  # still recorded, even though no worktree can be made
    assert worktrees.create(cfg, created["id"], created["base_branch"]) is None
    assert not worktrees.path_for(cfg, created["id"]).exists()


def test_worktrees_create_handles_a_missing_base_branch(tmp_path):
    """Real regression: a hand-migrated issue written before base_branch
    existed has it missing from frontmatter entirely (None, not just
    unresolved) — worktrees.create must fall back, not crash a subprocess
    call with a None argument."""
    cfg = make_project_with_real_public_repo(tmp_path)
    created = issues.create_issue(cfg, "title", "body", None, 0, *IDENTITY, assignee="builder")

    path = worktrees.create(cfg, created["id"], None)

    assert path == worktrees.path_for(cfg, created["id"])
    assert path.is_dir()


def _commit_in(path: Path, name: str) -> None:
    (path / name).write_text("work")
    subprocess.run(["git", "add", name], cwd=path, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", name],
                   cwd=path, check=True)


def test_a_reopened_issue_gets_its_worktree_back_with_its_commits(tmp_path):
    cfg = make_project_with_real_public_repo(tmp_path)
    created = issues.create_issue(cfg, "title", "body", None, 0, *IDENTITY, assignee="builder")
    path = worktrees.create(cfg, created["id"], created["base_branch"])
    _commit_in(path, "work.txt")
    worktrees.remove(cfg, created["id"])
    assert not path.exists()

    again = worktrees.create(cfg, created["id"], created["base_branch"])

    assert again == path and (path / "work.txt").exists(), "the branch's commits come back"
    assert subprocess.run(["git", "-C", str(path), "rev-list", "--count", "main..HEAD"],
                          capture_output=True, text=True).stdout.strip() == "1"


def test_a_worktree_left_registered_after_its_directory_vanished_is_recreated(tmp_path):
    cfg = make_project_with_real_public_repo(tmp_path)
    created = issues.create_issue(cfg, "title", "body", None, 0, *IDENTITY, assignee="builder")
    path = worktrees.create(cfg, created["id"], created["base_branch"])
    subprocess.run(["rm", "-rf", str(path)], check=True)  # simulate a crash mid-cleanup

    assert worktrees.create(cfg, created["id"], created["base_branch"]) == path
    assert path.is_dir()


def _issue_with_worktree(cfg, state="open", held=False):
    created = issues.create_issue(cfg, "title", "body", None, 0, *IDENTITY, assignee="builder")
    worktrees.create(cfg, created["id"], created["base_branch"])
    if state != "open":
        current = issues.get_issue(cfg, created["id"])
        issues.update_issue(cfg, created["id"], current["hash"], {"state": state}, *IDENTITY)
    return created["id"]


def test_sweep_removes_only_the_worktrees_of_finished_issues(tmp_path):
    cfg = make_project_with_real_public_repo(tmp_path)
    done = _issue_with_worktree(cfg, "done")
    merged = _issue_with_worktree(cfg, "merged")
    canceled = _issue_with_worktree(cfg, "canceled")
    open_ = _issue_with_worktree(cfg)
    blocked = _issue_with_worktree(cfg, "blocked")

    removed = worktrees.sweep(cfg)

    assert sorted(removed) == sorted([done, merged, canceled])
    assert not worktrees.path_for(cfg, done).exists()
    assert worktrees.path_for(cfg, open_).exists()
    assert worktrees.path_for(cfg, blocked).exists(), "blocked may still be reopened"


def test_sweep_keeps_the_branch_so_the_work_can_come_back(tmp_path):
    cfg = make_project_with_real_public_repo(tmp_path)
    done = _issue_with_worktree(cfg, "done")
    _commit_in(worktrees.path_for(cfg, done), "keep.txt")
    worktrees.sweep(cfg)
    branches = subprocess.run(["git", "-C", str(cfg.public_repo), "branch", "--list", f"issue/{done}"],
                              capture_output=True, text=True).stdout
    assert f"issue/{done}" in branches


def test_sweep_never_removes_a_worktree_with_uncommitted_work(tmp_path):
    cfg = make_project_with_real_public_repo(tmp_path)
    done = _issue_with_worktree(cfg, "done")
    (worktrees.path_for(cfg, done) / "unsaved.txt").write_text("not committed")
    removed = worktrees.sweep(cfg)
    assert removed == []
    assert (worktrees.path_for(cfg, done) / "unsaved.txt").exists()


def test_sweep_respects_its_limit_per_call(tmp_path):
    cfg = make_project_with_real_public_repo(tmp_path)
    for _ in range(3):
        _issue_with_worktree(cfg, "canceled")
    assert len(worktrees.sweep(cfg, limit=2)) == 2
    assert len(worktrees.sweep(cfg, limit=2)) == 1


def test_sweep_removes_a_finished_issues_worktree_whose_file_is_gone(tmp_path):
    cfg = make_project_with_real_public_repo(tmp_path)
    gone = _issue_with_worktree(cfg)
    (cfg.issues_dir() / f"{gone}.md").unlink()
    assert worktrees.sweep(cfg) == [gone]


def test_a_held_worktree_is_not_removed(tmp_path):
    from mcp import holders
    cfg = make_project_with_real_public_repo(tmp_path)
    done = _issue_with_worktree(cfg, "done")
    holders.claim(cfg.project_root, done, "task-x", __import__("os").getpid(), "builder")
    assert worktrees.sweep(cfg) == []


DAY = 86400


def test_untracked_files_alone_are_removed_after_ten_days(tmp_path):
    import time
    cfg = make_project_with_real_public_repo(tmp_path)
    done = _issue_with_worktree(cfg, "done")
    path = worktrees.path_for(cfg, done)
    (path / "shared-shots").mkdir()
    (path / "shared-shots" / "shot.png").write_bytes(b"\x89PNG")

    assert worktrees.sweep(cfg, now=time.time() + 9 * DAY) == [], "still inside the grace period"
    assert path.exists()

    assert worktrees.sweep(cfg, now=time.time() + 11 * DAY) == [done]
    assert not path.exists()
    assert subprocess.run(["git", "-C", str(cfg.public_repo), "branch", "--list", f"issue/{done}"],
                          capture_output=True, text=True).stdout.strip(), "the branch survives"


def test_a_modified_tracked_file_is_kept_however_old(tmp_path):
    import time
    cfg = make_project_with_real_public_repo(tmp_path)
    done = _issue_with_worktree(cfg, "done")
    path = worktrees.path_for(cfg, done)
    (path / "README.md").write_text("changed, not committed")
    assert worktrees.sweep(cfg, now=time.time() + 365 * DAY) == []
    assert (path / "README.md").read_text() == "changed, not committed"


def test_a_clean_finished_worktree_goes_at_once(tmp_path):
    cfg = make_project_with_real_public_repo(tmp_path)
    done = _issue_with_worktree(cfg, "done")
    assert worktrees.sweep(cfg) == [done]



def test_the_grace_counts_from_the_artifacts_not_from_a_later_commit_on_the_issue(tmp_path):
    # Moving or editing an issue commits its file again. That must not restart the
    # clock for artifacts written long before: the grace is about the artifacts.
    import os
    import time
    cfg = make_project_with_real_public_repo(tmp_path)
    done = _issue_with_worktree(cfg, "done")
    path = worktrees.path_for(cfg, done)
    shot = path / "shared-shots" / "shot.png"
    shot.parent.mkdir()
    shot.write_bytes(b"\x89PNG")
    old = time.time() - 30 * DAY
    os.utime(shot, (old, old))
    assert worktrees.sweep(cfg) == [done], "artifacts untouched for 30 days are past the grace"
    assert not path.exists()
