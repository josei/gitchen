"""Usage records: what a turn cost, per model, however many calls it took."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mcp import cost  # noqa: E402
from mcp.harness import TurnResult  # noqa: E402


def turn(usage, cumulative=False):
    return TurnResult("s", "text", {}, True, usage=usage, usage_cumulative=cumulative)


def test_a_turn_without_usage_keeps_what_was_recorded():
    before = [cost.record("m", usd=0.5)]
    assert cost.merge_usage(before, TurnResult("s", "t", {}, True)) == before


def test_per_call_usage_is_summed_per_model():
    first = [cost.record("m", usd=0.25, input=100, output=10)]
    second = [cost.record("m", usd=0.5, input=50, output=5), cost.record("other", usd=1.0)]
    merged = cost.merge_usage(first, turn(second))
    by_model = {r["model"]: r for r in merged}
    assert by_model["m"]["usd"] == 0.75 and by_model["m"]["input"] == 150
    assert by_model["m"]["output"] == 15
    assert by_model["other"]["usd"] == 1.0


def test_cumulative_totals_replace_what_was_recorded():
    """Claude Code reports the whole session on a resume; adding would double-count."""
    first = [cost.record("m", usd=0.25)]
    totals = [cost.record("m", usd=0.4)]
    assert cost.merge_usage(first, turn(totals, cumulative=True)) == totals


def test_total_usd_adds_every_model():
    assert cost.total_usd([cost.record("a", usd=0.1), cost.record("b", usd=0.2)]) == 0.3
