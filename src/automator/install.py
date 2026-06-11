"""`bmad-auto init`: make a target project orchestratable.

- copies the hook relay script to <project>/.automator/bmad_auto_hook.py
- idempotently merges hook registrations into <project>/.claude/settings.json
- writes .automator/policy.toml from the template (if missing)
- gitignores .automator/runs/
"""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

from .policy import POLICY_TEMPLATE

HOOK_EVENTS = ("SessionStart", "Stop", "SessionEnd", "PreCompact")
HOOK_SCRIPT_REL = ".automator/bmad_auto_hook.py"
HOOK_MARKER = "bmad_auto_hook.py"


def _hook_command(event: str) -> str:
    return f'python3 "$CLAUDE_PROJECT_DIR"/{HOOK_SCRIPT_REL} {event}'


def merge_hooks(settings: dict) -> tuple[dict, bool]:
    """Add bmad-auto hook entries to a settings dict. Returns (settings, changed)."""
    changed = False
    hooks = settings.setdefault("hooks", {})
    for event in HOOK_EVENTS:
        matchers = hooks.setdefault(event, [])
        already = any(
            HOOK_MARKER in handler.get("command", "")
            for matcher in matchers
            if isinstance(matcher, dict)
            for handler in matcher.get("hooks", [])
            if isinstance(handler, dict)
        )
        if not already:
            matchers.append({"hooks": [{"type": "command", "command": _hook_command(event)}]})
            changed = True
    return settings, changed


def install_into(project: Path) -> int:
    project = project.resolve()
    automator_dir = project / ".automator"
    automator_dir.mkdir(parents=True, exist_ok=True)

    # 1. hook relay script
    script_target = project / HOOK_SCRIPT_REL
    script_source = resources.files("automator.data").joinpath("bmad_auto_hook.py")
    script_target.write_text(script_source.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"  hook script: {script_target}")

    # 2. settings.json hook registration
    settings_path = project / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings: dict = {}
    if settings_path.is_file():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"FAIL: {settings_path} is not valid JSON; fix it and re-run init")
            return 1
    settings, changed = merge_hooks(settings)
    if changed:
        settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
        print(f"  hooks registered: {settings_path}")
    else:
        print("  hooks already registered")

    # 3. policy template
    policy_path = automator_dir / "policy.toml"
    if policy_path.is_file():
        print("  policy exists, leaving untouched")
    else:
        policy_path.write_text(POLICY_TEMPLATE, encoding="utf-8")
        print(f"  policy written: {policy_path}")

    # 4. gitignore runs dir
    gitignore = project / ".gitignore"
    ignore_line = ".automator/runs/"
    existing = gitignore.read_text(encoding="utf-8") if gitignore.is_file() else ""
    if ignore_line not in existing.splitlines():
        with gitignore.open("a", encoding="utf-8") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write(ignore_line + "\n")
        print(f"  gitignored: {ignore_line}")

    print(
        "init complete. One-time setup: if Claude Code has never run in this "
        "project, start it once (`claude`) and accept the workspace-trust "
        "dialog (and any hooks approval) before `bmad-auto run` — spawned "
        "sessions cannot answer first-run dialogs."
    )
    return 0
