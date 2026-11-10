"""What counts as an artifact: a file an agent produced that is worth keeping
with its issue but doesn't belong in a repository (a screenshot, a log, a
report). One table, shared by the daemon (what it will copy and rewrite a
link for) and the web view (what it will serve)."""

from __future__ import annotations

from pathlib import Path

_TEXT = "text/plain; charset=utf-8"

# Images render inline; text-like files are served as plain text, never as
# HTML or script, whatever the extension claims.
TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif", ".pdf": "application/pdf",
    ".txt": _TEXT, ".log": _TEXT, ".md": _TEXT, ".json": _TEXT,
    ".csv": _TEXT, ".diff": _TEXT, ".patch": _TEXT,
}
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif")
MAX_BYTES = 8 * 1024 * 1024


def is_artifact(path: Path | str) -> bool:
    return Path(path).suffix.lower() in TYPES
