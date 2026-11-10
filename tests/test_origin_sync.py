"""The daily origin sync: fetch, fast-forward or push — and never force."""

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mcp import origin_sync  # noqa: E402
from mcp.config import ProjectConfig  # noqa: E402


def git(repo, *args, check=True):
    return subprocess.run(["git", "-C", str(repo), *args], check=check, capture_output=True, text=True)


def commit(repo, message):
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "--allow-empty", "-m", message], check=True)


def make_project(tmp_path, cron="* * * * *"):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(origin)], check=True)
    public = tmp_path / "public"
    subprocess.run(["git", "clone", "-q", str(origin), str(public)], check=True)
    commit(public, "init")
    git(public, "push", "-q", "origin", "main")
    root = tmp_path / "instance"
    root.mkdir()
    (root / "project.yml").write_text(
        "public_repo: ../public\n"
        f"origin_sync:\n  cron: \"{cron}\"\n  remote: origin\n  branch: main\n")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return ProjectConfig.load(root), public, origin


def other_clone(tmp_path):
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(tmp_path / "origin.git"), str(other)], check=True)
    return other


def result(cfg):
    return json.loads((cfg.project_root / ".agents-daemon" / "origin_sync.json").read_text())["result"]


def test_local_commits_are_pushed(tmp_path):
    cfg, public, origin = make_project(tmp_path)
    commit(public, "agent work")

    origin_sync.tick(cfg)

    assert "pushed 1 commit" in result(cfg)
    assert git(origin, "log", "-1", "--format=%s", "main").stdout.strip() == "agent work"


def test_remote_commits_are_fast_forwarded(tmp_path):
    cfg, public, _origin = make_project(tmp_path)
    other = other_clone(tmp_path)
    commit(other, "work from elsewhere")
    git(other, "push", "-q", "origin", "main")

    origin_sync.tick(cfg)

    assert "fast-forwarded 1 commit" in result(cfg)
    assert git(public, "log", "-1", "--format=%s", "main").stdout.strip() == "work from elsewhere"


def test_diverged_histories_are_never_forced_or_merged(tmp_path):
    cfg, public, origin = make_project(tmp_path)
    other = other_clone(tmp_path)
    commit(other, "their work")
    git(other, "push", "-q", "origin", "main")
    commit(public, "our work")

    origin_sync.tick(cfg)

    assert "diverged" in result(cfg)
    assert git(origin, "log", "-1", "--format=%s", "main").stdout.strip() == "their work", \
        "origin must be untouched when the histories diverge"
    assert git(public, "log", "-1", "--format=%s", "main").stdout.strip() == "our work", \
        "the local branch must not be merged or reset either"


def test_a_matching_minute_fires_once(tmp_path, monkeypatch):
    cfg, _public, _origin = make_project(tmp_path)
    calls = []
    monkeypatch.setattr(origin_sync, "_sync", lambda *a: calls.append(a) or "stub")

    origin_sync.tick(cfg)
    origin_sync.tick(cfg)

    assert len(calls) == 1


def test_a_cron_that_does_not_match_does_nothing(tmp_path):
    cfg, _public, _origin = make_project(tmp_path, cron="0 0 1 1 *")
    origin_sync.tick(cfg)
    assert not (cfg.project_root / ".agents-daemon" / "origin_sync.json").exists()


def test_a_project_without_origin_sync_is_untouched(tmp_path):
    root = tmp_path / "instance"
    (tmp_path / "public").mkdir()
    root.mkdir()
    (root / "project.yml").write_text("public_repo: ../public\n")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    cfg = ProjectConfig.load(root)

    origin_sync.tick(cfg)

    assert cfg.origin_sync is None
    assert not (cfg.project_root / ".agents-daemon" / "origin_sync.json").exists()
