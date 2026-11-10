import yaml
import pytest
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mcp import dispatch, holders, issues, pause, worktrees  # noqa: E402
from mcp import harness as harness_module  # noqa: E402
from mcp.config import ProjectConfig  # noqa: E402
from mcp.harness import TurnResult  # noqa: E402


def make_project(tmp_path: Path, **extra_yaml) -> ProjectConfig:
    root = tmp_path / "instance"
    (root / "agents").mkdir(parents=True)
    for _role in ("builder", "reviewer", "pm", "researcher"):
        _f = root / "agents" / f"{_role}.md"
        if not _f.exists():
            _f.write_text(f"---\nname: {_role}\n---\n{_role}")
    # Real agent files always have frontmatter (---\nname: ...\n---\n) —
    # a bare string here would never have caught the real --- -as-a-CLI-flag
    # regression _build_prompt had, so every fixture uses the real shape.
    (root / "agents" / "builder.md").write_text(
        "---\nname: builder\ndescription: builds things\n---\nYou are the builder.")
    (root / "agents" / "reviewer.md").write_text(
        "---\nname: reviewer\ndescription: reviews things\n---\nYou are the reviewer.")
    public_repo = tmp_path / "public"
    public_repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=public_repo)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
                    "--allow-empty", "-m", "init"], cwd=public_repo)
    extra = "".join(f"{k}: {v}\n" for k, v in extra_yaml.items())
    (root / "project.yml").write_text(
        f"public_repo: ../public\n{extra}"
    )
    subprocess.run(["git", "init", "-q"], cwd=root)
    return ProjectConfig.load(root)


def _expire_pause(root):
    path = Path(root) / ".agents-daemon" / "pause.json"
    state = json.loads(path.read_text())
    state["until"] = 0
    path.write_text(json.dumps(state))


def _daemon_comments(issue):
    """The daemon never comments: it tells the running agent, or records an event."""
    return [line["text"] for line in issue["lines"]
            if line["text"].startswith("### ") and line["text"].endswith("— daemon")]


def _join_all(threads):
    for t in threads:
        t.join(timeout=5)


def test_tick_runs_eligible_issue_and_return_directive_pops_assignee(tmp_path):
    class H:
        def start(self, prompt, cwd, settings):
            return TurnResult("s1", "Done.\nNEXT: return", {}, True)

        def resume(self, *a, **k):
            raise AssertionError("should not need a retry")

        def interrupt(self, *a, **k):
            pass

    harness_module._REGISTRY["fake"] = H
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "title", "body", None, 0, "pm", "pm", assignee="builder")

    threads = dispatch.tick(cfg)
    assert len(threads) == 1
    _join_all(threads)

    final = issues.get_issue(cfg, created["id"])
    assert final["assignee"] == "pm"
    assert final["assignment_history"][-1]["type"] == "return"
    body_text = "\n".join(line["text"] for line in final["lines"])
    assert "Done." in body_text
    assert holders.is_held(cfg.project_root, created["id"]) is False


def test_assign_directive_pushes_when_target_is_permitted(tmp_path):
    class H:
        def start(self, prompt, cwd, settings):
            return TurnResult("s1", "Handed off.\nNEXT: assign reviewer", {}, True)

        def resume(self, *a, **k):
            raise AssertionError

        def interrupt(self, *a, **k):
            pass

    harness_module._REGISTRY["fake"] = H
    cfg = make_project(tmp_path, allowed_tools="\n  builder: [assign_issue]")
    created = issues.create_issue(cfg, "title", "body", None, 0, "pm", "pm", assignee="builder")

    _join_all(dispatch.tick(cfg))

    final = issues.get_issue(cfg, created["id"])
    assert final["assignee"] == "reviewer"
    assert final["assignment_history"][-1] == {
        "type": "assign", "role": "reviewer", "by": "builder",
        "at": final["assignment_history"][-1]["at"]}


def test_assign_from_a_role_without_assign_issue_is_a_bad_directive(tmp_path):
    """A role that can't assign must not be left holding the issue. This
    used to post a "rejected" comment and leave the assignee alone, which
    meant the very next tick re-dispatched the same role forever: nothing
    in assignment_history changed, so even the circuit breaker never
    noticed. It now gets the same corrective resume as any other invalid
    directive, and the correction no longer offers the verb it can't use."""
    class H:
        def __init__(self):
            self.corrections = []

        def start(self, prompt, cwd, settings):
            return TurnResult("s1", "Sneaky.\nNEXT: assign security", {}, True)

        def resume(self, session_id, prompt, cwd, settings):
            self.corrections.append(prompt)
            return TurnResult(session_id, "Handing back.\nNEXT: return", {}, True)

        def interrupt(self, *a, **k):
            pass

    h = H()
    harness_module._REGISTRY["fake"] = lambda: h
    cfg = make_project(tmp_path)  # builder has no assign_issue
    created = issues.create_issue(cfg, "title", "body", None, 0, "pm", "pm", assignee="builder")

    _join_all(dispatch.tick(cfg))

    assert len(h.corrections) == 1
    assert "assign" not in h.corrections[0]
    final = issues.get_issue(cfg, created["id"])
    assert final["assignee"] == "pm", "an unpermitted assign must hand the issue back, not strand it"
    assert final["assignment_history"][-1]["type"] == "return"


def test_the_directive_line_only_offers_verbs_the_role_can_take(tmp_path):
    cfg = make_project(tmp_path, allowed_tools="\n  pm: [assign_issue, update_issue]\n  builder: [get_issue]")
    pm_line = dispatch._directive_line(cfg, "pm")
    builder_line = dispatch._directive_line(cfg, "builder")
    assert "NEXT: assign <role>" in pm_line
    assert "NEXT: done" in pm_line
    assert "NEXT: return" in builder_line
    assert "assign" not in builder_line
    assert "NEXT: done" not in builder_line


def test_invalid_directive_gets_a_corrective_resume_then_succeeds(tmp_path):
    class H:
        def __init__(self):
            self.resumed = False

        def start(self, prompt, cwd, settings):
            return TurnResult("s1", "I did stuff, no directive here", {}, True)

        def resume(self, session_id, prompt, cwd, settings):
            self.resumed = True
            return TurnResult(session_id, "Fixed.\nNEXT: return", {}, True)

        def interrupt(self, *a, **k):
            pass

    h = H()
    harness_module._REGISTRY["fake"] = lambda: h
    cfg = make_project(tmp_path, directive_retries=2)
    created = issues.create_issue(cfg, "title", "body", None, 0, "pm", "pm", assignee="builder")

    _join_all(dispatch.tick(cfg))

    assert h.resumed is True
    final = issues.get_issue(cfg, created["id"])
    assert final["assignee"] == "pm"
    body_text = "\n".join(line["text"] for line in final["lines"])
    assert "I did stuff" in body_text
    assert "Fixed." not in body_text, "a correction supplies the directive, not the report"
    assert issues.comment_count(final) == 1


def _commit_in_worktree(cwd, name="work.txt"):
    (Path(cwd) / name).write_text("x")
    subprocess.run(["git", "add", name], cwd=cwd, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", name],
                   cwd=cwd, check=True)


def test_only_one_comment_per_turn_even_with_corrections(tmp_path):
    class H:
        def start(self, prompt, cwd, settings):
            return TurnResult("s1", "The full report.", {}, True)

        def resume(self, session_id, prompt, cwd, settings):
            return TurnResult(session_id, "Still thinking." if "NEXT" in prompt else "", {}, True)

        def interrupt(self, *a, **k):
            pass

    harness_module._REGISTRY["fake"] = H
    cfg = make_project(tmp_path, directive_retries=2)
    created = issues.create_issue(cfg, "title", "body", None, 0, "pm", "pm", assignee="builder")
    _join_all(dispatch.tick(cfg))
    final = issues.get_issue(cfg, created["id"])
    assert issues.comment_count(final) == 1
    assert "The full report." in "\n".join(line["text"] for line in final["lines"])


def test_a_correction_line_does_not_replace_the_report(tmp_path):
    class H:
        def start(self, prompt, cwd, settings):
            return TurnResult("s1", "The full report.", {}, True)

        def resume(self, session_id, prompt, cwd, settings):
            return TurnResult(session_id, "Sorry about that.\nNEXT: return", {}, True)

        def interrupt(self, *a, **k):
            pass

    harness_module._REGISTRY["fake"] = H
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "title", "body", None, 0, "pm", "pm", assignee="builder")
    _join_all(dispatch.tick(cfg))
    text = "\n".join(line["text"] for line in issues.get_issue(cfg, created["id"])["lines"])
    assert "The full report." in text and "Sorry about that." not in text


def test_an_empty_report_takes_the_text_of_the_correction(tmp_path):
    class H:
        def start(self, prompt, cwd, settings):
            return TurnResult("s1", "", {}, True)

        def resume(self, session_id, prompt, cwd, settings):
            return TurnResult(session_id, "What I did.\nNEXT: return", {}, True)

        def interrupt(self, *a, **k):
            pass

    harness_module._REGISTRY["fake"] = H
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "title", "body", None, 0, "pm", "pm", assignee="builder")
    _join_all(dispatch.tick(cfg))
    assert "What I did." in "\n".join(
        line["text"] for line in issues.get_issue(cfg, created["id"])["lines"])


def test_a_turn_with_nothing_to_say_still_posts_one_comment(tmp_path):
    class H:
        def start(self, prompt, cwd, settings):
            return TurnResult("s1", "NEXT: return", {}, True)

        def resume(self, *a, **k):
            raise AssertionError

        def interrupt(self, *a, **k):
            pass

    harness_module._REGISTRY["fake"] = H
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "title", "body", None, 0, "pm", "pm", assignee="builder")
    _join_all(dispatch.tick(cfg))
    final = issues.get_issue(cfg, created["id"])
    assert issues.comment_count(final) == 1
    assert "(no report)" in "\n".join(line["text"] for line in final["lines"])


def test_a_refused_close_resumes_the_agent_and_posts_no_daemon_comment(tmp_path):
    """Refused because the branch holds unmerged commits. The agent is told, in
    its own session, and the comment is the report it writes after that."""
    class H:
        def __init__(self):
            self.asked = []

        def start(self, prompt, cwd, settings):
            _commit_in_worktree(cwd)
            return TurnResult("s1", "Finished, no code.\nNEXT: done", {}, True)

        def resume(self, session_id, prompt, cwd, settings):
            self.asked.append(prompt)
            return TurnResult(session_id, "There was code. Merging it.\nNEXT: merge", {}, True)

        def interrupt(self, *a, **k):
            pass

    h = H()
    harness_module._REGISTRY["fake"] = lambda: h
    cfg = make_project(tmp_path, allowed_tools="\n  builder: [update_issue]")
    created = issues.create_issue(cfg, "title", "body", None, 0, "pm", "pm", assignee="builder")
    _join_all(dispatch.tick(cfg))

    final = issues.get_issue(cfg, created["id"])
    assert len(h.asked) == 1
    assert "commit(s)" in h.asked[0], "the agent hears why it was refused"
    assert "NEXT: merge" in h.asked[0], "and what it can do instead"
    assert final["state"] == "merged"
    assert not _daemon_comments(final)
    assert issues.comment_count(final) == 1
    text = "\n".join(line["text"] for line in final["lines"])
    assert "There was code." in text and "Finished, no code." not in text


def test_a_refused_close_that_never_succeeds_blocks(tmp_path):
    class H:
        def __init__(self):
            self.resumes = 0

        def start(self, prompt, cwd, settings):
            _commit_in_worktree(cwd)
            return TurnResult("s1", "Finished.\nNEXT: done", {}, True)

        def resume(self, session_id, prompt, cwd, settings):
            self.resumes += 1
            return TurnResult(session_id, f"Attempt {self.resumes}.\nNEXT: done", {}, True)

        def interrupt(self, *a, **k):
            pass

    h = H()
    harness_module._REGISTRY["fake"] = lambda: h
    cfg = make_project(tmp_path, directive_retries=2,
                       allowed_tools="\n  builder: [update_issue]")
    created = issues.create_issue(cfg, "title", "body", None, 0, "pm", "pm", assignee="builder")
    _join_all(dispatch.tick(cfg))

    final = issues.get_issue(cfg, created["id"])
    assert h.resumes == 2
    assert final["state"] == "blocked"
    assert final["assignee"] == "builder"
    event = final["assignment_history"][-1]
    assert event["kind"] == "blocked" and "commit(s)" in event["reason"]
    assert "Respond" not in event["reason"], "the reason is for a person, not the prompt to the agent"
    assert not _daemon_comments(final)
    assert issues.comment_count(final) == 1
    assert "Attempt 2." in "\n".join(line["text"] for line in final["lines"])


def test_a_close_retries_past_a_concurrent_edit(tmp_path, monkeypatch):
    """Someone edited the issue between the read and the write. That is not a
    refusal to bother the agent with."""
    class H:
        def start(self, prompt, cwd, settings):
            return TurnResult("s1", "Accepted.\nNEXT: done", {}, True)

        def resume(self, *a, **k):
            raise AssertionError("a stale hash must not reach the agent")

        def interrupt(self, *a, **k):
            pass

    harness_module._REGISTRY["fake"] = H
    cfg = make_project(tmp_path, allowed_tools="\n  builder: [update_issue]")
    created = issues.create_issue(cfg, "title", "body", None, 0, "pm", "pm", assignee="builder")
    real = dispatch.server.dispatch
    calls = {"n": 0}

    def stale_once(c, name, args, identity):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"ok": False, "error": "stale hash"}
        return real(c, name, args, identity)

    monkeypatch.setattr(dispatch.server, "dispatch", stale_once)
    _join_all(dispatch.tick(cfg))
    assert issues.get_issue(cfg, created["id"])["state"] == "done"


def test_the_daemon_never_comments(tmp_path, monkeypatch):
    """Every path where the framework has something to say, in one place."""
    monkeypatch.setattr(dispatch, "MAX_COMMENTS", 3)

    def scenario(name, start, resume=None, **project):
        class H:
            def start(self, prompt, cwd, settings):
                return start(cwd)

            def resume(self, session_id, prompt, cwd, settings):
                return (resume or (lambda: TurnResult("s1", "no directive", {}, True)))()

            def interrupt(self, *a, **k):
                pass

        harness_module._REGISTRY["fake"] = H
        return make_project(tmp_path / name, **project)

    def boom(cwd):
        raise RuntimeError("a bug")

    def refused(cwd):
        _commit_in_worktree(cwd)
        return TurnResult("s1", "Done.\nNEXT: done", {}, True)

    # a crash
    cfg = scenario("crash", boom)
    crash = issues.create_issue(cfg, "t", "b", None, 0, "pm", "pm", assignee="builder")
    _join_all(dispatch.tick(cfg))
    assert not _daemon_comments(issues.get_issue(cfg, crash["id"]))

    # no directive, ever
    cfg = scenario("silent", lambda cwd: TurnResult("s1", "no directive", {}, True))
    silent = issues.create_issue(cfg, "t", "b", None, 0, "pm", "pm", assignee="builder")
    _join_all(dispatch.tick(cfg))
    assert not _daemon_comments(issues.get_issue(cfg, silent["id"]))

    # a close that is refused every time
    cfg = scenario("refused", refused, lambda: TurnResult("s1", "Done.\nNEXT: done", {}, True),
                   allowed_tools="\n  builder: [update_issue]")
    closing = issues.create_issue(cfg, "t", "b", None, 0, "pm", "pm", assignee="builder")
    _join_all(dispatch.tick(cfg))
    assert not _daemon_comments(issues.get_issue(cfg, closing["id"]))

    # a return with nobody above
    cfg = scenario("top", lambda cwd: TurnResult("s1", "Done.\nNEXT: return", {}, True))
    top = issues.create_issue(cfg, "t", "b", None, 0, "coordinator", "coordinator", assignee="builder")
    _join_all(dispatch.tick(cfg))
    assert not _daemon_comments(issues.get_issue(cfg, top["id"]))

    # the circuit breaker
    cfg = scenario("breaker", lambda cwd: TurnResult("s1", "x\nNEXT: return", {}, True))
    runaway = issues.create_issue(cfg, "t", "b", None, 0, "pm", "pm", assignee="builder")
    for n in range(3):
        issues.add_comment(cfg, runaway["id"], f"turn {n}", "builder", "builder")
    dispatch.tick(cfg)
    assert issues.get_issue(cfg, runaway["id"])["state"] == "blocked"
    assert not _daemon_comments(issues.get_issue(cfg, runaway["id"]))


def test_invalid_directive_exhausts_and_auto_returns(tmp_path):
    class H:
        def start(self, prompt, cwd, settings):
            return TurnResult("s1", "still no directive", {}, True)

        def resume(self, session_id, prompt, cwd, settings):
            return TurnResult(session_id, "still nothing", {}, True)

        def interrupt(self, *a, **k):
            pass

    harness_module._REGISTRY["fake"] = H
    cfg = make_project(tmp_path, directive_retries=1)
    created = issues.create_issue(cfg, "title", "body", None, 0, "pm", "pm", assignee="builder")

    _join_all(dispatch.tick(cfg))

    final = issues.get_issue(cfg, created["id"])
    assert final["assignee"] == "pm", "exhausting retries must still hand the issue back, not strand it"
    assert final["assignment_history"][-1]["type"] == "return"
    events = [e for e in final["assignment_history"] if e["type"] == "event"]
    assert [e["kind"] for e in events] == ["no_directive"]
    assert not _daemon_comments(final)


def test_none_is_no_longer_a_directive():
    """A role with nothing to do returns, blocks or cancels; `none` is gone."""
    assert dispatch._parse_next("Waiting.\nNEXT: none", True, True, ["builder"]) is None
    assert dispatch._parse_next("Done.\nNEXT: return", False, False) == ("return", None)


def test_one_issue_at_a_time_then_the_next_issue_is_taken(tmp_path):
    """The queue is serial: one issue's turn at a time, and the next tick
    takes the next issue rather than another turn on the same one."""
    class H:
        def start(self, prompt, cwd, settings):
            return TurnResult("s1", "Done.\nNEXT: return", {}, True)

        def resume(self, *a, **k):
            raise AssertionError

        def interrupt(self, *a, **k):
            pass

    harness_module._REGISTRY["fake"] = H
    cfg = make_project(tmp_path)
    first = issues.create_issue(cfg, "first", "body", None, 0, "coordinator", "coordinator",
                                assignee="builder")
    second = issues.create_issue(cfg, "second", "body", None, 0, "coordinator", "coordinator",
                                 assignee="builder")

    threads = dispatch.tick(cfg)
    assert len(threads) == 1, "one issue at a time"
    _join_all(threads)

    # whichever issue ran returned with nobody above it, so it is blocked;
    # the other is still untouched
    first_after = issues.get_issue(cfg, first["id"])
    second_after = issues.get_issue(cfg, second["id"])
    resolved = [i for i in (first_after, second_after) if i["state"] == "blocked"]
    untouched = [i for i in (first_after, second_after) if i["state"] == "open"]
    assert len(resolved) == 1 and len(untouched) == 1

    threads2 = dispatch.tick(cfg)
    assert len(threads2) == 1
    _join_all(threads2)
    assert issues.get_issue(cfg, untouched[0]["id"])["state"] == "blocked"


def test_spawn_uses_the_issues_own_worktree_as_cwd(tmp_path):
    seen_cwd = {}

    class H:
        def start(self, prompt, cwd, settings):
            seen_cwd["cwd"] = cwd
            return TurnResult("s1", "Done.\nNEXT: return", {}, True)

        def resume(self, *a, **k):
            raise AssertionError

        def interrupt(self, *a, **k):
            pass

    harness_module._REGISTRY["fake"] = H
    cfg = make_project(tmp_path)
    public = cfg.public_repo
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=public)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=public)
    subprocess.run(["git", "config", "user.name", "t"], cwd=public)
    (public / "README.md").write_text("hi\n")
    subprocess.run(["git", "add", "README.md"], cwd=public)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=public)

    created = issues.create_issue(cfg, "title", "body", None, 0, "pm", "pm", assignee="builder")

    _join_all(dispatch.tick(cfg))

    assert seen_cwd["cwd"] == str(worktrees.path_for(cfg, created["id"]))
    assert seen_cwd["cwd"] != str(public)


def test_spawn_grants_sandbox_access_to_public_repo_s_git_dir_only(tmp_path, gitchen_global_config):
    """Real regression: a worktree's `.git` is a file pointing back at
    public_repo's `.git/worktrees/<name>` — git needs real access there
    for any operation, not just the worktree directory. Found on a real
    dispatch run: builder built and tested a real change, then couldn't
    commit it, because git inside the worktree was denied reading
    public_repo's `.git`.

    Grants only `.git`, never public_repo itself: public_repo's own
    working tree normally sits on `main`, not an issue branch, so a
    readwrite grant on the whole directory would let a sandboxed
    builder/reviewer read and write files directly on `main` — caught in
    review, not by a failing test, since nothing here previously checked
    *what* got granted beyond "something public_repo-shaped."

    Also checks this doesn't mutate the machine's sandbox config in
    place — it's the same object across every dispatch this
    (long-running) daemon process makes."""
    seen_settings = []

    class H:
        def start(self, prompt, cwd, settings):
            seen_settings.append(settings)
            return TurnResult("s1", "Done.\nNEXT: return", {}, True)

        def resume(self, *a, **k):
            raise AssertionError

        def interrupt(self, *a, **k):
            pass

    gitchen_global_config.write_text(
        "harness: fake\n"
        "harnesses:\n  fake:\n    sandbox: {profile: nolabs-ai/claude, allow: [/extra]}\n"
        "models: {}\n")
    harness_module._REGISTRY["fake"] = H
    cfg = make_project(tmp_path)
    original_sandbox = cfg.global_config.sandbox("fake")

    issues.create_issue(cfg, "title", "body", None, 0, "pm", "pm", assignee="builder")
    _join_all(dispatch.tick(cfg))
    issues.create_issue(cfg, "title2", "body2", None, 0, "pm", "pm", assignee="builder")
    _join_all(dispatch.tick(cfg))

    git_dir = cfg.public_repo / ".git"
    for settings in seen_settings:
        sandbox = settings["sandbox"]
        assert sandbox["allow"][0] == "/extra", "the machine's own grants come first"
        assert str(git_dir) in sandbox["read"], "git can read the whole .git, to see other branches"
        writable = sandbox["allow"][1:]
        assert str(git_dir / "objects") in writable
        assert str(git_dir / "refs" / "heads" / "issue") in writable
        assert str(git_dir / "logs" / "refs" / "heads" / "issue") in writable
        assert settings["shared"]["dir"] in writable, "the artifacts folder is its own grant"
        assert str(cfg.public_repo) not in writable, "the main checkout is never writable"
        assert not any(p.endswith("/refs/heads") for p in writable), \
            "no write to other branches: only the issue refs"
        assert not any(p == str(git_dir) for p in writable), "git config and hooks stay read-only"
    # the second dispatch must not have seen a doubled-up allow list from
    # the first call having mutated the machine config's dict
    assert original_sandbox == {"profile": "nolabs-ai/claude", "allow": ["/extra"]}
    assert "read" not in original_sandbox
    assert cfg.global_config.sandbox("fake") == original_sandbox


def test_issue_closed_at_creation_never_gets_dispatched_or_a_worktree(tmp_path):
    """A decision issue is created already closed and is never anyone's
    live assignee's work-in-progress — tick() must never touch it, and it
    must never pay for a worktree it'll never use."""
    class H:
        def start(self, *a, **k):
            raise AssertionError("a closed issue must never be dispatched")

        def resume(self, *a, **k):
            raise AssertionError

        def interrupt(self, *a, **k):
            pass

    harness_module._REGISTRY["fake"] = H
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "a decision", "body", ["decision"], 0, "coordinator", "coordinator", assignee="builder")
    issues.update_issue(cfg, created["id"], created["hash"], {"state": "done"}, "coordinator", "coordinator")

    threads = dispatch.tick(cfg)

    assert threads == []
    assert not worktrees.path_for(cfg, created["id"]).exists()


def test_holder_is_released_after_the_turn_even_on_error(tmp_path):
    class Boom(Exception):
        pass

    class H:
        def start(self, prompt, cwd, settings):
            raise Boom("harness exploded")

        def resume(self, *a, **k):
            raise AssertionError

        def interrupt(self, *a, **k):
            pass

    harness_module._REGISTRY["fake"] = H
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "title", "body", None, 0, "pm", "pm", assignee="builder")

    # _run_one runs in a background thread; an unexpected exception there
    # must still release the holder via the `finally` block rather than
    # stranding it — and, since this is a real regression fix, must also
    # be caught and escalated rather than just propagating out of the
    # thread silently (see test_unexpected_crash_is_escalated_not_retried_silently).
    threads = dispatch.tick(cfg)
    _join_all(threads)

    assert holders.is_held(cfg.project_root, created["id"]) is False


def test_unexpected_crash_is_escalated_not_retried_silently(tmp_path):
    """Real regression: an unexpected exception (a bug, not a harness/model
    judgment call) used to just propagate out of the thread uncaught —
    nothing about it produced a comment, so the circuit breaker never saw
    it either, meaning a persistent bug would retry silently
    every tick forever with zero visibility anywhere but the daemon log."""
    class Boom(Exception):
        pass

    class H:
        def start(self, *a, **k):
            raise Boom("something broke")

        def resume(self, *a, **k):
            raise AssertionError

        def interrupt(self, *a, **k):
            pass

    harness_module._REGISTRY["fake"] = H
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "title", "body", None, 0, "pm", "pm", assignee="builder")

    _join_all(dispatch.tick(cfg))

    final = issues.get_issue(cfg, created["id"])
    assert final["state"] == "blocked", "a bug stops the issue rather than retrying it"
    assert final["assignee"] == "builder", "reopening it retries the same role"
    event = final["assignment_history"][-1]
    assert event["type"] == "event" and event["kind"] == "blocked"
    assert "Boom" in event["reason"] and "something broke" in event["reason"]
    assert not _daemon_comments(final)


def test_circuit_breaker_stops_dispatch_and_returns_past_the_comment_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(dispatch, "MAX_COMMENTS", 3)

    class H:
        def start(self, *a, **k):
            raise AssertionError("must never be dispatched past the comment limit")

        def resume(self, *a, **k):
            raise AssertionError

        def interrupt(self, *a, **k):
            pass

    harness_module._REGISTRY["fake"] = H
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "title", "body", None, 0, "pm", "pm", assignee="builder")
    for n in range(3):
        issues.add_comment(cfg, created["id"], f"turn {n}", "builder", "builder")
    assert issues.comment_count(issues.get_issue(cfg, created["id"])) == 3

    threads = dispatch.tick(cfg)

    assert threads == [], "must not spawn once the comment limit is reached"
    final = issues.get_issue(cfg, created["id"])
    assert final["state"] == "blocked", "the circuit breaker must mark the issue blocked"
    assert final["assignee"] == "builder", "reopening it retries the same role"
    event = final["assignment_history"][-1]
    assert event["kind"] == "blocked" and "circuit breaker" in event["reason"]
    assert not _daemon_comments(final)


def test_reopening_resets_the_circuit_breaker(tmp_path, monkeypatch):
    """A person who reads the reason and reopens the issue gets a fresh count."""
    monkeypatch.setattr(dispatch, "MAX_COMMENTS", 3)

    class H:
        runs = 0

        def start(self, prompt, cwd, settings):
            H.runs += 1
            return TurnResult("s1", "Done.\nNEXT: return", {}, True)

        def resume(self, *a, **k):
            raise AssertionError

        def interrupt(self, *a, **k):
            pass

    harness_module._REGISTRY["fake"] = H
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "title", "body", None, 0, "pm", "pm", assignee="builder")
    for n in range(3):
        issues.add_comment(cfg, created["id"], f"turn {n}", "builder", "builder")
    _join_all(dispatch.tick(cfg))
    assert issues.get_issue(cfg, created["id"])["state"] == "blocked"
    assert H.runs == 0

    current = issues.get_issue(cfg, created["id"])
    issues.update_issue(cfg, created["id"], current["hash"], {"state": "open"}, "coordinator", "coordinator")
    assert issues.turns_since_reopen(issues.get_issue(cfg, created["id"])) == 0
    _join_all(dispatch.tick(cfg))
    assert H.runs == 1, "the reopened issue runs again"


def test_a_daemon_reopen_does_not_reset_the_breaker(tmp_path, monkeypatch):
    monkeypatch.setattr(dispatch, "MAX_COMMENTS", 3)
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "title", "body", None, 0, "pm", "pm", assignee="builder")
    for n in range(3):
        issues.add_comment(cfg, created["id"], f"turn {n}", "builder", "builder")
    current = issues.get_issue(cfg, created["id"])
    issues.update_issue(cfg, created["id"], current["hash"], {"state": "blocked"}, "daemon", "daemon")
    current = issues.get_issue(cfg, created["id"])
    issues.update_issue(cfg, created["id"], current["hash"], {"state": "open"}, "daemon", "daemon")
    assert issues.turns_since_reopen(issues.get_issue(cfg, created["id"])) == 3
    assert dispatch._too_many_comments(cfg, issues.get_issue(cfg, created["id"]))


def test_each_tick_sweeps_finished_worktrees_without_stopping_dispatch(tmp_path):
    class H:
        def start(self, prompt, cwd, settings):
            return TurnResult("s1", "Done.\nNEXT: return", {}, True)

        def resume(self, *a, **k):
            raise AssertionError

        def interrupt(self, *a, **k):
            pass

    harness_module._REGISTRY["fake"] = H
    cfg = make_project(tmp_path)
    old = issues.create_issue(cfg, "old", "b", None, 0, "coordinator", "coordinator", assignee="builder")
    worktrees.create(cfg, old["id"], old["base_branch"])
    current = issues.get_issue(cfg, old["id"])
    issues.update_issue(cfg, old["id"], current["hash"], {"state": "canceled"}, "coordinator", "coordinator")
    new = issues.create_issue(cfg, "new", "b", None, 0, "coordinator", "coordinator", assignee="builder")

    _join_all(dispatch.tick(cfg))

    assert not worktrees.path_for(cfg, old["id"]).exists()
    assert issues.get_issue(cfg, new["id"])["state"] == "blocked", \
        "the new issue still ran: returning with nobody above blocks it"


def test_a_failing_sweep_is_logged_and_does_not_stop_the_tick(tmp_path, monkeypatch, capsys):
    def broken(cfg, limit=5):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(worktrees, "sweep", broken)
    harness_module._REGISTRY["fake"] = lambda: type("H", (), {
        "start": lambda self, *a, **k: TurnResult("s1", "Done.\nNEXT: return", {}, True),
        "resume": lambda self, *a, **k: None, "interrupt": lambda self, *a, **k: None})()
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "t", "b", None, 0, "pm", "pm", assignee="builder")
    _join_all(dispatch.tick(cfg))
    assert "disk on fire" in capsys.readouterr().err
    assert issues.get_issue(cfg, created["id"])["assignee"] == "pm"


def test_cost_is_recorded_once_with_the_final_comment(tmp_path):
    from mcp import cost as cost_module

    class H:
        def start(self, prompt, cwd, settings):
            return TurnResult("s1", "Done.\nNEXT: return", {}, True,
                              usage=[cost_module.record("m-a", usd=0.25, input=10)])

        def resume(self, *a, **k):
            raise AssertionError

        def interrupt(self, *a, **k):
            pass

    harness_module._REGISTRY["fake"] = H
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "title", "body", None, 0, "pm", "pm", assignee="builder")
    _join_all(dispatch.tick(cfg))
    final = issues.get_issue(cfg, created["id"])
    assert len(final["turns"]) == 1
    assert final["turns"][0]["role"] == "builder" and final["turns"][0]["model"] == "m-a"
    assert final["turns"][0]["usd"] == 0.25 and final["turns"][0]["seconds"] >= 0
    assert final["cost_usd"] == 0.25


def test_a_corrected_turn_on_a_cumulative_harness_is_not_double_counted(tmp_path):
    from mcp import cost as cost_module

    class H:
        def start(self, prompt, cwd, settings):
            return TurnResult("s1", "Report.", {}, True, usage=[cost_module.record("m", usd=0.1)],
                              usage_cumulative=True)

        def resume(self, session_id, prompt, cwd, settings):
            # cumulative: the session's total so far, including the first call
            return TurnResult(session_id, "NEXT: return", {}, True,
                              usage=[cost_module.record("m", usd=0.15)], usage_cumulative=True)

        def interrupt(self, *a, **k):
            pass

    harness_module._REGISTRY["fake"] = H
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "title", "body", None, 0, "pm", "pm", assignee="builder")
    _join_all(dispatch.tick(cfg))
    assert issues.get_issue(cfg, created["id"])["cost_usd"] == 0.15, "0.25 would be double-counted"


def test_a_per_call_harness_adds_a_correction_to_the_first_call(tmp_path):
    from mcp import cost as cost_module

    class H:
        def start(self, prompt, cwd, settings):
            return TurnResult("s1", "Report.", {}, True, usage=[cost_module.record("m", usd=0.1)])

        def resume(self, session_id, prompt, cwd, settings):
            return TurnResult(session_id, "NEXT: return", {}, True,
                              usage=[cost_module.record("m", usd=0.05)])

        def interrupt(self, *a, **k):
            pass

    harness_module._REGISTRY["fake"] = H
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "title", "body", None, 0, "pm", "pm", assignee="builder")
    _join_all(dispatch.tick(cfg))
    final = issues.get_issue(cfg, created["id"])
    assert final["cost_usd"] == 0.15
    assert len(final["turns"]) == 1, "one turn, one record per model"


def test_a_multi_model_turn_records_each_model_with_the_time_on_the_first(tmp_path):
    from mcp import cost as cost_module

    class H:
        def start(self, prompt, cwd, settings):
            return TurnResult("s1", "Done.\nNEXT: return", {}, True, usage=[
                cost_module.record("m-a", usd=0.2), cost_module.record("m-b", usd=0.3)])

        def resume(self, *a, **k):
            raise AssertionError

        def interrupt(self, *a, **k):
            pass

    harness_module._REGISTRY["fake"] = H
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "title", "body", None, 0, "pm", "pm", assignee="builder")
    _join_all(dispatch.tick(cfg))
    turns = issues.get_issue(cfg, created["id"])["turns"]
    assert [t["model"] for t in turns] == ["m-a", "m-b"]
    assert turns[0]["seconds"] >= 0 and turns[1]["seconds"] == 0


def test_work_already_under_way_goes_before_a_higher_priority_new_issue(tmp_path):
    class H:
        def start(self, prompt, cwd, settings):
            return TurnResult("s1", "Done.\nNEXT: assign reviewer", {}, True)

        def resume(self, *a, **k):
            raise AssertionError

        def interrupt(self, *a, **k):
            pass

    harness_module._REGISTRY["fake"] = H
    cfg = make_project(tmp_path, allowed_tools="\n  builder: [assign_issue]")
    started = issues.create_issue(cfg, "started", "b", None, 5, "coordinator", "coordinator",
                                  assignee="pm")
    issues.push_assignee(cfg, started["id"], "builder", "pm", "pm")  # a hand-off: it has started
    issues.create_issue(cfg, "urgent", "b", None, 0, "coordinator", "coordinator", assignee="builder")
    _join_all(dispatch.tick(cfg))
    assert issues.get_issue(cfg, started["id"])["assignee"] == "reviewer", \
        "the started issue went first, despite its lower priority"


def test_nothing_waits_after_a_burst_ends(tmp_path):
    """There is no cooldown: when one issue's work is over, the next starts at
    the next tick. Spending is the only thing that paces the queue."""
    class H:
        def start(self, prompt, cwd, settings):
            return TurnResult("s1", "Done.\nNEXT: return", {}, True)

        def resume(self, *a, **k):
            raise AssertionError

        def interrupt(self, *a, **k):
            pass

    harness_module._REGISTRY["fake"] = H
    cfg = make_project(tmp_path)
    first = issues.create_issue(cfg, "first", "b", None, 0, "coordinator", "coordinator", assignee="builder")
    second = issues.create_issue(cfg, "second", "b", None, 0, "coordinator", "coordinator", assignee="builder")
    _join_all(dispatch.tick(cfg))
    _join_all(dispatch.tick(cfg))
    assert issues.get_issue(cfg, first["id"])["state"] == "blocked"
    assert issues.get_issue(cfg, second["id"])["state"] == "blocked", "the second ran at the next tick"


def test_circuit_breaker_does_not_trip_below_the_comment_limit(tmp_path):
    class H:
        def start(self, prompt, cwd, settings):
            return TurnResult("s1", "Done.\nNEXT: return", {}, True)

        def resume(self, *a, **k):
            raise AssertionError

        def interrupt(self, *a, **k):
            pass

    harness_module._REGISTRY["fake"] = H
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "title", "body", None, 0, "pm", "pm", assignee="builder")
    issues.add_comment(cfg, created["id"], "one normal turn", "builder", "builder")

    _join_all(dispatch.tick(cfg))

    assert issues.get_issue(cfg, created["id"])["assignee"] == "pm"


def test_blocked_state_blocks_dispatch_regardless_of_assignee(tmp_path):
    class H:
        def start(self, *a, **k):
            raise AssertionError("a blocked issue must never be dispatched")

        def resume(self, *a, **k):
            raise AssertionError

        def interrupt(self, *a, **k):
            pass

    harness_module._REGISTRY["fake"] = H
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "title", "body", None, 0, "pm", "pm", assignee="builder",
                                   state="blocked")

    threads = dispatch.tick(cfg)

    assert threads == []
    assert issues.get_issue(cfg, created["id"])["assignee"] == "builder"  # untouched, not even returned


def test_clearing_blocked_resumes_dispatch(tmp_path):
    class H:
        def start(self, prompt, cwd, settings):
            return TurnResult("s1", "Done.\nNEXT: return", {}, True)

        def resume(self, *a, **k):
            raise AssertionError

        def interrupt(self, *a, **k):
            pass

    harness_module._REGISTRY["fake"] = H
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "title", "body", None, 0, "pm", "pm", assignee="builder",
                                   state="blocked")
    issues.update_issue(cfg, created["id"], created["hash"], {"state": "open"}, "coordinator", "coordinator")

    _join_all(dispatch.tick(cfg))

    assert issues.get_issue(cfg, created["id"])["assignee"] == "pm"


def test_prompt_includes_assignment_history(tmp_path):
    seen_prompt = {}

    class H:
        def start(self, prompt, cwd, settings):
            seen_prompt["text"] = prompt
            return TurnResult("s1", "Done.\nNEXT: return", {}, True)

        def resume(self, *a, **k):
            raise AssertionError

        def interrupt(self, *a, **k):
            pass

    harness_module._REGISTRY["fake"] = H
    cfg = make_project(tmp_path)
    issues.create_issue(cfg, "title", "body", None, 0, "pm", "pm", assignee="builder")

    _join_all(dispatch.tick(cfg))

    assert "## Assignment history" in seen_prompt["text"]
    assert "-> builder (by pm)" in seen_prompt["text"]


def test_an_event_is_in_the_history_the_next_role_is_shown(tmp_path):
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "title", "body", None, 0, "pm", "pm", assignee="builder")
    issues.add_event(cfg, created["id"], "blocked", "circuit breaker: 30 turns")
    issue = issues.get_issue(cfg, created["id"])
    assert issue["assignment_history"][-1]["role"] == "builder"
    assert "blocked (circuit breaker: 30 turns)" in dispatch._format_history(issue["assignment_history"])
    assert issues.comment_count(issue) == 0, "an event is not a comment"
    assert issues.return_assignee(cfg, created["id"], "builder", "builder")["assignee"] == "pm", \
        "an event does not disturb the assignment stack"


def test_blocking_never_reopens_an_issue_that_already_closed(tmp_path):
    """A crash after a turn closed its issue must not turn merged back into blocked."""
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "title", "body", None, 0, "pm", "pm", assignee="builder")
    issues.update_issue(cfg, created["id"], created["hash"], {"state": "canceled"}, "pm", "pm")
    dispatch._block(cfg, created["id"], "something broke afterwards")
    issue = issues.get_issue(cfg, created["id"])
    assert issue["state"] == "canceled"
    assert issue["assignment_history"][-1]["reason"] == "something broke afterwards"


def test_prompt_never_contains_the_agent_files_frontmatter(tmp_path):
    """Real regression: _build_prompt used to pass agents/<role>.md's raw
    file content straight through, frontmatter included — a leading
    `---` in a CLI argument value gets misread as an option by the real
    `claude` binary's argument parser, so every real spawn failed before
    the model ever ran. No existing test caught it because every fixture
    agent file was a bare string with no frontmatter at all."""
    seen_prompt = {}

    class H:
        def start(self, prompt, cwd, settings):
            seen_prompt["text"] = prompt
            return TurnResult("s1", "Done.\nNEXT: return", {}, True)

        def resume(self, *a, **k):
            raise AssertionError

        def interrupt(self, *a, **k):
            pass

    harness_module._REGISTRY["fake"] = H
    cfg = make_project(tmp_path)
    issues.create_issue(cfg, "title", "body", None, 0, "pm", "pm", assignee="builder")

    _join_all(dispatch.tick(cfg))

    assert not seen_prompt["text"].startswith("---")
    assert "name: builder" not in seen_prompt["text"]
    assert "You are the builder." in seen_prompt["text"]


def test_transient_harness_launch_failure_is_retried_and_the_turn_completes(tmp_path, monkeypatch):
    from mcp import retry as retry_module
    monkeypatch.setattr(retry_module, "BACKOFF_SECONDS", 0)
    attempts = {"n": 0}

    class H:
        def start(self, prompt, cwd, settings):
            attempts["n"] += 1
            if attempts["n"] == 1:
                return TurnResult("", "(exit 1, no output)", {"returncode": 1}, False, never_ran=True)
            return TurnResult("s1", "Done.\nNEXT: return", {}, True)

        def resume(self, *a, **k):
            raise AssertionError("should not need a correction")

        def interrupt(self, *a, **k):
            pass

    harness_module._REGISTRY["fake"] = H
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "title", "body", None, 0, "pm", "pm", assignee="builder")
    _join_all(dispatch.tick(cfg))

    final = issues.get_issue(cfg, created["id"])
    assert attempts["n"] == 2
    assert final["assignee"] == "pm"
    assert final["assignment_history"][-1]["type"] == "return"
    assert "unexpected error" not in "\n".join(line["text"] for line in final["lines"])


class _Unreachable:
    """A provider that refuses every call: a usage limit, an outage."""
    calls = 0
    text = "You have hit your usage limit"

    def start(self, prompt, cwd, settings):
        type(self).calls += 1
        return TurnResult("", self.text, {"returncode": 1}, False, never_ran=True)

    def resume(self, *a, **k):
        raise AssertionError

    def interrupt(self, *a, **k):
        pass


def test_a_provider_failure_changes_nothing_and_pauses(tmp_path, monkeypatch):
    """The turn never ran, so it says nothing about the issue. It used to be
    returned up the stack as if it had finished, and canceled at the top."""
    from mcp import retry as retry_module
    monkeypatch.setattr(retry_module, "BACKOFF_SECONDS", 0)
    _Unreachable.calls = 0
    harness_module._REGISTRY["fake"] = _Unreachable
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "title", "body", None, 0, "coordinator", "coordinator",
                                  assignee="builder")
    before = issues.get_issue(cfg, created["id"])

    _join_all(dispatch.tick(cfg))

    assert issues.get_issue(cfg, created["id"]) == before, "not one byte of the issue changes"
    assert _Unreachable.calls == retry_module.ATTEMPTS
    state = pause.active(cfg.project_root)
    assert state and "usage limit" in state["reason"]
    assert not holders.is_held(cfg.project_root, created["id"])
    assert dispatch.tick(cfg) == [], "nothing else starts while paused"
    assert _Unreachable.calls == retry_module.ATTEMPTS
    assert "paused" in dispatch.next_action(cfg)


def test_the_pause_backs_off_and_clears_after_a_good_turn(tmp_path, monkeypatch):
    from mcp import retry as retry_module
    monkeypatch.setattr(retry_module, "BACKOFF_SECONDS", 0)
    harness_module._REGISTRY["fake"] = _Unreachable
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "title", "body", None, 0, "pm", "pm", assignee="builder")
    root = cfg.project_root

    _join_all(dispatch.tick(cfg))
    first = pause.active(root)
    assert first["failures"] == 1 and first["until"] - time.time() <= 60

    pause.record_failure(root, "again")
    second = pause.active(root)
    assert second["failures"] == 2 and 60 < second["until"] - time.time() <= 120

    for _ in range(10):
        pause.record_failure(root, "again")
    assert pause.active(root)["until"] - time.time() <= 1800, "the wait is capped"

    _expire_pause(root)
    assert pause.active(root) is None

    class Back:
        def start(self, prompt, cwd, settings):
            return TurnResult("s1", "Done.\nNEXT: return", {}, True)

        def resume(self, *a, **k):
            raise AssertionError

        def interrupt(self, *a, **k):
            pass

    harness_module._REGISTRY["fake"] = Back
    _join_all(dispatch.tick(cfg))
    assert issues.get_issue(cfg, created["id"])["assignee"] == "pm", "the same issue carries on"
    pause.record_failure(root, "later")
    assert pause.active(root)["failures"] == 1, "a good turn resets the backoff"


def test_a_failure_during_a_correction_also_leaves_the_issue_alone(tmp_path, monkeypatch):
    from mcp import retry as retry_module
    monkeypatch.setattr(retry_module, "BACKOFF_SECONDS", 0)

    class H:
        def start(self, prompt, cwd, settings):
            return TurnResult("s1", "A report with no directive.", {}, True)

        def resume(self, session_id, prompt, cwd, settings):
            return TurnResult(session_id, "limit", {"returncode": 1}, False, never_ran=True)

        def interrupt(self, *a, **k):
            pass

    harness_module._REGISTRY["fake"] = H
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "title", "body", None, 0, "pm", "pm", assignee="builder")
    before = issues.get_issue(cfg, created["id"])
    _join_all(dispatch.tick(cfg))
    assert issues.get_issue(cfg, created["id"]) == before
    assert pause.active(cfg.project_root)


def test_a_turn_that_spent_tokens_is_not_a_launch_failure(tmp_path):
    """It ran and may have changed files, so running it again blindly is not
    safe. It goes through the normal path, and is never retried from scratch."""
    class H:
        def __init__(self):
            self.starts = 0

        def start(self, prompt, cwd, settings):
            self.starts += 1
            return TurnResult("s1", "It went wrong halfway.", {"returncode": 1}, False)

        def resume(self, session_id, prompt, cwd, settings):
            return TurnResult(session_id, "NEXT: return", {}, True)

        def interrupt(self, *a, **k):
            pass

    h = H()
    harness_module._REGISTRY["fake"] = lambda: h
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "title", "body", None, 0, "pm", "pm", assignee="builder")
    _join_all(dispatch.tick(cfg))
    final = issues.get_issue(cfg, created["id"])
    assert h.starts == 1
    assert pause.active(cfg.project_root) is None
    assert final["assignee"] == "pm"
    assert "It went wrong halfway." in "\n".join(line["text"] for line in final["lines"])


def test_the_full_error_is_kept_not_cut_at_500_chars(tmp_path, monkeypatch, capsys):
    """The reason used to be cut off before the part that said what went wrong."""
    from mcp import retry as retry_module
    monkeypatch.setattr(retry_module, "BACKOFF_SECONDS", 0)

    class Long(_Unreachable):
        text = "x" * 1000 + "THE ACTUAL REASON"

    harness_module._REGISTRY["fake"] = Long
    cfg = make_project(tmp_path)
    issues.create_issue(cfg, "title", "body", None, 0, "pm", "pm", assignee="builder")
    _join_all(dispatch.tick(cfg))
    assert "THE ACTUAL REASON" in pause.active(cfg.project_root)["reason"]
    assert "THE ACTUAL REASON" in capsys.readouterr().err


def test_a_bug_in_the_harness_is_not_retried(tmp_path, monkeypatch):
    from mcp import retry as retry_module
    monkeypatch.setattr(retry_module, "BACKOFF_SECONDS", 0)
    attempts = {"n": 0}

    class H:
        def start(self, prompt, cwd, settings):
            attempts["n"] += 1
            raise ValueError("a bug in the adapter")

        def resume(self, *a, **k):
            raise AssertionError

        def interrupt(self, *a, **k):
            pass

    harness_module._REGISTRY["fake"] = H
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "title", "body", None, 0, "pm", "pm", assignee="builder")
    _join_all(dispatch.tick(cfg))

    assert attempts["n"] == 1
    assert issues.get_issue(cfg, created["id"])["state"] == "blocked"


def test_a_long_turn_is_capped_in_its_comment_but_routed_from_its_full_text(tmp_path, capsys):
    """Comments must stay bounded whatever the turn printed, and the NEXT:
    line at the end still routes the issue. The full output goes to stderr."""
    long_output = "x" * 20000 + "\nNEXT: return"

    class H:
        def start(self, prompt, cwd, settings):
            return TurnResult("s1", long_output, {}, True)

        def resume(self, *a, **k):
            raise AssertionError

        def interrupt(self, *a, **k):
            pass

    harness_module._REGISTRY["fake"] = H
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "title", "body", None, 0, "pm", "pm", assignee="builder")
    _join_all(dispatch.tick(cfg))

    final = issues.get_issue(cfg, created["id"])
    assert final["assignee"] == "pm"
    assert final["assignment_history"][-1]["type"] == "return"
    comment = next(line["text"] for line in final["lines"] if "omitted" in line["text"] or line["text"].startswith("xxxx"))
    assert len(comment) < dispatch.COMMENT_LIMIT + 300
    body_text = "\n".join(line["text"] for line in final["lines"])
    assert "NEXT: return" not in body_text, "the machine directive is not prose for a reader"
    assert "_→" not in body_text, "the comment is the report; the outcome lives in the issue state"
    assert long_output in capsys.readouterr().err


def test_a_role_is_never_run_in_the_main_checkout(tmp_path):
    """No worktree means no run: falling back to the main checkout would give
    the agent write access to main, so the turn must refuse instead."""
    cfg = make_project(tmp_path)
    issue = {"id": 7, "base_branch": "main", "lines": [], "assignment_history": []}
    with pytest.raises(RuntimeError, match="no worktree"):
        dispatch._cwd_and_settings(cfg, "builder", "t", issue["id"])


def test_the_prompt_tells_the_role_its_workspace_and_names_no_other_checkout(tmp_path):
    cfg = make_project(tmp_path)
    issue = {"id": 7, "title": "t", "base_branch": "main", "lines": [{"text": "body"}],
             "assignment_history": []}
    prompt = dispatch._build_prompt(cfg, "builder", issue, {"dir": str(tmp_path), "url": "/shared/7/abc"})
    assert "## Your workspace" in prompt
    assert "issue/7" in prompt
    assert "relative to it" in prompt
    assert str(cfg.public_repo) not in prompt
    assert "packed-refs.lock" in prompt, "the expected sandbox message is explained, not investigated"


def test_a_closing_verb_sets_the_state_and_keeps_the_closing_role(tmp_path):
    class H:
        def start(self, prompt, cwd, settings):
            return TurnResult("s1", "Accepted, no code.\nNEXT: done", {}, True)

        def resume(self, *a, **k):
            raise AssertionError

        def interrupt(self, *a, **k):
            pass

    harness_module._REGISTRY["fake"] = H
    cfg = make_project(tmp_path, allowed_tools="\n  builder: [update_issue]")
    created = issues.create_issue(cfg, "title", "body", None, 0, "pm", "pm", assignee="builder")
    _join_all(dispatch.tick(cfg))
    final = issues.get_issue(cfg, created["id"])
    assert final["state"] == "done"
    assert final["assignee"] == "builder", \
        "a closed issue stays with the role that closed it, not the creator"


def test_a_closing_verb_from_a_role_not_allowed_to_close_is_a_bad_directive(tmp_path):
    calls = {"resume": 0}

    class H:
        def start(self, prompt, cwd, settings):
            return TurnResult("s1", "Accepted.\nNEXT: done", {}, True)

        def resume(self, session_id, prompt, cwd, settings):
            calls["resume"] += 1
            return TurnResult("s1", "Handing back.\nNEXT: return", {}, True)

        def interrupt(self, *a, **k):
            pass

    harness_module._REGISTRY["fake"] = H
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "title", "body", None, 0, "pm", "pm", assignee="builder")
    _join_all(dispatch.tick(cfg))
    final = issues.get_issue(cfg, created["id"])
    assert calls["resume"] == 1
    assert final["state"] == "open"
    assert final["assignee"] == "pm"




def test_a_file_written_straight_into_the_artifacts_folder_is_linked_by_its_path(tmp_path):
    class H:
        def start(self, prompt, cwd, settings):
            folder = Path(settings["shared"]["dir"])
            (folder / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n")
            return TurnResult("s1", f"Done.\n\n![shot]({folder}/shot.png) and "
                                    f"![gone]({folder}/missing.png)\nNEXT: return", {}, True)

        def resume(self, *a, **k):
            raise AssertionError

        def interrupt(self, *a, **k):
            pass

    harness_module._REGISTRY["fake"] = H
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "title", "body", None, 0, "pm", "pm", assignee="builder")

    _join_all(dispatch.tick(cfg))

    body_text = "\n".join(line["text"] for line in issues.get_issue(cfg, created["id"])["lines"])
    assert f"![shot](/shared/{created['id']}/" in body_text, "linked where the kitchen can serve it"
    assert "![gone](" in body_text and "![gone](/shared/" not in body_text, \
        "a file the agent never wrote is left as written, not pointed at the kitchen"
    assert "shared:" not in body_text


def test_markdown_links_to_artifacts_are_copied_and_rewritten(tmp_path):
    """Images and plain links alike; a link to a file the repo tracks, a
    remote URL, a missing file and a non-artifact type stay exactly as written."""
    repo = tmp_path / "wt"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "README.md").write_text("tracked docs")
    (repo / "main.js").write_text("code")
    subprocess.run(["git", "add", "README.md", "main.js"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "i"],
                   cwd=repo, check=True)
    (repo / "out").mkdir()
    (repo / "out" / "run.log").write_text("log line")
    (repo / "out" / "report.md").write_text("# findings")
    (repo / "after.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    shared = {"dir": str(tmp_path / "shared"), "url": "/shared/9/abc"}
    Path(shared["dir"]).mkdir()

    text = ("![after](after.png) [run log](out/run.log) [findings](out/report.md) "
            "[docs](README.md) [code](main.js) [site](https://example.com/x.png) "
            "[nope](out/missing.log) [exe](out/tool.exe)")
    out = dispatch._relocate_artifacts(text, shared, str(repo))

    assert "![after](/shared/9/abc/after.png)" in out
    assert "[run log](/shared/9/abc/run.log)" in out
    assert "[findings](/shared/9/abc/report.md)" in out
    assert "[docs](README.md)" in out, "a tracked file is a reference to the repo, not an artifact"
    assert "[code](main.js)" in out
    assert "[site](https://example.com/x.png)" in out
    assert "[nope](out/missing.log)" in out
    assert "[exe](out/tool.exe)" in out
    assert (Path(shared["dir"]) / "run.log").read_text() == "log line"
    assert not (repo / "out" / "tool.exe").exists()


def test_a_screenshot_named_in_backticks_is_still_embedded(tmp_path):
    """The failure seen in practice: `test-results/x.png` as inline code shows no
    picture. A real untracked image named that way is embedded; a tracked
    file, a missing one, and a fenced block are left alone."""
    repo = tmp_path / "wt"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    subprocess.run(["git", "add", "logo.png"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "i"],
                   cwd=repo, check=True)
    (repo / "test-results").mkdir()
    (repo / "test-results" / "open.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    shared = {"dir": str(tmp_path / "shared"), "url": "/shared/9/abc"}
    Path(shared["dir"]).mkdir()

    out = dispatch._relocate_artifacts(
        "- Open cancelled: `test-results/open.png`\n- logo `logo.png` and `gone.png`\n"
        "```\ntest-results/open.png\n```", shared, str(repo))

    assert "![open](/shared/9/abc/open.png)" in out
    assert "`logo.png`" in out and "`gone.png`" in out
    assert "```\ntest-results/open.png\n```" in out


def test_static_parts_are_the_common_prefix_before_the_role_prompt(tmp_path):
    """Prompt caches are prefix-based. Everything every role shares — shared
    context, workspace rules, posting rules — must come before the role
    prompt, so two roles start with the same bytes. The issue id, branch and
    artifacts folder live in the issue section instead, so the static text
    stays identical across issues too."""
    cfg = make_project(tmp_path)
    (cfg.project_root / "context.md").write_text("Shared facts every role needs.")
    issue = {"id": 7, "title": "t", "base_branch": "main", "lines": [],
             "assignment_history": []}
    shared = {"dir": "/tmp/shared/7/abc", "url": "/shared/7/abc"}
    builder = dispatch._build_prompt(cfg, "builder", issue, shared)
    reviewer = dispatch._build_prompt(cfg, "reviewer", issue, shared)
    prefix = "## Shared context\nShared facts every role needs."
    assert builder.startswith(prefix) and reviewer.startswith(prefix)
    builder_role = builder.index("You are the builder.")
    reviewer_role = reviewer.index("You are the reviewer.")
    assert builder[:builder_role] == reviewer[:reviewer_role]
    assert "## Your workspace" in builder[:builder_role]
    assert "## Posting your report" in builder[:builder_role]
    assert "## How the turn ends" in builder[:builder_role]
    assert "issue/7" not in builder[:builder_role], \
        "the issue id and branch are issue-specific, so they belong after the role prompt"
    assert "Branch `issue/7`, created from `main`." in builder
    assert "Artifacts folder for this turn: `/tmp/shared/7/abc`." in builder
    assert "Allowed directives:" in builder[builder_role:], \
        "the role-specific values stay at the end, after the issue and history"


def test_the_prompt_names_the_artifacts_folder(tmp_path):
    cfg = make_project(tmp_path)
    issue = {"id": 7, "title": "t", "base_branch": "main", "lines": [],
             "assignment_history": []}
    prompt = dispatch._build_prompt(cfg, "builder", issue,
                                    {"dir": "/tmp/shared/7/abc", "url": "/shared/7/abc"})
    assert "## Your workspace" in prompt and "issue/7" in prompt
    assert "## Posting your report" in prompt
    assert "/tmp/shared/7/abc" in prompt
    assert "shared:" not in prompt, "markdown links are the only convention"
    assert "[run log](out/run.log)" in prompt
    assert "\n![dark mode toggle, on](test-results/dark-mode-on.png)\n" in prompt, \
        "the embed example is not wrapped in backticks, so the model can't copy the code form"
    assert "backticks" in prompt, "the agent is told a path in backticks shows no picture"
    assert "rewrites" in prompt, "the agent is told the daemon rewrites the link"
    assert "a log" in prompt and "exported file" in prompt, "artifacts are not just screenshots"


def test_markdown_images_are_copied_into_the_shared_folder(tmp_path):
    """Standard markdown is the convention: the daemon copies the referenced
    file out of the worktree and rewrites the link, so the issue holds no
    binary and the repo sees nothing."""
    class H:
        def start(self, prompt, cwd, settings):
            (Path(cwd) / "test-results").mkdir(exist_ok=True)
            (Path(cwd) / "test-results" / "after.png").write_bytes(b"\x89PNG\r\n\x1a\n")
            return TurnResult("s1", "Done.\n\n![after](test-results/after.png)\n\n"
                                    "![missing](nope.png)\nNEXT: return", {}, True)

        def resume(self, *a, **k):
            raise AssertionError

        def interrupt(self, *a, **k):
            pass

    harness_module._REGISTRY["fake"] = H
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "title", "body", None, 0, "pm", "pm", assignee="builder")

    _join_all(dispatch.tick(cfg))

    body_text = "\n".join(line["text"] for line in issues.get_issue(cfg, created["id"])["lines"])
    assert f"![after](/shared/{created['id']}/" in body_text
    assert "![missing](nope.png)" in body_text, "a path that does not exist stays as written"
    copied = list((cfg.project_root / ".agents-daemon" / "shared" / str(created["id"])).glob("*/after.png"))
    assert copied and copied[0].read_bytes().startswith(b"\x89PNG")




def test_an_assign_to_something_that_is_not_a_role_is_no_directive():
    text = "Done.\nNEXT: assign coordinator"
    assert dispatch._parse_next(text, may_close=False, may_assign=True, roles=["builder", "pm"]) is None
    assert dispatch._parse_next("Done.\nNEXT: assign pm", may_close=False, may_assign=True,
                                roles=["builder", "pm"]) == ("assign", "pm")


def test_a_role_that_can_assign_is_told_which_roles_exist(tmp_path):
    cfg = make_project(tmp_path, allowed_tools="\n  builder: [assign_issue]")
    issue = {"id": 7, "title": "t", "base_branch": "main", "lines": [{"text": "body"}],
             "assignment_history": []}
    prompt = dispatch._build_prompt(cfg, "builder", issue, {"dir": str(tmp_path), "url": "/s"})
    assert "## Roles you can hand work to" in prompt
    assert "- reviewer: reviews things" in prompt
    assert "- builder:" not in prompt.split("## Roles you can hand work to")[1], "not itself"


def test_a_role_that_cannot_assign_is_not_offered_a_list(tmp_path):
    cfg = make_project(tmp_path)
    issue = {"id": 7, "title": "t", "base_branch": "main", "lines": [{"text": "body"}],
             "assignment_history": []}
    prompt = dispatch._build_prompt(cfg, "builder", issue, {"dir": str(tmp_path), "url": "/s"})
    assert "## Roles you can hand work to" not in prompt


def test_a_role_that_may_close_is_told_how_to_wait_on_other_issues(tmp_path):
    cfg = make_project(tmp_path, allowed_tools="\n  pm: [update_issue]")
    assert "NEXT: blocked_by #<id>" in dispatch._directive_line(cfg, "pm")
    assert "blocked_by" not in dispatch._directive_line(make_project(tmp_path / "x"), "builder")
