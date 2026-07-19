"""SCAN machinery: transcript discovery, schema inference, registration,
CLI plumbing, and end-to-end scrub-through. No live CLI required."""

import json

import pytest
from conftest import machine_json

from bmad_loop import cli, probe
from bmad_loop.adapters.profile import get_profile

# ----------------------------------------------------------- fixtures / helpers


def _write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


CLAUDE_ROWS = [
    {"type": "assistant", "message": {"usage": {"input_tokens": 100, "output_tokens": 50}}},
    {
        "type": "assistant",
        "message": {
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_read_input_tokens": 2000,
                "cache_creation_input_tokens": 300,
            }
        },
    },
]

CODEX_ROWS = [
    {
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {
                    "input_tokens": 500,
                    "cached_input_tokens": 200,
                    "output_tokens": 60,
                }
            },
        },
    },
]

GEMINI_ROWS = [
    {"$set": {"messages": [{"id": "u1", "type": "user", "content": []}]}},
    {"id": "g1", "type": "gemini", "tokens": {"input": 12273, "output": 45, "cached": 0}},
]


# ----------------------------------------------------------- token inference


def test_infer_claude_candidates_and_self_check(tmp_path):
    path = _write_jsonl(tmp_path / "t.jsonl", CLAUDE_ROWS)
    schema = probe.infer_token_schema("claude-jsonl", path)
    assert "message.usage.input_tokens:int" in schema.token_field_candidates
    assert "message.usage.output_tokens:int" in schema.token_field_candidates
    # parsed self-check matches the real parser
    assert schema.parsed_usage == {
        "input_tokens": 110,
        "output_tokens": 55,
        "cache_read_tokens": 2000,
        "cache_creation_tokens": 300,
    }


def test_infer_codex_nested_candidates(tmp_path):
    path = _write_jsonl(tmp_path / "r.jsonl", CODEX_ROWS)
    schema = probe.infer_token_schema("codex-rollout", path)
    assert "payload.info.total_token_usage.input_tokens:int" in schema.token_field_candidates
    assert schema.parsed_usage["output_tokens"] == 60


def test_infer_gemini_list_paths_collapse(tmp_path):
    path = _write_jsonl(tmp_path / "s.jsonl", GEMINI_ROWS)
    schema = probe.infer_token_schema("gemini-chat", path)
    # list indices collapse to [] so the per-message tokens are one path
    assert any(
        "$set.messages[].tokens.input:int" == p for p in schema.token_field_candidates
    ) or any("tokens.input:int" in p for p in schema.token_field_candidates)


def test_key_paths_carry_types_never_values(tmp_path):
    path = _write_jsonl(tmp_path / "t.jsonl", CLAUDE_ROWS)
    schema = probe.infer_token_schema("claude-jsonl", path)
    blob = "\n".join(schema.key_paths)
    # types appear, raw integer values never do
    assert ":int" in blob
    assert "100" not in blob and "2000" not in blob


def test_infer_with_parser_none_still_finds_candidates(tmp_path):
    path = _write_jsonl(tmp_path / "t.jsonl", CLAUDE_ROWS)
    schema = probe.infer_token_schema("none", path)
    assert schema.parsed_usage is None  # no parser to self-check
    assert "message.usage.input_tokens:int" in schema.token_field_candidates


# Captured from a live agy 1.1.3 turn (probe-adapter antigravity --probe). Shape
# pinned deliberately: agy's transcript carries NO usage block, which is why the
# profile's usage_parser is "none" permanently rather than pending a parser.
ANTIGRAVITY_ROWS = [
    {
        "step_index": 0,
        "source": "USER_EXPLICIT",
        "type": "USER_INPUT",
        "status": "DONE",
        "created_at": "2026-07-17T02:05:24Z",
        "content": "<USER_REQUEST>\nReply with exactly: OK\n</USER_REQUEST>",
    },
    {
        "step_index": 2,
        "source": "MODEL",
        "type": "PLANNER_RESPONSE",
        "status": "DONE",
        "created_at": "2026-07-17T02:05:24Z",
        "content": "OK",
        "thinking": "**Processing User Request**",
    },
]


def test_infer_antigravity_transcript_has_no_token_fields(tmp_path):
    """agy exposes no usage in its transcript — the evidence for usage_parser="none".

    If this ever starts finding candidates, agy began emitting usage and the
    profile should be revisited (see antigravity.toml).
    """
    path = _write_jsonl(tmp_path / "transcript_full.jsonl", ANTIGRAVITY_ROWS)
    schema = probe.infer_token_schema("none", path)
    assert schema.entries_scanned == 2
    assert schema.token_field_candidates == []
    assert "step_index:int" in schema.key_paths


# ----------------------------------------------------------- discovery


def test_discover_picks_newest_mtime(tmp_path):
    base = tmp_path / "sessions"
    old = _write_jsonl(base / "old.jsonl", CLAUDE_ROWS)
    new = _write_jsonl(base / "new.jsonl", CLAUDE_ROWS)
    import os

    os.utime(old, (1, 1))
    os.utime(new, (10_000_000, 10_000_000))
    hints = probe.Hints(session_dir=str(base))
    found = probe.discover_transcript("none", cli="custom", hints=hints)
    assert found.real_path == new
    assert found.multiple is True


def test_discover_transcript_override(tmp_path):
    path = _write_jsonl(tmp_path / "exact.jsonl", CLAUDE_ROWS)
    found = probe.discover_transcript(
        "claude-jsonl", cli="claude", hints=probe.Hints(transcript=str(path))
    )
    assert found.real_path == path
    assert found.location and "exact.jsonl" in found.location


def test_discover_missing_override_notes(tmp_path):
    found = probe.discover_transcript(
        "claude-jsonl", cli="claude", hints=probe.Hints(transcript=str(tmp_path / "nope.jsonl"))
    )
    assert found.real_path is None
    assert "does not exist" in found.note


def test_discover_location_redacts_username(tmp_path, monkeypatch):
    # a munged-cwd dir embedding a username must not survive verbatim
    monkeypatch.setenv("HOME", str(tmp_path))
    path = _write_jsonl(tmp_path / ".secret-home-dir" / "abc-123.jsonl", CLAUDE_ROWS)
    found = probe.discover_transcript("none", cli="x", hints=probe.Hints(transcript=str(path)))
    assert found.location.startswith("~/")
    assert ".secret-home-dir" not in found.location
    assert "abc-123.jsonl" in found.location  # the id-like filename survives


# ----------------------------------------------------------- registration


@pytest.mark.parametrize("dialect_cli", ["claude", "codex", "gemini", "copilot", "antigravity"])
def test_probe_hook_registers_under_native_events(dialect_cli):
    from bmad_loop.install import ANTIGRAVITY_HOOK_GROUP, merge_hooks

    profile = get_profile(dialect_cli)
    registrations = {
        native: f"python3 /tmp/bmad_loop_probe_hook.py {canonical}"
        for native, canonical in profile.hooks.events.items()
    }
    config, changed = merge_hooks({}, registrations, profile.hooks.dialect)
    assert changed
    # agy keys by hook-group name at the top level; the others wrap in "hooks".
    container = (
        config[ANTIGRAVITY_HOOK_GROUP]
        if profile.hooks.dialect == "antigravity-hooks-json"
        else config["hooks"]
    )
    for native in profile.hooks.events:
        assert native in container
    # idempotent re-run
    again, changed2 = merge_hooks(config, registrations, profile.hooks.dialect)
    assert not changed2


@pytest.mark.parametrize("cli", ["claude", "antigravity"])
def test_scan_reports_registered_state(project, cli):
    """Registration detection must follow each dialect's container shape.

    agy keys .agents/hooks.json by hook-GROUP name with no "hooks" wrapper, so
    reading "hooks" there reports a correctly-installed relay as unregistered.
    """
    proj = project.project
    profile = get_profile(cli)
    finding = probe.scan(cli=cli, profile=profile, project=proj, hints=probe.Hints())
    assert finding.registered is False  # nothing installed in the sandbox
    # now install hooks and re-scan
    from bmad_loop.install import install_into

    install_into(proj, clis=(cli,))
    finding2 = probe.scan(cli=cli, profile=profile, project=proj, hints=probe.Hints())
    assert finding2.registered is True


# ----------------------------------------------------------- CLI plumbing


def test_cli_scan_produces_sections(tmp_path, capsys):
    path = _write_jsonl(tmp_path / "t.jsonl", CLAUDE_ROWS)
    rc = cli.main(
        ["probe-adapter", "claude", "--project", str(tmp_path), "--transcript", str(path)]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "# Profile finalize report — claude (scan)" in out
    assert "## Hook payload shape" in out
    assert "## Token usage schema" in out
    assert "message.usage.input_tokens:int" in out


def test_cli_unknown_cli_without_binary_fails(tmp_path, capsys):
    rc = cli.main(["probe-adapter", "no-such-cli", "--project", str(tmp_path)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "FAIL" in err


def test_cli_unknown_cli_with_binary_reduced_report(tmp_path, capsys):
    rc = cli.main(["probe-adapter", "no-such-cli", "--project", str(tmp_path), "--binary", "true"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "reduced report" in out or "no (reduced report)" in out


@pytest.mark.parametrize("json_mode", [False, True], ids=["text", "json"])
def test_cli_hookless_profile_politely_refused(tmp_path, capsys, json_mode):
    """opencode-http (resolved via its `opencode` alias) is HTTP/SSE-driven —
    there is no transcript or hook surface to finalize, so probe-adapter
    explains itself instead of producing a meaningless report. The explanation
    is stderr-only, so JSON mode leaves stdout empty rather than half a
    document."""
    argv = ["probe-adapter", "opencode", "--project", str(tmp_path)]
    rc = cli.main([*argv, "--json"] if json_mode else argv)
    assert rc == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert "hookless" in err
    assert "opencode_http" in err
    assert "FAIL" not in err  # a polite refusal, not an error dump


def test_cli_out_writes_file(tmp_path):
    path = _write_jsonl(tmp_path / "t.jsonl", CLAUDE_ROWS)
    out_file = tmp_path / "report.md"
    rc = cli.main(
        [
            "probe-adapter",
            "claude",
            "--project",
            str(tmp_path),
            "--transcript",
            str(path),
            "--out",
            str(out_file),
        ]
    )
    assert rc == 0
    assert out_file.is_file()
    assert "Profile finalize report" in out_file.read_text()


def _assert_keys_sorted(raw):
    """Every object in the document has its keys in sorted order (`sort_keys=True`).

    Only `object_pairs_hook` can see key ORDER — `json.loads` into a dict keeps
    insertion order, so a plain round-trip comparison can never detect the flag
    being dropped. Sorted output is what makes two probes of the same CLI diffable.

    Takes the RAW string for that reason. Handing it anything re-serialized here
    (`json.dumps(doc, sort_keys=True)`) would sort the keys on the way in and
    assert nothing at all.
    """

    def hook(pairs):
        keys = [k for k, _ in pairs]
        assert keys == sorted(keys), f"object keys not sorted: {keys}"
        return dict(pairs)

    json.loads(raw, object_pairs_hook=hook)


def test_render_json_sorts_keys_at_every_depth(project):
    """Asserted against `render_json`'s own return value, not the CLI's stdout:
    key order lives in the raw bytes, and the shared `--json` CLI helper hands
    back a parsed dict, which has already lost it. This is the renderer's
    property anyway — the CLI just prints what it returns."""
    profile = get_profile("claude")
    finding = probe.scan(
        cli="claude", profile=profile, project=project.project, hints=probe.Hints()
    )
    _assert_keys_sorted(probe.render_json(finding))


def test_cli_json_emits_pure_document(tmp_path, capsys):
    """--json is the whole of stdout: parsing the full stream (not a fence out of
    prose) is itself the assertion that nothing else was printed.

    `err_contains="ok:"` rather than the helper's default empty stderr: probe's
    human trailer is not gone, it moved off stdout, and pinning it here is what
    catches it moving back. The fenced form leaving stdout is covered by the
    helper parsing the whole stream — a fence in prose would not parse.
    """
    path = _write_jsonl(tmp_path / "t.jsonl", CLAUDE_ROWS)
    data = machine_json(
        [
            "probe-adapter",
            "claude",
            "--project",
            str(tmp_path),
            "--transcript",
            str(path),
            "--json",
        ],
        capsys,
        err_contains="ok:",
    )
    assert data["cli"] == "claude" and data["mode"] == "scan"
    # schema_version is a NEW sibling of `version`, which holds the probed CLI's
    # own --version output — the two must not be conflated.
    # `!=` between them would be vacuously true (str vs int never compare equal),
    # so pin the TYPE contract instead — that is what breaks if the two are ever
    # conflated: schema_version is our int, version is the CLI's own string.
    assert data["schema_version"] == probe.SCHEMA_VERSION == 1
    assert isinstance(data["schema_version"], int)
    assert "version" in data and (data["version"] is None or isinstance(data["version"], str))


def test_cli_json_out_writes_document_and_keeps_stdout_empty(tmp_path, capsys):
    path = _write_jsonl(tmp_path / "t.jsonl", CLAUDE_ROWS)
    out_file = tmp_path / "report.json"
    rc = cli.main(
        [
            "probe-adapter",
            "claude",
            "--project",
            str(tmp_path),
            "--transcript",
            str(path),
            "--json",
            "--out",
            str(out_file),
        ]
    )
    assert rc == 0
    out, err = capsys.readouterr()
    assert out == ""  # the document went to the file; stdout stays empty
    assert "document written to" in err  # the noun tracks what was produced
    assert json.loads(out_file.read_text())["cli"] == "claude"


def test_cli_json_unknown_profile_notice_goes_to_stderr(tmp_path, capsys):
    """A reduced report still emits a clean document: the notice is stderr-only."""
    rc = cli.main(
        [
            "probe-adapter",
            "no-such-cli",
            "--project",
            str(tmp_path),
            "--binary",
            "definitely-not-a-real-binary",
            "--json",
        ]
    )
    assert rc == 0
    out, err = capsys.readouterr()
    assert json.loads(out)["cli"] == "no-such-cli"
    assert "unknown profile" in err


# ----------------------------------------------------------- scrub-through


def _seed_pii_scan(tmp_path, monkeypatch):
    """A transcript whose rows carry an email and a home-rooted path."""
    monkeypatch.setenv("HOME", str(tmp_path))
    rows = [
        {
            "type": "assistant",
            "author": "secret@example.com",
            "cwd": f"{tmp_path}/private/project",
            "message": {"usage": {"input_tokens": 7, "output_tokens": 3}},
        }
    ]
    return _write_jsonl(tmp_path / "t.jsonl", rows)


def _pii_argv(tmp_path, path, *extra):
    return [
        "probe-adapter",
        "claude",
        "--project",
        str(tmp_path),
        "--transcript",
        str(path),
        *extra,
    ]


def test_scan_report_contains_no_pii(tmp_path, capsys, monkeypatch):
    """A transcript carrying an email + a home path produces a report with neither.

    Text mode (no --json): the markdown report is what carries the schema table.
    """
    path = _seed_pii_scan(tmp_path, monkeypatch)
    assert cli.main(_pii_argv(tmp_path, path)) == 0
    out = capsys.readouterr().out
    assert "secret@example.com" not in out
    assert "private/project" not in out
    # but the token schema is still there
    assert "message.usage.input_tokens:int" in out


def test_scan_json_document_contains_no_pii(tmp_path, capsys, monkeypatch):
    """The same scrub-through holds for the JSON document, which is rendered by a
    separate path (render_json, not render_markdown) — assert it on its own."""
    path = _seed_pii_scan(tmp_path, monkeypatch)
    assert cli.main(_pii_argv(tmp_path, path, "--json")) == 0
    out = capsys.readouterr().out
    assert "secret@example.com" not in out
    assert "private/project" not in out
    doc = json.loads(out)
    assert "message.usage.input_tokens:int" in doc["tokens"]["key_paths"]
