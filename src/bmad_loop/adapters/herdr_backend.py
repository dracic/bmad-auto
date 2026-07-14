"""Herdr backend for the terminal-multiplexer seam.

Herdr (https://herdr.dev) is a cross-platform, agent-aware terminal workspace
manager: a headless background server plus a CLI, talking over a Unix domain
socket on POSIX and a **named pipe on Windows**. Implementing the
:class:`~.multiplexer.TerminalMultiplexer` contract on top of it gives bmad-loop a
native-Windows-capable transport *and* herdr's agent-status sidebar for watching
runs — with no core edits (a new backend is a new file plus one registration
line; see ``docs/porting-to-a-new-os.md``).

Unlike the tmux family, this backend does **not** subclass the tmux base: herdr's
object model and CLI are a different binary family, so it implements the contract
fresh. The mapping (characterized against herdr 0.7.3 / protocol 16 — see the
plan's Phase-0 Findings):

- bmad-loop session  -> herdr **workspace** (label == the session name)
- bmad-loop window   -> herdr **tab** (one shell pane); the native window id we
  hand back is that tab's ``root_pane.pane_id`` (``w1:p1``-shaped)
- the launched command runs via a typed ``exec <argv>`` into that pane
  (``pane run`` = type + Enter atomically) so process-exit == pane-close ==
  tab-close == tmux-identical window death

All herdr subprocess I/O is funnelled through :class:`_HerdrClient` (the ``_run``
/ ``_herdr`` / ``_herdr_json`` primitives plus the server lifecycle), so a future
socket transport can replace it without touching :class:`HerdrMultiplexer`.

**Degradation ledger (PR 1 scope — the run path; the TUI-launch surface is PR 2):**

- ``pipe_pane`` has no herdr ``pipe-pane``/tee to hand off to, so it runs a
  per-window :class:`_PanePoller` daemon that snapshots ``pane read`` into the
  log whenever the pane content changes (content-hash-gated — the CLI
  ``revision`` is unusable, it stays 0). This drives the two log consumers a
  tmux tee would: ``generic._log_activity_key`` re-arms the dev-stall grace on
  log growth, and ``probe`` finds completion markers in the log.
- ``new_parked_window`` **raises** ``HerdrError`` — only ``tui/launch.py`` calls
  it (PR 2).
- ``detach_client`` is a no-op — herdr detach is a keybinding, with no CLI verb.
- Session/window **options** have no native herdr equivalent, so they live in a
  cross-process **sidecar** JSON (``~/.bmad-loop/herdr-state.json``, override
  ``BMAD_LOOP_HERDR_STATE``), written via :func:`platform_util.atomic_replace`;
  entries for a workspace that is gone are pruned on the next enumeration.
- ``new_session`` geometry (cols/lines) is **advisory** — herdr exposes no
  absolute headless resize; a detached pane takes an attaching client's size.
- Protocol policy: fail below :data:`SUPPORTED_PROTOCOL`, warn once above.

See :mod:`.multiplexer` for the contract and the raisers-vs-sentinels split.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import threading
import time
import warnings
from pathlib import Path

from .. import platform_util
from .multiplexer import MultiplexerError, TerminalMultiplexer

HERDR_TIMEOUT_S = 30
# The herdr server wire protocol this backend was written against (0.7.3). Read
# back from `herdr status --json` -> .server.protocol on the first server op.
SUPPORTED_PROTOCOL = 16
# How long _start_server waits for a freshly spawned `herdr server` to report
# itself running before giving up. Module-level so tests can shrink it.
SERVER_START_TIMEOUT_S = 5.0
_SERVER_POLL_S = 0.1

# pipe_pane poller cadence: how often a _PanePoller re-reads its pane, and how
# many CONSECUTIVE server-answered pane-not-founds retire it (the pane vanished
# on process exit — see Phase-0 O1). Module-level so tests can shrink them.
POLL_INTERVAL_S = 1.0
POLL_NOT_FOUND_LIMIT = 3

# Env var herdr injects into every pane it spawns; its presence is how the
# current_* accessors know this process is running inside a herdr pane.
_HERDR_ENV_MARKER = "HERDR_ENV"


class HerdrError(MultiplexerError):
    """A herdr transport op failed — the seam type, so call sites catch it via
    :class:`~.multiplexer.MultiplexerError` without importing this backend."""


# --------------------------------------------------------------- sidecar state
#
# herdr has no per-session/per-window user options (tmux's `set-option -t`), so
# the @bmad_project prune tag and the @bmad_return_pane launch marker are kept in
# a small JSON file shared across processes. Keyed by the durable identities
# bmad-loop already uses: session name (== workspace label) and native window id
# (== pane id). Reads tolerate a missing/corrupt file; writes go through
# atomic_replace so a concurrent reader never sees a torn file.


def _state_path() -> Path:
    override = os.environ.get("BMAD_LOOP_HERDR_STATE")
    if override:
        return Path(override)
    return Path.home() / ".bmad-loop" / "herdr-state.json"


def _load_state() -> dict:
    """The sidecar as a ``{"sessions": {...}, "windows": {...}}`` dict. A missing
    or unreadable/corrupt file reads as empty — never raises."""
    try:
        raw = _state_path().read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    sessions = data.get("sessions")
    windows = data.get("windows")
    return {
        "sessions": sessions if isinstance(sessions, dict) else {},
        "windows": windows if isinstance(windows, dict) else {},
    }


def _save_state(state: dict) -> None:
    """Persist the sidecar atomically. Propagates ``OSError`` — a *raiser* caller
    (set_session_option) wraps it as :class:`HerdrError`; *sentinel* callers
    swallow it."""
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
    try:
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        platform_util.atomic_replace(tmp, path)
    except OSError:
        # Don't leave a half-written temp behind on a failed swap.
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


# ----------------------------------------------------------------- transport
#
# The ONE place herdr is spawned. Everything protocol-specific about the CLI wire
# format lives above this line as argv the multiplexer builds; the client only
# spawns, decodes, and enforces the protocol/server lifecycle. A socket transport
# would reimplement these primitives and leave HerdrMultiplexer untouched.


class _HerdrClient:
    """Isolates all herdr subprocess I/O behind three spawn primitives plus the
    server-lifecycle guard, so a socket transport can swap in later."""

    #: Output decoding for captured herdr text; None = locale default (POSIX),
    #: a Windows leaf could set "utf-8". Mirrors BaseTmuxBackend._ENCODING.
    _ENCODING: str | None = None

    def __init__(self) -> None:
        # Set once the server is confirmed up AND its protocol checked, so the
        # per-op ensure_server() is a cheap no-op for the rest of the run.
        self._ensured = False
        self._proto_warned = False

    def _run(
        self,
        argv: list[str],
        *,
        check: bool = True,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """The ONE place herdr is spawned. ``argv`` are the args after ``herdr``.

        With ``check=True`` a non-zero exit raises :class:`HerdrError`; with
        ``check=False`` the completed process is returned as-is so callers can
        apply their own tolerant / server-answered-vs-transport handling. A
        timeout / missing binary always propagates raw (``TimeoutExpired`` /
        ``OSError``) — the seam-honesty guarantee is enforced one level up, in the
        helpers and contract methods, exactly as the tmux base does."""
        proc = subprocess.run(
            ["herdr", *argv],
            capture_output=True,
            text=True,
            encoding=self._ENCODING,
            env=env,
            timeout=HERDR_TIMEOUT_S,
        )
        if check and proc.returncode != 0:
            raise HerdrError(f"herdr {' '.join(argv[:2])} failed: {proc.stderr.strip()}")
        return proc

    def _herdr(self, *args: str) -> str:
        """Strict spawn: non-zero already raises inside :meth:`_run`; a timeout /
        missing binary is trapped here and re-raised as the seam type. Returns the
        stripped stdout."""
        try:
            return self._run(list(args), check=True).stdout.strip()
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise HerdrError(f"herdr {args[0] if args else ''} failed: {exc}") from exc

    def _herdr_json(self, *args: str) -> dict:
        """Strict spawn of a structured herdr command, returning its ``result``
        object. herdr prints a single-line envelope ``{"id":..,"result":{..}}`` by
        default (no ``--json`` flag — passing one errors); a non-JSON body is a
        transport/version fault -> :class:`HerdrError`."""
        out = self._herdr(*args)
        try:
            envelope = json.loads(out)
        except json.JSONDecodeError as exc:
            raise HerdrError(f"herdr {args[0] if args else ''} returned non-JSON: {out!r}") from exc
        result = envelope.get("result") if isinstance(envelope, dict) else None
        return result if isinstance(result, dict) else {}

    # ---- server lifecycle

    def ensure_server(self) -> None:
        """Idempotently guarantee a running, protocol-compatible server. Called
        from the session-creation ops (never from diagnostics like ``list_sessions``
        or ``detect_multiplexers``, which must not spawn a server). No CLI verb
        autostarts the server, so this is mandatory before the first mutating op.

        Warm path (server already up) is a SINGLE ``status`` read — the ``_ensured``
        flag then makes every later mutating op skip the probe entirely."""
        if self._ensured:
            return
        server = self._server()  # one status read: gives both running and protocol
        if not server.get("running"):
            self._start_server()  # polls status until running
            server = self._server()  # re-read for the post-start protocol
        self._check_protocol(server)
        self._ensured = True

    def _status(self) -> dict:
        """Parsed ``herdr status --json`` (rc=0 even when the server is down — a
        safe probe). Raises :class:`HerdrError` on a timeout / missing binary."""
        try:
            proc = self._run(["status", "--json"], check=False)
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise HerdrError(f"herdr status failed: {exc}") from exc
        try:
            data = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise HerdrError(f"herdr status returned non-JSON: {proc.stdout!r}") from exc
        return data if isinstance(data, dict) else {}

    def _server(self) -> dict:
        server = self._status().get("server")
        return server if isinstance(server, dict) else {}

    def _server_running(self) -> bool:
        return bool(self._server().get("running"))

    def _start_server(self) -> None:
        """Spawn a detached ``herdr server`` and poll until it reports running."""
        try:
            subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                ["herdr", "server"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **platform_util.detach_kwargs(),
            )
        except OSError as exc:
            raise HerdrError(f"could not start herdr server: {exc}") from exc
        deadline = time.monotonic() + SERVER_START_TIMEOUT_S
        while time.monotonic() < deadline:
            if self._server_running():
                return
            time.sleep(_SERVER_POLL_S)
        raise HerdrError(f"herdr server did not report running within {SERVER_START_TIMEOUT_S}s")

    def _check_protocol(self, server: dict) -> None:
        """Fail below :data:`SUPPORTED_PROTOCOL`, warn once above. ``server`` is the
        ``.server`` object from a ``status --json`` read (``protocol`` is ``null``
        when down — read the SERVER's, not the binary-local ``api schema``)."""
        proto = server.get("protocol")
        if not isinstance(proto, int):
            return  # server down / unknown — the following op fails loudly on its own
        if proto < SUPPORTED_PROTOCOL:
            raise HerdrError(
                f"herdr server protocol {proto} < required {SUPPORTED_PROTOCOL}; upgrade herdr"
            )
        if proto > SUPPORTED_PROTOCOL and not self._proto_warned:
            self._proto_warned = True
            warnings.warn(
                f"herdr server protocol {proto} newer than tested {SUPPORTED_PROTOCOL}; "
                "proceeding but behavior is unverified",
                stacklevel=2,
            )


def _error_code(proc: subprocess.CompletedProcess[str]) -> str | None:
    """The server-answered error ``code`` from a non-zero result, or None when the
    failure was transport-level (non-JSON stderr == server down / unreachable).

    herdr's error bodies are ``{"error":{"code","message"},...}`` for most verbs
    and a bare ``{"code","message"}`` for ``pane read`` — both handled."""
    try:
        payload = json.loads((proc.stderr or "").strip())
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    err = payload.get("error", payload)
    if isinstance(err, dict) and isinstance(err.get("code"), str):
        return err["code"]
    return None


# ------------------------------------------------------------- pipe_pane poller
#
# herdr has no `pipe-pane`/tee (nor any push stream on the CLI transport), so the
# tmux "append the pane's output to a log file" contract is emulated by polling
# `pane read` and appending a fresh snapshot whenever the pane's content changes.
# Two consumers read that log and MUST see it grow while a session is producing
# output: generic._log_activity_key (mtime,size) re-arms the dev-stall grace, and
# probe scans the log for completion markers. A #85-style no-op would leave the
# log flat and mis-stall a long silent-but-working turn.

_PANE_GONE = object()  # sentinel: `pane read` was answered with pane_not_found


class _PanePoller(threading.Thread):
    """A daemon thread that tees one herdr pane into a log file by polling.

    Every :data:`POLL_INTERVAL_S` it reads the pane's ``recent-unwrapped`` text
    (unwrapped so herdr's narrow default width can't split a marker across lines)
    and appends it to the log **only when the content changed** — content-hash
    gated, because the CLI ``revision`` is unusable (it stays 0 across both
    normal-buffer growth and alt-screen repaints; see the Phase-0 Findings). A
    static screen therefore stops growing the log, so a genuinely idle session
    can still stall; an actively-repainting one keeps re-arming the grace window.

    Retired by :meth:`stop` (kill_window / kill_session) or, on its own, after
    :data:`POLL_NOT_FOUND_LIMIT` consecutive server-answered ``pane_not_found``
    reads — the pane vanished when its process exited (Phase-0 O1). A transport
    hiccup (couldn't ask) is neither growth nor death: the tick is skipped and
    the not-found streak is left intact, exactly as the wait loop treats a probe
    error as "not proof of death"."""

    def __init__(
        self,
        client: _HerdrClient,
        pane_id: str,
        log_file: Path,
        *,
        interval_s: float | None = None,
        not_found_limit: int | None = None,
    ) -> None:
        super().__init__(daemon=True, name=f"herdr-poll-{pane_id}")
        self._client = client
        self._pane_id = pane_id
        self._log_file = Path(log_file)
        # Resolve the cadence from the module globals at construction (not as
        # signature defaults) so a test can shrink POLL_* before starting one.
        self._interval_s = POLL_INTERVAL_S if interval_s is None else interval_s
        self._not_found_limit = POLL_NOT_FOUND_LIMIT if not_found_limit is None else not_found_limit
        self._stop_event = threading.Event()
        self._last_hash: str | None = None

    def stop(self) -> None:
        """Signal the thread to exit. Returns immediately: the event wakes the
        interval sleep at once, but an in-flight ``pane read`` still finishes
        first (the thread is a daemon, so a hung read never blocks shutdown)."""
        self._stop_event.set()

    def prime(self) -> bool:
        """Do one synchronous read before the thread starts. Returns True (and
        logs the first snapshot) if the pane answered with text; False if it is
        already gone or unreachable — the caller then declines to spin up a
        thread, which is how :meth:`HerdrMultiplexer.pipe_pane` stays tolerant of
        a pane that died on launch (probe.py depends on that tolerance)."""
        snapshot = self._read_snapshot()
        if isinstance(snapshot, str):
            self._record(snapshot)
            return True
        return False

    def run(self) -> None:
        not_found = 0
        # wait() returns True the instant stop() fires (-> exit) or False on
        # timeout (-> poll). prime() already captured t0, so the first poll is
        # one interval in — no immediate re-read.
        while not self._stop_event.wait(self._interval_s):
            snapshot = self._read_snapshot()
            if snapshot is _PANE_GONE:
                not_found += 1
                if not_found >= self._not_found_limit:
                    return
            elif isinstance(snapshot, str):
                not_found = 0
                self._record(snapshot)
            # else: transport hiccup — skip this tick, keep the not-found streak.

    def _read_snapshot(self) -> str | object | None:
        """One ``pane read``: the raw pane text (str), :data:`_PANE_GONE` when the
        server answered ``pane_not_found``, or None on a transport failure."""
        try:
            proc = self._client._run(
                ["pane", "read", self._pane_id, "--source", "recent-unwrapped"],
                check=False,
            )
        except (subprocess.SubprocessError, OSError):
            return None
        if proc.returncode == 0:
            return proc.stdout
        if _error_code(proc) == "pane_not_found":
            return _PANE_GONE
        return None  # non-JSON `Error: Os` etc. — unreachable, retry next tick

    def _record(self, text: str) -> None:
        digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
        if digest == self._last_hash:
            return  # unchanged screen: not activity, don't grow the log
        self._last_hash = digest
        if text.strip():  # a blank repaint isn't worth a log line
            self._append(text)

    def _append(self, text: str) -> None:
        # Append-only so the log's inode/size grow monotonically (the activity
        # signal). A write failure must never crash the tee thread.
        if not text.endswith("\n"):
            text += "\n"
        try:
            self._log_file.parent.mkdir(parents=True, exist_ok=True)
            with self._log_file.open("a", encoding="utf-8") as handle:
                handle.write(text)
        except OSError:
            pass


class HerdrMultiplexer(TerminalMultiplexer):
    """herdr backend implementing the full :class:`TerminalMultiplexer` contract.

    The constructor does **no I/O** — ``detect_multiplexers`` instantiates every
    registered backend, so ``available()`` (a plain ``shutil.which``) is the only
    host probe, and the server is only ever touched by the mutating ops via
    :meth:`_HerdrClient.ensure_server`."""

    def __init__(self) -> None:
        self._client = _HerdrClient()
        # Live pipe_pane tees, keyed by native window id (== pane id). Mutated
        # only from the caller's thread (pipe_pane / kill_*); the poller threads
        # never touch it. The lock is defensive hygiene, not a hot path.
        self._pollers: dict[str, _PanePoller] = {}
        self._pollers_lock = threading.Lock()

    # -------------------------------------------------- enumeration helpers

    def _list_workspaces_strict(self) -> list[dict]:
        """Every workspace; raises on ANY failure (transport or server-down).

        This is the liveness-honest enumeration: a successful ``workspace list``
        proves the server answered, so an absent label is honestly "no such
        workspace", never "couldn't ask". ``workspace list`` has no not-found
        case, so any non-zero exit is a transport failure -> raise."""
        try:
            proc = self._client._run(["workspace", "list"], check=False)
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise HerdrError(f"herdr workspace list failed: {exc}") from exc
        if proc.returncode != 0:
            raise HerdrError(f"herdr workspace list failed: {proc.stderr.strip()}")
        return _envelope_items(proc.stdout, "workspaces", strict=True)

    def _list_workspaces_tolerant(self) -> list[dict] | None:
        """Every workspace, or None when the query could not be answered (herdr
        missing / server down / transport failure). Used by the diagnostic
        sentinels, which must never start a server or raise."""
        if not shutil.which("herdr"):
            return None
        try:
            proc = self._client._run(["workspace", "list"], check=False)
        except (subprocess.SubprocessError, OSError):
            return None
        if proc.returncode != 0:
            return None
        return _envelope_items(proc.stdout, "workspaces", strict=False)

    def _list_panes_strict(self) -> list[dict]:
        """Every pane across all workspaces; raises on failure. Never
        ``pane list --workspace <maybe-absent>`` — that RAISES ``workspace_not_found``
        rather than returning [], so we enumerate all panes and filter in-process."""
        try:
            proc = self._client._run(["pane", "list"], check=False)
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise HerdrError(f"herdr pane list failed: {exc}") from exc
        if proc.returncode != 0:
            raise HerdrError(f"herdr pane list failed: {proc.stderr.strip()}")
        return _envelope_items(proc.stdout, "panes", strict=True)

    def _workspace_id(self, label: str, *, strict: bool) -> str | None:
        """First-match resolution of a session name to a workspace id. herdr
        allows DUPLICATE labels (tmux session names are unique; herdr's are not),
        so callers must tolerate more than one and take the first."""
        workspaces = self._list_workspaces_strict() if strict else self._list_workspaces_tolerant()
        if not workspaces:
            return None
        for ws in workspaces:
            if ws.get("label") == label:
                wid = ws.get("workspace_id")
                return wid if isinstance(wid, str) else None
        return None

    # ------------------------------------------------------------ sessions

    def has_session(self, name: str) -> bool:
        # Ensures the server first: this gates session creation, so a fresh run
        # that finds the server down must bring it up to answer authoritatively
        # (and to then create the workspace). A transport failure raises.
        self._client.ensure_server()
        return self._workspace_id(name, strict=True) is not None

    def new_session(
        self, name: str, cwd: Path, cols: int | None = None, lines: int | None = None
    ) -> None:
        # Workspace create auto-spawns a root shell tab, which keeps the workspace
        # alive after task tabs close (tmux window-0 role). Geometry is advisory:
        # herdr has no absolute headless resize, so cols/lines are ignored here.
        self._client.ensure_server()
        # herdr allows duplicate labels; guard so a re-armed/resumed run does not
        # spawn a second workspace with the same name.
        if self._workspace_id(name, strict=True) is not None:
            return
        self._client._herdr_json(
            "workspace", "create", "--label", name, "--cwd", str(cwd), "--no-focus"
        )

    def kill_session(self, name: str) -> None:
        # Best-effort teardown: never start a server just to tear one down, and
        # tolerate the workspace already being gone. Also retire the session's
        # pipe_pane tees so no poller outlives the workspace it was watching.
        wid: str | None = None
        if shutil.which("herdr"):
            try:
                wid = self._workspace_id(name, strict=False)
                if wid is not None:
                    self._client._run(["workspace", "close", wid], check=False)
            except (subprocess.SubprocessError, OSError):
                wid = None
        if wid is not None:
            self._stop_pollers_for_workspace(wid)
        _drop_state("sessions", name)

    def list_sessions(self) -> list[str]:
        # [] when herdr is missing, no server is running, or the query fails —
        # indistinguishable and all "nothing live", as with tmux list-sessions.
        workspaces = self._list_workspaces_tolerant()
        if workspaces is None:
            return []
        return [ws["label"] for ws in workspaces if isinstance(ws.get("label"), str)]

    def session_options(self, option: str) -> dict[str, str]:
        # Map live-session name -> sidecar option value. Also prunes sidecar
        # entries whose workspace is gone (only when liveness is knowable — a
        # None enumeration can't prove a workspace dead, so it prunes nothing).
        workspaces = self._list_workspaces_tolerant()
        if workspaces is None:
            return {}
        labels = {ws["label"] for ws in workspaces if isinstance(ws.get("label"), str)}
        state = _load_state()
        sessions = state["sessions"]
        result = {
            label: sessions[label][option]
            for label in labels
            if label in sessions and option in sessions[label]
        }
        dead = [key for key in sessions if key not in labels]
        if dead:
            for key in dead:
                sessions.pop(key, None)
            try:
                _save_state(state)
            except OSError:
                pass  # best-effort prune
        return result

    def set_session_option(self, name: str, option: str, value: str) -> None:
        # Raiser: a sidecar write failure is this backend's "transport failure".
        try:
            state = _load_state()
            state["sessions"].setdefault(name, {})[option] = value
            _save_state(state)
        except OSError as exc:
            raise HerdrError(f"herdr sidecar write failed: {exc}") from exc

    # ------------------------------------------------------------- windows

    def new_window(
        self, session: str, name: str, cwd: Path, env: dict[str, str], command: str
    ) -> str:
        # A window is a tab with a single shell pane; the native window id we hand
        # back is that tab's root pane id. The command (a shlex-joined argv, per
        # the contract) is re-split and launched via a typed `exec` so process
        # exit == pane close == tab close == tmux-identical death semantics.
        self._client.ensure_server()
        wid = self._workspace_id(session, strict=True)
        if wid is None:
            raise HerdrError(f"herdr workspace for session {session!r} not found")
        argv: list[str] = ["tab", "create", "--workspace", wid, "--label", name, "--cwd", str(cwd)]
        for key, val in env.items():
            argv += ["--env", f"{key}={val}"]
        argv.append("--no-focus")
        result = self._client._herdr_json(*argv)
        pane_id = _root_pane_id(result)
        if pane_id is None:
            raise HerdrError(f"herdr tab create did not return a root pane id: {result!r}")
        self._launch(pane_id, shlex.split(command))
        return pane_id

    def _launch(self, pane_id: str, argv: list[str]) -> None:
        # `pane run` types the line and presses Enter atomically. `exec` replaces
        # the shell so the process IS the pane; POSIX-only by design (the Windows
        # launch path uses agent.start — a Phase-6 follow-up).
        self._client._herdr("pane", "run", pane_id, "exec " + shlex.join(argv))

    def new_parked_window(
        self, session: str, name: str, cwd: Path, argv: list[str], return_opt: str
    ) -> str:
        # The parked/return-to-origin recipe is the TUI-launch surface (PR 2);
        # only tui/launch.py calls this. Fail loudly rather than half-implement.
        raise HerdrError(
            "herdr new_parked_window (TUI launch surface) is not implemented yet — see PR 2"
        )

    def list_window_ids(self, session: str) -> list[str]:
        # The engine's liveness probe: [] means "no windows", and a transport
        # failure must RAISE (never []), so a mere server hang can't read as
        # "window dead -> crashed". Does NOT ensure_server: a liveness check must
        # not resurrect a dead server; an unreachable server is honestly "unknown"
        # -> raise. An absent workspace, though, is a knowable "no windows" -> [].
        wid = self._workspace_id(session, strict=True)
        if wid is None:
            return []
        return [
            pane["pane_id"]
            for pane in self._list_panes_strict()
            if pane.get("workspace_id") == wid and isinstance(pane.get("pane_id"), str)
        ]

    def list_windows(self, session: str, fields: list[str]) -> list[tuple[str, ...]]:
        # Best-effort metadata (sentinel [] on any failure). Fields are looked up
        # directly on each pane dict by herdr-native key; full tmux-field mapping
        # for tui/launch.py is PR 2.
        wid = self._workspace_id(session, strict=False)
        if wid is None:
            return []
        try:
            panes = self._list_panes_strict()
        except MultiplexerError:
            return []
        rows: list[tuple[str, ...]] = []
        for pane in panes:
            if pane.get("workspace_id") != wid:
                continue
            rows.append(tuple(str(pane.get(field, "")) for field in fields))
        return rows

    def window_alive(self, session: str, window_id: str) -> bool:
        # Single-signal liveness: server-answered pane presence. No linger was
        # observed for the exec launch (the pane vanishes on exit), so pane
        # presence alone is authoritative. May raise (transport unknowable).
        return self._pane_counts_as_live(window_id)

    def _pane_counts_as_live(self, pane_id: str) -> bool:
        try:
            proc = self._client._run(["pane", "get", pane_id], check=False)
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise HerdrError(f"herdr pane get failed: {exc}") from exc
        if proc.returncode == 0:
            try:
                data = json.loads(proc.stdout)
            except json.JSONDecodeError as exc:
                raise HerdrError(f"herdr pane get returned non-JSON: {proc.stdout!r}") from exc
            result = data.get("result") if isinstance(data, dict) else None
            return isinstance(result, dict) and "pane" in result
        # Non-zero: a server-answered pane_not_found is honestly "dead"; a
        # non-JSON transport error (server down / bogus socket) is unknowable.
        if _error_code(proc) == "pane_not_found":
            return False
        raise HerdrError(f"herdr pane get failed: {proc.stderr.strip()}")

    def kill_window(self, target: str) -> None:
        # Best-effort: `pane close` cascades the now-empty tab closed; tolerate the
        # pane already being gone (a CLI that crashes on launch races its window
        # down before teardown — probe.py depends on this tolerance).
        self._stop_poller(target)
        if shutil.which("herdr"):
            try:
                self._client._run(["pane", "close", target], check=False)
            except (subprocess.SubprocessError, OSError):
                pass
        _drop_state("windows", target)

    def select_window(self, target: str) -> None:
        # Best-effort focus: resolve the pane's tab and focus it (herdr has
        # `tab focus`, not `pane focus`). Swallow any failure to the no-op sentinel.
        if not shutil.which("herdr"):
            return
        try:
            proc = self._client._run(["pane", "get", target], check=False)
            if proc.returncode != 0:
                return
            pane = json.loads(proc.stdout).get("result", {}).get("pane", {})
            tab_id = pane.get("tab_id")
            if isinstance(tab_id, str) and tab_id:
                self._client._run(["tab", "focus", tab_id], check=False)
        except (subprocess.SubprocessError, OSError, json.JSONDecodeError):
            pass

    def set_window_option(self, target: str, option: str, value: str) -> None:
        # Best-effort sidecar write (sentinel: swallow OSError to a no-op).
        try:
            state = _load_state()
            state["windows"].setdefault(target, {})[option] = value
            _save_state(state)
        except OSError:
            pass

    def unset_window_option(self, target: str, option: str) -> None:
        try:
            state = _load_state()
            opts = state["windows"].get(target)
            if isinstance(opts, dict) and option in opts:
                del opts[option]
                if not opts:
                    del state["windows"][target]
                _save_state(state)
        except OSError:
            pass

    def show_window_option(self, target: str, option: str) -> str:
        # "" reads as "unset" — also the failure sentinel.
        opts = _load_state()["windows"].get(target, {})
        value = opts.get(option, "") if isinstance(opts, dict) else ""
        return value if isinstance(value, str) else ""

    def pipe_pane(self, window_id: str, log_file: Path) -> None:
        # Emulate tmux `pipe-pane` with a per-window polling tee (see _PanePoller).
        # Sentinel contract: never raise. A prime() read that fails means the pane
        # already died on launch (or the server is unreachable) — mirror tmux
        # swallowing that race by simply not starting a tee; the dead window is
        # then reported as a crash by wait_for_completion.
        if not shutil.which("herdr"):
            return None
        poller = _PanePoller(self._client, window_id, Path(log_file))
        if not poller.prime():
            return None
        with self._pollers_lock:
            previous = self._pollers.pop(window_id, None)
            self._pollers[window_id] = poller
        if previous is not None:  # a re-armed window replaces its old tee
            previous.stop()
        poller.start()
        return None

    def _stop_poller(self, window_id: str) -> None:
        with self._pollers_lock:
            poller = self._pollers.pop(window_id, None)
        if poller is not None:
            poller.stop()

    def _stop_pollers_for_workspace(self, workspace_id: str) -> None:
        # A pane id is `<workspace_id>:p<n>`, so the prefix identifies the session's
        # tees without re-querying herdr. (A poller whose pane merely vanished
        # would also self-retire on not-founds; this just frees it promptly.)
        with self._pollers_lock:
            doomed = [pid for pid in self._pollers if pid.split(":", 1)[0] == workspace_id]
            pollers = [self._pollers.pop(pid) for pid in doomed]
        for poller in pollers:
            poller.stop()

    def send_text(self, window_id: str, text: str) -> None:
        # Literal paste, let the TUI ingest it, then submit — the tmux
        # send-text / sleep / Enter ordering, in herdr verbs (Enter is lowercase).
        self._client._herdr("pane", "send-text", window_id, text)
        time.sleep(0.3)
        self._client._herdr("pane", "send-keys", window_id, "enter")

    # ----------------------------------------------------- client / attach

    def attach_target_argv(self, target: str) -> list[str]:
        # Attach a plain terminal to the window's pane via its terminal_id. Strip a
        # tmux-style '=' target prefix a caller might pass; full '=session:window'
        # parsing is PR 2 (here `target` is our native pane id).
        pane_id = target[1:] if target.startswith("=") else target
        try:
            result = self._client._herdr_json("pane", "get", pane_id)
        except HerdrError as exc:
            raise HerdrError(
                f"cannot resolve a herdr terminal for {target!r} to attach: {exc}"
            ) from exc
        terminal_id = result.get("pane", {}).get("terminal_id")
        if not isinstance(terminal_id, str) or not terminal_id:
            raise HerdrError(f"herdr pane {pane_id!r} has no terminal to attach")
        return ["herdr", "terminal", "attach", terminal_id]

    def current_pane_id(self) -> str | None:
        return self._current_from_env("HERDR_PANE_ID")

    def current_window_id(self) -> str | None:
        # Our native window id is the tab's root pane id; inside a single-pane
        # bmad-loop window the current pane IS that root pane.
        return self._current_from_env("HERDR_PANE_ID")

    def current_session(self) -> str | None:
        # Resolve the injected workspace id back to its label (the session name);
        # best-effort, None when not inside herdr or the label can't be resolved.
        if os.environ.get(_HERDR_ENV_MARKER) != "1":
            return None
        ws_id = os.environ.get("HERDR_WORKSPACE_ID")
        if not ws_id:
            return None
        workspaces = self._list_workspaces_tolerant() or []
        for ws in workspaces:
            if ws.get("workspace_id") == ws_id and isinstance(ws.get("label"), str):
                return ws["label"]
        return None

    def _current_from_env(self, key: str) -> str | None:
        if os.environ.get(_HERDR_ENV_MARKER) != "1":
            return None
        value = os.environ.get(key)
        return value or None

    def detach_client(self) -> None:
        # herdr detach is a keybinding, with no CLI verb — documented no-op.
        return None

    def switch_client(self, target: str, last_fallback: bool = False) -> bool:
        # Client switching is the TUI-launch surface (PR 2). Until then no switch
        # happens, so the honest sentinel is False.
        return False

    def available(self) -> bool:
        # A plain PATH probe — NEVER touches the server (detect_multiplexers
        # instantiates every backend and must stay side-effect-free).
        return shutil.which("herdr") is not None

    def version(self) -> str | None:
        if not shutil.which("herdr"):
            return None
        try:
            return self._client._herdr("--version")
        except (MultiplexerError, subprocess.SubprocessError, OSError):
            return None


# --------------------------------------------------------------- parse helpers


def _envelope_items(stdout: str, key: str, *, strict: bool) -> list[dict]:
    """Extract ``result.<key>`` (a list of dicts) from a herdr JSON envelope. In
    strict mode a non-JSON body raises :class:`HerdrError`; otherwise it yields []."""
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        if strict:
            raise HerdrError(f"herdr returned non-JSON: {stdout!r}") from exc
        return []
    result = data.get("result") if isinstance(data, dict) else None
    items = result.get(key) if isinstance(result, dict) else None
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _root_pane_id(result: dict) -> str | None:
    root = result.get("root_pane")
    if isinstance(root, dict) and isinstance(root.get("pane_id"), str):
        return root["pane_id"]
    return None


def _drop_state(section: str, key: str) -> None:
    """Best-effort removal of one sidecar entry (a workspace/window gone). Never
    raises — a teardown/kill must not fail on a sidecar hiccup."""
    try:
        state = _load_state()
        if key in state.get(section, {}):
            del state[section][key]
            _save_state(state)
    except OSError:
        pass
