"""The read-only web view: JSON endpoints, SSE, and the static page."""

import http.client
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mcp import holders, issues, web  # noqa: E402
from mcp.config import ProjectConfig  # noqa: E402


def make_project(tmp_path: Path) -> ProjectConfig:
    root = tmp_path / "instance"
    (root / "agents").mkdir(parents=True)
    for _role in ("builder", "reviewer", "pm", "researcher"):
        _f = root / "agents" / f"{_role}.md"
        if not _f.exists():
            _f.write_text(f"---\nname: {_role}\n---\n{_role}")
    (root / "agents" / "builder.md").write_text("---\nname: builder\n---\nbuild")
    (root / "agents" / "reviewer.md").write_text("---\nname: reviewer\n---\nreview")
    (tmp_path / "public").mkdir()
    (root / "project.yml").write_text(
        "public_repo: ../public\n"
        "role_settings:\n  builder: {model: small}\n  reviewer: {model: small}\n"
        "allowed_tools:\n  builder: [get_issue]\n  reviewer: [get_issue]\n")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return ProjectConfig.load(root)


def serve(cfg: ProjectConfig):
    server = web.make_server(cfg.project_root, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def get(url: str):
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.status, response.read()


def get_json(url: str):
    status, body = get(url)
    return status, json.loads(body)


def test_roles_come_from_project_yml_not_from_a_fixed_list(tmp_path):
    cfg = make_project(tmp_path)
    (cfg.project_root / "agents" / "reviewer.md").unlink()  # declared role, no agent file
    server, base = serve(cfg)
    try:
        _, overview = get_json(base + "/api/overview")
        assert overview["roles"] == ["builder", "reviewer"]
    finally:
        server.shutdown()


def test_overview_lists_every_turn_cooking_at_once(tmp_path):
    # With parallel turns the view must show all of them, not only the first.
    cfg = make_project(tmp_path)
    a = issues.create_issue(cfg, "first dish", "b", None, 1, "coordinator", "coordinator", assignee="builder")
    b = issues.create_issue(cfg, "second dish", "b", None, 2, "coordinator", "coordinator", assignee="reviewer")
    assert holders.claim(cfg.project_root, a["id"], "task-aaaaaaaa", os.getpid(), "builder")
    assert holders.claim(cfg.project_root, b["id"], "task-bbbbbbbb", os.getpid(), "reviewer")
    server, base = serve(cfg)
    try:
        _, overview = get_json(base + "/api/overview")
        assert [(h["issue"], h["role"], h["title"]) for h in overview["holders"]] == [
            (a["id"], "builder", "first dish"), (b["id"], "reviewer", "second dish")]
    finally:
        server.shutdown()


def test_overview_reports_the_queue_and_the_live_turn(tmp_path):
    cfg = make_project(tmp_path)
    first = issues.create_issue(cfg, "build the thing", "b", ["roadmap"], 3, "coordinator", "coordinator",
                                assignee="builder")
    issues.create_issue(cfg, "blocked thing", "b", None, 4, "coordinator", "coordinator",
                        assignee="reviewer", state="blocked")
    assert holders.claim(cfg.project_root, first["id"], "task-12345678", os.getpid(), "builder")
    server, base = serve(cfg)
    try:
        _, overview = get_json(base + "/api/overview")
        assert overview["project"] == "public", "the view names the repo being built"
        assert overview["active_issue"]["id"] == first["id"], "the live turn is the active issue"
        assert "pacing" not in overview
        assert overview["holders"] == [{"issue": first["id"], "role": "builder", "task": "task-123",
                                        "title": "build the thing"}]
        assert [i["id"] for i in overview["queue"]] == [first["id"]]
        assert [i["id"] for i in overview["blocked"]] == [2]
        assert overview["counts"]["open"] == 1 and overview["counts"]["blocked"] == 1
        assert overview["daemon"]["running"] is False
    finally:
        server.shutdown()


def test_the_view_exposes_scopes_and_filters_issues_by_them(tmp_path):
    cfg = make_project(tmp_path)
    (cfg.project_root / "project.yml").write_text(
        "public_repo: ../public\nfolders: [product, company]\ndefault_folder: product\n")
    cfg = ProjectConfig.load(cfg.project_root)
    product = issues.create_issue(cfg, "product thing", "b", None, 0, "pm", "pm",
                                  assignee="builder", folder="product")
    company = issues.create_issue(cfg, "company thing", "b", None, 0, "pm", "pm",
                                  assignee="builder", folder="company")
    server, base = serve(cfg)
    try:
        _, overview = get_json(base + "/api/overview")
        assert overview["folders"] == [{"name": "product", "count": 1},
                                       {"name": "company", "count": 1}]
        assert overview["default_folder"] == "product"
        _, detail = get_json(f"{base}/api/issues/{company['id']}")
        assert detail["folder"] == "company"
        _, filtered = get_json(base + "/api/issues?folder=product")
        assert [i["id"] for i in filtered["issues"]] == [product["id"]]
        _, both = get_json(base + "/api/issues?folder=product&state=open")
        assert [i["id"] for i in both["issues"]] == [product["id"]]
    finally:
        server.shutdown()


def test_overview_reports_schedules_and_their_last_fired_minute(tmp_path):
    cfg = make_project(tmp_path)
    (cfg.project_root / "schedules").mkdir()
    (cfg.project_root / "schedules" / "nightly.md").write_text(
        "---\ncron: '7 * * * *'\nassignee: builder\ntitle: nightly audit\npriority: 5\n---\nCheck the thing.\n")
    (cfg.project_root / ".agents-daemon").mkdir(exist_ok=True)
    (cfg.project_root / ".agents-daemon" / "schedule_state.json").write_text(
        '{"nightly": "2026-11-07 06:07"}')
    server, base = serve(cfg)
    try:
        _, overview = get_json(base + "/api/overview")
        assert overview["schedules"] == [{
            "name": "nightly", "cron": "7 * * * *", "assignee": "builder",
            "title": "nightly audit", "body": "Check the thing.", "labels": [],
            "priority": 5, "last_fired": "2026-11-07 06:07"}]
    finally:
        server.shutdown()


def test_issue_detail_carries_the_body_and_comments(tmp_path):
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "t", "the description", None, 0, "coordinator", "coordinator",
                                  assignee="builder")
    issues.add_comment(cfg, created["id"], "a **note** from pm", "pm", "pm")
    server, base = serve(cfg)
    try:
        status, issue = get_json(f"{base}/api/issues/{created['id']}")
        assert status == 200
        assert issue["body"] == "the description"
        assert issue["comments"][0]["who"] == "pm"
        assert issue["comments"][0]["text"] == "a **note** from pm"
        try:
            get_json(f"{base}/api/issues/999")
            raise AssertionError("a missing issue should be a 404")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
    finally:
        server.shutdown()


def test_a_role_shows_its_own_prompt_without_shared_or_workspace(tmp_path):
    cfg = make_project(tmp_path)
    (cfg.project_root / "context.md").write_text("Label conventions: use roadmap for buildable work.")
    created = issues.create_issue(cfg, "build the thing", "do it", ["roadmap"], 3,
                                  "coordinator", "coordinator", assignee="builder")
    assert holders.claim(cfg.project_root, created["id"], "task-12345678", os.getpid(), "builder")
    server, base = serve(cfg)
    try:
        _, context = get_json(base + "/api/roles/builder")
        assert context["role"] == "builder"
        assert context["role_prompt"] == "build", "the system prompt has no frontmatter"
        assert context["issue"] == {"id": created["id"], "title": "build the thing", "state": "open"}
        assert context["live"] == [{"id": created["id"], "title": "build the thing"}]
        assert context["model"] == "small" and context["tools"] == ["get_issue"]
        titles = [section["title"] for section in context["sections"]]
        assert {"title": None, "body": "build"} in context["sections"]
        assert not {"Shared context", "Your workspace", "Posting your report",
                    "How the turn ends"} & set(titles)
        assert "Label conventions" not in context["prompt"], \
            "the panel does not repeat the shared context every role loads"
        assert "## Your workspace" not in context["prompt"]
        assert "## Posting your report" not in context["prompt"]
        assert "## How the turn ends" not in context["prompt"]
        assert context["prompt_omits_static"] is True
        assert f"## Issue #{created['id']}: build the thing" in context["prompt"]
        assert "## Assignment history" in context["prompt"]
        assert context["prompt"].startswith("build"), "the role prompt is still first"
        assert "Allowed directives:" in context["prompt"], "the actual values stay at the end"
    finally:
        server.shutdown()


def test_an_idle_role_shows_its_standing_context_without_an_issue(tmp_path):
    cfg = make_project(tmp_path)
    server, base = serve(cfg)
    try:
        _, context = get_json(base + "/api/roles/reviewer")
        assert context["issue"] is None and context["prompt"] is None
        assert context["role_prompt"] == "review"
        titles = [section["title"] for section in context["sections"]]
        assert not {"Shared context", "Your workspace", "Posting your report",
                    "How the turn ends"} & set(titles)
        assert not any(title and title.startswith("Issue #") for title in titles)
        try:
            get_json(base + "/api/roles/nobody")
            raise AssertionError("a role with no agents/<role>.md should be a 404")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
    finally:
        server.shutdown()


def test_the_view_is_read_only(tmp_path):
    cfg = make_project(tmp_path)
    server, base = serve(cfg)
    try:
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            request = urllib.request.Request(base + "/api/issues", method=method, data=b"{}")
            try:
                urllib.request.urlopen(request, timeout=5)
                raise AssertionError(f"{method} should be refused")
            except urllib.error.HTTPError as exc:
                assert exc.code == 405
    finally:
        server.shutdown()


def test_static_assets_are_served_and_traversal_is_refused(tmp_path):
    cfg = make_project(tmp_path)
    server, base = serve(cfg)
    try:
        status, body = get(base + "/")
        assert status == 200 and b"<title>gitchen</title>" in body
        assert get(base + "/static/app.js")[0] == 200
        # the bundled rounded font the page's stylesheet asks for
        with urllib.request.urlopen(base + "/static/fonts/fredoka.woff2") as response:
            assert response.headers["Content-Type"] == "font/woff2"
            assert response.read(4) == b"wOF2"
        try:
            get(base + "/static/../mcp/web.py")
            raise AssertionError("traversal should be refused")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
    finally:
        server.shutdown()


def test_sse_pushes_a_change_when_an_issue_changes(tmp_path):
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "t", "b", None, 0, "coordinator", "coordinator", assignee="builder")
    server, base = serve(cfg)
    port = server.server_address[1]
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/events")
        response = conn.getresponse()
        assert response.getheader("Content-Type").startswith("text/event-stream")
        # The stream opens with a padding comment (to defeat proxy buffering);
        # the hello event follows it.
        line = response.readline()
        while line.startswith(b":") or line in (b"\n", b"\r\n"):
            line = response.readline()
        assert b"event: hello" in line
        issues.add_comment(cfg, created["id"], "a new note", "pm", "pm")
        deadline = time.time() + 10
        seen = b""
        while time.time() < deadline and b"event: change" not in seen:
            line = response.readline()
            if not line:
                break
            seen += line
        assert b"event: change" in seen
        conn.close()
    finally:
        server.shutdown()


def test_comment_parsing_understands_both_formats():
    body = ("description\n\n## Comments\n\n"
            "- 2026-11-05T10:00:00Z (builder): old style\nsecond line\n\n"
            "### 2026-11-05T11:00:00Z — reviewer\nnew style\n")
    comments = web.parse_comments(body)
    assert [c["who"] for c in comments] == ["builder", "reviewer"]
    assert comments[0]["text"] == "old style\nsecond line"
    assert comments[1]["text"] == "new style"


def test_shared_evidence_is_served_but_only_from_that_folder(tmp_path):
    cfg = make_project(tmp_path)
    shared = cfg.project_root / ".agents-daemon" / "shared" / "154" / "abc12345"
    shared.mkdir(parents=True)
    (shared / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    (shared / "report.txt").write_text("a plain-text report")
    (shared / "page.html").write_text("<script>alert(1)</script>")
    (shared / "tool.exe").write_bytes(b"MZ")
    server, base = serve(cfg)
    try:
        status, body = get(base + "/shared/154/abc12345/shot.png")
        assert status == 200 and body.startswith(b"\x89PNG")
        with urllib.request.urlopen(base + "/shared/154/abc12345/report.txt") as response:
            assert response.headers["Content-Type"].startswith("text/plain")
            assert response.headers["X-Content-Type-Options"] == "nosniff"
            assert response.read() == b"a plain-text report"
        for bad in ("/shared/154/abc12345/page.html", "/shared/154/abc12345/tool.exe",
                    "/shared/../project.yml",
                    "/shared/154/abc12345/missing.png"):
            try:
                get(base + bad)
                raise AssertionError(f"{bad} should be refused")
            except urllib.error.HTTPError as exc:
                assert exc.code == 404
    finally:
        server.shutdown()


def test_a_heading_inside_a_comment_is_not_a_new_comment():
    body = ("description\n\n## Comments\n\n"
            "### 2026-11-05T11:00:00Z — reviewer\n"
            "## Verdict: passes\n\n### Step — do the thing\nmore prose\n")
    comments = web.parse_comments(body)
    assert len(comments) == 1
    assert "### Step — do the thing" in comments[0]["text"]


def test_the_overview_shows_the_budget_and_any_pause(tmp_path):
    from mcp import budget, pause
    cfg = make_project(tmp_path)
    (cfg.project_root / "project.yml").write_text(
        "public_repo: ../public\nbudget: {usd: 3, per: 5h}\n")
    cfg = ProjectConfig.load(cfg.project_root)
    pause.record_failure(cfg.project_root, "usage limit reached")
    server, base = serve(cfg)
    try:
        _, overview = get_json(base + "/api/overview")
        assert overview["budget"]["limit"] == 3 and overview["budget"]["per"] == "5h"
        assert overview["budget"]["over"] is False
        assert overview["paused"]["reason"] == "usage limit reached"
        assert "pacing" not in overview
    finally:
        server.shutdown()


def test_a_comment_gets_the_cost_of_the_turn_that_wrote_it():
    comments = [{"when": "2026-11-09T21:14:58Z", "who": "builder (task x)"}]
    turns = [{"at": "2026-11-09T21:14:57Z", "role": "builder", "model": "m", "usd": 0.1},
             {"at": "2026-11-09T21:14:57Z", "role": "builder", "model": "n", "usd": 0.2}]
    web._attach_turns(comments, turns)
    assert [turn["model"] for turn in comments[0]["turns"]] == ["m", "n"]


def test_a_comment_without_a_turn_has_no_cost():
    comments = [{"when": "2026-11-09T21:14:58Z", "who": "pm (task x)"}]
    turns = [{"at": "2026-11-09T21:14:57Z", "role": "builder", "model": "m", "usd": 0.1}]
    web._attach_turns(comments, turns)
    assert comments[0]["turns"] == []


def test_an_issue_shows_what_it_cost_and_what_the_kitchen_noted(tmp_path):
    cfg = make_project(tmp_path)
    created = issues.create_issue(cfg, "build it", "b", None, 0, "pm", "pm", assignee="builder")
    with patch.object(issues, "_now", return_value="2026-11-09T10:00:00Z"):
        issues.add_comment(cfg, created["id"], "the report", "builder", "builder", turns=[{
            "at": "2026-11-09T10:00:00Z", "role": "builder", "model": "m", "usd": 0.42, "input": 9,
            "output": 3, "cache_read": 0, "cache_write": 0, "seconds": 12}])
    issues.add_event(cfg, created["id"], "blocked", "circuit breaker: 30 turns")
    server, base = serve(cfg)
    try:
        _, detail = get_json(base + f"/api/issues/{created['id']}")
        assert detail["cost_usd"] == 0.42
        assert detail["turns"][0]["model"] == "m"
        assert detail["comments"][0]["turns"][0]["model"] == "m", \
            "the cost note belongs to the comment it produced"
        assert detail["events"] == [{"at": detail["events"][0]["at"], "kind": "blocked",
                                     "reason": "circuit breaker: 30 turns"}]
        assert "the report" in [c["text"] for c in detail["comments"]][0]
    finally:
        server.shutdown()
