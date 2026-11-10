"""The event-driven dispatcher behind the daemon's tick.

An assignment is acted on as soon as it lands: every hop an issue makes —
builder to reviewer, reviewer back to builder — is spawned on the next tick,
because "assigned to a role with no live holder" is true the moment the
previous turn's directive lands.

The queue is paced by spending, not by time. Each tick starts turns for the
free slots (`max_parallel` in project.yml, one by default), and only while the
instance's budget (see budget.py) has room. Each turn holds its own issue, so
the same role may run on several issues at once. A started issue always goes
before one that hasn't started, so work in progress finishes before new work
begins, and then priority decides.

Every turn ends by changing the issue's disposition (it is handed on,
returned, or closed, or blocked when nobody above can take it), so there is
no turn that leaves an issue sitting on a role with nothing to do.

`state` is a small, fixed framework vocabulary (see issues.py):
`open | blocked | done | merged | canceled`. Only `open` issues are ever
auto-dispatched — that's the entire pause mechanism, no separate config
needed. `blocked` is a waypoint, not a resting place: it's mechanically
identical to `done`/`merged`/`canceled` for dispatch purposes (not `open`, so
skipped), but distinct for whoever's triaging — "still needs a decision"
versus "a decision was already made." Moving an issue to `blocked` is
deliberately not just "returned to the requester": the requester might
itself be an automated role (a researcher, a planner), and trusting it
to notice and not just reassign the issue right back is exactly the kind
of thing a structural guarantee shouldn't depend on. Only a deliberate,
privileged `update_issue` setting `state` back to `open` (or on to
`canceled`, if it's not getting resolved) resumes/ends things.

Two mechanical, non-agentic checks run every tick, before any agent is
spawned:

- The circuit breaker (`_too_many_comments`): one turn always posts exactly
  one comment, so an unusually high comment count means an unusually high
  number of turns. Past `MAX_COMMENTS`, dispatch marks the issue `blocked`
  and forces a return with a flagging comment. A structural backstop against
  a runaway loop that doesn't depend on any one role's own in-context
  counting being right. It is internal, not a project setting.

Every spawned prompt includes the issue's own `assignment_history`
verbatim (see `_format_history`), not just its body/comments — so a role
like `pm` can reason about *when* it last left its own sequencing and
came back, structurally, rather than needing a text marker in a comment.

End-of-turn contract, uniform regardless of role: the turn's raw output
always becomes a comment (no agent choice, no separate tool); its last
`NEXT:` line is parsed and performed on the agent's behalf. A role may assign
only with `assign_issue` in its `allowed_tools`, and may close an issue only
with `update_issue` there; both are the same permissions a tool call faces,
and the directive line a role is given only advertises the verbs it can
take. A missing line, or one naming a verb this role may not use, gets a
bounded number of corrective `resume()` nudges before falling back to an
automatic return with a comment flagging the compliance failure — never
silently inert, and never trusting the *content* of a verdict, only a small,
fixed, permission-checked vocabulary of actions.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from . import artifacts, budget, cost, drain, frontmatter, holders, issues, pause, retry, server, worktrees
from .config import ProjectConfig, declared_outcomes
from .harness import get_harness

# A closing verb sets the issue's state and hands it back to whoever delegated
# it, so the state never needs to be a separate field.
# The circuit breaker is internal: past this many comments (one per turn) an
# issue is marked blocked. Not a project setting, so a project can't quietly
# turn it off.
MAX_COMMENTS = 30

VERB_STATES = {"done": "done", "merge": "merged", "cancel": "canceled", "block": "blocked"}


_NEXT_RE = re.compile(r"^\s*NEXT:\s*(return|done|merge|cancel|block|blocked_by\s+(#?\d+(?:[\s,]+#?\d+)*)|assign\s+(\S+))\s*$",
                      re.IGNORECASE | re.MULTILINE)
_SYSTEM_IDENTITY = ("daemon", "daemon")

def _identity_for(role: str, task_id: str) -> tuple[str, str]:
    return f"{role} (task {task_id[:8]})", role


def _sandbox_with_git_grants(sandbox: dict, worktree: Path, shared_dir: Path) -> dict:
    """What git needs inside a worktree, and nothing more. Git reads the whole
    shared `.git` (so other branches can be inspected), but writes only where a
    commit on this issue needs it: the object store, this worktree's own git
    directory, the issue refs and their reflogs, and the artifacts folder.

    Probed under nono, not assumed: with this grant a commit succeeds, while
    setting config, moving `main` and writing a hook all fail. The old grant
    (the whole `.git`, writable) let all three through. Returns a new dict:
    the machine config is shared across every dispatch."""
    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(worktree), *args], capture_output=True,
                              text=True, check=True).stdout.strip()

    worktree_git = Path(git("rev-parse", "--absolute-git-dir"))
    common = (worktree / git("rev-parse", "--git-common-dir")).resolve()  # absolute or relative
    issue_refs = common / "refs" / "heads" / "issue"
    issue_logs = common / "logs" / "refs" / "heads" / "issue"
    issue_refs.mkdir(parents=True, exist_ok=True)
    issue_logs.mkdir(parents=True, exist_ok=True)
    grant = dict(sandbox)
    grant["read"] = [*(sandbox.get("read") or []), str(common)]
    grant["allow"] = [*(sandbox.get("allow") or []), str(worktree_git), str(common / "objects"),
                      str(issue_refs), str(issue_logs), str(shared_dir)]
    return grant


def _port_for(issue_id: int) -> int:
    """The first of ten ports this issue's turns may serve on. Issues are ten
    apart, so two turns running at once never share one."""
    return 20000 + (issue_id * 10) % 40000


def _cwd_and_settings(cfg: ProjectConfig, role: str, task_id: str, issue_id: int) -> tuple[str, dict]:
    settings = cfg.settings_for_role(role)
    if role in cfg.allowed_tools:
        # `tools` rides along so the harness can pre-allow exactly these —
        # a headless `-p` session has no human to answer a permission
        # prompt, so without this every MCP call is silently denied (found
        # via a real dispatch run). Not a wider grant than allowed_tools
        # already gives: the MCP server itself still enforces the same
        # list server-side regardless of what the CLI was told to allow.
        settings["mcp"] = {"role": role, "project_root": str(cfg.project_root), "task_id": task_id,
                            "tools": cfg.allowed_tools[role]}
    shared_dir = cfg.project_root / ".agents-daemon" / "shared" / str(issue_id) / task_id[:8]
    shared_dir.mkdir(parents=True, exist_ok=True)
    settings["shared"] = {"dir": str(shared_dir), "url": f"/shared/{issue_id}/{task_id[:8]}"}
    settings["env"] = {**os.environ, "PORT": str(_port_for(issue_id)),
                       "GITCHEN_TASK_ID": task_id, "GITCHEN_PROJECT_ROOT": str(cfg.project_root)}
    program = cfg.program_of(role)
    if program is not None:
        # A program role runs its script, which is read from the instance repo: the
        # one file of it the sandbox lets the turn read.
        settings["program"] = str(program)
        if settings.get("sandbox"):
            settings["sandbox"] = {**settings["sandbox"],
                                   "read": [*(settings["sandbox"].get("read") or []), str(program)]}
    if cfg.workspace_of(role) == "none":
        # A role with no repository works in an empty folder of its own, which is
        # removed when its turn ends. No worktree, no git grant.
        scratch = cfg.project_root / ".agents-daemon" / "scratch" / task_id
        scratch.mkdir(parents=True, exist_ok=True)
        return str(scratch), settings
    worktree = worktrees.path_for(cfg, issue_id)
    if not worktree.exists():
        # Never fall back to the main checkout: a sandbox grant that lets a
        # role work there would hand it write access to main.
        raise RuntimeError(f"issue #{issue_id} has no worktree to run in (its base branch may not exist yet)")
    if settings.get("sandbox"):
        settings["sandbox"] = _sandbox_with_git_grants(settings["sandbox"], worktree, shared_dir)
    cwd = str(worktree)
    return cwd, settings


class _HarnessLaunchError(RuntimeError):
    """The turn never ran: a usage limit, an outage, a CLI that wouldn't start.
    Retried briefly, then dispatch pauses and the issue is left exactly as it
    was, because this says nothing about the work. A turn that did run and went
    wrong is not this: it may have changed files, so it isn't re-run blindly."""


def _checked_turn(turn):
    if turn.never_ran:
        raise _HarnessLaunchError(turn.text)
    return turn


def _format_history(history: list[dict]) -> str:
    def line(e: dict) -> str:
        if e["type"] == "event":
            return f"- {e['at']}: {e['kind']} ({e['reason']})"
        return f"- {e['at']}: {e['type']} -> {e['role']} (by {e['by']})"
    return "\n".join(line(e) for e in history)


def _workspace_text(cfg: ProjectConfig, role: str) -> str:
    """What the role is told about its workspace: its own worktree, or none.
    No issue id or base branch here: those live in the issue section, so this
    text is identical for every issue the role runs on and stays in the
    prompt's cached prefix."""
    if cfg.workspace_of(role) == "none":
        return ("You have no repository in this turn. Your working directory is an empty folder for "
                "this turn alone, and it is removed when the turn ends. Anything you want kept goes in "
                "your report.")
    return (
        "You are working in this issue's own git worktree: a checkout of the issue branch, created "
        "from its base branch. Your working directory is that worktree, and every relative path is "
        "relative to it. It is the only copy of the repository you can use. Do not look for, read, or "
        "write any other checkout, and do not run git against another repository path. Anything you "
        "commit goes to this branch only; a human or another role decides what reaches the main branch. "
        "Use the `PORT` environment variable for anything you serve, and the nine ports after it: other "
        "turns may be running at the same time, on their own ports. "
        "Other branches can be read, but nothing can be written to them, and `git stash` is unavailable: "
        "commit to this branch instead. Git may print `packed-refs.lock: Operation not permitted` while "
        "committing. That message is expected: this sandbox does not allow that one lock file, and the "
        "commit is still recorded. Don't investigate it or try to work around it. Confirm the commit with "
        "`git log` and carry on.")


_POSTING_RULES = (
    "Your final response is posted on the issue as a comment, rendered as markdown. To show a "
    "picture in it, write the markdown image syntax on a line of its own, with the path of a file "
    "you produced (relative to your working directory, or absolute):"
    "\n\n![dark mode toggle, on](test-results/dark-mode-on.png)\n\n"
    "Only that syntax displays the picture. A path written in backticks, quoted, or just named in a "
    "sentence is shown as text and the reader sees no image, so never do that for a screenshot. "
    "Give every image a caption that says what it shows. For anything that is not a picture (a log, "
    "a report, an exported file) use a link: [run log](out/run.log). When your turn ends the daemon "
    "copies each linked file out of the worktree into this issue's artifacts folder and rewrites "
    "the link to point there, so nothing is committed to either repository; a link to a file the "
    "repository tracks (source, docs) is left as written. You can also write a file straight into "
    "this turn's artifacts folder, named in the issue section, and link it by that absolute path.")


def prompt_parts(cfg: ProjectConfig, role: str, issue: dict | None,
                  shared: dict) -> list[tuple[str | None, str]]:
    """The prompt as (section title, body) pairs, in the order a turn sees
    them. `issue` is None for the standing context a role loads when it is
    idle: the issue and its history are the only parts that need one.

    Order is by how widely a part is shared, because prompt caches are
    prefix-based: shared context, workspace, posting rules and the directive
    rule are the same for every role on the machine (workspace differs only
    for a role with no repository). The role prompt and its scope/roles
    follow, then the issue, which carries the id, branch and artifacts folder
    that would otherwise make the earlier text issue-specific. Only the
    role-specific directive values stay at the end, where the model acts on
    them.

    The role prompt has no title: it is the system prompt itself, and the
    headings around it are the framework's additions."""
    parts: list[tuple[str | None, str]] = []
    context = cfg.shared_context()
    if context:
        parts.append(("Shared context", context))
    parts.append(("Your workspace", _workspace_text(cfg, role)))
    parts.append(("Posting your report", _POSTING_RULES))
    parts.append(("How the turn ends", _DIRECTIVE_RULE))
    _front, role_prompt = frontmatter.parse(cfg.agent_prompt_path(role).read_text())
    parts.append((None, role_prompt.strip()))
    parts += _skills_section(cfg, role)
    parts += _scope_section(cfg, role) + _roles_section(cfg, role)
    if issue:
        base = issue.get("base_branch") or "main"
        branch = (f"Branch `issue/{issue['id']}`, created from `{base}`.\n"
                  if cfg.workspace_of(role) == "worktree" else "")
        body = "\n".join(line["text"] for line in issue["lines"])
        parts.append((f"Issue #{issue['id']}: {issue['title']}",
                      f"{branch}Artifacts folder for this turn: `{shared['dir']}`.\n\n{body}"))
        parts.append(("Assignment history", _format_history(issue["assignment_history"])))
    parts.append((None, _directive_values(cfg, role)))
    return parts


def render_prompt(parts: list[tuple[str | None, str]]) -> str:
    """Join the parts with one blank line between them, exactly as a turn
    receives them. A titled part gets its `##` heading."""
    out: list[str] = []
    for index, (title, body) in enumerate(parts):
        if index:
            out.append("")
        if title is not None:
            out.append(f"## {title}")
        out.append(body)
    return "\n".join(out)


def _build_prompt(cfg: ProjectConfig, role: str, issue: dict, shared: dict) -> str:
    """Real regression: this used to pass agent_prompt_path(role)'s raw
    file content straight through, frontmatter included. Every real role
    file starts with `---\\nname: ...\\n---\\n` — fine as plain text, but
    handed to a CLI as an argument value, a leading `---` reads as an
    option to the argument parser, not literal text, so every spawn
    failed before the model ever saw anything. No test caught it because
    every fixture's agent file was a bare string with no frontmatter at
    all. `frontmatter.parse` strips it the same way issues.py/schedules.py
    already do; a file with no frontmatter (still fine for a quick test
    fixture) round-trips unchanged."""
    return render_prompt(prompt_parts(cfg, role, issue, shared))


def _skills_section(cfg: ProjectConfig, role: str) -> list[tuple[str, str]]:
    """The skills the role lists in its frontmatter, preloaded in full, the way
    Claude Code preloads a subagent's `skills:`. The daemon does it, so it
    works on any harness. Supporting files a skill mentions are in its folder."""
    return [(f"Skill: {skill['name']}",
             (f"{skill['description']}\n\n" if skill["description"] else "")
             + f"Files this skill refers to are in `{skill['dir']}`.\n\n{skill['body']}")
            for skill in cfg.skills_for(role)]


def _scope_section(cfg: ProjectConfig, role: str) -> list[tuple[str, str]]:
    """For a role limited to some folders: which, and how to ask for the rest."""
    scope = (cfg.edit_scopes or {}).get(role)
    if scope is None:
        return []
    names = ", ".join(scope) if scope else "none"
    return [("Where you may edit",
             f"You can edit issues in: {names}. Everything else is read-only for you. To request a "
             "change there, create a new issue in that folder, assigned to a role that owns it.")]


def _roles_section(cfg: ProjectConfig, role: str) -> list[tuple[str, str]]:
    """For a role that can hand work on: the other roles it may assign to, with
    what each one is for, so the choice is made from the prompt, not guessed."""
    if not _may_assign(cfg, role):
        return []
    lines = []
    for other in cfg.roles():
        if other == role:
            continue
        front, _body = frontmatter.parse(cfg.agent_prompt_path(other).read_text())
        lines.append(f"- {other}: {front.get('description', '').strip() or 'no description'}")
    return [("Roles you can hand work to", "\n".join(lines))]


def _may_close(cfg: ProjectConfig, role: str) -> bool:
    """Closing an issue is a privilege of having update_issue in allowed_tools."""
    return "update_issue" in cfg.allowed_tools.get(role, [])


def _may_assign(cfg: ProjectConfig, role: str) -> bool:
    """Assigning is a privilege of having assign_issue in allowed_tools."""
    return "assign_issue" in cfg.allowed_tools.get(role, [])


# The rule is the same for every role, so it lives in the prompt's common
# prefix; only the allowed values at the end vary by role.
_DIRECTIVE_RULE = (
    "End your response with exactly one line. The line must be exactly one of "
    "the directives named at the end of this prompt, and nothing after it. Do "
    "not invent another.")


def _directive_values(cfg: ProjectConfig, role: str) -> str:
    """The role-specific part of the directive: the values it may use. This
    goes last in the prompt, after the issue, so the common rule can stay in
    the cached prefix and the model still sees its actual options at the end."""
    table = (cfg.transitions or {}).get(role)
    if table is not None:
        meanings = declared_outcomes(cfg.agent_prompt_path(role))
        offered = ", ".join(f"'NEXT: {outcome}' ({meanings.get(outcome, '').strip() or outcome})"
                            for outcome in table)
        return f"Allowed directives: {offered}."
    """Only the verbs this role can actually perform are offered. Offering
    an action the role would then be refused for is worse than not
    mentioning it: the turn is already over, so there's no tool error the
    model can react to, and the issue would sit on a role that has nothing
    left to do with it (see `_parse_next`)."""
    actions = ["'NEXT: return' (hand it back to whoever delegated it; if no role did, because "
               "a human filed it, the issue is blocked for a human)"]
    if _may_assign(cfg, role):
        actions.append("'NEXT: assign <role>'")
    text = "Allowed directives: " + ", ".join(actions) + "."
    if _may_close(cfg, role):
        text += (" To close the issue instead, end with 'NEXT: done' (accepted, no code), "
                 "'NEXT: merge' (accepted, merge its commits into the base), 'NEXT: cancel' "
                 "(not accepted), or 'NEXT: block' (needs a human). To wait until other issues "
                 "have closed, end with 'NEXT: blocked_by #<id> #<id>'; the issue opens by itself "
                 "when they all have.")
    return text


def _directive_line(cfg: ProjectConfig, role: str) -> str:
    """The full instruction, for a corrective resume: the common rule plus
    this role's values, in one sentence."""
    return f"{_DIRECTIVE_RULE} {_directive_values(cfg, role)}"


def _parse_next(text: str, may_close: bool, may_assign: bool,
                roles: list[str] | None = None) -> tuple[str, str | None] | None:
    """The turn's last NEXT: line, or None if it names something this role
    may not do. A closing verb needs update_issue and `assign` needs
    assign_issue, the same permission a tool call would face — but a turn
    that ends with one can't get a tool error back, so an unpermitted verb
    is treated as no valid directive at all: the corrective resume gives the
    model a chance to end with one it can actually take, and failing that
    the issue is returned automatically instead of being stranded on a role
    whose only remaining move would be the one it isn't allowed to make."""
    matches = list(_NEXT_RE.finditer(text or ""))
    if not matches:
        return None
    m = matches[-1]
    word = m.group(1).strip().lower()
    if word in VERB_STATES:
        return ("state", VERB_STATES[word]) if may_close else None
    if word == "return":
        return "return", None
    if word.startswith("blocked_by"):
        # Waiting on other issues is a closing decision, like done or cancel.
        if not may_close:
            return None
        return "blocked_by", [int(n) for n in re.findall(r"\d+", m.group(2))]
    if not may_assign:
        return None
    # An assign to something that is not a role is no directive at all: the
    # corrective resume tells the model to pick one that exists.
    if roles is not None and m.group(3) not in roles:
        return None
    return "assign", m.group(3)


def _try_block_on(cfg: ProjectConfig, issue_id: int, ids: list[int], identity: str,
                  role_for_email: str) -> str | None:
    """Starts waiting on other issues. None when it worked, or why it can't be
    (a missing issue, a cycle, every issue named already finished)."""
    try:
        issues.block_on(cfg, issue_id, ids, identity, role_for_email)
    except ValueError as exc:
        return str(exc)
    return None


def _try_close(cfg: ProjectConfig, issue_id: int, state: str, identity: str,
               role_for_email: str) -> str | None:
    """Closing is the update_issue tool, called on the role's behalf, so the
    same permission and the same checks apply as for a tool call. Returns None
    when it worked, or why it didn't (a merge that cannot fast-forward, done
    while work is unmerged). The daemon tells the agent, not the issue.

    A concurrent edit is retried here: it says nothing about the work.

    A closed issue keeps the role that closed it as its assignee. Handing it
    back to the creator was pointless — the issue is closed — and it made
    merged issues read as "assigned to the coordinator", a role that cannot
    even be dispatched (an issue can no longer be assigned to one at all). The assignment history still records who closed it."""
    for _ in range(3):
        current = issues.get_issue(cfg, issue_id)
        try:
            result = server.dispatch(cfg, "update_issue",
                                     {"id": issue_id, "hash": current["hash"], "state": state},
                                     (identity, role_for_email))
        except (ValueError, PermissionError) as exc:
            return str(exc)
        if result.get("ok"):
            return None
    return f"the issue kept changing while setting it {state}"


def _block(cfg: ProjectConfig, issue_id: int, reason: str) -> None:
    """Stops an issue and records why, for the view and for whoever reopens it.
    It keeps its assignee, so reopening retries the same role. An issue that
    already closed (a crash after its turn finished) stays as it is."""
    for _ in range(3):
        current = issues.get_issue(cfg, issue_id)
        if current["state"] != "open":
            break
        if issues.update_issue(cfg, issue_id, current["hash"], {"state": "blocked"},
                               *_SYSTEM_IDENTITY).get("ok"):
            break
    issues.add_event(cfg, issue_id, "blocked", reason)


def _return_safely(cfg: ProjectConfig, issue_id: int, identity: str, role_for_email: str) -> None:
    """Hands the issue back up the assignment stack. When nobody above is a
    role (it was filed by a human or the coordinator session), there is
    nowhere to send it: it is blocked for a human, not stranded on someone
    who can't be dispatched."""
    issues.return_or_block(cfg, issue_id, identity, role_for_email)


def _run_one(cfg: ProjectConfig, role: str, issue_id: int, task_id: str) -> None:
    """Runs one turn. The holder was claimed by the tick that started it, before
    this thread existed, so a slot is taken the moment it is handed out."""
    try:
        try:
            _run_one_turn(cfg, role, issue_id, task_id)
            pause.clear(cfg.project_root)
        except _HarnessLaunchError as exc:
            state = pause.record_failure(cfg.project_root, str(exc)[:2000])
            print(f"[dispatch] issue #{issue_id} {role} never ran; pausing dispatch "
                  f"(failure {state['failures']}): {exc}", file=sys.stderr, flush=True)
        except Exception as exc:
            # An *unexpected* crash (a bug, a bad config, a corrupt file —
            # not a harness/model problem) must not just retry silently
            # forever: it is not a judgment call, and the daemon log is the
            # only place it'd ever show up otherwise. Block the issue and
            # record why, so a human finds out from the issue itself.
            print(f"[dispatch] issue #{issue_id} {role}: {type(exc).__name__}: {exc}",
                  file=sys.stderr, flush=True)
            try:
                _block(cfg, issue_id, f"{type(exc).__name__}: {exc}")
            except Exception:
                pass  # the issue file itself may be unreadable; the original traceback is still in the daemon log
    finally:
        holders.release(cfg.project_root, issue_id, task_id)
        shutil.rmtree(cfg.project_root / ".agents-daemon" / "scratch" / task_id, ignore_errors=True)


COMMENT_LIMIT = 16000


def _without_directive(text: str) -> str:
    """The NEXT: line is the daemon's contract, not prose for a reader: the
    comment is the turn's report, and where the issue went is already in its
    state and assignee (the UI shows both)."""
    lines = text.rstrip().splitlines()
    for index in range(len(lines) - 1, -1, -1):
        if _NEXT_LINE.match(lines[index]):
            del lines[index]
            break
    return "\n".join(lines).strip()


# A screenshot named in backticks (`test-results/x.png`) is the commonest way a
# model fails to embed one; if it names a real untracked image, embed it.
_CODE_IMAGE = re.compile(r"(?<!`)`([^`\s]+\.(?:png|jpe?g|webp|gif))`(?!`)", re.IGNORECASE)
_MD_LINK = re.compile(r"(!?)\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


def _tracked(cwd: str, path: Path) -> bool:
    """Is this file part of the repository? A link to source or docs the repo
    tracks is a reference, not an artifact, and is left as the agent wrote it."""
    try:
        return subprocess.run(["git", "ls-files", "--error-unmatch", str(path)], cwd=cwd,
                              capture_output=True).returncode == 0
    except OSError:
        return False


def _relocate_artifacts(text: str, shared: dict, cwd: str) -> str:
    """A standard markdown image or link is the whole convention: the agent
    points at a file it produced (relative to its worktree, or absolute), and
    the daemon copies it into the issue's artifacts folder and rewrites the
    URL. The issue never holds a binary, the public repo never sees one, and
    several in one report are fine. A file already in the artifacts folder is
    just re-pointed. A remote URL, a missing file, a file type that isn't an
    artifact, an oversized file, or (for a plain link, not an image) a file
    the repo tracks is left exactly as written."""
    shared_dir = Path(shared["dir"])

    def embed_code_path(match: re.Match) -> str:
        src = match.group(1)
        path = Path(src) if os.path.isabs(src) else Path(cwd) / src
        if path.is_file() and not _tracked(cwd, path):
            return f"![{path.stem}]({src})"
        return match.group(0)

    text = _CODE_IMAGE.sub(embed_code_path, text)

    def replace(match: re.Match) -> str:
        bang, label, src = match.groups()
        if src.startswith(("http://", "https://", "data:", "mailto:", "#", shared["url"])):
            return match.group(0)
        path = Path(src) if os.path.isabs(src) else Path(cwd) / src
        try:
            if not path.is_file() or not artifacts.is_artifact(path):
                return match.group(0)
            if path.stat().st_size > artifacts.MAX_BYTES:
                return match.group(0)
            if path.resolve().parent == shared_dir.resolve():
                dest = path
            else:
                if not bang and _tracked(cwd, path):
                    return match.group(0)
                dest = shared_dir / path.name
                counter = 2
                while dest.exists():
                    if dest.read_bytes() == path.read_bytes():
                        break
                    dest = shared_dir / f"{path.stem}-{counter}{path.suffix}"
                    counter += 1
                if not dest.exists():
                    shutil.copyfile(path, dest)
        except OSError:
            return match.group(0)
        return f"{bang}[{label}]({shared['url']}/{dest.name})"
    return _MD_LINK.sub(replace, text)


def _turn_records(role: str, usage: list[dict], started: float) -> list[dict]:
    """The turn's cost as it is written on the issue: one record per model, with
    the turn's wall-clock seconds on the first, so a retro can total time too."""
    at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    seconds = int(time.monotonic() - started)
    return [{"at": at, "role": role, "model": r["model"], "usd": r["usd"], "input": r["input"],
             "output": r["output"], "cache_read": r["cache_read"], "cache_write": r["cache_write"],
             "seconds": seconds if i == 0 else 0} for i, r in enumerate(usage)]


def _publish(cfg: ProjectConfig, issue_id: int, report: str, identity: str,
             role_for_email: str, shared: dict, cwd: str, turns: list[dict] | None = None) -> None:
    """A turn posts exactly one comment, its report, however many attempts it
    took. The issue gets a bounded copy; `_attempt` already logged the full text
    to stderr (the daemon log). Uniform on purpose: a comment is never sized by
    what kind of output it is, so no failure mode can paste an unbounded blob
    into an issue."""
    text = _relocate_artifacts(report, shared, cwd) or "(no report)"
    if len(text) > COMMENT_LIMIT:
        head = COMMENT_LIMIT // 2
        tail = COMMENT_LIMIT - head
        text = (f"{text[:head]}\n\n[... {len(text) - COMMENT_LIMIT} characters omitted; "
                f"full output is in the daemon log ...]\n\n{text[-tail:]}")
    issues.add_comment(cfg, issue_id, text, identity, role_for_email, turns=turns)


def _attempt(fn, role: str, issue_id: int, identity: str):
    """One call to the model: a launch failure is retried, and the full text of
    whatever came back goes to the daemon log."""
    turn = retry.retry(lambda: _checked_turn(fn()), retry_on=(_HarnessLaunchError,),
                       label=f"{role} turn on #{issue_id}")
    print(f"[turn output] issue #{issue_id} {identity}: {turn.text}", file=sys.stderr, flush=True)
    return turn


def _correction(cfg: ProjectConfig, role: str) -> str:
    return ("That response didn't end with a NEXT: directive this role can take. "
            "Respond with exactly one line, as described: " + _directive_line(cfg, role))


def _refusal_message(cfg: ProjectConfig, role: str, refusal: str) -> str:
    return (f"That could not be done: {refusal}. Act on this if you can, write your full report "
            "again, and end with a directive you can take: " + _directive_line(cfg, role))


_NEXT_WORD = re.compile(r"^\s*NEXT:\s*(\S+)\s*$", re.IGNORECASE | re.MULTILINE)
_NEXT_LINE = re.compile(r"^\s*NEXT:\s*\S.*$", re.IGNORECASE)


def _verb_directive(verb: str) -> tuple[str, str | None]:
    """The directive a configured verb means. The table is the authorization:
    a role that reports an outcome the table lists may do exactly what the
    table says, whatever its own tool list would otherwise allow."""
    if verb.startswith("assign "):
        return "assign", verb.split(None, 1)[1].strip()
    if verb == "return":
        return "return", None
    if verb == "block":
        return "state", "blocked"
    return "state", VERB_STATES[verb]


def _resolve_directive(cfg: ProjectConfig, role: str, text: str) -> tuple[str, str | None] | None:
    """A role wired in `transitions` answers with one of its outcomes, and only
    those are read. Any other role answers with the plain directives."""
    table = (cfg.transitions or {}).get(role)
    if table is None:
        return _parse_next(text, _may_close(cfg, role), _may_assign(cfg, role), cfg.roles())
    matches = list(_NEXT_WORD.finditer(text or ""))
    if not matches:
        return None
    verb = table.get(matches[-1].group(1).lower())
    return None if verb is None else _verb_directive(verb)


def _hand_on(cfg: ProjectConfig, issue_id: int, directive: tuple[str, str | None],
             identity: str, role_for_email: str) -> None:
    kind, target = directive
    if kind == "return":
        _return_safely(cfg, issue_id, identity, role_for_email)
    else:
        # Permission was already checked in _parse_next: an assign from a
        # role without assign_issue never becomes a directive at all.
        issues.push_assignee(cfg, issue_id, target, identity, role_for_email)


def _run_one_turn(cfg: ProjectConfig, role: str, issue_id: int, task_id: str) -> None:
    """One turn, one comment. The agent is asked again in its own session when
    its directive is missing or refused, up to `directive_retries` times, and
    nothing is published until the outcome is known.

    A closing verb is tried first and published after, because a refusal gets a
    new report. A handoff publishes first, so the next role can't start on an
    issue that lacks the report it was handed."""
    identity, role_for_email = _identity_for(role, task_id)
    harness = get_harness("program" if cfg.program_of(role) is not None else cfg.harness())
    issue = issues.get_issue(cfg, issue_id)
    if cfg.workspace_of(role) == "worktree":
        worktrees.create(cfg, issue_id, issue["base_branch"])  # lazy, idempotent
    cwd, settings = _cwd_and_settings(cfg, role, task_id, issue_id)
    shared = settings["shared"]
    prompt = _build_prompt(cfg, role, issue, shared)
    started = time.monotonic()
    turn = _attempt(lambda: harness.start(prompt, cwd, settings), role, issue_id, identity)
    usage = cost.merge_usage([], turn)
    report = _without_directive(turn.text)
    session_id = turn.session_id
    directive = _resolve_directive(cfg, role, turn.text)
    attempts = 0

    while True:
        refusal = None
        if directive is not None and directive[0] == "state":
            if directive[1] == "merged" and cfg.workspace_of(role) == "none":
                refusal = "this role has no repository, so there is nothing to merge"
            else:
                refusal = _try_close(cfg, issue_id, directive[1], identity, role_for_email)
            if refusal is None:
                _publish(cfg, issue_id, report, identity, role_for_email, shared, cwd,
                     turns=_turn_records(role, usage, started))
                return
        elif directive is not None and directive[0] == "blocked_by":
            refusal = _try_block_on(cfg, issue_id, directive[1], identity, role_for_email)
            if refusal is None:
                _publish(cfg, issue_id, report, identity, role_for_email, shared, cwd,
                         turns=_turn_records(role, usage, started))
                return
        elif directive is not None:
            _publish(cfg, issue_id, report, identity, role_for_email, shared, cwd,
                     turns=_turn_records(role, usage, started))
            _hand_on(cfg, issue_id, directive, identity, role_for_email)
            return

        if attempts >= cfg.directive_retries:
            _publish(cfg, issue_id, report, identity, role_for_email, shared, cwd,
                     turns=_turn_records(role, usage, started))
            if refusal is not None:
                _block(cfg, issue_id, refusal)
            else:
                issues.add_event(cfg, issue_id, "no_directive",
                                 f"no valid NEXT: directive after {attempts} corrections")
                _return_safely(cfg, issue_id, identity, role_for_email)
            return

        attempts += 1
        ask = _correction(cfg, role) if refusal is None else _refusal_message(cfg, role, refusal)
        turn = _attempt(lambda: harness.resume(session_id, ask, cwd, settings),
                        role, issue_id, identity)
        usage = cost.merge_usage(usage, turn)
        session_id = turn.session_id
        fresh = _without_directive(turn.text)
        # A refusal asks for a new report. A correction only asks for the
        # directive line, so what it says doesn't replace the work's report.
        if fresh and (refusal is not None or not report):
            report = fresh
        directive = _resolve_directive(cfg, role, turn.text)


def _too_many_comments(cfg: ProjectConfig, issue: dict) -> bool:
    cap = MAX_COMMENTS
    return bool(cap) and issues.turns_since_reopen(issue) >= cap


def _spawnable(cfg: ProjectConfig, role: str | None) -> bool:
    """No agents/<role>.md means this isn't a role the daemon can spawn —
    e.g. a human-ish creator role that issues simply rest with until
    something else (a human, an interactive session) moves them."""
    return bool(role) and cfg.agent_prompt_path(role).exists()


def _candidates(cfg: ProjectConfig) -> list[dict]:
    """Issues that may run now, in order: open, assigned to a role the daemon can
    spawn, and not held. Work already under way goes first, then priority, then age."""
    candidates = [i for i in issues.list_issues(cfg, state="open")
                  if _spawnable(cfg, i["assignee"])
                  and not holders.is_held(cfg.project_root, i["id"])]
    candidates.sort(key=lambda i: (not i["started"], i["priority"], i["id"]))
    return candidates


def _next_issue(cfg: ProjectConfig) -> dict | None:
    candidates = _candidates(cfg)
    return candidates[0] if candidates else None


def _live_count(cfg: ProjectConfig) -> int:
    """How many turns are running right now, from the live holder records."""
    count = 0
    for path in (cfg.project_root / ".agents-daemon" / "holders").glob("*.json"):
        if path.stem.isdigit() and holders.current(cfg.project_root, int(path.stem)) is not None:
            count += 1
    return count


def _dispatch(cfg: ProjectConfig, summary: dict) -> list[threading.Thread]:
    role = summary["assignee"]
    issue = issues.get_issue(cfg, summary["id"])
    if _too_many_comments(cfg, issue):
        _block(cfg, issue["id"],
               f"circuit breaker: {issues.turns_since_reopen(issue)} turns without closing, well past "
               f"a normal review cycle. {role}'s own closing logic may not have triggered, so "
               f"check agents/{role}.md, not only the issue.")
        return []
    task_id = str(uuid.uuid4())
    if not holders.claim(cfg.project_root, issue["id"], task_id, os.getpid(), role):
        return []
    thread = threading.Thread(target=_run_one, args=(cfg, role, issue["id"], task_id), daemon=True)
    thread.start()
    return [thread]


def _over_budget(cfg: ProjectConfig) -> bool:
    state = budget.status(cfg)
    return bool(state and state["over"])


def next_action(cfg: ProjectConfig, force: bool = False, limit: int | None = None) -> str:
    if drain.requested(cfg.project_root):
        return "draining: no new turn starts, and the daemon leaves when the running ones finish"
    """What a tick would do right now, in words, without doing it. The manual
    `gitchen tick` command uses this for its dry run and for the "nothing
    happened" case, so a human can see why the queue is idle."""
    slots = (limit or cfg.max_parallel) - _live_count(cfg)
    if slots <= 0:
        return f"{_live_count(cfg)} turn(s) in flight, at the limit of {limit or cfg.max_parallel}"
    paused = pause.active(cfg.project_root)
    if paused and not force:
        left = int(paused["until"] - time.time())
        return f"paused for {max(0, left)}s after a turn that never ran: {paused['reason'][:200]}"
    if _over_budget(cfg) and not force:
        return f"budget reached: {budget.describe(budget.status(cfg))}"
    summary = _next_issue(cfg)
    if summary is None:
        return "nothing to dispatch"
    return f"start #{summary['id']} → {summary['assignee']} ({summary['title']})"


def _sweep_worktrees(cfg: ProjectConfig) -> None:
    """Housekeeping, never allowed to stop dispatch: a failure is logged, and
    the next tick tries again."""
    try:
        worktrees.sweep(cfg)
    except Exception as exc:  # noqa: BLE001 - logged, never fatal to dispatch
        print(f"[worktrees] sweep failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)


def tick(cfg: ProjectConfig, force: bool = False, limit: int | None = None) -> list[threading.Thread]:
    """Non-blocking: starts turns for free slots and returns immediately.
    A slot is one of `max_parallel`, or `limit` when given (a manual tick takes
    one). `force` is for a person running `gitchen tick`: it goes past a pause
    and past a spent budget."""
    _sweep_worktrees(cfg)
    if drain.requested(cfg.project_root):
        return []  # draining: running turns finish, and nothing new starts
    try:
        issues.wake_dependents(cfg)
    except Exception as exc:  # noqa: BLE001 - logged; dispatch carries on
        print(f"[dispatch] waking waiting issues failed: {type(exc).__name__}: {exc}",
              file=sys.stderr, flush=True)
    if pause.active(cfg.project_root) and not force:
        return []  # every issue would fail the same way
    if not force and _over_budget(cfg):
        return []  # nothing starts; issues stay where they are until a reset
    slots = (limit or cfg.max_parallel) - _live_count(cfg)
    if slots <= 0:
        return []  # every slot holds a turn; its directive continues its work
    threads: list[threading.Thread] = []
    for summary in _candidates(cfg)[:slots]:
        threads.extend(_dispatch(cfg, summary))
    return threads