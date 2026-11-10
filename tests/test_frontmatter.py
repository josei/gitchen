import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mcp import frontmatter  # noqa: E402

LIBRARY_AGENTS = Path(__file__).resolve().parent.parent / "library" / "agents"


def test_parse_and_serialize_round_trip():
    raw = "---\nname: builder\ndescription: does things\n---\nThe body text.\n"
    front, body = frontmatter.parse(raw)
    assert front == {"name": "builder", "description": "does things"}
    assert body == "The body text.\n"
    assert frontmatter.serialize(front, body) == raw


def test_no_frontmatter_returns_empty_dict_and_raw_text():
    front, body = frontmatter.parse("just a plain file, no frontmatter\n")
    assert front == {}
    assert body == "just a plain file, no frontmatter\n"


def test_unquoted_colon_in_a_field_needs_quoting_to_parse():
    """Real regression: an agent file's `description:` field containing an
    unquoted 'WORD: ...' pattern (e.g. "ends with a NEXT: directive")
    breaks YAML parsing — a colon-space inside a plain scalar reads as a
    nested mapping key, not literal text. Found via a real dispatch run
    once dispatch.py started parsing agent file frontmatter at all (it
    used to pass the raw file through unparsed, which is a different bug
    — see claude_code harness history — but masked this one)."""
    raw = '---\nname: builder\ndescription: "ends with a NEXT: directive, not a tool call"\n---\nBody.\n'
    front, body = frontmatter.parse(raw)
    assert front["description"] == "ends with a NEXT: directive, not a tool call"


def test_unquoted_colon_without_quoting_raises():
    """The failure mode this guards against, made explicit: without
    quoting, the same text is a YAML parse error, not silently wrong
    output — so this is a real crash, not a subtle bug, if it recurs."""
    import yaml
    raw = "---\nname: builder\ndescription: ends with a NEXT: directive\n---\nBody.\n"
    try:
        frontmatter.parse(raw)
        assert False, "expected a YAML parse error from the unquoted colon"
    except yaml.YAMLError:
        pass


def test_every_real_library_agent_file_actually_parses():
    """The direct guard against this recurring: every shipped role prompt
    must parse cleanly, not just the hand-picked example above. dispatch.py
    parses these for real on every spawn (see _build_prompt)."""
    for path in LIBRARY_AGENTS.glob("*.md"):
        front, body = frontmatter.parse(path.read_text())
        assert front.get("name"), f"{path.name} has no parsed 'name' — frontmatter likely broke"
        assert body.strip(), f"{path.name} has no body after stripping frontmatter"
