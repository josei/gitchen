"""The git grants from dispatch, exercised under real nono. Skipped when nono
or the profile isn't installed. The scratch repo lives under the home
directory on purpose: the gitchen profile allows temporary folders, so a
test under /tmp could pass for the wrong reason. Results are checked from the
host side, never by trusting what the sandboxed process reports."""

import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mcp import dispatch  # noqa: E402
from mcp.harnesses import sandbox  # noqa: E402

PROFILE = os.environ.get("GITCHEN_TEST_PROFILE", "gitchen")
pytestmark = pytest.mark.skipif(shutil.which("nono") is None, reason="needs nono")


@pytest.fixture
def scratch():
    root = Path.home() / f".gitchen-sandbox-test-{uuid.uuid4().hex[:8]}"
    root.mkdir()
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _git(cwd, *args):
    return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
                          cwd=cwd, capture_output=True, text=True)


def _worktree(scratch):
    public = scratch / "public"
    public.mkdir()
    _git(public, "init", "-q", "-b", "main")
    _git(public, "commit", "-q", "--allow-empty", "-m", "init")
    worktree = scratch / "wt"
    _git(public, "worktree", "add", "-q", str(worktree), "-b", "issue/1")
    return public, worktree


SCRIPT = r'''
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
echo work >> f.txt
git add f.txt >/dev/null 2>&1; a=$?
git commit -qm work >/dev/null 2>&1; c=$?
git config core.fsmonitor "touch MARKER" >/dev/null 2>&1; f=$?
git update-ref refs/heads/main HEAD >/dev/null 2>&1; m=$?
touch "GITDIR/hooks/post-merge" 2>/dev/null; h=$?   # touch, not `:`: a failed redirect on a special builtin ends the shell
echo "RESULT add=$a commit=$c config=$f main=$m hook=$h"
'''


def test_a_commit_succeeds_and_the_shared_repo_stays_as_it_was(scratch):
    public, worktree = _worktree(scratch)
    common = public / ".git"
    shared = scratch / "shared"
    shared.mkdir()
    grant = dispatch._sandbox_with_git_grants({"profile": PROFILE}, worktree, shared)
    prefix = sandbox.prefix({"sandbox": grant}, str(worktree))
    script = SCRIPT.replace("MARKER", str(scratch / "pwned")).replace("GITDIR", str(common))
    before_main = _git(public, "rev-parse", "main").stdout.strip()

    run = subprocess.run(prefix + ["/bin/sh", "-c", script], cwd=worktree,
                         capture_output=True, text=True, timeout=120)
    result = run.stdout + run.stderr
    line = next((l for l in result.splitlines() if l.startswith("RESULT")), "")
    assert line, f"nono produced no result:\n{result[-800:]}"
    fields = dict(part.split("=") for part in line.split()[1:])

    assert fields["add"] == "0" and fields["commit"] == "0", "the work itself must commit"
    assert fields["config"] != "0", "git config must not be writable"
    assert fields["main"] != "0", "other branches must not be writable"
    assert fields["hook"] != "0", "git hooks must not be writable"
    # host-side: what actually changed, not what the sandbox reported
    assert _git(public, "rev-parse", "main").stdout.strip() == before_main
    assert _git(public, "config", "core.fsmonitor").returncode != 0
    assert not (scratch / "pwned").exists()
    assert not (common / "hooks" / "post-merge").exists()
    assert _git(worktree, "rev-list", "--count", "main..HEAD").stdout.strip() == "1"


GITCHEN_ROOT = Path(__file__).resolve().parent.parent


def test_a_turns_mcp_server_starts_and_answers_under_its_own_grants(scratch):
    """Regression: the turn's MCP server read every role's prompt to check the
    transitions table, which its sandbox does not allow, so it died at start and
    the role had no tools. It must start with only the grants a turn gets."""
    import json
    instance = scratch / "instance"
    (instance / "agents").mkdir(parents=True)
    for role in ("builder", "reviewer", "pm"):
        (instance / "agents" / f"{role}.md").write_text(
            f"---\nname: {role}\ndescription: {role}\noutcomes: {{done: ok}}\n---\n{role}")
    public = scratch / "public"
    public.mkdir()
    _git(public, "init", "-q", "-b", "main")
    _git(public, "commit", "-q", "--allow-empty", "-m", "init")
    (instance / "project.yml").write_text(
        "public_repo: ../public\n"
        "allowed_tools:\n  pm: [list_issues, get_issue]\n"
        "transitions:\n  builder: {done: return}\n")
    _git(instance, "init", "-q")
    worktree = instance / ".agents-daemon" / "worktrees" / "1"
    worktree.mkdir(parents=True)

    settings = {"sandbox": {"profile": PROFILE},
                "mcp": {"role": "pm", "project_root": str(instance), "task_id": "probe0001",
                        "tools": ["list_issues", "get_issue"]}}
    argv = sandbox.prefix(settings, str(worktree)) + [
        sys.executable, str(GITCHEN_ROOT / "bin" / "gitchen"), "serve",
        "--project-root", str(instance), "--role", "pm", "--task-id", "probe0001"]
    request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n"
    run = subprocess.run(argv, input=request, capture_output=True, text=True,
                         cwd=worktree, timeout=120)
    assert '"tools"' in run.stdout, f"the server did not answer:\n{run.stderr[-800:]}"
    assert "list_issues" in run.stdout


def test_a_program_role_runs_under_the_real_sandbox(scratch):
    """Regression: a program's script is one file, which the sandbox must grant
    with --read-file. The unit tests run without a sandbox, so only this catches it."""
    from mcp.harnesses.program import ProgramHarness
    script = scratch / "collector.py"
    script.write_text("import sys\nprint('ran: ' + sys.stdin.read().strip())\n")
    settings = {"sandbox": {"profile": PROFILE, "read": [str(script)]},
                "program": str(script), "env": dict(os.environ)}
    result = ProgramHarness().start("collect now", str(scratch), settings)
    assert result.ok is True, result.text[-600:]
    assert result.text == "ran: collect now"
