"""Tests the Claude Code harness adapter against a stub `claude` binary, so
the review-loop logic is exercised without burning a real agent turn."""

import json
import os
import stat
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import subprocess  # noqa: E402

from mcp.harnesses import claude_code as cc  # noqa: E402
from mcp.harnesses import process  # noqa: E402
from mcp.harnesses.claude_code import ClaudeCodeHarness  # noqa: E402


def make_stub(tmp_path: Path, behavior: str) -> Path:
    """behavior is inlined python (already dedented) that reads argv and
    prints a JSON result line."""
    stub = tmp_path / "claude"
    header = "#!/usr/bin/env python3\nimport sys, json\nargv = sys.argv[1:]\n"
    stub.write_text(header + textwrap.dedent(behavior))
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    return stub


def test_start_parses_json_result(tmp_path):
    stub = make_stub(tmp_path, textwrap.dedent("""\
        sid = argv[argv.index("--session-id") + 1]
        print(json.dumps({"session_id": sid, "result": "did the thing", "is_error": False}))
    """))
    h = ClaudeCodeHarness()
    result = h.start("do the thing", str(tmp_path), {"binary": str(stub)})
    assert result.ok is True
    assert result.text == "did the thing"
    assert result.session_id


def test_start_detects_error(tmp_path):
    stub = make_stub(tmp_path, textwrap.dedent("""\
        print(json.dumps({"session_id": "abc", "result": "oops", "is_error": True}))
    """))
    h = ClaudeCodeHarness()
    result = h.start("do the thing", str(tmp_path), {"binary": str(stub)})
    assert result.ok is False
    assert result.text == "oops"


def test_resume_passes_session_id(tmp_path):
    stub = make_stub(tmp_path, textwrap.dedent("""\
        sid = argv[argv.index("--resume") + 1]
        print(json.dumps({"session_id": sid, "result": f"resumed {sid}", "is_error": False}))
    """))
    h = ClaudeCodeHarness()
    result = h.resume("session-123", "follow up", str(tmp_path), {"binary": str(stub)})
    assert result.session_id == "session-123"
    assert result.text == "resumed session-123"


def test_start_forwards_model_and_effort(tmp_path):
    stub = make_stub(tmp_path, textwrap.dedent("""\
        sid = argv[argv.index("--session-id") + 1]
        model = argv[argv.index("--model") + 1] if "--model" in argv else None
        effort = argv[argv.index("--effort") + 1] if "--effort" in argv else None
        print(json.dumps({"session_id": sid,
                           "result": json.dumps({"model": model, "effort": effort}),
                           "is_error": False}))
    """))
    h = ClaudeCodeHarness()
    result = h.start("do it", str(tmp_path),
                      {"binary": str(stub), "model": "claude-opus-5-5", "effort": "high"})
    assert json.loads(result.text) == {"model": "claude-opus-5-5", "effort": "high"}


def test_resume_forwards_model_and_effort(tmp_path):
    stub = make_stub(tmp_path, textwrap.dedent("""\
        sid = argv[argv.index("--resume") + 1]
        model = argv[argv.index("--model") + 1] if "--model" in argv else None
        effort = argv[argv.index("--effort") + 1] if "--effort" in argv else None
        print(json.dumps({"session_id": sid,
                           "result": json.dumps({"model": model, "effort": effort}),
                           "is_error": False}))
    """))
    h = ClaudeCodeHarness()
    result = h.resume("s", "follow up", str(tmp_path),
                       {"binary": str(stub), "model": "claude-haiku-5-5", "effort": "low"})
    assert json.loads(result.text) == {"model": "claude-haiku-5-5", "effort": "low"}


def test_no_model_or_effort_passes_no_such_flags(tmp_path):
    stub = make_stub(tmp_path, textwrap.dedent("""\
        sid = argv[argv.index("--session-id") + 1]
        print(json.dumps({"session_id": sid,
                           "result": json.dumps({"model": "--model" in argv, "effort": "--effort" in argv}),
                           "is_error": False}))
    """))
    h = ClaudeCodeHarness()
    result = h.start("do it", str(tmp_path), {"binary": str(stub)})
    assert json.loads(result.text) == {"model": False, "effort": False}


def test_non_json_stdout_is_treated_as_error(tmp_path):
    stub = make_stub(tmp_path, 'print("not json")')
    h = ClaudeCodeHarness()
    result = h.start("x", str(tmp_path), {"binary": str(stub)})
    assert result.ok is False
    assert result.text == "not json"


def test_cwd_is_the_subprocess_cwd_not_a_cli_flag(tmp_path):
    """Real regression: an earlier version passed `--cwd <path>` as a CLI
    argument, which isn't a flag this CLI has at all — every real call
    failed with `error: unknown option '--cwd'`, invisible here because a
    stub binary doesn't validate its own flags. The fix is cwd=... on the
    subprocess itself; this asserts that's actually what's happening by
    having the stub report its own os.getcwd()."""
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    stub = make_stub(tmp_path, textwrap.dedent("""\
        import os
        sid = argv[argv.index("--session-id") + 1]
        assert "--cwd" not in argv, "there is no --cwd flag on the real CLI"
        print(json.dumps({"session_id": sid, "result": os.getcwd(), "is_error": False}))
    """))
    h = ClaudeCodeHarness()
    result = h.start("do it", str(workdir), {"binary": str(stub)})
    assert result.ok is True
    assert result.text == str(workdir)


def test_cli_failure_with_empty_stdout_surfaces_stderr_not_a_silent_blank(tmp_path):
    """A nonzero exit with no parseable JSON used to come back as
    text="", raw={} — ok=False with no explanation anywhere. This is what
    actually happened in the --cwd bug above before it was diagnosed."""
    stub = make_stub(tmp_path, textwrap.dedent("""\
        import sys
        print("error: unknown option '--bogus'", file=sys.stderr)
        sys.exit(1)
    """))
    h = ClaudeCodeHarness()
    result = h.start("do it", str(tmp_path), {"binary": str(stub)})
    assert result.ok is False
    assert "unknown option" in result.text
    assert result.raw["returncode"] == 1
    assert "unknown option" in result.raw["stderr"]


ZERO_USAGE = {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0,
              "cache_read_input_tokens": 0}


def _failing_stub(tmp_path, usage, exit_code=1):
    payload = {"session_id": "s", "is_error": True, "result": "You have hit your usage limit",
               "total_cost_usd": 0, "usage": usage}
    return make_stub(tmp_path, f"import json, sys\nprint(json.dumps({payload!r}))\nsys.exit({exit_code})\n")


def test_a_failed_call_that_used_no_tokens_never_ran(tmp_path):
    h = ClaudeCodeHarness()
    result = h.start("do it", str(tmp_path), {"binary": str(_failing_stub(tmp_path, ZERO_USAGE))})
    assert result.ok is False and result.never_ran is True
    assert result.text == "You have hit your usage limit", "the reason, not the whole JSON"
    assert result.raw["returncode"] == 1
    assert result.session_id == "s"


def test_the_same_failure_with_a_clean_exit_never_ran_either(tmp_path):
    h = ClaudeCodeHarness()
    stub = _failing_stub(tmp_path, ZERO_USAGE, exit_code=0)
    result = h.start("do it", str(tmp_path), {"binary": str(stub)})
    assert result.ok is False and result.never_ran is True


def test_a_failed_call_that_spent_tokens_did_run(tmp_path):
    h = ClaudeCodeHarness()
    stub = _failing_stub(tmp_path, {**ZERO_USAGE, "input_tokens": 50, "output_tokens": 9})
    result = h.start("do it", str(tmp_path), {"binary": str(stub)})
    assert result.ok is False and result.never_ran is False
    assert result.text == "You have hit your usage limit"


def test_a_cli_that_died_without_json_never_ran(tmp_path):
    stub = make_stub(tmp_path, "import sys\nprint('boom', file=sys.stderr)\nsys.exit(1)\n")
    result = ClaudeCodeHarness().start("do it", str(tmp_path), {"binary": str(stub)})
    assert result.never_ran is True and "boom" in result.text


def test_a_good_call_ran(tmp_path):
    stub = make_stub(tmp_path, "import json\nprint(json.dumps({'session_id': 's', 'result': 'ok'}))\n")
    result = ClaudeCodeHarness().start("do it", str(tmp_path), {"binary": str(stub)})
    assert result.ok is True and result.never_ran is False and result.text == "ok"


def test_start_attaches_scoped_mcp_config_and_cleans_up(tmp_path):
    captured_path = {}

    stub = make_stub(tmp_path, textwrap.dedent("""\
        sid = argv[argv.index("--session-id") + 1]
        cfg_path = argv[argv.index("--mcp-config") + 1]
        strict = "--strict-mcp-config" in argv
        with open(cfg_path) as f:
            cfg_contents = f.read()
        print(json.dumps({"session_id": sid, "result": json.dumps({
            "cfg_path": cfg_path, "cfg_contents": cfg_contents, "strict": strict,
        }), "is_error": False}))
    """))
    h = ClaudeCodeHarness()
    settings = {"binary": str(stub), "mcp": {"role": "reviewer", "project_root": "/x/instance"}}
    result = h.start("review this", str(tmp_path), settings)

    payload = json.loads(result.text)
    assert payload["strict"] is True
    cfg = json.loads(payload["cfg_contents"])
    server = cfg["mcpServers"]["gitchen"]
    assert server["command"] == "gitchen"
    assert server["args"] == ["serve", "--project-root", "/x/instance", "--role", "reviewer"]

    # the temp config file must not outlive the call
    assert not os.path.exists(payload["cfg_path"])


def test_start_without_mcp_settings_passes_no_mcp_flags(tmp_path):
    stub = make_stub(tmp_path, textwrap.dedent("""\
        sid = argv[argv.index("--session-id") + 1]
        has_mcp = "--mcp-config" in argv
        print(json.dumps({"session_id": sid, "result": json.dumps({"has_mcp": has_mcp}), "is_error": False}))
    """))
    h = ClaudeCodeHarness()
    result = h.start("build this", str(tmp_path), {"binary": str(stub)})
    assert json.loads(result.text)["has_mcp"] is False


def test_start_mcp_config_includes_task_id_when_given(tmp_path):
    stub = make_stub(tmp_path, textwrap.dedent("""\
        sid = argv[argv.index("--session-id") + 1]
        cfg_path = argv[argv.index("--mcp-config") + 1]
        with open(cfg_path) as f:
            cfg = json.load(f)
        print(json.dumps({"session_id": sid, "result": json.dumps(cfg["mcpServers"]["gitchen"]["args"]), "is_error": False}))
    """))
    h = ClaudeCodeHarness()
    settings = {"binary": str(stub),
                "mcp": {"role": "reviewer", "project_root": "/x/instance", "task_id": "abc123"}}
    result = h.start("review this", str(tmp_path), settings)
    server_args = json.loads(result.text)
    assert server_args == ["serve", "--project-root", "/x/instance",
                            "--role", "reviewer", "--task-id", "abc123"]


def test_start_mcp_config_omits_task_id_when_absent(tmp_path):
    stub = make_stub(tmp_path, textwrap.dedent("""\
        sid = argv[argv.index("--session-id") + 1]
        cfg_path = argv[argv.index("--mcp-config") + 1]
        with open(cfg_path) as f:
            cfg = json.load(f)
        print(json.dumps({"session_id": sid, "result": json.dumps(cfg["mcpServers"]["gitchen"]["args"]), "is_error": False}))
    """))
    h = ClaudeCodeHarness()
    settings = {"binary": str(stub), "mcp": {"role": "builder", "project_root": "/x/instance"}}
    result = h.start("build this", str(tmp_path), settings)
    server_args = json.loads(result.text)
    assert "--task-id" not in server_args


def test_start_pre_allows_the_profiles_own_mcp_tools(tmp_path):
    """Real regression: a headless `-p` session has no human to answer a
    permission prompt, so without --allowedTools every MCP call in
    allowed_tools was silently denied regardless of permission_mode
    (found via a real dispatch run)."""
    stub = make_stub(tmp_path, textwrap.dedent("""\
        sid = argv[argv.index("--session-id") + 1]
        rest = argv[argv.index("--allowedTools") + 1:]
        allowed = []
        for v in rest:
            if v.startswith("--"):
                break
            allowed.append(v)
        print(json.dumps({"session_id": sid, "result": json.dumps(allowed), "is_error": False}))
    """))
    h = ClaudeCodeHarness()
    settings = {"binary": str(stub),
                "mcp": {"role": "pm", "project_root": "/x/instance", "tools": ["get_issue", "update_issue"]}}
    result = h.start("do it", str(tmp_path), settings)
    assert json.loads(result.text) == ["mcp__gitchen__get_issue", "mcp__gitchen__update_issue"]


def test_start_omits_allowed_tools_flag_when_no_mcp_settings(tmp_path):
    stub = make_stub(tmp_path, textwrap.dedent("""\
        sid = argv[argv.index("--session-id") + 1]
        print(json.dumps({"session_id": sid, "result": json.dumps("--allowedTools" in argv), "is_error": False}))
    """))
    h = ClaudeCodeHarness()
    result = h.start("build this", str(tmp_path), {"binary": str(stub)})
    assert json.loads(result.text) is False


def make_nono_stub(tmp_path: Path) -> Path:
    """A passthrough `nono` that asserts it was invoked as `run --profile
    <p> ... -- <real command>` (checking `run`/`--profile`/the trailing
    `--` by position, since those are fixed, but everything between them
    by lookup, since the exact set of grants there is what's under test
    elsewhere) and then just execs the real command — so the inner
    `claude` stub's own behavior still determines the result."""
    stub = tmp_path / "nono"
    stub.write_text(textwrap.dedent("""\
        #!/usr/bin/env python3
        import subprocess, sys
        argv = sys.argv[1:]
        assert argv[0] == "run" and argv[1] == "--profile", argv
        split = argv.index("--")
        sys.exit(subprocess.run(argv[split + 1:]).returncode)
    """))
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    return stub


def test_start_wraps_in_nono_and_disables_claude_code_s_own_sandbox(tmp_path, monkeypatch):
    """Real-world finding: bypassPermissions is only safe to turn on once
    something else (nono) actually contains the blast radius — and once
    it does, Claude Code's own (redundant, weaker) sandbox and its
    dangerouslyDisableSandbox escape hatch need to be turned off, or
    Claude Code is still managing a boundary nono already owns."""
    claude_stub = tmp_path / "claude"
    claude_stub.write_text(textwrap.dedent("""\
        #!/usr/bin/env python3
        import sys, json
        argv = sys.argv[1:]
        sid = argv[argv.index("--session-id") + 1]
        settings_path = argv[argv.index("--settings") + 1]
        with open(settings_path) as f:
            content = json.load(f)
        print(json.dumps({"session_id": sid,
                           "result": json.dumps({"path": settings_path, "content": content}),
                           "is_error": False}))
    """))
    claude_stub.chmod(claude_stub.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    make_nono_stub(tmp_path)

    h = ClaudeCodeHarness()
    settings = {"binary": str(claude_stub), "permission_mode": "bypassPermissions",
                "sandbox": {"profile": "nolabs-ai/claude"}}
    result = h.start("build this", str(tmp_path), settings)

    assert result.ok is True
    seen = json.loads(result.text)
    assert seen["content"] == {"sandbox": {"enabled": False},
                                "permissions": {"allowUnsandboxedCommands": False}}
    # the harness must have already cleaned the temp file up by the time start() returns
    assert not os.path.exists(seen["path"])


def test_start_passes_the_real_cwd_as_nono_workdir(tmp_path, monkeypatch):
    """Real regression: nono starts the sandboxed child in `/` unless
    told its workdir explicitly, even though `subprocess.run`'s own
    `cwd=` already chdir'd before nono was even exec'd — a real dispatch
    run hit this and the builder was denied reading its own worktree."""
    nono_stub = tmp_path / "nono"
    nono_stub.write_text(textwrap.dedent("""\
        #!/usr/bin/env python3
        import json, sys
        argv = sys.argv[1:]
        workdir = argv[argv.index("--workdir") + 1]
        print(json.dumps({"session_id": "x", "result": workdir, "is_error": False}))
    """))
    nono_stub.chmod(nono_stub.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")

    h = ClaudeCodeHarness()
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    settings = {"binary": "claude", "sandbox": {"profile": "nolabs-ai/claude"}}
    result = h.start("build this", str(worktree), settings)
    assert result.text == str(worktree)


def test_start_grants_nono_extra_allow_paths_and_expands_home(tmp_path, monkeypatch):
    """`nono.allow` is for grants the profile itself can't know about —
    a tool's own cache directory outside both the project root and the
    worktree (e.g. Playwright's browser cache)."""
    nono_stub = tmp_path / "nono"
    nono_stub.write_text(textwrap.dedent("""\
        #!/usr/bin/env python3
        import json, sys
        argv = sys.argv[1:]
        allowed = [argv[i + 1] for i, v in enumerate(argv) if v == "--allow"]
        print(json.dumps({"session_id": "x", "result": json.dumps(allowed), "is_error": False}))
    """))
    nono_stub.chmod(nono_stub.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("HOME", str(tmp_path))

    h = ClaudeCodeHarness()
    settings = {"binary": "claude", "sandbox": {"profile": "nolabs-ai/claude",
                                              "allow": ["~/Library/Caches/ms-playwright"]}}
    result = h.start("build this", str(tmp_path), settings)
    allowed = json.loads(result.text)
    assert str(tmp_path / "Library/Caches/ms-playwright") in allowed
    assert str(tmp_path) in allowed, "the workspace is always granted as well"


def test_start_allows_only_the_claude_api_plus_listed_domains(tmp_path, monkeypatch):
    """Outbound network is an allowlist: the Claude API host is always
    reachable, and a role's own allow_domains are the only additions.
    Nothing here may open the network up, or a builder could push to
    origin (github.com) on its own, which the default must prevent."""
    nono_stub = tmp_path / "nono"
    nono_stub.write_text(textwrap.dedent("""\
        #!/usr/bin/env python3
        import json, sys
        argv = sys.argv[1:]
        domains = [argv[i + 1] for i, v in enumerate(argv) if v == "--allow-domain"]
        print(json.dumps({"session_id": "x", "result": json.dumps({"domains": domains,
                          "argv": argv}), "is_error": False}))
    """))
    nono_stub.chmod(nono_stub.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")

    h = ClaudeCodeHarness()
    settings = {"binary": "claude", "sandbox": {"profile": "nolabs-ai/claude",
                                              "allow_domains": ["registry.npmjs.org"]}}
    seen = json.loads(h.start("build this", str(tmp_path), settings).text)
    assert seen["domains"] == ["api.anthropic.com", "registry.npmjs.org"]
    assert "--allow-net" not in seen["argv"]
    assert "--block-net" not in seen["argv"]


def test_start_with_no_allow_domains_still_reaches_only_the_claude_api(tmp_path, monkeypatch):
    nono_stub = tmp_path / "nono"
    nono_stub.write_text(textwrap.dedent("""\
        #!/usr/bin/env python3
        import json, sys
        argv = sys.argv[1:]
        domains = [argv[i + 1] for i, v in enumerate(argv) if v == "--allow-domain"]
        print(json.dumps({"session_id": "x", "result": json.dumps(domains), "is_error": False}))
    """))
    nono_stub.chmod(nono_stub.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")

    h = ClaudeCodeHarness()
    result = h.start("build this", str(tmp_path), {"binary": "claude", "sandbox": {"profile": "nolabs-ai/claude"}})
    assert json.loads(result.text) == ["api.anthropic.com"]


def test_start_grants_nono_read_access_to_gitchen_s_own_root(tmp_path, monkeypatch):
    """Real regression: `gitchen` (invoked for --mcp-config) is a script
    inside this very checkout — outside both the worktree and the
    project root. nono's own denial message named the exact fix:
    `gitchen serve` couldn't read its own source, so the MCP server died
    before it could start, and Claude Code only ever saw a dead
    connection with nothing pointing at why."""
    nono_stub = tmp_path / "nono"
    nono_stub.write_text(textwrap.dedent("""\
        #!/usr/bin/env python3
        import json, sys
        argv = sys.argv[1:]
        read_path = argv[argv.index("--read") + 1]
        print(json.dumps({"session_id": "x", "result": read_path, "is_error": False}))
    """))
    nono_stub.chmod(nono_stub.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")

    h = ClaudeCodeHarness()
    settings = {"binary": "claude", "sandbox": {"profile": "nolabs-ai/claude"}}
    result = h.start("build this", str(tmp_path), settings)

    from mcp.harnesses.sandbox import _GITCHEN_ROOT
    assert result.text == str(_GITCHEN_ROOT)


def test_the_mcp_proxy_needs_project_yml_and_the_socket_not_the_instance(tmp_path, monkeypatch):
    """A turn's MCP server only forwards each call to the daemon's control
    socket, so it needs its own project.yml and the socket directory, not the
    instance repo. The old grant of the whole instance repo was there because
    the server used to read issues itself; it is no longer needed, and a turn
    that could write there could rewrite the roles and policy it runs under."""
    nono_stub = tmp_path / "nono"
    nono_stub.write_text(textwrap.dedent("""\
        #!/usr/bin/env python3
        import json, sys
        argv = sys.argv[1:]
        def every(flag):
            return [argv[i + 1] for i, v in enumerate(argv) if v == flag]
        print(json.dumps({"session_id": "x",
                           "result": json.dumps({"allow": every("--allow"),
                                                 "read_file": every("--read-file"),
                                                 "socket_dir": every("--allow-unix-socket-dir"),
                                                 "workdir": argv[argv.index("--workdir") + 1]}),
                           "is_error": False}))
    """))
    nono_stub.chmod(nono_stub.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")

    h = ClaudeCodeHarness()
    root = tmp_path / "instance"
    worktree = root / ".agents-daemon" / "worktrees" / "1"
    worktree.mkdir(parents=True)
    settings = {"binary": "claude", "sandbox": {"profile": "nolabs-ai/claude"},
                "mcp": {"role": "pm", "project_root": str(root), "tools": ["get_issue"]}}
    seen = json.loads(h.start("build this", str(worktree), settings).text)
    assert seen["allow"] == [str(worktree)], "the workspace is the only writable project path"
    assert str(root / "project.yml") in seen["read_file"]
    assert seen["socket_dir"] == [str(root / ".agents-daemon")]
    assert seen["workdir"] == str(worktree)


def test_start_without_nono_passes_no_settings_override(tmp_path):
    stub = make_stub(tmp_path, textwrap.dedent("""\
        sid = argv[argv.index("--session-id") + 1]
        print(json.dumps({"session_id": sid, "result": json.dumps("--settings" in argv), "is_error": False}))
    """))
    h = ClaudeCodeHarness()
    result = h.start("build this", str(tmp_path), {"binary": str(stub)})
    assert json.loads(result.text) is False


def test_start_reaps_servers_the_turn_left_running(tmp_path):
    """Real problem: an agent's dev server or Playwright webServer that
    outlives its turn keeps its port bound, so the next turn fails with
    "port in use" or runs against stale code, and nothing reports it. Two
    cases: a child in the turn's own process group, and one that detached
    with setsid (which a process group kill alone would miss). Both must
    be gone by the time start() returns. The children inherit stdout too,
    which used to make subprocess.run block until they exited. The turn
    runs in a linked worktree, the only kind of directory swept."""
    import time as _time
    main = tmp_path / "main"
    main.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=main, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
                    "--allow-empty", "-m", "init"], cwd=main, check=True)
    worktree = tmp_path / "wt"
    subprocess.run(["git", "worktree", "add", "-q", str(worktree), "-b", "issue/9"],
                   cwd=main, check=True)

    pids_file = tmp_path / "pids"
    stub = make_stub(tmp_path, textwrap.dedent(f"""\
        import subprocess
        import os
        in_group = subprocess.Popen(["sleep", "300"])
        detached = subprocess.Popen(["sleep", "301"], start_new_session=True)
        with open({str(pids_file)!r}, "w") as f:
            f.write(f"{{in_group.pid}} {{detached.pid}}")
        sid = argv[argv.index("--session-id") + 1]
        print(json.dumps({{"session_id": sid, "result": "ok", "is_error": False}}))
    """))
    h = ClaudeCodeHarness()
    result = h.start("do it", str(worktree), {"binary": str(stub)})
    assert result.ok is True

    in_group_pid, detached_pid = (int(p) for p in pids_file.read_text().split())
    for pid in (in_group_pid, detached_pid):
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            _time.sleep(0.1)
        else:
            raise AssertionError(f"process {pid} outlived its turn")


def test_reap_never_sweeps_a_main_checkout(tmp_path):
    """The sweep only applies to linked worktrees. A main checkout is a
    real working copy, and its processes may belong to the user."""
    main = tmp_path / "main"
    main.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=main, check=True)
    assert process.is_linked_worktree(str(main)) is False


def test_sweep_matches_a_process_whose_cwd_is_reached_through_a_symlink(tmp_path):
    """macOS reports a process's working directory with symlinks resolved
    (/tmp is /private/tmp), so a sweep root given through the symlink must
    still match. Real regression: a server left behind by a turn was not
    reaped because its cwd was reported as /private/tmp/... and the root
    was /tmp/..."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    proc = subprocess.Popen(["sleep", "302"], cwd=real)
    try:
        assert proc.pid in process.pids_with_cwd_under(str(link))
    finally:
        proc.kill()
        proc.wait()


def test_start_passes_each_read_path_to_nono(tmp_path, monkeypatch):
    nono_stub = tmp_path / "nono"
    nono_stub.write_text(textwrap.dedent("""\
        #!/usr/bin/env python3
        import json, sys
        argv = sys.argv[1:]
        reads = [argv[i + 1] for i, v in enumerate(argv) if v == "--read"]
        print(json.dumps({"session_id": "x", "result": json.dumps(reads), "is_error": False}))
    """))
    nono_stub.chmod(nono_stub.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    h = ClaudeCodeHarness()
    result = h.start("build", str(tmp_path), {"binary": "claude",
                     "sandbox": {"profile": "nolabs-ai/claude", "read": ["/some/repo"]}})
    assert "/some/repo" in json.loads(result.text)


def test_usage_is_read_per_model_and_marked_cumulative(tmp_path):
    payload = {"session_id": "s", "result": "ok", "is_error": False, "total_cost_usd": 0.5,
               "usage": {"input_tokens": 9, "output_tokens": 3,
                         "cache_creation_input_tokens": 4, "cache_read_input_tokens": 7},
               "modelUsage": {"claude-haiku-5-5": {"costUSD": 0.2, "inputTokens": 5, "outputTokens": 2,
                                                   "cacheReadInputTokens": 6,
                                                   "cacheCreationInputTokens": 1},
                              "claude-sonnet-5-5": {"costUSD": 0.3, "inputTokens": 4, "outputTokens": 1,
                                                    "cacheReadInputTokens": 1,
                                                    "cacheCreationInputTokens": 3}}}
    stub = make_stub(tmp_path, f"print(json.dumps({payload!r}))\n")
    result = ClaudeCodeHarness().start("go", str(tmp_path), {"binary": str(stub)})
    assert result.usage_cumulative is True
    by_model = {r["model"]: r for r in result.usage}
    assert by_model["claude-haiku-5-5"]["usd"] == 0.2
    assert by_model["claude-sonnet-5-5"]["input"] == 4
    assert by_model["claude-sonnet-5-5"]["cache_write"] == 3


def test_without_per_model_detail_the_configured_model_carries_the_total(tmp_path):
    payload = {"session_id": "s", "result": "ok", "is_error": False, "total_cost_usd": 0.5,
               "usage": {"input_tokens": 9, "output_tokens": 3}}
    stub = make_stub(tmp_path, f"print(json.dumps({payload!r}))\n")
    result = ClaudeCodeHarness().start("go", str(tmp_path),
                                       {"binary": str(stub), "model": "claude-haiku-5-5"})
    assert result.usage == [{"model": "claude-haiku-5-5", "usd": 0.5, "input": 9, "output": 3,
                             "cache_read": 0, "cache_write": 0}]


def test_a_failed_call_that_spent_nothing_records_no_usage(tmp_path):
    stub = _failing_stub(tmp_path, ZERO_USAGE)
    result = ClaudeCodeHarness().start("go", str(tmp_path), {"binary": str(stub)})
    assert result.usage == [] or all(r["usd"] == 0 for r in result.usage)
