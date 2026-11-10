"""Loads an instance repo's project.yml. One ProjectConfig per MCP server process.

project.yml is only what the *project* decides: which repo it works on, how
many turns may run per role at once, which MCP tools each role may call,
and each role's model alias. How a turn actually runs — which CLI, which
model id, how much thinking, which sandbox profile — is machine config and
lives in mcp/global_config.py, so the same project.yml works on a laptop
running omp and on a box running Claude Code.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from . import frontmatter
from .global_config import GlobalConfig


def _origin_sync(conf) -> dict | None:
    """Validated `origin_sync:` block, or None when the project doesn't sync."""
    if not conf:
        return None
    missing = [k for k in ("cron", "remote", "branch") if not conf.get(k)]
    if missing:
        raise ValueError(f"origin_sync needs {missing}")
    return {"cron": str(conf["cron"]), "remote": str(conf["remote"]), "branch": str(conf["branch"])}


def _merge(conf) -> dict | None:
    """Validated `merge:` block: `squash` (bool) and an optional `author`
    (`Name <email>`). None keeps plain fast-forward merges."""
    if not conf:
        return None
    if not isinstance(conf, dict):
        raise ValueError("merge is a mapping: {squash: true, author: \"Name <email>\"}")
    extra = sorted(set(conf) - {"squash", "author"})
    if extra:
        raise ValueError(f"merge sets {extra}; it takes only `squash` and `author`")
    out = {"squash": bool(conf.get("squash", False))}
    if conf.get("author"):
        from .merge import parse_author
        parse_author(str(conf["author"]))
        out["author"] = str(conf["author"])
    return out


_FOLDER_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _folder_name(name) -> str:
    if not isinstance(name, str) or not _FOLDER_RE.match(name):
        raise ValueError(f"folder names are lowercase letters, digits and hyphens, not {name!r}")
    return name


def _folders(value) -> list:
    """The folders an issue may be filed in, as the instance declares them. A name
    not listed here is refused, so a typo can't start a folder nobody meant."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("folders must be a list of folder names")
    return [_folder_name(name) for name in value]


def _edit_scopes(value, declared) -> dict:
    """role -> the folders that role may edit. A role with no entry may edit
    everything. Every folder named must be declared."""
    if not value:
        return {}
    if not isinstance(value, dict):
        raise ValueError("edit_scopes must map each role to its folders")
    known = set(declared or [])
    out = {}
    for role, names in value.items():
        out[str(role)] = [_folder_name(n) for n in (names or [])]
        unknown = [n for n in out[str(role)] if n not in known]
        if unknown:
            raise ValueError(f"edit_scopes.{role} names folders not declared in folders: {unknown}")
    return out


def _role_needs(role: str, overrides: dict) -> None:
    network = overrides.get("network")
    if network is not None and (not isinstance(network, list)
                                or not all(isinstance(h, str) and h for h in network)):
        raise ValueError(f"role_settings.{role}.network must be a list of host names")
    workspace = overrides.get("workspace")
    if workspace is not None and workspace not in ("worktree", "none"):
        raise ValueError(f"role_settings.{role}.workspace must be `worktree` or `none`, not {workspace!r}")


def _max_parallel(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"max_parallel must be a whole number of at least 1, not {value!r}")
    return value


VERBS = ("return", "done", "merge", "cancel", "block")  # plus `assign <role>`


def declared_outcomes(agent_file: Path) -> dict[str, str]:
    """A role's outcomes from its prompt's frontmatter: name -> what it means."""
    front, _body = frontmatter.parse(agent_file.read_text())
    return {str(k): str(v) for k, v in (front.get("outcomes") or {}).items()}


def _check_programs(root: Path) -> None:
    """A role whose prompt names a script (`run:`) needs that script in the
    instance: a missing one, or a shell script that isn't executable, would
    fail every turn. Found here, at load. A `.py` file runs under Python, so
    it needs no executable bit."""
    for agent in sorted((root / "agents").glob("*.md")):
        front, _body = frontmatter.parse(agent.read_text())
        run = front.get("run")
        if not run:
            continue
        script = (root / str(run)).resolve()
        if not script.is_file():
            raise ValueError(f"role {agent.stem}: no script at {run} (run: names a file in the instance)")
        if script.suffix != ".py" and not os.access(script, os.X_OK):
            raise ValueError(f"role {agent.stem}: {run} is not executable (chmod +x it, or name a .py file)")


def role_skill_names(agent_file: Path) -> list[str]:
    """The skills a role preloads: the `skills:` list in its prompt's
    frontmatter, the same field Claude Code subagents use."""
    front, _body = frontmatter.parse(agent_file.read_text())
    names = front.get("skills") or []
    if isinstance(names, str):
        names = [names]
    if not isinstance(names, list):
        raise ValueError(f"role {agent_file.stem}: `skills` must be a list of skill names")
    return [str(name) for name in names]


def load_skill(root: Path, name: str) -> dict:
    """`skills/<name>/SKILL.md` in the instance, in the standard Agent Skills
    format. A skill marked `disable-model-invocation: true` is for a person to
    invoke, never for a role to preload (Claude Code refuses the same)."""
    if not name or Path(name).name != name:
        raise ValueError(f"skill {name!r}: a skill is named by its folder under skills/")
    path = root / "skills" / name / "SKILL.md"
    if not path.is_file():
        raise ValueError(f"skill {name}: there is no skills/{name}/SKILL.md")
    front, body = frontmatter.parse(path.read_text())
    if front.get("disable-model-invocation") is True:
        raise ValueError(f"skill {name}: it is marked disable-model-invocation, so it is "
                         "invoked by a person and can't be preloaded by a role")
    return {"name": name, "description": str(front.get("description") or "").strip(),
            "dir": str(path.parent), "body": body.strip()}


def _check_skills(root: Path) -> None:
    """Every skill a role lists must exist and be preloadable. Found here, at
    load: a role silently missing a skill would do its work without it."""
    for agent in sorted((root / "agents").glob("*.md")):
        for name in role_skill_names(agent):
            try:
                load_skill(root, name)
            except ValueError as error:
                raise ValueError(f"role {agent.stem}: {error}") from None


def _transitions(conf, root: Path) -> dict:
    """The instance's `transitions:` table, checked against the roles' own
    declared outcomes. A mistake here is refused at load, not discovered when
    an outcome arrives with nowhere to go."""
    if not conf:
        return {}
    if not isinstance(conf, dict):
        raise ValueError("transitions must map each role to its outcomes")
    out: dict[str, dict[str, str]] = {}
    for role, table in conf.items():
        agent = root / "agents" / f"{role}.md"
        if not agent.is_file():
            raise ValueError(f"transitions.{role}: there is no agents/{role}.md")
        declared = declared_outcomes(agent)
        if not declared:
            raise ValueError(f"transitions.{role}: agents/{role}.md declares no outcomes")
        out[role] = {}
        for outcome, verb in (table or {}).items():
            if outcome not in declared:
                raise ValueError(f"transitions.{role}.{outcome}: agents/{role}.md does not declare "
                                 f"that outcome (it declares {', '.join(declared)})")
            verb = str(verb).strip()
            if verb.startswith("assign "):
                target = verb.split(None, 1)[1].strip()
                if not (root / "agents" / f"{target}.md").is_file():
                    raise ValueError(f"transitions.{role}.{outcome}: assign {target!r} names no role")
            elif verb not in VERBS:
                raise ValueError(f"transitions.{role}.{outcome}: {verb!r} is not a verb (use assign <role>, "
                                 "return, done, merge, cancel or block)")
            out[role][str(outcome)] = verb
    return out


_PER_RE = re.compile(r"^(\d+)([mhd])$")
_PER_SECONDS = {"m": 60, "h": 3600, "d": 86400}


def _budget(conf) -> dict | None:
    """Validated `budget:` block: `{usd: <limit>, per: <window>}`, where `per` is
    optional (`5h`, `30m`, `2d`). Without `per`, spending stays stopped until
    someone resets it. A limit is always in dollars at list price."""
    if conf is None:
        return None
    if not isinstance(conf, dict) or "usd" not in conf:
        raise ValueError("budget needs `usd: <limit>` and optionally `per: <window>`")
    usd = conf["usd"]
    if isinstance(usd, bool) or not isinstance(usd, (int, float)) or usd <= 0:
        raise ValueError(f"budget.usd must be a positive number, not {usd!r}")
    per = conf.get("per")
    seconds = None
    if per is not None:
        match = _PER_RE.match(str(per))
        if not match or int(match.group(1)) == 0:
            raise ValueError(f"budget.per must look like 30m, 5h or 2d, not {per!r}")
        seconds = int(match.group(1)) * _PER_SECONDS[match.group(2)]
    return {"usd": float(usd), "per": None if per is None else str(per), "per_seconds": seconds}


@dataclass
class ProjectConfig:
    project_root: Path
    public_repo: Path
    allowed_tools: dict  # role -> MCP tool names that role may call; see server.py's --role
    directive_retries: int  # max corrective resume() attempts for a missing/invalid NEXT: line
    role_settings: dict  # role -> {"model": <alias>}; resolved by global_config
    global_config: GlobalConfig
    origin_sync: dict | None  # {"cron", "remote", "branch"}: the daily mechanical push, or None
    budget: dict | None = None  # {"usd", "per", "per_seconds"}: see _budget; None means no limit
    transitions: dict | None = None  # role -> {outcome: verb}; see _transitions
    max_parallel: int = 1  # turns allowed at once; each holds its own issue
    folders: list = None  # declared folders under issues/; see _folders
    default_folder: str | None = None  # where an issue goes when none is named
    edit_scopes: dict = None  # role -> folders it may edit; no entry means every folder
    merge: dict | None = None  # {"squash", "author"?}: see _merge; None means plain fast-forward

    @classmethod
    def load(cls, project_root: str | Path, global_config: GlobalConfig | None = None,
             check_outcomes: bool = True) -> "ProjectConfig":
        """`check_outcomes` reads every wired role's prompt to check the
        transitions table. The daemon and the CLI do that. A turn's MCP server
        runs sandboxed without read access to the role prompts, and never
        looks at the table, so it loads with the check off."""
        root = Path(project_root).resolve()
        data = yaml.safe_load((root / "project.yml").read_text()) or {}
        if check_outcomes:
            _check_programs(root)
            _check_skills(root)
        for key in ("harness", "role_harness"):
            if key in data:
                raise ValueError(
                    f"{root / 'project.yml'}: `{key}` is machine config, not a project decision — "
                    "set it in the global gitchen config instead")
        if "issue_interval_seconds" in data:
            raise ValueError(f"{root / 'project.yml'}: `issue_interval_seconds` is gone: work is "
                             "paced by the budget now (project.yml `budget:`)")
        role_settings = data.get("role_settings") or {}
        for role, overrides in role_settings.items():
            extra = sorted(set(overrides or {}) - {"model", "network", "workspace"})
            if extra:
                raise ValueError(
                    f"{root / 'project.yml'}: role_settings.{role} sets {extra}, which is machine "
                    "config — a project may name only `model`, `network` and `workspace`")
            _role_needs(role, overrides or {})
        return cls(
            project_root=root,
            public_repo=(root / data["public_repo"]).resolve(),
            allowed_tools=data.get("allowed_tools", {}),
            directive_retries=int(data.get("directive_retries", 2)),
            role_settings=role_settings,
            global_config=global_config or GlobalConfig.load(),
            origin_sync=_origin_sync(data.get("origin_sync")),
            merge=_merge(data.get("merge")),
            budget=_budget(data.get("budget")),
            transitions=_transitions(data.get("transitions"), root) if check_outcomes else {},
            max_parallel=_max_parallel(data.get("max_parallel", 1)),
            folders=_folders(data.get("folders")),
            default_folder=_folder_name(data.get("default_folder")) if data.get("default_folder") else None,
            edit_scopes=_edit_scopes(data.get("edit_scopes"), data.get("folders")),
        )

    def agent_prompt_path(self, role: str) -> Path:
        return self.project_root / "agents" / f"{role}.md"

    def skills_for(self, role: str) -> list[dict]:
        """The skills `role` preloads, in the order its frontmatter lists them."""
        agent = self.agent_prompt_path(role)
        return [load_skill(self.project_root, name) for name in role_skill_names(agent)]

    def roles(self) -> list[str]:
        """The project's roles: every `agents/<role>.md`. This is the whole
        definition of "a gitchen agent" — the only thing an issue may be
        assigned to. The interactive coordinator session is not one."""
        return sorted(p.stem for p in (self.project_root / "agents").glob("*.md"))

    def is_role(self, name: str | None) -> bool:
        return bool(name) and Path(name).name == name and self.agent_prompt_path(name).is_file()

    def harness(self) -> str:
        """Which adapter runs every turn on this machine."""
        return self.global_config.harness

    def program_of(self, role: str) -> Path | None:
        """The script a program role runs instead of a model, from its prompt's
        `run:` frontmatter (relative to the instance repo), or None."""
        agent = self.agent_prompt_path(role)
        if not agent.is_file():
            return None
        front, _body = frontmatter.parse(agent.read_text())
        run = front.get("run")
        return (self.project_root / run).resolve() if run else None

    def workspace_of(self, role: str) -> str:
        """`worktree` (the default: the issue's own checkout) or `none`: the role
        has no repository, and runs in an empty scratch folder."""
        return (self.role_settings.get(role) or {}).get("workspace") or "worktree"

    def settings_for_role(self, role: str) -> dict:
        """The machine's settings for the selected harness, plus this role's
        model alias resolved for that harness. Whether a given key actually
        does anything is the adapter's call (`mcp/harnesses/<name>.py`) —
        this just decides what settings dict a role gets."""
        harness = self.harness()
        settings = self.global_config.settings(harness)
        sandbox = self.global_config.sandbox(harness)
        if sandbox:
            settings["sandbox"] = sandbox
        alias = (self.role_settings.get(role) or {}).get("model")
        if alias:
            settings.update(self.global_config.model_settings(alias, harness))
        network = (self.role_settings.get(role) or {}).get("network")
        if network is not None and settings.get("sandbox"):
            # A role names the hosts it reaches; they replace the machine's list for
            # it. The adapter's own model host is still added by the adapter.
            settings["sandbox"] = {**settings["sandbox"], "allow_domains": list(network)}
        return settings

    def shared_context(self) -> str | None:
        """context.md: prose, not config — unconditionally included in every
        spawned prompt by dispatch.py. For facts every agent needs
        regardless of task (e.g. label conventions), not read by any code,
        only by agents. Optional; None if the project has none yet."""
        path = self.project_root / "context.md"
        return path.read_text() if path.exists() else None

    def issues_dir(self) -> Path:
        p = self.project_root / "issues"
        p.mkdir(exist_ok=True)
        return p
