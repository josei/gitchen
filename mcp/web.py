"""The read-only web view: an overview of the kitchen, one issue at a time.

Deliberately not part of the daemon. `gitchen web` is its own process with
its own lifecycle: it reads the same files the daemon writes (issues,
holders, the daemon pidfile) and serves them over HTTP + SSE. Start
it, stop it, restart it while the daemon runs or while it doesn't — the
worst case is a page that says the daemon is offline.

Read-only forever: every handler is a GET, there is no mutation endpoint and
no token. When `--tunnel` is used, the unguessable tunnel URL is the access
control; if that ever stops being enough, Cloudflare Access is the layer to
add, not a write API.

The role names are the project's own (`agents/*.md`), never assumed: another
project's roles show up with their own names and colours, because the
frontend builds one little worker per role it is told about.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import Counter
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Empty, Queue
from urllib.parse import parse_qs, unquote, urlparse

from . import artifacts, budget, daemon, dispatch, frontmatter, holders, issues, pause, schedules
from .config import ProjectConfig

_GITCHEN_ROOT = Path(__file__).resolve().parents[1]
_WEB_ROOT = _GITCHEN_ROOT / "web"
_COMMENT_HEAD = re.compile(r"^### (\d{4}-\d{2}-\d{2}T\S+) — (.+)$")
_COMMENT_BULLET = re.compile(r"^- (\d{4}-\d{2}-\d{2}T\S+) \((.+)\): ?(.*)$")
_TUNNEL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
_TUNNEL_URL: str | None = None


# --- reading the kitchen ---------------------------------------------------

def parse_comments(body: str) -> list[dict]:
    """`## Comments` into [{when, who, text}], newest last. Understands the
    heading format and the older bullet one, so old issues read fine."""
    marker = body.find("\n## Comments")
    if marker == -1:
        return []
    section = body[marker:]
    parts = section.split("\n", 2)
    section = parts[2] if len(parts) > 2 else ""
    blocks: list[dict] = []
    current: dict | None = None
    for line in section.splitlines():
        heading = _COMMENT_HEAD.match(line)
        bullet = _COMMENT_BULLET.match(line)
        if heading:
            if current:
                blocks.append(current)
            current = {"when": heading.group(1), "who": heading.group(2), "lines": []}
        elif bullet:
            if current:
                blocks.append(current)
            current = {"when": bullet.group(1), "who": bullet.group(2), "lines": [bullet.group(3)]}
        elif current is not None:
            current["lines"].append(line)
    if current:
        blocks.append(current)
    for block in blocks:
        block["text"] = "\n".join(block.pop("lines")).strip()
    return blocks


def _at_seconds(value) -> float | None:
    try:
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ").timestamp()
    except (TypeError, ValueError):
        return None


def _attach_turns(comments: list[dict], turns: list[dict]) -> None:
    """Give each comment the cost records of the turn that wrote it. A turn's
    `at` is written just before its comment, so the comment can be a second
    later; match by role to the nearest turn at or before it. A comment with
    no turn (a human's, or an older one) gets an empty list."""
    groups: list[list] = []
    for turn in turns or []:
        at, role = turn.get("at"), turn.get("role")
        if groups and groups[-1][0] == at and groups[-1][1] == role:
            groups[-1][2].append(turn)
        else:
            groups.append([at, role, [turn]])
    used: set[int] = set()
    for comment in comments:
        role = comment.get("who", "").split(" (")[0].strip()
        when = _at_seconds(comment.get("when"))
        best = None
        for index, (at, turn_role, group) in enumerate(groups):
            if index in used or turn_role != role:
                continue
            at_seconds = _at_seconds(at)
            if when is None or at_seconds is None:
                continue
            delta = when - at_seconds
            if 0 <= delta <= 300 and (best is None or at_seconds > best[0]):
                best = (at_seconds, index, group)
        comment["turns"] = best[2] if best else []
        if best:
            used.add(best[1])


def _issue_view(cfg: ProjectConfig, issue_id: int) -> dict:
    issue = issues.get_issue(cfg, issue_id)
    body = "\n".join(line["text"] for line in issue["lines"])
    marker = body.find("\n## Comments")
    description = body[:marker].strip() if marker != -1 else body.strip()
    events = [{"at": e["at"], "kind": e["kind"], "reason": e["reason"]}
              for e in issue["assignment_history"] if e.get("type") == "event"]
    comments = parse_comments(body)
    _attach_turns(comments, issue["turns"])
    return {
        "id": issue["id"], "title": issue["title"], "state": issue["state"],
        "folder": issue.get("folder"),
        "labels": issue["labels"], "priority": issue["priority"],
        "assignee": issue["assignee"], "base_branch": issue["base_branch"],
        "body": description, "comments": comments,
        "turns": issue["turns"], "cost_usd": issue["cost_usd"], "events": events,
        "blocked_by": issue["blocked_by"],
    }


def _roles(cfg: ProjectConfig, summaries: list[dict]) -> list[str]:
    """The project's own roles, from `project.yml`: the keys of
    `role_settings` and `allowed_tools` are the roles this project declares.
    Falls back to `agents/*.md`, then to the assignees actually in use, so a
    project that declares none still shows something. Never a fixed list."""
    declared = sorted(set(cfg.role_settings) | set(cfg.allowed_tools))
    if declared:
        return declared
    agents = cfg.project_root / "agents"
    roles = sorted(p.stem for p in agents.glob("*.md")) if agents.exists() else []
    return roles or sorted({i["assignee"] for i in summaries if i["assignee"]})


def _holders(cfg: ProjectConfig) -> list[dict]:
    out = []
    directory = cfg.project_root / ".agents-daemon" / "holders"
    if not directory.exists():
        return out
    for path in sorted(directory.glob("*.json")):
        if not path.stem.isdigit():
            continue
        record = holders.current(cfg.project_root, int(path.stem))
        if record:
            out.append({"issue": int(path.stem), "role": record.get("role"),
                        "task": str(record.get("task_id", ""))[:8]})
    return out


def _role_context(cfg: ProjectConfig, role: str) -> dict:
    """What a role loads when it runs, assembled from the same function the
    dispatcher uses, minus the parts every role shares: shared context, the
    workspace rules, the posting rules and the directive rule (workspace
    differs only for a role with no repository). The panel shows only what is
    specific to this role and issue. The role prompt is the system prompt;
    `sections` is every other part. With a live turn, `prompt` is the text
    that turn received, minus those static parts. An idle role has no issue,
    so the issue-specific sections are absent and `prompt` is None: the rest
    is its standing context."""
    if not cfg.is_role(role):
        raise FileNotFoundError(f"no such role: {role}")
    front, role_prompt = frontmatter.parse(cfg.agent_prompt_path(role).read_text())
    live = [h for h in _holders(cfg) if h.get("role") == role]
    issue = issues.get_issue(cfg, live[0]["issue"]) if live else None
    record = holders.current(cfg.project_root, issue["id"]) if issue else None
    if record and record.get("role") != role:
        record = None
    task = str((record or {}).get("task_id", ""))[:8]
    if issue and task:
        shared = {"dir": str(cfg.project_root / ".agents-daemon" / "shared" / str(issue["id"]) / task),
                  "url": f"/shared/{issue['id']}/{task}"}
    else:
        shared = {"dir": str(cfg.project_root / ".agents-daemon" / "shared" / "<issue>" / "<task>"),
                  "url": "/shared/<issue>/<task>"}
    parts = dispatch.prompt_parts(cfg, role, issue, shared)
    # These parts are the same for every role and say nothing about this one:
    # shared context, workspace rules, posting rules and the directive rule.
    # The turn still receives them first, as the common cache prefix (see
    # dispatch.prompt_parts); only the role-specific directive values remain.
    static_sections = {"Shared context", "Your workspace", "Posting your report", "How the turn ends"}
    shown = [(title, body) for title, body in parts if title not in static_sections]
    summaries = {i["id"]: i["title"] for i in issues.list_issues(cfg)}
    return {
        "role": role,
        "description": str(front.get("description") or ""),
        "outcomes": front.get("outcomes") or {},
        "program": front.get("run"),
        "model": (cfg.role_settings.get(role) or {}).get("model"),
        "harness": cfg.harness(),
        "workspace": cfg.workspace_of(role),
        "tools": cfg.allowed_tools.get(role, []),
        "live": [{"id": h["issue"], "title": summaries.get(h["issue"], "")} for h in live],
        "issue": {"id": issue["id"], "title": issue["title"], "state": issue["state"]} if issue else None,
        "role_prompt": role_prompt.strip(),
        "sections": [{"title": title, "body": body} for title, body in shown],
        "prompt": dispatch.render_prompt(shown) if issue else None,
        "prompt_omits_static": len(shown) != len(parts),  # shared context + workspace rules
    }


def overview(cfg: ProjectConfig) -> dict:
    summaries = issues.list_issues(cfg)
    now = time.time()
    live = _holders(cfg)
    counts = Counter(i["state"] for i in summaries)
    folder_counts = Counter(i["folder"] for i in summaries if i.get("folder"))
    declared = list(cfg.folders or [])
    folders = [{"name": name, "count": folder_counts.get(name, 0)} for name in declared]
    folders += [{"name": name, "count": folder_counts[name]}
                for name in sorted(folder_counts) if name not in declared]
    fired = schedules.read_state(cfg.project_root)
    titles = {i["id"]: i["title"] for i in summaries}
    live = [{**h, "title": titles.get(h["issue"], "")} for h in live]  # every turn on the stove
    active_issue = next((i for i in summaries if live and i["id"] == live[0]["issue"]), None)
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "project": cfg.public_repo.name,
        "daemon": daemon.daemon_status(cfg.project_root),
        "active_issue": active_issue,
        "holders": live,
        "budget": budget.status(cfg),
        "paused": pause.active(cfg.project_root),
        "roles": _roles(cfg, summaries),
        "counts": {name: counts.get(name, 0) for name in issues.STATES},
        "queue": [i for i in summaries if i["state"] == "open"],
        "blocked": [i for i in summaries if i["state"] == "blocked"],
        "labels": sorted({label for i in summaries for label in i["labels"]}),
        "folders": folders,
        "default_folder": cfg.default_folder,
        "schedules": [
            {"name": s.name, "cron": s.cron, "assignee": s.assignee, "title": s.title,
             "body": s.body, "labels": s.labels, "priority": s.priority,
             "last_fired": fired.get(s.name)}
            for s in schedules.load_schedules(cfg.project_root)
        ],
    }


# --- SSE --------------------------------------------------------------------

class _Hub:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: set[Queue] = set()
        self.version = 0

    def subscribe(self) -> Queue:
        queue: Queue = Queue()
        with self._lock:
            self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: Queue) -> None:
        with self._lock:
            self._subscribers.discard(queue)

    def publish(self, event: dict) -> None:
        with self._lock:
            self.version += 1
            event = {**event, "v": self.version}
            for queue in self._subscribers:
                queue.put(event)


def _fingerprint(cfg: ProjectConfig) -> str:
    parts = []
    for path in sorted((cfg.project_root / "issues").glob("*/*.md")) + sorted(
            (cfg.project_root / "issues").glob("*.md")):
        parts.append(f"{path.parent.name}/{path.name}:{path.stat().st_mtime_ns}")
    for name in ("daemon.pid", "budget.json", "pause.json"):
        path = cfg.project_root / ".agents-daemon" / name
        parts.append(f"{name}:{path.stat().st_mtime_ns if path.exists() else 0}")
    holders_dir = cfg.project_root / ".agents-daemon" / "holders"
    if holders_dir.exists():
        for path in sorted(holders_dir.glob("*.json")):
            parts.append(f"h:{path.name}:{path.stat().st_mtime_ns}")
    return hashlib.sha1("\n".join(parts).encode()).hexdigest()


def _watch(cfg: ProjectConfig, hub: _Hub) -> None:
    last = None
    while True:
        try:
            current = _fingerprint(cfg)
        except OSError:
            current = last
        if current != last:
            last = current
            hub.publish({"type": "change"})
        time.sleep(1.0)


# --- HTTP -------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    server_version = "gitchen-web"

    def log_message(self, fmt: str, *args) -> None:  # keep the terminal clean
        return

    def handle_one_request(self) -> None:
        # A browser or tunnel probe that drops a keep-alive socket is normal;
        # stdlib would print a traceback for it.
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError):
            self.close_connection = True

    def _json(self, payload: dict, status: int = 200) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _static(self, name: str) -> None:
        path = (_WEB_ROOT / name).resolve()
        if _WEB_ROOT not in path.parents or not path.is_file():
            self.send_error(404)
            return
        body = path.read_bytes()
        kind = {"html": "text/html; charset=utf-8", "css": "text/css; charset=utf-8",
                "js": "text/javascript; charset=utf-8", "woff2": "font/woff2",
                "txt": "text/plain; charset=utf-8"}.get(path.suffix.lstrip("."), "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    _SHARED_TYPES = artifacts.TYPES

    def _shared(self, cfg: ProjectConfig, rel: str) -> None:
        """Serves the agents' artifacts folder (screenshots, logs, reports).
        Read-only, a fixed list of file types, and never outside that folder."""
        root = (cfg.project_root / ".agents-daemon" / "shared").resolve()
        path = (root / rel).resolve()
        if (path != root and root not in path.parents) or not path.is_file() \
                or path.suffix.lower() not in self._SHARED_TYPES:
            self.send_error(404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", self._SHARED_TYPES[path.suffix.lower()])
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse(self) -> None:
        hub: _Hub = self.server.hub  # type: ignore[attr-defined]
        queue = hub.subscribe()
        self.close_connection = True  # a stream is never reused for a next request
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            # Some proxies (notably a cloudflared quick tunnel) buffer a
            # streamed response until enough bytes have arrived. A padding
            # comment forces the first flush, after which events flow.
            self.wfile.write(b": " + b" " * 4096 + b"\n\n")
            self.wfile.write(b"event: hello\ndata: {}\n\n")
            self.wfile.flush()
            while True:
                try:
                    event = queue.get(timeout=15)
                except Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                self.wfile.write(f"event: change\ndata: {json.dumps(event)}\n\n".encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            hub.unsubscribe(queue)

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        cfg: ProjectConfig = self.server.cfg  # type: ignore[attr-defined]
        parsed = urlparse(self.path)
        route = parsed.path
        try:
            if route == "/":
                self._static("index.html")
            elif route.startswith("/static/"):
                self._static(route[len("/static/"):])
            elif route == "/api/overview":
                self._json(overview(cfg))
            elif route == "/api/issues":
                query = parse_qs(parsed.query)
                state = query.get("state", [None])[0]
                label = query.get("label", [None])[0]
                assignee = query.get("assignee", [None])[0]
                folder = query.get("folder", [None])[0]
                self._json({"issues": issues.list_issues(cfg, state=state, label=label,
                                                         assignee=assignee, folder=folder)})
            elif route.startswith("/api/roles/"):
                self._json(_role_context(cfg, unquote(route[len("/api/roles/"):])))
            elif route.startswith("/api/issues/"):
                self._json(_issue_view(cfg, int(route.rsplit("/", 1)[1])))
            elif route == "/api/health":
                self._json({"ok": True})
            elif route.startswith("/shared/"):
                self._shared(cfg, route[len("/shared/"):])
            elif route == "/events":
                self._sse()
            else:
                self._json({"error": "not found"}, status=404)
        except FileNotFoundError as exc:
            self._json({"error": str(exc) or "not found"}, status=404)
        except (ValueError, OSError) as exc:
            self._json({"error": str(exc)}, status=400)

    def do_POST(self) -> None:  # noqa: N802
        self._json({"error": "read-only"}, status=405)

    do_PUT = do_DELETE = do_PATCH = do_POST  # type: ignore[assignment]


def make_server(project_root: str | Path, host: str = "127.0.0.1",
                port: int = 8787) -> ThreadingHTTPServer:
    cfg = ProjectConfig.load(project_root)
    server = ThreadingHTTPServer((host, port), _Handler)
    server.daemon_threads = True
    server.cfg = cfg  # type: ignore[attr-defined]
    server.hub = _Hub()  # type: ignore[attr-defined]
    threading.Thread(target=_watch, args=(cfg, server.hub), daemon=True).start()
    return server


def _start_tunnel(host: str, port: int, project_root: Path) -> subprocess.Popen | None:
    """Spawn a quick tunnel and surface its URL: printed to the terminal and
    written to `.agents-daemon/web-tunnel.url`, so a script can read it."""
    global _TUNNEL_URL
    exe = shutil.which("cloudflared")
    if not exe:
        print("cloudflared not on PATH; run without --tunnel or install it", file=sys.stderr, flush=True)
        return None
    proc = subprocess.Popen([exe, "tunnel", "--url", f"http://{host}:{port}"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)

    def read_url() -> None:
        global _TUNNEL_URL
        assert proc.stderr is not None
        for line in proc.stderr:
            match = _TUNNEL_RE.search(line)
            if match and not _TUNNEL_URL:
                _TUNNEL_URL = match.group(0)
                print(f"public: {_TUNNEL_URL}", flush=True)
                try:
                    target = Path(project_root) / ".agents-daemon" / "web-tunnel.url"
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(_TUNNEL_URL + "\n")
                except OSError:
                    pass

    threading.Thread(target=read_url, daemon=True).start()

    def cleanup() -> None:
        proc.terminate()
        target = Path(project_root) / ".agents-daemon" / "web-tunnel.url"
        try:
            if target.read_text().strip() == (_TUNNEL_URL or ""):
                target.unlink()
        except OSError:
            pass

    atexit.register(cleanup)
    return proc


def serve(project_root: str | Path, host: str = "127.0.0.1", port: int = 8787,
          tunnel: bool = False) -> None:
    server = make_server(project_root, host, port)
    actual_port = server.server_address[1]
    print(f"gitchen web: http://{host}:{actual_port}", flush=True)
    if tunnel:
        _start_tunnel(host, actual_port, Path(project_root))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
