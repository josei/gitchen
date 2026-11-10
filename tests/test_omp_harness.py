"""Tests the omp harness adapter against a stub `omp` binary, so no real
CLI, model, or network is involved. The stub speaks omp's `--mode json`
event stream and reports back the argv and working-directory state it saw.
"""

import json
import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mcp.harnesses.omp import OmpHarness  # noqa: E402

STUB = textwrap.dedent("""\
    #!/usr/bin/env python3
    import json, os, sys
    argv = sys.argv[1:]
    payload = {"argv": argv, "cwd": os.getcwd()}
    mcp_path = os.path.join(os.getcwd(), ".omp", "mcp.json")
    if os.path.exists(mcp_path):
        payload["mcp"] = open(mcp_path).read()
    print(json.dumps({"type": "session", "id": "sess-1"}))
    print(json.dumps({"type": "agent_end", "isTerminal": True, "messages": [
        {"role": "user", "content": [{"type": "text", "text": "go"}]},
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "considering"},
            {"type": "text", "text": json.dumps(payload)}]}]}))
""")


def make_stub(tmp_path: Path) -> Path:
    stub = tmp_path / "omp"
    stub.write_text(STUB)
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    return stub


def git_repo(path: Path) -> None:
    path.mkdir(exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)


def test_start_passes_model_thinking_and_the_prompt_after_a_separator(tmp_path):
    h = OmpHarness()
    result = h.start("build this", str(tmp_path), {
        "binary": str(make_stub(tmp_path)), "model": "some/model", "thinking": "high"})

    payload = json.loads(result.text)
    argv = payload["argv"]
    assert argv[:2] == ["-p", "--mode"]
    assert "json" in argv
    assert "--auto-approve" in argv
    assert argv[argv.index("--model") + 1] == "some/model"
    assert argv[argv.index("--thinking") + 1] == "high"
    # everything after `--` is message text, never a flag
    assert argv[-2:] == ["--", "build this"]
    assert result.session_id == "sess-1"
    assert result.ok is True


def test_start_scopes_mcp_to_the_role_and_cleans_the_file_up(tmp_path):
    git_repo(tmp_path)
    h = OmpHarness()
    result = h.start("build this", str(tmp_path), {
        "binary": str(make_stub(tmp_path)),
        "mcp": {"role": "builder", "project_root": "/x/instance", "task_id": "abc123"}})

    payload = json.loads(result.text)
    config = json.loads(payload["mcp"])
    server = config["mcpServers"]["gitchen"]
    assert server["command"] == "gitchen"
    assert server["args"] == ["serve", "--project-root", "/x/instance",
                              "--role", "builder", "--task-id", "abc123"]
    assert not (tmp_path / ".omp" / "mcp.json").exists(), \
        "the scoped server definition must not outlive the turn"
    assert not (tmp_path / ".omp").exists(), "the directory this turn created should go too"


def test_start_keeps_the_mcp_file_out_of_git_status(tmp_path):
    """The file lives in the worktree for the duration of the turn, so an
    agent that stages everything must not be able to commit it."""
    git_repo(tmp_path)
    (tmp_path / "README.md").write_text("hi\n")
    h = OmpHarness()
    h.start("build this", str(tmp_path), {
        "binary": str(make_stub(tmp_path)),
        "mcp": {"role": "builder", "project_root": "/x/instance"}})

    exclude = (tmp_path / ".git" / "info" / "exclude").read_text()
    assert ".omp/" in exclude.splitlines()
    # idempotent: a second turn must not append it again
    h.start("build this", str(tmp_path), {
        "binary": str(make_stub(tmp_path)),
        "mcp": {"role": "builder", "project_root": "/x/instance"}})
    assert (tmp_path / ".git" / "info" / "exclude").read_text().count(".omp/") == 1


def test_resume_passes_the_session_id_and_keeps_it_when_the_stream_omits_it(tmp_path):
    stub = tmp_path / "omp"
    stub.write_text(textwrap.dedent("""\
        #!/usr/bin/env python3
        import json, sys
        print(json.dumps({"type": "agent_end", "isTerminal": True, "messages": [
            {"role": "assistant", "content": [{"type": "text", "text": json.dumps(sys.argv[1:])}]}]}))
    """))
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)

    result = OmpHarness().resume("sess-9", "fix it", str(tmp_path), {"binary": str(stub)})

    argv = json.loads(result.text)
    assert argv[argv.index("--resume") + 1] == "sess-9"
    assert result.session_id == "sess-9"
    assert result.ok is True


def test_a_run_without_agent_end_is_a_failure_even_with_exit_zero(tmp_path):
    """omp exits 0 on some failures (an unknown model, for one) and prints
    the reason to stderr; the absence of agent_end is the real signal."""
    stub = tmp_path / "omp"
    stub.write_text("#!/usr/bin/env python3\nimport sys\nprint('Model \"nope\" not found', file=sys.stderr)\n")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)

    result = OmpHarness().start("build this", str(tmp_path), {"binary": str(stub)})

    assert result.ok is False
    assert "not found" in result.text


def test_a_terminal_false_agent_end_is_a_failure(tmp_path):
    stub = tmp_path / "omp"
    stub.write_text(textwrap.dedent("""\
        #!/usr/bin/env python3
        import json
        print(json.dumps({"type": "agent_end", "isTerminal": False, "messages": []}))
    """))
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)

    result = OmpHarness().start("build this", str(tmp_path), {"binary": str(stub)})

    assert result.ok is False
    assert result.text.strip()


def _stub_printing(tmp_path, body):
    stub = tmp_path / "omp"
    stub.write_text("#!/usr/bin/env python3\nimport json\n" + body)
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    return stub


def test_a_run_that_reached_a_terminal_end_ran(tmp_path):
    result = OmpHarness().start("go", str(tmp_path), {"binary": str(make_stub(tmp_path))})
    assert result.ok is True and result.never_ran is False


def test_a_run_without_agent_end_never_ran(tmp_path):
    stub = _stub_printing(tmp_path, "print('unknown model')\n")
    result = OmpHarness().start("go", str(tmp_path), {"binary": str(stub)})
    assert result.ok is False and result.never_ran is True


def test_a_terminal_false_end_never_ran(tmp_path):
    stub = _stub_printing(tmp_path, "print(json.dumps({'type': 'agent_end', 'isTerminal': False, 'messages': []}))\n")
    result = OmpHarness().start("go", str(tmp_path), {"binary": str(stub)})
    assert result.ok is False and result.never_ran is True


def test_usage_is_read_for_every_assistant_message_by_model(tmp_path):
    body = (
        "print(json.dumps({'type': 'session', 'id': 'sess-9'}))\n"
        "print(json.dumps({'type': 'agent_end', 'isTerminal': True, 'messages': ["
        " {'role': 'user', 'content': []},"
        " {'role': 'assistant', 'model': 'm-a', 'content': [{'type': 'text', 'text': 'one'}],"
        "  'usage': {'input': 100, 'output': 10, 'cacheRead': 5, 'cacheWrite': 2,"
        "            'cost': {'total': 0.25}}},"
        " {'role': 'assistant', 'content': [{'type': 'text', 'text': 'two'}],"
        "  'usage': {'input': 50, 'output': 4, 'cacheRead': 0, 'cacheWrite': 0,"
        "            'cost': {'total': 0.5}}}]}))\n"
    )
    stub = _stub_printing(tmp_path, body)
    result = OmpHarness().start("go", str(tmp_path), {"binary": str(stub), "model": "m-config"})
    assert result.usage_cumulative is False
    by_model = {r["model"]: r for r in result.usage}
    assert by_model["m-a"] == {"model": "m-a", "usd": 0.25, "input": 100, "output": 10,
                               "cache_read": 5, "cache_write": 2}, "a model named on the message wins"
    assert by_model["m-config"] == {"model": "m-config", "usd": 0.5, "input": 50, "output": 4,
                                    "cache_read": 0, "cache_write": 0}
