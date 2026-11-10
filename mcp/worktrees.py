"""Per-issue git worktrees against the public repo.

Every issue gets one, unconditionally — not just ones a "code-touching"
role will work on. Classifying roles that way generically isn't worth the
complexity it'd add: an issue nobody ever commits against just leaves an
empty branch, and "merge or not" already depends on whether there are
commits, not on what kind of role was assigned.

Created once, eagerly, at issue creation (see issues.create_issue) — every
subsequent spawn against that issue (builder, reviewer, builder again)
reuses the same worktree, so one attempt's work is visible to the next
rather than discarded. The framework owns this entirely; a spawned agent
never sees worktree plumbing, just the `cwd` it's given, same as always.

Skipped, not an error, if the public repo has nothing to branch from yet
(no commits) — `create_issue` still succeeds; dispatch.py falls back to
the shared repo as cwd for that issue, same as before this existed.

A finished issue's worktree (done, merged, canceled, or its file is gone) is
removed by `sweep`, a few per tick, and never while the issue is held. The
branch stays, so reopening the issue brings its commits back.

Uncommitted work is protected in two steps. A changed tracked file is never
removed. Files that are only untracked (screenshots, logs a turn left behind)
are removed once the issue has been unchanged for ARTIFACT_GRACE_DAYS, so a
person can still look at them after the work is finished.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from . import gitutil, retry
from .config import ProjectConfig

ARTIFACT_GRACE_DAYS = 10


def path_for(cfg: ProjectConfig, issue_id: int) -> Path:
    return cfg.project_root / ".agents-daemon" / "worktrees" / str(issue_id)


def branch_for(issue_id: int) -> str:
    return f"issue/{issue_id}"


def create(cfg: ProjectConfig, issue_id: int, base_branch: str | None) -> Path | None:
    """Idempotent: an existing worktree for this issue is left alone and
    its path is just returned.

    `base_branch` can be `None` — an issue written before this field
    existed (a hand-migrated record, say) has it missing from its
    frontmatter entirely, not just unresolved. Falling back here, the same
    way `create_issue` resolves it when it's never given one, means a
    caller can pass the field straight through without checking first;
    the alternative is a `None` silently reaching a subprocess call and
    crashing with a confusing TypeError instead of a clear fallback."""
    path = path_for(cfg, issue_id)
    if path.exists():
        return path
    base_branch = base_branch or gitutil.current_branch(cfg.public_repo) or "main"
    if gitutil.run(cfg.public_repo, "rev-parse", "--verify", base_branch).returncode != 0:
        return None  # nothing to branch from yet (e.g. a repo with no commits)
    path.parent.mkdir(parents=True, exist_ok=True)
    branch = branch_for(issue_id)
    # A reopened issue's branch still holds its commits: check it out, don't
    # recreate it (which would fail, or reset it to the base).
    exists = gitutil.run(cfg.public_repo, "rev-parse", "--verify", "-q",
                         f"refs/heads/{branch}").returncode == 0

    def _add() -> None:
        gitutil.run(cfg.public_repo, "worktree", "prune")  # a registration whose directory is gone
        args = ["worktree", "add", str(path), branch] if exists \
            else ["worktree", "add", str(path), "-b", branch, base_branch]
        result = gitutil.run(cfg.public_repo, *args)
        if result.returncode != 0:
            raise gitutil.GitError(f"git worktree add failed: {result.stderr.strip()}")

    # Raises rather than returning None on failure: the caller would otherwise
    # fall back to running the agent in the main checkout, with write access
    # to main, because of a transient git error.
    retry.retry(_add, retry_on=(gitutil.GitError,), label=f"worktree for #{issue_id}")
    return path


def _status(path: Path) -> list[str]:
    result = subprocess.run(["git", "-C", str(path), "status", "--porcelain"],
                            capture_output=True, text=True)
    return [line for line in result.stdout.splitlines() if line.strip()]


def _artifacts_changed(path: Path) -> float | None:
    """When the untracked files in a worktree were last written, newest first.
    Their age, not the issue's commit history, decides when they may go: an
    issue file re-committed (moved, edited) must not restart the clock."""
    newest = None
    for line in _status(path):
        if not line.startswith("??"):
            continue
        target = path / line[3:].strip()
        files = [target] if target.is_file() else [
            Path(root) / name for root, _, names in os.walk(target) for name in names]
        for file in files:
            try:
                mtime = file.stat().st_mtime
            except OSError:
                continue
            newest = mtime if newest is None else max(newest, mtime)
    return newest


def remove(cfg: ProjectConfig, issue_id: int, force: bool = False) -> bool:
    """Removes an issue's worktree directory, keeping its branch. Without
    `force`, git refuses when there are any changes, which is the default."""
    path = path_for(cfg, issue_id)
    if not path.exists():
        return True
    args = ["worktree", "remove"] + (["--force"] if force else []) + [str(path)]
    result = gitutil.run(cfg.public_repo, *args)
    if result.returncode != 0:
        import sys
        print(f"[worktrees] kept {path}: {result.stderr.strip() or 'git refused to remove it'}",
              file=sys.stderr, flush=True)
        return False
    return True


FINISHED = ("done", "merged", "canceled")


def _removable(cfg: ProjectConfig, issue_id: int, path: Path, now: float) -> bool:
    lines = _status(path)
    if any(not line.startswith("??") for line in lines):
        return False  # changed tracked files: never removed, however old
    if lines:
        changed = _artifacts_changed(path)
        if changed is None or now - changed < ARTIFACT_GRACE_DAYS * 86400:
            return False  # only untracked files, but too recent to discard
        return remove(cfg, issue_id, force=True)
    return remove(cfg, issue_id)


def sweep(cfg: ProjectConfig, limit: int = 5, now: float | None = None) -> list[int]:
    """Removes the worktrees of finished issues, at most `limit` per call so a
    large backlog never stalls a tick. Returns the issue ids removed. `now` is
    for tests; the daemon uses the clock."""
    from . import holders, issues  # local: issues imports this module
    now = time.time() if now is None else now
    root = cfg.project_root / ".agents-daemon" / "worktrees"
    if not root.exists():
        return []
    removed: list[int] = []
    for path in sorted(root.iterdir(), key=lambda p: p.name):
        if len(removed) >= limit:
            break
        if not path.name.isdigit() or not path.is_dir():
            continue
        issue_id = int(path.name)
        try:
            finished = issues.get_issue(cfg, issue_id)["state"] in FINISHED
        except FileNotFoundError:
            finished = True
        if not finished or holders.is_held(cfg.project_root, issue_id):
            continue
        if _removable(cfg, issue_id, path, now):
            removed.append(issue_id)
    gitutil.run(cfg.public_repo, "worktree", "prune")
    return removed
