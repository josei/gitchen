"""Per-role network and workspace. A role may name the hosts it reaches and may
run without an issue's worktree at all (a collector reads a feed, it doesn't
build). Machine config still decides how a turn runs; a project only says what
a role needs."""

import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mcp import config as config_module  # noqa: E402
from mcp.config import ProjectConfig  # noqa: E402
from mcp.global_config import GlobalConfig  # noqa: E402


def machine(tmp_path) -> GlobalConfig:
    path = tmp_path / "machine.yml"
    path.write_text(yaml.safe_dump({
        "harness": "fake",
        "harnesses": {"fake": {"settings": {}, "sandbox": {"profile": "p",
                                                           "allow_domains": ["api.machine.example"]}}},
        "models": {"small": {"fake": {"model": "m"}}}}))
    return GlobalConfig.load(path)


def make_project(tmp_path, role_settings) -> ProjectConfig:
    base = Path(tempfile.mkdtemp(prefix="rw", dir="/tmp"))
    root = base / "i"
    (root / "agents").mkdir(parents=True)
    for role in ("builder", "collector"):
        (root / "agents" / f"{role}.md").write_text(f"---\nname: {role}\ndescription: {role}\n---\n{role}")
    (base / "public").mkdir()
    (root / "project.yml").write_text(yaml.safe_dump({"public_repo": "../public",
                                                     "role_settings": role_settings}))
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return ProjectConfig.load(root, global_config=machine(tmp_path))


def test_a_role_names_its_own_network(tmp_path):
    cfg = make_project(tmp_path, {"collector": {"model": "small", "network": ["api.feed.example"]}})
    sandbox = cfg.settings_for_role("collector")["sandbox"]
    assert sandbox["allow_domains"] == ["api.feed.example"], "its own list replaces the machine's"


def test_a_role_without_its_own_network_keeps_the_machines(tmp_path):
    cfg = make_project(tmp_path, {"builder": {"model": "small"}})
    assert cfg.settings_for_role("builder")["sandbox"]["allow_domains"] == ["api.machine.example"]


def test_a_role_may_set_only_model_network_and_workspace(tmp_path):
    with pytest.raises(ValueError, match="machine config"):
        make_project(tmp_path, {"builder": {"model": "small", "harness": "x"}})


def test_workspace_must_be_none_or_unset(tmp_path):
    with pytest.raises(ValueError, match="workspace"):
        make_project(tmp_path, {"collector": {"workspace": "everything"}})
    cfg = make_project(tmp_path, {"collector": {"workspace": "none"}})
    assert cfg.workspace_of("collector") == "none"
    assert cfg.workspace_of("builder") == "worktree"


def test_a_role_with_no_workspace_gets_no_worktree_and_no_git_grant(tmp_path):
    from mcp import dispatch, harness as harness_module
    from mcp.harness import TurnResult
    seen = []

    class H:
        def start(self, prompt, cwd, settings):
            seen.append((cwd, settings))
            return TurnResult("s", "Collected.\nNEXT: reported", {}, True)

        def resume(self, *a, **k):
            raise AssertionError

        def interrupt(self, *a, **k):
            pass

    harness_module._REGISTRY["fake"] = lambda: H()
    cfg = make_project(tmp_path, {"collector": {"workspace": "none"}})
    (cfg.project_root / "project.yml").write_text(yaml.safe_dump({
        "public_repo": "../public", "role_settings": {"collector": {"workspace": "none"}},
        "allowed_tools": {"collector": ["list_issues"]}, "transitions": {"collector": {"reported": "return"}}}))
    (cfg.project_root / "agents" / "collector.md").write_text(
        "---\nname: collector\ndescription: c\noutcomes: {reported: done}\n---\ncollector")
    cfg = ProjectConfig.load(cfg.project_root, global_config=machine(tmp_path))
    created = dispatch.issues.create_issue(cfg, "collect", "b", None, 0, "pm", "pm", assignee="collector")
    for t in dispatch.tick(cfg):
        t.join(10)
    cwd, settings = seen[0]
    assert "worktrees" not in cwd and "scratch" in cwd, "it runs in a scratch folder"
    assert not (cfg.project_root / ".agents-daemon" / "worktrees" / str(created["id"])).exists()
    assert not any(p.endswith("/objects") for p in settings["sandbox"].get("allow", [])), "no git grant"
