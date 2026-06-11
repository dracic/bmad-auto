---
---

# Step Auto-Finalize (automation mode only)

Terminal step when `{auto_mode}` is set. Replaces step-04-review and
step-05-present: the orchestrator runs code review in a separate fresh-context
session and creates the commit itself.

## RULES

- No commit. No push. No editor. No review subagents.
- Do not generate a Suggested Review Order.

## INSTRUCTIONS

1. Verify every task in the `## Tasks & Acceptance` section of `{spec_file}` is
   marked `[x]`. If any are not done, go back and finish them first — an
   incomplete task list fails the orchestrator's verification and burns a retry.
2. Change `{spec_file}` status to `in-review` in the frontmatter.
3. Follow `./sync-sprint-status.md` with `{target_status}` = `review`.
4. Write `$BMAD_AUTO_RUN_DIR/tasks/$BMAD_AUTO_TASK_ID/result.json`:

   ```json
   {
     "workflow": "quick-dev",
     "story_key": "<{story_key}, or null if unset>",
     "spec_file": "<absolute path to {spec_file}>",
     "baseline_commit": "<baseline_commit from {spec_file} frontmatter>",
     "tasks_total": <count of tasks in the spec>,
     "tasks_done": <count of tasks marked [x]>,
     "escalations": [<contents of any escalations raised this run, else empty>]
   }
   ```

5. State in one line what was implemented and end your turn. Do not ask
   questions, offer next steps, or wait for anything.

## On Complete

Run: `python3 {project-root}/_bmad/scripts/resolve_customization.py --skill {skill-root} --key workflow.on_complete`

If the resolved `workflow.on_complete` is non-empty, follow it as the final terminal instruction before exiting.
