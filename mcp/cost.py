"""What a turn cost, per model, from what the harness reports.

A usage record is {"model", "usd", "input", "output", "cache_read",
"cache_write"}. Harnesses report usage in one of two ways, and a turn can be
resumed, so the difference matters:

- Claude Code reports totals for the whole session, including earlier calls
  on a `--resume`. Its turn sets `usage_cumulative`, and the latest totals
  replace what was recorded before.
- omp reports each model call on its own. A resumed turn's new messages are
  added to what was recorded before.

Either way, a turn's records are written once, with its final comment.
"""

from __future__ import annotations

_AMOUNTS = ("usd", "input", "output", "cache_read", "cache_write")


def record(model: str, usd: float = 0.0, input: int = 0, output: int = 0,
           cache_read: int = 0, cache_write: int = 0) -> dict:
    return {"model": model, "usd": round(float(usd), 6), "input": int(input),
            "output": int(output), "cache_read": int(cache_read), "cache_write": int(cache_write)}


def merge_usage(previous: list[dict], turn) -> list[dict]:
    """The turn's usage so far, after one more call's result."""
    if not turn.usage:
        return previous
    if turn.usage_cumulative:
        return [dict(r) for r in turn.usage]
    totals: dict[str, dict] = {}
    for item in [*previous, *turn.usage]:
        total = totals.setdefault(item["model"], {"model": item["model"], **{k: 0 for k in _AMOUNTS}})
        for key in _AMOUNTS:
            total[key] += item.get(key, 0)
    return [{**t, "usd": round(t["usd"], 6)} for t in totals.values()]


def total_usd(records: list[dict]) -> float:
    return round(sum(r.get("usd", 0.0) for r in records), 6)
