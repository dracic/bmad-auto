"""The machine-readable output contract for CLI ``--json`` modes.

``--json`` in its pure-document form means exactly one JSON object on stdout
and nothing else — no trailers, no fenced blocks, no log lines. Every document
carries an inline integer ``schema_version``; each command owns its own version
constant (e.g. ``cli.STATUS_SCHEMA_VERSION``) so the documents evolve
independently, the same convention as ``diagnostics.SCHEMA_VERSION``. Evolution
is additive-only: new fields may appear, but anything breaking — removing or
renaming a field, changing a type or the meaning of a value — bumps that
command's version.

Errors never produce a partial or error document: the message goes to stderr,
stdout stays empty, and the exit code is nonzero. Consumers may rely on
"stdout is either one complete valid document or empty".

``diagnose --json`` and ``probe-adapter --json`` predate this contract — they
append a fenced JSON block to a markdown/text report instead of emitting a
pure document. Unifying them is tracked in
https://github.com/bmad-code-org/bmad-loop/issues/195.
"""

from __future__ import annotations

import argparse
import json


def emit(doc: dict[str, object]) -> None:
    """The single stdout write in JSON mode."""
    print(json.dumps(doc, indent=2))


def add_json_flag(parser: argparse.ArgumentParser, what: str) -> None:
    """Register ``--json`` with the standard help text."""
    parser.add_argument(
        "--json",
        action="store_true",
        help=f"emit a stable machine-readable JSON document ({what}) instead of text",
    )
