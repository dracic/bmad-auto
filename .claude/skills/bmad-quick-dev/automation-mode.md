# Automation Mode

You are running unattended inside a `bmad-auto` orchestrator session. No human
is watching this conversation; a deterministic program spawned you, will verify
your artifacts on disk, and will kill this session after your final turn.
These rules override conversational behavior everywhere in this workflow.

## Identity & I/O contract

- `$BMAD_AUTO_RUN_DIR` and `$BMAD_AUTO_TASK_ID` are set in your environment.
- Your **result file** is `$BMAD_AUTO_RUN_DIR/tasks/$BMAD_AUTO_TASK_ID/result.json`.
  Writing it is the LAST action of a successful run (step-auto-finalize does this).
- Your **escalation file** is `$BMAD_AUTO_RUN_DIR/tasks/$BMAD_AUTO_TASK_ID/escalation.json`.
  Write it when you hit a blocker no rule below resolves, then write the result
  file with the escalation included and END YOUR TURN. Schema:

  ```json
  {"escalations": [{"type": "<short-kebab-kind>", "severity": "CRITICAL|PREFERENCE",
                    "detail": "<one or two sentences>"}]}
  ```

  - `CRITICAL` = work cannot proceed safely (missing config, broken repo state,
    contradictory frozen intent). The orchestrator pauses the whole run for a human.
  - `PREFERENCE` = you made a judgment call a human might want to revisit.
    The orchestrator logs it and continues — prefer this when work CAN proceed.

## Behavior rules

1. **Never HALT for input. Never ask the user anything.** Every HALT/ask/menu
   point in the step files resolves via the decision table below. There is no
   user — an unanswered question stalls the run until a timeout kills you.
2. **No greeting, no conversational framing.** Skip the activation greeting.
   Keep narration to one line per step; spend tokens on the work.
3. **The invocation argument IS the intent.** The skill was invoked with a
   sprint-status story key (e.g. `3-2-digest-delivery`). Set `{story_key}` to it
   verbatim, derive `{epic_num}`/`{story_num}` from its leading numeric segments,
   and treat the intent as: implement that story from the epic. Skip the rest of
   the intent-check cascade. Follow step-01's **Epic story path** (epic context
   cache, previous-story continuity) as written.
4. **Always route plan-code-review.** Never one-shot — review runs as a separate
   orchestrated session with fresh context.
5. **Never run step-04-review or step-05-present.** After step-03-implement,
   read fully and follow `./step-auto-finalize.md` (step-03's NEXT section
   handles this). The orchestrator runs review and commits; you do neither.
6. **Never open an editor, never commit, never push, never offer follow-ups.**

## Decision table (replaces HALTs)

| Step file HALT | Automation decision |
|---|---|
| step-01 active-specs menu | If a spec for `{story_key}` already exists: status `draft` → resume into step-02; `ready-for-dev`/`in-progress` → resume into step-03. Ignore unrelated specs. |
| step-01 prior `in-review` spec "ask whether to load" | Load it. |
| step-01 dirty tree / branch mismatch | Escalate `CRITICAL` (`type: dirty-worktree`) — the orchestrator guarantees a clean tree, so this signals external interference. |
| step-01 multi-goal check | Choose **[S] Split**: implement the first goal, append the rest to the deferred-work file per `./deferred-work-format.md`. |
| step-01/02 unclear intent after investigation | Escalate `CRITICAL` (`type: intent-gap`). Do not fantasize requirements. |
| step-02 token budget exceeded | Choose **[S] Split** (defer secondary scope per `./deferred-work-format.md`). |
| step-02 CHECKPOINT 1 | Perform the self-review against the READY FOR DEVELOPMENT standard, fix what it surfaces, then auto-approve: set status `ready-for-dev`, lock the frozen block, continue to step-03. |
| step-03 missing/empty spec precondition | Escalate `CRITICAL` (`type: missing-spec`). |
| Any other HALT or menu | Take the most conservative option that keeps work moving; if none is safe, escalate `CRITICAL`. |

## Sub-agent note

Sub-agent usage is pre-authorized for the whole run — never ask. When sub-agents
are unavailable, do the work inline; never generate prompt files for a human to run.
