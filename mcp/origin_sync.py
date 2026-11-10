"""The daily mechanical sync with origin: fetch, fast-forward or push.

No agent ever touches origin — the network decision stands (a sandboxed
role can only reach the model API and the hosts its profile allows, and
the remote is SSH, which nono's proxy cannot carry). This runs in the
daemon process, outside every sandbox, on a cron schedule, and only ever
does what a person would do by hand:

- fetch the remote;
- behind only  -> fast-forward the local branch;
- ahead only   -> push it;
- diverged     -> do nothing and say so in the log. Never force, never
                  auto-merge: two lineages that diverged are a decision for
                  a human, not something to paper over at 04:00.

Configured per project in project.yml:

    origin_sync:
      cron: "17 4 * * *"   # daily
      remote: origin
      branch: main

The repo synced is the project's `public_repo` (the code the agents build),
not the instance repo. Last-run state lives in
`.agents-daemon/origin_sync.json`, so a matching minute fires once.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

from . import gitutil, schedules
from .config import ProjectConfig


def _state_path(project_root: Path) -> Path:
    return Path(project_root) / ".agents-daemon" / "origin_sync.json"


def _load_state(project_root: Path) -> dict:
    try:
        return json.loads(_state_path(project_root).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_state(project_root: Path, state: dict) -> None:
    path = _state_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, path)


def _report(message: str) -> None:
    print(f"[origin_sync] {message}", file=sys.stderr, flush=True)


def _sync(repo: Path, remote: str, branch: str) -> str:
    fetch = gitutil.run(repo, "fetch", remote, "--prune")
    if fetch.returncode != 0:
        return f"fetch failed: {fetch.stderr.strip() or fetch.stdout.strip()}"
    counts = gitutil.run(repo, "rev-list", "--left-right", "--count", f"{remote}/{branch}...{branch}")
    if counts.returncode != 0:
        return f"cannot compare with {remote}/{branch}: {counts.stderr.strip()}"
    behind, ahead = (int(n) for n in counts.stdout.split())
    if ahead and behind:
        return (f"diverged: {ahead} local commit(s) not on {remote}/{branch}, {behind} there not local. "
                "Not pushing and not merging — resolve by hand, then the daily sync resumes.")
    if behind:
        merge = gitutil.run(repo, "merge", "--ff-only", f"{remote}/{branch}")
        if merge.returncode != 0:
            return f"fast-forward failed: {merge.stderr.strip() or merge.stdout.strip()}"
        return f"fast-forwarded {behind} commit(s) from {remote}/{branch}"
    if ahead:
        push = gitutil.run(repo, "push", remote, branch)
        if push.returncode != 0:
            return f"push failed: {push.stderr.strip() or push.stdout.strip()}"
        return f"pushed {ahead} commit(s) to {remote}/{branch}"
    return f"already up to date with {remote}/{branch}"


def tick(cfg: ProjectConfig) -> None:
    conf = cfg.origin_sync
    if not conf:
        return
    now = datetime.now()
    if not schedules.cron_matches(conf["cron"], now):
        return
    minute = now.strftime("%Y-%m-%dT%H:%M")
    state = _load_state(cfg.project_root)
    if state.get("last_run") == minute:
        return  # this matching minute already fired
    result = _sync(cfg.public_repo, conf["remote"], conf["branch"])
    _save_state(cfg.project_root, {"last_run": minute, "result": result})
    _report(f"{cfg.public_repo}: {result}")
