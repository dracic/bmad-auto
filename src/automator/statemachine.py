"""Story lifecycle transition table — the single source of truth for legal moves."""

from __future__ import annotations

from .model import Phase, StoryTask


class IllegalTransition(Exception):
    pass


TRANSITIONS: dict[Phase, frozenset[Phase]] = {
    Phase.PENDING: frozenset({Phase.DEV_RUNNING}),
    Phase.DEV_RUNNING: frozenset({Phase.DEV_VERIFY}),
    Phase.DEV_VERIFY: frozenset(
        {Phase.DEV_RUNNING, Phase.REVIEW_RUNNING, Phase.DEFERRED, Phase.ESCALATED}
    ),
    Phase.REVIEW_RUNNING: frozenset({Phase.REVIEW_VERIFY}),
    Phase.REVIEW_VERIFY: frozenset(
        {Phase.REVIEW_RUNNING, Phase.COMMITTING, Phase.DEFERRED, Phase.ESCALATED}
    ),
    Phase.COMMITTING: frozenset({Phase.DONE, Phase.ESCALATED}),
    Phase.DONE: frozenset(),
    Phase.DEFERRED: frozenset(),
    Phase.ESCALATED: frozenset(),
}


def advance(task: StoryTask, to: Phase) -> None:
    allowed = TRANSITIONS[task.phase]
    if to not in allowed:
        raise IllegalTransition(
            f"{task.story_key}: {task.phase} -> {to} (allowed: {sorted(allowed)})"
        )
    task.phase = to
