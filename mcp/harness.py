"""Harness abstraction: the only place that knows how to drive a coding-agent CLI.

A Harness starts an agent turn, resumes it with a follow-up prompt, and can
interrupt it. Everything else in this package (issue dispatch, the
worktree-per-issue lifecycle, the MCP tool calls) is harness-agnostic and
talks only to this interface.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class TurnResult:
    session_id: str
    text: str
    raw: dict
    ok: bool
    usage: list[dict] = field(default_factory=list)
    usage_cumulative: bool = False
    # The model never got to work: a usage limit, an outage, a CLI that wouldn't
    # start. Different from a turn that ran and went wrong, which may have
    # changed files and spent money.
    never_ran: bool = False


class Harness(Protocol):
    def start(self, prompt: str, cwd: str, settings: dict) -> TurnResult:
        """Run a fresh turn in `cwd`. Blocks until the turn finishes."""
        ...

    def resume(self, session_id: str, prompt: str, cwd: str, settings: dict) -> TurnResult:
        """Continue a prior turn's session with a follow-up prompt. Blocks."""
        ...

    def interrupt(self, session_id: str) -> None:
        """Best-effort stop of an in-flight turn. No-op if already finished."""
        ...


_REGISTRY: dict[str, type] = {}


def register(name: str):
    def deco(cls):
        _REGISTRY[name] = cls
        return cls

    return deco


def registered() -> dict[str, type]:
    """Every adapter that has registered itself. Used by machine_setup to
    detect what's installed, so it imports the package the same lazy way."""
    if not _REGISTRY:
        from . import harnesses  # noqa: F401
    return dict(_REGISTRY)


def get_harness(name: str) -> Harness:
    if name not in _REGISTRY:
        # Built-in adapters only register themselves (via @register) on
        # import, and nothing imports mcp.harnesses eagerly — try it here,
        # once, before giving up. Idempotent: a second import is a no-op.
        from . import harnesses  # noqa: F401
    if name not in _REGISTRY:
        raise KeyError(f"unknown harness '{name}'; registered: {sorted(_REGISTRY)}")
    return _REGISTRY[name]()
