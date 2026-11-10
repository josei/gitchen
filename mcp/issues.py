"""Issues: one addressable, mutable record per id, at <project_root>/issues/
— not inside any folder of role-owned files on purpose, since labels+priority let a roadmap (or a
mission, or a decision log) be a *query* over issues rather than a growing
flat file someone has to hand-edit. Format: YAML frontmatter (id, title,
state, labels, priority, assignee, assignment_history) + a markdown body
with a `## Comments` section.

`state` is a small, fixed framework vocabulary, not a project-extensible one:
`open` (eligible for dispatch), `blocked` (paused, needs a human or privileged
look before it can continue), `done` (accepted, with no commits left unmerged),
`merged` (accepted, with its commits fast-forwarded into the base branch), and
`canceled` (closed unsuccessfully). Only `open` is dispatched. `done` is refused
while the issue's branch has commits its base lacks; `merged` is what merges
them, and is refused when there is nothing to merge. Anything more specific
(why it was canceled, what kind of decision this is) is a label.

Ownership model: `assignee` is the current responsible role; it's never
null — `create_issue` defaults it to the creator. `assignment_history` is
an append-only log of every push (`assign`, a delegation) and pop
(`return`, a hand-back — same word a call uses for going back to its
caller), each tagged with its type — never truncated, so the full
chronology of who delegated to whom survives directly in the file, not
just reconstructable from git log. Returning always goes to exactly
whoever did the pushing (pure history, not a role hierarchy): the entry
that made the current role the assignee already recorded who held it right
before, so this just reads that back.

Who may push/pop at all, and the runtime mutex over which live instance may
currently act on a given issue, are *not* this module's concern — that's
`dispatch.py` (permission from the role's allowed_tools) and
`holders.py` (the ephemeral, non-git liveness lock). This module only ever
trusts its caller to have already checked those; it just mutates the file
safely.

Concurrency: real, because a coordinator connection and a scoped
worker/reviewer connection can legitimately be alive at once against the
same checkout (see server.py's allowed_tools). Two mechanisms:

- Whole-file optimistic locking for `update_issue`: every read returns a
  content hash; every targeted update must supply the hash it read. A
  mismatch means something changed since — the response carries the fresh
  state so the caller doesn't need a second round-trip before retrying.
- Plain pessimistic locking (flock, held only for the single read-modify-
  write) for comments, push/pop, and id assignment — all naturally
  conflict-free operations if just serialized, so forcing a hash check
  there would only produce pointless retries for no benefit.
"""

from __future__ import annotations

import fcntl
import hashlib
import re
import time
from pathlib import Path

from . import cost, frontmatter, gitutil, merge, worktrees
from .config import ProjectConfig

HASH_LEN = 16  # hex chars; 64 bits is effectively collision-proof at this scale


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()[:HASH_LEN]


def _find(cfg: ProjectConfig, issue_id: int) -> Path | None:
    """Where an issue's file is: at the top of issues/, or in one folder under it."""
    top = cfg.issues_dir() / f"{issue_id}.md"
    if top.exists():
        return top
    for path in cfg.issues_dir().glob(f"*/{issue_id}.md"):
        return path
    return None


def _path(cfg: ProjectConfig, issue_id: int) -> Path:
    return _find(cfg, issue_id) or cfg.issues_dir() / f"{issue_id}.md"


def _rel(cfg: ProjectConfig, issue_id: int) -> str:
    """The issue's file relative to the instance repo, for git."""
    return str(_path(cfg, issue_id).relative_to(cfg.project_root))


def _all_paths(cfg: ProjectConfig) -> list[Path]:
    """Every issue file, at the top or in a folder."""
    found = [p for p in cfg.issues_dir().glob("*.md") if p.stem.isdigit()]
    found += [p for p in cfg.issues_dir().glob("*/*.md") if p.stem.isdigit()]
    return sorted(found, key=lambda p: int(p.stem))


def folder_of(cfg: ProjectConfig, issue_id: int) -> str | None:
    """The folder an issue sits in, or None at the top level."""
    path = _find(cfg, issue_id)
    if path is None or path.parent == cfg.issues_dir():
        return None
    return path.parent.name


def _parse(raw: str) -> tuple[dict, str]:
    front, body = frontmatter.parse(raw)
    if not front:
        raise ValueError("issue file is missing YAML frontmatter")
    return front, body


def _replace_description(body: str, description: str) -> str:
    """The body is the description followed by the `## Comments` section. A
    new description replaces only the part before that section; the comments
    (and everything after the marker) are kept as they are.

    A caller that read the issue and passed the whole body back — comments
    included — must not duplicate the timeline: anything from `## Comments`
    on in the *new* text is dropped before the existing tail is appended.
    Found on a real pm turn, which rewrote a description three times and
    tripled the comments section."""
    marker = description.find("\n## Comments")
    if marker != -1:
        description = description[:marker]
    marker = body.find("\n## Comments")
    tail = body[marker + 1:] if marker != -1 else "## Comments\n"
    return description.rstrip() + "\n\n" + tail


def _serialize(front: dict, body: str) -> str:
    return frontmatter.serialize(front, body)


def _as_view(issue_id: int, raw: str) -> dict:
    front, body = _parse(raw)
    h = _hash(raw)
    lines = [{"n": i + 1, "hash": h, "text": line} for i, line in enumerate(body.splitlines())]
    return {"id": issue_id, "title": front.get("title", ""), "state": front.get("state", "open"),
            "labels": front.get("labels", []), "priority": front.get("priority", 0),
            "assignee": front.get("assignee"), "assignment_history": front.get("assignment_history", []),
            "base_branch": front.get("base_branch"),
            "turns": front.get("turns", []), "cost_usd": cost.total_usd(front.get("turns", [])),
            "blocked_by": front.get("blocked_by", []),
            "hash": h, "lines": lines}


def _require_role(cfg: ProjectConfig, role: str, assigning: bool = True) -> None:
    """An open issue is work for a role the daemon can spawn. The interactive
    coordinator session is not an agent, so it can never hold an issue; there
    is no "unassigned" or "human" assignee either."""
    if cfg.is_role(role):
        return
    known = ", ".join(cfg.roles()) or "none defined"
    if assigning:
        raise ValueError(f"cannot assign to {role!r}: it is not a role (no agents/{role}.md). "
                         f"Roles: {known}.")
    raise ValueError(f"an open issue needs an assignee that is a role, and {role!r} is not one "
                     f"(it is not an agent). Pass assignee=<role>. Roles: {known}.")


def _git_author(identity: str, role_for_email: str) -> str:
    return f"{identity} <{role_for_email}@gitchen.local>"


class _FileLock:
    """Held only for one read-modify-write, never across an agent's think
    time — that's what keeps this 'optimistic' in spirit even where we do
    take a lock: the lock protects a single atomic step, not a conversation."""

    def __init__(self, path: Path):
        self._path = path
        self._fh = None

    def __enter__(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self._path, "a+")
        fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        self._fh.close()


def _counter_lock(cfg: ProjectConfig) -> _FileLock:
    return _FileLock(cfg.issues_dir() / ".counter.lock")


def _issue_lock(cfg: ProjectConfig, issue_id: int) -> _FileLock:
    return _FileLock(cfg.issues_dir() / f".{issue_id}.lock")


def _check_folder(cfg: ProjectConfig, folder: str | None) -> str | None:
    """A folder must be declared in project.yml. None means the top level."""
    if folder is None:
        return None
    declared = set(cfg.folders or []) | ({cfg.default_folder} if cfg.default_folder else set())
    if folder not in declared:
        raise ValueError(f"folder {folder!r} is not declared in project.yml (folders: "
                         f"{', '.join(cfg.folders or []) or 'none'})")
    return folder


def create_issue(cfg: ProjectConfig, title: str, body: str, labels: list[str] | None,
                  priority: int, identity: str, role_for_email: str,
                  assignee: str | None = None, base_branch: str | None = None,
                  state: str = "open", blocked_by: list[int] | None = None,
                  folder: str | None = None) -> dict:
    """`assignee` defaults to the creator's own role — there is no
    "unassigned" state. If delegating immediately on creation, pass the
    target role; that's recorded as a push from the creator, same as any
    other delegation.

    `base_branch` (default: whatever branch the public repo is currently
    on — usually `main`) is resolved and recorded once, here, and is
    never patchable afterward (not one of update_issue's fields). Pass
    another issue's own branch (`issue/<id>`) to stack this one on top of
    in-progress, not-yet-merged work instead — the same thing a human
    does picking a PR's base branch, not a parent/child link the
    framework tracks itself.

    The worktree itself is *not* created here — only lazily, on this
    issue's first actual dispatch (see dispatch.py). A decision issue
    created already `done`, or a roadmap issue marked `canceled` before
    anyone's ever assigned to work it, never gets dispatched at all, so
    it never pays for one — no "is this issue going to need code" check
    required, it falls out of just deferring the creation. `state`
    defaults to `open`; pass `done` for e.g. a decision record that's
    final the moment it's written, with no second call needed to close
    it right after."""
    owner = assignee or role_for_email
    folder = folder if folder is not None else cfg.default_folder
    if folder is None and cfg.folders:
        raise ValueError(
            "this project declares folders and sets no default_folder; pass `folder` "
            f"(one of: {', '.join(cfg.folders)})")
    folder = _check_folder(cfg, folder)
    if blocked_by:
        # Starts waiting: it is blocked until every issue named has closed. The
        # same checks as block_on, against an issue that doesn't exist yet.
        state = "blocked"
        _check_waits(cfg, None, sorted(set(int(i) for i in blocked_by)))
    if state == "open":
        _require_role(cfg, owner, assigning=assignee is not None)
    with _counter_lock(cfg):
        existing = [int(p.stem) for p in _all_paths(cfg)]
        issue_id = max(existing, default=0) + 1
        resolved_base = base_branch or gitutil.current_branch(cfg.public_repo) or "main"
        history = [{"type": "assign", "role": owner, "by": role_for_email, "at": _now()}]
        front = {"id": issue_id, "title": title, "state": state,
                  "labels": labels or [], "priority": priority,
                  "assignee": owner, "assignment_history": history, "base_branch": resolved_base}
        if blocked_by:
            front["blocked_by"] = sorted(set(int(i) for i in blocked_by))
        raw = _serialize(front, body.rstrip() + "\n\n## Comments\n")
        target = cfg.issues_dir() / folder / f"{issue_id}.md" if folder else cfg.issues_dir() / f"{issue_id}.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(raw)
        gitutil.commit(cfg.project_root, [str(target.relative_to(cfg.project_root))], f"Issue #{issue_id}: {title}",
                        author=_git_author(identity, role_for_email))
    return {**_as_view(issue_id, raw), "folder": folder}


_COMMENT_LINE = re.compile(r"^(?:### |- )\d{4}-\d{2}-\d{2}T")


def _count_comment_lines(body: str) -> int:
    return sum(1 for line in body.splitlines() if _COMMENT_LINE.match(line))


def comment_count(issue: dict) -> int:
    """How many comments the issue has. The circuit breaker uses this: a
    turn always posts exactly one comment, so an unusually high count means
    an unusually high number of turns, without keeping a separate history."""
    return sum(1 for line in issue["lines"] if _COMMENT_LINE.match(line["text"]))


def turns_since_reopen(issue: dict) -> int:
    """Comments since the last person reopened the issue. The breaker counts
    these, so a reopen gives the issue a fresh allowance."""
    reopened = [e for e in issue.get("assignment_history", []) if e.get("type") == "reopen"]
    base = reopened[-1]["comments"] if reopened else 0
    return comment_count(issue) - base


def get_issue(cfg: ProjectConfig, issue_id: int) -> dict:
    return {**_as_view(issue_id, _path(cfg, issue_id).read_text()), "folder": folder_of(cfg, issue_id)}


STATES = ("open", "blocked", "done", "merged", "canceled")


def _unmerged_commits(cfg: ProjectConfig, issue_id: int, base: str) -> int:
    branch = worktrees.branch_for(issue_id)
    if gitutil.run(cfg.public_repo, "rev-parse", "--verify", "-q", branch).returncode != 0:
        return 0  # no branch: an issue that produced no code has nothing to merge
    ahead = gitutil.run(cfg.public_repo, "rev-list", "--count", f"{base}..{branch}")
    return int(ahead.stdout.strip() or 0)


def update_issue(cfg: ProjectConfig, issue_id: int, hash_: str, patch: dict,
                  identity: str, role_for_email: str) -> dict:
    """The privileged path: title/state/labels/priority/body. Deliberately
    excludes `assignee`/`assignment_history` — those only ever move via
    `push_assignee`/`return_assignee`, never an arbitrary patch, so no role
    can forge its own (or anyone else's) handoff through this call.

    `done` is refused while the issue's own branch holds commits its base
    branch does not: work that was reviewed but never merged is not done.
    An issue that produced no code has no branch and closes as usual."""
    with _issue_lock(cfg, issue_id):
        path = _path(cfg, issue_id)
        raw = path.read_text()
        current_hash = _hash(raw)
        if current_hash != hash_:
            return {"ok": False, "error": "stale hash", "current": _as_view(issue_id, raw)}

        front, body = _parse(raw)
        base = front.get("base_branch") or "main"
        state = patch.get("state")
        if state not in (None, *STATES):
            raise ValueError(f"unknown state {state!r}; expected one of {', '.join(STATES)}")
        if state == "done":
            unmerged = _unmerged_commits(cfg, issue_id, base)
            if unmerged:
                raise ValueError(
                    f"issue #{issue_id} has {unmerged} commit(s) on {worktrees.branch_for(issue_id)} "
                    f"that {base} does not have; mark it merged to merge them, or canceled")
        if state == "merged":
            if gitutil.run(cfg.public_repo, "rev-parse", "--verify", "-q",
                           worktrees.branch_for(issue_id)).returncode != 0:
                raise ValueError(f"issue #{issue_id} has no branch, so nothing to merge; mark it done instead")
            merge.fast_forward(cfg, issue_id, base, patch.get("title") or front.get("title", ""))
        previous_state = front.get("state")
        for key in ("title", "state", "labels", "priority", "base_branch"):
            if key in patch:
                front[key] = patch[key]
        if "body" in patch:
            body = _replace_description(body, patch["body"])
        if state == "open" and previous_state != "open" and role_for_email != "daemon":
            # A person reopening an issue is the only thing that resets the
            # circuit breaker: it counts comments from this point on.
            history = front.get("assignment_history", [])
            history.append({"type": "reopen", "role": front.get("assignee"), "by": role_for_email,
                            "at": _now(), "comments": _count_comment_lines(body)})
            front["assignment_history"] = history
        new_raw = _serialize(front, body)
        path.write_text(new_raw)
        gitutil.commit(cfg.project_root, [_rel(cfg, issue_id)],
                        f"Issue #{issue_id}: {', '.join(patch.keys())}",
                        author=_git_author(identity, role_for_email))
    return {"ok": True, **_as_view(issue_id, new_raw)}


WAKE_ON = ("done", "merged", "canceled")


def _check_waits(cfg: ProjectConfig, issue_id: int | None, ids: list[int]) -> None:
    """Refuses a wait that can never be met: an issue waiting on itself, an issue
    that does not exist, a wait on issues that have all already finished, or a
    cycle back to the waiter."""
    if issue_id is not None and issue_id in ids:
        raise ValueError(f"an issue cannot wait for itself (#{issue_id})")
    open_ids = []
    for target in ids:
        try:
            state = get_issue(cfg, target)["state"]
        except FileNotFoundError:
            raise ValueError(f"#{target} does not exist, so there is nothing to wait for")
        if state not in WAKE_ON:
            open_ids.append(target)
    if not open_ids:
        raise ValueError("every issue named has already finished, so there is nothing to wait for")
    if issue_id is not None:
        seen, frontier = set(), list(open_ids)
        while frontier:
            current = frontier.pop()
            if current == issue_id:
                raise ValueError(f"waiting on {', '.join('#' + str(i) for i in ids)} would make a cycle back to #{issue_id}")
            if current in seen:
                continue
            seen.add(current)
            try:
                frontier.extend(get_issue(cfg, current).get("blocked_by", []))
            except FileNotFoundError:
                pass


def block_on(cfg: ProjectConfig, issue_id: int, ids: list[int], identity: str,
             role_for_email: str) -> dict:
    """The issue waits until every issue named has closed. Nothing else is
    needed from anyone: `wake_dependents` opens it when the last one closes."""
    ids = sorted(set(int(i) for i in ids))
    if not ids:
        raise ValueError("name at least one issue to wait for")
    _check_waits(cfg, issue_id, ids)
    with _issue_lock(cfg, issue_id):
        path = _path(cfg, issue_id)
        front, body = _parse(path.read_text())
        front["state"] = "blocked"
        front["blocked_by"] = ids
        path.write_text(_serialize(front, body))
        gitutil.commit(cfg.project_root, [_rel(cfg, issue_id)],
                       f"Issue #{issue_id}: blocked by {', '.join('#' + str(i) for i in ids)}",
                       author=_git_author(identity, role_for_email))
    add_event(cfg, issue_id, "waiting",
              f"waiting for {', '.join('#' + str(i) for i in ids)} to close; it opens by itself then",
              identity, role_for_email)
    return {"ok": True, **get_issue(cfg, issue_id)}


def wake_dependents(cfg: ProjectConfig) -> list[int]:
    """Opens every blocked issue whose waits have all closed. Runs each tick, as
    the daemon, so it never counts as a person reopening the issue. A stacked
    child whose parent merged is retargeted onto the parent's own base, since
    the parent's branch no longer exists as a base."""
    woken = []
    for path in _all_paths(cfg):
        issue_id = int(path.stem)
        front, _body = _parse(path.read_text())
        waits = front.get("blocked_by") or []
        if front.get("state") != "blocked" or not waits:
            continue
        targets = {}
        for target in waits:
            try:
                targets[target] = get_issue(cfg, target)
            except FileNotFoundError:
                targets[target] = {"state": "missing", "base_branch": None}
        if any(t["state"] not in WAKE_ON for t in targets.values()):
            continue
        with _issue_lock(cfg, issue_id):
            front, body = _parse(path.read_text())
            if front.get("state") != "blocked" or not front.get("blocked_by"):
                continue
            base = front.get("base_branch")
            retarget = None
            for target, view in targets.items():
                if view["state"] == "merged" and base == worktrees.branch_for(target):
                    retarget = view.get("base_branch") or "main"
                    base = retarget
            front["state"] = "open"
            front.pop("blocked_by", None)
            if retarget:
                front["base_branch"] = retarget
            history = front.get("assignment_history", [])
            reason = ", ".join(f"#{t} {v['state']}" for t, v in targets.items())
            history.append({"type": "event", "kind": "woken", "role": front.get("assignee"),
                            "by": "daemon", "reason": f"{reason}; opened by itself"
                            + (f", base retargeted to {retarget}" if retarget else ""),
                            "at": _now()})
            front["assignment_history"] = history
            path.write_text(_serialize(front, body))
            gitutil.commit(cfg.project_root, [_rel(cfg, issue_id)],
                           f"Issue #{issue_id}: woken", author=_git_author("daemon", "daemon"))
        woken.append(issue_id)
    return woken


def move_issue(cfg: ProjectConfig, issue_id: int, before: int | None = None,
               after: int | None = None, identity: str = "coordinator",
               role_for_email: str = "coordinator") -> dict:
    """Places an open issue just before or just after another open one, so the
    backlog can be reordered without anyone renumbering it by hand.

    Only the moved issue's priority changes, to a value between its new
    neighbours. Only when the two neighbours have the same priority (there is
    no room between them) is the whole open queue renumbered in steps of ten,
    in one commit."""
    if (before is None) == (after is None):
        raise ValueError("give exactly one of before or after")
    anchor = before if before is not None else after
    if anchor == issue_id:
        raise ValueError("an issue cannot be moved relative to itself")
    target = get_issue(cfg, issue_id)
    if target["state"] != "open":
        raise ValueError(f"issue #{issue_id} is {target['state']}, not open, so it has no place in the queue")
    queue = [i for i in list_issues(cfg, state="open") if i["id"] != issue_id]
    ids = [i["id"] for i in queue]
    if anchor not in ids:
        raise ValueError(f"#{anchor} is not an open issue, so there is nothing to place #{issue_id} against")
    pos = ids.index(anchor) + (1 if after is not None else 0)
    below = queue[pos - 1]["priority"] if pos > 0 else None
    above = queue[pos]["priority"] if pos < len(queue) else None
    if below is None and above is None:
        new = target["priority"]
    elif below is None:
        new = above - 10
    elif above is None:
        new = below + 10
    elif above - below > 1e-6:
        new = (below + above) / 2
    else:
        ordered = queue[:pos] + [target] + queue[pos:]
        changes = {}
        for index, item in enumerate(ordered):
            value = (index + 1) * 10
            if item["id"] != issue_id and item["priority"] == value:
                continue
            changes[item["id"]] = value
        _set_priorities(cfg, changes, identity, role_for_email, f"move #{issue_id}")
        return get_issue(cfg, issue_id)
    _set_priorities(cfg, {issue_id: new}, identity, role_for_email, f"move #{issue_id}")
    return get_issue(cfg, issue_id)


def _set_priorities(cfg: ProjectConfig, changes: dict[int, float], identity: str,
                    role_for_email: str, message: str) -> None:
    """Several issues' priorities, written under all their locks, in one commit."""
    from contextlib import ExitStack
    with ExitStack() as stack:
        for issue_id in sorted(changes):
            stack.enter_context(_issue_lock(cfg, issue_id))
        for issue_id, value in changes.items():
            path = _path(cfg, issue_id)
            front, body = _parse(path.read_text())
            front["priority"] = value
            path.write_text(_serialize(front, body))
        paths = [_rel(cfg, issue_id) for issue_id in sorted(changes)]
        gitutil.commit(cfg.project_root, paths, f"Issue #{min(changes)}: {message}",
                       author=_git_author(identity, role_for_email))


def add_comment(cfg: ProjectConfig, issue_id: int, text: str,
                 identity: str, role_for_email: str, turns: list[dict] | None = None) -> dict:
    """Not exposed as its own MCP tool — a spawned turn's output always
    becomes a comment automatically (see dispatch.py); this is the
    mechanics that call uses, kept here because it's still just an issue
    file mutation like any other.

    `turns` are the cost records of the turn that wrote this comment. They go
    into the same file write and commit, so a report and what it cost never
    get out of step."""
    with _issue_lock(cfg, issue_id):
        path = _path(cfg, issue_id)
        raw = path.read_text()
        front, body = _parse(raw)
        if turns:
            front["turns"] = [*front.get("turns", []), *turns]
        ts = _now()
        # A heading per comment, not a markdown bullet: the text is
        # multi-line prose, and a bullet only keeps its first line inside
        # the list item, so the rest rendered as loose paragraphs and the
        # timeline read as noise. `### <when> — <who>` renders one block
        # per turn, in order, with the prose and its NEXT: line intact.
        comment_block = f"### {ts} — {identity}\n\n{text.strip()}\n"
        if "## Comments" in body:
            body = body.rstrip() + "\n\n" + comment_block
        else:
            body = body.rstrip() + "\n\n## Comments\n\n" + comment_block
        new_raw = _serialize(front, body)
        path.write_text(new_raw)
        gitutil.commit(cfg.project_root, [_rel(cfg, issue_id)], f"Issue #{issue_id}: comment",
                        author=_git_author(identity, role_for_email))
    return {"ok": True, **_as_view(issue_id, new_raw)}


def add_event(cfg: ProjectConfig, issue_id: int, kind: str, reason: str,
              identity: str = "daemon", role_for_email: str = "daemon") -> dict:
    """Something the framework did or noticed. The daemon never comments on an
    issue: comments are what roles report. An event goes in the history, where
    the next role's prompt and the view both show it. `return_assignee` only
    reads `assign` entries, so an event never disturbs the stack."""
    with _issue_lock(cfg, issue_id):
        path = _path(cfg, issue_id)
        front, body = _parse(path.read_text())
        history = front.get("assignment_history", [])
        history.append({"type": "event", "kind": kind, "role": front.get("assignee"),
                        "by": role_for_email, "reason": reason[:2000], "at": _now()})
        front["assignment_history"] = history
        new_raw = _serialize(front, body)
        path.write_text(new_raw)
        gitutil.commit(cfg.project_root, [_rel(cfg, issue_id)], f"Issue #{issue_id}: {kind}",
                       author=_git_author(identity, role_for_email))
    return {"ok": True, **_as_view(issue_id, new_raw)}


class NothingToReturnTo(Exception):
    """Raised by return_assignee when the current assignee is the issue's
    original creator (the base of the stack) — there's nowhere above to
    hand back to. Closing/abandoning from there is `update_issue`'s job
    (a privileged, deliberate act), not an implicit pop."""


def push_assignee(cfg: ProjectConfig, issue_id: int, to_role: str,
                   identity: str, role_for_email: str) -> dict:
    """`role_for_email` is the pushing role — only the current holder may
    push (dispatch.py checks this against the runtime holder before
    calling here; this function trusts that already happened)."""
    _require_role(cfg, to_role)
    with _issue_lock(cfg, issue_id):
        path = _path(cfg, issue_id)
        raw = path.read_text()
        front, body = _parse(raw)
        history = front.get("assignment_history", [])
        history.append({"type": "assign", "role": to_role, "by": role_for_email, "at": _now()})
        front["assignee"] = to_role
        front["assignment_history"] = history
        new_raw = _serialize(front, body)
        path.write_text(new_raw)
        gitutil.commit(cfg.project_root, [_rel(cfg, issue_id)],
                        f"Issue #{issue_id}: assigned to {to_role}",
                        author=_git_author(identity, role_for_email))
    return {"ok": True, **_as_view(issue_id, new_raw)}


def return_assignee(cfg: ProjectConfig, issue_id: int, identity: str, role_for_email: str) -> dict:
    """Return: hand back to exactly whoever pushed the current holder —
    pure history, not a role hierarchy, same as a call returning to its
    caller. The entry that made `role_for_email` the assignee already
    recorded who held it right before (its `by`); this just restores that."""
    with _issue_lock(cfg, issue_id):
        path = _path(cfg, issue_id)
        raw = path.read_text()
        front, body = _parse(raw)
        history = front.get("assignment_history", [])
        current_role = front.get("assignee")
        last_assign = next(
            (e for e in reversed(history) if e["type"] == "assign" and e["role"] == current_role), None)
        if last_assign is None or last_assign["by"] == last_assign["role"] \
                or not cfg.is_role(last_assign["by"]):
            # "Above" only counts a role. A human or the coordinator session
            # that filed the issue is not on the stack: they cannot be
            # dispatched, so handing the issue to them would strand it.
            raise NothingToReturnTo(
                f"issue #{issue_id} has no one above {front.get('assignee')} to return to")
        previous_role = last_assign["by"]
        history.append({"type": "return", "role": previous_role, "by": role_for_email, "at": _now()})
        front["assignee"] = previous_role
        front["assignment_history"] = history
        new_raw = _serialize(front, body)
        path.write_text(new_raw)
        gitutil.commit(cfg.project_root, [_rel(cfg, issue_id)],
                        f"Issue #{issue_id}: returned to {previous_role}",
                        author=_git_author(identity, role_for_email))
    return {"ok": True, **_as_view(issue_id, new_raw)}


def return_or_block(cfg: ProjectConfig, issue_id: int, identity: str, role_for_email: str,
                    system: tuple[str, str] = ("daemon", "daemon")) -> dict:
    """Return the issue to whoever delegated it. If nobody above is a role, an
    open issue is blocked for a human, with the reason as an event. It is never
    canceled: canceling is a decision, and a role that may not close issues
    must not be able to do it by returning. An issue already off `open` keeps
    the state it has."""
    try:
        return return_assignee(cfg, issue_id, identity, role_for_email)
    except NothingToReturnTo as exc:
        current = get_issue(cfg, issue_id)
        if current["state"] != "open":
            return {"ok": True, "blocked": False, "error": str(exc)}
        update_issue(cfg, issue_id, current["hash"], {"state": "blocked"}, *system)
        add_event(cfg, issue_id, "nobody_above",
                  f"{role_for_email} returned it, but no role is above it to take it back.",
                  *system)
        return {"ok": True, "blocked": True, "error": str(exc)}


def all_turns(cfg: ProjectConfig) -> list[dict]:
    """Every cost record in every issue, each tagged with its issue id."""
    out = []
    for p in _all_paths(cfg):
        front, _body = _parse(p.read_text())
        out.extend({**t, "issue": int(p.stem)} for t in front.get("turns", []))
    return out


def _updated(front: dict) -> str | None:
    """When the issue last moved: its latest turn, handoff or event. Every
    role comment comes from a turn, so this is also its newest role comment."""
    stamps = [e.get("at") for e in front.get("turns", []) + front.get("assignment_history", [])]
    stamps = [s for s in stamps if s]
    return max(stamps) if stamps else None


def list_issues(cfg: ProjectConfig, state: str | None = None, label: str | None = None,
                 assignee: str | None = None, folder: str | None = None,
                 since: str | None = None) -> list[dict]:
    """`since` (an ISO time, e.g. `2026-01-31` or `2026-01-31T09:00:00Z`) keeps
    only issues whose `updated` is at or after it."""
    out = []
    for p in _all_paths(cfg):
        front, _body = _parse(p.read_text())
        updated = _updated(front)
        if since and (updated is None or updated < since):
            continue
        issue_folder = p.parent.name if p.parent != cfg.issues_dir() else None
        if state and front.get("state") != state:
            continue
        if label and label not in (front.get("labels") or []):
            continue
        if assignee and front.get("assignee") != assignee:
            continue
        if folder and issue_folder != folder:
            continue
        history = front.get("assignment_history", [])
        events = [e for e in history if e.get("type") == "event"]
        out.append({"id": front["id"], "title": front.get("title", ""), "folder": issue_folder,
                     "state": front.get("state", "open"), "labels": front.get("labels", []),
                     "priority": front.get("priority", 0), "assignee": front.get("assignee"),
                     "cost_usd": cost.total_usd(front.get("turns", [])),
                     "blocked_by": front.get("blocked_by", []), "updated": updated,
                     # Started: a role has taken it at least once after it was filed.
                     "started": sum(1 for e in history if e.get("type") in ("assign", "return")) > 1,
                     "reason": events[-1]["reason"] if events and front.get("state") == "blocked" else None})
    out.sort(key=lambda i: (i["priority"], i["id"]))
    return out
