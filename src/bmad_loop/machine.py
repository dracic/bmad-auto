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

**Success does not imply a document on stdout.** ``diagnose`` and
``probe-adapter`` also take ``--out FILE``; with both flags the document goes to
the file and stdout is legitimately empty at exit 0, with only a confirmation on
stderr. So empty stdout means "no document *here*" — check the exit code to tell
a redirect from a failure, and do not treat rc 0 as a promise of bytes to parse.
That file is held to the same standard as the stream: :func:`write_document` is
the only way it is written, and validates exactly as :func:`emit_document` does.

Every command that takes ``--json`` shares this contract; there is no exception
(#195 removed the last two, ``diagnose`` and ``probe-adapter``, which used to
append a fenced JSON block to a markdown/text report). A command adopting the
flag adopts the whole of it — the contract is the flag's meaning, not a style
the earlier commands happen to follow.

Two ways in, by what the caller already holds:

- :func:`emit` takes a ``dict`` and serializes it — ``status`` and ``list``.
- :func:`emit_document` takes an already-serialized ``str`` — ``diagnose`` and
  ``probe-adapter``, whose renderers return text. Their reasons differ, and only
  one is a constraint: ``probe.render_json`` simply *returns a string*, so there
  is nothing to re-encode, whereas ``diagnostics.render_json`` serializes
  *before* running its leak self-check, making those exact bytes the ones the
  check verified — re-encoding them here would emit bytes nothing verified.

:func:`write_document` is the ``--out`` sibling of :func:`emit_document`, taking
the same already-serialized string to a file instead of stdout.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def emit(doc: dict[str, object]) -> None:
    """The single stdout write in JSON mode."""
    emit_document(json.dumps(doc, indent=2))


def _validated(rendered: str) -> str:
    """Return ``rendered`` unchanged, or raise if it is not a whole JSON document.

    Parse to assert, never to re-serialize: the caller's exact bytes are what gets
    written. ``diagnostics.render_json`` runs its leak self-check *before* handing
    the string over, so re-encoding here would ship bytes nothing checked.
    """
    try:
        json.loads(rendered)
    except json.JSONDecodeError as e:
        raise ValueError(f"refusing to emit a malformed JSON document: {e}") from e
    return rendered


def emit_document(rendered: str) -> None:
    """The single stdout write, for a document that is *already* serialized.

    Verifies well-formedness before writing, then writes the ORIGINAL string —
    never a re-serialization of the parsed result. Half the commands reach stdout
    through here rather than through :func:`emit`, so without this the contract
    would hold for them by convention only; but ``diagnostics.render_json``
    validated these exact bytes with its leak self-check, so emitting anything
    re-derived would ship bytes nothing checked. Parse to assert, print verbatim.

    Raises :class:`ValueError` on a malformed document — a bug in the caller's
    renderer, and far better surfaced as a crash with empty stdout (which the
    contract permits) than as a half-parsable stream a consumer has to diagnose.
    """
    print(_validated(rendered))


def write_document(path: Path, rendered: str) -> None:
    """The single file write in JSON mode — the bytes :func:`emit_document` prints.

    ``--out FILE`` is the other half of the same contract, so it gets the same
    validation and the same trailing newline: ``--json --out FILE`` and
    ``--json > FILE`` produce byte-identical files. Without the parse the file was
    the weaker half — stdout refused a malformed document while the file accepted
    it, which is backwards, since a document written to a file is the one nobody
    eyeballs before feeding it to a parser.

    Raises :class:`ValueError` on a malformed document, before the file is created.
    """
    path.write_text(_validated(rendered) + "\n", encoding="utf-8")


def add_json_flag(parser: argparse.ArgumentParser, what: str) -> None:
    """Register ``--json`` with the standard help text."""
    parser.add_argument(
        "--json",
        action="store_true",
        help=f"emit a stable machine-readable JSON document ({what}) instead of text",
    )
