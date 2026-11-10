"""Harness adapter for the omp CLI (Oh My Pi).

Each turn is one blocking `omp -p --mode json ...` subprocess. `--mode json`
emits a JSONL event stream rather than rendered text: the first `session`
event carries omp's own session id, and the closing `agent_end` carries
`isTerminal` plus the full message list — the last assistant message's text
blocks are the turn's answer. A run that dies before `agent_end` (bad flag,
unknown model, a crash) prints plain text to stderr and exits 0, so the
absence of `agent_end` is the failure signal, not the exit code.

Usage is per call, not cumulative: checked against omp 2026-11-09 by resuming a
session. The second `agent_end` holds only that call's messages, and each call's
cost is its own, so the turn sums the calls (`cost.merge_usage`).

Settings (from the global config, see mcp/global_config.py): `binary`
(default `omp`), `model`, `thinking` (`off|minimal|low|medium|high|xhigh|
max|auto`), and `mcp` (same shape the other adapters use: `role`,
`project_root`, `task_id`). `--auto-approve` is safe for the same reason
`permission_mode: bypassPermissions` is: the `sandbox` prefix contains the
whole process.

omp has no per-invocation MCP flag. It discovers project-scoped MCP config
from its working directory (`.omp/mcp.json`), and a turn's working
directory is the issue's worktree — so the scoped gitchen server definition
is written there for the turn and removed afterward. `.omp/` is also added
to the repository's local `info/exclude`, so an agent that commits
everything still can't publish a file naming the gitchen checkout. This is
omp's analogue of the `--mcp-config` + `--strict-mcp-config` pair the
Claude Code adapter uses.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from ..harness import TurnResult, register
from .process import run as run_process
from .sandbox import prefix as sandbox_prefix


def _mcp_config_file(cwd: str, mcp: dict) -> Path:
    """Writes the scoped gitchen server definition where omp will find it,
    and returns the path for cleanup."""
    server_args = ["serve", "--project-root", mcp["project_root"], "--role", mcp["role"]]
    if mcp.get("task_id"):
        server_args += ["--task-id", mcp["task_id"]]
    config = {"mcpServers": {"gitchen": {"command": "gitchen", "args": server_args}}}
    path = Path(cwd) / ".omp" / "mcp.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config))
    return path


def _exclude_from_git(cwd: str) -> None:
    """Keeps the turn's `.omp/mcp.json` out of `git status` in the worktree.
    The file is gone by the time the turn ends, but an agent that stages
    everything mid-turn would otherwise commit a path into the gitchen
    checkout. `info/exclude` is local to the clone (never committed), and
    shared by every worktree of it — exactly the lifetime this needs."""
    out = subprocess.run(["git", "-C", cwd, "rev-parse", "--git-common-dir"],
                         capture_output=True, text=True)
    if out.returncode != 0:
        return
    exclude = Path(cwd, out.stdout.strip()).resolve() / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    text = exclude.read_text() if exclude.exists() else ""
    if ".omp/" not in text.splitlines():
        exclude.write_text(text + ("" if not text or text.endswith("\n") else "\n") + ".omp/\n")


def _last_assistant_text(messages: list[dict]) -> str:
    for message in reversed(messages):
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        return "".join(block.get("text", "") for block in content or []
                       if isinstance(block, dict) and block.get("type") == "text")
    return ""


def _usage(messages: list[dict], model: str | None) -> list[dict]:
    """Each assistant message carries its own call's usage and cost. A model
    named on the message wins over the configured one."""
    from ..cost import record
    totals: dict[str, dict] = {}
    for message in messages:
        if message.get("role") != "assistant" or not message.get("usage"):
            continue
        u = message["usage"]
        name = message.get("model") or model or "unknown"
        item = totals.setdefault(name, record(name))
        item["usd"] = round(item["usd"] + (u.get("cost") or {}).get("total", 0), 6)
        item["input"] += u.get("input", 0)
        item["output"] += u.get("output", 0)
        item["cache_read"] += u.get("cacheRead", 0)
        item["cache_write"] += u.get("cacheWrite", 0)
    return list(totals.values())


def _parse_stream(stdout: str, model: str | None = None) -> tuple[str, str, bool, list[dict]]:
    """(session id, final text, terminal, usage) from omp's --mode json stream."""
    session_id, text, terminal, usage = "", "", False, []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "session" and event.get("id"):
            session_id = event["id"]
        elif event.get("type") == "agent_end":
            terminal = bool(event.get("isTerminal"))
            messages = event.get("messages") or []
            text = _last_assistant_text(messages)
            usage = _usage(messages, model)
    return session_id, text, terminal, usage


@register("omp")
class OmpHarness:
    default_binary = "omp"

    def start(self, prompt: str, cwd: str, settings: dict) -> TurnResult:
        return self._run(prompt, cwd, settings, resume_id=None)

    def resume(self, session_id: str, prompt: str, cwd: str, settings: dict) -> TurnResult:
        return self._run(prompt, cwd, settings, resume_id=session_id)

    def _run(self, prompt: str, cwd: str, settings: dict, resume_id: str | None) -> TurnResult:
        binary = settings.get("binary", "omp")
        args = [binary, "-p", "--mode", "json", "--auto-approve", "--no-title"]
        if resume_id:
            args += ["--resume", resume_id]
        if settings.get("model"):
            args += ["--model", settings["model"]]
        if settings.get("thinking"):
            args += ["--thinking", settings["thinking"]]
        # The prompt goes last, after `--`: a prompt whose first line looks
        # like a flag (the role prompt starts with prose, but the issue body
        # can start with anything) must never be parsed as one.
        args += ["--", prompt]

        mcp_path = None
        if settings.get("mcp"):
            mcp_path = _mcp_config_file(cwd, settings["mcp"])
            _exclude_from_git(cwd)
        try:
            returncode, stdout, stderr = run_process(sandbox_prefix(settings, cwd) + args, cwd,
                                                     settings.get("env"))
        finally:
            if mcp_path is not None:
                mcp_path.unlink(missing_ok=True)
                try:
                    mcp_path.parent.rmdir()  # only if this turn created it
                except OSError:
                    pass

        session_id, text, terminal, usage = _parse_stream(stdout, settings.get("model"))
        ok = returncode == 0 and terminal
        if not ok and not text:
            text = stderr.strip() or stdout.strip() or f"(exit {returncode}, no output)"
        return TurnResult(session_id=session_id or (resume_id or ""), text=text, ok=ok,
                           raw={"returncode": returncode, "stdout": stdout, "stderr": stderr},
                           never_ran=not ok and not terminal, usage=usage)

    def interrupt(self, session_id: str) -> None:
        # Turns are strictly sequential and block on the subprocess; the
        # caller stops it by terminating that process.
        pass
