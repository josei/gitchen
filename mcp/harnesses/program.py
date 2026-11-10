"""A program role: its prompt names a script, and the script is the role.

No model runs. The script gets the turn's prompt on stdin, its working folder
and its environment, and what it prints is the report. To change the project
it calls the gitchen tools through `gitchen call`, which the daemon authorizes
exactly as it would an agent's call. The script's last line may be a directive
(`NEXT: ...`), read the same way as an agent's. A script that fails is a failed
turn. It spends no model tokens, so it records no cost."""

from __future__ import annotations

import sys
from pathlib import Path

from ..harness import TurnResult, register
from .process import run as run_process
from .sandbox import prefix as sandbox_prefix


@register("program")
class ProgramHarness:
    def start(self, prompt: str, cwd: str, settings: dict) -> TurnResult:
        program = Path(settings["program"])
        args = [sys.executable, str(program)] if program.suffix == ".py" else [str(program)]
        returncode, stdout, stderr = run_process(sandbox_prefix(settings, cwd) + args, cwd,
                                                 settings.get("env"), stdin_text=prompt)
        if returncode == 0:
            text = stdout.strip() or stderr.strip() or "(exit 0, no output)"
        else:
            # A failure's reason is on stderr; stdout may hold only a sandbox's
            # banner. The end of stderr is where the reason is.
            text = (stderr.strip() or stdout.strip() or f"(exit {returncode}, no output)")[-2000:]
        return TurnResult(session_id="", text=text, ok=returncode == 0,
                          raw={"returncode": returncode, "stdout": stdout, "stderr": stderr})

    def resume(self, session_id: str, prompt: str, cwd: str, settings: dict) -> TurnResult:
        # A program is not a conversation: there is nothing to correct. A missing
        # directive ends the turn on the normal no-directive path.
        return TurnResult(session_id=session_id, text="", ok=True, raw={})

    def interrupt(self, session_id: str) -> None:
        pass
