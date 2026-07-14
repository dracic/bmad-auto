"""Conformance tests for the herdr terminal-multiplexer backend.

No real herdr server: every test drives :class:`HerdrMultiplexer` against a fake
that stands in for ``subprocess.run`` (the ONE spawn seam), maintaining an
in-memory server (workspaces + panes) and recording every argv so we can pin the
exact CLI each contract method emits. Mirrors ``tests/test_multiplexer.py``'s
``_RecordRun`` + seam-honesty patterns for the herdr (non-tmux-family) backend.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from bmad_loop.adapters import herdr_backend
from bmad_loop.adapters.herdr_backend import HerdrError, HerdrMultiplexer
from bmad_loop.adapters.multiplexer import MultiplexerError

PROJECT_OPTION = "@bmad_project"
RETURN_OPTION = "@bmad_return_pane"


# --------------------------------------------------------------- fake transport


def _flag(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


class FakeHerdr:
    """Stand-in for ``subprocess.run(["herdr", ...])``. Holds an in-memory server
    (a running flag, a protocol number, workspaces, panes), answers the CLI verbs
    this backend uses with herdr's real envelope shapes, and records every argv."""

    def __init__(self, *, running: bool = True, protocol: int = 16) -> None:
        self.running = running
        self.protocol = protocol
        self.workspaces: list[dict] = []  # {"label","workspace_id"}
        self.panes: list[dict] = []  # {"pane_id","workspace_id","tab_id","terminal_id"}
        self.calls: list[list[str]] = []
        # Scripted `pane read` output per pane: a list of successive raw-text
        # screens; each read advances the cursor and sticks on the last entry.
        self.pane_reads: dict[str, list[str]] = {}
        self._pane_read_idx: dict[str, int] = {}
        self._ws_seq = 0
        self._tab_seq: dict[str, int] = {}
        self._pane_seq: dict[str, int] = {}
        self._term_seq = 0

    # ---- helpers a test can call to seed state

    def _new_tab_id(self, wid: str) -> str:
        self._tab_seq[wid] = self._tab_seq.get(wid, 0) + 1
        return f"{wid}:t{self._tab_seq[wid]}"

    def _new_pane(self, wid: str, tab_id: str) -> dict:
        self._pane_seq[wid] = self._pane_seq.get(wid, 0) + 1
        self._term_seq += 1
        pane = {
            "pane_id": f"{wid}:p{self._pane_seq[wid]}",
            "workspace_id": wid,
            "tab_id": tab_id,
            "terminal_id": f"term{self._term_seq}",
        }
        self.panes.append(pane)
        return pane

    def add_workspace(self, label: str) -> str:
        """Create a workspace (with its root shell tab+pane) and return its id."""
        self._ws_seq += 1
        wid = f"w{self._ws_seq}"
        self.workspaces.append({"label": label, "workspace_id": wid})
        self._new_pane(wid, self._new_tab_id(wid))
        return wid

    def set_pane_reads(self, pane_id: str, screens: list[str]) -> None:
        """Script the raw-text screens successive ``pane read`` calls return."""
        self.pane_reads[pane_id] = list(screens)
        self._pane_read_idx[pane_id] = 0

    def _pane_read_out(self, pane_id: str) -> str:
        screens = self.pane_reads.get(pane_id)
        if not screens:
            return ""  # a live-but-unscripted pane reads as a blank screen
        idx = self._pane_read_idx.get(pane_id, 0)
        self._pane_read_idx[pane_id] = idx + 1
        return screens[min(idx, len(screens) - 1)]

    # ---- CompletedProcess builders

    @staticmethod
    def _cp(cmd, rc: int, out: str = "", err: str = "") -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(cmd, rc, stdout=out, stderr=err)

    def _ok(self, cmd, result: dict) -> subprocess.CompletedProcess:
        return self._cp(cmd, 0, out=json.dumps({"id": "cli:1", "result": result}))

    def _server_err(self, cmd, code: str, msg: str) -> subprocess.CompletedProcess:
        # herdr's nested error body (bare {"code",...} only for `pane read`).
        return self._cp(
            cmd, 1, err=json.dumps({"error": {"code": code, "message": msg}, "id": "x"})
        )

    def _bare_err(self, cmd, code: str, msg: str) -> subprocess.CompletedProcess:
        # `pane read` uses herdr's BARE error body (no {"error": ...} envelope).
        return self._cp(cmd, 1, err=json.dumps({"code": code, "message": msg}))

    def _down(self, cmd) -> subprocess.CompletedProcess:
        # server down / bogus socket: a non-JSON transport error.
        return self._cp(cmd, 1, err='Error: Os { code: 2, kind: NotFound, message: "nope" }')

    # ---- subprocess.run replacement

    def __call__(self, cmd, **kwargs) -> subprocess.CompletedProcess:
        assert cmd[0] == "herdr"
        argv = list(cmd[1:])
        self.calls.append(argv)
        return self._dispatch(cmd, argv)

    def _dispatch(self, cmd, argv: list[str]) -> subprocess.CompletedProcess:
        if argv == ["--version"]:
            return self._cp(cmd, 0, out="herdr 0.7.3")
        if argv[:2] == ["status", "--json"]:
            server = {"running": self.running, "protocol": self.protocol if self.running else None}
            return self._cp(cmd, 0, out=json.dumps({"server": server}))
        group = argv[0] if argv else ""
        if not self.running and group in {"workspace", "pane", "tab", "agent", "wait"}:
            return self._down(cmd)
        verb = argv[1] if len(argv) > 1 else ""
        if group == "workspace" and verb == "list":
            return self._ok(cmd, {"workspaces": self.workspaces})
        if group == "workspace" and verb == "create":
            wid = self.add_workspace(_flag(argv, "--label"))
            ws = next(w for w in self.workspaces if w["workspace_id"] == wid)
            root = next(p for p in self.panes if p["workspace_id"] == wid)
            return self._ok(cmd, {"workspace": ws, "root_pane": root})
        if group == "workspace" and verb == "close":
            wid = argv[2]
            self.workspaces = [w for w in self.workspaces if w["workspace_id"] != wid]
            self.panes = [p for p in self.panes if p["workspace_id"] != wid]
            return self._ok(cmd, {"type": "ok"})
        if group == "tab" and verb == "create":
            wid = _flag(argv, "--workspace")
            root = self._new_pane(wid, self._new_tab_id(wid))
            return self._ok(cmd, {"tab": {"tab_id": root["tab_id"]}, "root_pane": root})
        if group == "tab" and verb == "focus":
            return self._ok(cmd, {"type": "ok"})
        if group == "pane" and verb == "list":
            return self._ok(cmd, {"panes": self.panes})
        if group == "pane" and verb == "get":
            pane = next((p for p in self.panes if p["pane_id"] == argv[2]), None)
            if pane is None:
                return self._server_err(cmd, "pane_not_found", f"pane {argv[2]} not found")
            return self._ok(cmd, {"pane": pane})
        if group == "pane" and verb == "close":
            self.panes = [p for p in self.panes if p["pane_id"] != argv[2]]
            return self._ok(cmd, {"type": "ok"})
        if group == "pane" and verb == "read":
            pane_id = argv[2]
            if not any(p["pane_id"] == pane_id for p in self.panes):
                return self._bare_err(cmd, "pane_not_found", f"pane {pane_id} not found")
            # pane read prints RAW TEXT, not a JSON envelope.
            return self._cp(cmd, 0, out=self._pane_read_out(pane_id))
        if group == "pane" and verb in {"run", "send-text", "send-keys"}:
            return self._ok(cmd, {"type": "ok"})
        return self._ok(cmd, {"type": "ok"})  # permissive fallback


@pytest.fixture
def fake(monkeypatch, tmp_path):
    f = FakeHerdr()
    monkeypatch.setattr(herdr_backend.subprocess, "run", f)
    monkeypatch.setattr(herdr_backend.shutil, "which", lambda _name: "/usr/bin/herdr")
    monkeypatch.setenv("BMAD_LOOP_HERDR_STATE", str(tmp_path / "herdr-state.json"))
    monkeypatch.delenv("HERDR_ENV", raising=False)
    return f


def _creates(fake: FakeHerdr, group: str, verb: str) -> list[list[str]]:
    return [c for c in fake.calls if c[:2] == [group, verb]]


# ------------------------------------------------------------- availability


def test_available_and_version(fake):
    mux = HerdrMultiplexer()
    assert mux.available() is True
    assert mux.version() == "herdr 0.7.3"


def test_available_false_without_binary(monkeypatch):
    monkeypatch.setattr(herdr_backend.shutil, "which", lambda _name: None)
    mux = HerdrMultiplexer()
    assert mux.available() is False
    assert mux.version() is None


def test_constructor_does_no_io(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("constructor spawned a subprocess")

    monkeypatch.setattr(herdr_backend.subprocess, "run", boom)
    monkeypatch.setattr(herdr_backend.subprocess, "Popen", boom)
    monkeypatch.setattr(herdr_backend.shutil, "which", lambda _name: "/usr/bin/herdr")
    mux = HerdrMultiplexer()  # must not spawn anything
    assert mux.available() is True  # shutil.which only — never the server


# ------------------------------------------------------------- exact argv


def test_new_session_creates_workspace_argv(fake):
    cwd = str(Path("/work"))  # backend stringifies the Path; '\\work' on win32
    HerdrMultiplexer().new_session("bmad-loop-x", Path("/work"), 220, 50)
    creates = _creates(fake, "workspace", "create")
    assert creates == [
        ["workspace", "create", "--label", "bmad-loop-x", "--cwd", cwd, "--no-focus"]
    ]


def test_new_session_guards_duplicate_label(fake):
    mux = HerdrMultiplexer()
    mux.new_session("bmad-loop-x", Path("/work"))
    mux.new_session("bmad-loop-x", Path("/work"))  # already present -> no second create
    assert len(_creates(fake, "workspace", "create")) == 1
    assert len([w for w in fake.workspaces if w["label"] == "bmad-loop-x"]) == 1


def test_new_window_tab_create_and_exec_launch(fake):
    cwd = str(Path("/work"))  # backend stringifies the Path; '\\work' on win32
    fake.add_workspace("bmad-loop-x")
    mux = HerdrMultiplexer()
    pane_id = mux.new_window("bmad-loop-x", "win", Path("/work"), {"A": "1", "B": "2"}, "echo hi")
    (tab_create,) = _creates(fake, "tab", "create")
    assert tab_create == [
        "tab", "create",
        "--workspace", "w1",
        "--label", "win",
        "--cwd", cwd,
        "--env", "A=1",
        "--env", "B=2",
        "--no-focus",
    ]  # fmt: skip
    (pane_run,) = _creates(fake, "pane", "run")
    assert pane_run == ["pane", "run", pane_id, "exec echo hi"]
    assert pane_id.startswith("w1:")


def test_new_window_missing_workspace_raises(fake):
    with pytest.raises(HerdrError):
        HerdrMultiplexer().new_window("bmad-loop-absent", "win", Path("/w"), {}, "echo hi")


def test_new_window_shlex_resplit_roundtrip(fake):
    # The contract hands new_window a POSIX shlex-joined argv (generic.build_command
    # = " ".join(shlex.quote(a) ...)). The exec launch must re-split it faithfully:
    # shlex.split(command) then shlex.join back, so a tricky arg survives intact.
    import shlex

    argv = ["claude", "-p", "hello world", "--dangerously-skip", "a&&b"]
    command = " ".join(shlex.quote(a) for a in argv)
    fake.add_workspace("bmad-loop-x")
    HerdrMultiplexer().new_window("bmad-loop-x", "win", Path("/w"), {}, command)
    (pane_run,) = _creates(fake, "pane", "run")
    launched = pane_run[3]
    assert launched == "exec " + shlex.join(argv)
    assert shlex.split(launched)[0] == "exec"
    assert shlex.split(launched)[1:] == argv


def test_send_text_literal_then_sleep_then_enter(fake, monkeypatch):
    sleeps: list[tuple[float, int]] = []
    # record the call count at sleep time to pin the ordering: paste, THEN sleep,
    # THEN submit.
    monkeypatch.setattr(herdr_backend.time, "sleep", lambda s: sleeps.append((s, len(fake.calls))))
    HerdrMultiplexer().send_text("w1:p1", "hello world")
    assert fake.calls == [
        ["pane", "send-text", "w1:p1", "hello world"],
        ["pane", "send-keys", "w1:p1", "enter"],
    ]
    assert sleeps == [(0.3, 1)]  # exactly one 0.3s sleep, after the paste (1 call so far)


# ------------------------------------------------------------- liveness honesty


def test_list_window_ids_absent_workspace_returns_empty(fake):
    # server reachable (running), label absent -> honest [] (not "couldn't ask").
    assert HerdrMultiplexer().list_window_ids("bmad-loop-x") == []


def test_list_window_ids_filters_by_workspace(fake):
    fake.add_workspace("bmad-loop-a")
    fake.add_workspace("bmad-loop-b")
    fake.add_workspace("bmad-loop-a")  # duplicate label -> first-match resolution
    mux = HerdrMultiplexer()
    ids_a = mux.list_window_ids("bmad-loop-a")
    ids_b = mux.list_window_ids("bmad-loop-b")
    assert ids_a and all(i.startswith("w1:") for i in ids_a)  # first "a" workspace wins
    assert ids_b and all(i.startswith("w2:") for i in ids_b)
    assert set(ids_a).isdisjoint(ids_b)


def test_list_window_ids_raises_when_server_unreachable(fake):
    fake.running = False
    with pytest.raises(HerdrError):
        HerdrMultiplexer().list_window_ids("bmad-loop-x")


def test_window_alive_pane_get_outcomes(fake):
    fake.add_workspace("bmad-loop-x")
    pane_id = fake.panes[-1]["pane_id"]
    mux = HerdrMultiplexer()
    assert mux.window_alive("bmad-loop-x", pane_id) is True  # present
    assert mux.window_alive("bmad-loop-x", "w9:p9") is False  # pane_not_found (answered)
    fake.running = False
    with pytest.raises(HerdrError):  # Error: Os (unreachable) -> unknowable -> raise
        mux.window_alive("bmad-loop-x", pane_id)


# ----------------------------------------------------------------- seam honesty
#
# No herdr contract method may leak a raw subprocess.TimeoutExpired / OSError:
# raisers re-raise as the seam type, sentinels degrade to their documented value.
# set_session_option is a sidecar write (no subprocess), so its transport-failure
# mode is an OSError on the file, tested separately below — not here.


@pytest.fixture(params=[subprocess.TimeoutExpired(["herdr"], 30), FileNotFoundError("herdr")])
def boom(request, monkeypatch, tmp_path):
    monkeypatch.setattr(herdr_backend.shutil, "which", lambda _name: "/usr/bin/herdr")

    def _boom(*_a, **_k):
        raise request.param

    monkeypatch.setattr(herdr_backend.subprocess, "run", _boom)
    monkeypatch.setenv("BMAD_LOOP_HERDR_STATE", str(tmp_path / "herdr-state.json"))
    monkeypatch.delenv("HERDR_ENV", raising=False)


def test_seam_methods_never_leak_raw_subprocess_error(boom, tmp_path):
    mux = HerdrMultiplexer()
    raisers = [
        lambda: mux.list_window_ids("s"),
        lambda: mux.window_alive("s", "w1:p1"),
        lambda: mux.has_session("s"),
        lambda: mux.new_session("s", tmp_path),
        lambda: mux.new_window("s", "n", tmp_path, {}, "cmd"),
        lambda: mux.send_text("w1:p1", "hi"),
        lambda: mux.new_parked_window("s", "n", tmp_path, ["echo", "hi"], ""),  # always raises
    ]
    for call in raisers:
        with pytest.raises(MultiplexerError) as excinfo:
            call()
        assert not isinstance(excinfo.value, subprocess.SubprocessError)
        assert not isinstance(excinfo.value, OSError)

    # Sentinel returners degrade to the documented value, never raise.
    assert mux.list_windows("s", ["pane_id"]) == []
    assert mux.show_window_option("w1:p1", "opt") == ""
    assert mux.switch_client("s") is False
    assert mux.switch_client("s", last_fallback=True) is False
    assert mux.kill_window("w1:p1") is None
    assert mux.kill_session("s") is None
    assert mux.select_window("w1:p1") is None
    assert mux.set_window_option("w1:p1", "opt", "v") is None
    assert mux.unset_window_option("w1:p1", "opt") is None
    assert mux.detach_client() is None
    assert mux.pipe_pane("w1:p1", tmp_path / "log") is None
    assert mux.list_sessions() == []
    assert mux.session_options("opt") == {}
    assert mux.version() is None
    assert mux.current_pane_id() is None
    assert mux.current_window_id() is None
    assert mux.current_session() is None


# ------------------------------------------------------------------- sidecar


def test_session_option_roundtrip(fake):
    fake.add_workspace("bmad-loop-x")
    mux = HerdrMultiplexer()
    mux.set_session_option("bmad-loop-x", PROJECT_OPTION, "/proj")
    assert mux.session_options(PROJECT_OPTION) == {"bmad-loop-x": "/proj"}


def test_session_option_persists_across_instances(fake):
    HerdrMultiplexer().set_session_option("bmad-loop-x", PROJECT_OPTION, "/proj")
    fake.add_workspace("bmad-loop-x")
    assert HerdrMultiplexer().session_options(PROJECT_OPTION) == {"bmad-loop-x": "/proj"}


def test_session_options_prunes_dead_workspace(fake):
    fake.add_workspace("bmad-loop-x")
    mux = HerdrMultiplexer()
    mux.set_session_option("bmad-loop-x", PROJECT_OPTION, "/proj")
    assert mux.session_options(PROJECT_OPTION) == {"bmad-loop-x": "/proj"}

    fake.workspaces.clear()  # workspace gone out-of-band
    fake.panes.clear()
    assert mux.session_options(PROJECT_OPTION) == {}
    state = json.loads(Path(os.environ["BMAD_LOOP_HERDR_STATE"]).read_text())
    assert "bmad-loop-x" not in state["sessions"]  # sidecar entry pruned


def test_session_options_no_prune_when_unreachable(fake):
    fake.add_workspace("bmad-loop-x")
    mux = HerdrMultiplexer()
    mux.set_session_option("bmad-loop-x", PROJECT_OPTION, "/proj")
    fake.running = False  # can't prove anything dead -> prune nothing, return {}
    assert mux.session_options(PROJECT_OPTION) == {}
    state = json.loads(Path(os.environ["BMAD_LOOP_HERDR_STATE"]).read_text())
    assert state["sessions"]["bmad-loop-x"][PROJECT_OPTION] == "/proj"


def test_sidecar_write_is_atomic_no_temp_left(fake, tmp_path):
    HerdrMultiplexer().set_session_option("bmad-loop-x", PROJECT_OPTION, "/proj")
    target = tmp_path / "herdr-state.json"
    assert json.loads(target.read_text())["sessions"]["bmad-loop-x"][PROJECT_OPTION] == "/proj"
    assert list(tmp_path.glob("herdr-state.json.tmp*")) == []  # no half-written temp


def test_set_session_option_raises_on_write_failure(fake, monkeypatch):
    def boom_replace(_tmp, _target):
        raise OSError("disk full")

    monkeypatch.setattr(herdr_backend.platform_util, "atomic_replace", boom_replace)
    with pytest.raises(HerdrError):
        HerdrMultiplexer().set_session_option("bmad-loop-x", PROJECT_OPTION, "/proj")


def test_window_option_roundtrip(fake):
    mux = HerdrMultiplexer()
    assert mux.show_window_option("w1:p1", RETURN_OPTION) == ""
    mux.set_window_option("w1:p1", RETURN_OPTION, "w1:p2")
    assert mux.show_window_option("w1:p1", RETURN_OPTION) == "w1:p2"
    mux.unset_window_option("w1:p1", RETURN_OPTION)
    assert mux.show_window_option("w1:p1", RETURN_OPTION) == ""


# --------------------------------------------------------------- teardown ops


def test_kill_window_closes_pane_and_prunes_sidecar(fake):
    fake.add_workspace("bmad-loop-x")
    pane = fake.panes[-1]
    mux = HerdrMultiplexer()
    mux.set_window_option(pane["pane_id"], "opt", "v")
    mux.kill_window(pane["pane_id"])
    assert pane not in fake.panes  # pane closed (cascades the empty tab)
    assert mux.show_window_option(pane["pane_id"], "opt") == ""  # sidecar entry gone


def test_kill_session_closes_workspace_and_prunes_sidecar(fake):
    wid = fake.add_workspace("bmad-loop-x")
    mux = HerdrMultiplexer()
    mux.set_session_option("bmad-loop-x", PROJECT_OPTION, "/proj")
    mux.kill_session("bmad-loop-x")
    assert all(w["workspace_id"] != wid for w in fake.workspaces)
    state = json.loads(Path(os.environ["BMAD_LOOP_HERDR_STATE"]).read_text())
    assert "bmad-loop-x" not in state["sessions"]


def test_kill_session_tolerates_missing_binary(monkeypatch, tmp_path):
    monkeypatch.setattr(herdr_backend.shutil, "which", lambda _name: None)
    monkeypatch.setenv("BMAD_LOOP_HERDR_STATE", str(tmp_path / "s.json"))
    assert HerdrMultiplexer().kill_session("bmad-loop-x") is None  # no server op, no raise


def test_list_sessions(fake):
    fake.add_workspace("bmad-loop-a")
    fake.add_workspace("bmad-loop-b")
    assert set(HerdrMultiplexer().list_sessions()) == {"bmad-loop-a", "bmad-loop-b"}


def test_list_sessions_empty_when_server_down(fake):
    fake.running = False
    assert HerdrMultiplexer().list_sessions() == []


# ---------------------------------------------------------- server + protocol


def test_ensure_server_starts_when_down(fake, monkeypatch):
    fake.running = False
    popen_calls: list[tuple] = []

    def fake_popen(argv, **kwargs):
        popen_calls.append((argv, kwargs))
        fake.running = True  # the spawned server comes up
        return object()

    monkeypatch.setattr(herdr_backend.subprocess, "Popen", fake_popen)
    mux = HerdrMultiplexer()
    mux.new_session("bmad-loop-x", Path("/work"))
    assert popen_calls and popen_calls[0][0] == ["herdr", "server"]
    # detached spawn (POSIX start_new_session / win32 creationflags)
    assert "start_new_session" in popen_calls[0][1] or "creationflags" in popen_calls[0][1]
    assert any(w["label"] == "bmad-loop-x" for w in fake.workspaces)


def test_ensure_server_is_probed_once(fake):
    mux = HerdrMultiplexer()
    mux.new_session("bmad-loop-a", Path("/work"))
    mux.new_window("bmad-loop-a", "win", Path("/work"), {}, "echo hi")
    # the server was confirmed up once, not re-probed before every mutating op
    assert len([c for c in fake.calls if c[:2] == ["status", "--json"]]) == 1


def test_protocol_below_supported_raises(fake):
    fake.protocol = herdr_backend.SUPPORTED_PROTOCOL - 1
    with pytest.raises(HerdrError):
        HerdrMultiplexer().new_session("bmad-loop-x", Path("/work"))


def test_protocol_above_supported_warns_but_proceeds(fake):
    fake.protocol = herdr_backend.SUPPORTED_PROTOCOL + 1
    mux = HerdrMultiplexer()
    with pytest.warns(UserWarning):
        mux.new_session("bmad-loop-x", Path("/work"))
    assert _creates(fake, "workspace", "create")  # created despite the warning


# --------------------------------------------------------------- degradations


def test_new_parked_window_raises(fake, tmp_path):
    with pytest.raises(HerdrError):
        HerdrMultiplexer().new_parked_window("s", "n", tmp_path, ["echo", "hi"], "")


def test_pipe_pane_tolerates_dead_pane_and_detach_switch_noop(fake, tmp_path):
    # pipe_pane races a pane that already died on launch: the priming read gets
    # pane_not_found, so no tee thread is spun up (tmux swallows the same race).
    mux = HerdrMultiplexer()
    assert mux.pipe_pane("w9:p9", tmp_path / "log") is None
    assert mux._pollers == {}  # nothing left running
    assert mux.detach_client() is None
    assert mux.switch_client("w1:p1") is False


def test_attach_target_argv_resolves_terminal(fake):
    fake.add_workspace("bmad-loop-x")
    pane = fake.panes[-1]
    mux = HerdrMultiplexer()
    assert mux.attach_target_argv(pane["pane_id"]) == [
        "herdr", "terminal", "attach", pane["terminal_id"],
    ]  # fmt: skip
    # a tmux-style '=' prefix a caller might pass is stripped
    assert mux.attach_target_argv("=" + pane["pane_id"])[-1] == pane["terminal_id"]


def test_attach_target_argv_missing_pane_raises(fake):
    with pytest.raises(HerdrError):
        HerdrMultiplexer().attach_target_argv("w9:p9")


def test_current_accessors_resolve_from_env(fake, monkeypatch):
    wid = fake.add_workspace("bmad-loop-x")
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("HERDR_WORKSPACE_ID", wid)
    monkeypatch.setenv("HERDR_PANE_ID", "w1:p1")
    mux = HerdrMultiplexer()
    assert mux.current_session() == "bmad-loop-x"
    assert mux.current_pane_id() == "w1:p1"
    assert mux.current_window_id() == "w1:p1"


def test_current_accessors_none_outside_herdr(fake, monkeypatch):
    monkeypatch.delenv("HERDR_ENV", raising=False)
    mux = HerdrMultiplexer()
    assert mux.current_session() is None
    assert mux.current_pane_id() is None
    assert mux.current_window_id() is None
