# OpenCode 1.18.2 API pins (Phase 0 — Day-0 API pinning)

Pinned 2026-07-16 against the real local binary (`opencode --version` → `1.18.2`,
Linux) via `opencode serve` in a scratch dir + `GET /doc` (OpenAPI 3.1, archived
verbatim as [`opencode-openapi-1.18.2.json`](opencode-openapi-1.18.2.json)) and
live curl probes. Zero token spend: sessions were created/aborted/deleted freely;
no prompt was ever sent to a model.

## ⚠ Deviations & load-bearing surprises (read first)

Both hard gates **PASS**, but several facts differ from or refine the plan's
assumptions:

1. **HARD GATE (item 9) PASSES** — `OPENCODE_CONFIG_CONTENT` outranks project
   `opencode.json` on conflicting keys (verified with a control server; see §9).
   No fallback to `OPENCODE_CONFIG` needed.
2. **HARD GATE (item 6) PASSES, with a twist** — the poll fallback is
   `GET /session/status`, a `{sessionID: {type: "busy"|"retry"|"idle"}}` map, but
   **idle sessions are simply absent from the map** (observed `{}` for a fresh and
   a just-aborted session). Poll semantics must be "absent ⇒ not busy", not
   "wait for `type:"idle"`". Bonus: `POST /api/session/{id}/wait` (v2 surface)
   blocks until done and returns 204 — a second, stronger fallback.
3. **Item 5 resolved: per-prompt `model` is an OBJECT** —
   `{"providerID": "...", "modelID": "..."}` in `prompt_async` (and
   `{id, providerID}` in `POST /session`). The `"provider/model"` string form is
   only for the config-file `model` key. Both config-model and per-prompt-model
   are viable; per-prompt needs the object form.
4. **Skill discovery leaks the operator's personal skills by default** —
   `GET /skill` from a probe worktree returned the project's
   `.claude/skills/pin-probe` **plus `~/.claude/skills/*` and
   `~/.agents/skills/*`**. For hermetic runs the disable flags are all-or-nothing
   (`OPENCODE_DISABLE_CLAUDE_CODE_SKILLS=1` kills project `.claude/skills` too).
   Verified hermetic recipe: `OPENCODE_DISABLE_EXTERNAL_SKILLS=1` **plus**
   `skills.paths: ["<abs worktree>/.claude/skills"]` inside
   `OPENCODE_CONFIG_CONTENT` → exactly built-in + project skills. See §10.
5. **Orphan risk is real (item 11)** — `opencode serve` survives parent SIGKILL
   (reparents to init, keeps serving). Adapter must track PIDs and kill in
   `finally` + reconcile paths. SIGTERM exits it cleanly.
6. **Password mode 401s _everything_, including `/global/health`** — the
   readiness poll itself needs credentials when `OPENCODE_SERVER_PASSWORD` is
   set. Basic auth username must be literally `opencode`; Bearer is rejected.
7. **Abort on an idle session still emits `session.status{idle}` +
   `session.idle`** — an idle-event observed after calling abort is NOT proof a
   turn ran (artifact-distrust relevant).
8. Minor: `/health` is the web-UI SPA catch-all, not a health endpoint; `theme`
   is not echoed by `GET /config` (client-side concern, not load-bearing);
   `/api/*` is a parallel "v2" surface (`/api/health`, `/api/event`,
   `/api/session/{id}/wait`, `/api/skill`, …) alongside the classic paths.

---

## 1. Health endpoint (readiness-poll target)

`GET /global/health` → `200`:

```json
{ "healthy": true, "version": "1.18.2" }
```

Schema: `{healthy: true (const), version: string}`, both required. Server
answered within the first 0.5 s poll after spawn. `GET /health` is **not** a
health endpoint (returns the SPA HTML shell). `/api/health` also exists (v2).
With `OPENCODE_SERVER_PASSWORD` set, unauthenticated `/global/health` → `401`
(see §2) — readiness polling must send credentials in that mode.

## 2. Event stream(s): path, framing, filtering, auth

**Both** `/event` and `/global/event` exist and stream (`text/event-stream`).

- `GET /event` — flat frames, `data:`-only (no `event:`/`id:` SSE fields):

  ```text
  data: {"id":"evt_f6bb95b22001U53bfDWRdzlcDG","type":"server.connected","properties":{}}

  data: {"id":"evt_...","type":"session.created","properties":{"sessionID":"ses_...","info":{...Session...}}}

  data: {"id":"evt_...","type":"server.heartbeat","properties":{}}
  ```

- `GET /global/event` — same payloads wrapped with routing context, **plus extra
  `"type":"sync"` frames** (event-sourcing duplicates with `seq`/`aggregateID`):

  ```text
  data: {"directory":"/abs/session/dir","project":"global","payload":{"id":"evt_...","type":"session.idle","properties":{"sessionID":"ses_..."}}}

  data: {"directory":"...","project":"global","payload":{"type":"sync","syncEvent":{"type":"session.created.1","seq":0,"aggregateID":"ses_...","data":{...}}}}
  ```

- First frame on connect: `server.connected`. Keepalive: `server.heartbeat`
  frames ≈ every 10 s (two heartbeats 10 003 ms apart by event-id timestamps).
- No server-side per-session filtering on either path — client filters by
  `properties.sessionID`. (A per-session stream exists on the v2 surface:
  `GET /api/session/{sessionID}/event`.)
- **Recommendation: use `/event`** — simpler framing, no sync-frame noise.
- **Auth** (`OPENCODE_SERVER_PASSWORD=…`): every probed endpoint (health, event,
  session create) → `401` without credentials. `curl -u opencode:<password>`
  works (username is fixed: `-u x:<password>` → 401; `Authorization: Bearer` →
  401).

## 3. Idle event + session-id field; abort emits idle

- `session.idle` exists in 1.18.2 exactly as planned:

  ```json
  { "id": "evt_...", "type": "session.idle", "properties": { "sessionID": "ses_..." } }
  ```

- The newer transition event `session.status` also exists:

  ```json
  { "id": "evt_...", "type": "session.status", "properties": { "sessionID": "ses_...", "status": { "type": "idle" } } }
  ```

  `status` is `SessionStatus`: `{type:"idle"} | {type:"busy"} | {type:"retry",
attempt, message, next, action?}`.

- Session-id property field: **`properties.sessionID`** (pattern `^ses`).
- **`POST /session/:id/abort` on an idle session emitted BOTH events**
  (`session.status{idle}` then `session.idle`, observed live). So idle events
  are emitted even when no turn was running — completion detection must pair
  the idle signal with proof-of-work (message `time.completed`), not treat
  `session.idle` alone as "turn finished".
- Errors surface as `session.error` (`properties.error` = ProviderAuthError |
  MessageAbortedError | ContextOverflowError | APIError | …).

## 4. `POST /session` request/response

Request (all fields optional, `additionalProperties: false`):

```json
{
  "parentID": "ses_...", // pattern ^ses
  "title": "string",
  "agent": "string",
  "model": { "id": "...", "providerID": "...", "variant?": "..." },
  "metadata": {},
  "permission": [{ "permission": "...", "pattern": "...", "action": "..." }], // PermissionRuleset — per-session permission override!
  "workspaceID": "wrk..."
}
```

Live: `POST /session {"title":"pin-probe-A"}` → `200`:

```json
{
  "id": "ses_094469d2dffefWgFnLP2YDmkz2",
  "slug": "calm-garden",
  "projectID": "global",
  "directory": "/abs/server/cwd",
  "path": "relative/form",
  "cost": 0,
  "tokens": { "input": 0, "output": 0, "reasoning": 0, "cache": { "read": 0, "write": 0 } },
  "title": "pin-probe-A",
  "version": "1.18.2",
  "time": { "created": 1784218739410, "updated": 1784218739410 }
}
```

- Id field is **`id`**, pattern `^ses`. `parentID` round-trips (child create
  verified live). `time.*` are epoch **milliseconds**.
- `directory` = server cwd (the per-session-server design gives every session
  the right worktree automatically).
- `DELETE /session/:id` → `200` body `true`; subsequent `GET` → `404`
  `{"name":"NotFoundError","data":{"message":"Session not found: ses_..."}}`.

## 5. `POST /session/:id/prompt_async` body + 204

From the archived OpenAPI (not exercised live — a live call spends tokens);
`required: ["parts"]`, `additionalProperties: false`:

```json
{
  "messageID": "msg...",                                  // optional, pattern ^msg
  "model": {"providerID": "...", "modelID": "..."},       // OBJECT form (both required if present)
  "agent": "string",
  "noReply": false,
  "tools": {"toolname": true},
  "format": OutputFormat,
  "system": "string",
  "variant": "string",
  "parts": [ TextPartInput | FilePartInput | AgentPartInput | SubtaskPartInput ]
}
```

`TextPartInput` = `{"type": "text", "text": "..."}` (+ optional `id` `^prt`,
`synthetic`, `ignored`, `time`, `metadata`). Responses: **`204` "Prompt
accepted"** (confirmed in spec), `400`, `404`. The sync counterpart is
`POST /session/:id/message` (returns the assistant message). Note `system`
and per-prompt `agent` exist — useful knobs later.

## 6. Poll fallback (HARD GATE) — `GET /session/status`

**PASSES.** The `Session` object itself has **no** status/busy field (observed
keys: `id, slug, projectID, directory, path, cost, tokens, title, version,
time` + optional `parentID/summary/share/model/agent/metadata/revert`). Instead:

- `GET /session/status` → `200` map `{ "<sessionID>": SessionStatus }` where
  `SessionStatus = {type:"idle"|"busy"} | {type:"retry", attempt, message,
next, action?}`.
- **Observed: idle sessions are ABSENT from the map** — fresh session and
  just-aborted session both yielded `{}`. Poll rule: `status[sid]` missing or
  `type=="idle"` ⇒ not running; `busy`/`retry` ⇒ running.
- Message-level proof-of-work fallback (as planned):
  `GET /session/:id/message` → `[{info: Message, parts: [...]}]`;
  `AssistantMessage.time = {created (required), completed?}` — a last assistant
  message with `time.completed` set ⇒ turn finished. Empty session → `[]`
  (verified live).
- Bonus (v2): `POST /api/session/{sessionID}/wait` → blocks, `204` on
  completion (also 401/404/503 defined) — a zero-poll degraded path candidate.

## 7. Usage fields (tokens/cost)

`AssistantMessage` (per-message):

```json
"tokens": {"total?": n, "input": n, "output": n, "reasoning": n,
           "cache": {"read": n, "write": n}},   // input/output/reasoning/cache required
"cost": n
```

Plus `modelID`, `providerID`, `time.{created,completed?}`, `error?`,
`summary?: boolean`. **Session-level aggregates exist too**: `Session.tokens`
(same shape, no `total`) and `Session.cost` — live-verified zeroed on create.
`read_usage` can take one `GET /session/:id` instead of summing messages.

## 8. `POST /session/:id/abort`

- Response: `200`, body `true` (plain JSON boolean) — even for an idle session
  with no turn running (no 4xx/error for "nothing to abort").
- Session state after abort (idle case): unchanged — `GET /session/:id`
  byte-identical, no `revert` key, `time.updated` unchanged; still usable
  (child session created against it afterwards).
- Side effect: emits `session.status{idle}` + `session.idle` (§3).
- An aborted _running_ turn's message error is `MessageAbortedError` (in the
  `AssistantMessage.error` union) — schema-pinned; not exercisable without
  spending tokens.

## 9. `OPENCODE_CONFIG_CONTENT` precedence (HARD GATE)

**PASSES — env-injected config outranks project `opencode.json`.**

Setup: server cwd contained `opencode.json` with
`{"permission": {"edit": "deny", "bash": "deny"}, "theme": "gruvbox"}`; env had
`OPENCODE_CONFIG_CONTENT='{"theme":"tokyonight","model":"anthropic/claude-opus-4-8","permission":{"edit":"allow","bash":"allow","skill":"allow"}}'`.

| probe                          | `GET /config` permission                          | model                         |
| ------------------------------ | ------------------------------------------------- | ----------------------------- |
| control (no env var), same cwd | `{"edit":"deny","bash":"deny"}`                   | absent                        |
| with `OPENCODE_CONFIG_CONTENT` | `{"edit":"allow","bash":"allow","skill":"allow"}` | `"anthropic/claude-opus-4-8"` |

Proof the project file participated in the merge (i.e. the win is real, not
vacuous): server log shows `loading path=<cwd>/opencode.json`, and the project
file's `$schema` key appears in the merged `/config` response. Load order from
logs: `~/.config/opencode/config.json` → `~/.config/opencode/opencode.json(c)`
→ `<project>/opencode.json` → `~/.opencode/opencode.json(c)`, with env content
applied as a **"final local-scope merge"** (the built-in `customize-opencode`
skill's own wording, corroborating the observation).

- `PermissionConfig` schema (accepted by 1.18.2): either a bare action string
  or a map over known keys `read, edit, glob, grep, list, bash, task,
external_directory, todowrite, question, webfetch, websearch, lsp, doom_loop,
skill` (+ arbitrary keys); per-key value = action string or
  `{pattern: action}` object (**last matching rule wins**). Actions:
  `allow | ask | deny`.
- Related escape hatches that exist in 1.18.2: `OPENCODE_CONFIG` (extra explicit
  config file), `OPENCODE_DISABLE_PROJECT_CONFIG=1`, `OPENCODE_PURE=1`,
  `OPENCODE_DISABLE_DEFAULT_PLUGINS=1`.
- Quirk: `theme` (from either source) is not echoed by `GET /config`; `/config`
  returned only `$schema, agent, command, mode, model, permission, plugin,
username`. Not load-bearing for the adapter.

## 10. Skill discovery + `skill` permission key

`GET /skill` → array of `{name, description?, location, content}` (content =
SKILL.md body, frontmatter stripped).

- **Project discovery confirmed**: `probe-root/.claude/skills/pin-probe/SKILL.md`
  listed with its frontmatter `name`/`description`.
- **Walk-up confirmed**: server started in `probe-root/subdir/` (probe-root
  being a git repo) still finds `probe-root/.claude/skills/pin-probe` — walks up
  from cwd to the worktree root.
- **Default leak**: the same listing included `~/.claude/skills/pelican-egg` and
  `~/.agents/skills/find-skills` — the operator's personal skills are visible to
  every server by default.
- Disable-flag matrix (live-verified):

  | env                                                                                            | project `.claude/skills` | `~/.claude/skills` | `~/.agents/skills` |
  | ---------------------------------------------------------------------------------------------- | ------------------------ | ------------------ | ------------------ |
  | (none)                                                                                         | ✓                        | ✓ (leak)           | ✓ (leak)           |
  | `OPENCODE_DISABLE_CLAUDE_CODE_SKILLS=1`                                                        | ✗ (!)                    | ✗                  | ✓                  |
  | `OPENCODE_DISABLE_EXTERNAL_SKILLS=1`                                                           | ✗ (!)                    | ✗                  | ✗                  |
  | `OPENCODE_DISABLE_EXTERNAL_SKILLS=1` + `skills.paths=[<abs>/.claude/skills]` in config content | ✓                        | ✗                  | ✗                  |

  The last row is the **verified hermetic recipe** for the adapter: project
  skills only (plus the `customize-opencode` built-in, which is always present).

- Permission key: `skill` is a first-class `PermissionConfig` key of type
  `PermissionRuleConfig` (supports per-skill `{pattern: action}` rules), so
  `{"permission": {"skill": "allow"}}` in `OPENCODE_CONFIG_CONTENT` is valid —
  live-verified via §9's probe (the `skill: allow` key round-tripped through
  `GET /config`).

## 11. Orphan behavior

`opencode serve` does **not** die with its parent. Live test: spawned via an
intermediate shell, parent SIGKILLed → opencode reparented (PPID → init/user
manager) and kept serving (`/global/health` → 200 afterwards). Plain SIGTERM
terminates it cleanly (no SIGKILL escalation needed).

**Consequence:** per-session servers WILL leak if the orchestrator dies without
cleanup. The adapter needs authoritative PID tracking (kill in `finally` +
`_post_kill_reconcile`-style sweep), and stale-server detection on startup is
worth considering (e.g. pinging recorded ports / scanning recorded PIDs).

---

_Probe hygiene: all servers were spawned in a scratch dir outside the repo,
bound to 127.0.0.1 on pre-checked-free ports; every spawned server was killed
and `pgrep opencode` verified empty afterwards. The OpenAPI archive is the
byte-exact `GET /doc` response._
