import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mcp import issues, schedules  # noqa: E402
from mcp.config import ProjectConfig  # noqa: E402


def test_cron_matches_every_minute():
    assert schedules.cron_matches("* * * * *", datetime(2026, 1, 1, 13, 37))


def test_cron_matches_hourly_on_the_hour():
    assert schedules.cron_matches("0 * * * *", datetime(2026, 1, 1, 13, 0))
    assert not schedules.cron_matches("0 * * * *", datetime(2026, 1, 1, 13, 1))


def make_project(tmp_path: Path) -> ProjectConfig:
    root = tmp_path / "instance"
    root.mkdir(parents=True)
    (tmp_path / "public").mkdir()
    (root / "project.yml").write_text("public_repo: ../public\n")
    (root / "agents").mkdir()
    for role in ("pm", "builder"):
        (root / "agents" / f"{role}.md").write_text(f"---\nname: {role}\n---\n{role}")
    subprocess.run(["git", "init", "-q"], cwd=root)
    return ProjectConfig.load(root)


def test_load_schedules_reads_schedules_dir(tmp_path):
    cfg = make_project(tmp_path)
    schedules_dir = cfg.project_root / "schedules"
    schedules_dir.mkdir()
    (schedules_dir / "weekly-review.md").write_text(
        "---\ncron: \"0 9 * * 1\"\nassignee: pm\nlabels: [review]\npriority: 5\n---\n"
        "Review everything closed or blocked in the past week.\n"
    )

    loaded = schedules.load_schedules(cfg.project_root)

    assert len(loaded) == 1
    sched = loaded[0]
    assert sched.name == "weekly-review"
    assert sched.cron == "0 9 * * 1"
    assert sched.assignee == "pm"
    assert sched.labels == ["review"]
    assert sched.priority == 5
    assert sched.body == "Review everything closed or blocked in the past week."


def test_tick_creates_an_issue_when_due(tmp_path):
    cfg = make_project(tmp_path)
    schedules_dir = cfg.project_root / "schedules"
    schedules_dir.mkdir()
    (schedules_dir / "review.md").write_text(
        "---\ncron: \"* * * * *\"\nassignee: pm\n---\nDo the periodic review.\n"
    )
    now = datetime(2026, 1, 1, 13, 37)

    fired = schedules.tick(cfg, now)

    assert len(fired) == 1
    assert fired[0]["assignee"] == "pm"
    all_open = issues.list_issues(cfg, state="open")
    assert len(all_open) == 1
    assert all_open[0]["assignee"] == "pm"
    state = schedules.read_state(cfg.project_root)
    assert state["review"] == "2026-01-01 13:37", "the minute key is the last-fired record"
    assert state["issue:review"] == fired[0]["id"], "the issue key is the no-stacking record"


def test_tick_does_not_refire_within_the_same_minute(tmp_path):
    cfg = make_project(tmp_path)
    schedules_dir = cfg.project_root / "schedules"
    schedules_dir.mkdir()
    (schedules_dir / "review.md").write_text(
        "---\ncron: \"* * * * *\"\nassignee: pm\n---\nDo the periodic review.\n"
    )
    now = datetime(2026, 1, 1, 13, 37)

    schedules.tick(cfg, now)
    schedules.tick(cfg, now)  # same minute again

    assert len(issues.list_issues(cfg, state="open")) == 1

    # The next minute files again once the first has closed (an unfinished one
    # is never stacked: see the test below on waiting).
    first = issues.list_issues(cfg, state="open")[0]
    issues.update_issue(cfg, first["id"], issues.get_issue(cfg, first["id"])["hash"],
                        {"state": "done"}, "pm", "pm")
    later = datetime(2026, 1, 1, 13, 38)
    schedules.tick(cfg, later)
    assert len(issues.list_issues(cfg, state="open")) == 1
    assert len(issues.list_issues(cfg)) == 2


def test_tick_does_nothing_when_not_due(tmp_path):
    cfg = make_project(tmp_path)
    schedules_dir = cfg.project_root / "schedules"
    schedules_dir.mkdir()
    (schedules_dir / "weekly.md").write_text(
        "---\ncron: \"0 9 * * 1\"\nassignee: pm\n---\nWeekly review.\n"
    )
    not_monday_9am = datetime(2026, 1, 1, 13, 37)

    fired = schedules.tick(cfg, not_monday_9am)

    assert fired == []
    assert issues.list_issues(cfg, state="open") == []


def test_tick_is_fine_with_no_schedules_dir(tmp_path):
    cfg = make_project(tmp_path)
    assert schedules.tick(cfg, datetime(2026, 1, 1, 13, 37)) == []


def test_a_schedule_naming_something_that_is_not_a_role_does_not_stop_the_loop(tmp_path, capsys):
    cfg = make_project(tmp_path)
    (cfg.project_root / "schedules").mkdir()
    (cfg.project_root / "schedules" / "bad.md").write_text(
        "---\ncron: \"* * * * *\"\nassignee: coordinator\n---\nbody\n")
    (cfg.project_root / "schedules" / "good.md").write_text(
        "---\ncron: \"* * * * *\"\nassignee: pm\n---\nbody\n")

    fired = schedules.tick(cfg, datetime(2026, 1, 1, 13, 37))

    assert len(fired) == 1, "the good schedule still fires"
    assert "bad: not fired" in capsys.readouterr().err
    assert schedules.tick(cfg, datetime(2026, 1, 1, 13, 37)) == [], "not retried within the same minute"


def _folders_project(tmp_path: Path) -> ProjectConfig:
    cfg = make_project(tmp_path)
    (cfg.project_root / "project.yml").write_text(
        "public_repo: ../public\nfolders: [product, company]\ndefault_folder: product\n")
    return ProjectConfig.load(cfg.project_root)


def test_a_schedule_files_its_issue_in_the_folder_it_names(tmp_path):
    cfg = _folders_project(tmp_path)
    (cfg.project_root / "schedules").mkdir()
    (cfg.project_root / "schedules" / "plan.md").write_text(
        "---\ncron: \"* * * * *\"\nassignee: pm\nfolder: company\n---\nPlan the round.\n")
    fired = schedules.tick(cfg, datetime(2026, 1, 1, 9, 0))
    assert len(fired) == 1
    assert issues.folder_of(cfg, fired[0]["id"]) == "company"


def test_a_schedule_waits_while_the_issue_it_filed_last_is_unfinished(tmp_path):
    # A round that is still running (open, or blocked on its own work) is not
    # stacked with another one; the next match after it closes files a new one.
    cfg = make_project(tmp_path)
    (cfg.project_root / "schedules").mkdir()
    (cfg.project_root / "schedules" / "round.md").write_text(
        "---\ncron: \"* * * * *\"\nassignee: pm\n---\nOne round.\n")
    first = schedules.tick(cfg, datetime(2026, 1, 1, 9, 0))
    assert len(first) == 1
    assert schedules.tick(cfg, datetime(2026, 1, 1, 9, 1)) == [], "the first round is still open"
    current = issues.get_issue(cfg, first[0]["id"])
    issues.update_issue(cfg, first[0]["id"], current["hash"], {"state": "blocked"}, "pm", "pm")
    assert schedules.tick(cfg, datetime(2026, 1, 1, 9, 2)) == [], "blocked is unfinished too"
    current = issues.get_issue(cfg, first[0]["id"])
    issues.update_issue(cfg, first[0]["id"], current["hash"], {"state": "done"}, "pm", "pm")
    again = schedules.tick(cfg, datetime(2026, 1, 1, 9, 3))
    assert len(again) == 1 and again[0]["id"] != first[0]["id"]
