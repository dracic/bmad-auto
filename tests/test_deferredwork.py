"""Ledger parsing and editing: deferredwork.py."""

from pathlib import Path

from automator.deferredwork import append_decision, mark_done, open_ids, parse_ledger

LEDGER = """\
# Deferred Work

### DW-1: Harden unicode handling

origin: quick-dev split of spec-3-2-digest.md, 2026-06-01
location: src/strings.py:40
reason: out of scope for the digest story.
status: open

### DW-2: Old closed item

origin: code review of spec-1-1.md, 2026-05-20
location: src/foo.py:10
reason: pre-existing.
status: done 2026-05-25

### DW-3: Needs human decision

origin: code review of spec-2-2.md, 2026-06-05
location: src/retry.py:12
reason: auto-mode: needs human decision
status: open
seen-again: 2026-06-08 (code review of spec-2-3.md)
"""


def write_ledger(tmp_path: Path, text: str = LEDGER) -> Path:
    path = tmp_path / "deferred-work.md"
    path.write_text(text, encoding="utf-8")
    return path


def test_parse_ledger_entries():
    entries = parse_ledger(LEDGER)
    assert [e.id for e in entries] == ["DW-1", "DW-2", "DW-3"]
    assert entries[0].title == "Harden unicode handling"
    assert entries[0].open
    assert not entries[1].open
    assert entries[1].status == "done 2026-05-25"
    assert entries[2].open  # seen-again line does not affect status


def test_open_ids():
    assert open_ids(LEDGER) == {"DW-1", "DW-3"}


def test_parse_tolerates_freeform_sections():
    text = (
        "## Deferred from: code review of story 0.3 (2026-06-08)\n\n"
        "- W1-b — some freeform item with no DW format\n\n" + LEDGER
    )
    assert open_ids(text) == {"DW-1", "DW-3"}


def test_entry_ends_at_next_section_heading():
    text = LEDGER + "\n## Notes\n\nstatus: open\n"
    entries = parse_ledger(text)
    # the stray status line under "## Notes" must not leak into DW-3
    assert entries[-1].id == "DW-3"
    assert "## Notes" not in entries[-1].body


def test_entry_without_status_is_not_open():
    text = "### DW-7: Malformed entry\n\norigin: somewhere\n"
    entries = parse_ledger(text)
    assert entries[0].status == ""
    assert not entries[0].open
    assert open_ids(text) == set()


def test_mark_done_touches_only_target(tmp_path):
    path = write_ledger(tmp_path)
    assert mark_done(path, "DW-1", "2026-06-11", "guards added in src/strings.py")
    text = path.read_text(encoding="utf-8")
    entries = {e.id: e for e in parse_ledger(text)}
    assert entries["DW-1"].status == "done 2026-06-11"
    assert "resolution: guards added in src/strings.py" in entries["DW-1"].body
    assert entries["DW-3"].open
    assert "resolution:" not in entries["DW-3"].body
    assert entries["DW-2"].status == "done 2026-05-25"


def test_mark_done_idempotent(tmp_path):
    path = write_ledger(tmp_path)
    assert mark_done(path, "DW-1", "2026-06-11", "fixed")
    snapshot = path.read_text(encoding="utf-8")
    assert not mark_done(path, "DW-1", "2026-06-12", "fixed again")
    assert path.read_text(encoding="utf-8") == snapshot


def test_mark_done_missing_entry(tmp_path):
    path = write_ledger(tmp_path)
    snapshot = path.read_text(encoding="utf-8")
    assert not mark_done(path, "DW-99", "2026-06-11", "n/a")
    assert path.read_text(encoding="utf-8") == snapshot


def test_append_decision(tmp_path):
    path = write_ledger(tmp_path)
    assert append_decision(path, "DW-3", "2026-06-11", "Keep cap", "frozen intent stands")
    entries = {e.id: e for e in parse_ledger(path.read_text(encoding="utf-8"))}
    assert "decision: 2026-06-11 Keep cap — frozen intent stands" in entries["DW-3"].body
    assert entries["DW-3"].open  # decision alone does not close
    assert "decision:" not in entries["DW-1"].body


def test_append_decision_then_mark_done(tmp_path):
    path = write_ledger(tmp_path)
    assert append_decision(path, "DW-3", "2026-06-11", "Close", "")
    assert mark_done(path, "DW-3", "2026-06-11", "closed by human decision")
    entries = {e.id: e for e in parse_ledger(path.read_text(encoding="utf-8"))}
    assert entries["DW-3"].status == "done 2026-06-11"
    assert "decision: 2026-06-11 Close" in entries["DW-3"].body


def test_append_decision_missing_file(tmp_path):
    assert not append_decision(tmp_path / "nope.md", "DW-1", "2026-06-11", "x", "y")
    assert not mark_done(tmp_path / "nope.md", "DW-1", "2026-06-11", "x")
