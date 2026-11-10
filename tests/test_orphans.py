"""A turn's processes never outlive the daemon that started them. Reproduced:
stopping the daemon while a turn ran left the turn's child running, with its
results and its holder record gone, so nothing could ever report on it."""

import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

OWNER = textwrap.dedent("""\
    import sys, threading, time
    sys.path.insert(0, sys.argv[1])
    from mcp.harnesses import process
    process.install_term_handler()
    threading.Thread(target=lambda: process.run(["sleep", "300"], sys.argv[2]), daemon=True).start()
    print("ready", flush=True)
    time.sleep(600)
""")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _child_of_sleep(marker: str) -> int | None:
    out = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True).stdout.split()
    return int(out[0]) if out else None


def test_stopping_the_owner_ends_the_turn_it_started(tmp_path):
    """Without install_term_handler the turn's process survives its owner: the
    same run, checked with ps, shows it reparented and still sleeping."""
    owner = tmp_path / "owner.py"
    owner.write_text(OWNER)
    marker = f"sleep 300"
    proc = subprocess.Popen([sys.executable, str(owner), str(Path(__file__).resolve().parent.parent),
                             str(tmp_path)], stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "ready"
    deadline = time.time() + 10
    child = None
    while time.time() < deadline and child is None:
        child = _child_of_sleep(marker)
        time.sleep(0.1)
    assert child is not None, "the turn's process should be running"
    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=30)
    deadline = time.time() + 15
    while time.time() < deadline and _alive(child):
        time.sleep(0.2)
    assert not _alive(child), "the turn's process must not outlive its owner"
    subprocess.run(["pkill", "-f", marker], capture_output=True)
