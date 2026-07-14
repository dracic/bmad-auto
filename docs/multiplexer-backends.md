# Terminal multiplexer backends

The orchestrator drives every agent session — and the TUI's launch/attach keys — through
a terminal multiplexer behind a pluggable seam (`TerminalMultiplexer`). Two backends ship
today: **tmux**, the default everywhere except native Windows, and **herdr**, opt-in.
This page is for operators: which backend is running, how to switch, and what changes
from your seat on herdr. Contributors porting a new backend should start with
[Porting bmad-loop to a new OS](porting-to-a-new-os.md).

## Which backend is running, and how to switch

`bmad-loop mux` lists the registered backends (platform · availability · version) and
shows which one is selected and why. Selection precedence, highest first:

1. `BMAD_LOOP_MUX_BACKEND=<name>` — forces a backend for one invocation.
2. `bmad-loop mux set <name>` — persists the choice into the gitignored, machine-scoped
   `[mux] backend` key in `.bmad-loop/policy.toml` (`mux set --clear` reverts to auto).
3. The platform default — tmux, everywhere except native Windows — when installed.
4. The first registered backend that matches the platform and is available.

The choice applies to the next invocation — switch between runs, not while one is live:
`attach`, `cleanup`, and the TUI all look for sessions in the currently selected backend.
After switching, `bmad-loop validate` reports the selected backend's availability and
version as part of the preflight.

## tmux (the default)

tmux is the reference backend: everything else in these docs — the `bmad-loop-<run-id>`
and `bmad-loop-ctl` session names, the `ctrl-b d` detach chord — describes tmux behavior.
It is required for launching, attaching, and driving runs; pure TUI observation works
without it.

## herdr (opt-in)

[herdr](https://herdr.dev) is a cross-platform, agent-aware terminal workspace manager —
a background server plus a CLI — whose agent-status sidebar is a natural fit for watching
runs. The backend is opt-in: tmux stays the default on POSIX, and herdr is selected only
when you ask for it.

```bash
bmad-loop mux set herdr                    # persist for this machine
BMAD_LOOP_MUX_BACKEND=herdr bmad-loop run  # …or force a single invocation
```

A bmad-loop **session** becomes a herdr **workspace** (same name) and each **window** a
**tab**. From your seat, runs behave as on tmux: windows close when the agent's process
exits, parked `[bmad-loop exited <code> — press enter]` windows wait for Enter, and the
TUI's Log tab, stall detection, and completion probes all work. One difference is
functional and needs a keypress from you; the rest are cosmetic or invisible.

### Detach is manual: press `ctrl+b q`

herdr has no CLI command to detach an attached terminal — detach exists only as herdr's
own keybinding, by default `ctrl+b q` (`ctrl+b ctrl+b` sends a literal `ctrl+b`). So the
one moment where bmad-loop would detach your terminal **for** you becomes manual:

- **When it bites** — you attached from a plain terminal (or the TUI suspended itself to
  attach you) to answer a sweep decision. On tmux, answering prints
  `✓ decisions recorded — sweep continues in the background` and your shell or dashboard
  comes back by itself. On herdr you see the same message, but your terminal **stays
  attached** to the sweep's window.
- **What to do** — press `ctrl+b q`. Nothing is lost or waiting on you: the answer is
  already recorded and the sweep runs unattended; the keypress only hands your terminal
  back. A suspended TUI resumes the moment the attach exits.
- **What still returns automatically** — everything that rides a window _closing_: when a
  parked window's command finishes and you press Enter, the pane closes and the blocking
  attach exits on its own, exactly as on tmux. Attaching from **inside** herdr is a tab
  switch and behaves like tmux's `switch-client`.

### Differences you may notice

| Where                          | On tmux                                  | On herdr                                                                                                              | What to do                                                                               |
| ------------------------------ | ---------------------------------------- | --------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------- |
| Session logs (TUI Log tab)     | output streams continuously (a tmux tee) | the pane is snapshotted about once a second, and only when its content changes — a static screen doesn't grow the log | nothing — stall detection and completion markers read the same log and work unchanged    |
| Backend state                  | lives in the tmux server                 | lives in a lock-guarded JSON sidecar, `~/.bmad-loop/herdr-state.json` (override: `BMAD_LOOP_HERDR_STATE`)             | leave it alone while runs are live; entries for gone workspaces are pruned automatically |
| Switching back after an attach | falls back to your most recent client    | herdr has no "last client" — if the pane you came from is gone, focus stays put                                       | switch tabs yourself                                                                     |
| Detached window size           | honors the requested geometry headless   | advisory — a detached pane takes the size of whichever client attaches                                                | nothing; the first attach may briefly reflow                                             |
| Server lifecycle               | any tmux command starts the server       | bmad-loop starts the herdr server lazily, and only for operations that create or change something                     | nothing — `bmad-loop mux`, `validate`, and listings never resurrect a stopped server     |

### Current limits

- **POSIX launches only for now.** The herdr backend launches windows through a POSIX
  shell recipe, so it runs on Linux, macOS, and WSL today; the native-Windows launch path
  is tracked in [the roadmap](ROADMAP.md#native-windows-multiplexer-backend) (#140).
- **Don't hand-create `bmad-loop-*` workspaces.** herdr allows duplicate workspace labels
  (tmux session names are unique); bmad-loop refuses to create a duplicate itself and
  always resolves the first match, but a hand-made duplicate can shadow a run's real
  workspace.
