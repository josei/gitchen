"""First-run setup: a machine with an agent CLI installed should get a
working config without anyone hand-writing the harness, aliases or sandbox
block."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mcp import machine_setup  # noqa: E402
from mcp.config import ProjectConfig  # noqa: E402
from mcp.global_config import GlobalConfig  # noqa: E402


def install(monkeypatch, *binaries):
    """Pretends exactly `binaries` are on PATH."""
    monkeypatch.setattr(machine_setup.shutil, "which",
                        lambda name: f"/usr/local/bin/{name}" if name in binaries else None)


def test_only_installed_adapters_are_detected(monkeypatch):
    install(monkeypatch, "omp")
    assert machine_setup.detect_harnesses() == {"omp": "omp"}


def test_omp_is_preferred_when_both_are_installed(monkeypatch):
    install(monkeypatch, "omp", "claude")
    assert machine_setup.choose_harness(machine_setup.detect_harnesses()) == "omp"


def test_a_generated_config_is_valid_and_complete(monkeypatch, tmp_path):
    install(monkeypatch, "omp", "claude", "nono", "npm")
    config_path = tmp_path / "gitchen" / "config.yml"

    machine_setup.write_default(config_path)

    text = config_path.read_text()
    assert "harness: omp" in text
    assert "profile: gitchen" in text, "the sandbox section has to come for free"
    assert "thinking: auto" in text and "effort: medium" in text, \
        "each harness's own effort flag, with its own vocabulary"

    config = GlobalConfig.load(config_path)
    assert config.harness == "omp"
    assert config.sandbox("omp")["profile"] == "gitchen"
    assert config.model_settings("big", "claude-code") == {"effort": "high"}


def test_a_generated_config_runs_a_project_end_to_end(monkeypatch, tmp_path):
    install(monkeypatch, "omp", "nono", "npm")
    config_path = tmp_path / "config.yml"
    machine_setup.write_default(config_path)

    project = tmp_path / "instance"
    project.mkdir()
    (project / "project.yml").write_text("public_repo: ../public\nrole_settings:\n  builder: {model: small}\n")
    (tmp_path / "public").mkdir()

    cfg = ProjectConfig.load(project, global_config=GlobalConfig.load(config_path))
    assert cfg.settings_for_role("builder") == {
        "binary": "omp",
        "sandbox": {"profile": "gitchen", "allow": ["~/.npm"],
                    "allow_domains": ["api.anthropic.com", "api.openai.com", "api.deepinfra.com",
                                      "openrouter.ai", "generativelanguage.googleapis.com",
                                      "api.deepseek.com", "registry.npmjs.org"]},
        "thinking": "low",
    }


def test_without_nono_the_generated_config_says_so(monkeypatch, tmp_path):
    install(monkeypatch, "omp")
    config_path = tmp_path / "config.yml"

    machine_setup.write_default(config_path)

    assert "sandbox: false" in config_path.read_text()


def test_nothing_installed_is_a_human_decision_not_a_default(monkeypatch, tmp_path):
    install(monkeypatch)
    with pytest.raises(RuntimeError, match="no supported agent CLI"):
        machine_setup.write_default(tmp_path / "config.yml")


def test_loading_a_missing_config_generates_it(monkeypatch, tmp_path):
    install(monkeypatch, "omp", "nono", "npm")
    config_path = tmp_path / "config.yml"
    monkeypatch.setenv("GITCHEN_CONFIG", str(config_path))

    config = GlobalConfig.load()

    assert config_path.exists()
    assert config.harness == "omp"


def test_the_project_root_flag_is_honoured_before_a_subcommand_too(tmp_path):
    # `gitchen --project-root X budget` must read X's project.yml, not the one in
    # the current directory. The error for a missing file names the path it used.
    import subprocess
    project = tmp_path / "instance"
    project.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    gitchen = Path(__file__).resolve().parent.parent / "bin" / "gitchen"
    run = subprocess.run([sys.executable, str(gitchen), "--project-root", str(project), "budget"],
                         cwd=elsewhere, capture_output=True, text=True, timeout=60)
    assert str(project / "project.yml") in run.stderr, run.stdout + run.stderr
