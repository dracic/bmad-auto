"""Read-only model of sprint-status.yaml — the single source of workflow truth.

The orchestrator NEVER writes this file. Only the BMAD skills mutate it
(via sync-sprint-status); the orchestrator re-reads it to pick the next
story and to verify what a session claims to have done.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

EPIC_RE = re.compile(r"^epic-(\d+)$")
RETRO_RE = re.compile(r"^epic-(\d+)-retrospective$")
RETRO_ITEM_RE = re.compile(r"^epic-(\d+)-retro-item-(\d+)-(.+)$")
STORY_RE = re.compile(r"^(\d+)-(\d+)-(.+)$")
SHORT_REF_RE = re.compile(r"^(\d+)[-.](\d+)$")  # short story ref: 3-1 or 3.1
BARE_NUM_RE = re.compile(r"^(\d+)$")  # a lone story number, needs --epic

STORY_STATUSES = {"backlog", "ready-for-dev", "in-progress", "review", "done"}
LEGACY_STORY_STATUSES = {"drafted": "ready-for-dev"}
ACTIONABLE_STATUSES = {"backlog", "ready-for-dev"}


class SprintStatusError(Exception):
    pass


@dataclass(frozen=True)
class Story:
    key: str
    epic: int
    num: int
    slug: str
    status: str


@dataclass(frozen=True)
class RetroItem:
    """A retrospective action item tracked in sprint-status under the
    RETRO ACTION ITEMS section: ``epic-{epic}-retro-item-{num}-{slug}``.

    Recognized so they no longer fall into ``unknown_keys``; the orchestrator
    does not yet drive them as work (see roadmap: retro-item automation).
    """

    key: str
    epic: int
    num: int
    slug: str
    status: str


@dataclass(frozen=True)
class SprintStatus:
    path: Path
    epics: dict[int, str]
    stories: tuple[Story, ...]
    retros: dict[int, str]
    retro_items: tuple[RetroItem, ...]
    unknown_keys: tuple[str, ...]


def load(path: Path) -> SprintStatus:
    if not path.is_file():
        raise SprintStatusError(f"sprint status file not found: {path}")
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise SprintStatusError(f"sprint status is not valid YAML: {path}: {e}") from e
    if not isinstance(doc, dict):
        raise SprintStatusError(f"sprint status has no top-level mapping: {path}")
    dev = doc.get("development_status")
    if not isinstance(dev, dict):
        raise SprintStatusError(f"sprint status missing development_status map: {path}")

    epics: dict[int, str] = {}
    stories: list[Story] = []
    retros: dict[int, str] = {}
    retro_items: list[RetroItem] = []
    unknown: list[str] = []
    for key, raw_status in dev.items():
        key = str(key)
        status = str(raw_status).strip()
        if m := RETRO_ITEM_RE.match(key):
            retro_items.append(
                RetroItem(
                    key=key,
                    epic=int(m.group(1)),
                    num=int(m.group(2)),
                    slug=m.group(3),
                    status=status,
                )
            )
        elif m := RETRO_RE.match(key):
            retros[int(m.group(1))] = status
        elif m := EPIC_RE.match(key):
            epics[int(m.group(1))] = status
        elif m := STORY_RE.match(key):
            status = LEGACY_STORY_STATUSES.get(status, status)
            stories.append(
                Story(
                    key=key,
                    epic=int(m.group(1)),
                    num=int(m.group(2)),
                    slug=m.group(3),
                    status=status,
                )
            )
        else:
            unknown.append(key)

    return SprintStatus(
        path=path,
        epics=epics,
        stories=tuple(stories),
        retros=retros,
        retro_items=tuple(retro_items),
        unknown_keys=tuple(unknown),
    )


def next_actionable(ss: SprintStatus, skip: set[str] | None = None) -> Story | None:
    """First story in file order whose status allows starting work."""
    skip = skip or set()
    for story in ss.stories:
        if story.key in skip:
            continue
        if story.status in ACTIONABLE_STATUSES:
            return story
    return None


def story_status(path: Path, key: str) -> str | None:
    """Fresh re-read of one story's status, for post-session verification."""
    ss = load(path)
    for story in ss.stories:
        if story.key == key:
            return story.status
    return None


@dataclass(frozen=True)
class StorySelector:
    """Resolves a human story reference (``--epic``/``--story``) to the
    stories it selects. Forms accepted by :func:`parse_selector`:

    * full key ``3-1-user-auth`` — exact match
    * short ref ``3-1`` / ``3.1`` — epic 3, story 1 (any slug)
    * bare number ``1`` with ``--epic 3`` — epic 3, story 1
    * slug fragment ``user-auth`` / ``auth`` — substring of the slug (must be unique)
    * epic only (``--epic 3``, blank story) — every story in the epic
    """

    epic: int | None = None
    num: int | None = None
    key: str | None = None  # exact full key
    slug: str | None = None  # slug substring

    @property
    def is_targeted(self) -> bool:
        """True when the selector names one intended story rather than just
        an epic-wide (or empty) filter."""
        return any(v is not None for v in (self.key, self.num, self.slug))

    def matches(self, story: Story) -> bool:
        if self.key is not None:
            return story.key == self.key
        if self.epic is not None and story.epic != self.epic:
            return False
        if self.num is not None and story.num != self.num:
            return False
        if self.slug is not None and self.slug not in story.slug:
            return False
        return True


def parse_selector(epic: int | None, story: str | None) -> StorySelector:
    """Translate the ``--epic``/``--story`` pair into a :class:`StorySelector`.

    Raises :class:`SprintStatusError` on bad or ambiguous input.
    """
    text = (story or "").strip()
    if not text:
        return StorySelector(epic=epic)

    def _check_epic(parsed_epic: int) -> None:
        if epic is not None and epic != parsed_epic:
            raise SprintStatusError(
                f"--epic {epic} conflicts with story '{text}' (epic {parsed_epic})"
            )

    if m := STORY_RE.match(text):  # full key 3-1-slug
        e, n = int(m.group(1)), int(m.group(2))
        _check_epic(e)
        return StorySelector(epic=e, num=n, key=text)
    if m := SHORT_REF_RE.match(text):  # 3-1 / 3.1
        e, n = int(m.group(1)), int(m.group(2))
        _check_epic(e)
        return StorySelector(epic=e, num=n)
    if m := BARE_NUM_RE.match(text):  # bare story number, needs --epic
        if epic is None:
            raise SprintStatusError(
                f"ambiguous story '{text}': use --epic E --story {text}, or E-{text}"
            )
        return StorySelector(epic=epic, num=int(m.group(1)))
    return StorySelector(epic=epic, slug=text)  # slug fragment


def select_actionable(ss: SprintStatus, epic: int | None, story: str | None) -> list[Story]:
    """Stories selected by ``--epic``/``--story`` that are ready to start, in
    file order. Raises :class:`SprintStatusError` with a targeted message when a
    named story is missing, ambiguous, or exists but is not actionable.
    """
    sel = parse_selector(epic, story)
    matches = [s for s in ss.stories if sel.matches(s)]
    if sel.is_targeted:
        if not matches:
            raise SprintStatusError(f"no story matches '{story}'")
        if sel.slug is not None:
            keys = sorted({s.key for s in matches})
            if len(keys) > 1:
                raise SprintStatusError(
                    f"story '{sel.slug}' is ambiguous — matches: {', '.join(keys)}"
                )
    actionable = [s for s in matches if s.status in ACTIONABLE_STATUSES]
    if sel.is_targeted and matches and not actionable:
        s = matches[0]
        raise SprintStatusError(
            f"story {story} matched {s.key} but its status is " f"'{s.status}' (not actionable)"
        )
    return actionable
