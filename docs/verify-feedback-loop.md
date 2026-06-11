# Operator guide: verifying the verify→feedback→fix loop (live E2E)

The 2026-06-11 changes made the pipeline verification-driven: `[verify].commands`
run at DEV_VERIFY (not just after review), and a failure no longer triggers a
blind retry/re-review — the orchestrator writes the failing output to
`.automator/runs/<run-id>/feedback/` and re-invokes quick-dev with
`--feedback <file>`, keeping the working tree. The engine routing is covered by
unit tests (`test_dev_verify_command_failure_routes_feedback_fix` and friends);
what only a live run can prove is that a **real LLM session actually reads the
feedback file and repairs the tree** instead of starting over or stalling.

Sandbox: `/tmp/bmad-e2e` (greeter project). General mechanics — reset
discipline, CONFIG_BASE, first-run trust, monitoring, failure signatures — are
in `verify-codex-gemini.md`; this guide only adds what's specific to the
feedback loop. Total time: ~20 minutes, one dev story with Claude.

---

## Step 1 — sync the updated skills into the sandbox

The sandbox holds COPIES of the skills from before the feedback-mode changes.
Re-copy both changed skills (the two reviewer skills are unchanged):

```bash
cd /tmp/bmad-e2e
git reset --hard <CONFIG_BASE> && git clean -fd
cp -r ~/dev/bmad-automator2/.claude/skills/bmad-quick-dev .claude/skills/
cp -r ~/dev/bmad-automator2/.claude/skills/bmad-code-review .claude/skills/
```

Sanity check the copy took: `grep -q 'Feedback mode' .claude/skills/bmad-quick-dev/automation-mode.md && echo ok`

## Step 2 — seed a verify command that MUST fail on the first pass

Don't rely on the LLM writing buggy code — make the first failure
deterministic. Append to `/tmp/bmad-e2e/.automator/policy.toml`:

```toml
[verify]
commands = [
  "test -f fix-proof || { echo 'FEEDBACK CHECK: create a file named fix-proof (any content) in the repo root to satisfy this gate'; exit 1; }",
]
```

The first dev session has no reason to create `fix-proof`, so DEV_VERIFY fails
with that message. The only way the story converges is if the fix session
**reads the feedback file and follows the instruction inside it** — which is
exactly the property under test. (The spec's own `## Verification` section
won't mention `fix-proof` either, so step-auto-finalize's new self-check can't
satisfy the gate preemptively.)

**Stale-policy check:** a sandbox policy.toml that predates the per-stage
adapter change still has `model_dev` / `model_review` under `[adapter]` —
`bmad-auto validate` rejects these (`adapter.model_dev was removed`). Replace
them with a single `model = ""` (and optional `[adapter.dev]` /
`[adapter.review]` tables). Set `[adapter] name = "claude"` for this guide —
the adapter exercise leaves it on its last CLI. Same reset hazard as the
`extra_args` note in `verify-codex-gemini.md`: resetting to a commit that
predates this fix resurrects the bad keys.

Commit everything (clean-worktree requirement):

```bash
git add -A && git commit -m "sandbox: updated skills + feedback-gate verify command"
git rev-parse --short HEAD   # new CONFIG_BASE for this exercise
```

## Step 3 — run

```bash
bmad-auto validate
bmad-auto run --story 1-1-add-farewell-support
```

Monitor from a second shell (`bmad-auto attach` / `bmad-auto status` /
`tail -f .automator/runs/<run-id>/journal.jsonl`).

## Step 4 — what success looks like

In order, in `journal.jsonl`:

1. `dev-decision` for attempt 1: `action: retry`, reason starts with
   `verify command failed` and contains the `FEEDBACK CHECK` text.
2. `session-start` for `…-dev-2` whose `prompt` contains `--feedback`.
3. `dev-decision` for attempt 2: `action: proceed`.
4. Review cycle(s), then `story-done`.

And on disk:

- `.automator/runs/<run-id>/feedback/1-1-add-farewell-support-1.md` exists and
  contains the failing command + output.
- `fix-proof` exists and is in the orchestrator's story commit (`git show --stat HEAD`).
- The first attempt's implementation survived — the farewell change and the
  spec were NOT reset between attempts (`git log -p` shows one coherent story
  commit, not a from-scratch rewrite; the pane log of dev-2 should show it
  reading the feedback file, not re-planning the spec).

Also confirm the new result contract: `cat .automator/runs/<run-id>/tasks/*-dev-*/result.json`
— dev results should now carry a `verification` array (may be empty if the
spec had no Verification section).

## Step 5 — cleanup

Remove the `[verify]` block (or restore the real one, e.g. `["python -m pytest -q"]`),
delete `fix-proof`, commit. Reset to this commit for future exercises.

## Failure signatures (beyond the table in verify-codex-gemini.md)

| Symptom | Likely cause |
|---|---|
| attempt 2 prompt has `--feedback` but the session re-plans the spec from scratch | skill copy in the sandbox is stale (Step 1 skipped/failed) — feedback mode lives in `automation-mode.md` rule 3 |
| attempt 2 never starts; story defers after attempt 1 | `max_dev_attempts` is 1 in the sandbox policy — needs >= 2 |
| attempt 2 stalls without result.json | fix session escalated or asked a question; check the pane log and `tasks/<id>/escalation.json` |
| story defers with "verify commands kept failing" | fix session completed but didn't create `fix-proof` — read its pane log; this is the actual behavior regression the test exists to catch |

## Optional: realistic variant

Instead of the synthetic gate, seed a real failing acceptance test: write
`tests/test_farewell.py` asserting one exact output format
(`farewell("Bob") == "Goodbye, Bob!"`), set `[verify] commands = ["python -m pytest -q"]`,
and leave the epic's wording ambiguous about punctuation. If the dev session's
first guess mismatches, you get the same loop with organic test output as
feedback. Less deterministic (the model may match on the first try — then the
run simply passes without exercising the loop), but closer to production.

The review-stage variant (clean review whose verify commands fail →
`REVIEW_VERIFY → DEV_RUNNING` fix → re-review) is hard to trigger live on
demand — it requires a review patch that breaks the gates. It shares the
feedback mechanics proven above and is pinned by
`test_review_verify_failure_routes_fix_session_then_rereview`.
