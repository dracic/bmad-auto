"""ClaudeTmuxAdapter tests.

Unit tests need no tmux. The integration test drives a REAL tmux session but
substitutes a tiny shell script for the claude binary: the script writes
result.json and emits hook-style event files itself, exercising spawn / env
propagation / hook-signal waiting / kill end-to-end.
"""

import shutil
import subprocess
import time

import pytest

from automator.adapters.base import SessionSpec
from automator.adapters.claude_tmux import ClaudeTmuxAdapter
from automator.policy import AdapterPolicy, LimitsPolicy, Policy

HAVE_TMUX = shutil.which("tmux") is not None


def make_adapter(tmp_path, claude_bin="claude", **policy_kw) -> ClaudeTmuxAdapter:
    run_dir = tmp_path / "run"
    policy = Policy(
        adapter=AdapterPolicy(extra_args=("--permission-mode", "bypassPermissions")),
        limits=LimitsPolicy(**policy_kw) if policy_kw else LimitsPolicy(),
    )
    return ClaudeTmuxAdapter(run_dir=run_dir, policy=policy, claude_bin=claude_bin)


def make_spec(tmp_path, task_id="1-1-a-dev-1", timeout_s=30.0) -> SessionSpec:
    return SessionSpec(
        task_id=task_id,
        role="dev",
        prompt="/bmad-quick-dev 1-1-a",
        cwd=tmp_path,
        env={"BMAD_AUTO_MODE": "1", "BMAD_AUTO_TASK_ID": task_id},
        model="sonnet",
        timeout_s=timeout_s,
    )


def test_build_command_quotes_prompt(tmp_path):
    adapter = make_adapter(tmp_path)
    cmd = adapter.build_command(make_spec(tmp_path))
    assert cmd.startswith("claude '/bmad-quick-dev 1-1-a' --permission-mode bypassPermissions")
    assert cmd.endswith("--model sonnet")


def test_read_result_variants(tmp_path):
    adapter = make_adapter(tmp_path)
    task_dir = adapter.tasks_dir / "t1"
    task_dir.mkdir(parents=True)
    assert adapter._read_result("t1") is None  # missing
    (task_dir / "result.json").write_text("{broken")
    assert adapter._read_result("t1") is None  # malformed
    (task_dir / "result.json").write_text('["not a dict"]')
    assert adapter._read_result("t1") is None  # wrong shape
    (task_dir / "result.json").write_text('{"clean": true}')
    assert adapter._read_result("t1") == {"clean": True}


def test_await_result_grace_expires_fast(tmp_path):
    adapter = make_adapter(tmp_path)
    (adapter.tasks_dir / "t1").mkdir(parents=True)
    start = time.monotonic()
    assert adapter._await_result("t1", grace_s=0.2) is None
    assert time.monotonic() - start < 5


@pytest.mark.skipif(not HAVE_TMUX, reason="tmux not available")
def test_tmux_end_to_end_with_fake_claude(tmp_path):
    """Spawn a real tmux window running a fake 'claude' that behaves like a
    hook-instrumented session: emits SessionStart + result.json + Stop."""
    fake = tmp_path / "fake-claude"
    fake.write_text(
        """#!/bin/bash
# fake claude: $1 is the prompt; env comes from tmux -e
ts=$(date +%s%N)
mkdir -p "$BMAD_AUTO_RUN_DIR/events" "$BMAD_AUTO_RUN_DIR/tasks/$BMAD_AUTO_TASK_ID"
printf '{"ts": %s, "event": "SessionStart", "task_id": "%s", "session_id": "fake-1"}' \\
    "$ts" "$BMAD_AUTO_TASK_ID" > "$BMAD_AUTO_RUN_DIR/events/$ts-$BMAD_AUTO_TASK_ID-SessionStart.json"
echo "{\\"workflow\\": \\"quick-dev\\", \\"prompt\\": \\"$1\\"}" \\
    > "$BMAD_AUTO_RUN_DIR/tasks/$BMAD_AUTO_TASK_ID/result.json"
ts2=$(( ts + 1 ))
printf '{"ts": %s, "event": "Stop", "task_id": "%s", "session_id": "fake-1"}' \\
    "$ts2" "$BMAD_AUTO_TASK_ID" > "$BMAD_AUTO_RUN_DIR/events/$ts2-$BMAD_AUTO_TASK_ID-Stop.json"
sleep 60  # stay alive like an idle interactive session
"""
    )
    fake.chmod(0o755)

    adapter = make_adapter(tmp_path, claude_bin=str(fake))
    spec_env = {
        "BMAD_AUTO_MODE": "1",
        "BMAD_AUTO_RUN_DIR": str(adapter.run_dir),
        "BMAD_AUTO_TASK_ID": "t-int-1",
    }
    spec = SessionSpec(
        task_id="t-int-1",
        role="dev",
        prompt="/bmad-quick-dev 1-1-a",
        cwd=tmp_path,
        env=spec_env,
        timeout_s=30.0,
    )
    try:
        result = adapter.run(spec)
    finally:
        subprocess.run(
            ["tmux", "kill-session", "-t", adapter.session_name], capture_output=True
        )

    assert result.status == "completed"
    assert result.result_json["workflow"] == "quick-dev"
    assert result.result_json["prompt"] == "/bmad-quick-dev 1-1-a"
    assert result.session_id == "fake-1"
    # prompt recorded for debugging
    assert (adapter.tasks_dir / "t-int-1" / "prompt.txt").read_text().strip() == spec.prompt


@pytest.mark.skipif(not HAVE_TMUX, reason="tmux not available")
def test_tmux_crash_detected(tmp_path):
    """A session that dies without writing result.json -> crashed."""
    fake = tmp_path / "fake-claude"
    fake.write_text("#!/bin/bash\nexit 1\n")
    fake.chmod(0o755)

    adapter = make_adapter(tmp_path, claude_bin=str(fake), stop_without_result_nudges=0)
    spec = SessionSpec(
        task_id="t-crash",
        role="dev",
        prompt="x",
        cwd=tmp_path,
        env={"BMAD_AUTO_RUN_DIR": str(adapter.run_dir), "BMAD_AUTO_TASK_ID": "t-crash"},
        timeout_s=20.0,
    )
    try:
        result = adapter.run(spec)
    finally:
        subprocess.run(
            ["tmux", "kill-session", "-t", adapter.session_name], capture_output=True
        )
    assert result.status == "crashed"
    assert result.result_json is None
