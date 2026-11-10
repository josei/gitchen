"""The shared sandbox prefix: harness-agnostic settings in, nono argv out,
and the gitchen-shipped profile installed where nono can find it."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mcp.harnesses import sandbox  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_profile_dir(tmp_path, monkeypatch):
    """Never install into the real ~/.config/nono during tests."""
    monkeypatch.setenv("GITCHEN_PROFILE_DIR", str(tmp_path / "profiles"))
    sandbox._INSTALLED.clear()
    yield
    sandbox._INSTALLED.clear()


def test_no_sandbox_setting_means_no_prefix(tmp_path):
    assert sandbox.prefix({}, str(tmp_path)) == []


def test_prefix_carries_the_profile_grants_and_workdir(tmp_path, monkeypatch):
    monkeypatch.setenv("GITCHEN_CONFIG", str(tmp_path / "absent.yml"))
    args = sandbox.prefix(
        {"sandbox": {"profile": "some-machine-profile", "allow": ["~/.npm"],
                     "allow_domains": ["registry.npmjs.org"]},
         "mcp": {"project_root": "/x/instance"}},
        str(tmp_path), api_hosts=("api.example.com",))

    assert args[:3] == ["nono", "run", "--profile"]
    assert args[args.index("--profile") + 1] == "some-machine-profile"
    assert str(Path("~/.npm").expanduser()) in args
    assert args[args.index("--allow-domain") + 1] == "api.example.com"
    assert "registry.npmjs.org" in args
    assert args[args.index("--workdir") + 1] == str(tmp_path)
    assert args[-1] == "--", "the wrapped command must come after --"


def test_the_turn_reads_its_project_file_and_writes_only_its_workspace(tmp_path, monkeypatch):
    """The instance repo holds every other issue, the roles and the policy. A
    turn needs none of it writable, and only its own worktree."""
    monkeypatch.setenv("GITCHEN_CONFIG", str(tmp_path / "absent.yml"))
    worktree = tmp_path / "instance" / ".agents-daemon" / "worktrees" / "7"
    args = sandbox.prefix(
        {"sandbox": {"profile": "some-machine-profile"},
         "mcp": {"project_root": str(tmp_path / "instance")}},
        str(worktree))
    allowed = [args[i + 1] for i, a in enumerate(args) if a == "--allow"]
    assert allowed == [str(worktree)], "the worktree is the only writable project path"
    assert str(tmp_path / "instance") not in allowed
    read_files = [args[i + 1] for i, a in enumerate(args) if a == "--read-file"]
    assert str(tmp_path / "instance" / "project.yml") in read_files


def test_prefix_grants_read_only_on_the_machine_config(tmp_path, monkeypatch):
    """The agent's own MCP server is `gitchen serve`, which loads this file
    before answering any tool call; without the grant it dies with a
    PermissionError and the agent sees only a closed connection."""
    config = tmp_path / "config.yml"
    config.write_text("harness: fake\n")
    monkeypatch.setenv("GITCHEN_CONFIG", str(config))

    args = sandbox.prefix({"sandbox": {"profile": "some-machine-profile"}}, str(tmp_path))

    assert args[args.index("--read-file") + 1] == str(config)


def test_no_config_grant_when_the_file_does_not_exist(tmp_path, monkeypatch):
    monkeypatch.setenv("GITCHEN_CONFIG", str(tmp_path / "absent.yml"))
    args = sandbox.prefix({"sandbox": {"profile": "some-machine-profile"}}, str(tmp_path))
    assert "--read-file" not in args


def test_a_shipped_profile_is_installed_on_first_use(tmp_path):
    sandbox.ensure_profile("gitchen")

    installed = tmp_path / "profiles" / "gitchen.json"
    assert installed.exists()
    assert json.loads(installed.read_text())["extends"] == "nolabs-ai/claude"


def test_installed_profile_gets_this_platforms_port_policy(tmp_path, monkeypatch):
    """nono has no portable "every localhost port": macOS wants port 0,
    Linux wants a range. Resolving that at install time is why ports live in
    the profile and not in a CLI flag."""
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    sandbox.ensure_profile("gitchen")
    linux = json.loads((tmp_path / "profiles" / "gitchen.json").read_text())
    assert linux["network"] == {"open_port_range": [[1, 65535]]}

    sandbox._INSTALLED.clear()
    monkeypatch.setenv("GITCHEN_PROFILE_DIR", str(tmp_path / "mac-profiles"))
    monkeypatch.setattr(sandbox.sys, "platform", "darwin")
    sandbox.ensure_profile("gitchen")
    mac = json.loads((tmp_path / "mac-profiles" / "gitchen.json").read_text())
    assert mac["network"] == {"open_port": [0]}


def test_an_already_installed_profile_is_left_alone(tmp_path):
    sandbox.ensure_profile("gitchen")
    installed = tmp_path / "profiles" / "gitchen.json"
    installed.write_text(installed.read_text().replace("gitchen agent sandbox", "locally edited"))

    sandbox._INSTALLED.clear()
    sandbox.ensure_profile("gitchen")

    assert "locally edited" in installed.read_text()


def test_a_profile_gitchen_does_not_ship_is_never_written(tmp_path):
    sandbox.ensure_profile("custom-profile")
    assert not (tmp_path / "profiles" / "custom-profile.json").exists()


def test_a_single_file_is_granted_with_read_file_and_a_folder_with_read(tmp_path, monkeypatch):
    """Regression: a program role's script is one file, and nono refuses it under
    --read (which takes a folder). Found only by running the real sandbox."""
    monkeypatch.setenv("GITCHEN_CONFIG", str(tmp_path / "absent.yml"))
    script = tmp_path / "collector.py"
    script.write_text("print('x')\n")
    folder = tmp_path / "data"
    folder.mkdir()
    args = sandbox.prefix({"sandbox": {"profile": "p", "read": [str(script), str(folder)]}}, str(tmp_path))
    assert args[args.index(str(script)) - 1] == "--read-file"
    assert args[args.index(str(folder)) - 1] == "--read"
