---
deferred_work_file: '{implementation_artifacts}/deferred-work.md'
spec_file: '' # set below for every route
story_key: '' # set below: the sprint-status key, or dw-<bundle_name> in bundle mode
---

# Step 1: Resolve Task

Determine what was asked, set the I/O paths, and route. No questions — the invocation is authoritative.

## INSTRUCTIONS

1. **Set I/O paths from the environment.**
   - `{result_file}` = `$BMAD_AUTO_RUN_DIR/tasks/$BMAD_AUTO_TASK_ID/result.json`
   - `{escalation_file}` = `$BMAD_AUTO_RUN_DIR/tasks/$BMAD_AUTO_TASK_ID/escalation.json`
   - If `$BMAD_AUTO_RUN_DIR` or `$BMAD_AUTO_TASK_ID` is missing, escalate `CRITICAL` (`type: missing-env`) and end the run — you have nowhere to write your result.

2. **Parse the invocation** into exactly one mode:
   - **story** — `<story-key>`
   - **story + feedback** — `<story-key> --feedback <path>`
   - **bundle** — `--dw-bundle <path>`
   - **bundle + feedback** — `--dw-bundle <path> --feedback <path>`

3. **Resolve the spec target.**
   - **Bundle mode:** read the bundle file FIRST. Set `{bundle_name}`, `{dw_ids}` (the bundle's deferred-work ids), and capture its intent, any human decision, and the verbatim ledger entries as context. Set `{story_key}` = `dw-{bundle_name}`. Set `{spec_file}` = `{implementation_artifacts}/spec-dw-{bundle_name}.md`. Bundles have no epic and no sprint-status entry.
   - **Story mode:** set `{story_key}` to the invocation argument verbatim. Derive `{epic_num}` and `{story_num}` from its leading numeric segments (exact numeric equality per segment, so `1-1` never matches `1-10`). Set `{spec_file}` = `{implementation_artifacts}/spec-{story_key}.md`.

4. **Read feedback first (repair sessions).** If a `--feedback <path>` was passed, read that file now — it contains the failing command and its output. This is a repair session: the working tree still holds the previous attempt's changes, and the spec for this task already exists. Do **not** regenerate the spec, and do **not** change its status if it is already `done`. If the tree was reset and the spec is gone, fall through to the normal path with the feedback as added context.

5. **Worktree sanity.** The orchestrator guarantees a clean tree on a sensible branch. If the tree is dirty in a way inconsistent with a known feedback/in-progress repair, or the branch is an obvious mismatch, escalate `CRITICAL` (`type: dirty-worktree`) — this signals external interference. Skip this check when version control is unavailable.

6. **Route — choose exactly one** (read the spec's `status:` frontmatter if it exists):
   - feedback mode **and** `{spec_file}` exists → `./step-03-implement.md` (repair directly; skip planning)
   - `{spec_file}` exists with `status: draft` → `./step-02-plan.md` (resume planning the draft)
   - `{spec_file}` exists with `status: ready-for-dev | in-progress | in-review | done` → `./step-03-implement.md`
   - otherwise (no usable spec) → `./step-02-plan.md`

## NEXT

Read fully and follow the routed step:

- Step 2: `./step-02-plan.md`
- Step 3: `./step-03-implement.md`
