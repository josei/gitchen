#!/usr/bin/env python3
"""Minimal MCP server (stdio, JSON-RPC 2.0) for the gitchen framework.

No `mcp` SDK dependency on purpose — this is the whole of the framework's
"communicating" job, implemented in plain stdlib so there's nothing to
version-match against a client library. Each JSON-RPC request is dispatched
to its own thread so concurrent calls never block on one another.

Spawning agents against issues isn't a tool here at all — that's the
daemon's job (see dispatch.py): it watches assignee/holder state and spawns
on its own, with the spawned agent's own end-of-turn output (always a
comment, plus a parsed NEXT: directive) driving what happens next. A
dispatched worker/reviewer only ever gets reads plus `create_issue` (the
one way to request work from someone else). `update_issue`, `assign_issue`,
`return_issue` and `comment_issue` are the privileged tier — for the
coordinator's own interactive session, which never goes through
dispatch.py's automatic handling.

Usage: server.py --project-root <path-to-instance-repo>
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

if __package__ in (None, ""):
    # Invoked directly (`python3 .../mcp/server.py`), not as `python3 -m mcp.server`:
    # put gitchen/ on sys.path so the package-relative imports below resolve.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from mcp import control, gitutil, issues
    from mcp.config import ProjectConfig
else:
    from . import control, gitutil, issues
    from .config import ProjectConfig

_STDOUT_LOCK = threading.Lock()

TOOLS = [
    {
        "name": "create_issue",
        "description": "Create an issue (roadmap item, decision, bug, anything addressable with an "
                        "id) — the only way to request work from another role. Defaults to assigned "
                        "to yourself; pass `assignee` to delegate immediately. Gets its own worktree "
                        "branched from `base_branch` (default: the public repo's current branch) — "
                        "pass another issue's own branch (`issue/<id>`) to stack this one on top of "
                        "its in-progress, not-yet-merged work instead. Defaults to `state: open`; "
                        "pass `done` for e.g. a decision record that's final the moment it's "
                        "written, with no separate update_issue call needed to close it.",
        "inputSchema": {"type": "object", "properties": {
            "title": {"type": "string"}, "body": {"type": "string"},
            "labels": {"type": "array", "items": {"type": "string"}},
            "priority": {"type": "number", "description": "lower = sooner; default 0"},
            "assignee": {"type": "string", "description": "role to delegate to; defaults to yourself"},
            "base_branch": {"type": "string", "description": "defaults to the public repo's current branch"},
            "state": {"type": "string", "enum": ["open", "blocked", "done", "merged", "canceled"]},
            "blocked_by": {"type": "array", "items": {"type": "integer"},
                           "description": "issues this one waits for; it opens when they all close"},
            "folder": {"type": "string", "description": "a folder the instance declares; required when the project declares folders and sets no default_folder"}},
            "required": ["title", "body"]},
    },
    {
        "name": "get_issue",
        "description": "Read an issue. Returns its fields (including assignee and the full "
                        "assignment_history) plus body lines each tagged with the file's current "
                        "content hash — quote that hash back to update_issue.",
        "inputSchema": {"type": "object", "properties": {"id": {"type": "integer"}},
                         "required": ["id"]},
    },
    {
        "name": "update_issue",
        "description": "Patch an issue's title/state/labels/priority/body. Requires the hash from "
                        "get_issue; a mismatch means it changed since you read it — the response "
                        "includes the fresh state, re-read and retry rather than guessing. Cannot "
                        "touch assignee/assignment_history — handoffs only ever happen through the "
                        "normal end-of-turn NEXT: directive, never an arbitrary patch.",
        "inputSchema": {"type": "object", "properties": {
            "id": {"type": "integer"}, "hash": {"type": "string"},
            "title": {"type": "string"},
            "state": {"type": "string", "enum": ["open", "blocked", "done", "merged", "canceled"]},
            "labels": {"type": "array", "items": {"type": "string"}},
            "priority": {"type": "number"}, "body": {"type": "string"}},
            "required": ["id", "hash"]},
    },
    {
        "name": "list_issues",
        "description": "List issues, optionally filtered by state, label, and/or assignee, sorted "
                        "by priority. Each has `updated`, the time of its latest turn or handoff; "
                        "`since` (an ISO date or time) keeps only issues updated at or after it.",
        "inputSchema": {"type": "object", "properties": {
            "state": {"type": "string"}, "label": {"type": "string"}, "assignee": {"type": "string"},
            "since": {"type": "string"}}},
    },
    {
        "name": "reset_budget",
        "description": "Start counting the instance's spending from now, so turns may run again "
                        "after the budget was reached. Coordinator only: a dispatched turn can't call it.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "move_issue",
        "description": "Place an open issue just before or just after another open issue in the queue. "
                        "Only the moved issue's priority changes, unless the two neighbours have no room "
                        "between them, in which case the open queue is renumbered in one commit.",
        "inputSchema": {"type": "object", "properties": {
            "id": {"type": "integer"}, "before": {"type": "integer"}, "after": {"type": "integer"}},
            "required": ["id"]},
    },
    {
        "name": "assign_issue",
        "description": "Delegate an issue to a role (push). Privileged — same tier as update_issue, "
                        "for the coordinator's own interactive session, which doesn't go through "
                        "dispatch.py's automatic end-of-turn handling. A dispatched worker/reviewer "
                        "never gets this tool; its own disposition is read from its turn's NEXT: line "
                        "instead, so it can't forge a handoff it wasn't permitted to make.",
        "inputSchema": {"type": "object", "properties": {
            "id": {"type": "integer"}, "to": {"type": "string"}},
            "required": ["id", "to"]},
    },
    {
        "name": "return_issue",
        "description": "Hand an issue back to whoever assigned it (pop). Privileged, same "
                        "reasoning as assign_issue — for administrative use outside the normal "
                        "dispatch flow (undoing a bad delegation, etc).",
        "inputSchema": {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]},
    },
    {
        "name": "comment_issue",
        "description": "Append a comment to an issue. Privileged, same reasoning as assign_issue — "
                        "a dispatched worker/reviewer's comments are posted automatically from its "
                        "turn output, never through a tool call it could choose to skip.",
        "inputSchema": {"type": "object", "properties": {
            "id": {"type": "integer"}, "text": {"type": "string"}},
            "required": ["id", "text"]},
    },
]


def _identity(role: str | None, task_id: str | None) -> tuple[str, str]:
    """(label, role_for_email): label is what appears as the comment
    attribution and the git commit author's display name — role_for_email
    stays stable per role (task_id would make a distinct "author" per task
    otherwise, which is noise for `git shortlog`). Derived entirely from how
    this connection was launched (serve()'s --role/--task-id,
    decided by loop.py, not the caller) — nothing here comes from a tool
    argument, so it can't be spoofed by whatever's on the other end."""
    role = role or "coordinator"
    label = role if not task_id else f"{role} (task {task_id[:8]})"
    return label, role


def _git_author(identity: tuple[str, str]) -> str:
    label, role = identity
    return f"{label} <{role}@gitchen.local>"


def dispatch(cfg: ProjectConfig, name: str, args: dict, identity: tuple[str, str]) -> dict:
    if name == "create_issue":
        return issues.create_issue(cfg, args["title"], args["body"], args.get("labels"),
                                    args.get("priority", 0), identity[0], identity[1],
                                    assignee=args.get("assignee"), base_branch=args.get("base_branch"),
                                    state=args.get("state", "open"), blocked_by=args.get("blocked_by"),
                                    folder=args.get("folder"))
    if name == "get_issue":
        return issues.get_issue(cfg, args["id"])
    if name == "update_issue":
        patch = {k: v for k, v in args.items() if k in ("title", "state", "labels", "priority", "body")}
        return issues.update_issue(cfg, args["id"], args["hash"], patch, identity[0], identity[1])
    if name == "list_issues":
        return {"issues": issues.list_issues(cfg, args.get("state"), args.get("label"), args.get("assignee"),
                                                   since=args.get("since"))}
    if name == "assign_issue":
        return issues.push_assignee(cfg, args["id"], args["to"], identity[0], identity[1])
    if name == "return_issue":
        return issues.return_or_block(cfg, args["id"], identity[0], identity[1])
    if name == "comment_issue":
        return issues.add_comment(cfg, args["id"], args["text"], identity[0], identity[1])
    if name == "move_issue":
        return issues.move_issue(cfg, args["id"], before=args.get("before"), after=args.get("after"),
                                 identity=identity[0], role_for_email=identity[1])
    if name == "reset_budget":
        from . import budget
        budget.reset(cfg.project_root)
        return budget.status(cfg)
    raise ValueError(f"unknown tool: {name}")


def write_message(msg: dict) -> None:
    line = json.dumps(msg)
    with _STDOUT_LOCK:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def handle_request(cfg: ProjectConfig, req: dict, allowed_tools: set[str] | None,
                    identity: tuple[str, str], proxy_task: str | None = None) -> None:
    """`allowed_tools` is None for an unrestricted connection (the
    coordinator's own session) or a set of tool names for a scoped one —
    see `serve()`'s `--role`. Enforced here, server-side, so it
    holds regardless of which harness or CLI flags spawned the caller;
    a harness only has to know how to point an invocation at this server,
    never how to restrict what it does once connected."""
    req_id = req.get("id")
    method = req.get("method")
    params = req.get("params") or {}

    def reply(result=None, error=None):
        if req_id is None:
            return  # notification, no response expected
        msg = {"jsonrpc": "2.0", "id": req_id}
        msg["error" if error else "result"] = error or result
        write_message(msg)

    try:
        if method == "initialize":
            reply({"protocolVersion": "2024-11-05",
                   "serverInfo": {"name": "gitchen-mcp", "version": "0.1.0"},
                   "capabilities": {"tools": {}}})
        elif method == "notifications/initialized":
            pass
        elif method == "tools/list":
            tools = TOOLS if allowed_tools is None else [t for t in TOOLS if t["name"] in allowed_tools]
            reply({"tools": tools})
        elif method == "tools/call":
            tool_name = params["name"]
            if allowed_tools is not None and tool_name not in allowed_tools:
                reply(error={"code": -32601, "message": f"'{tool_name}' is not permitted for this agent"})
                return
            arguments = params.get("arguments") or {}
            if proxy_task is not None:
                result = control.call(cfg.project_root, proxy_task, tool_name, arguments)
            else:
                result = dispatch(cfg, tool_name, arguments, identity)
            reply({"content": [{"type": "text", "text": json.dumps(result)}]})
        elif method == "ping":
            reply({})
        else:
            reply(error={"code": -32601, "message": f"method not found: {method}"})
    except Exception as exc:  # noqa: BLE001 - report to the client, don't crash the server
        reply(error={"code": -32000, "message": str(exc)})


def _pid_path(project_root: Path, role: str | None) -> Path:
    d = project_root / ".agents-daemon"
    d.mkdir(exist_ok=True)
    # Keyed by role, not just project: a scoped worker/reviewer
    # connection can legitimately run at the same time as the coordinator's
    # own unrestricted one (the daemon spawns it against an issue while the
    # coordinator's own `gitchen serve` is still connected) — those must
    # not fight over one pidfile. Two connections for the *same* role
    # still take over one another, which is the case this singleton exists
    # for.
    name = "mcp.pid" if role is None else f"mcp-{role}.pid"
    return d / name


def _is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _take_over_singleton(project_root: Path, role: str | None) -> None:
    """Only one `gitchen serve` per (project, role) may run at a time.
    If an older one for the same role is still around (e.g. a previous
    session's harness process never got cleaned up), kill it and take over
    rather than erroring out — a second one is nearly always meant to
    replace the first."""
    pid_path = _pid_path(project_root, role)
    if pid_path.exists():
        old_pid_text = pid_path.read_text().strip()
        old_pid = int(old_pid_text) if old_pid_text.isdigit() else None
        if old_pid and old_pid != os.getpid() and _is_running(old_pid):
            os.kill(old_pid, signal.SIGTERM)
            for _ in range(20):  # up to ~2s
                if not _is_running(old_pid):
                    break
                time.sleep(0.1)
            else:
                os.kill(old_pid, signal.SIGKILL)
    pid_path.write_text(str(os.getpid()))


def serve(project_root: str, role: str | None = None, task_id: str | None = None) -> None:
    """Blocking stdio loop. Called directly by `gitchen serve`, or by
    __main__ below when server.py is invoked as a script.

    `role=None` is the coordinator's own unrestricted connection.
    Any other value restricts this connection to `project.yml`'s
    `allowed_tools[role]` list of tool names — enforced in
    `handle_request`, so it holds no matter which harness or CLI spawned
    whoever is talking to this process. `task_id`, when this connection
    belongs to a worker/reviewer turn, rides along the same way and feeds
    `_identity` for comment/commit attribution."""
    # Role prompts aren't readable from a turn's sandbox, and this process never
    # needs the transitions table: the daemon checks it at load, not here.
    cfg = ProjectConfig.load(project_root, check_outcomes=False)
    allowed_tools = None if role is None else set(cfg.allowed_tools.get(role, []))
    identity = _identity(role, task_id)
    # A dispatched turn's connection is a proxy: the daemon runs its calls.
    # The coordinator's own connection still runs them locally.
    proxy_task = task_id if role is not None and task_id is not None else None
    if proxy_task is None:
        _take_over_singleton(cfg.project_root, role)
        gitutil.pull(cfg.project_root)

    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            req = json.loads(line)
            threading.Thread(target=handle_request, args=(cfg, req, allowed_tools, identity, proxy_task),
                              daemon=True).start()
    finally:
        pid_path = _pid_path(cfg.project_root, role)
        if pid_path.exists() and pid_path.read_text().strip() == str(os.getpid()):
            pid_path.unlink()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project-root", required=True)
    ap.add_argument("--role", default=None,
                     help="restrict this connection to project.yml's allowed_tools[role]; "
                          "omit for the coordinator's own unrestricted connection")
    ap.add_argument("--task-id", default=None,
                     help="this connection's dispatch task id, for comment/commit attribution")
    args = ap.parse_args()
    serve(args.project_root, args.role, args.task_id)


if __name__ == "__main__":
    main()
