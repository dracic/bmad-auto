# Deferred Work Format

Canonical entry format for `{implementation_artifacts}/deferred-work.md`.
Used by quick-dev (multi-goal splits, token splits, review defers) and
code-review (defer findings). The file is append-only — never rewrite or
delete existing entries.

## Before appending: dedupe check

Scan the existing file for an entry describing the same issue or goal (same
location and same substance, even if worded differently). If one exists, do
NOT append a duplicate — add a `seen-again:` line to the existing entry
instead:

```markdown
seen-again: 2026-06-12 (code review of spec-3-3-export.md)
```

## Entry format

Number entries sequentially (`DW-1`, `DW-2`, …) by scanning the file for the
highest existing number. One entry per deferred item:

```markdown
### DW-<seq>: <one-line title>

origin: <workflow + artifact + date, e.g. "quick-dev split of spec-3-2-digest.md, 2026-06-12">
location: <file:line or component, or "n/a" for deferred goals>
reason: <why this was deferred rather than done now, one or two sentences>
status: open
```

When a deferred item is later completed, set its `status:` to `done` with the
date (e.g. `status: done 2026-06-20`) — do not delete the entry.
