"""Importing this package registers every built-in harness adapter (each
module's `@register(...)` decorator runs on import) — so anything that
calls `get_harness()` needs this imported first, which `mcp/harness.py`
does itself below rather than leaving it to every caller to remember."""

from . import claude_code  # noqa: F401
from . import omp  # noqa: F401
from . import program  # noqa: F401
