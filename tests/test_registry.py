import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mcp import registry  # noqa: E402


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def make_shared_repo(tmp_path: Path) -> Path:
    shared = tmp_path / "shared"
    (shared / "library" / "agents").mkdir(parents=True)
    git(shared, "init", "-q")
    (shared / "library" / "agents" / "researcher.md").write_text("v1 prompt")
    git(shared, "add", ".")
    git(shared, "-c", "user.email=a@b.c", "-c", "user.name=a", "commit", "-q", "-m", "v1")
    return shared


def make_instance_repo(tmp_path: Path, shared: Path) -> Path:
    inst = tmp_path / "instance"
    (inst / "agents").mkdir(parents=True)
    inst_rel_shared = Path("..") / shared.relative_to(tmp_path)
    (inst / "registry.yml").write_text(
        f"repos:\n  shared: {inst_rel_shared}\n"
        f"sources:\n  agents/researcher.md: shared//library/agents/researcher.md\n"
    )
    return inst


def unpin_by_hand(inst: Path, key: str) -> None:
    """What a human does to stop tracking a pin: strip the @<ref> out of
    registry.yml directly. No command for this — the next sync() re-floats
    it and immediately re-pins at whatever it resolves to."""
    data = registry.load(inst)
    alias, path, _ref = registry._parse_descriptor(data["sources"][key])
    data["sources"][key] = f"{alias}//{path}"
    registry.save(inst, data)


def test_sync_pulls_and_auto_pins_a_floating_source(tmp_path):
    shared = make_shared_repo(tmp_path)
    inst = make_instance_repo(tmp_path, shared)

    synced = registry.sync(inst)

    assert synced == ["agents/researcher.md"]
    assert (inst / "agents" / "researcher.md").read_text() == "v1 prompt"
    # sync() doesn't just materialize — it pins what it just resolved
    descriptor = registry.load(inst)["sources"]["agents/researcher.md"]
    assert "@" in descriptor


def test_pinned_source_ignores_later_commits_until_unpinned_by_hand(tmp_path):
    shared = make_shared_repo(tmp_path)
    inst = make_instance_repo(tmp_path, shared)
    registry.sync(inst)  # first sync auto-pins at v1

    # shared moves on; the now-pinned instance should NOT pick it up
    (shared / "library" / "agents" / "researcher.md").write_text("v2 prompt")
    git(shared, "add", ".")
    git(shared, "-c", "user.email=a@b.c", "-c", "user.name=a", "commit", "-q", "-m", "v2")

    registry.sync(inst)
    assert (inst / "agents" / "researcher.md").read_text() == "v1 prompt"

    unpin_by_hand(inst, "agents/researcher.md")
    registry.sync(inst)
    assert (inst / "agents" / "researcher.md").read_text() == "v2 prompt"
    # and it's pinned again, at the new commit, automatically
    assert "@" in registry.load(inst)["sources"]["agents/researcher.md"]


def test_remote_url_repo_floating_and_pinned(tmp_path, monkeypatch):
    """A repo alias can be a git URL (here file:// so it's hermetic, but
    the same code path handles https:// and git@ too) instead of a local
    sibling path. Pinning still works via plain git history."""
    monkeypatch.setenv("GITCHEN_CACHE_DIR", str(tmp_path / "cache"))

    shared = make_shared_repo(tmp_path)
    url = f"file://{shared}"

    inst = tmp_path / "instance"
    (inst / "agents").mkdir(parents=True)
    (inst / "registry.yml").write_text(
        f"repos:\n  shared: {url}\n"
        f"sources:\n  agents/researcher.md: shared//library/agents/researcher.md\n"
    )

    synced = registry.sync(inst)
    assert synced == ["agents/researcher.md"]
    assert (inst / "agents" / "researcher.md").read_text() == "v1 prompt"
    assert (tmp_path / "cache").exists(), "expected a cached bare clone"
    assert "@" in registry.load(inst)["sources"]["agents/researcher.md"]

    git(shared, "-c", "user.email=a@b.c", "-c", "user.name=a", "commit", "--allow-empty",
        "-q", "-m", "noop")  # advance the remote
    (shared / "library" / "agents" / "researcher.md").write_text("v2 prompt")
    git(shared, "add", ".")
    git(shared, "-c", "user.email=a@b.c", "-c", "user.name=a", "commit", "-q", "-m", "v2")

    registry.sync(inst)
    assert (inst / "agents" / "researcher.md").read_text() == "v1 prompt", \
        "pinned remote source must not follow the remote's new commits"

    unpin_by_hand(inst, "agents/researcher.md")
    registry.sync(inst)
    assert (inst / "agents" / "researcher.md").read_text() == "v2 prompt"
