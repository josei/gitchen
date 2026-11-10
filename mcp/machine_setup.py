"""First-run machine config: detect the agent CLIs that are installed and
write a working `~/.config/gitchen/config.yml`.

Nobody should have to hand-write the harness, model aliases and sandbox
block to get started. What's actually installed is discoverable (the
adapters know their own binary), and everything else has a defensible
default: the gitchen-shipped sandbox profile, npm's registry and cache when
npm is present, and aliases that vary the effort level of whatever model the
CLI is already configured to use — gitchen doesn't guess model ids, the CLI
already knows its own default.

The generated file is a starting point, not a managed one: it is written
only when it does not exist (`gitchen config init --force` regenerates it).
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from .harness import registered

# Tried in order; the first installed one becomes `harness:`.
PREFERENCE = ("omp", "claude-code")

# Model-API hosts a harness may need. Generated configs allow the common
# ones so a fresh machine works without the user first discovering why the
# model call is blocked. Any `allow_domains` entry is what puts nono in
# proxy mode, where only listed hosts are reachable — leaving this empty
# would silently open *all* outbound traffic, including a `git push`.
API_HOSTS = {
    "omp": ("api.anthropic.com", "api.openai.com", "api.deepinfra.com",
            "openrouter.ai", "generativelanguage.googleapis.com", "api.deepseek.com"),
    "claude-code": ("api.anthropic.com",),
}

# What small/medium/big mean for each harness's own effort flag. omp calls it
# `thinking` and accepts `auto`; Claude Code calls it `effort` and does not.
LEVELS = {
    "omp": {"small": "low", "medium": "auto", "big": "high"},
    "claude-code": {"small": "low", "medium": "medium", "big": "high"},
}


def detect_harnesses() -> dict[str, str]:
    """harness name -> the binary name it would use, for every registered
    adapter whose binary is on PATH."""
    found = {}
    for name, cls in registered().items():
        binary = getattr(cls, "default_binary", None)
        if binary and shutil.which(binary):
            found[name] = binary
    return found


def choose_harness(found: dict[str, str]) -> str:
    for name in PREFERENCE:
        if name in found:
            return name
    return sorted(found)[0]


def render(harness: str, found: dict[str, str]) -> str:
    lines = [
        "# Written by gitchen on first use, from the agent CLIs it found installed.",
        "# Edit freely — gitchen only writes this file when it does not exist.",
        f"harness: {harness}",
        "",
        "harnesses:",
    ]
    has_npm = shutil.which("npm") is not None
    for name in [harness, *sorted(n for n in found if n != harness)]:
        lines.append(f"  {name}:")
        if name == "claude-code":
            lines.append(f"    settings: {{binary: {found[name]}, permission_mode: bypassPermissions}}")
        else:
            lines.append(f"    settings: {{binary: {found[name]}}}")
        if shutil.which("nono") is None:
            lines.append("    sandbox: false          # nono not found on PATH; install it to sandbox turns")
            continue
        lines.append("    sandbox:")
        lines.append("      # Installed by gitchen on first use; see profiles/gitchen.json.")
        lines.append("      profile: gitchen")
        if has_npm:
            lines.append('      allow: ["~/.npm"]')
        hosts = [*API_HOSTS.get(name, ()), *(("registry.npmjs.org",) if has_npm else ())]
        if hosts:
            lines.append(f"      allow_domains: [{', '.join(hosts)}]")

    lines += ["", "models:"]
    for alias in ("small", "medium", "big"):
        entries = []
        for name in sorted(found):
            # `thinking` is omp's effort flag, `effort` is Claude Code's. No
            # model id: each CLI already has its own default model, and the
            # alias only decides how hard it thinks.
            key = "thinking" if name == "omp" else "effort"
            level = LEVELS.get(name, {}).get(alias, alias)
            entries.append(f"{name}: {{{key}: {level}}}")
        lines.append(f"  {alias}: {{{', '.join(entries)}}}")
    return "\n".join(lines) + "\n"


def write_default(path: Path) -> str:
    """Writes a generated config to `path` and returns its text. Raises when
    there is nothing to generate from — that's a human decision, not a
    default to invent."""
    found = detect_harnesses()
    if not found:
        raise RuntimeError(
            f"no supported agent CLI found on PATH (looked for {', '.join(sorted(_binaries()))}): "
            f"install one, or write {path} yourself — config.example.yml is the template")
    harness = choose_harness(found)
    text = render(harness, found)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    print(f"wrote machine config {path} (harness: {harness}; detected: {', '.join(sorted(found))})",
          file=sys.stderr, flush=True)
    if shutil.which("nono") is None:
        print("warning: nono is not on PATH — turns will run unsandboxed until it is installed",
              file=sys.stderr, flush=True)
    return text


def _binaries() -> list[str]:
    return sorted({cls.default_binary for cls in registered().values()
                   if getattr(cls, "default_binary", None)})
