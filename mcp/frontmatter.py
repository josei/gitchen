"""Tiny shared parser for the `---\\nYAML\\n---\\nbody` shape used by issues,
schedules, and agent/skill role files alike — one format, one place that
knows it."""

from __future__ import annotations

import re

import yaml

_RE = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.DOTALL)


def parse(raw: str) -> tuple[dict, str]:
    """Returns (frontmatter, body). No frontmatter present -> ({}, raw)."""
    m = _RE.match(raw)
    if not m:
        return {}, raw
    front = yaml.safe_load(m.group(1)) or {}
    return front, m.group(2)


def serialize(front: dict, body: str) -> str:
    return "---\n" + yaml.safe_dump(front, sort_keys=False).strip() + "\n---\n" + body
