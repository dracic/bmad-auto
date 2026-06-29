"""Cross-platform process primitives.

The pid kill/liveness primitives now live behind the :class:`~automator.process_host.ProcessHost`
seam; ``terminate_pid``/``pid_alive`` remain here as thin back-compat shims that
delegate to it. ``detach_kwargs`` stays a real implementation — it is spawn
configuration, not a process-lifecycle primitive, so it does not belong on the
host. On Linux/macOS — and WSL, which *is* Linux — these preserve today's exact
behavior; the Windows branch degrades gracefully and is not yet exercised.
"""

from __future__ import annotations

import subprocess
import sys

from .process_host import get_process_host


def terminate_pid(pid: int) -> None:
    """Politely terminate ``pid``. Back-compat shim over
    :meth:`ProcessHost.terminate` — prefer ``get_process_host().terminate(pid)``
    in new code."""
    get_process_host().terminate(pid)


def pid_alive(pid: int) -> bool:
    """Read-only liveness check for ``pid``. Back-compat shim over
    :meth:`ProcessHost.is_alive` — prefer ``get_process_host().is_alive(pid)`` in
    new code."""
    return get_process_host().is_alive(pid)


def detach_kwargs() -> dict[str, object]:
    """``Popen`` kwargs that detach a child so it outlives its launcher.

    POSIX uses ``start_new_session``; Windows uses a new process group via
    ``creationflags`` (not exercised yet)."""
    if sys.platform == "win32":
        # portability: start_new_session is POSIX-only; CREATE_NEW_PROCESS_GROUP
        # is the Windows analogue.
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}  # portability: POSIX detach kwarg; Windows branch above
