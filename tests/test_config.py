"""project.yml is the project's decisions; the machine config is the
machine's. These tests pin the seam between them: aliases resolve through
the machine config, and anything that belongs to the machine is refused in
project.yml rather than silently ignored."""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mcp.config import ProjectConfig  # noqa: E402
from mcp.global_config import GlobalConfig  # noqa: E402


def make_project(tmp_path: Path, extra: str = "") -> ProjectConfig:
    root = tmp_path / "instance"
    root.mkdir()
    (tmp_path / "public").mkdir()
    (root / "project.yml").write_text("public_repo: ../public\n" + extra)
    subprocess.run(["git", "init", "-q"], cwd=root)
    return ProjectConfig.load(root)


def test_machine_settings_apply_to_every_role(tmp_path, gitchen_global_config):
    gitchen_global_config.write_text(
        "harness: fake\n"
        "harnesses:\n  fake:\n    settings: {permission_mode: default}\n    sandbox: false\n"
        "models:\n  small: {fake: {model: fake-small}}\n")
    cfg = make_project(tmp_path)
    assert cfg.settings_for_role("builder") == {"permission_mode": "default"}


def test_model_alias_resolves_through_the_machine_config(tmp_path, gitchen_global_config):
    gitchen_global_config.write_text(
        "harness: fake\n"
        "harnesses:\n  fake:\n    settings: {permission_mode: default}\n    sandbox: false\n"
        "models:\n"
        "  small: {fake: {model: fake-small, thinking: low}}\n"
        "  big: {fake: {model: fake-big, thinking: high}}\n")
    cfg = make_project(tmp_path, "role_settings:\n  builder: {model: big}\n")

    assert cfg.settings_for_role("builder") == {
        "permission_mode": "default", "model": "fake-big", "thinking": "high"}
    assert cfg.settings_for_role("reviewer") == {"permission_mode": "default"}, \
        "a role with no alias must be unaffected by another role's"


def test_sandbox_comes_from_the_machine_config(tmp_path, gitchen_global_config):
    gitchen_global_config.write_text(
        "harness: fake\n"
        "harnesses:\n  fake:\n    sandbox: {profile: gitchen}\n"
        "models: {}\n")
    cfg = make_project(tmp_path)
    assert cfg.settings_for_role("builder")["sandbox"] == {"profile": "gitchen"}


def test_missing_sandbox_key_is_an_error_not_a_silent_no_sandbox(tmp_path, gitchen_global_config):
    """Auto-approving tool calls is only safe inside a sandbox, so "no
    sandbox" has to be written down (`sandbox: false`), never inferred from
    a missing key."""
    gitchen_global_config.write_text("harness: fake\nharnesses:\n  fake:\n    settings: {}\nmodels: {}\n")
    cfg = make_project(tmp_path)
    with pytest.raises(ValueError, match="sandbox is required"):
        cfg.settings_for_role("builder")


def test_sandbox_false_means_unsandboxed_on_purpose(tmp_path, gitchen_global_config):
    gitchen_global_config.write_text(
        "harness: fake\nharnesses:\n  fake:\n    sandbox: false\nmodels: {}\n")
    cfg = make_project(tmp_path)
    assert "sandbox" not in cfg.settings_for_role("builder")


def test_unknown_alias_is_an_error_not_a_fallback(tmp_path, gitchen_global_config):
    cfg = make_project(tmp_path, "role_settings:\n  builder: {model: enormous}\n")
    with pytest.raises(ValueError, match="unknown model alias 'enormous'"):
        cfg.settings_for_role("builder")


def test_alias_missing_for_the_selected_harness_is_an_error(tmp_path, gitchen_global_config):
    gitchen_global_config.write_text(
        "harness: fake\nharnesses: {fake: {sandbox: false}}\nmodels:\n  small: {other: {model: x}}\n")
    cfg = make_project(tmp_path, "role_settings:\n  builder: {model: small}\n")
    with pytest.raises(ValueError, match="no entry for harness 'fake'"):
        cfg.settings_for_role("builder")


def test_project_may_not_select_a_harness(tmp_path):
    with pytest.raises(ValueError, match="machine config"):
        make_project(tmp_path, "harness:\n  name: claude-code\n")


def test_project_may_not_override_harness_settings(tmp_path):
    with pytest.raises(ValueError, match="machine config"):
        make_project(tmp_path, "role_settings:\n  builder: {model: small, permission_mode: yolo}\n")


def test_missing_machine_config_with_nothing_installed_is_an_error(tmp_path, monkeypatch):
    """First run auto-generates the config from what's installed; with no
    supported CLI on PATH there is nothing to generate from."""
    from mcp import machine_setup
    monkeypatch.setattr(machine_setup, "detect_harnesses", lambda: {})
    monkeypatch.setenv("GITCHEN_CONFIG", str(tmp_path / "nope.yml"))
    with pytest.raises(RuntimeError, match="no supported agent CLI"):
        GlobalConfig.load()


def test_the_retired_issue_interval_is_refused_not_silently_ignored(tmp_path):
    import pytest
    from mcp.config import ProjectConfig
    root = tmp_path / "instance"
    root.mkdir()
    (tmp_path / "public").mkdir()
    (root / "project.yml").write_text("public_repo: ../public\nissue_interval_seconds: 5\n")
    with pytest.raises(ValueError, match="is gone"):
        ProjectConfig.load(root)


