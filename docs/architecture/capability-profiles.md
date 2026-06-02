# Capability Profiles (RFC, Phase 13)

> Status: **Design / RFC**. This is a docs-only artifact. No runtime behavior
> changes ship with this document. Every field and mechanism described here is
> additive and gated behind new, opt-in configuration. With none of the new
> keys present, the agent behaves byte-for-byte as it does today.

This RFC proposes **capability profiles**: a single, declarative description of
*what a delegated agent is allowed to be* — its responsibility, prompt shape,
tool scope, model/budget, workspace and verification policy, handoff contract,
approval gates, and fallbacks. A profile is the future abstraction that unifies
three things Hermes already ships as separate, partially-overlapping concepts:

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

A profile is a named mapping. Every field is optional and has a fail-closed
default (narrowest scope, read-only posture, no side effects). The proposed
top-level shape (Phase 13+; **not yet implemented**):

```yaml
capability_profiles:
  <profile-name>:
    responsibility:        # WHAT this profile is for — one binding sentence
    prompt_sections:       # ordered prompt shape (recipe ref or inline sections)
    allowed_toolsets:      # tool scope; narrow-only, intersect semantics
    model:                 # model/provider/budget block
    provider:
    budget:
    workspace_policy:      # where it may run + whether it may mutate
    verification_policy:   # what counts as "done"
    handoff_schema:        # structured result it must return
    approval_gates:        # actions requiring explicit human approval
    fallbacks:             # ordered provider/model candidates on transport error
```

### Field reference

| Field | Type | Default | Meaning |
|-------|------|---------|---------|
| `responsibility` | string | *required* | One binding sentence describing the slice of work this profile owns. Rendered into the child prompt as the scope contract; it is the *only* identity the child gets. No persona name. |
| `prompt_sections` | recipe-ref \| list | `readonly-advisor` | Either `recipe: <name>` (a built-in from `agent_recipes.py`) or an inline ordered list of `{id, title, body, required}` sections rendered by `render_agent_recipe`. Inline sections may only *add* boundaries, never remove the base scope/role/verification sections. |
| `allowed_toolsets` | list[str] | `[]` (no tools) | Upper bound on tool scope. Resolved with `toolsets_mode: intersect` only — effective scope is `profile ∩ parent ∩ caller-requested`, order preserved. A profile can only ever **narrow**. |
| `model` / `provider` | string | inherit parent | Plain identifiers only — never credentials. Empty inherits the parent's resolved model/provider/credential pool. |
| `budget` | mapping | conservative caps | `{reasoning_effort, max_iterations, child_timeout_seconds}`. Bounds runtime cost. Defaults to the tightest sensible caps so an unset profile cannot run unbounded. |
| `workspace_policy` | mapping | `{kind: scratch, mutate: false}` | `kind` ∈ `scratch \| worktree \| dir`; `mutate` gates whether the child may write. Read-only profiles set `mutate: false`; runtime dispatch denies mutation tools (`write_file`, `patch`, destructive terminal commands) even if a broad read surface such as `file` is present. |
| `verification_policy` | mapping | `{require_evidence: true}` | What the child must do before claiming success: `require_evidence`, optional `commands` (named, advisory), and `on_unverifiable` ∈ `report \| fail`. Mirrors the recipe's *Verification Gates* section, made declarative. |
| `handoff_schema` | mapping | minimal summary | Structured fields the child must return: `changed_files`, `commands_run`, `findings`, `blockers`, plus any profile-specific keys. Drives the parent-visible summary; nothing else enters the parent context. |
| `approval_gates` | list[str] | `[push, merge, publish, send_message]` | Actions that require explicit human approval before the child may perform them. Fail-closed: an action on this list is denied unless an operator approves. |
| `fallbacks` | list | `[]` | Ordered `{provider, model}` candidates tried on a *retryable transport/availability* error only (auth, model-not-found, timeout, rate limit) — never on a model-produced task failure. An entry may narrow but never widen `allowed_toolsets`. |

### Worked example

```yaml
capability_profiles:
  review:
    responsibility: >
      Read the changed files and report correctness, security, and style
      defects. Never execute or mutate anything.
    prompt_sections:
      recipe: critic-reviewer          # reuse the built-in read-only posture
    allowed_toolsets: [file, search]   # no terminal — review must not execute
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
      findings: list           # {file, line, severity, claim}
      blockers: list
    approval_gates: [push, merge, publish, send_message]
    fallbacks:
      - { provider: openrouter, model: "google/gemini-3-flash-preview" }
```

This profile is exactly today's `review` category + `critic-reviewer` recipe +
team `reviewer` role, expressed once. Resolution order, when a profile is
requested on `delegate_task`:

1. Explicit per-call override on the call (highest).
2. The named `capability_profiles.<name>`.
3. Legacy `delegation.categories.<name>` (if the profile only references it).
4. Global `delegation.model` / `delegation.provider`.
5. Parent inheritance (current default, lowest).

If `capability_profiles` is absent and no profile is requested, the engine falls
through to step 3/4/5 — **exactly today's behavior**. A requested-but-unknown
profile returns a structured `unknown_profile` error listing valid names
*before* any child spawns, exactly as `resolve_delegation_category` does today
for `unknown_category`.

## Mapping current concepts into profiles

Profiles are designed so that everything Hermes ships today maps onto exactly
one profile field, with no information lost. The tables below are the migration
crosswalk implementers should preserve.

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

The five shipped categories (`quick`, `deep`, `review`, `visual`, `writing`) map
one-to-one onto five starter profiles. The intersect-only safety property and
the "no credentials in config" rule carry over unchanged — `fallbacks` continue
to hold provider/model identifiers only.

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

This RFC explicitly **does not** propose:

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

| Surface | Default (no `capability_profiles`) | With profiles |
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

Profiles ship as a sequence of additive, individually-revertible steps. Each
step is gated and inert until an operator opts in.

| Step | Scope | Gate | Verification |
|------|-------|------|--------------|
| 13.0 | This RFC (docs only) | none | Docs lint: files exist, headings present, no tabs/trailing whitespace, no secret-like strings, `git diff --check`. |
| 13.1 | Profile resolver (pure, no I/O) reading `capability_profiles`, returning a spec; references recipes/categories | absent key ⇒ inactive | `pytest -k "profile or recipe or categor"` |
| 13.2 | `delegate_task` accepts `profile=`; resolves via 13.1; fail-closed defaults | per-call opt-in | targeted delegate tests |
| 13.3 | Starter profile presets shipped as docs/config YAML (the five category mappings) | copy-in only | preset lint |
| 13.4 | Team templates may reference a profile per node | template opt-in | `pytest -k "team or kanban"` |
| 13.5 | WebUI read-only profile inspector (see UX contract) | feature flag | UI smoke test |
| Pre-merge (every step) | Full `pytest` suite green | — | full suite |

Steps 13.1–13.4 are backend-only and headless-safe. Step 13.5 is the only
UI-touching step and is independently flag-gated.

## Canary plan

Profiles are validated on a narrow, low-blast-radius path before any default
flips:

1. **Resolver-only canary.** Enable 13.1/13.2 for a single operator on a single
   board. Exercise the five starter profiles against real delegations; compare
   the resolved spec to the equivalent legacy category resolution and assert
   they are identical (a profile that references a category must produce the
   same effective `toolsets`, model, and budget).
2. **Read-only profiles first.** Canary `review`, `explore`, and `advise`-shaped
   profiles (no terminal, `mutate: false`) before any write-capable profile, so
   the worst-case canary failure is a no-op read.
3. **Approval-gate assertion.** With a write-capable profile in canary, confirm
   every `approval_gates` action is denied without explicit approval — a single
   un-gated push during canary is a hard stop.
4. **Local audit diff.** Confirm each resolution and each fallback advance emits
   exactly one local audit line and zero network calls.
5. **Promotion criteria.** Promote past canary only when: resolved specs match
   legacy for all reference profiles, no approval gate was bypassed, no remote
   call was observed, and the full `pytest` suite is green.

`default_profile` (the analogue of `default_category`) stays empty throughout
canary; it is only suggested for general use after the criteria above hold.

## Rollback plan

Every step is removable by deleting keys — no migration, no data rewrite.

| Step | Rollback action | Result |
|------|-----------------|--------|
| 13.1 / 13.2 | Delete `capability_profiles`; stop passing `profile=` | Resolver inactive; delegation falls back to categories/global/inherit |
| 13.3 | Delete the starter preset block | Hand-write profiles or use categories |
| 13.4 | Remove `profile:` from team nodes | Team nodes use inline toolsets/approval as today |
| 13.5 | Disable the WebUI flag | Inspector hidden; backend unaffected |

Emergency rollback is a single config delete: removing `capability_profiles`
(and any `default_profile`) reverts the engine to legacy delegation with no
restart of any resolver state required, because resolution is pure and per-call.
Because profiles never mutate existing config, there is nothing to un-migrate.

## WebUI UX contract

The WebUI surface for profiles is **read-only and inspectional** in this RFC.
Editing profiles is out of scope (operators edit YAML); the UI explains and
audits, it does not grant.

Contract:

- **Profile inspector (read-only).** For each configured profile, show:
  `responsibility`, resolved `allowed_toolsets`, `model`/`provider` (identifiers
  only — never credentials, which render as `[REDACTED]`), `budget`,
  `workspace_policy`, `verification_policy`, `handoff_schema` keys,
  `approval_gates`, and the `fallbacks` provider/model list. No field that could
  carry a secret is rendered.
- **Effective-scope preview.** Given a profile and the current parent's
  toolsets, show the *resolved* `profile ∩ parent ∩ requested` set, so an
  operator sees the real narrowing before delegating. The preview must label
  read-only profiles (`mutate: false`, no terminal, write tools denied)
  explicitly.
- **Approval-gate visibility.** When a running child requests a gated action,
  the UI surfaces a clear, blocking approval prompt naming the action and the
  profile; declining is the default and denial is fail-closed. The UI must never
  pre-approve or remember an approval across delegations.
- **Audit view.** A local-only timeline of profile resolutions and fallback
  advances for the current session, sourced from the local audit log — no remote
  fetch. Each entry names the profile, resolved provider/model, and effective
  toolsets.
- **No editing, no execution, no secrets.** The UI cannot create or mutate a
  profile, cannot launch a delegation that widens scope, and never displays
  credential material. Disabling the WebUI flag removes the inspector with zero
  backend effect.

## CI / verification plan

Phase 13.0 (this document) ships no code, so only local docs checks apply:

- files exist with the expected headings,
- no tabs and no trailing whitespace (`git diff --check`),
- no secret-like strings (provider/model identifiers only; credentials shown as
  `[REDACTED]`),
- no dependencies installed, no gateway/WebUI restart, no push.

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
