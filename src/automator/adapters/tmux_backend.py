"""tmux backend for the terminal-multiplexer seam.

This is the **only** file in the codebase allowed to shell out to ``tmux`` —
every POSIX-shell trailer and tmux invocation is quarantined here so a future
non-POSIX backend (an eventual native-Windows "psmux") can replace it wholesale.
See :mod:`.multiplexer` for the contract.

Phase 1 implements the subset the generic adapter drives plus the parked-window
trailer; the remaining operations are stubbed and filled as the other call sites
(``runs.py``, ``tui/launch.py``, ``probe.py``, ``tui/data.py``) migrate in Phase 2.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import time
from pathlib import Path

from .multiplexer import TerminalMultiplexer

TMUX_TIMEOUT_S = 30
# Per-window option value (vs a pane id) telling the parked trailer to detach the
# client rather than switch it. Pane ids are %N, so this never collides with one.
PARKED_RETURN_DETACH = "detach"


class TmuxError(Exception):
    pass


class TmuxMultiplexer(TerminalMultiplexer):
    def _tmux(self, *args: str) -> str:
        proc = subprocess.run(
            ["tmux", *args], capture_output=True, text=True, timeout=TMUX_TIMEOUT_S
        )
        if proc.returncode != 0:
            raise TmuxError(f"tmux {' '.join(args[:2])} failed: {proc.stderr.strip()}")
        return proc.stdout.strip()

    # ----------------------------------------------------------- sessions

    def has_session(self, name: str) -> bool:
        probe = subprocess.run(
            ["tmux", "has-session", "-t", f"={name}"],
            capture_output=True,
            timeout=TMUX_TIMEOUT_S,
        )
        return probe.returncode == 0

    def new_session(self, name: str, cwd: Path, cols: int, lines: int) -> None:
        # Window 0 is a plain shell so the session survives task windows closing.
        self._tmux(
            "new-session",
            "-d",
            "-s",
            name,
            "-c",
            str(cwd),
            "-x",
            str(cols),
            "-y",
            str(lines),
        )

    def set_session_option(self, name: str, option: str, value: str) -> None:
        # set-option has no '=' exact-match form; callers pass a unique full
        # session name so plain-name targeting resolves it unambiguously.
        self._tmux("set-option", "-t", name, option, value)

    def kill_session(self, name: str) -> None:
        raise NotImplementedError("kill_session: Phase 2")  # Phase 2: runs.py

    def list_sessions(self) -> list[str]:
        raise NotImplementedError("list_sessions: Phase 2")  # Phase 2: runs.py

    def session_options(self, option: str) -> dict[str, str]:
        raise NotImplementedError("session_options: Phase 2")  # Phase 2: runs.py

    # ------------------------------------------------------------ windows

    def new_window(
        self, session: str, name: str, cwd: Path, env: dict[str, str], command: str
    ) -> str:
        env_args: list[str] = []
        for key, value in env.items():
            env_args += ["-e", f"{key}={value}"]
        return self._tmux(
            "new-window",
            "-t",
            f"={session}:",
            "-n",
            name,
            "-c",
            str(cwd),
            "-P",
            "-F",
            "#{window_id}",
            *env_args,
            command,
        )

    def new_parked_window(
        self, session: str, name: str, cwd: Path, argv: list[str], return_opt: str
    ) -> str:
        # The window runs under explicit `sh -c` (the user's login shell may be
        # fish); the trailing `read` keeps the exit status inspectable instead of
        # tmux closing the window the moment the process exits. After the read the
        # return trailer switches an attached client back to its origin pane:
        #   - return_opt == a pane id (%N): switch that client back there
        #     (`switch-client -l` is a best-effort fallback when it is gone);
        #   - return_opt == PARKED_RETURN_DETACH: detach the client so a blocking
        #     `tmux attach` returns and a suspended TUI resumes;
        #   - unset/empty: nobody attached interactively -> park as-is.
        return_trailer = (
            f"ret=$(tmux show-options -wqv {return_opt} 2>/dev/null); "
            f'if [ "$ret" = "{PARKED_RETURN_DETACH}" ]; then tmux detach-client 2>/dev/null; '
            'elif [ -n "$ret" ]; then '
            'tmux switch-client -t "$ret" 2>/dev/null || tmux switch-client -l 2>/dev/null; '
            "fi"
        )
        inner = shlex.join(argv)
        shell = (
            f'{inner}; ec=$?; echo "[bmad-auto exited $ec — press enter]"; '
            f"read -r; {return_trailer}"
        )
        return self._tmux(
            "new-window",
            "-d",
            "-P",
            "-F",
            "#{window_id}",
            "-t",
            f"={session}:",
            "-n",
            name,
            "-c",
            str(cwd),
            "sh",
            "-c",
            shell,
        )

    def list_window_ids(self, session: str) -> list[str]:
        # display-message -t <dead-window> exits 0 with empty output, so list the
        # session's window ids and check membership instead.
        probe = subprocess.run(
            ["tmux", "list-windows", "-t", f"={session}", "-F", "#{window_id}"],
            capture_output=True,
            text=True,
            timeout=TMUX_TIMEOUT_S,
        )
        if probe.returncode != 0:
            return []
        return probe.stdout.split()

    def pipe_pane(self, window_id: str, log_file: Path) -> None:
        # A CLI that crashes on launch (bad args, instant auth failure) can take
        # its window down before pipe-pane attaches, which races as "can't find
        # window". That is not a setup failure, so tolerate it instead of raising.
        try:
            self._tmux("pipe-pane", "-t", window_id, "-o", f"cat >> {shlex.quote(str(log_file))}")
        except TmuxError:
            pass

    def send_text(self, window_id: str, text: str) -> None:
        self._tmux("send-keys", "-t", window_id, "-l", text)
        time.sleep(0.3)  # let the TUI ingest the paste before submitting
        self._tmux("send-keys", "-t", window_id, "Enter")

    def kill_window(self, target: str) -> None:
        subprocess.run(
            ["tmux", "kill-window", "-t", target],
            capture_output=True,
            timeout=TMUX_TIMEOUT_S,
        )

    def list_windows(self, session: str, fields: list[str]) -> list[tuple[str, ...]]:
        raise NotImplementedError("list_windows: Phase 2")  # Phase 2: tui/launch.py

    def window_alive(self, session: str, window_id: str) -> bool:
        raise NotImplementedError("window_alive: Phase 2")  # Phase 2: probe.py, tui/data.py

    def select_window(self, target: str) -> None:
        raise NotImplementedError("select_window: Phase 2")  # Phase 2: tui/launch.py

    def set_window_option(self, target: str, option: str, value: str) -> None:
        raise NotImplementedError("set_window_option: Phase 2")  # Phase 2: tui/launch.py

    def show_window_option(self, target: str, option: str) -> str:
        raise NotImplementedError("show_window_option: Phase 2")  # Phase 2: tui/launch.py

    # ----------------------------------------------------- client / attach

    def attach_target_argv(self, target: str) -> list[str]:
        raise NotImplementedError("attach_target_argv: Phase 2")  # Phase 2: runs.py

    def current_pane_id(self) -> str | None:
        raise NotImplementedError("current_pane_id: Phase 2")  # Phase 2: tui/launch.py

    def current_window_id(self) -> str | None:
        raise NotImplementedError("current_window_id: Phase 2")  # Phase 2: tui/launch.py

    def current_session(self) -> str | None:
        raise NotImplementedError("current_session: Phase 2")  # Phase 2: tui/launch.py

    def detach_client(self) -> None:
        raise NotImplementedError("detach_client: Phase 2")  # Phase 2: tui/launch.py

    def switch_client(self, target: str, last_fallback: bool = False) -> bool:
        raise NotImplementedError("switch_client: Phase 2")  # Phase 2: tui/launch.py

    def available(self) -> bool:
        return shutil.which("tmux") is not None
