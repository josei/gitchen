"""Program roles: a role whose prompt names a script runs that script, not a
model. It reaches the gitchen tools through `gitchen call`, with the same
permissions an agent has, and a scheduled program may run with no issue at all."""

import json
import os
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mcp import control, dispatch, holders, issues, jobs, schedules  # noqa: E402
from mcp.config import ProjectConfig  # noqa: E402
from mcp.harnesses.program import ProgramHarness  # noqa: E402


def script(path: Path, body: str) -> Path:
    path.write_text("#!/usr/bin/env python3\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def make_project(script_body: str, roles_extra=None, allowed=None, schedule=None) -> ProjectConfig:
    base = Path(tempfile.mkdtemp(prefix="pr", dir="/tmp"))
    root = base / "i"
    (root / "agents").mkdir(parents=True)
    (root / "scripts").mkdir()
    prog = script(root / "scripts" / "collector.py", script_body)
    (root / "agents" / "collector.md").write_text(
        f"---\nname: collector\ndescription: collects\nrun: scripts/{prog.name}\n"
        "outcomes: {reported: the notes are filed}\n---\ncollects")
    (root / "agents" / "builder.md").write_text("---\nname: builder\ndescription: b\n---\nbuild")
    (root / "agents" / "pm.md").write_text("---\nname: pm\ndescription: p\n---\npm")
    public = base / "public"
    public.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=public, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
                    "--allow-empty", "-m", "init"], cwd=public, check=True)
    data = {"public_repo": "../public", "allowed_tools": allowed or {"collector": ["list_issues", "create_issue"]},
            "transitions": {"collector": {"reported": "return"}}}
    if roles_extra:
        data.update(roles_extra)
    (root / "project.yml").write_text(yaml.safe_dump(data))
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    if schedule is not None:
        (root / "schedules").mkdir()
        (root / "schedules" / "collect.md").write_text(schedule)
    return ProjectConfig.load(root)


def test_a_program_gets_its_prompt_on_stdin_and_its_output_is_the_report(tmp_path):
    cfg = make_project("import sys\nprint('heard: ' + sys.stdin.read().strip())\n")
    result = ProgramHarness().start("collect the feed", str(cfg.project_root), {
        "program": str(cfg.project_root / "scripts" / "collector.py"), "env": {**os.environ}})
    assert result.ok is True and result.text == "heard: collect the feed"
    assert result.usage == [], "a program spends no model tokens, so it records no cost"


def test_a_program_that_fails_is_a_failed_turn_not_a_silent_one(tmp_path):
    cfg = make_project("import sys\nsys.exit(3)\n")
    result = ProgramHarness().start("x", str(cfg.project_root), {
        "program": str(cfg.project_root / "scripts" / "collector.py"), "env": {**os.environ}})
    assert result.ok is False


def test_an_issue_turn_of_a_program_role_ends_with_its_directive(tmp_path):
    cfg = make_project("print('Filed the notes.')\nprint('NEXT: reported')\n")
    issue = issues.create_issue(cfg, "collect", "b", None, 0, "pm", "pm", assignee="collector")
    for t in dispatch.tick(cfg):
        t.join(20)
    final = issues.get_issue(cfg, issue["id"])
    assert final["assignee"] == "pm", "reported returned it to whoever delegated it"
    text = "\n".join(line["text"] for line in final["lines"])
    assert "Filed the notes." in text and "NEXT: reported" not in text


def test_a_job_holds_a_slot_under_its_own_key_and_is_authorized_like_a_role(tmp_path):
    cfg = make_project("print('quiet')\n")
    assert holders.claim(cfg.project_root, "job-collect", "task-job", os.getpid(), "collector")
    control.serve_in_background(cfg)
    try:
        created = control.call(cfg.project_root, "task-job", "create_issue", {"title": "from a job", "body": "b"})
        assert created["title"] == "from a job"
        with __import__("pytest").raises(control.ControlError, match="not permitted"):
            control.call(cfg.project_root, "task-job", "comment_issue", {"id": created["id"], "text": "x"})
    finally:
        holders.release(cfg.project_root, "job-collect", "task-job")


def test_a_scheduled_program_with_no_issue_writes_nothing_when_it_has_nothing_to_do(tmp_path):
    schedule = "---\ncron: \"* * * * *\"\nrun_role: collector\n---\ncheck the feed\n"
    cfg = make_project("print('nothing new')\n", schedule=schedule)
    head_before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=cfg.project_root,
                                 capture_output=True, text=True).stdout.strip()
    before = issues.list_issues(cfg)
    threads = jobs.start_scheduled(cfg)
    for t in threads:
        t.join(20)
    assert issues.list_issues(cfg) == before, "no issue was created"
    head_after = subprocess.run(["git", "rev-parse", "HEAD"], cwd=cfg.project_root,
                                capture_output=True, text=True).stdout.strip()
    assert head_after == head_before, "a quiet run commits nothing"
    assert holders.current(cfg.project_root, "job-collect") is None, "the slot is released"


def test_gitchen_call_is_how_a_program_reaches_the_tools(tmp_path):
    """The command a program runs, end to end: it reads the task from its
    environment and is answered by the daemon's control socket."""
    cfg = make_project("print('quiet')\n")
    assert holders.claim(cfg.project_root, "job-collect", "task-cli", os.getpid(), "collector")
    control.serve_in_background(cfg)
    env = {**os.environ, "GITCHEN_TASK_ID": "task-cli", "GITCHEN_PROJECT_ROOT": str(cfg.project_root)}
    gitchen = Path(__file__).resolve().parent.parent / "bin" / "gitchen"
    try:
        run = subprocess.run([sys.executable, str(gitchen), "call", "create_issue",
                              json.dumps({"title": "from the command line", "body": "b"})],
                             env=env, capture_output=True, text=True, timeout=60)
        assert run.returncode == 0, run.stderr
        assert json.loads(run.stdout)["title"] == "from the command line"
        bare = subprocess.run([sys.executable, str(gitchen), "call", "list_issues"],
                              env={k: v for k, v in env.items() if k != "GITCHEN_TASK_ID"},
                              capture_output=True, text=True, timeout=60)
        assert bare.returncode == 2 and "GITCHEN_TASK_ID" in bare.stderr
    finally:
        holders.release(cfg.project_root, "job-collect", "task-cli")


def test_a_program_whose_script_is_missing_or_not_executable_is_refused_at_load():
    # Found at load, not on the first turn, which would otherwise fail every tick.
    cfg = make_project("print('ok')\n")
    agent = cfg.project_root / "agents" / "collector.md"
    shell = cfg.project_root / "scripts" / "collector.sh"
    shell.write_text("echo ok\n")
    shell.chmod(0o644)
    agent.write_text(agent.read_text().replace("scripts/collector.py", "scripts/collector.sh"))
    with pytest.raises(ValueError, match="collector.*executable"):
        ProjectConfig.load(cfg.project_root)
    shell.chmod(0o755)
    ProjectConfig.load(cfg.project_root)  # a .sh that is executable is fine
    agent.write_text(agent.read_text().replace("scripts/collector.sh", "scripts/gone.py"))
    with pytest.raises(ValueError, match="collector.*no script"):
        ProjectConfig.load(cfg.project_root)


def test_a_failed_program_reports_its_stderr_not_the_banner_on_stdout():
    cfg = make_project("import sys\nprint('banner on stdout')\nprint('the real reason', file=sys.stderr)\nsys.exit(1)\n")
    result = ProgramHarness().start("x", str(cfg.project_root), {
        "program": str(cfg.project_root / "scripts" / "collector.py"), "env": {**os.environ}})
    assert result.ok is False
    assert "the real reason" in result.text, result.text
