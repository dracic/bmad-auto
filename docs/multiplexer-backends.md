# Terminal multiplexer backends

The orchestrator drives every agent session — and the TUI's launch/attach keys — through
a terminal multiplexer behind a pluggable seam (`TerminalMultiplexer`). One backend ships
in the box: **tmux**, the default everywhere except native Windows. Additional backends
install as separate packages and register themselves automatically. This page is for
operators: which backend is running, how to switch, and how external backends arrive.
Contributors porting a new backend should start with
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
While tmux is the selected backend it is required for launching, attaching, and driving
runs (an external backend brings its own session mechanism instead); pure TUI observation
works without any backend.

## External backends

Every backend beyond tmux is a separate package that you co-install with bmad-loop; it
registers itself through the `bmad_loop.mux_backends` entry-point group, so installation
is the entire setup — the new backend simply appears in `bmad-loop mux`, selectable and
persistable like a bundled one. With bmad-loop installed as a `uv` tool:

```bash
uv tool install "bmad-loop @ git+https://github.com/bmad-code-org/bmad-loop.git" \
  --with "<adapter package or git URL>"
bmad-loop mux            # the new backend's row appears
bmad-loop mux set <name> # persist for this machine
```

The reference external backend is
**[bmad-loop-adapter-herdr](https://github.com/pbean/bmad-loop-adapter-herdr)** —
[herdr](https://herdr.dev) is a cross-platform, agent-aware terminal workspace manager
whose agent-status sidebar is a natural fit for watching runs. What changes from your
seat on herdr (one manual detach chord, `ctrl+b q`; polled logs; a JSON state sidecar) is
documented in
[that repo's operator guide](https://github.com/pbean/bmad-loop-adapter-herdr/blob/main/docs/adapter-multiplexer-herdr.md).

Two operational notes that apply to any external backend:

- **A broken adapter package never breaks bmad-loop.** If an installed backend fails to
  import, selection proceeds without it; `bmad-loop mux` prints a
  `warning: external backend '<name>' failed to load: <reason>` line and `validate` notes
  the same. The fix is usually reinstalling or upgrading the adapter.
- **`mux set --force` covers late registrations.** A backend that only registers on some
  other machine (where the package IS installed) can still be persisted in a shared
  workflow with `bmad-loop mux set <name> --force`.
