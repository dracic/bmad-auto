"""Integration tests for the herdr backend, gated on a live herdr install.

These mirror ``test_generic_tmux.py``'s three live tests — fake-CLI end-to-end,
crash detection, and pipe_pane log growth — but drive them through the
:class:`~bmad_loop.adapters.herdr_backend.HerdrMultiplexer` against a REAL herdr
0.7.3 server. As with the tmux ones a tiny shell script stands in for the CLI
binary (it writes its own SessionStart/result.json/Stop, exactly what a
hook-instrumented session produces), so spawn / env-propagation / hook-signal
waiting / window-death / kill are exercised end-to-end.

Isolation: each test runs under its own ``HERDR_SESSION=bmad-test-<uuid>`` — a
private per-session herdr server + socket (``~/.config/herdr/sessions/<name>/``)
— and forces the herdr backend by name (``BMAD_LOOP_MUX_BACKEND=herdr``); the
sidecar is redirected into ``tmp_path``. A finalizer stops+deletes that session
so no server, workspace, or poller thread outlives the test. The whole module is
skipped when herdr is not installed, and on win32, where the POSIX ``exec``
launch does not apply (the Windows launch path is a PR-6 follow-up).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from bmad_loop.adapters import herdr_backend, multiplexer
from bmad_loop.adapters.base import SessionSpec
from bmad_loop.adapters.generic import GenericTmuxAdapter
from bmad_loop.adapters.profile import get_profile
from bmad_loop.policy import LimitsPolicy, Policy

HAVE_HERDR = sys.platform != "win32" and shutil.which("herdr") is not None
pytestmark = pytest.mark.skipif(not HAVE_HERDR, reason="herdr not available")

# Same hook-instrumented fake as test_generic_tmux.py's FAKE_CLI: the last
# positional arg is the rendered prompt; the run dir + task id ride the pane env
# (herdr `--env`, where tmux used `-e`). Emits SessionStart + result.json + Stop,
# then idles like a live interactive session until its window is killed.
FAKE_CLI = """#!/bin/bash
prompt="${@: -1}"
ts=$(date +%s%N)
mkdir -p "$BMAD_LOOP_RUN_DIR/events" "$BMAD_LOOP_RUN_DIR/tasks/$BMAD_LOOP_TASK_ID"
printf '{"ts": %s, "event": "SessionStart", "task_id": "%s", "session_id": "fake-1"}' \\
    "$ts" "$BMAD_LOOP_TASK_ID" > "$BMAD_LOOP_RUN_DIR/events/$ts-$BMAD_LOOP_TASK_ID-SessionStart.json"
echo "{\\"workflow\\": \\"auto-dev\\", \\"prompt\\": \\"$prompt\\"}" \\
    > "$BMAD_LOOP_RUN_DIR/tasks/$BMAD_LOOP_TASK_ID/result.json"
ts2=$(( ts + 1 ))
printf '{"ts": %s, "event": "Stop", "task_id": "%s", "session_id": "fake-1"}' \\
    "$ts2" "$BMAD_LOOP_TASK_ID" > "$BMAD_LOOP_RUN_DIR/events/$ts2-$BMAD_LOOP_TASK_ID-Stop.json"
sleep 60  # stay alive like an idle interactive session
"""


def _teardown_session(name: str) -> None:
    """Tear down an isolated herdr session (its server + socket + everything under
    it). Best-effort: a never-started session makes both verbs harmless no-ops."""
    for verb in ("stop", "delete"):
        subprocess.run(["herdr", "session", verb, name], capture_output=True, text=True)


@pytest.fixture
def herdr_session(tmp_path, monkeypatch):
    """Isolate the herdr backend onto a private per-test server/socket and force it
    selected by name, with a guaranteed teardown.

    ``HERDR_SESSION`` gives every ``herdr`` subprocess this backend spawns its own
    server + socket, so tests never touch the user's default session or each other
    (safe under xdist — the name is unique per test). ``BMAD_LOOP_MUX_BACKEND=herdr``
    + a cache clear on both ends makes ``get_multiplexer()`` pick herdr regardless
    of host platform (and not leak the pick). The sidecar is redirected out of
    ``~/.bmad-loop``. The finalizer stops+deletes the session even on failure."""
    name = f"bmad-test-{uuid.uuid4().hex[:12]}"
    monkeypatch.setenv("HERDR_SESSION", name)
    monkeypatch.setenv("BMAD_LOOP_MUX_BACKEND", "herdr")
    monkeypatch.setenv("BMAD_LOOP_HERDR_STATE", str(tmp_path / "herdr-state.json"))
    multiplexer.get_multiplexer.cache_clear()
    try:
        yield name
    finally:
        multiplexer.get_multiplexer.cache_clear()
        _teardown_session(name)


def _make_adapter(
    tmp_path, profile_name="claude", binary=None, extra_args=None, **policy_kw
) -> GenericTmuxAdapter:
    # Unique run dir per adapter => unique session name (== workspace label), so
    # concurrent tests on one isolated server never race a teardown vs a create.
    run_dir = tmp_path / f"run-{uuid.uuid4().hex[:8]}"
    policy = Policy(limits=LimitsPolicy(**policy_kw) if policy_kw else LimitsPolicy())
    return GenericTmuxAdapter(
        run_dir=run_dir,
        policy=policy,
        profile=get_profile(profile_name),
        binary=binary,
        extra_args=extra_args,
    )


def _write_fake_cli(tmp_path: Path, body: str = FAKE_CLI) -> Path:
    fake = tmp_path / "fake-cli"
    fake.write_text(body)
    fake.chmod(0o755)
    return fake


@pytest.mark.parametrize("profile_name", ["claude", "codex", "gemini"])
def test_herdr_end_to_end_with_fake_cli(tmp_path, herdr_session, profile_name):
    """Spawn a real herdr workspace/tab/pane running a fake CLI that behaves like a
    hook-instrumented session (SessionStart + result.json + Stop) -> completed. The
    same shape as the tmux end-to-end test, proving the herdr transport carries the
    full launch/env/hook/read-back path for every profile."""
    fake = _write_fake_cli(tmp_path)
    # extra_args=() drops the bypass flags so the rendered prompt is the last argv
    # entry for every profile (claude/codex positional, gemini behind -i).
    adapter = _make_adapter(tmp_path, profile_name=profile_name, binary=str(fake), extra_args=())
    # Guard: we are genuinely exercising herdr, not a silent fall-back to tmux.
    assert isinstance(adapter.mux, herdr_backend.HerdrMultiplexer)
    spec = SessionSpec(
        task_id="t-int-1",
        role="dev",
        prompt="/bmad-dev-auto 1-1-a",
        cwd=tmp_path,
        env={
            "BMAD_LOOP_MODE": "1",
            "BMAD_LOOP_RUN_DIR": str(adapter.run_dir),
            "BMAD_LOOP_TASK_ID": "t-int-1",
        },
        timeout_s=30.0,
    )
    result = adapter.run(spec)

    assert result.status == "completed"
    assert result.result_json["workflow"] == "auto-dev"
    # the fake echoes back the rendered prompt it received via the pane env
    assert result.result_json["prompt"] == adapter.profile.render_prompt(spec.prompt)
    assert result.session_id == "fake-1"
    assert (adapter.tasks_dir / "t-int-1" / "prompt.txt").read_text().strip() == spec.prompt


def test_herdr_crash_detected(tmp_path, herdr_session):
    """A session that dies without writing result.json -> crashed. Pins Phase-0 O1:
    an exec'd process exit vanishes the pane (no linger), so pane-presence liveness
    reports window death authoritatively — the guarantee the SessionEnd-less codex
    path relies on."""
    fake = _write_fake_cli(tmp_path, "#!/bin/bash\nexit 1\n")
    adapter = _make_adapter(
        tmp_path, profile_name="codex", binary=str(fake), stop_without_result_nudges=0
    )
    assert isinstance(adapter.mux, herdr_backend.HerdrMultiplexer)
    spec = SessionSpec(
        task_id="t-crash",
        role="dev",
        prompt="x",
        cwd=tmp_path,
        env={"BMAD_LOOP_RUN_DIR": str(adapter.run_dir), "BMAD_LOOP_TASK_ID": "t-crash"},
        timeout_s=20.0,
    )
    result = adapter.run(spec)

    assert result.status == "crashed"
    assert result.result_json is None


def test_herdr_pipe_pane_log_grows_under_real_pane(tmp_path, herdr_session, monkeypatch):
    """pipe_pane tees a live pane's output into the log by polling (herdr has no
    native pipe-pane). Under a pane emitting changing text the log must GROW and
    the latest snapshot must be discoverable in it — the two consumers a tmux tee
    drives: generic._log_activity_key (dev-stall re-arm on log growth) and probe
    (completion-marker scan). A #85-style no-op tee would leave the log flat and
    mis-stall a long silent-but-working turn. kill_session then retires the tee so
    no poller thread outlives the workspace it watched."""
    # Shrink the poll interval so the growth window is a couple of seconds, not
    # tens. _PanePoller reads this global at construction (pipe_pane time).
    monkeypatch.setattr(herdr_backend, "POLL_INTERVAL_S", 0.25)
    # Prints a fresh non-blank line ~5×/s (blank repaints aren't logged), then
    # idles alive so the pane stays readable while we watch the log.
    script = tmp_path / "grow.sh"
    script.write_text(
        '#!/bin/bash\nfor i in $(seq 1 40); do echo "MARKER line $i"; sleep 0.2; done\nsleep 30\n'
    )
    script.chmod(0o755)

    mux = multiplexer.get_multiplexer()
    assert isinstance(mux, herdr_backend.HerdrMultiplexer)
    session = "bmad-loop-grow"
    mux.new_session(session, tmp_path)
    window_id = mux.new_window(session, "grow", tmp_path, {}, str(script))
    log_file = tmp_path / "grow.log"
    mux.pipe_pane(window_id, log_file)

    # pipe_pane primes the first snapshot synchronously, so the log exists at once;
    # wait for a later poll to append more (growth == the activity signal).
    first_size: int | None = None
    grew = False
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if log_file.exists():
            size = log_file.stat().st_size
            if first_size is None:
                first_size = size
            elif size > first_size:
                grew = True
                break
        time.sleep(0.2)

    assert grew, "pipe_pane poller never grew the log under a producing pane"
    assert "MARKER" in log_file.read_text(encoding="utf-8")  # probe-marker discoverability

    # kill_session stops the session's tees (poller registry emptied), so no
    # daemon keeps polling a vanished pane.
    mux.kill_session(session)
    assert mux._pollers == {}
