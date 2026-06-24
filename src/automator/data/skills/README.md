# BMAD Auto module (`bauto`)

A BMAD module pairing the automation skills with the
[bmad-auto orchestrator tool](https://github.com/bmad-code-org/bmad-auto) (the
Python program that drives the loop). The skills can be installed by the BMAD
installer, or laid down by `bmad-auto init` (the orchestrator's wheel **bundles**
them); either way `bmad-auto-setup` installs the `bmad-auto` package from its
Git repository, so installing this module gives you a working system — skills
plus the orchestrator that invokes them. Standard BMAD installs are never
modified; the skills are automator-owned — some are forks maintained against
their upstream counterparts (`bmad-auto-review`), others are standalone or
automator-native (see the table below).

| Component           | Forked from          | Role                                                                                                                                              |
| ------------------- | -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| `bmad-auto`         | — (this repo, Git)   | the orchestrator: ralph-loop, hooks, tmux adapters, TUI. CLI `bmad-auto`. Installed by `bmad-auto-setup` from Git.                                |
| `bmad-auto-review`  | `bmad-code-review`   | unattended adversarial review of a dev spec in a fresh context                                                                                    |
| `bmad-auto-resolve` | — (automator-native) | interactive CRITICAL-escalation resolution: a human disambiguates a frozen spec so a paused story can be re-driven (`/bmad-auto-resolve <story>`) |
| `bmad-auto-sweep`   | — (automator-native) | read-only deferred-work ledger triage                                                                                                             |
| `bmad-auto-setup`   | — (scaffolded)       | registers the module in `_bmad/config.yaml` + `module-help.csv`, **installs the orchestrator tool from Git**, runs `bmad-auto init` + `validate`  |

The **inner dev primitive is the upstream `bmad-dev-auto` skill** (BMAD-METHOD's
generic unattended dev session). It is **not** owned or bundled here — the
orchestrator drives it as an external skill that must already be installed
(by the BMad installer / bmm-core). The automator synthesizes its `result.json`
from the spec the session leaves on disk (see `automator.devcontract`).

## Install into a project

The orchestrator tool now bundles these skills, so `bmad-auto init` lays them
down for you:

```bash
uv tool install "bmad-auto[tui] @ git+https://github.com/bmad-code-org/bmad-auto.git"
bmad-auto init --project /path/to/project --cli claude   # add --cli codex/gemini as needed
claude "/bmad-auto-setup accept all defaults"            # registers _bmad/ config + help
```

`bmad-auto init` installs the `bmad-auto-*` skills into `.claude/skills/`
(claude) and/or `.agents/skills/` (codex/gemini), registers hooks, writes
`.automator/policy.toml`, and gitignores the runs dir. Existing skill dirs are
left untouched (`--force-skills` to overwrite, `--no-skills` to skip).
`bmad-auto-setup` is one-shot for the BMAD-side wiring: it merges config + help
entries, ensures the tool is installed, then runs `bmad-auto init` and
`bmad-auto validate` (preflight).

The skills must be installed **together**: `bmad-auto-review` appends
deferred-work entries per its own `deferred-work-format.md`, and the
upstream `bmad-dev-auto` dev session must also be present. Requires the BMad
Method (bmm) module (`_bmad/bmm/config.yaml`) and a `sprint-status.yaml` from
`bmad-sprint-planning`.

`_bmad/custom/<skill-name>.toml` customization overrides are keyed by skill
directory name — duplicate any `bmad-code-review.toml` override as
`bmad-auto-review.toml`.

## Maintaining the forks

- This directory (`src/automator/data/skills/`) is **canonical** for the skills
  and is bundled into the wheel as package data, so `bmad-auto init` can install
  them. The repo's `.claude/skills/` and `.agents/skills/` hold dev-workspace
  copies; `tests/test_module_skills_sync.py` fails if they drift. After editing
  here, re-copy the skill dirs into both trees.
- The orchestrator tool is **not** bundled in the skill dirs — the BMAD installer
  copies only the skill directories, so a sibling `tool/` would never reach an
  installed project. `bmad-auto-setup` installs the `bmad-auto` package from
  <https://github.com/bmad-code-org/bmad-auto> (`src/automator`, `pyproject.toml`
  are canonical at the repo root). (The skills, by contrast, ride along inside
  the package wheel.)
- `bmad-auto-review` is still a fork of `bmad-code-review` and keeps the upstream
  file structure. To pull upstream improvements:
  `diff -r <bmad-install>/bmad-code-review bmad-auto-review`, merge manually.
- The inner dev primitive `bmad-dev-auto` is **not** maintained here — it is the
  upstream bmm-core skill, driven unmodified. Nothing in this directory mirrors
  it; the orchestrator adapts to it via `automator.devcontract`.
- Do **not** rename the result.json `workflow` values — they are machine
  contracts the orchestrator validates, not skill names:
  - dev → `"auto-dev"` (checked by `verify.DEV_WORKFLOW` in
    `verify_dev` / `verify_dev_bundle`; the orchestrator forges this value in
    `devcontract` when synthesizing the dev result from the spec).
  - sweep triage / migrate → `"deferred-sweep-triage"` / `"deferred-sweep-migrate"`
    (checked in `sweep.py`).
  - `bmad-auto-review` → `"code-review"` (informational only — `verify_review`
    is not handed the result.json, so this value is not enforced).

Validate after changes (from the repo root):

```bash
python3 .claude/skills/bmad-module-builder/scripts/validate-module.py src/automator/data/skills
```
