"""`gitchen retro`: what the recorded turns cost, by role and model, and which
issues cost the most. It reads only what is on the issues, and changes nothing.

A retro is the place to see whether a model is worth its price for a role, or
which kind of work keeps coming back. It totals the cost records (see cost.py),
so it is only as complete as those are."""

from __future__ import annotations

import calendar
import re
import time

from . import issues
from .config import ProjectConfig

_SINCE = re.compile(r"^(\d+)([hd])$")


def parse_since(text: str) -> int:
    """`12h` or `7d`, as seconds."""
    match = _SINCE.match(text.strip())
    if not match or int(match.group(1)) == 0:
        raise ValueError(f"--since must look like 12h or 7d, not {text!r}")
    return int(match.group(1)) * (3600 if match.group(2) == "h" else 86400)


def _epoch(at: str) -> float:
    return float(calendar.timegm(time.strptime(at, "%Y-%m-%dT%H:%M:%SZ")))


def summarize(cfg: ProjectConfig, since: int | None = None, top: int = 10,
              now: float | None = None) -> dict:
    now = time.time() if now is None else now
    start = 0.0 if since is None else now - since
    turns = [t for t in issues.all_turns(cfg) if _epoch(t["at"]) >= start]

    grouped: dict[tuple[str, str], dict] = {}
    for t in turns:
        row = grouped.setdefault((t["role"], t["model"]), {
            "role": t["role"], "model": t["model"], "records": 0, "usd": 0.0,
            "tokens": 0, "seconds": 0})
        row["records"] += 1
        row["usd"] = round(row["usd"] + t["usd"], 6)
        row["tokens"] += t.get("input", 0) + t.get("output", 0)
        row["seconds"] += t.get("seconds", 0)

    per_issue: dict[int, float] = {}
    for t in turns:
        per_issue[t["issue"]] = round(per_issue.get(t["issue"], 0.0) + t["usd"], 6)
    titles = {i["id"]: i["title"] for i in issues.list_issues(cfg)}
    costliest = sorted(per_issue.items(), key=lambda kv: (-kv[1], kv[0]))[:top]

    return {
        "turns": len({(t["issue"], t["at"], t["role"]) for t in turns}),
        "total_usd": round(sum(t["usd"] for t in turns), 6),
        "by_role_model": sorted(grouped.values(), key=lambda r: (-r["usd"], r["role"], r["model"])),
        "costliest": [{"id": i, "title": titles.get(i, ""), "usd": usd} for i, usd in costliest],
    }


def render(summary: dict) -> str:
    if not summary["turns"]:
        return "No turns recorded yet."
    lines = [f"{summary['turns']} turns, ${summary['total_usd']:.2f} in all", "",
             f"{'role':<12} {'model':<34} {'calls':>5} {'tokens':>9} {'time':>8} {'usd':>9}"]
    for row in summary["by_role_model"]:
        lines.append(f"{row['role']:<12} {row['model'][:34]:<34} {row['records']:>5} "
                     f"{row['tokens']:>9} {row['seconds']:>7}s ${row['usd']:>8.2f}")
    lines += ["", "costliest issues"]
    for item in summary["costliest"]:
        lines.append(f"  #{item['id']:<5} ${item['usd']:>8.2f}  {item['title']}")
    return "\n".join(lines)
