"""The OS sandbox every sandboxed turn runs in, independent of which CLI
drives the agent.

`settings["sandbox"]` is harness-agnostic config — a profile name plus the
grants a role needs on top of it:

    sandbox:
      profile: gitchen
      allow: ["~/.npm"]
      allow_domains: [api.deepinfra.com, registry.npmjs.org]

The only implementation today is nono (https://nono.sh), which wraps the
whole subprocess (`nono run --profile <profile> -- <binary>`) in an
OS-level filesystem/network sandbox. That is what makes
`permission_mode: bypassPermissions` / `--auto-approve` safe: there is
nothing to prompt for, and nothing to escape to, because the blast radius
is contained outside the process instead of gated inside it. An adapter
that has its own sandbox story can ignore this key; the config is not
nono-specific, only this translation is.

A profile whose name matches `profiles/<name>.json` in this checkout is
*gitchen's own*: it is installed into nono's profile directory on first
use, with the port policy for this platform filled in (see `_port_policy`
— "every localhost port" is `open_port: [0]` on macOS but a port range on
Linux, and no single CLI flag expresses both). Any other name is a profile
the machine already has; gitchen only names it.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# mcp/harnesses/sandbox.py -> mcp/harnesses -> mcp -> gitchen's own root.
# Resolved from this file's own location, not PATH, so it's right regardless
# of where `gitchen` on PATH happens to symlink from.
_GITCHEN_ROOT = Path(__file__).resolve().parents[2]
_INSTALLED: set[str] = set()


def prefix(settings: dict, cwd: str, api_hosts: tuple[str, ...] = ()) -> list[str]:
    """nono's argv prefix for this turn, or [] when the role runs
    unsandboxed. `api_hosts` are the model-API hosts this adapter cannot
    run without, added on top of the configured allowlist."""
    sandbox = settings.get("sandbox")
    if not sandbox:
        return []
    ensure_profile(sandbox["profile"])
    args = ["nono", "run", "--profile", sandbox["profile"]]
    # The turn's own workspace is the only project path it can write. The
    # instance repo around it (issues, roles, policy, other worktrees) is not
    # granted at all; the one file a turn's MCP proxy needs is read-only.
    args += ["--allow", cwd]
    project_root = (settings.get("mcp") or {}).get("project_root")
    if project_root:
        args += ["--read-file", str(Path(project_root) / "project.yml")]
        # The turn's MCP proxy connects to the daemon's control socket. connect
        # only: nothing here can listen on it.
        args += ["--allow-unix-socket-dir", str(Path(project_root) / ".agents-daemon")]
    for extra in sandbox.get("allow") or []:
        args += ["--allow", os.path.expanduser(extra)]
    for path in sandbox.get("read") or []:
        # nono takes a directory with --read and a single file with --read-file.
        flag = "--read-file" if Path(os.path.expanduser(path)).is_file() else "--read"
        args += [flag, os.path.expanduser(path)]
    # Outbound network is an allowlist, not open: `--allow-domain` switches
    # nono into proxy mode, where only listed hosts are reachable and
    # direct connections (including SSH on port 22, which the proxy can't
    # see) are refused. The adapter's own model API hosts are always
    # included, since the agent can't run without them. Anything else a
    # role needs (a package registry, say) is listed in the global config;
    # pushing to origin is never allowed by default, so a builder can't
    # publish work on its own.
    for host in [*api_hosts, *(sandbox.get("allow_domains") or [])]:
        args += ["--allow-domain", host]
    # The `gitchen` command invoked for the MCP config is itself a script
    # inside this very checkout, outside both the worktree and the
    # project root — found on a real dispatch run (nono's own denial
    # message named the exact fix): `gitchen serve` couldn't even read
    # its own source, so the MCP server died before it could start, and
    # the agent only ever saw a dead connection, no reason why. Grants
    # read-only, since a sandboxed agent has no business writing to the
    # framework's own code regardless of what else it's allowed.
    args += ["--read", str(_GITCHEN_ROOT)]
    # The MCP server the agent spawns is itself `gitchen serve`, which loads
    # the machine config (harness, aliases, sandbox) before it can answer a
    # single tool call. That file lives under ~/.config, outside every grant
    # above — found on a real dispatch run: omp reported "MCP subprocess
    # closed stdout before responding", and the server's own log showed a
    # PermissionError on this exact path. One file, read-only.
    config = _machine_config()
    if config:
        args += ["--read-file", str(config)]
    return args + ["--workdir", cwd, "--"]


def ensure_profile(name: str) -> None:
    """Installs a gitchen-shipped profile if the machine doesn't have it (or
    has an older version of it), so naming one in the machine config is the
    whole setup step. A name gitchen doesn't ship is left alone: that's a
    profile the machine already manages."""
    if name in _INSTALLED:
        return
    template_path = _GITCHEN_ROOT / "profiles" / f"{name}.json"
    if not template_path.exists():
        _INSTALLED.add(name)
        return
    profile = json.loads(template_path.read_text())
    ports = _port_policy()
    if ports:
        profile["network"] = {**(profile.get("network") or {}), **ports}
    target = _profiles_dir() / f"{name}.json"
    installed = json.loads(target.read_text()) if target.exists() else None
    if installed is not None and _version(installed) >= _version(profile):
        _INSTALLED.add(name)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(profile, indent=2) + "\n")
    print(f"installed nono profile '{name}' at {target}", file=sys.stderr, flush=True)
    _INSTALLED.add(name)


def _version(profile: dict) -> tuple[int, ...]:
    raw = str((profile.get("meta") or {}).get("version") or "0")
    parts = []
    for piece in raw.split("."):
        parts.append(int(piece) if piece.isdigit() else 0)
    return tuple(parts)


def _profiles_dir() -> Path:
    """Where nono looks for user profiles. `GITCHEN_PROFILE_DIR` exists so
    tests never touch the real machine config."""
    explicit = os.environ.get("GITCHEN_PROFILE_DIR")
    if explicit:
        return Path(explicit).expanduser()
    nono_config = os.environ.get("NONO_CONFIG")
    base = Path(nono_config).expanduser() if nono_config else Path.home() / ".config" / "nono"
    return base / "profiles"


def _port_policy() -> dict:
    """nono has no portable "every localhost port": on macOS it's
    `open_port: [0]` (`localhost:*`), while Linux treats 0 as a literal port
    and needs a range (Landlock handles it natively; the 16,384-rule limit is
    a macOS seatbelt constraint). This is why ports live in the profile
    rather than a CLI flag — the platform difference has to be resolved
    somewhere, and a generated profile is the only place nono accepts it."""
    if sys.platform == "darwin":
        return {"open_port": [0]}
    if sys.platform.startswith("linux"):
        return {"open_port_range": [[1, 65535]]}
    return {}


def _machine_config() -> Path | None:
    """The global config every `gitchen` invocation loads, resolved the same
    way `GlobalConfig.load` resolves it — the agent's MCP server has to be
    able to read exactly the file its parent process was configured with."""
    from ..global_config import default_path
    path = Path(os.environ.get("GITCHEN_CONFIG") or default_path())
    return path if path.exists() else None


__all__ = ["prefix", "ensure_profile"]
