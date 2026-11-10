import pytest
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mcp import issues  # noqa: E402
from mcp.config import ProjectConfig  # noqa: E402


def make_project(tmp_path: Path) -> ProjectConfig:
    root = tmp_path / "instance"
    (root / "agents").mkdir(parents=True)
    for _role in ("builder", "reviewer", "pm", "researcher"):
        _f = root / "agents" / f"{_role}.md"
        if not _f.exists():
            _f.write_text(f"---\nname: {_role}\n---\n{_role}")
    (root / "project.yml").write_text("public_repo: ../public\n")
    (tmp_path / "public").mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root)
    return ProjectConfig.load(root)


def test_create_get_roundtrip(tmp_path):
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "Add satin underlay", "Body text here.",
                                   ["roadmap"], 5, "coordinator", "coordinator", assignee="builder")
    assert created["id"] == 1
    assert created["title"] == "Add satin underlay"
    assert created["state"] == "open"
    assert created["labels"] == ["roadmap"]
    assert created["priority"] == 5
    assert created["lines"][0]["text"] == "Body text here."
    assert all(line["hash"] == created["hash"] for line in created["lines"])

    fetched = issues.get_issue(cfg, 1)
    assert fetched == created


def test_a_role_that_creates_without_an_assignee_holds_the_issue(tmp_path):
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "title", "body", None, 0, "pm", "pm")
    assert created["assignee"] == "pm"
    assert created["assignment_history"] == [
        {"type": "assign", "role": "pm", "by": "pm",
         "at": created["assignment_history"][0]["at"]}
    ]


def test_the_coordinator_is_not_an_agent_and_can_never_hold_an_open_issue(tmp_path):
    cfg = make_project(tmp_path)
    for kwargs in ({}, {"assignee": "coordinator"}, {"assignee": "nobody"}):
        try:
            issues.create_issue(cfg, "title", "body", None, 0, "coordinator", "coordinator", **kwargs)
            assert False, f"expected a refusal for {kwargs}"
        except ValueError as exc:
            assert "role" in str(exc) and "builder" in str(exc), "the error must name the real roles"
    assert issues.list_issues(cfg) == []


def test_a_closed_record_may_be_filed_by_the_coordinator(tmp_path):
    """A decision written already `done` is not work for anyone, so it needs no role."""
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "decision", "why", ["decision"], 0,
                                  "coordinator", "coordinator", state="done")
    assert created["state"] == "done"


def test_assigning_to_something_that_is_not_a_role_is_refused(tmp_path):
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "title", "body", None, 0, "pm", "pm", assignee="builder")
    for target in ("coordinator", "nobody"):
        try:
            issues.push_assignee(cfg, created["id"], target, "builder", "builder")
            assert False, f"expected a refusal for {target}"
        except ValueError:
            pass
    assert issues.get_issue(cfg, created["id"])["assignee"] == "builder"


def test_create_can_delegate_immediately(tmp_path):
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "title", "body", None, 0, "pm", "pm", assignee="builder")
    assert created["assignee"] == "builder"
    assert created["assignment_history"][0] == {
        "type": "assign", "role": "builder", "by": "pm", "at": created["assignment_history"][0]["at"]}


def test_create_assigns_sequential_ids(tmp_path):
    cfg = make_project(tmp_path)
    a = issues.create_issue(cfg, "first", "body", None, 0, "coordinator", "coordinator", assignee="builder")
    b = issues.create_issue(cfg, "second", "body", None, 0, "coordinator", "coordinator", assignee="builder")
    assert (a["id"], b["id"]) == (1, 2)


def test_push_then_return_restores_previous_assignee(tmp_path):
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "title", "body", None, 0, "pm", "pm", assignee="builder")  # assignee: pm
    issues.push_assignee(cfg, created["id"], "builder", "pm", "pm")
    issues.push_assignee(cfg, created["id"], "reviewer", "builder (task x)", "builder")

    after_pushes = issues.get_issue(cfg, created["id"])
    assert after_pushes["assignee"] == "reviewer"
    assert [e["type"] for e in after_pushes["assignment_history"]] == ["assign", "assign", "assign"]

    returned_once = issues.return_assignee(cfg, created["id"], "reviewer (task y)", "reviewer")
    assert returned_once["assignee"] == "builder"  # back to whoever pushed reviewer, not pm

    returned_twice = issues.return_assignee(cfg, created["id"], "builder (task x)", "builder")
    assert returned_twice["assignee"] == "pm"  # back to whoever pushed builder

    history_types = [e["type"] for e in returned_twice["assignment_history"]]
    assert history_types == ["assign", "assign", "assign", "return", "return"], \
        "returns must append, never truncate — full chronology stays in the file"


def test_return_from_the_original_creator_raises(tmp_path):
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "title", "body", None, 0, "pm", "pm")
    try:
        issues.return_assignee(cfg, created["id"], "pm", "pm")
        assert False, "expected NothingToReturnTo"
    except issues.NothingToReturnTo:
        pass


def test_a_filer_that_is_not_a_role_is_not_on_the_stack(tmp_path):
    """The coordinator session filed it and assigned builder: nobody above builder."""
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "title", "body", None, 0, "coordinator", "coordinator",
                                  assignee="builder")
    try:
        issues.return_assignee(cfg, created["id"], "builder", "builder")
        assert False, "expected NothingToReturnTo"
    except issues.NothingToReturnTo:
        pass


def test_returning_with_no_one_above_blocks_the_issue(tmp_path):
    """A role without close rights must not be able to cancel by returning."""
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "title", "body", None, 0, "coordinator", "coordinator",
                                  assignee="builder")
    result = issues.return_or_block(cfg, created["id"], "builder", "builder")
    after = issues.get_issue(cfg, created["id"])
    assert result["blocked"] is True and after["state"] == "blocked"
    assert after["assignee"] == "builder"
    assert after["assignment_history"][-1]["kind"] == "nobody_above"
    assert issues.comment_count(after) == 0, "the reason is an event, not a comment"


def test_returning_an_issue_already_off_open_leaves_its_state_alone(tmp_path):
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "title", "body", None, 0, "coordinator", "coordinator",
                                  assignee="builder")
    issues.update_issue(cfg, created["id"], created["hash"], {"state": "blocked"}, "daemon", "daemon")
    result = issues.return_or_block(cfg, created["id"], "daemon", "daemon")
    assert result["blocked"] is False
    assert issues.get_issue(cfg, created["id"])["state"] == "blocked"


def test_returning_to_a_role_above_still_works_and_does_not_block(tmp_path):
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "title", "body", None, 0, "pm", "pm", assignee="builder")
    result = issues.return_or_block(cfg, created["id"], "builder", "builder")
    assert result["assignee"] == "pm"
    assert issues.get_issue(cfg, created["id"])["state"] == "open"


def test_update_with_correct_hash_succeeds(tmp_path):
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "title", "body", ["roadmap"], 0, "coordinator", "coordinator", assignee="builder")

    result = issues.update_issue(cfg, created["id"], created["hash"], {"state": "done", "priority": 9},
                                  "coordinator", "coordinator")

    assert result["ok"] is True
    assert result["state"] == "done"
    assert result["priority"] == 9
    assert result["hash"] != created["hash"]  # content changed, hash must move


def test_update_cannot_touch_assignee(tmp_path):
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "title", "body", None, 0, "pm", "pm", assignee="builder")
    result = issues.update_issue(cfg, created["id"], created["hash"],
                                  {"title": "renamed", "assignee": "reviewer"}, "pm", "pm")
    assert result["assignee"] == "builder", "update_issue must silently ignore assignee — only push/pop move it"


def test_update_with_stale_hash_is_rejected_and_returns_current(tmp_path):
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "title", "body", None, 0, "coordinator", "coordinator", assignee="builder")
    issues.update_issue(cfg, created["id"], created["hash"], {"title": "renamed"},
                         "coordinator", "coordinator")  # moves the hash forward

    stale_result = issues.update_issue(cfg, created["id"], created["hash"], {"title": "clobber attempt"},
                                        "reviewer", "reviewer")

    assert stale_result["ok"] is False
    assert stale_result["error"] == "stale hash"
    assert stale_result["current"]["title"] == "renamed"  # the real current state, not the rejected one


def test_comment_appends_without_needing_a_hash(tmp_path):
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "title", "body", None, 0, "coordinator", "coordinator", assignee="builder")

    r1 = issues.add_comment(cfg, created["id"], "first comment", "builder (task abc12345)", "builder")
    r2 = issues.add_comment(cfg, created["id"], "second comment", "reviewer", "reviewer")

    body_text = "\n".join(line["text"] for line in r2["lines"])
    assert "### " in body_text and "— builder (task abc12345)" in body_text
    assert "— reviewer" in body_text
    assert "first comment" in body_text and "second comment" in body_text
    assert body_text.index("first comment") < body_text.index("second comment"), \
        "comments stay in chronological order"


def test_list_issues_filters_and_sorts_by_priority(tmp_path):
    cfg = make_project(tmp_path)
    low = issues.create_issue(cfg, "low priority", "b", ["roadmap"], 50, "coordinator", "coordinator", assignee="builder")
    high = issues.create_issue(cfg, "high priority", "b", ["roadmap"], 1, "coordinator", "coordinator", assignee="builder")
    issues.create_issue(cfg, "a decision", "b", ["decision"], 0, "coordinator", "coordinator", assignee="builder")
    issues.update_issue(cfg, low["id"], low["hash"], {"state": "done"}, "coordinator", "coordinator")

    open_roadmap = issues.list_issues(cfg, state="open", label="roadmap")

    assert [i["id"] for i in open_roadmap] == [high["id"]]


def test_list_issues_filters_by_assignee(tmp_path):
    cfg = make_project(tmp_path)
    a = issues.create_issue(cfg, "a", "b", None, 0, "pm", "pm", assignee="builder")
    issues.create_issue(cfg, "b", "b", None, 0, "pm", "pm", assignee="reviewer")

    assert [i["id"] for i in issues.list_issues(cfg, assignee="builder")] == [a["id"]]


def test_list_issues_reports_updated_and_filters_by_since(tmp_path):
    cfg = make_project(tmp_path)
    old = issues.create_issue(cfg, "old", "b", None, 0, "pm", "pm", assignee="builder")
    new = issues.create_issue(cfg, "new", "b", None, 0, "pm", "pm", assignee="builder")
    for issue_id, at in ((old["id"], "2026-01-01T10:00:00Z"), (new["id"], "2026-03-01T10:00:00Z")):
        path = issues._path(cfg, issue_id)
        front, body = issues._parse(path.read_text())
        for entry in front["assignment_history"]:
            entry["at"] = at
        front["turns"] = [{"at": at, "role": "builder", "usd": 0}]
        path.write_text(issues._serialize(front, body))

    listed = {i["id"]: i["updated"] for i in issues.list_issues(cfg)}
    assert listed == {old["id"]: "2026-01-01T10:00:00Z", new["id"]: "2026-03-01T10:00:00Z"}
    assert [i["id"] for i in issues.list_issues(cfg, since="2026-02-01")] == [new["id"]]


def test_commits_are_authored_by_the_given_identity(tmp_path):
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "title", "body", None, 0, "builder (task abc12345)", "builder")

    log = subprocess.run(["git", "-C", str(cfg.project_root), "log", "-1", "--format=%an <%ae>"],
                          capture_output=True, text=True, check=True).stdout.strip()
    assert log == "builder (task abc12345) <builder@gitchen.local>"

    issues.add_comment(cfg, created["id"], "a note", "reviewer", "reviewer")
    log2 = subprocess.run(["git", "-C", str(cfg.project_root), "log", "-1", "--format=%an <%ae>"],
                           capture_output=True, text=True, check=True).stdout.strip()
    assert log2 == "reviewer <reviewer@gitchen.local>"


def test_concurrent_creates_get_distinct_ids(tmp_path):
    """Real cross-process concurrency: N processes racing create_issue must
    never collide on an id — the counter lock is what prevents that."""
    cfg = make_project(tmp_path)
    script = tmp_path / "create_one.py"
    script.write_text(
        "import sys; sys.path.insert(0, %r)\n"
        "from mcp import issues\n"
        "from mcp.config import ProjectConfig\n"
        "cfg = ProjectConfig.load(%r)\n"
        "r = issues.create_issue(cfg, 'concurrent', 'body', None, 0, 'coordinator', 'coordinator', assignee='builder')\n"
        "print(r['id'])\n" % (str(Path(__file__).resolve().parent.parent), str(cfg.project_root))
    )
    procs = [subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE, text=True)
             for _ in range(8)]
    ids = sorted(int(p.communicate(timeout=10)[0].strip()) for p in procs)
    assert ids == list(range(1, 9)), f"expected 8 distinct sequential ids, got {ids}"


def test_concurrent_comments_both_land(tmp_path):
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "title", "body", None, 0, "coordinator", "coordinator", assignee="builder")
    root = Path(__file__).resolve().parent.parent

    def script_for(text):
        return (
            "import sys; sys.path.insert(0, %r)\n"
            "from mcp import issues\n"
            "from mcp.config import ProjectConfig\n"
            "cfg = ProjectConfig.load(%r)\n"
            "issues.add_comment(cfg, %r, %r, 'tester', 'tester')\n"
            % (str(root), str(cfg.project_root), created["id"], text)
        )

    script_a = cfg.project_root.parent / "a.py"
    script_b = cfg.project_root.parent / "b.py"
    script_a.write_text(script_for("comment A"))
    script_b.write_text(script_for("comment B"))

    pa = subprocess.Popen([sys.executable, str(script_a)])
    pb = subprocess.Popen([sys.executable, str(script_b)])
    pa.wait(timeout=10)
    pb.wait(timeout=10)

    final = issues.get_issue(cfg, created["id"])
    body_text = "\n".join(line["text"] for line in final["lines"])
    assert "comment A" in body_text
    assert "comment B" in body_text


def test_create_issue_can_be_created_directly_as_done(tmp_path):
    """A decision record is final the moment it's written — no separate
    update_issue call needed just to close it."""
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "a decision", "body", ["decision"], 0,
                                   "coordinator", "coordinator", state="done", assignee="builder")

    assert created["state"] == "done"
    assert issues.list_issues(cfg, state="open") == []
    assert [i["id"] for i in issues.list_issues(cfg, state="done")] == [created["id"]]


def test_four_states_are_all_independently_queryable(tmp_path):
    cfg = make_project(tmp_path)
    o = issues.create_issue(cfg, "o", "b", None, 0, "coordinator", "coordinator", assignee="builder")
    b = issues.create_issue(cfg, "b", "b", None, 0, "coordinator", "coordinator", state="blocked", assignee="builder")
    d = issues.create_issue(cfg, "d", "b", None, 0, "coordinator", "coordinator", state="done", assignee="builder")
    f = issues.create_issue(cfg, "f", "b", None, 0, "coordinator", "coordinator", state="canceled", assignee="builder")

    assert [i["id"] for i in issues.list_issues(cfg, state="open")] == [o["id"]]
    assert [i["id"] for i in issues.list_issues(cfg, state="blocked")] == [b["id"]]
    assert [i["id"] for i in issues.list_issues(cfg, state="done")] == [d["id"]]
    assert [i["id"] for i in issues.list_issues(cfg, state="canceled")] == [f["id"]]


def _public_repo_with_issue_branch(tmp_path, cfg, issue_id, commits_ahead):
    public = cfg.public_repo
    git = lambda *a: subprocess.run(["git", "-C", str(public), *a], capture_output=True, text=True, check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(public)], check=True)
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init")
    if commits_ahead:
        git("checkout", "-q", "-b", f"issue/{issue_id}")
        for n in range(commits_ahead):
            git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", f"work {n}")
        git("checkout", "-q", "main")


def test_done_is_refused_while_the_issue_branch_has_unmerged_commits(tmp_path):
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "t", "b", None, 0, "coordinator", "coordinator", base_branch="main", assignee="builder")
    _public_repo_with_issue_branch(tmp_path, cfg, created["id"], commits_ahead=2)
    with pytest.raises(ValueError, match="mark it merged"):
        issues.update_issue(cfg, created["id"], created["hash"], {"state": "done"}, "pm", "pm")


def test_done_is_allowed_once_the_branch_is_merged_into_base(tmp_path):
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "t", "b", None, 0, "coordinator", "coordinator", base_branch="main", assignee="builder")
    _public_repo_with_issue_branch(tmp_path, cfg, created["id"], commits_ahead=2)
    subprocess.run(["git", "-C", str(cfg.public_repo), "merge", "-q", "--ff-only", f"issue/{created['id']}"], check=True)
    current = issues.get_issue(cfg, created["id"])
    done = issues.update_issue(cfg, created["id"], current["hash"], {"state": "done"}, "coordinator", "coordinator")
    assert done["state"] == "done"


def test_done_is_allowed_for_an_issue_that_produced_no_branch(tmp_path):
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "t", "b", None, 0, "coordinator", "coordinator", base_branch="main", assignee="builder")
    _public_repo_with_issue_branch(tmp_path, cfg, created["id"], commits_ahead=0)
    done = issues.update_issue(cfg, created["id"], created["hash"], {"state": "done"}, "coordinator", "coordinator")
    assert done["state"] == "done"


def _current(cfg, issue_id):
    return issues.get_issue(cfg, issue_id)


def test_marking_merged_fast_forwards_the_branch_into_base(tmp_path):
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "t", "b", None, 0, "coordinator", "coordinator", base_branch="main", assignee="builder")
    _public_repo_with_issue_branch(tmp_path, cfg, created["id"], commits_ahead=2)
    merged = issues.update_issue(cfg, created["id"], _current(cfg, created["id"])["hash"],
                                 {"state": "merged"}, "pm", "pm")
    assert merged["state"] == "merged"
    ahead = subprocess.run(["git", "-C", str(cfg.public_repo), "rev-list", "--count", f"main..issue/{created['id']}"],
                           capture_output=True, text=True).stdout.strip()
    assert ahead == "0"


def _squash_project(tmp_path, author="Jane Doe <jane@example.com>"):
    cfg = make_project(tmp_path)
    merge_conf = f'merge: {{squash: true, author: "{author}"}}\n' if author else "merge: {squash: true}\n"
    (cfg.project_root / "project.yml").write_text("public_repo: ../public\n" + merge_conf)
    return ProjectConfig.load(cfg.project_root)


def test_squash_merge_lands_one_commit_with_the_title_and_configured_author(tmp_path):
    cfg = _squash_project(tmp_path)
    created = issues.create_issue(cfg, "Select all lettering text with Cmd+A", "b", None, 0,
                                   "coordinator", "coordinator", base_branch="main", assignee="builder")
    public = cfg.public_repo
    git = lambda *a: subprocess.run(["git", "-C", str(public), *a], capture_output=True, text=True, check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(public)], check=True)
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init")
    git("checkout", "-q", "-b", f"issue/{created['id']}")
    for n in range(2):
        (public / f"f{n}.txt").write_text(str(n))
        git("add", f"f{n}.txt")
        git("-c", "user.email=private@example.org", "-c", "user.name=someone", "commit", "-q",
            "-m", f"work {n}\n\nCo-Authored-By: Helper <helper@example.net>")
    branch_tree = git("rev-parse", f"issue/{created['id']}^{{tree}}").stdout.strip()
    git("checkout", "-q", "main")

    merged = issues.update_issue(cfg, created["id"], _current(cfg, created["id"])["hash"],
                                 {"state": "merged"}, "pm", "pm")

    assert merged["state"] == "merged"
    assert git("rev-list", "--count", "main").stdout.strip() == "2", "one commit on top of init"
    assert git("log", "-1", "--format=%B", "main").stdout.strip() == "Select all lettering text with Cmd+A"
    assert git("log", "-1", "--format=%an <%ae>|%cn <%ce>", "main").stdout.strip() == \
        "Jane Doe <jane@example.com>|Jane Doe <jane@example.com>"
    assert git("rev-parse", "main^{tree}").stdout.strip() == branch_tree
    assert git("rev-list", "--count", f"main..issue/{created['id']}").stdout.strip() == "0", \
        "the branch has nothing the base lacks, so done and reopen keep working"
    assert (public / "f1.txt").read_text() == "1", "the checkout has the work"


def test_squash_merge_moves_a_checked_out_issue_worktree_without_touching_its_files(tmp_path):
    cfg = _squash_project(tmp_path)
    public = cfg.public_repo
    git = lambda repo, *a: subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True, check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(public)], check=True)
    git(public, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init")
    created = issues.create_issue(cfg, "Add a thing", "b", None, 0, "coordinator", "coordinator",
                                   base_branch="main", assignee="builder")
    from mcp import worktrees
    wt = worktrees.create(cfg, created["id"], "main")
    (wt / "thing.txt").write_text("x")
    git(wt, "add", "thing.txt")
    git(wt, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "add thing")
    (wt / "scratch.png").write_text("untracked artifact")

    issues.update_issue(cfg, created["id"], _current(cfg, created["id"])["hash"], {"state": "merged"}, "pm", "pm")

    assert git(wt, "rev-parse", "HEAD").stdout == git(public, "rev-parse", "main").stdout
    assert git(wt, "status", "--porcelain", "--untracked-files=no").stdout == ""
    assert (wt / "scratch.png").exists()


def test_merge_config_rejects_a_malformed_author(tmp_path):
    with pytest.raises(ValueError, match="Name <email>"):
        _squash_project(tmp_path, author="just-a-name")


def test_marking_merged_is_refused_without_a_branch_and_leaves_the_issue_open(tmp_path):
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "t", "b", None, 0, "coordinator", "coordinator", base_branch="main", assignee="builder")
    _public_repo_with_issue_branch(tmp_path, cfg, created["id"], commits_ahead=0)
    with pytest.raises(ValueError, match="no branch"):
        issues.update_issue(cfg, created["id"], _current(cfg, created["id"])["hash"], {"state": "merged"}, "pm", "pm")
    assert _current(cfg, created["id"])["state"] == "open"


def test_marking_merged_is_refused_when_base_has_moved_past_the_branch(tmp_path):
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "t", "b", None, 0, "coordinator", "coordinator", base_branch="main", assignee="builder")
    _public_repo_with_issue_branch(tmp_path, cfg, created["id"], commits_ahead=1)
    subprocess.run(["git", "-C", str(cfg.public_repo), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "--allow-empty", "-m", "main moves on"], check=True)
    with pytest.raises(ValueError, match="rebase it onto main first"):
        issues.update_issue(cfg, created["id"], _current(cfg, created["id"])["hash"], {"state": "merged"}, "pm", "pm")
    assert _current(cfg, created["id"])["state"] == "open"


def test_marking_merged_is_refused_on_a_dirty_public_checkout(tmp_path):
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "t", "b", None, 0, "coordinator", "coordinator", base_branch="main", assignee="builder")
    _public_repo_with_issue_branch(tmp_path, cfg, created["id"], commits_ahead=1)
    (cfg.public_repo / "stray.txt").write_text("uncommitted")
    with pytest.raises(ValueError, match="uncommitted changes"):
        issues.update_issue(cfg, created["id"], _current(cfg, created["id"])["hash"], {"state": "merged"}, "pm", "pm")


def test_marking_merged_an_issue_whose_commits_are_already_on_base_records_it(tmp_path):
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "t", "b", None, 0, "coordinator", "coordinator", base_branch="main", assignee="builder")
    _public_repo_with_issue_branch(tmp_path, cfg, created["id"], commits_ahead=1)
    subprocess.run(["git", "-C", str(cfg.public_repo), "merge", "-q", "--ff-only", f"issue/{created['id']}"], check=True)
    merged = issues.update_issue(cfg, created["id"], _current(cfg, created["id"])["hash"],
                                 {"state": "merged"}, "coordinator", "coordinator")
    assert merged["state"] == "merged"


def test_merging_a_branch_with_nothing_new_records_it_even_after_base_moved(tmp_path):
    # A branch with no commits of its own has nothing to rebase or verify again:
    # sending it back to the reviewer would only spend a turn.
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "t", "b", None, 0, "coordinator", "coordinator", base_branch="main", assignee="builder")
    _public_repo_with_issue_branch(tmp_path, cfg, created["id"], commits_ahead=1)
    subprocess.run(["git", "-C", str(cfg.public_repo), "merge", "-q", "--ff-only", f"issue/{created['id']}"], check=True)
    subprocess.run(["git", "-C", str(cfg.public_repo), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "--allow-empty", "-m", "main moves on"], check=True)
    head = subprocess.run(["git", "-C", str(cfg.public_repo), "rev-parse", "main"], capture_output=True, text=True).stdout
    merged = issues.update_issue(cfg, created["id"], _current(cfg, created["id"])["hash"],
                                 {"state": "merged"}, "reviewer", "reviewer")
    assert merged["state"] == "merged"
    assert subprocess.run(["git", "-C", str(cfg.public_repo), "rev-parse", "main"],
                          capture_output=True, text=True).stdout == head


def test_replacing_the_body_keeps_the_comments_and_front_matter(tmp_path):
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "t", "original body", None, 0, "coordinator", "coordinator", assignee="builder")
    issues.add_comment(cfg, created["id"], "a comment that must survive", "pm", "pm")
    current = issues.get_issue(cfg, created["id"])
    issues.update_issue(cfg, created["id"], current["hash"], {"body": "new description"}, "coordinator", "coordinator")
    after = issues.get_issue(cfg, created["id"])
    text = "\n".join(line["text"] for line in after["lines"])
    assert "new description" in text
    assert "original body" not in text
    assert "a comment that must survive" in text
    assert after["assignee"] == created["assignee"]
    assert after["title"] == "t"


def test_a_stacked_child_starts_once_its_parent_lands_and_is_retargeted(tmp_path):
    cfg = make_project(tmp_path)
    parent = issues.create_issue(cfg, "parent", "b", None, 0, "coordinator", "coordinator",
                                 base_branch="main", assignee="builder")
    child = issues.create_issue(cfg, "child", "b", None, 0, "coordinator", "coordinator",
                                assignee="pm", base_branch=f"issue/{parent['id']}",
                                blocked_by=[parent["id"]])
    _public_repo_with_issue_branch(tmp_path, cfg, parent["id"], commits_ahead=1)

    issues.update_issue(cfg, parent["id"], issues.get_issue(cfg, parent["id"])["hash"],
                        {"state": "merged"}, "pm", "pm")
    assert issues.get_issue(cfg, child["id"])["state"] == "blocked", "it opens on the next tick, not in the merge"
    issues.wake_dependents(cfg)

    reopened = issues.get_issue(cfg, child["id"])
    assert reopened["state"] == "open"
    assert reopened["base_branch"] == "main", \
        "the child is retargeted onto the parent's own base, not the merged branch"
    assert reopened["assignment_history"][-1]["kind"] == "woken"
    assert issues.comment_count(reopened) == 0, "the daemon does not comment"


def test_a_body_edit_that_includes_the_comments_does_not_duplicate_them(tmp_path):
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "t", "body", None, 0, "coordinator", "coordinator", assignee="builder")
    issues.add_comment(cfg, created["id"], "first note", "pm", "pm")
    current = issues.get_issue(cfg, created["id"])
    whole_body = "\n".join(line["text"] for line in current["lines"])
    # What a real pm turn passes back: the description it edited, plus the
    # comments it was shown. The new line goes in the description, not after
    # the comments marker.
    edited = whole_body.replace("body\n", "body\nnew line\n", 1)

    issues.update_issue(cfg, created["id"], current["hash"], {"body": edited}, "pm", "pm")

    text = "\n".join(line["text"] for line in issues.get_issue(cfg, created["id"])["lines"])
    assert text.count("## Comments") == 1, "the comments section must not be duplicated"
    assert text.count("first note") == 1
    assert "new line" in text


def test_turn_records_are_written_in_the_same_commit_as_the_comment(tmp_path):
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "title", "body", None, 0, "pm", "pm", assignee="builder")
    turns = [{"at": "2026-11-09T10:00:00Z", "role": "builder", "model": "m", "usd": 0.4,
              "input": 1, "output": 2, "cache_read": 0, "cache_write": 0, "seconds": 7}]
    issues.add_comment(cfg, created["id"], "report", "builder", "builder", turns=turns)
    issue = issues.get_issue(cfg, created["id"])
    assert issue["turns"] == turns and issue["cost_usd"] == 0.4
    show = subprocess.run(["git", "-C", str(cfg.project_root), "show", "--stat", "--format=%s", "HEAD"],
                          capture_output=True, text=True).stdout
    assert "comment" in show.splitlines()[0] and f"issues/{created['id']}.md" in show



def _queue(cfg, *titles_priorities):
    return [issues.create_issue(cfg, title, "b", None, prio, "coordinator", "coordinator",
                                assignee="builder")["id"] for title, prio in titles_priorities]


def _order(cfg):
    return [i["id"] for i in issues.list_issues(cfg, state="open")]


def test_move_places_an_issue_between_two_others_without_touching_them(tmp_path):
    cfg = make_project(tmp_path)
    a, b, c = _queue(cfg, ("a", 10), ("b", 20), ("c", 30))
    before = {i: issues.get_issue(cfg, i)["priority"] for i in (a, b, c)}
    issues.move_issue(cfg, c, after=a)
    assert _order(cfg) == [a, c, b]
    assert issues.get_issue(cfg, a)["priority"] == before[a], "neighbours keep their priority"
    assert issues.get_issue(cfg, b)["priority"] == before[b]
    assert 10 < issues.get_issue(cfg, c)["priority"] < 20


def test_move_to_the_top_and_to_the_bottom(tmp_path):
    cfg = make_project(tmp_path)
    a, b, c = _queue(cfg, ("a", 10), ("b", 20), ("c", 30))
    issues.move_issue(cfg, c, before=a)
    assert _order(cfg) == [c, a, b]
    issues.move_issue(cfg, c, after=b)
    assert _order(cfg) == [a, b, c]


def test_a_tie_is_spread_out_only_as_far_as_needed(tmp_path):
    cfg = make_project(tmp_path)
    a, b, c = _queue(cfg, ("a", 50), ("b", 50), ("c", 50))
    issues.move_issue(cfg, c, before=b)
    assert _order(cfg) == [a, c, b]
    prios = [issues.get_issue(cfg, i)["priority"] for i in (a, c, b)]
    assert prios == sorted(prios) and len(set(prios)) == 3


def test_move_is_refused_for_bad_anchors(tmp_path):
    cfg = make_project(tmp_path)
    a, b = _queue(cfg, ("a", 10), ("b", 20))
    done = issues.create_issue(cfg, "closed", "b", None, 5, "coordinator", "coordinator",
                               assignee="builder", state="done")["id"]
    with pytest.raises(ValueError, match="exactly one"):
        issues.move_issue(cfg, b, before=a, after=a)
    with pytest.raises(ValueError, match="not an open issue"):
        issues.move_issue(cfg, b, after=999)
    with pytest.raises(ValueError, match="not an open issue"):
        issues.move_issue(cfg, b, after=done)
    with pytest.raises(ValueError, match="itself"):
        issues.move_issue(cfg, b, after=b)
    with pytest.raises(ValueError, match="open"):
        issues.move_issue(cfg, done, after=a)


def test_a_move_is_one_commit_however_many_issues_it_renumbers(tmp_path):
    cfg = make_project(tmp_path)
    a, b, c = _queue(cfg, ("a", 50), ("b", 50), ("c", 50))
    head = subprocess.run(["git", "-C", str(cfg.project_root), "rev-list", "--count", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    issues.move_issue(cfg, c, before=b)
    after = subprocess.run(["git", "-C", str(cfg.project_root), "rev-list", "--count", "HEAD"],
                           capture_output=True, text=True).stdout.strip()
    assert int(after) == int(head) + 1
