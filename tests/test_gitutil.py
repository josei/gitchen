"""gitutil.commit must never push, even when a remote exists — pushing is
the `publish` skill's job, deliberate and reviewed, not something a task
log flush or a knowledge write should ever trigger on its own."""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mcp import gitutil  # noqa: E402


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def make_repo_with_remote(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))

    local = tmp_path / "local"
    git(tmp_path, "clone", "-q", str(remote), str(local))
    (local / "README.md").write_text("init\n")
    git(local, "add", ".")
    git(local, "-c", "user.email=a@b.c", "-c", "user.name=a", "commit", "-q", "-m", "init")
    git(local, "push", "-q", "origin", "HEAD")
    return local, remote


def test_commit_does_not_push_even_with_a_remote_configured(tmp_path):
    local, remote = make_repo_with_remote(tmp_path)
    remote_head_before = git(remote, "rev-parse", "HEAD").stdout.strip()

    (local / "knowledge.md").write_text("new content\n")
    gitutil.commit(local, ["knowledge.md"], "add knowledge")

    local_head = git(local, "-c", "user.email=a@b.c", "-c", "user.name=a", "rev-parse", "HEAD").stdout.strip()
    remote_head_after = git(remote, "rev-parse", "HEAD").stdout.strip()

    assert local_head != remote_head_before, "the local commit should have happened"
    assert remote_head_after == remote_head_before, "the remote must be untouched — commit() must never push"


def test_commit_is_a_noop_when_nothing_changed(tmp_path):
    local, _remote = make_repo_with_remote(tmp_path)
    head_before = git(local, "rev-parse", "HEAD").stdout.strip()

    gitutil.commit(local, ["README.md"], "nothing changed")

    assert git(local, "rev-parse", "HEAD").stdout.strip() == head_before


def test_parallel_commits_never_bundle_each_others_files(tmp_path):
    """Real problem, reproduced: with several turns writing at once, a plain
    commit took whatever another writer had staged, so one commit could carry
    several issues under one author. A commit must hold only its own paths."""
    import threading
    repo = tmp_path / "instance"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
                    "--allow-empty", "-m", "init"], cwd=repo, check=True)
    files = []
    for n in range(8):
        path = repo / f"file{n}.md"
        path.write_text("v0\n")
        files.append(path.name)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "base"],
                   cwd=repo, check=True)
    start = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True,
                           text=True).stdout.strip()

    def write(name, n):
        for step in range(5):
            (repo / name).write_text(f"step {step}\n")
            gitutil.commit(repo, [name], f"{name}: step {step}", author=f"role{n} <role{n}@gitchen.local>")

    threads = [threading.Thread(target=write, args=(name, n)) for n, name in enumerate(files)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    log = subprocess.run(["git", "log", "--format=%H %s", f"{start}..HEAD"], cwd=repo,
                         capture_output=True, text=True).stdout.splitlines()
    assert len(log) == 40, "one commit per write"
    for line in log:
        sha = line.split()[0]
        touched = subprocess.run(["git", "show", "--name-only", "--format=", sha], cwd=repo,
                                 capture_output=True, text=True).stdout.split()
        assert len(touched) == 1, f"{line} bundled {touched}"
