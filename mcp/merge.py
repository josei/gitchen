"""Fast-forward merge of an issue branch into its base branch.

Used when an issue is marked `merged`. Only ever a fast-forward: if the base
has moved past the branch, the merge is refused and the work has to be rebased
first, so no merge commit is created here. Runs under a lock because the public
checkout is shared, and the base must be the branch checked out there, with
nothing uncommitted, or the working files would drift from the ref.

With `merge: {squash: true}` in project.yml, the branch lands as one commit the
daemon writes itself: the branch's exact tree, the issue title as the whole
message, and the configured author as author and committer. Nothing a turn put
in its own commits (messages, trailers, identities) reaches the base. Afterwards
the issue branch points at that commit, so it has nothing the base lacks.

A branch rebased here goes back to the turn that asked for the merge, to be
checked again on its new base. Until that turn ends, the base is reserved for
it: no other merge lands there, so the check is never outrun by a base that
keeps moving. The reservation lives in the daemon's memory only, so a restart
can never leave one behind. A merge from another process (a person's own
session) does not see it, and lands like any commit made by hand.
"""

from __future__ import annotations

import fcntl
import os
import re
import subprocess
import threading
from contextlib import contextmanager

from . import gitutil, holders, worktrees
from .config import ProjectConfig


@contextmanager
def _merge_lock(cfg: ProjectConfig):
    path = cfg.project_root / ".agents-daemon" / "merge.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


class Rebased(ValueError):
    """The base had moved, so the branch was rebased onto it, and nothing is merged
    yet. It must be checked again on its new base before it lands."""


class Conflict(ValueError):
    """The base moved and the branch can't be rebased onto it without a conflict.
    The rebase was aborted: the branch is exactly as it was."""


class Reserved(ValueError):
    """Another issue was rebased onto this base and is being checked again; the
    base is kept for it until that check ends. Nothing was done."""

    def __init__(self, base: str, holder: int):
        super().__init__(f"{base} is held for #{holder}, which is being checked again after a rebase; "
                         "nothing was merged. Try again once it has merged.")
        self.base = base
        self.holder = holder


# (public repo, base) -> the issue whose rebased branch is being checked again.
_reservations: dict[tuple[str, str], int] = {}
_released = threading.Condition()


def _key(cfg: ProjectConfig, base: str) -> tuple[str, str]:
    return str(cfg.public_repo.resolve()), base


def _reserve_if_in_turn(cfg: ProjectConfig, issue_id: int, base: str) -> None:
    """Only a turn running in this process can check the branch again and end,
    and so release what it holds. A merge without one reserves nothing."""
    holder = holders.current(cfg.project_root, issue_id)
    if holder is not None and holder.get("pid") == os.getpid():
        with _released:
            _reservations[_key(cfg, base)] = issue_id


def release(cfg: ProjectConfig, issue_id: int) -> None:
    """Frees whatever this issue holds. Called when it merges and when its turn
    ends, however it ends."""
    repo = str(cfg.public_repo.resolve())
    with _released:
        for key in [k for k, v in _reservations.items() if k[0] == repo and v == issue_id]:
            del _reservations[key]
        _released.notify_all()


def wait_for(cfg: ProjectConfig, base: str, issue_id: int) -> None:
    """Returns once nobody but this issue holds the base."""
    with _released:
        _released.wait_for(lambda: _reservations.get(_key(cfg, base)) in (None, issue_id))


def _rebase(cfg: ProjectConfig, issue_id: int, base: str, branch: str) -> None:
    """Rebases the branch in its own worktree, or says why it can't."""
    wt = worktrees.path_for(cfg, issue_id)
    if not wt.exists():
        raise ValueError(f"{branch} does not fast-forward from {base}, and its worktree is gone, "
                         f"so it can't be rebased here; rebase it onto {base} first")
    if gitutil.run(wt, "status", "--porcelain", "--untracked-files=no").stdout.strip():
        raise ValueError(f"{branch} has uncommitted changes in its worktree, so it can't be rebased "
                         f"onto {base}; commit or discard them first")
    if gitutil.run(wt, "rebase", base).returncode != 0:
        files = gitutil.run(wt, "diff", "--name-only", "--diff-filter=U").stdout.split()
        gitutil.run(wt, "rebase", "--abort")
        raise Conflict(f"{branch} conflicts with {base} in {', '.join(files) or 'files git could not name'}. "
                       "Nothing was merged, and the branch is as it was.")
    onto = gitutil.run(wt, "rev-parse", "--short", base).stdout.strip()
    head = gitutil.run(wt, "rev-parse", "--short", "HEAD").stdout.strip()
    _reserve_if_in_turn(cfg, issue_id, base)
    raise Rebased(f"{branch} was rebased onto {base} at {onto} because {base} moved; the branch is now at "
                  f"{head}. Nothing is merged yet: check it again on its new base, and repeat your outcome "
                  "if it is still good.")


_AUTHOR_RE = re.compile(r"^\s*([^<>]+?)\s*<([^<>\s]+@[^<>\s]+)>\s*$")


def parse_author(text: str) -> tuple[str, str]:
    """`Name <email>` as (name, email), or ValueError."""
    m = _AUTHOR_RE.match(text or "")
    if not m:
        raise ValueError(f"an author is written `Name <email>`, not {text!r}")
    return m.group(1), m.group(2)


def _squash_commit(repo, base: str, branch: str, message: str, author: str | None) -> str:
    """One commit on top of `base` holding `branch`'s tree. Written with
    commit-tree, so no hook runs and nothing of the branch's own commits is kept."""
    env = dict(os.environ)
    if author:
        name, email = parse_author(author)
        env.update(GIT_AUTHOR_NAME=name, GIT_AUTHOR_EMAIL=email,
                   GIT_COMMITTER_NAME=name, GIT_COMMITTER_EMAIL=email)
    result = subprocess.run(["git", "-C", str(repo), "commit-tree", f"{branch}^{{tree}}", "-p", base,
                             "-m", message], capture_output=True, text=True, env=env)
    if result.returncode != 0:
        raise ValueError(f"squash failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _point_branch_at(cfg: ProjectConfig, issue_id: int, branch: str, commit: str) -> None:
    """After a squash the branch and the new commit hold the same tree, so moving
    the branch changes no file in its worktree."""
    wt = worktrees.path_for(cfg, issue_id)
    if wt.exists():
        result = gitutil.run(wt, "reset", "-q", "--keep", commit)
    else:
        result = gitutil.run(cfg.public_repo, "update-ref", f"refs/heads/{branch}", commit)
    if result.returncode != 0:
        raise ValueError(f"merged, but {branch} could not be moved to {commit}: {result.stderr.strip()}")


def fast_forward(cfg: ProjectConfig, issue_id: int, base: str, title: str = "") -> str:
    """Returns the new short head of the base branch. A branch the base has moved
    past is rebased and sent back for a check (`Rebased`), or refused with the
    files in conflict (`Conflict`). While another issue's rebased branch is being
    checked, the base is refused (`Reserved`) before anything is looked at."""
    branch = worktrees.branch_for(issue_id)
    repo = cfg.public_repo
    with _merge_lock(cfg):
        with _released:
            holder = _reservations.get(_key(cfg, base))
        if holder not in (None, issue_id):
            raise Reserved(base, holder)
        if gitutil.run(repo, "rev-parse", "--verify", "-q", branch).returncode != 0:
            raise ValueError(f"issue #{issue_id} has no branch {branch}: nothing to merge")
        if gitutil.current_branch(repo) != base:
            raise ValueError(f"the public checkout is not on {base}; refusing to merge")
        if gitutil.run(repo, "status", "--porcelain").stdout.strip():
            raise ValueError("the public checkout has uncommitted changes; refusing to merge")
        if gitutil.run(repo, "merge-base", "--is-ancestor", branch, base).returncode == 0:
            # Nothing of its own: there is nothing to merge, rebase or verify again.
            release(cfg, issue_id)
            return gitutil.run(repo, "rev-parse", "--short", "HEAD").stdout.strip()
        if gitutil.run(repo, "merge-base", "--is-ancestor", base, branch).returncode != 0:
            _rebase(cfg, issue_id, base, branch)
        squash = (cfg.merge or {}).get("squash")
        target = branch
        if squash:
            message = " ".join((title or "").split()) or f"Issue {issue_id}"
            target = _squash_commit(repo, base, branch, message, cfg.merge.get("author"))
        result = gitutil.run(repo, "merge", "--ff-only", target)
        if result.returncode != 0:
            raise ValueError(f"merge failed: {result.stderr.strip()}")
        if squash:
            _point_branch_at(cfg, issue_id, branch, target)
        release(cfg, issue_id)
        return gitutil.run(repo, "rev-parse", "--short", "HEAD").stdout.strip()
