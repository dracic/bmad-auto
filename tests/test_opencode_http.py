"""OpencodeHttpAdapter: unit tests + fake-binary E2E against a FakeOpencode.

The E2E cases spawn the adapter's real code path end to end: a fake `opencode`
binary (a stdlib-only HTTP server implementing the pinned 1.18.2 surface —
see the ``opencode_http`` module docstring) is launched by the adapter itself via the
conftest ``write_script_launcher`` shim, scripted per scenario through env vars
riding ``spec.env`` (the same channel the engine's BMAD_LOOP_* contract uses).
Everything binds 127.0.0.1; no real opencode binary or network access anywhere.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest
from conftest import write_script_launcher

from bmad_loop.adapters import generic
from bmad_loop.adapters.base import SessionHandle, SessionSpec
from bmad_loop.adapters.generic import NUDGE_TEXT, STALL_NUDGE_TEXT
from bmad_loop.adapters.opencode_http import (
    OpencodeDevAdapter,
    OpencodeHttpAdapter,
    OpencodeServerError,
    _free_port,
    _now_ms,
    _parse_sse_lines,
    _sum_usage,
)
from bmad_loop.adapters.profile import get_profile
from bmad_loop.bmadconfig import ProjectPaths
from bmad_loop.model import TokenUsage
from bmad_loop.policy import LimitsPolicy, Policy
from bmad_loop.process_host import get_process_host

# A pinned example timestamp from the pins file (§4): OpenCode `time.*` values
# are epoch MILLISECONDS. The proof-of-work floor must live in the same unit —
# a ns-vs-ms comparison is always False and silently disables the poll
# fallback, so these tests anchor the unit explicitly.
PINNED_EPOCH_MS = 1_784_218_739_410

# ---------------------------------------------------------------- FakeOpencode
#
# Scenario contract (FAKE_OPENCODE_SCENARIO):
#   completed          prompt -> write result.json -> SSE session.idle
#   nudge-then-complete first prompt idles result-less; the second (the nudge)
#                      writes the result and idles
#   stall              every prompt idles result-less, forever
#   busy-forever       the turn never finishes and never idles
#   die-after-result   prompt -> write result.json -> the server process exits
#   die-no-result      prompt -> the server process exits
#   sse-black-hole     the SSE stream closes right after connecting (every
#                      reconnect); the turn completes result+messages only —
#                      completion is reachable only via the HTTP poll fallback
# FAKE_OPENCODE_START_FAILURES=N makes the first N spawns exit(1) pre-bind.
# FAKE_OPENCODE_SPEC_PATH/_SPEC_TEXT: a bmad-dev-auto-style terminal spec the
# turn writes wherever a scenario writes its result (and at the start of
# busy-forever, for the post-kill rescue). Unset = no-op, like RESULT_PATH.
#
# Recordings under FAKE_OPENCODE_DIR: sessions.jsonl (incl. the Authorization
# header), prompts.jsonl, aborts.jsonl, pid (the server's own pid).

FAKE_OPENCODE = r"""
import json, os, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SCENARIO = os.environ.get("FAKE_OPENCODE_SCENARIO", "completed")
REC_DIR = os.environ["FAKE_OPENCODE_DIR"]
RESULT_PATH = os.environ.get("FAKE_OPENCODE_RESULT_PATH", "")
SPEC_PATH = os.environ.get("FAKE_OPENCODE_SPEC_PATH", "")
SPEC_TEXT = os.environ.get("FAKE_OPENCODE_SPEC_TEXT", "")
START_FAILURES = int(os.environ.get("FAKE_OPENCODE_START_FAILURES", "0"))

argv = sys.argv[1:]
assert argv and argv[0] == "serve", argv
PORT = int(argv[argv.index("--port") + 1])
HOST = argv[argv.index("--hostname") + 1]

if START_FAILURES:
    counter = os.path.join(REC_DIR, "start-count")
    n = 0
    if os.path.exists(counter):
        with open(counter, encoding="utf-8") as fh:
            n = int(fh.read().strip() or 0)
    with open(counter, "w", encoding="utf-8") as fh:
        fh.write(str(n + 1))
    if n < START_FAILURES:
        sys.exit(1)

SESSION_ID = "ses_fake0000000000000000000001"
LOCK = threading.Lock()
STATE = {"busy": False, "completed_ms": 0, "prompts": 0}
EVENTS = []


def now_ms():
    return int(time.time() * 1000)


def record(name, obj):
    with LOCK:
        with open(os.path.join(REC_DIR, name), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(obj) + "\n")


def push(evt):
    with LOCK:
        EVENTS.append(evt)


def pop_events():
    with LOCK:
        out, EVENTS[:] = EVENTS[:], []
    return out


def idle_event():
    return {"type": "session.idle", "properties": {"sessionID": SESSION_ID}}


def write_result():
    if RESULT_PATH:
        with open(RESULT_PATH, "w", encoding="utf-8") as fh:
            json.dump({"ok": True, "workflow": "fake-triage"}, fh)


def write_spec():
    if SPEC_PATH:
        os.makedirs(os.path.dirname(SPEC_PATH), exist_ok=True)
        with open(SPEC_PATH, "w", encoding="utf-8") as fh:
            fh.write(SPEC_TEXT)


def finish_turn():
    with LOCK:
        STATE["completed_ms"] = now_ms()
        STATE["busy"] = False


def run_turn():
    with LOCK:
        STATE["busy"] = True
        n = STATE["prompts"]
    # let the 204 flush before any scripted death tears the connection down
    time.sleep(0.15)
    if SCENARIO == "completed":
        write_result(); write_spec(); finish_turn(); push(idle_event())
    elif SCENARIO == "nudge-then-complete":
        if n >= 2:
            write_result()
        finish_turn(); push(idle_event())
    elif SCENARIO == "stall":
        finish_turn(); push(idle_event())
    elif SCENARIO == "busy-forever":
        write_spec()  # visible only post-kill: the turn never ends or idles
    elif SCENARIO == "die-after-result":
        write_result(); os._exit(0)
    elif SCENARIO == "die-no-result":
        os._exit(0)
    elif SCENARIO == "sse-black-hole":
        write_result(); finish_turn()  # completion visible over HTTP only


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/global/health":
            self._json(200, {"healthy": True, "version": "1.18.2"})
        elif self.path == "/session/status":
            with LOCK:
                busy = STATE["busy"]
            self._json(200, {SESSION_ID: {"type": "busy"}} if busy else {})
        elif self.path.startswith("/session/") and self.path.endswith("/message"):
            with LOCK:
                done = STATE["completed_ms"]
            msgs = []
            if done:
                msgs = [{
                    "info": {
                        "id": "msg_fake1", "role": "assistant",
                        "time": {"created": done - 10, "completed": done},
                        "tokens": {"input": 100, "output": 50, "reasoning": 5,
                                   "cache": {"read": 7, "write": 3}},
                        "cost": 0.01,
                    },
                    "parts": [],
                }]
            self._json(200, msgs)
        elif self.path == "/event":
            self._sse()
        else:
            self._json(404, {"name": "NotFoundError"})

    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

        def emit(evt):
            self.wfile.write(b"data: " + json.dumps(evt).encode("utf-8") + b"\n\n")
            self.wfile.flush()

        try:
            emit({"type": "server.connected", "properties": {}})
            if SCENARIO == "sse-black-hole":
                return  # close the stream: events are never deliverable
            last_beat = time.time()
            while True:
                for evt in pop_events():
                    emit(evt)
                if time.time() - last_beat > 0.2:
                    emit({"type": "server.heartbeat", "properties": {}})
                    last_beat = time.time()
                time.sleep(0.02)
        except OSError:
            return  # client went away

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/session":
            record("sessions.jsonl",
                   {"body": body, "auth": self.headers.get("Authorization", "")})
            self._json(200, {"id": SESSION_ID, "title": body.get("title", ""),
                             "cost": 0,
                             "tokens": {"input": 0, "output": 0, "reasoning": 0,
                                        "cache": {"read": 0, "write": 0}},
                             "time": {"created": now_ms(), "updated": now_ms()}})
        elif self.path.endswith("/prompt_async"):
            record("prompts.jsonl", body)
            with LOCK:
                STATE["prompts"] += 1
            threading.Thread(target=run_turn, daemon=True).start()
            self.send_response(204)
            self.end_headers()
        elif self.path.endswith("/abort"):
            record("aborts.jsonl", {"path": self.path})
            self._json(200, True)
        else:
            self._json(404, {"name": "NotFoundError"})


if sys.platform == "win32":
    # SO_REUSEADDR on Windows allows binding a port already in LISTEN — under
    # xdist two fakes could silently share one port. Bind exclusively instead.
    ThreadingHTTPServer.allow_reuse_address = False
ThreadingHTTPServer.daemon_threads = True
ThreadingHTTPServer.block_on_close = False

server = ThreadingHTTPServer((HOST, PORT), Handler)
with open(os.path.join(REC_DIR, "pid"), "w", encoding="utf-8") as fh:
    fh.write(str(os.getpid()))
server.serve_forever(poll_interval=0.05)
"""


# -------------------------------------------------------------------- helpers


def _policy(**limits) -> Policy:
    return Policy(limits=LimitsPolicy(**limits) if limits else LimitsPolicy())


def _shrink_timing(adapter: OpencodeHttpAdapter) -> OpencodeHttpAdapter:
    """Shrink every cadence for tests; the defaults are minutes of wall clock."""
    adapter.health_timeout_s = 10.0
    adapter.health_poll_s = 0.05
    adapter.reconnect_sleep_s = 0.05
    adapter.silence_threshold_s = 2.0
    adapter.poll_tick_s = 0.05
    adapter.result_grace_s = 0.5
    return adapter


def make_adapter(tmp_path: Path, binary: str = "opencode", **kwargs) -> OpencodeHttpAdapter:
    adapter = OpencodeHttpAdapter(
        run_dir=tmp_path / "run",
        policy=kwargs.pop("policy", _policy()),
        profile=get_profile("opencode"),
        binary=binary,
        **kwargs,
    )
    return _shrink_timing(adapter)


@pytest.fixture
def fake_opencode(tmp_path):
    """The fake `opencode` launcher plus its recording dir."""
    rec = tmp_path / "recordings"
    rec.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    launcher = write_script_launcher(bin_dir, "opencode", FAKE_OPENCODE)
    return launcher, rec


def make_spec(
    tmp_path: Path,
    rec: Path,
    scenario: str,
    task_id: str = "t-1",
    timeout_s: float = 30.0,
    stall_nudges_cap: int | None = 6,
    extra_env: dict | None = None,
) -> SessionSpec:
    env = {
        "FAKE_OPENCODE_SCENARIO": scenario,
        "FAKE_OPENCODE_DIR": str(rec),
        "FAKE_OPENCODE_RESULT_PATH": str(tmp_path / "run" / "tasks" / task_id / "result.json"),
        **(extra_env or {}),
    }
    return SessionSpec(
        task_id=task_id,
        role="triage",
        prompt="/bmad-loop-sweep run it",
        cwd=tmp_path,
        env=env,
        timeout_s=timeout_s,
        stall_nudges_cap=stall_nudges_cap,
    )


def read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def assert_server_gone(rec: Path) -> None:
    """The fake's recorded pid must be dead once the adapter is done with it —
    `opencode serve` survives parent death, so a leak here is a real leak."""
    pid_file = rec / "pid"
    if not pid_file.is_file():
        return  # never got far enough to serve
    pid = int(pid_file.read_text(encoding="utf-8"))
    host = get_process_host()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if not host.is_alive(pid):
            return
        time.sleep(0.05)
    raise AssertionError(f"fake opencode server (pid {pid}) is still alive")


def prompt_texts(rec: Path) -> list[str]:
    return [p["parts"][0]["text"] for p in read_jsonl(rec / "prompts.jsonl")]


# ------------------------------------------------------------------ unit tests


def test_free_port_is_bindable():
    import socket

    port = _free_port()
    assert 0 < port < 65536
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", port))  # still free (racy by design, but just picked)


def test_ms_floor_unit_matches_opencode_timestamps():
    """OpenCode timestamps are epoch ms. The completion floor derives
    from time_ns // 1e6 and MUST be comparable to them: same unit, same epoch."""
    floor = time.time_ns() // 1_000_000
    assert abs(floor - _now_ms()) < 5_000
    # comparable to the pins' pinned example (a real 1.18.2 response value):
    # the floor is later than that 2026 timestamp but within the same magnitude.
    assert PINNED_EPOCH_MS < floor < PINNED_EPOCH_MS * 10


def test_config_content_shapes(tmp_path):
    adapter = make_adapter(tmp_path)
    spec = SessionSpec(task_id="t", role="triage", prompt="p", cwd=tmp_path)
    config = json.loads(adapter._config_content(spec))
    assert config["permission"] == "allow"
    # hermetic-skills recipe (adapter docstring): the project skill tree, absolute
    assert config["skills"]["paths"] == [str(tmp_path / ".claude" / "skills")]
    assert "model" not in config

    spec_model = SessionSpec(
        task_id="t", role="triage", prompt="p", cwd=tmp_path, model="anthropic/claude-x"
    )
    config = json.loads(adapter._config_content(spec_model))
    assert config["model"] == "anthropic/claude-x"


def test_session_env_carries_contract(tmp_path):
    adapter = make_adapter(tmp_path)
    spec = SessionSpec(
        task_id="t", role="triage", prompt="p", cwd=tmp_path, env={"BMAD_LOOP_TASK_ID": "t"}
    )
    env = adapter._session_env(spec, "sekrit")
    assert env["BMAD_LOOP_TASK_ID"] == "t"
    assert env["OPENCODE_SERVER_PASSWORD"] == "sekrit"
    assert env["OPENCODE_DISABLE_EXTERNAL_SKILLS"] == "1"
    json.loads(env["OPENCODE_CONFIG_CONTENT"])  # valid JSON


def test_sse_parser_accumulates_and_tolerates_junk():
    lines = [
        "data: " + json.dumps({"type": "server.connected", "properties": {}}),
        "",
        ": a comment",
        "id: 42",
        "data: not json",
        "",
        "data: " + json.dumps({"type": "session.idle", "properties": {"sessionID": "ses_1"}}),
        "",
    ]
    events = list(_parse_sse_lines(lines))
    assert [e["type"] for e in events] == ["server.connected", "session.idle"]


def test_sse_dispatch_filters_child_sessions(tmp_path):
    """A child/subagent session's idle must not read as the parent's turn-end,
    but its frames DO count as activity (the parent is silent while a child
    streams — the tmux pane-log analogue)."""
    from bmad_loop.adapters.opencode_http import _ServerSession

    adapter = make_adapter(tmp_path)
    sess = _ServerSession(process=None, port=0, base_url="", password="", log_fh=None)
    sess.session_id = "ses_parent"
    adapter._dispatch_sse(sess, {"type": "server.heartbeat", "properties": {}})
    assert sess.activity == 0 and sess.events.empty()
    adapter._dispatch_sse(sess, {"type": "session.idle", "properties": {"sessionID": "ses_child"}})
    assert sess.activity == 1 and sess.events.empty()
    adapter._dispatch_sse(
        sess, {"type": "message.part.updated", "properties": {"sessionID": "ses_child"}}
    )
    assert sess.activity == 2 and sess.events.empty()
    adapter._dispatch_sse(sess, {"type": "session.idle", "properties": {"sessionID": "ses_parent"}})
    assert sess.events.get_nowait() == "idle"
    adapter._dispatch_sse(
        sess, {"type": "session.error", "properties": {"sessionID": "ses_parent"}}
    )
    assert sess.events.get_nowait() == "error"


def test_sum_usage_maps_opencode_tokens():
    messages = [
        {"info": {"role": "user", "tokens": {"input": 999}}},  # not assistant: ignored
        {
            "info": {
                "role": "assistant",
                "tokens": {
                    "input": 100,
                    "output": 50,
                    "reasoning": 5,
                    "cache": {"read": 7, "write": 3},
                },
            }
        },
        {
            "info": {
                "role": "assistant",
                "tokens": {
                    "input": 10,
                    "output": 20,
                    "reasoning": 0,
                    "cache": {"read": 1, "write": 2},
                },
            }
        },
        {"info": {"role": "assistant"}},  # tokenless (e.g. aborted): ignored
    ]
    usage = _sum_usage(messages)
    assert usage == TokenUsage(
        input_tokens=110, output_tokens=75, cache_read_tokens=8, cache_creation_tokens=5
    )
    assert _sum_usage("garbage") == TokenUsage()


def test_missing_httpx_names_the_extra(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "httpx", None)
    with pytest.raises(OpencodeServerError, match=r"bmad-loop\[opencode\]"):
        make_adapter(tmp_path)


def test_missing_binary_is_a_clean_error(tmp_path):
    adapter = make_adapter(tmp_path, binary="definitely-not-a-real-binary-xyz")
    spec = SessionSpec(task_id="t", role="triage", prompt="p", cwd=tmp_path)
    with pytest.raises(OpencodeServerError, match="not found on PATH"):
        adapter.start_session(spec)


def test_kill_unknown_handle_is_a_noop(tmp_path):
    adapter = make_adapter(tmp_path)
    adapter.kill(SessionHandle(task_id="never-started", native_id="ses_x"))


def test_read_usage_returns_stash_by_session_id(tmp_path):
    from bmad_loop.adapters.base import SessionResult

    adapter = make_adapter(tmp_path)
    adapter._usage["ses_1"] = TokenUsage(input_tokens=1)
    assert adapter.read_usage(SessionResult(status="completed", session_id="ses_1")).total == 1
    assert adapter.read_usage(SessionResult(status="completed", session_id="ses_2")) is None
    assert adapter.read_usage(SessionResult(status="completed")) is None


# ------------------------------------------------------------------- E2E tests


def test_e2e_completed(tmp_path, fake_opencode):
    launcher, rec = fake_opencode
    adapter = make_adapter(tmp_path, binary=str(launcher))
    spec = make_spec(tmp_path, rec, "completed")

    result = adapter.run(spec)

    assert result.status == "completed"
    assert result.result_json == {"ok": True, "workflow": "fake-triage"}
    assert result.session_id and result.session_id.startswith("ses_")
    # transcript = the raw messages dump; usage stashed for read_usage
    assert result.transcript_path and Path(result.transcript_path).is_file()
    usage = adapter.read_usage(result)
    assert usage == TokenUsage(
        input_tokens=100, output_tokens=55, cache_read_tokens=7, cache_creation_tokens=3
    )
    # the prompt went through the profile's template
    assert prompt_texts(rec) == ["Use the bmad-loop-sweep skill now: run it"]
    # authenticated transport end to end
    sessions = read_jsonl(rec / "sessions.jsonl")
    assert sessions and sessions[0]["auth"].startswith("Basic ")
    # teardown: registry empty, server dead, log tee landed
    assert adapter._sessions == {}
    assert_server_gone(rec)
    assert (tmp_path / "run" / "logs" / "t-1.log").exists()


def test_e2e_result_less_stop_nudges_then_completes(tmp_path, fake_opencode):
    launcher, rec = fake_opencode
    adapter = make_adapter(tmp_path, binary=str(launcher))
    spec = make_spec(tmp_path, rec, "nudge-then-complete")

    result = adapter.run(spec)

    assert result.status == "completed"
    texts = prompt_texts(rec)
    assert len(texts) == 2
    assert texts[1] == NUDGE_TEXT  # the wake-up carried the result-contract nudge
    assert_server_gone(rec)


def test_e2e_stall_after_nudge_budget(tmp_path, fake_opencode):
    launcher, rec = fake_opencode
    adapter = make_adapter(tmp_path, binary=str(launcher))
    spec = make_spec(tmp_path, rec, "stall")

    result = adapter.run(spec)

    # default budget: 1 stop-nudge; the second result-less idle is a stall
    assert result.status == "stalled"
    assert result.result_json is None
    assert len(prompt_texts(rec)) == 2
    breadcrumbs = read_jsonl(tmp_path / "run" / "tasks" / "t-1" / "resultless-stops.jsonl")
    assert breadcrumbs and all(b["verdict"] == "no-result-json" for b in breadcrumbs)
    assert_server_gone(rec)


def test_e2e_monotonic_stall_cap(tmp_path, fake_opencode):
    """#149 on the HTTP transport: every result-less idle refills the per-silence
    wake budget, so only the monotonic spec.stall_nudges_cap bounds a session
    that keeps ending its turn without a result."""
    launcher, rec = fake_opencode
    adapter = make_adapter(tmp_path, binary=str(launcher), stop_without_result_nudges=0)
    adapter._stall_grace_s = 0.3
    adapter._stall_nudges = 99  # per-silence budget effectively unbounded
    spec = make_spec(tmp_path, rec, "stall", stall_nudges_cap=2, timeout_s=60.0)

    result = adapter.run(spec)

    assert result.status == "stalled"
    assert result.result_json is None
    texts = prompt_texts(rec)
    # initial prompt + exactly cap stall-nudges, then the stall verdict
    assert len(texts) == 3
    assert texts[1] == STALL_NUDGE_TEXT and texts[2] == STALL_NUDGE_TEXT
    assert_server_gone(rec)


def test_e2e_timeout_aborts(tmp_path, fake_opencode):
    launcher, rec = fake_opencode
    adapter = make_adapter(tmp_path, binary=str(launcher))
    spec = make_spec(tmp_path, rec, "busy-forever", timeout_s=1.5)

    result = adapter.run(spec)

    assert result.status == "timeout"
    assert result.result_json is None
    aborts = read_jsonl(rec / "aborts.jsonl")
    assert aborts and "/abort" in aborts[0]["path"]
    assert_server_gone(rec)


def test_e2e_server_death_with_artifact_completes(tmp_path, fake_opencode):
    """Server death ≙ window death: the crash path vouches for a landed
    result.json (accept_result=True), so finished-then-died reads completed."""
    launcher, rec = fake_opencode
    adapter = make_adapter(tmp_path, binary=str(launcher))
    spec = make_spec(tmp_path, rec, "die-after-result")

    result = adapter.run(spec)

    assert result.status == "completed"
    assert result.result_json == {"ok": True, "workflow": "fake-triage"}
    assert_server_gone(rec)


def test_e2e_server_death_without_artifact_crashes(tmp_path, fake_opencode):
    launcher, rec = fake_opencode
    adapter = make_adapter(tmp_path, binary=str(launcher))
    spec = make_spec(tmp_path, rec, "die-no-result")

    result = adapter.run(spec)

    assert result.status == "crashed"
    assert result.result_json is None
    assert_server_gone(rec)


def test_e2e_sse_loss_degrades_to_poll_fallback(tmp_path, fake_opencode):
    """The stream closes after every connect, so session.idle is never
    deliverable; completion must arrive via GET /session/status + the
    message-level proof-of-work — in epoch-ms units (the regression test for
    the ns-vs-ms floor bug, which silently disables this whole path)."""
    launcher, rec = fake_opencode
    adapter = make_adapter(tmp_path, binary=str(launcher))
    spec = make_spec(tmp_path, rec, "sse-black-hole")

    result = adapter.run(spec)

    assert result.status == "completed"
    assert result.result_json == {"ok": True, "workflow": "fake-triage"}
    assert_server_gone(rec)


def test_e2e_spawn_retry_survives_one_early_death(tmp_path, fake_opencode):
    launcher, rec = fake_opencode
    adapter = make_adapter(tmp_path, binary=str(launcher))
    spec = make_spec(tmp_path, rec, "completed", extra_env={"FAKE_OPENCODE_START_FAILURES": "1"})

    result = adapter.run(spec)

    assert result.status == "completed"
    assert int((rec / "start-count").read_text(encoding="utf-8")) == 2
    assert_server_gone(rec)


def test_e2e_spawn_gives_up_after_attempts(tmp_path, fake_opencode):
    launcher, rec = fake_opencode
    adapter = make_adapter(tmp_path, binary=str(launcher))
    spec = make_spec(tmp_path, rec, "completed", extra_env={"FAKE_OPENCODE_START_FAILURES": "99"})

    with pytest.raises(OpencodeServerError, match="after 3 attempts"):
        adapter.run(spec)
    assert adapter._sessions == {}
    # every attempt appended to one log for the task
    assert (tmp_path / "run" / "logs" / "t-1.log").exists()


# --------------------------------------------- OpencodeDevAdapter (Phase 4)
#
# Dev/review sessions run the generic bmad-dev-auto skill, which writes NO
# result.json: the outcome lives in the terminal spec it leaves on disk,
# synthesized via devcontract by _DevSynthesisMixin — the same machinery
# GenericDevAdapter uses, composed over the HTTP transport.

_DONE_SPEC = (
    "---\nstatus: done\nbaseline_revision: abc123\n---\n\n"
    "## Auto Run Result\n\nStatus: done\nImplemented.\n"
)


def make_dev_adapter(
    tmp_path: Path, binary: str = "opencode", **kwargs
) -> tuple[OpencodeDevAdapter, Path]:
    impl = tmp_path / "impl"
    impl.mkdir(exist_ok=True)
    # project root == tmp_path so rebased(spec.cwd=tmp_path) is a no-op: these
    # sessions run in place, where cwd == the project root.
    paths = ProjectPaths(
        project=tmp_path,
        implementation_artifacts=impl,
        planning_artifacts=tmp_path / "plan",
    )
    adapter = OpencodeDevAdapter(
        run_dir=tmp_path / "run",
        policy=kwargs.pop("policy", _policy()),
        profile=get_profile("opencode"),
        binary=binary,
        paths=paths,
        **kwargs,
    )
    return _shrink_timing(adapter), impl


def make_dev_spec(
    tmp_path: Path,
    rec: Path,
    scenario: str,
    spec_path: Path,
    spec_text: str = _DONE_SPEC,
    story_key: str = "3-1",
    task_id: str = "3-1-dev-1",
    timeout_s: float = 30.0,
    extra_env: dict | None = None,
) -> SessionSpec:
    env = {
        "FAKE_OPENCODE_SCENARIO": scenario,
        "FAKE_OPENCODE_DIR": str(rec),
        # Deliberately NO FAKE_OPENCODE_RESULT_PATH: the dev skill writes no
        # result.json, and the dev adapter must never lean on one.
        "FAKE_OPENCODE_SPEC_PATH": str(spec_path),
        "FAKE_OPENCODE_SPEC_TEXT": spec_text,
        "BMAD_LOOP_STORY_KEY": story_key,
        **(extra_env or {}),
    }
    return SessionSpec(
        task_id=task_id,
        role="dev",
        prompt=f"/bmad-dev-auto {story_key}",
        cwd=tmp_path,
        env=env,
        timeout_s=timeout_s,
        stall_nudges_cap=6,
    )


def test_dev_knobs_configured(tmp_path):
    """_configure_dev_knobs over the HTTP knob names: no result-contract stop
    nudges (the skill writes no result.json), stall grace + wake budget from
    policy — same contract as GenericDevAdapter."""
    adapter, _ = make_dev_adapter(
        tmp_path, policy=_policy(dev_stall_grace_s=123, dev_stall_nudges=4)
    )
    assert adapter._stop_nudges == 0
    assert adapter._stall_grace_s == 123.0
    assert adapter._stall_nudges == 4


def test_dev_probe_alive_never_none(tmp_path):
    """The post-kill liveness seam: poll() on the retained Popen handle is
    always answerable (True/False), never the tri-state unknown a tmux probe
    can hit — and a task with no retained process owns nothing alive."""

    class _Proc:
        def __init__(self, rc):
            self._rc = rc

        def poll(self):
            return self._rc

    adapter, _ = make_dev_adapter(tmp_path)
    handle = SessionHandle(task_id="t", native_id="ses_x")
    assert adapter._probe_alive(handle) is False  # never spawned
    adapter._server_procs["t"] = _Proc(None)
    assert adapter._probe_alive(handle) is True  # kill silently failed: keep verdict
    adapter._server_procs["t"] = _Proc(0)
    assert adapter._probe_alive(handle) is False


def test_dev_result_json_ignores_result_file(tmp_path):
    """MRO pin: _DevSynthesisMixin's spec synthesis shadows the core adapter's
    result.json read-back — a stray result.json with no spec on disk is not a
    dev result."""
    adapter, _ = make_dev_adapter(tmp_path)
    task_dir = adapter.tasks_dir / "3-1-dev-1"
    task_dir.mkdir(parents=True)
    (task_dir / "result.json").write_text('{"ok": true}', encoding="utf-8")
    spec = SessionSpec(
        task_id="3-1-dev-1",
        role="dev",
        prompt="/bmad-dev-auto 3-1",
        cwd=tmp_path,
        env={"BMAD_LOOP_STORY_KEY": "3-1"},
    )
    handle = SessionHandle(task_id="3-1-dev-1", native_id="ses_x")
    assert adapter._result_json(handle, spec, wait=False) is None


def test_e2e_dev_synthesizes_terminal_spec(tmp_path, fake_opencode):
    launcher, rec = fake_opencode
    adapter, impl = make_dev_adapter(tmp_path, binary=str(launcher))
    spec = make_dev_spec(tmp_path, rec, "completed", impl / "spec-3-1-foo.md")

    result = adapter.run(spec)

    assert result.status == "completed"
    rj = result.result_json
    assert rj["workflow"] == "auto-dev"
    assert rj["status"] == "done"
    assert rj["baseline_commit"] == "abc123"  # mapped from baseline_revision
    assert rj["story_key"] == "3-1"
    assert rj["escalations"] == []
    assert "post_kill_reconciled" not in rj  # vouched by the idle, not rescued
    # transport parity with the classic path: usage, template, teardown
    assert adapter.read_usage(result) == TokenUsage(
        input_tokens=100, output_tokens=55, cache_read_tokens=7, cache_creation_tokens=3
    )
    assert prompt_texts(rec) == ["Use the bmad-dev-auto skill now: 3-1"]
    assert adapter._sessions == {}
    assert_server_gone(rec)


def test_e2e_dev_stories_mode_resolves_by_id(tmp_path, fake_opencode, monkeypatch):
    """Folder+id dispatch (BMAD_LOOP_SPEC_FOLDER): the story spec is resolved
    at its deterministic id-keyed path — never via the mtime scan."""
    launcher, rec = fake_opencode
    adapter, impl = make_dev_adapter(tmp_path, binary=str(launcher))

    def boom(*a, **k):
        raise AssertionError("stories mode must not call the mtime scan")

    monkeypatch.setattr(generic.devcontract, "find_result_artifact", boom)
    spec = make_dev_spec(
        tmp_path,
        rec,
        "completed",
        tmp_path / "epic" / "stories" / "1-foo.md",
        story_key="1",
        task_id="1-dev-1",
        extra_env={"BMAD_LOOP_SPEC_FOLDER": "epic"},
    )

    result = adapter.run(spec)

    assert result.status == "completed"
    assert result.result_json["status"] == "done"
    assert result.result_json["story_key"] == "1"
    assert_server_gone(rec)


def test_e2e_dev_post_kill_rescue(tmp_path, fake_opencode):
    """#61 over HTTP: the turn wrote its terminal spec but its idle was never
    seen (the fake stays busy forever), so the loop times out — a verdict
    reached under a live server, where the artifact is advisory (#48/#53).
    run()'s kill settles the liveness question; _post_kill_reconcile re-probes
    the now-dead server process and rescues the self-consistent done spec."""
    launcher, rec = fake_opencode
    adapter, impl = make_dev_adapter(tmp_path, binary=str(launcher))
    spec = make_dev_spec(tmp_path, rec, "busy-forever", impl / "spec-3-1-foo.md", timeout_s=1.5)

    result = adapter.run(spec)

    assert result.status == "completed"
    assert result.result_json["status"] == "done"
    assert result.result_json["post_kill_reconciled"] is True
    assert_server_gone(rec)
