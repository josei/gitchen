"""Thin wrapper for the git-as-database writes the MCP makes to an instance repo.

v1: single coordinator process, no concurrent writers. Pull once at server
startup (see server.py); every mutating call commits locally only — never
pushes. Pushing is a deliberate, reviewed act (the `publish` skill), not
something an issue comment should ever do on its own.
No conflict resolution here — that's explicit future work.
"""

from __future__ import annotations

import fcntl
import subprocess
from contextlib import contextmanager
from pathlib import Path

from . import retry


class GitError(RuntimeError):
    pass


def run(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def pull(repo: Path) -> None:
    if not run(repo, "remote").stdout.strip():
        return  # no remote configured yet — nothing to pull
    run(repo, "pull", "--ff-only")


def current_branch(repo: Path) -> str | None:
    """None if `repo` isn't a git repo yet, or has no commits to be on a
    branch from — callers (see worktrees.py) treat that as "nothing to
    branch off of yet", not an error."""
    result = run(repo, "rev-parse", "--abbrev-ref", "HEAD")
    return result.stdout.strip() if result.returncode == 0 else None


def commit(repo: Path, paths: list[str], message: str, author: str | None = None) -> None:
    """`author` ("Name <email>"), when given, is the acting agent's verified
    identity (role, + task id when known — see server.py), never something
    the caller supplies as a tool argument. One identity, used both as the
    commit author and as the attribution text inside the content itself —
    no separate bookkeeping to keep in sync.

    Retried on failure: the usual cause is a stale or briefly-held
    `index.lock`, and a failed commit writes nothing, so re-running it is safe."""
    retry.retry(lambda: _commit_once(repo, paths, message, author),
                retry_on=(GitError,), label=f"git commit in {repo.name}")


def _commit_once(repo: Path, paths: list[str], message: str, author: str | None) -> None:
    # Under one lock per repository, and naming the paths on commit: a plain
    # commit takes whatever is staged, which another writer may have staged, so
    # one commit could carry several issues under one author.
    with _repo_lock(repo):
        run(repo, "add", "--", *paths)
        status = run(repo, "diff", "--cached", "--quiet", "--", *paths)
        if status.returncode == 0:
            return  # nothing staged for these paths, nothing to commit
        args = ["commit", "-q", "-m", message]
        if author:
            args += ["--author", author]
        result = run(repo, *args, "--only", "--", *paths)
        if result.returncode != 0:
            raise GitError(f"git commit failed: {result.stderr.strip()}")


@contextmanager
def _repo_lock(repo: Path):
    """Serializes adds and commits on one repository across threads and processes."""
    common = run(repo, "rev-parse", "--git-common-dir").stdout.strip() or ".git"
    path = (Path(repo) / common).resolve() / "gitchen-commit.lock"
    with open(path, "a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
