"""Resolver for an instance repo's registry.yml.

Flat lookup, no inheritance: every entry in `sources:` maps one local path
to exactly one `<repo-alias>//<path-in-that-repo>[@<ref>]` descriptor. This
module only materializes files; it has no opinion about what's inside them.

A repo alias may be a local sibling path (the common case — `../gitchen`,
`../other-project-agents`) or a remote git URL. Either way, pinning is just git
history: `git show <ref>:<path>`, against a local repo's own `.git` or a
cached mirror clone for a remote one. There's no separate pin/unpin step —
`sync()` does both: an unpinned entry is resolved against the source's
current commit *and* immediately rewritten with `@<that commit>`, so every
sync leaves every source pinned. To go back to floating (track future
changes), edit the `@<ref>` out of registry.yml by hand and sync again —
that's the whole "unpin" operation, no command needed for it.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path

import yaml

_URL_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://|^git@|\.git$")


def load(project_root: Path) -> dict:
    path = project_root / "registry.yml"
    if not path.exists():
        return {"repos": {}, "sources": {}}
    data = yaml.safe_load(path.read_text()) or {}
    data.setdefault("repos", {})
    data.setdefault("sources", {})
    return data


def save(project_root: Path, data: dict) -> None:
    (project_root / "registry.yml").write_text(yaml.safe_dump(data, sort_keys=False))


def _parse_descriptor(descriptor: str) -> tuple[str, str, str | None]:
    alias, _, rest = descriptor.partition("//")
    if not rest:
        raise ValueError(f"malformed registry descriptor (missing //): {descriptor}")
    path, _, ref = rest.partition("@")
    return alias, path, (ref or None)


def _is_url(location: str) -> bool:
    return bool(_URL_RE.search(location))


def _cache_root() -> Path:
    override = os.environ.get("GITCHEN_CACHE_DIR")
    return Path(override) if override else Path.home() / ".cache" / "gitchen" / "repos"


def _cached_clone(url: str) -> Path:
    """A mirror clone, not just --bare: a plain --bare clone has no fetch
    refspec configured, so `git fetch` wouldn't update any local ref and
    floating sources would silently stay stuck at clone time. --mirror
    wires `+refs/*:refs/*` so `git fetch origin` actually refreshes it.
    Only ever read via `git show`, never checked out."""
    slug = hashlib.sha1(url.encode()).hexdigest()[:16]
    cache_dir = _cache_root() / slug
    if not cache_dir.exists():
        cache_dir.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--mirror", "-q", url, str(cache_dir)], check=True)
    return cache_dir


def _repo_location(project_root: Path, repos: dict, alias: str) -> tuple[Path, bool]:
    """Returns (repo_path, is_remote)."""
    if alias not in repos:
        raise KeyError(f"registry.yml has no repo alias '{alias}'")
    location = repos[alias]
    if _is_url(location):
        return _cached_clone(location), True
    return (project_root / location).resolve(), False


def _fetch(repo_root: Path) -> None:
    subprocess.run(["git", "-C", str(repo_root), "fetch", "-q", "origin"], check=True)


def _show(repo_root: Path, ref: str, path: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo_root), "show", f"{ref}:{path}"],
        capture_output=True, text=True, check=True,
    ).stdout


def _current_commit(repo_root: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def resolve_one(project_root: Path, descriptor: str) -> str:
    data = load(project_root)
    alias, path, ref = _parse_descriptor(descriptor)
    repo_root, is_remote = _repo_location(project_root, data["repos"], alias)

    if is_remote:
        if ref:
            try:
                return _show(repo_root, ref, path)
            except subprocess.CalledProcessError:
                _fetch(repo_root)  # the pinned commit may not be cached yet
                return _show(repo_root, ref, path)
        _fetch(repo_root)  # floating: always track the remote's current tip
        return _show(repo_root, "HEAD", path)

    # Local repos resolve through git history too, same as remote — not a
    # raw working-tree read. That keeps the auto-pin below always correct:
    # the commit sync() records is guaranteed to reproduce exactly the
    # content it just materialized, even if the source repo happens to have
    # unrelated uncommitted changes sitting in its working tree.
    return _show(repo_root, ref or "HEAD", path)


def sync(project_root: Path, only: str | None = None) -> list[str]:
    data = load(project_root)
    synced = []
    registry_changed = False
    for local_path, descriptor in data["sources"].items():
        if only and local_path != only:
            continue
        content = resolve_one(project_root, descriptor)
        dest = project_root / local_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content)
        synced.append(local_path)

        alias, path, ref = _parse_descriptor(descriptor)
        if not ref:
            repo_root, _is_remote = _repo_location(project_root, data["repos"], alias)
            commit = _current_commit(repo_root)
            data["sources"][local_path] = f"{alias}//{path}@{commit}"
            registry_changed = True
    if registry_changed:
        save(project_root, data)
    return synced
