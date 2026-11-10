"""A drain: the daemon stops taking new turns, lets the running ones finish, then
leaves on its own. It is a request on disk, not a pause: a completed turn does not
clear it, so the daemon can't come back to work partway through a drain."""

from __future__ import annotations

import json
import time
from pathlib import Path


def _path(root: Path) -> Path:
    return Path(root) / ".agents-daemon" / "draining"


def request(root: Path) -> None:
    path = _path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"requested_at": time.time()}))


def requested(root: Path) -> bool:
    return _path(root).exists()


def clear(root: Path) -> None:
    _path(root).unlink(missing_ok=True)
