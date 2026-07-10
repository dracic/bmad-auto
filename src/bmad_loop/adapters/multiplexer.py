"""Terminal-multiplexer seam.

The coding-CLI adapter (:class:`~.base.CodingCLIAdapter`) abstracts *which CLI*
to drive and how its prompts/hooks work. This module abstracts the orthogonal
**transport** axis: how sessions, windows, and panes are created, observed, and
torn down. Today the only backend is tmux (:class:`~.tmux_backend.TmuxMultiplexer`);
the seam exists so a future non-POSIX backend (an eventual native-Windows "psmux")
can slot in without the rest of the codebase shelling out to ``tmux`` directly.

``TerminalMultiplexer`` is the contract a backend author implements. Operation
names mirror today's call sites verbatim so the migration is mechanical. Backends
register themselves through :func:`register_multiplexer` (bundled ones from
:func:`_load_builtin_backends`, out-of-tree ones at import time); the process-wide
backend is selected by registry and returned by :func:`get_multiplexer`.

Selection precedence (issue #87): the ``BMAD_LOOP_MUX_BACKEND`` env var, then the
policy ``[mux] backend`` choice (installed once per CLI invocation via
:func:`configure_multiplexer`), then the platform default when registered and
available, then the first registered backend that matches the platform and is
available, then the historical fallback (first platform match regardless of
availability, bottoming out at tmux). :func:`detect_multiplexers` enumerates the
registry for ``bmad-loop mux`` and the ``validate`` preflight.
"""

from __future__ import annotations

import functools
import os
import sys
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


class MultiplexerError(Exception):
    """A transport-backend operation failed. Backends raise a subclass (e.g.
    :class:`~.tmux_backend.TmuxError`) so call sites can catch the seam-level type
    without importing a backend."""


class TerminalMultiplexer(ABC):
    """Transport backend for agent sessions: sessions, windows, and clients.

    A backend must shell out to (or otherwise drive) exactly one multiplexer and
    nothing else — it is the single place POSIX-shell / tmux knowledge is allowed
    to live. The full surface below is the contract; Phase 1 wired only the subset
    the generic adapter needs, and Phase 2 fills in the rest as ``runs.py``,
    ``tui/launch.py``, ``probe.py``, and ``tui/data.py`` migrate onto it.
    """

    # ----------------------------------------------------------- sessions

    @abstractmethod
    def has_session(self, name: str) -> bool:
        """True iff a session named exactly ``name`` exists."""

    @abstractmethod
    def new_session(
        self, name: str, cwd: Path, cols: int | None = None, lines: int | None = None
    ) -> None:
        """Create a detached session with a single shell window rooted at ``cwd``.
        When ``cols``/``lines`` are given the session is pinned to that geometry
        (agent sessions are observed detached, so their pane size must be fixed);
        omit both for a session whose size is irrelevant (e.g. the control session,
        which is only ever attached, and an attaching client resizes it anyway)."""

    @abstractmethod
    def kill_session(self, name: str) -> None:
        """Kill the named session (tolerant of it already being gone)."""

    @abstractmethod
    def list_sessions(self) -> list[str]:
        """Names of all live sessions."""

    @abstractmethod
    def session_options(self, option: str) -> dict[str, str]:
        """Map of session name -> value of ``option`` across all sessions."""

    @abstractmethod
    def set_session_option(self, name: str, option: str, value: str) -> None:
        """Set a user option on the named session."""

    # ------------------------------------------------------------ windows

    @abstractmethod
    def new_window(
        self, session: str, name: str, cwd: Path, env: dict[str, str], command: str
    ) -> str:
        """Create a window running ``command`` (with ``env`` layered on) in
        ``session``, rooted at ``cwd``. Returns the backend-native window id.

        ``command`` is a POSIX shlex-joined argv string, not a shell line:
        shell-operator behavior (``&&``, ``|``, ...) is backend-defined —
        one backend may hand the string to a shell, another may shlex
        re-split it into literal argv — so callers must not rely on it."""

    @abstractmethod
    def new_parked_window(
        self, session: str, name: str, cwd: Path, argv: list[str], return_opt: str
    ) -> str:
        """Create a window that runs ``argv`` then *parks* — waiting on a key so
        the exit status stays inspectable instead of the window closing the moment
        the process exits — and finally returns an attached client to its origin
        (keyed by the per-window ``return_opt``). Returns the native window id."""

    @abstractmethod
    def list_window_ids(self, session: str) -> list[str]:
        """Native ids of every window in ``session`` (empty if it is gone).

        Raises :class:`MultiplexerError` if the transport itself fails (timeout /
        missing binary): an empty list means "no windows" and must not be
        conflated with "couldn't ask" — this op backs the engine's liveness
        probe (:meth:`window_alive`)."""

    @abstractmethod
    def list_windows(self, session: str, fields: list[str]) -> list[tuple[str, ...]]:
        """One tuple per window in ``session``, each holding the requested
        backend fields in order. Best-effort: returns ``[]`` on a transport
        failure (unlike :meth:`list_window_ids`, this is metadata, not a liveness
        probe, so a sentinel is safe)."""

    @abstractmethod
    def window_alive(self, session: str, window_id: str) -> bool:
        """True iff ``window_id`` is still a window of ``session``.

        May raise :class:`MultiplexerError` when liveness is unknowable (a
        transport timeout / missing binary) — callers must treat that as "don't
        know", not "dead", and must not tear down a possibly-working session on
        it."""

    @abstractmethod
    def kill_window(self, target: str) -> None:
        """Kill the targeted window (tolerant of it already being gone, and a
        no-op on a transport failure)."""

    @abstractmethod
    def select_window(self, target: str) -> None:
        """Make ``target`` the current window of its session (best-effort: a no-op
        on a transport failure)."""

    @abstractmethod
    def set_window_option(self, target: str, option: str, value: str) -> None:
        """Set a user option on the targeted window (best-effort: a no-op on a
        transport failure)."""

    @abstractmethod
    def unset_window_option(self, target: str, option: str) -> None:
        """Remove a user option from the targeted window (so a later read sees it
        as unset, not as an empty value). Best-effort: a no-op on a transport
        failure."""

    @abstractmethod
    def show_window_option(self, target: str, option: str) -> str:
        """Value of a user option on the targeted window ('' if unset, and '' on a
        transport failure)."""

    @abstractmethod
    def pipe_pane(self, window_id: str, log_file: Path) -> None:
        """Tee the window's pane output to ``log_file`` (tolerant of the window
        having already died)."""

    @abstractmethod
    def send_text(self, window_id: str, text: str) -> None:
        """Send ``text`` literally to the window, then submit it (Enter)."""

    # ----------------------------------------------------- client / attach

    @abstractmethod
    def attach_target_argv(self, target: str) -> list[str]:
        """argv that attaches the caller's terminal to ``target``."""

    @abstractmethod
    def current_pane_id(self) -> str | None:
        """Native id of the pane this process runs in, or None when not inside
        the multiplexer."""

    @abstractmethod
    def current_window_id(self) -> str | None:
        """Native id of the window this process runs in, or None when not inside
        the multiplexer."""

    @abstractmethod
    def current_session(self) -> str | None:
        """Name of the session this process runs in, or None when not inside the
        multiplexer."""

    @abstractmethod
    def detach_client(self) -> None:
        """Detach the client viewing the current session (best-effort: a no-op on
        a transport failure)."""

    @abstractmethod
    def switch_client(self, target: str, last_fallback: bool = False) -> bool:
        """Switch the current client to ``target`` (optionally falling back to
        the last client on failure). Returns True iff a switch happened — so a
        transport failure returns False."""

    @abstractmethod
    def available(self) -> bool:
        """True iff this backend can run on the current host (e.g. its binary is
        on PATH)."""

    def version(self) -> str | None:
        """The backend binary's version string, or None when unavailable. Not
        abstract: backends that can't report one inherit this default. Used by
        the diagnostic dump; the implementation owns the binary invocation so it
        stays behind the seam."""
        return None


# (name, matches(platform) -> bool, factory() -> TerminalMultiplexer)
_BACKENDS: list[tuple[str, Callable[[str], bool], Callable[[], TerminalMultiplexer]]] = []
_BUILTINS_LOADED = False
# The policy [mux] backend choice — (name, origin policy path) — installed once
# per CLI invocation by cli._configure_mux via configure_multiplexer. None = auto.
_CONFIGURED: tuple[str, Path | None] | None = None

# Per-platform default backend name, consulted only when that backend is both
# registered AND available on this host. Naming a backend that never registers
# is deliberate and harmless: psmux is an out-of-tree backend today, so on a
# win32 host without it the default simply doesn't apply.
_PLATFORM_DEFAULTS: dict[str, str] = {"win32": "psmux"}
_DEFAULT_BACKEND = "tmux"  # every platform not listed above


def register_multiplexer(
    name: str,
    matches: Callable[[str], bool],
    factory: Callable[[], TerminalMultiplexer],
) -> None:
    """Register a transport backend. ``matches(sys.platform)`` decides automatic
    selection; ``name`` is the key for the ``BMAD_LOOP_MUX_BACKEND`` override.
    Bundled backends register from :func:`_load_builtin_backends`; an out-of-tree
    backend calls this at import time — no core edit required."""
    _BACKENDS.append((name, matches, factory))
    get_multiplexer.cache_clear()  # a later registration must not be shadowed by a cached pick


def _load_builtin_backends() -> None:
    """Register the bundled backends. Idempotent and lazy (called from
    :func:`get_multiplexer`, not at module import) to stay cycle-safe. Registers
    inline rather than via tmux_backend's import side effect so the registry can be
    cleared and re-loaded deterministically (a re-import is a no-op once cached) —
    mirroring ``process_host._load_builtin_hosts``."""
    global _BUILTINS_LOADED
    if _BUILTINS_LOADED:
        return
    from .tmux_backend import TmuxMultiplexer

    # tmux is the default everywhere except native Windows (no tmux binary there);
    # get_multiplexer still falls back to tmux when no backend matches.
    register_multiplexer("tmux", lambda platform: platform != "win32", TmuxMultiplexer)
    _BUILTINS_LOADED = True  # set only after a successful import so a transient failure retries


def configure_multiplexer(name: str | None, *, origin: Path | None = None) -> None:
    """Install the policy ``[mux] backend`` choice (``None``/``""`` = auto).

    Called once per CLI invocation (``cli.main``, after parsing ``--project``)
    before any :func:`get_multiplexer` consumer runs, so probe/diagnose/attach —
    which never load policy themselves — select under the persisted choice too.
    Idempotent: the selection cache is cleared only when the effective value
    changes, so the process-wide singleton identity survives repeated
    same-value configuration."""
    global _CONFIGURED
    new = (name, origin) if name else None
    if new == _CONFIGURED:
        return
    _CONFIGURED = new
    get_multiplexer.cache_clear()


def _known() -> str:
    return ", ".join(name for name, _, _ in _BACKENDS) or "(none registered)"


def _factory_by_name(name: str) -> Callable[[], TerminalMultiplexer] | None:
    for reg_name, _, factory in _BACKENDS:
        if reg_name == name:  # duplicate registrations: first wins, as in the loop below
            return factory
    return None


def _usable(backend: TerminalMultiplexer) -> bool:
    """``available()`` read through a guard: selection must never crash on a
    backend's host probe, so a missing or raising probe reads as unavailable."""
    try:
        return bool(backend.available())
    except Exception:
        return False


def _select() -> tuple[TerminalMultiplexer, str, str]:
    """Resolve the backend by precedence; returns ``(instance, name, reason)``.

    1. ``env`` — ``BMAD_LOOP_MUX_BACKEND`` forces a backend by name
    2. ``policy`` — the ``[mux] backend`` choice installed by
       :func:`configure_multiplexer`, same forced-by-name semantics
    3. ``platform-default`` — this platform's default, iff registered +
       platform match + available
    4. ``first-match`` — first registered backend matching the platform that is
       available (registration order breaks ties among available backends)
    5. ``fallback`` — the historical behavior, preserved so a POSIX host without
       tmux still returns TmuxMultiplexer and ``validate`` reports it
       unavailable: first platform match regardless of availability, then tmux

    A forced name (1-2) bypasses both the platform predicate and ``available()``
    — an explicit choice is trusted, and the backend itself fails loudly if it
    can't run. A forced name matching nothing is a misconfiguration; never
    silently fall back to tmux (wrong/unsafe on a non-POSIX host)."""
    _load_builtin_backends()
    forced = os.environ.get("BMAD_LOOP_MUX_BACKEND")
    if forced:
        factory = _factory_by_name(forced)
        if factory is None:
            raise MultiplexerError(
                f"BMAD_LOOP_MUX_BACKEND={forced!r} matches no registered backend; "
                f"known: {_known()}"
            )
        return factory(), forced, "env"
    if _CONFIGURED is not None:
        name, origin = _CONFIGURED
        factory = _factory_by_name(name)
        if factory is None:
            where = f"[mux] backend = {name!r}" + (f" in {origin}" if origin else "")
            raise MultiplexerError(f"{where} matches no registered backend; known: {_known()}")
        return factory(), name, "policy"

    # Construct each candidate at most once across the remaining steps.
    instances: dict[str, TerminalMultiplexer] = {}

    def _instance(name: str, factory: Callable[[], TerminalMultiplexer]) -> TerminalMultiplexer:
        if name not in instances:
            instances[name] = factory()
        return instances[name]

    default = _PLATFORM_DEFAULTS.get(sys.platform, _DEFAULT_BACKEND)
    for name, matches, factory in _BACKENDS:
        if name != default:
            continue
        # first registration with the default name wins, as everywhere else;
        # it must also claim this platform — a name-colliding backend for
        # another platform doesn't get defaulted onto this one.
        if matches(sys.platform):
            backend = _instance(name, factory)
            if _usable(backend):
                return backend, name, "platform-default"
        break
    for name, matches, factory in _BACKENDS:
        if matches(sys.platform) and _usable(_instance(name, factory)):
            return instances[name], name, "first-match"
    for name, matches, factory in _BACKENDS:
        if matches(sys.platform):
            return _instance(name, factory), name, "fallback"
    from .tmux_backend import TmuxMultiplexer  # bottom fallback, as before

    return TmuxMultiplexer(), "tmux", "fallback"


@functools.lru_cache(maxsize=1)
def get_multiplexer() -> TerminalMultiplexer:
    """Return the process-wide terminal multiplexer, selected by registry.

    Selection precedence lives in :func:`_select` (env var, policy choice,
    platform default, first available match, historical fallback). Cached —
    tests that flip the env var must call ``get_multiplexer.cache_clear()``;
    :func:`register_multiplexer` and :func:`configure_multiplexer` clear it
    themselves."""
    return _select()[0]


@dataclass(frozen=True)
class MuxBackendInfo:
    """One registered backend's detection row, for ``bmad-loop mux`` and the
    ``validate`` preflight."""

    name: str
    matches_platform: bool
    available: bool
    version: str | None
    selected: bool
    reason: str  # "" unless selected: env | policy | platform-default | first-match | fallback


def detect_multiplexers() -> list[MuxBackendInfo]:
    """Probe every registered backend: availability, version, platform match,
    and which one :func:`_select` would pick (with its reason).

    Never raises — this feeds diagnostics, which must work on a misconfigured
    host: a forced unknown name yields rows with no selected mark, and a
    backend whose factory or probes blow up reads as unavailable. Constructs
    every registered backend, so factories must stay cheap, side-effect-free
    constructors (true of the tmux family)."""
    _load_builtin_backends()
    try:
        _, selected_name, reason = _select()
    except MultiplexerError:
        selected_name, reason = None, ""
    rows: list[MuxBackendInfo] = []
    seen: set[str] = set()
    for name, matches, factory in _BACKENDS:
        if name in seen:  # duplicate registrations: only the selectable (first) one is shown
            continue
        seen.add(name)
        try:
            matches_platform = bool(matches(sys.platform))
        except Exception:
            matches_platform = False
        version: str | None = None
        try:
            backend = factory()
            available = _usable(backend)
        except Exception:
            available = False
        else:
            # version() is cosmetic: its failure must not overwrite the
            # already-computed availability (a selected backend would
            # otherwise show a contradictory available=False row).
            try:
                version = backend.version()
            except Exception:
                version = None
        selected = name == selected_name
        rows.append(
            MuxBackendInfo(
                name=name,
                matches_platform=matches_platform,
                available=available,
                version=version,
                selected=selected,
                reason=reason if selected else "",
            )
        )
    return rows
