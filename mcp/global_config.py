"""Machine-level gitchen config: which CLI drives every turn, the sandbox it
runs in, and what a project's model aliases mean on this machine.

A project.yml says `model: small` and nothing else about execution. Which
harness, which provider, which model id, how much thinking, which sandbox
profile — all of that is a property of the machine (and of what the machine
has credentials for), not of the repository, so it lives here instead.

Path: `$GITCHEN_CONFIG` if set, else `$XDG_CONFIG_HOME/gitchen/config.yml`
(normally `~/.config/gitchen/config.yml`). Shape:

    harness: omp
    harnesses:
      omp:
        settings: {binary: omp}
        sandbox: {profile: omp-sandbox, allow: ["~/.npm"],
                  allow_domains: [api.deepinfra.com], open_ports: [0]}
      claude-code:
        settings: {binary: claude, permission_mode: bypassPermissions}
        sandbox: {profile: claude-sandbox, allow_domains: [registry.npmjs.org]}
    models:
      small:  {omp: {model: deepinfra/deepseek-ai/DeepSeek-V4.1-Flash-1017, thinking: low},
               claude-code: {model: claude-haiku-5-5}}
      medium: {omp: {..., thinking: auto}, ...}
      big:    {omp: {..., thinking: high}, ...}

`settings` are merged under a role's own settings, then the alias entry for
the selected harness on top of that; `sandbox` rides along as
`settings["sandbox"]` for whichever adapter knows how to use it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml


def default_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "gitchen" / "config.yml"


@dataclass(frozen=True)
class GlobalConfig:
    harness: str
    harnesses: dict  # harness name -> {"settings": {...}, "sandbox": {...}}
    models: dict  # alias -> harness name -> settings overrides
    path: Path

    @classmethod
    def load(cls, path: str | Path | None = None) -> "GlobalConfig":
        chosen = Path(path or os.environ.get("GITCHEN_CONFIG") or default_path())
        if not chosen.exists():
            # First run on this machine: write one from what's installed
            # rather than making a human hand-assemble the harness, aliases
            # and sandbox block.
            from .machine_setup import write_default
            write_default(chosen)
        data = yaml.safe_load(chosen.read_text()) or {}
        harness = data.get("harness")
        if not harness:
            raise ValueError(f"{chosen}: `harness` is required")
        return cls(harness=harness, harnesses=data.get("harnesses") or {},
                   models=data.get("models") or {}, path=chosen)

    def settings(self, harness: str) -> dict:
        """The adapter settings every role on this machine starts from."""
        return dict((self.harnesses.get(harness) or {}).get("settings") or {})

    def sandbox(self, harness: str) -> dict | None:
        """This harness's sandbox block, or None when the machine explicitly
        opted out with `sandbox: false`. An *absent* key is an error rather
        than a silent no-sandbox: the adapters auto-approve tool calls, which
        is only safe inside a sandbox, so running without one has to be a
        decision someone wrote down."""
        entry = self.harnesses.get(harness) or {}
        if "sandbox" not in entry:
            raise ValueError(
                f"{self.path}: harnesses.{harness}.sandbox is required — name a profile "
                "(gitchen ships one; it is installed on first use), or write "
                "`sandbox: false` to run turns unsandboxed on purpose")
        sandbox = entry["sandbox"]
        if sandbox is False or sandbox is None:
            return None
        if not isinstance(sandbox, dict) or not sandbox.get("profile"):
            raise ValueError(f"{self.path}: harnesses.{harness}.sandbox needs a `profile` "
                             "(or `false` to run unsandboxed)")
        return dict(sandbox)

    def model_settings(self, alias: str, harness: str) -> dict:
        """What `model: <alias>` resolves to on this machine, for this
        harness. Unknown alias or missing harness entry is a config error,
        never a silent fallback to some other model."""
        per_harness = self.models.get(alias)
        if per_harness is None:
            raise ValueError(f"unknown model alias {alias!r}; {self.path} defines {sorted(self.models)}")
        resolved = per_harness.get(harness)
        if resolved is None:
            raise ValueError(f"model alias {alias!r} has no entry for harness {harness!r} in {self.path}")
        return dict(resolved)
