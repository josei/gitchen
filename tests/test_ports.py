"""Each turn gets its own port range, so two turns serving a product at the same
time don't fight over one port (and a test server never tests another turn's code)."""

import json
import os
import stat
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mcp import dispatch  # noqa: E402
from mcp.harnesses.claude_code import ClaudeCodeHarness  # noqa: E402
from mcp.harnesses.omp import OmpHarness  # noqa: E402


def port_stub(tmp_path, name):
    stub = tmp_path / name
    stub.write_text(textwrap.dedent("""\
        #!/usr/bin/env python3
        import json, os, sys
        print(json.dumps({"session_id": "s", "result": os.environ.get("PORT", "none"),
                          "is_error": False, "type": "result", "isTerminal": True,
                          "messages": [{"role": "assistant", "content": [{"type": "text",
                          "text": os.environ.get("PORT", "none")}]}]}))
    """))
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    return stub


def test_the_port_of_an_issue_is_stable_and_spaced_apart():
    assert dispatch._port_for(1) != dispatch._port_for(2)
    assert dispatch._port_for(2) - dispatch._port_for(1) == 10, "room for the nine ports after it"
    assert 20000 <= dispatch._port_for(3999) < 60000


def test_a_claude_turn_is_started_with_its_port(tmp_path):
    stub = port_stub(tmp_path, "claude")
    result = ClaudeCodeHarness().start("go", str(tmp_path), {
        "binary": str(stub), "env": {**os.environ, "PORT": "23450"}})
    assert result.text == "23450"


def test_an_omp_turn_is_started_with_its_port(tmp_path):
    stub = tmp_path / "omp"
    stub.write_text(textwrap.dedent("""\
        #!/usr/bin/env python3
        import json, os
        print(json.dumps({"type": "session", "id": "s"}))
        print(json.dumps({"type": "agent_end", "isTerminal": True, "messages": [
            {"role": "assistant", "content": [{"type": "text", "text": os.environ.get("PORT", "none")}]}]}))
    """))
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    result = OmpHarness().start("go", str(tmp_path), {"binary": str(stub), "env": {**os.environ, "PORT": "23460"}})
    assert result.text == "23460"


def test_the_workspace_prompt_tells_a_turn_its_ports(tmp_path):
    from tests.test_dispatch import make_project
    cfg = make_project(tmp_path)
    issue = {"id": 7, "title": "t", "base_branch": "main", "lines": [{"text": "body"}],
             "assignment_history": []}
    prompt = dispatch._build_prompt(cfg, "builder", issue, {"dir": str(tmp_path), "url": "/s"})
    assert "PORT" in prompt and "nine ports" in prompt
