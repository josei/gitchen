"""The daemon's control socket. Every tool call from a dispatched turn runs
here, inside the daemon, not inside the turn's sandbox.

A dispatched turn's MCP process (`gitchen serve --role <role>
--task-id <id>`) is a thin proxy: it forwards each call to this socket. The
daemon authorizes the call from the live holder record for the task id, and
takes the role from that record, never from the caller. A request runs to
completion even if the caller has gone; only the reply can be lost.
"""

from __future__ import annotations

import json
import socket
import threading
from pathlib import Path

from . import holders
from .config import ProjectConfig

_MAX_PATH = 100  # AF_UNIX paths are limited to about 104 bytes on macOS


class ControlError(RuntimeError):
    pass


def socket_path(project_root: Path) -> Path:
    path = Path(project_root) / ".agents-daemon" / "control.sock"
    if len(str(path)) > _MAX_PATH:
        raise ControlError(f"control socket path is too long for AF_UNIX: {path}")
    return path


def call(project_root: Path, task_id: str, tool: str, args: dict) -> dict:
    try:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.connect(str(socket_path(project_root)))
    except OSError as exc:
        raise ControlError(f"the daemon is not reachable ({exc}); is it running?") from exc
    with conn:
        conn.sendall((json.dumps({"task_id": task_id, "tool": tool, "args": args}) + "\n").encode())
        chunks = []
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    reply = json.loads(b"".join(chunks) or b"{}")
    if "error" in reply:
        raise ControlError(reply["error"])
    return reply["result"]


# Tools a turn may never call, whatever its role's list says: they are the
# person's own levers (a spending reset), not work a role can do.
COORDINATOR_ONLY = frozenset({"reset_budget"})


# Tools that change an existing issue. Filing a new one is never scoped: that is
# how a role asks for a change in a folder it can't edit.
EDITING = frozenset({"update_issue", "move_issue", "assign_issue", "return_issue", "comment_issue"})


def _check_edit_scope(cfg: ProjectConfig, role: str, tool: str, args: dict) -> None:
    """A role with an edit scope may change only issues in its folders. Anything
    outside them, it asks for by filing a new issue there."""
    scope = (cfg.edit_scopes or {}).get(role)
    if scope is None or tool not in EDITING or "id" not in args:
        return
    from . import issues
    folder = issues.folder_of(cfg, int(args["id"]))
    if folder not in scope:
        where = folder or "the top level"
        raise PermissionError(f"{role} cannot edit issues in {where}. To request a change, create a new "
                              f"issue there.")


def _execute(cfg: ProjectConfig, task_id: str, tool: str, args: dict) -> dict:
    from . import server  # the tool implementations live with the coordinator's server
    holder = holders.find_by_task(cfg.project_root, task_id)
    if holder is None:
        raise PermissionError(f"no live turn for task {task_id}")
    if tool in COORDINATOR_ONLY:
        raise PermissionError(f"'{tool}' is coordinator-only; a dispatched turn can't call it")
    role = holder["role"]
    if tool not in cfg.allowed_tools.get(role, []):
        raise PermissionError(f"'{tool}' is not permitted for {role}")
    _check_edit_scope(cfg, role, tool, args)
    return server.dispatch(cfg, tool, args, server._identity(role, task_id))


def _handle(conn: socket.socket, cfg: ProjectConfig) -> None:
    with conn:
        try:
            req = json.loads(conn.makefile("rb").readline())
            reply = {"result": _execute(cfg, req["task_id"], req["tool"], req.get("args") or {})}
        except Exception as exc:  # noqa: BLE001 - reported to the caller, never crashes the daemon
            reply = {"error": f"{type(exc).__name__}: {exc}"}
        try:
            conn.sendall(json.dumps(reply).encode())
        except OSError:
            pass  # the caller went away; the work has already run


def serve_in_background(cfg: ProjectConfig) -> Path:
    path = socket_path(cfg.project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(64)

    def accept_loop() -> None:
        while True:
            conn, _ = listener.accept()
            threading.Thread(target=_handle, args=(conn, cfg), daemon=True).start()

    threading.Thread(target=accept_loop, daemon=True).start()
    return path
