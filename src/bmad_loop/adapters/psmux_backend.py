"""Native-Windows psmux backend for the terminal-multiplexer seam.

psmux (a Rust/ConPTY tmux re-implementation) speaks the tmux CLI through its
own distinctly-named ``psmux`` binary, so this leaf points the base's spawn
seam at that name, keeps every argv construction in :mod:`.tmux_base`, and
swaps only the shell dialect (PowerShell instead of POSIX sh) via the base's
hooks, plus the handful of behaviors where psmux diverges from tmux:
window-level ``-e`` is accepted but silently dropped, an attaching
``new-session`` is refused by a nesting guard when run from inside a psmux
pane, ``kill-session`` ignores the ``=name`` exact-match form, and a quoted
command string does not survive psmux's outer re-parse (so shell source
travels as ``pwsh -EncodedCommand``). ``available()`` additionally gates on
the reported version: psmux releases up to 3.3.6 kill recycled PIDs during
pane/session teardown without a process-identity check, which can take down
an unrelated long-lived process mid-run. See :mod:`.multiplexer` for the
contract.
"""

from __future__ import annotations

import base64
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from .tmux_base import PARKED_RETURN_DETACH, BaseTmuxBackend, TmuxError

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _pwsh_quote(value: str) -> str:
    # A single-quoted PowerShell literal: no interpolation, and the only escape
    # is doubling the quote itself.
    return "'" + value.replace("'", "''") + "'"


class PsmuxMultiplexer(BaseTmuxBackend):
    """psmux backend — tmux-family argv from the base, PowerShell dialect and
    the documented psmux divergences here.

    Registered by :func:`~.multiplexer._load_builtin_backends` for ``win32``,
    mirroring :class:`~.tmux_backend.TmuxMultiplexer`.
    """

    # psmux ships psmux/pmux/tmux binaries built from the same source; spawning
    # the distinct psmux name never collides with another tmux-family install
    # (e.g. a tmux-windows port owning ``tmux`` on the same PATH).
    _BINARY = "psmux"
    # psmux emits UTF-8; decoding with the console codepage (cp1252) garbles
    # format-string output, and a stray byte must degrade visibly, not raise.
    _ENCODING = "utf-8"
    _ERRORS = "backslashreplace"

    # ------------------------------------------- shell dialect (PowerShell)

    # A command pwsh could not even start (not recognized) still runs the rest of
    # the source but leaves $LASTEXITCODE unset — coalesce with a plain `if`
    # (works on any PowerShell version) rather than the PS7-only `??` syntax.
    _EXIT_CAPTURE = "$ec = if ($null -eq $LASTEXITCODE) { 1 } else { $LASTEXITCODE }"
    _ECHO = "Write-Host"
    _PARK = "Read-Host"

    def _join_argv(self, argv: list[str]) -> str:
        # The call operator runs a quoted executable with quoted args verbatim.
        # A bare `& ` is a pwsh parse error, so refuse an empty argv here rather
        # than shipping a window that dies on launch.
        if not argv:
            raise TmuxError("empty command")
        return "& " + " ".join(_pwsh_quote(arg) for arg in argv)

    def _source_prefix(self) -> str:
        # psmux windows inherit the claude environment of whichever process
        # cold-started the psmux server (teammate mode, session ids, SSE ports).
        # Clear it so a CLI launched here starts fresh instead of impersonating
        # that session.
        return (
            "Get-ChildItem Env: | Where-Object { $_.Name -like 'CLAUDE_CODE_*' "
            "-or $_.Name -like 'CLAUDECODE*' -or $_.Name -eq 'PSMUX_CLAUDE_TEAMMATE_MODE' } "
            "| ForEach-Object { Remove-Item ('Env:' + $_.Name) }; "
        )

    def _shell_wrap(self, source: str) -> list[str]:
        # psmux joins the trailing argv and re-parses it through an outer shell,
        # which strips embedded quoting; -EncodedCommand (base64 of UTF-16LE) is
        # the lossless transport for arbitrary shell source.
        encoded = base64.b64encode(source.encode("utf-16-le")).decode("ascii")
        return ["pwsh", "-NoProfile", "-EncodedCommand", encoded]

    def _parked_trailer(self, return_opt: str) -> str:
        # The base's trailer re-expressed in pwsh — the tmux verbs are protocol-
        # identical across the family. Errors go to $null: a client or pane that
        # is already gone means the window just parks as-is.
        mux = self._BINARY
        return (
            f"$ret = {mux} show-options -wqv {_pwsh_quote(return_opt)} 2>$null; "
            f"if ($ret -eq '{PARKED_RETURN_DETACH}') {{ {mux} detach-client 2>$null }} "
            f"elseif ($ret) {{ {mux} switch-client -t $ret 2>$null; "
            f"if ($LASTEXITCODE -ne 0) {{ {mux} switch-client -l 2>$null }} }}"
        )

    def _window_launch(self, env: dict[str, str], command: str) -> list[str]:
        # psmux accepts `new-window -e` but silently drops it, so the env rides
        # an in-source prelude instead. `command` arrives POSIX-quoted (callers
        # shlex-quote each arg), so split it here and re-quote for pwsh.
        for key in env:
            if not _ENV_NAME.fullmatch(key):
                raise TmuxError(f"invalid environment variable name: {key!r}")
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            raise TmuxError(f"unparseable command: {exc}") from exc
        prelude = "".join(f"$env:{key} = {_pwsh_quote(value)}; " for key, value in env.items())
        source = self._source_prefix() + prelude + self._join_argv(argv)
        return self._shell_wrap(source)

    # ------------------------------------------------- psmux divergences

    def new_session(
        self, name: str, cwd: Path, cols: int | None = None, lines: int | None = None
    ) -> None:
        # psmux's nesting guard refuses new-session from inside a psmux pane
        # (current builds only for an attaching one, older builds no-op'd `-d`
        # too — exit 0, nothing created); the documented bypass is one env var
        # on the create call, kept as a cheap belt. The create env copies the
        # parent env rather than building from scratch — Windows children need
        # SystemRoot etc. — but scrubs the claude session vars (the same names
        # _source_prefix clears per window): this call may cold-start the psmux
        # server, whose env every window then inherits.
        env = {
            k: v
            for k, v in os.environ.items()
            if not (
                k.upper().startswith(("CLAUDE_CODE_", "CLAUDECODE"))
                or k.upper() == "PSMUX_CLAUDE_TEAMMATE_MODE"
            )
        }
        env["PSMUX_ALLOW_NESTING"] = "1"
        geometry = ["-x", str(cols), "-y", str(lines)] if cols and lines else []
        try:
            proc = self._run(
                ["new-session", "-d", "-s", name, "-c", str(cwd), *geometry],
                check=False,
                env=env,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise TmuxError(f"{self._BINARY} new-session failed: {exc}") from exc
        if proc.returncode != 0:
            raise TmuxError(f"{self._BINARY} new-session failed: {proc.stderr.strip()}")
        # Belt for the nesting guard's historical no-op mode (exit 0, nothing
        # created): verify the session exists so the failure blames session
        # creation, not the next verb's "can't find session".
        if not self.has_session(name):
            raise TmuxError(
                f"{self._BINARY} new-session exited 0 but session {name!r} was not "
                "created (nesting guard no-op?)"
            )

    def kill_session(self, name: str) -> None:
        # psmux ignores the `=name` exact-match form for kill-session; plain-name
        # targeting works. Same best-effort tolerance as the base.
        if not shutil.which(self._BINARY):
            return
        try:
            self._run(["kill-session", "-t", name], check=False)
        except (subprocess.SubprocessError, OSError):
            pass

    def pipe_pane(self, window_id: str, log_file: Path) -> None:
        # The base's POSIX `cat >>` sink assumes a POSIX host shell; psmux runs
        # the pipe command on the host shell, so ship a pwsh append sink instead.
        # A raw stream copy is byte-exact like `cat >>`: no console decode of the
        # pane bytes, no Add-Content re-encode, no CRLF normalization.
        sink = (
            f"$out = [System.IO.File]::Open({_pwsh_quote(str(log_file))}, "
            "'Append', 'Write', 'Read'); "
            "[System.Console]::OpenStandardInput().CopyTo($out); $out.Dispose()"
        )
        wrapped = self._shell_wrap(sink)
        # base64 has no quoting to lose, so a plain join survives psmux's re-parse
        # — valid only while no wrapped arg contains a space.
        assert all(" " not in part for part in wrapped)
        try:
            self._tmux("pipe-pane", "-t", window_id, "-o", " ".join(wrapped))
        except TmuxError as exc:
            # Best-effort, as the base: a window that died on launch is not a
            # setup failure — but say so, or an empty run log is unexplainable.
            print(
                f"warning: pipe-pane log capture failed for {window_id}: {exc}",
                file=sys.stderr,
            )

    # Releases up to this version can force-kill a recycled PID during pane
    # teardown and let orphaned servers accumulate — engine-fatal, so they
    # must never be selected.
    _LAST_UNSUPPORTED = (3, 3, 6)
    # Class-level default; instances shadow it on first probe. Never assign on
    # the class outside tests — that would poison every future instance.
    _version_ok: bool | None = None

    def available(self) -> bool:
        # Every window launch needs pwsh alongside the psmux binary itself.
        # The version gate fails closed: psmux prints `tmux X.Y.Z` (the tmux
        # prefix is kept deliberately for tmux-version parsers), and an old or
        # unidentifiable install reads as unusable. A forced backend name (env
        # var or policy) still bypasses this probe, with a warning at the
        # launch gates (see multiplexer.mux_usable). The gate verdict is cached
        # on the instance so repeated availability polls don't each spawn a
        # version query; the lru-cached selected instance re-probes a swapped
        # install only on restart (detect_multiplexers' fresh instances
        # re-probe every call).
        if not all(shutil.which(exe) for exe in (self._BINARY, "pwsh")):
            return False
        if self._version_ok is None:
            # A missing patch segment reads as 0 — psmux hardwires three-part
            # Cargo semver today, but real tmux versions are two-part and the
            # compat prefix invites upstream to mirror that format someday.
            reported = re.match(r"tmux (\d+)\.(\d+)(?:\.(\d+))?", self.version() or "")
            self._version_ok = bool(reported) and (
                tuple(int(part or 0) for part in reported.groups()) > self._LAST_UNSUPPORTED
            )
        return self._version_ok
