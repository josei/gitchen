"""Harness adapter for the Claude Code CLI.

Each turn is one blocking `claude -p ... --output-format json` subprocess
call. A turn's completion *is* the process exit, so "wait" needs no polling.
`settings` (from the project's project.yml, see config.py's
`settings_for_role`) may set `binary`, `model`, `effort`, `permission_mode`,
`mcp` (`{"role": <name>, "project_root": <path>, "task_id": <uuid>,
"tools": [...]}` — `tools` is that role's own `allowed_tools` list,
pre-allowed via `--allowedTools` since a headless session has no human to
answer a permission prompt otherwise), `bash_tools` (a role's own list
of Bash permission-rule patterns, e.g. `["npm *", "git *"]` — same
headless-has-no-human-to-ask problem, for plain shell commands instead of
MCP tools; see `_allowed_tools_args`), and `sandbox` (`{"profile":
<name, e.g. "nolabs-ai/claude">, "allow": [<extra readwrite paths>],
"allow_domains": [...]}` — implemented with nono
(https://nono.sh); `allow_domains` is the outbound-network allowlist for
that role, on top of the Claude API host the harness always adds. `allow`
is for grants the profile itself has no way to know about, e.g. a tool's
own cache directory outside the project root and the worktree).

`bash_tools`/`--allowedTools` and `sandbox` solve the same headless-approval
problem at different layers and don't compose well: once a role actually
needs real shell access (installing deps, running a test suite, not just
reading files), hand-listing every Bash pattern it might need is exactly
the kind of allowlist that's always one real command behind. `sandbox`
wraps the whole subprocess in an OS-level sandbox (filesystem/network, via
`nono run --profile <profile> -- <binary>`) so `permission_mode` can
safely be `bypassPermissions` — nothing to prompt for, and nothing to
escape to, because the blast radius is contained outside the process
instead of gated inside it. When `sandbox` is set, this harness also writes
a throwaway `--settings` file turning Claude Code's own (redundant,
weaker) sandbox off and refusing its `dangerouslyDisableSandbox` escape
hatch, per nono's own integration guidance — otherwise Claude Code would
still try to manage a sandbox boundary nono already owns, and could still
ask to drop out of it. A project not using `sandbox` keeps `bash_tools` as
its only option.

Working directory is the subprocess's own `cwd`, not a CLI flag — there is
no `--cwd` option on this CLI (it just runs wherever it's invoked from);
an earlier version of this adapter assumed one existed and every call
silently failed (`error: unknown option '--cwd'`) until a real end-to-end
run against the actual binary caught it — nothing in the test suite uses
a real `claude` binary, so a stub that doesn't validate flags let this
through for a long time.

`mcp`, if present, is this harness's job to honor however its own CLI
supports attaching a scoped MCP server to one invocation — here that's
`--mcp-config <generated-json> --strict-mcp-config` pointing at `gitchen
serve --role <role>`. A different harness's adapter would do
this its own way, or not support it at all; dispatch.py never needs to know.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid

from ..harness import TurnResult, register
from .process import run as run_process
from .sandbox import prefix as sandbox_prefix

# mcp/harnesses/claude_code.py -> mcp/harnesses -> mcp -> gitchen's own root.
# Resolved from this file's own location, not PATH, so it's right regardless
# of where `gitchen` on PATH happens to symlink from.
_ANTHROPIC_API_HOST = "api.anthropic.com"


def _mcp_config_args(settings: dict) -> tuple[list[str], str | None]:
    """Returns (extra CLI args, temp file path to clean up afterward)."""
    mcp = settings.get("mcp")
    if not mcp:
        return [], None
    server_args = ["serve", "--project-root", mcp["project_root"], "--role", mcp["role"]]
    if mcp.get("task_id"):
        server_args += ["--task-id", mcp["task_id"]]
    config = {"mcpServers": {"gitchen": {"command": "gitchen", "args": server_args}}}
    fd, path = tempfile.mkstemp(suffix=".mcp.json")
    with os.fdopen(fd, "w") as f:
        json.dump(config, f)
    # --strict-mcp-config: this invocation gets *only* the scoped server
    # above, never whatever else might be lying around (a stray .mcp.json
    # in cwd, user/project settings, etc.) — the profile has to be the
    # whole story, not a floor under something broader.
    return ["--mcp-config", path, "--strict-mcp-config"], path


def _allowed_tools_args(settings: dict) -> list[str]:
    """A headless `-p` session has no human to answer a permission
    prompt, so without this, every one of these MCP calls is silently
    denied regardless of permission_mode (found via a real dispatch run
    — pm's own get_issue/list_issues/update_issue calls all failed this
    way). Pre-allowing exactly `allowed_tools[role]`'s own list isn't a
    wider grant than that profile already has: the MCP server enforces
    the same list server-side regardless of what the CLI was told to
    allow, this just lets the already-permitted calls actually run.

    The same gap hits plain shell commands: a builder with nothing here
    can read files and edit (acceptEdits covers that) but can't run
    `npm ci` or a test suite — every Bash call needs a prompt, and
    there's no human to answer it (found via a real dispatch run: the
    builder reported `npm ci` refused with "This command requires
    approval" and correctly stopped rather than working around it).
    `bash_tools` is this project's own opt-in per role, same shape as a
    Bash permission rule (e.g. "npm *"), never implied by allowed_tools."""
    tools = (settings.get("mcp") or {}).get("tools")
    mcp_args = [f"mcp__gitchen__{name}" for name in tools] if tools else []
    bash_args = [f"Bash({pattern})" for pattern in settings.get("bash_tools") or []]
    allowed = mcp_args + bash_args
    if not allowed:
        return []
    return ["--allowedTools", *allowed]


def _claude_sandbox_override_args(settings: dict) -> tuple[list[str], str | None]:
    """Only meaningful alongside `sandbox`: Claude Code's own sandbox is
    redundant with (and weaker than) nono's OS-level one, and its
    `dangerouslyDisableSandbox` retry is an escape hatch nono's guidance
    says to close. Returns (extra CLI args, temp file path to clean up)."""
    if not settings.get("sandbox"):
        return [], None
    config = {"sandbox": {"enabled": False}, "permissions": {"allowUnsandboxedCommands": False}}
    fd, path = tempfile.mkstemp(suffix=".settings.json")
    with os.fdopen(fd, "w") as f:
        json.dump(config, f)
    return ["--settings", path], path


def _spent_tokens(payload: dict) -> bool:
    """`usage` is for this call alone, even on a resume, so zero means this call
    did no work. It only ever says "ran" with evidence: a payload with no usage
    counts as not having run."""
    usage = payload.get("usage") or {}
    keys = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    return any(usage.get(key) for key in keys)


def _usage(payload: dict, model: str | None) -> list[dict]:
    """What the CLI says this session cost, per model. Totals for the whole
    session, including earlier calls on a resume (see cost.py)."""
    from ..cost import record
    per_model = payload.get("modelUsage") or {}
    if per_model:
        return [record(name, usd=m.get("costUSD", 0), input=m.get("inputTokens", 0),
                       output=m.get("outputTokens", 0), cache_read=m.get("cacheReadInputTokens", 0),
                       cache_write=m.get("cacheCreationInputTokens", 0))
                for name, m in per_model.items()]
    usage = payload.get("usage") or {}
    if not payload.get("total_cost_usd") and not any(usage.values()):
        return []
    return [record(model or "unknown", usd=payload.get("total_cost_usd", 0),
                   input=usage.get("input_tokens", 0), output=usage.get("output_tokens", 0),
                   cache_read=usage.get("cache_read_input_tokens", 0),
                   cache_write=usage.get("cache_creation_input_tokens", 0))]


def _run(args: list[str], cwd: str, model: str | None = None, env: dict | None = None) -> TurnResult:
    returncode, stdout, stderr = run_process(args, cwd, env)
    payload = {}
    if stdout:
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            payload = {}
    if not isinstance(payload, dict):
        payload = {}

    session_id = payload.get("session_id", "")
    if returncode != 0 or not payload:
        # The CLI failed. A usage limit still prints its result JSON and exits
        # non-zero, so read the reason from it when there is one; otherwise
        # surface whatever's available rather than a silent, unexplained empty
        # failure. This is what masked the --cwd bug above: an empty stdout
        # used to produce `text=""`, `raw={}`, with stderr nowhere in sight.
        text = (payload.get("result") or stdout or stderr.strip()
                or f"(exit {returncode}, no output)")
        return TurnResult(session_id=session_id, text=text, ok=False,
                          raw={"returncode": returncode, "stdout": stdout, "stderr": stderr},
                          never_ran=not _spent_tokens(payload),
                          usage=_usage(payload, model), usage_cumulative=True)

    text = payload.get("result", stdout)
    ok = not payload.get("is_error", False)
    return TurnResult(session_id=session_id, text=text, raw=payload, ok=ok,
                      never_ran=not ok and not _spent_tokens(payload),
                      usage=_usage(payload, model), usage_cumulative=True)


def _run_with_mcp_cleanup(args: list[str], cwd: str, settings: dict) -> TurnResult:
    mcp_args, mcp_tmp_path = _mcp_config_args(settings)
    settings_args, settings_tmp_path = _claude_sandbox_override_args(settings)
    try:
        # --allowedTools last, after --mcp-config/--strict-mcp-config: it's
        # a variadic flag (space-separated tool names), so anything placed
        # after it without its own leading -- would risk being swallowed
        # into that list by a less careful parser. The nono prefix goes in
        # front of everything, including the binary itself — it's a
        # separate process wrapping this whole invocation, not a flag to it.
        full_args = sandbox_prefix(settings, cwd, api_hosts=(_ANTHROPIC_API_HOST,)) + args + mcp_args + settings_args + _allowed_tools_args(settings)
        return _run(full_args, cwd, settings.get("model"), settings.get("env"))
    finally:
        for tmp_path in (mcp_tmp_path, settings_tmp_path):
            if tmp_path:
                os.unlink(tmp_path)


@register("claude-code")
class ClaudeCodeHarness:
    default_binary = "claude"

    def start(self, prompt: str, cwd: str, settings: dict) -> TurnResult:
        session_id = str(uuid.uuid4())
        binary = settings.get("binary", "claude")
        args = [
            binary, "-p", prompt,
            "--session-id", session_id,
            "--output-format", "json",
            "--permission-mode", settings.get("permission_mode", "default"),
        ]
        if settings.get("model"):
            args += ["--model", settings["model"]]
        if settings.get("effort"):
            args += ["--effort", settings["effort"]]
        result = _run_with_mcp_cleanup(args, cwd, settings)
        if not result.session_id:
            result.session_id = session_id
        return result

    def resume(self, session_id: str, prompt: str, cwd: str, settings: dict) -> TurnResult:
        binary = settings.get("binary", "claude")
        args = [
            binary, "-p", prompt,
            "--resume", session_id,
            "--output-format", "json",
        ]
        if settings.get("model"):
            args += ["--model", settings["model"]]
        if settings.get("effort"):
            args += ["--effort", settings["effort"]]
        result = _run_with_mcp_cleanup(args, cwd, settings)
        if not result.session_id:
            result.session_id = session_id
        return result

    def interrupt(self, session_id: str) -> None:
        # v1 turns run strictly sequentially and block on subprocess.run, so
        # interruption is the caller terminating that subprocess directly.
        # `claude stop <id>` exists for a future --bg-based design.
        pass
