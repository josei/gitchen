import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mcp import gitutil, retry  # noqa: E402


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    monkeypatch.setattr(retry, "BACKOFF_SECONDS", 0)


def test_retries_a_transient_failure_until_it_succeeds(capsys):
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise gitutil.GitError("index.lock held")
        return "ok"

    assert retry.retry(flaky, retry_on=(gitutil.GitError,), label="test") == "ok"
    assert len(calls) == 3
    assert "[retry] test: attempt 1/3" in capsys.readouterr().err


def test_gives_up_after_the_attempt_budget_and_raises(capsys):
    calls = []

    def always_fails():
        calls.append(1)
        raise gitutil.GitError("still locked")

    with pytest.raises(gitutil.GitError):
        retry.retry(always_fails, retry_on=(gitutil.GitError,), label="test")
    assert len(calls) == retry.ATTEMPTS


def test_does_not_retry_a_failure_it_was_not_told_about():
    calls = []

    def bug():
        calls.append(1)
        raise ValueError("a bug, not a transient failure")

    with pytest.raises(ValueError):
        retry.retry(bug, retry_on=(gitutil.GitError,), label="test")
    assert len(calls) == 1


def test_commit_retries_a_locked_index_and_then_commits(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    gitutil.run(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("hi")

    real_run = gitutil.run
    commit_attempts = []

    def run_with_one_lock(r, *args):
        if args[:1] == ("commit",):
            commit_attempts.append(1)
            if len(commit_attempts) == 1:
                return type("R", (), {"returncode": 128, "stdout": "", "stderr": "index.lock exists"})()
        return real_run(r, *args)

    monkeypatch.setattr(gitutil, "run", run_with_one_lock)
    gitutil.commit(repo, ["a.txt"], "add a", author="t <t@t>")
    assert len(commit_attempts) == 2
    assert real_run(repo, "log", "--oneline").stdout.strip()
