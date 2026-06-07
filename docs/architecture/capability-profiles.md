# Capability Profiles (Phase 13)

> Status: **Implemented incrementally**. The resolver, hardening, prompt renderer,
> `delegate_task` profile routing, Team/Kanban bridge, built-in profile pack,
> canary evidence, and WebUI contract/backend bridge are additive and opt-in.
> WebUI routes remain feature-flag gated. With no profile requested and no profile
> config present, delegation keeps the legacy category/global/provider behavior.

**Capability profiles** are a single, declarative description of *what a
delegated agent is allowed to be* — its responsibility, prompt shape, tool
scope, model/budget, workspace and verification policy, handoff contract,
approval gates, and fallbacks. A profile unifies three Hermes surfaces that used
to be separate, partially-overlapping concepts:

- **agent recipes** (`tools/agent_recipes.py`) — prompt posture + readonly flag,
- **delegation categories** (`tools/delegation_categories.py`) — provider/model/
  budget/toolset routing,
- **team metadata** (`hermes_cli/team.py`) — per-card role, workspace, approval
  guards, and handoff wiring.

The goal is to retire *named-agent thinking* — the habit of reaching for a
borrowed persona ("the architect", "the reviewer-bot") and importing its
prompt — and replace it with a Hermes-native, operator-owned profile whose every
field is explicit, reviewable, and fail-closed. A profile is data, not a
personality.

### A profile is a Task Contract

Operationally, a profile is the **Task Contract** for a delegated child: the
single record that fixes *what the child is allowed to be and what it must hand
back*, resolved and validated before any child spawns. `responsibility` is the
scope, `allowed_toolsets` the bounded surface (narrow-only intersect),
`workspace_policy` the mutate/where rule, `verification_policy` the definition of
done, `handoff_schema` the required result, and `approval_gates` the fail-closed
list of outward actions. Binding `profile=` on a `delegate_task` call replaces a
hand-written posture preamble with this enforced contract. The
[user guide](../../website/docs/user-guide/features/delegation.md) shows the
day-to-day call shapes and the dry-run → canary → live workflow; this document is
the schema and clean-room reference.

## Clean-room constraint and no-code-copy policy

This work is **clean-room with respect to external code, schemas, prompts, and
documentation**. The high-level idea (a profile that bundles capability) is
common; the schema and behavior here are independently derived from:

- this RFC and its sibling [`agent-capability-layer.md`](./agent-capability-layer.md),
- the existing Hermes repository and its public docs/style,
- the Hermes [`SECURITY.md`](../../SECURITY.md) trust model.

Contributors implementing later phases **must not** copy, paraphrase, or
transcribe code, config, schemas, prompts, or doc text from `oh-my-openagent`,
`oh-my-opencode`, or `oh-my-hermes`. Profiles are built from Hermes primitives
(`delegate_task`, toolsets, `SKILL.md`, Kanban tools, provider profiles, the
recipe renderer, the category resolver) — not imported from elsewhere. Persona
names from those projects are **not** valid profile names and carry no behavior;
see [Non-goals](#non-goals).

## Why a profile abstraction

Today the same delegated role is described in three places that can drift apart:

| Concern | Lives in | Today's shape |
|---------|----------|---------------|
| Prompt posture / boundaries | `tools/agent_recipes.py` | `AgentRecipe` + ordered `PromptSection`s |
| Provider / model / budget / tool scope | `tools/delegation_categories.py` | a `categories.<name>` block |
| Role / workspace / approval / handoff | `hermes_cli/team.py` | embedded team-meta JSON per card |

A reviewer who wants to answer "*what exactly can a `review` worker do?*" must
read a recipe, a category, and a team template, then reconcile them. A
capability **profile** collapses that into one named record whose fields are the
union of those three concerns, resolved as a single unit before any child
spawns.

Profiles are a **superset, not a replacement** of the existing surfaces: a
profile may *reference* an existing recipe and category instead of redefining
them, so adoption is incremental and the current resolvers keep working
unchanged.

## Profile schema

A profile is a named mapping. Unsupported top-level profile fields are rejected
fail-closed for typos and pass-through risk. Every supported field is optional
and has a fail-closed default (narrowest scope, read-only posture, no side
effects). The implemented runtime shape is:

```yaml
capabilities:
  default_profile: ""      # optional; empty keeps legacy delegation default
  profiles:                # canonical; top-level `capability_profiles:` is a
                           # supported legacy alias, normalized on load
    <profile-name>:
      extends:             # optional parent profile; parent merges first
      enabled:             # false ⇒ disabled_profile error before spawn
      category:            # optional legacy delegation-category reference
      responsibility:      # WHAT this profile is for — one binding sentence
      prompt_sections:     # ordered prompt shape (recipe ref or inline sections)
      allowed_toolsets:    # tool scope; narrow-only, intersect semantics
      model:               # model/provider/budget block
      provider:
      budget:
      workspace_policy:    # where it may run + whether it may mutate
      verification_policy: # what counts as "done"
      handoff_schema:      # structured result it must return
      approval_gates:      # actions requiring explicit human approval
      fallbacks:           # ordered provider/model candidates on transport error
```

`capability_profiles:` is still accepted as a legacy shorthand for
`capabilities.profiles`, but new config should use `capabilities.profiles`.

### Field reference

| Field | Type | Default | Meaning |
|-------|------|---------|---------|
| `responsibility` | string | *required* | One binding sentence describing the slice of work this profile owns. Rendered into the child prompt as the scope contract; it is the *only* identity the child gets. No persona name. |
| `extends` | string | none | Optional parent profile. Parent fields merge first, child overrides. Loops, unknown parents, and chains deeper than 32 profiles fail closed before any child spawn. |
| `enabled` | bool | `true` | `false` disables the profile and returns `disabled_profile` before any child spawn. |
| `category` | string | none | Optional legacy delegation-category reference. Category routing may seed provider/model/budget/toolset defaults; profile fields then narrow or override. Unknown or disabled categories fail closed. |
| `prompt_sections` | recipe-ref \| list | `readonly-advisor` | Either `recipe: <name>` (a built-in from `agent_recipes.py`) or an inline ordered list of `{id, title, body, required}` sections rendered by `render_agent_recipe`. Inline sections may only *add* boundaries, never remove the base scope/role/verification sections. |
| `allowed_toolsets` | list[str] | `[]` (no tools) | Upper bound on tool scope. Resolved with `toolsets_mode: intersect` only — effective scope is `profile ∩ parent ∩ caller-requested`, order preserved. A profile can only ever **narrow**. |
| `model` / `provider` | string | inherit parent | Plain identifiers only — never credentials, URLs/schemes, schemeless authorities, whitespace/control chars, or shell interpolation. Empty inherits the parent's resolved model/provider/credential pool. |
| `budget` | mapping | inherits legacy caps until enforcement | `{reasoning_effort, max_iterations, child_timeout_seconds}`. Bounds runtime cost once child-spawn enforcement lands. Phase 13.1 resolver preserves existing unset-category behavior (`None`/inherit) for backward compatibility; enforcement phases must set concrete caps before using a profile for spawning. |
| `workspace_policy` | mapping | `{kind: scratch, mutate: false}` | `kind` ∈ `scratch \| worktree \| dir`; `mutate` gates whether the child may write. Read-only profiles set `mutate: false`; runtime dispatch denies mutation tools (`write_file`, `patch`, destructive terminal commands) even if a broad read surface such as `file` is present. |
| `verification_policy` | mapping | `{require_evidence: true}` | What the child must do before claiming success: `require_evidence`, optional `commands` (named, advisory), and `on_unverifiable` ∈ `report \| fail`. Mirrors the recipe's *Verification Gates* section, made declarative. |
| `handoff_schema` | mapping | minimal summary | Structured fields the child must return: `changed_files`, `commands_run`, `findings`, `blockers`, plus any profile-specific keys. Drives the parent-visible summary; nothing else enters the parent context. |
| `approval_gates` | list[str] | `[push, merge, publish, send_message]` | Actions that require explicit human approval before the child may perform them. Fail-closed: an action on this list is denied unless an operator approves. |
| `fallbacks` | list | `[]` | Ordered `{provider, model}` candidates tried on a *retryable transport/availability* error only (auth, model-not-found, timeout, rate limit) — never on a model-produced task failure. An entry may include `profile` or a narrowed `allowed_toolsets` list, but no other keys. Provider/model/profile values must be plain identifiers: no env interpolation, URLs/schemes, headers, API keys, tokens, or secret-like fields. String values anywhere in a profile are rejected if they contain shell-style env/command interpolation (`${VAR}`, `${VAR:-default}`, `$VAR`, `%VAR%`, `$(...)`, backticks; Unicode-normalized before scan). An entry may narrow but never widen the final effective toolset scope. |

### Built-in profile pack

Hermes ships a clean-room built-in pack for common delegation modes. These names
are capability labels, not personas; each maps explicitly to a category/recipe
pair and a distinct policy surface.

| Profile | Category | Recipe | Mutates | Default toolsets | Handoff focus |
|---------|----------|--------|---------|------------------|---------------|
| `implementation` | `deep` | `deep-worker` | yes | `terminal`, `file`, `search` | changed files, commands, tests, risks, blockers |
| `review` | `review` | `critic-reviewer` | no | `file`, `search` | findings, evidence, commands, blockers |
| `testing` | `deep` | `focused-executor` | yes | `terminal`, `file`, `search` | tests added, commands, failures, coverage gaps |
| `research` | `writing` | `researcher` | no | `file`, `search`, `web` | sources, findings, recommendation, confidence |
| `orchestration` | `deep` | `team-orchestrator` | no | `delegation`, `file`, `search` | plan, created tasks, dependencies, handoffs |
| `documentation` | `writing` | `focused-executor` | yes | `file`, `search`, `web` | docs changed, source material, commands, gaps |
| `webui-ux` | `visual` | `deep-worker` | yes | `browser`, `vision`, `file`, `search`, `terminal` | user flows, screenshots, changed files, findings |

Legacy category-mirror built-ins (`quick`, `deep`, `visual`, `writing`) remain
available for compatibility. `review` keeps the old name but now uses the stricter
read-only profile contract above.

### Worked example

```yaml
capabilities:
  profiles:
    review:
      responsibility: >
        Read the changed files and report correctness, security, and style
        defects. Never execute or mutate anything.
      prompt_sections:
        recipe: critic-reviewer        # reuse the built-in read-only posture
      allowed_toolsets: [file, search] # no terminal — review must not execute
      # model/provider omitted -> inherit parent
      budget:
        reasoning_effort: high
        max_iterations: 40
        child_timeout_seconds: 600
      workspace_policy:
        kind: scratch
        mutate: false
      verification_policy:
        require_evidence: true
        on_unverifiable: report
      handoff_schema:
        findings: list         # {file, line, severity, claim}
        blockers: list
      approval_gates: [push, merge, publish, send_message]
      fallbacks:
        - { provider: openrouter, model: "google/gemini-3-flash-preview" }
```

This profile is exactly today's `review` category + `critic-reviewer` recipe +
team `reviewer` role, expressed once. Resolution order, when a profile is
requested on `delegate_task`:

1. Explicit per-call override on the call (highest).
2. The named `capabilities.profiles.<name>` entry; top-level
   `capability_profiles.<name>` is accepted only as a legacy alias.
3. Legacy `delegation.categories.<name>` (if the profile only references it).
4. Global `delegation.model` / `delegation.provider`.
5. Parent inheritance (current default, lowest).

If `capabilities.profiles` is absent and no profile is requested, the engine
falls through to step 3/4/5 — **exactly today's behavior**. A
requested-but-unknown profile returns a structured `unknown_profile` error
listing valid names *before* any child spawns, exactly as
`resolve_delegation_category` does today for `unknown_category`.

## Mapping current concepts into profiles

Profiles are designed so that everything Hermes ships today maps onto exactly
one profile field, with no information lost. The canonical config key is
`capabilities.profiles`; the legacy top-level `capability_profiles` map is
accepted as an alias only so old experiments load safely. The tables below are
the migration crosswalk implementers should preserve.

### Agent recipes → `prompt_sections`, `responsibility`, `workspace_policy`

`tools/agent_recipes.py` already defines the prompt shape. The mapping is
mechanical:

| Recipe element | Profile field |
|----------------|---------------|
| `AgentRecipe.identity` | folded into `responsibility` (no standalone persona) |
| `AgentRecipe.mode` (`leaf` / `orchestrator`) | `workspace_policy` + delegation posture |
| `AgentRecipe.readonly` | `workspace_policy.mutate: false` + tool scope with no terminal |
| ordered `PromptSection`s | `prompt_sections` (referenced by name, or inlined) |
| *Scope Contract* section | `responsibility` (binding goal/role/toolsets) |
| *Verification Gates* section | `verification_policy` |
| *Handoff Contract* section | `handoff_schema` |
| *Anti-Duplication / Anti-Escalation* | `approval_gates` + intersect-only `allowed_toolsets` |

Built-in recipes therefore become the default `prompt_sections` library:
`focused-executor`, `deep-worker`, `readonly-advisor`, `researcher`, `explorer`,
`critic-reviewer`, `orchestrator`, `team-orchestrator`. A profile names one of
these; it does not re-implement prompt rendering.

### Delegation categories → `model`/`provider`/`budget`, `allowed_toolsets`, `fallbacks`

`tools/delegation_categories.py` already resolves routing. Each category key has
a direct profile home:

| Category key (today) | Profile field |
|----------------------|---------------|
| `provider`, `model` | `provider`, `model` |
| `reasoning_effort`, `max_iterations`, `child_timeout_seconds` | `budget.*` |
| `toolsets` + `toolsets_mode: intersect` | `allowed_toolsets` (intersect is the *only* mode) |
| `recipe` | `prompt_sections.recipe` |
| `fallback_chain` (provider/model only) | `fallbacks` |
| `enabled: false` | profile-level `enabled: false` → `disabled_profile` error |

The shipped categories (`quick`, `deep`, `review`, `visual`, `writing`) map to
legacy category-mirror profiles and to the named built-in pack above. Mapping is
explicit: `implementation` and `testing` use `deep`, `research` and
`documentation` use `writing`, `orchestration` uses `deep`, and `webui-ux` uses
`visual`. The intersect-only safety property and the "no credentials in config"
rule carry over unchanged — `fallbacks` continue to hold provider/model
identifiers only.

### Team metadata → `responsibility`, `workspace_policy`, `approval_gates`, `handoff_schema`

`hermes_cli/team.py` embeds per-card metadata as JSON. Each field maps cleanly:

| Team-meta field | Profile field |
|-----------------|---------------|
| `role` (`lead`/`member`/`reviewer`) | `responsibility` + delegation posture |
| `category` | informational tag; routing comes from the profile body |
| `toolsets` (advisory) | `allowed_toolsets` (now enforced via intersect, not advisory) |
| `readonly` | `workspace_policy.mutate: false` |
| `workspace` (`scratch`/`worktree`/`dir`) | `workspace_policy.kind` |
| `approval_required` (`push`/`merge`/`publish`/`send_message`) | `approval_gates` |
| roster/mailbox comment + "post handoff as comment" prose | `handoff_schema` |
| `profile` (Hermes profile to assign) | execution binding (unchanged) |
| `skills` | skill load list (unchanged; orthogonal to capability scope) |

A team template thus becomes a small graph of profile *references*: lead →
members → reviewer, where each node names a capability profile instead of
re-declaring toolsets and approval guards inline. The deterministic, no-network,
no-inbound-control properties of team mode are preserved verbatim.

## Non-goals

The implemented layer still **does not** add:

1. **No copied `oh-my-*` prompts, schemas, or persona text.** Profiles are
   clean-room Hermes data. Built-in `prompt_sections` come only from
   `tools/agent_recipes.py`. No external prompt is bundled, referenced, or
   defaulted to.
2. **No persona-name dependency.** A profile's `name` is an operator-chosen
   label with no built-in meaning. There is no registry of magic persona names
   that unlock behavior; `responsibility` + the explicit fields are the whole
   identity. Renaming a profile must never change what it can do.
3. **No live side effects by default.** A profile with no explicit grants runs
   read-only, in a scratch workspace, with the full `approval_gates` list
   active. Every outward action (push, merge, publish, send) is fail-closed and
   requires explicit approval. Profiles cannot widen scope — only narrow it.
4. **No new spawning path, daemon, or inbound remote channel.** Profiles ride on
   `delegate_task` and the existing Kanban; they add no listener and no network
   trigger surface.
5. **No telemetry.** Resolution and fallback advancement emit *local* audit
   lines only (see [`agent-capability-layer.md`](./agent-capability-layer.md)).
   No remote sink is introduced, ever.
6. **No config migration in place.** Existing `delegation.*`, recipes, and team
   templates are never rewritten. Profiles are additive keys; deleting them
   reverts behavior exactly.

## Backward-compatibility guarantee

| Surface | Default (no `capabilities.profiles`) | With profiles |
|---------|-------------------------------------|---------------|
| `delegation.model` / `delegation.provider` | Unchanged | Still honored; profiles layer above |
| `delegation.categories.*` | Unchanged | A profile may reference a category |
| `delegate_task` | Unchanged | Optional `profile=` argument routes |
| `tools/agent_recipes.py` | Unchanged | Profiles select recipes as `prompt_sections` |
| `hermes_cli/team.py` templates | Unchanged | Nodes may reference a profile |
| Toolset intersect semantics | Unchanged | Identical; intersect is the only mode |
| Telemetry | None | None |

If none of the new keys are present, the agent behaves byte-for-byte as before.

## Rollout plan

Profiles shipped as additive, individually-revertible steps. Current backend
state:

| Step | Scope | Gate | Verification |
|------|-------|------|--------------|
| 13.0 | Architecture RFC and threat model | docs only | docs checks |
| 13.1 | Pure resolver for `capabilities.profiles` / legacy `capability_profiles` | absent key ⇒ inactive | resolver tests |
| 13.2 | `delegate_task(profile=...)` routing + capability prompt rendering | per-call opt-in | delegate/profile tests |
| 13.3 | Legacy category-mirror built-ins (`quick`, `deep`, `review`, `visual`, `writing`) | built-in only, no default flip | profile snapshot tests |
| 13.4 | Team templates may reference a profile per node and Kanban workers receive the resolved profile contract | template opt-in | `pytest -k "team or kanban"` |
| 13.5 | Named built-in profile pack (`implementation`, `review`, `testing`, `research`, `orchestration`, `documentation`, `webui-ux`) | explicit `profile=` or `default_profile` | `tests/tools/test_capability_profiles.py` + built-in snapshot tests |
| 13.6 | Canary/audit evidence for profile runtime | isolated temp `HERMES_HOME` | canary script/tests |
| 13.7 | WebUI API contract for task contract, plan preview, gates, and status (docs only) | none | docs lint + contract checklist |
| 13.7B | WebUI backend bridge exposes profile inspector/preview/status as read-only routes | feature flag | API tests; flag-off returns 404; Kanban routes unaffected |
| 13.8 | WebUI UX MVP for capability workflow panel | feature flag | UI/static tests and WebUI lint |
| 13.9 | Operator docs/manual workflow and canary/live playbook | none | docs build + YAML checks |
| Pre-merge | Full integration confidence | no push/restart without approval | targeted then broad tests where practical |

The runtime remains opt-in. With no `profile=` and no
`capabilities.default_profile`, `delegate_task` still uses the legacy
category/global/provider path. Steps 13.7B and 13.8 are WebUI-touching and
independently flag-gated.

## Canary plan

Profiles are validated on a narrow, low-blast-radius path before any default
flips:

1. **Resolver-only canary.** Enable profile calls for a single operator on a
   single board. Exercise the named built-in pack plus legacy mirror profiles
   against real delegations; compare resolved category-derived fields to the
   equivalent legacy category resolution where a profile references a category.
2. **Read-only profiles first.** Canary `review` and `research` before
   write-capable or child-spawning profiles, so the worst-case canary failure is
   a no-op read/synthesis run.
3. **Orchestration with child-spawn gates.** Canary `orchestration` only after
   read-only profiles pass, and constrain spawned children to read-only profiles
   until delegation routing/audit behavior is verified.
4. **Approval-gate assertion.** With a write-capable profile in canary, confirm
   every `approval_gates` action is denied without explicit approval — a single
   un-gated push during canary is a hard stop.
5. **Local audit diff.** Confirm each resolution and each fallback advance emits
   exactly one local audit line and zero network calls.
6. **Promotion criteria.** Promote past canary only when: resolved specs match
   legacy for all reference profiles, no approval gate was bypassed, no remote
   call was observed, and the full `pytest` suite is green.

`default_profile` (the analogue of `default_category`) stays empty throughout
canary; it is only suggested for general use after the criteria above hold.

## Manual operator playbook

The canary plan above maps onto a concrete, operator-driven sequence using
existing CLI and Kanban surfaces — no new tooling:

| Step | Action | How |
|------|--------|-----|
| Dry-run | Preview the graph and profile bindings, create nothing | `hermes team plan "<goal>" --team coding --dry-run` |
| Validate | Surface contract errors cheaply (pure resolution, no spawn) | one read-only `delegate_task(..., profile="review")`; bad config fails with `unknown_profile` / `disabled_profile` / `unknown_toolset` / `secret_field` |
| Canary | Exercise read-only profiles first (`review`, `research`, `mutate: false`) | pass `profile=` per call; keep `default_profile: ""` |
| Create cards | Materialize the board | `hermes team plan "<goal>" --team coding` (drop `--dry-run`) or `kanban_create` |
| Monitor | Track counts, blockers, armed gates | `hermes team status [--team coding]`; TUI `/agents` (`/tasks`); `kanban_show` / `kanban_list` |
| Approval gate | Confirm every gated action denies without approval | a single un-gated `push`/`merge`/`publish`/`send_message` is a hard stop |
| Rollback | Delete profile keys; clear `default_profile`; remove `profile:` from team nodes | reverts to legacy delegation once that config is loaded; do not restart live gateway without approval |

Live-safety constraints during canary:

- **Do not restart the live gateway/WebUI** to test profiles. Start the canary
  process with profile config already present, keep the live process untouched,
  and use the WebUI profile surface as read-only inspect/audit only. A needless
  live restart risks dropping active sessions and remains a separate release
  action.
- **Never reuse the live `DISCORD_BOT_TOKEN`** for a side-by-side canary
  instance. Token-scoped gateway locks prevent two instances from sharing one
  bot credential; give any second instance its own token and its own
  `HERMES_HOME`.
- **No secrets in profile config** — provider/model identifiers only;
  credentials stay in the environment and render as `[REDACTED]`.

## Rollback plan

Every step is removable by deleting keys — no migration, no data rewrite.

| Step | Rollback action | Result |
|------|-----------------|--------|
| 13.1 / 13.2 | Delete `capabilities.profiles` / legacy `capability_profiles`; stop passing `profile=` | Resolver inactive; delegation falls back to categories/global/inherit |
| 13.3 / 13.5 | Stop using built-in `profile=` names or clear `default_profile` | Calls use legacy categories or caller toolsets |
| 13.4 | Remove `profile:` from team nodes | Team nodes use inline toolsets/approval as today |
| 13.7 | Revert the docs-only WebUI contract update | No runtime effect |
| 13.7B / 13.8 | Disable the WebUI flag | Inspector/workflow hidden; backend routes unavailable; existing Kanban routes unaffected |

Emergency rollback is a single config delete: removing `capabilities.profiles`
(or the legacy alias `capability_profiles`) and any `default_profile` reverts the
engine to legacy delegation with no restart of resolver state required, because
resolution is pure and per-call. Because profiles never mutate existing config,
there is nothing to un-migrate.

## WebUI API contract

The WebUI surface for profiles is **read-only and inspectional by default**.
Editing profiles is out of scope (operators edit YAML); the UI explains,
audits, and previews contracts. It uses the existing WebUI Python bridge style
(`api/kanban_bridge.py`) and the Hermes backend resolver/renderer; it does not
add a new server, listener, remote channel, or profile editor.

The entire surface is gated by `webui.capability_inspector_enabled` (default
**false**). With the flag off, every `/api/capabilities/*` route returns `404`
and the existing `/api/kanban/*` routes continue to behave unchanged.

### Endpoints

| Method | Path | Purpose | Mutates |
|--------|------|---------|---------|
| `GET` | `/api/capabilities/profiles` | List builtin + config profile names with a redacted summary | no |
| `GET` | `/api/capabilities/profiles/{name}` | Resolved profile spec for the inspector | no |
| `POST` | `/api/capabilities/preview` | Plan preview + effective-scope for a task contract; **no spawn** | no |
| `GET` | `/api/capabilities/audit?session={id}` | Local-only resolution/fallback timeline | no |
| `GET` | `/api/capabilities/approvals` | Pending gated actions and current denial/expiry state | no |
| `POST` | `/api/capabilities/approvals/{id}` | Decide exactly one pending gated action | yes, decision only |

### Task contract for preview

`POST /api/capabilities/preview` accepts the delegation task contract and returns
the same resolved spec the engine would use, without spawning a child:

```json
{
  "profile": "review",
  "category": null,
  "role": "leaf",
  "parent_toolsets": ["file", "search", "terminal"],
  "requested_toolsets": ["file"],
  "context": "used for resolution only; never echoed back"
}
```

If `profile` is empty/omitted and no `default_profile` is configured, the
response is inactive (`active: false`) and the UI labels the call as legacy
delegation. That mirrors `resolve_capability_profile` exactly.

### Inspector / plan-preview response

`GET /profiles/{name}` and `POST /preview` return a redacted resolved spec:

```json
{
  "active": true,
  "profile": "review",
  "category": "review",
  "responsibility": "Read changed files and report defects without mutation.",
  "prompt_sections": { "recipe": "critic-reviewer" },
  "provider": "",
  "model": "",
  "budget": {
    "reasoning_effort": "high",
    "max_iterations": 40,
    "child_timeout_seconds": 600
  },
  "allowed_toolsets": ["file", "search"],
  "toolsets": ["file"],
  "workspace_policy": { "kind": "scratch", "mutate": false },
  "verification_policy": { "require_evidence": true, "on_unverifiable": "report" },
  "handoff_schema": { "findings": "list", "blockers": "list" },
  "approval_gates": ["push", "merge", "publish", "send_message"],
  "fallback_metadata": {
    "enabled": false,
    "count": 0,
    "providers": [],
    "models": [],
    "profiles": []
  },
  "prompt_preview": "## Capability Profile: review\n..."
}
```

- `allowed_toolsets` is the profile upper bound. `toolsets` is the effective
  `profile ∩ parent ∩ requested` set. The UI must show that narrowing before any
  delegation starts.
- Read-only profiles (`workspace_policy.mutate: false`, no `terminal`) must be
  visibly labeled read-only.
- `prompt_preview` is produced by `render_capability_profile_prompt(...)`. It is
  a local capability contract, not a task prompt, and it must never interpolate
  or echo user `goal`/`context` text.
- Raw `fallbacks` are not serialized. The API returns only `fallback_metadata`.

### Approval gates

A running child's gated action may surface as a pending approval. The decision
surface is the only mutating route in this contract, so it is fail-closed:

```json
{ "decision": "deny" }
```

Rules:

1. **Default deny.** Missing, malformed, expired, or unknown approval requests are
   denied.
2. **Single-shot scope.** A decision applies to exactly one pending action for one
   delegation. Nothing is remembered across delegations, browser sessions, or
   profiles.
3. **No widening.** Approval can only release an action already listed in the
   resolved profile's `approval_gates`; it cannot add tools, change model/provider,
   mutate profile config, or widen workspace access.
4. **Known gates only.** The valid gate ids are `push`, `merge`, `publish`, and
   `send_message`. Unknown actions are rejected, never approved.
5. **Authenticated bridge only.** The route inherits the WebUI's existing request
   authentication/CSRF boundary; direct unauthenticated writes are out of scope.

### Audit / status

`GET /api/capabilities/audit` returns a local-only timeline for the requested
session: profile name, resolved provider/model identifiers, effective toolsets,
and fallback advances. It performs no remote fetch and emits no telemetry.

`GET /api/capabilities/approvals` returns pending approval state only: action id,
profile, task/session id, expiry, and denied/expired reason if already resolved.
It must not include child scratchpad, full prompt text, credentials, or raw
fallback config.

### Error contract

Errors mirror `CapabilityProfileConfigError` so the UI renders backend truth:

```json
{
  "error": {
    "code": "unknown_profile",
    "field": "profile",
    "profile": "reviewr",
    "valid_profiles": ["deep", "quick", "review", "visual", "writing"],
    "message": "Unknown capability profile 'reviewr'"
  }
}
```

`unknown_profile` and `disabled_profile` map to `404`. All other resolver/config
errors (`secret_field`, `unknown_recipe`, `unknown_toolset`, `toolset_widening`,
`env_interpolation`, `extends_loop`, `unknown_category`, and similar) map to
`422`. `valid_profiles` is derived from the resolver at request time, including
operator-defined profiles; the example list above is illustrative. Flag-off is
always `404` and is not an error state in the UI.

### Redaction

Redaction is server-side and mandatory:

- Profile config rejects forbidden secret/env fields before serialization. The
  API serializer still applies defense-in-depth redaction and emits `[REDACTED]`
  for secret-shaped values.
- `provider` and `model` are plain identifiers only. Credential material, headers,
  endpoint URLs, and env references are never returned.
- No route returns raw fallback entries, raw child prompts, or child scratchpads.

### Rollback

Rollback is flag-off: set `webui.capability_inspector_enabled: false` and every
`/api/capabilities/*` route returns `404`. Profiles, Kanban tasks, and delegation
runtime state are untouched.

### Verification checklist for later backend/UI work

- Flag-off: every `/api/capabilities/*` route returns `404`; `/api/kanban/*` is
  unaffected.
- Redaction invariant: serialized responses contain no forbidden secret/env field
  keys and no secret-shaped values.
- Scope math: preview `toolsets` equals resolver output for representative
  parent/requested combinations.
- Error mapping: unknown/disabled profiles and config errors return the status
  and payload shape above.
- Approval safety: malformed approval defaults to denial; approvals are
  single-shot and non-persistent; unknown gate ids are rejected.
- No egress: preview, inspector, audit, and approval routes make zero network
  calls.

## CI / verification plan

Phase 13.7 (this contract update) ships no runtime code, so only local docs
checks apply:

- files exist with the expected headings,
- no tabs and no trailing whitespace (`git diff --check`),
- no secret-like strings (provider/model identifiers only; credentials shown as
  `[REDACTED]`),
- no dependencies installed, no gateway/WebUI restart, no push,
- contract checklist covers endpoints, redaction, flag-off rollback, approval
  default-deny, and no-egress expectations.

Later steps reuse the targeted-then-full pattern from
[`agent-capability-layer.md`](./agent-capability-layer.md): targeted
`pytest -k "profile or recipe or categor or team"` per step, full suite green
before merge.

## Open decisions before implementation

- **Storage for inline `prompt_sections`.** Inline sections vs. a dedicated
  profiles directory vs. referencing recipes only — inherits the open recipe
  storage question from the capability layer RFC.
- **Profile vs. category precedence** when both a `profile=` and a legacy
  `category=` are passed on one call. Proposed: `profile=` wins and `category=`
  is rejected as ambiguous, but this needs an explicit decision.
- **Fallback advancement scope.** As with categories, MVP limits advancement to
  provider/transport availability errors; model-level task failures need a
  separate later design.
- **`default_profile` interaction with `default_category`.** If both are set,
  proposed: `default_profile` wins; flagging the conflict at load time is
  preferred over silent precedence.

## Related documents

- [Agent capability layer (RFC, Phase 0)](./agent-capability-layer.md)
- [Skill-pack & MCP threat model](../security/skill-pack-and-mcp-threat-model.md)
- [Network egress isolation](../security/network-egress-isolation.md)
- [Issue split (phases 1-7)](../plans/agent-capability-layer-issue-split.md)
- [`SECURITY.md`](../../SECURITY.md)
- Safe presets: [`docs/config/delegation-category-presets.yaml`](../config/delegation-category-presets.yaml)
