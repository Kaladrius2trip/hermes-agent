# Agent Capability Layer (RFC, Phase 0)

> Status: **Design / RFC**. This is a Phase 0 docs-only artifact. No runtime
> behavior changes ship with this document. Every feature described here is
> additive and gated behind new, opt-in configuration. Existing behavior is
> preserved unless a new key or option is explicitly set.

This RFC proposes a Hermes-native **agent capability layer**: a thin,
config-driven set of conventions on top of the delegation, toolset, skills, and
Kanban systems Hermes already ships. The goal is to make multi-agent work
(routing, fallback, role recipes, skill packaging, team coordination) explicit
and reproducible without forking the engine or copying code from other
projects.

## Clean-room constraint and no-code-copy policy

This work is **clean-room with respect to external code, schemas, prompts, and
documentation**. The high-level feature inventory comes from the reference
plan, but implementation details must be independently re-derived from:

- this RFC and its sibling docs,
- the existing Hermes repository and its public docs/style,
- the Hermes [`SECURITY.md`](../../SECURITY.md) trust model.

Contributors implementing later phases **must not** copy, paraphrase, or
transcribe code, config, schemas, prompts, or doc text from `oh-my-openagent`,
`oh-my-opencode`, or `oh-my-hermes`. The plan is the spec; those projects are
not source material. The capability layer is built on Hermes primitives
(`delegate_task`, toolsets, `SKILL.md`, Kanban tools, provider profiles) — not
imported from elsewhere.

If you are unsure whether a primitive already exists in Hermes, search this
repo and ask in the issue thread rather than consulting an external project.

## Why a capability layer

Hermes already has the building blocks:

| Need | Existing primitive | Reference |
|------|--------------------|-----------|
| Spawn child agents | `delegate_task` tool + `delegation` config | `tools/delegate_tool.py`, `cli-config.yaml.example` |
| Per-tool grouping | Toolsets / presets | `toolsets.py`, `toolset_distributions.py` |
| Reusable knowledge | `SKILL.md` + Skills Hub | `tools/skills_tool.py`, `tools/skills_hub.py`, `skills/` |
| External tools | MCP servers | `mcp_serve.py`, `optional-mcps/` |
| Multi-agent boards | Kanban tools | `tools/kanban_tools.py` |
| Provider selection | Provider profiles | `providers/__init__.py`, `plugins/model-providers/` |

What is missing is a **convention layer** that ties these together so an
operator can say "route planning-type work to a planner recipe on a cheap-but-
capable model, with a defined fallback if that provider is down" without hand-
wiring each piece. This RFC defines that convention.

## Capability categories (delegation routing)

A **capability category** is a named class of delegated work. Today
`delegate_task` inherits the parent's model/provider unless `delegation.model`
/ `delegation.provider` override it globally. Categories add an optional second
dimension: route *by kind of task* instead of one global override, with
provider/model/toolset/budget settings resolved as one unit.

Proposed (illustrative) categories:

| Category | Intent | Typical toolset shape |
|----------|--------|-----------------------|
| `orchestrate` | Break down and route work to other agents | Kanban + delegate + read-only |
| `plan` | Produce a plan/spec, no mutation | read-only file/search/web |
| `deep-work` | Long, autonomous implementation | full terminal/file/web |
| `focused-exec` | One narrow, well-scoped change | terminal/file, narrowed |
| `advise` | Read-only analysis / answer | search/file/web, no terminal |
| `explore` | Broad search/research fan-out | search/web/file, read-only |
| `critic` | Review/verify someone else's output | read-only + diff |

Routing is **opt-in**. Proposed config sketch (Phase 1 / 1B; not yet
implemented):

```yaml
delegation:
  # existing keys unchanged ...
  # model: ""        # global override still works exactly as today
  # provider: ""
  categories:                 # NEW, optional. Absent => current behavior.
    plan:
      provider: openrouter
      model: "google/gemini-3-flash-preview"
      toolsets: [search, file, web]    # read-only shape
      toolsets_mode: intersect
      max_iterations: 12
      child_timeout_seconds: 300
    deep-work:
      provider: nous
      model: "<capable-model>"
      toolsets: [terminal, file, web, search]
      toolsets_mode: intersect
      max_iterations: 60
      child_timeout_seconds: 1200
```

Resolution order when a category is requested:

1. Explicit per-call override on `delegate_task` (highest).
2. Matching `delegation.categories.<name>`.
3. Global `delegation.model` / `delegation.provider`.
4. Parent inheritance (current default, lowest).

If `delegation.categories` is absent and no category is requested, the engine
falls back to step 3/4 — i.e. **exactly today's behavior**. If a caller
explicitly requests an unknown category, the tool returns a structured error
with the valid category list before spawning a child.

## Per-category fallback chains

Each category may declare an ordered list of provider/model candidates, while
toolsets and budgets stay category-level unless a chain entry narrows them. The
engine tries candidates in order and advances on a *retryable* failure (auth
error, model not found, provider timeout, rate limit) — never on a normal
task-level failure produced by the model itself.

```yaml
delegation:
  categories:
    deep-work:
      fallback_chain:                 # NEW, optional ordered chain
        - { provider: nous,       model: "<primary>" }
        - { provider: openrouter, model: "<secondary>" }
        - { provider: openrouter, model: "<cheap-last-resort>" }
      toolsets: [terminal, file, web, search]
```

Rules:

- A chain with one entry behaves like a plain category (no behavior change).
- Toolset, if set at category level, applies to every chain entry; an entry may
  narrow but **not widen** it.
- Fallback advances only on transport/availability errors, with a bounded retry
  count, and each advance emits one local audit log line (no remote telemetry).
- Absent `fallback_chain` => single candidate => current behavior.

See the threat model's *toolset escalation* and *MCP/env boundary* sections for
why fallback entries cannot widen scope:
[`docs/security/skill-pack-and-mcp-threat-model.md`](../security/skill-pack-and-mcp-threat-model.md).

## Agent recipes

A **recipe** is a named bundle of `{ system-prompt fragment, category,
toolset shape, delegation posture }` that gives a child agent a consistent
role. Recipes are prompt/config orchestration only — they do not add tools the
engine does not already expose.

| Recipe | Category | Posture | Delegate? | Mutates files? |
|--------|----------|---------|-----------|----------------|
| `orchestrator` | `orchestrate` | Decompose, assign via Kanban | yes | no |
| `team-orchestrator` / `planner` | `plan` / `orchestrate` | Plan + route a team | yes | no |
| `deep-worker` | `deep-work` | Long autonomous implementation | optional | yes |
| `focused-executor` | `focused-exec` | One scoped change, then stop | no | yes |
| `read-only-advisor` | `advise` | Answer/analyze, never write | no | no |
| `explorer` / `researcher` | `explore` | Broad fan-out search | no | no |
| `critic-reviewer` | `critic` | Adversarially verify output | no | no |

Recipes compose with categories: a recipe names the category whose
provider/model/fallback it should use, and contributes a prompt fragment plus a
toolset shape that can only be **equal to or narrower** than the category's.
Read-only recipes must resolve to a toolset with no `terminal`/write surface.

These map onto Hermes' existing `role="orchestrator"` mechanic and
`max_spawn_depth`; recipes do not introduce a new spawning path.

## Skill-pack and skill-scoped MCP packaging

A **skill pack** is an ordinary directory of one or more `SKILL.md` skills
(agentskills.io-compatible, exactly as Hermes already loads them) plus an
**optional** manifest declaring the MCP server(s) that pack's skills expect.

Goals:

- Distribute a coherent capability (skills + the MCP tools they call) as one
  reviewable unit.
- Keep MCP scope **bound to the skill that needs it** rather than granting it
  globally, narrowing blast radius.

Proposed manifest sketch (Phase 4; declarative, not yet implemented):

```yaml
# skill-pack/mcp.manifest.yaml  (illustrative)
pack: github-readonly
skills: [list-prs, read-issue]
mcp:
  github-ro:
    command: "<mcp-binary>"
    args: ["--read-only"]
    env:
      GITHUB_TOKEN: "[REDACTED]"     # operator supplies; never committed
    scope:
      skills: [list-prs, read-issue] # tools exposed only while these run
      read_only: true
```

Hard boundaries (enforced by importer/doctor, see Phase 3/4):

- The manifest **declares** an MCP config; it never auto-installs binaries,
  runs lifecycle/`postinstall` scripts, or fetches over the network at import.
- Secrets are operator-supplied at runtime; manifests ship redacted
  placeholders only (`[REDACTED]`). No project-controlled env expansion of
  secrets.
- MCP scope is additive and skill-bound; it cannot widen a parent agent's
  toolset.

This builds entirely on Hermes' existing `SKILL.md` loader and `mcp_servers`
config; the manifest is a thin, reviewable descriptor on top.

## Kanban team mode

**Team mode** is orchestration over the *existing* `delegate_task` + Kanban
tools — not a new coordination engine.

- An orchestrator recipe creates tasks with `kanban_create`, links them with
  `kanban_link`, and dispatches workers via `delegate_task`.
- Workers run with `HERMES_KANBAN_TASK` set (as the dispatcher already does),
  so they see only task-lifecycle tools (`kanban_show`, `kanban_complete`,
  `kanban_block`, `kanban_heartbeat`, `kanban_comment`).
- Board-routing tools (`kanban_list`, `kanban_unblock`) stay orchestrator-only,
  exactly as gated today.
- No remote inbound command channel is introduced. The board is local state;
  there is no default network listener accepting task commands.

Team mode is a documented *pattern* plus thin presets; the gating and tool set
are already in `tools/kanban_tools.py`.

## Additive / backward-compatible guarantee

| Surface | Default (no new config) | With new config |
|---------|-------------------------|-----------------|
| `delegation.model` / `delegation.provider` | Unchanged | Still honored; categories layer above |
| `delegate_task` | Unchanged | Optional `category` argument routes |
| Toolsets / presets | Unchanged | Recipes select narrower shapes |
| `SKILL.md` loading | Unchanged | Packs are ordinary skill dirs |
| `mcp_servers` | Unchanged | Skill-scoped manifests are additive |
| Kanban tools / gating | Unchanged | Team mode is a usage pattern |
| Telemetry | None | None (no default telemetry, ever) |

If none of the new keys are present, the agent behaves byte-for-byte as it does
before this work.

## Config / schema rollback notes per phase

Every phase that touches config must be **removable by deleting the new keys**.
No phase may migrate or rewrite existing config in place.

| Phase | New surface | Rollback |
|-------|-------------|----------|
| 1 — categories MVP | `delegation.categories.*` | Delete the `categories` block; engine reverts to global/inherit routing |
| 1B — fallback chains | `categories.<name>.fallback_chain` | Delete `fallback_chain` lists; single-candidate routing remains |
| 1C — recipes | recipe prompt/preset files | Stop referencing recipe; default prompts/toolsets apply |
| 2 — category presets | shipped preset YAML/docs | Delete preset; hand-write categories or omit |
| 3 — Skills Hub doctor/import hardening | new doctor checks (warn-only first) | Disable/skip doctor; import path unchanged |
| 4 — skill-scoped MCP manifest | `mcp.manifest.yaml` per pack | Remove manifest; skills load without MCP scope |
| 5 — team mode | team presets / docs | Use plain `delegate_task`; Kanban gating unchanged |
| 6 — GitHub read-only pack | optional skill pack dir | Delete the pack directory |
| 7 — local observability | local audit/log bundle | Disable bundle; no remote sink existed to remove |

## CI / verification plan

| Phase | Verification |
|-------|--------------|
| 0 (this doc) | **Docs lint only** — files exist, headings present, no tabs/trailing whitespace, no secret-like strings, `git diff --check`. No deps installed. |
| 1 / 1B | Targeted: `pytest tests/ -k "delegat or categor or fallback"` |
| 1C | Targeted: `pytest tests/ -k "recipe or prompt"` |
| 2 | Targeted: `pytest tests/ -k "preset or categor"` |
| 3 | Targeted: `pytest tests/ -k "skill or hub or doctor or import"` |
| 4 | Targeted: `pytest tests/ -k "mcp or manifest or skill_scope"` |
| 5 | Targeted: `pytest tests/ -k "kanban or team or delegat"` |
| 6 | Targeted: `pytest tests/ -k "skill"` + manual read-only check |
| 7 | Targeted: `pytest tests/ -k "observ or audit or log"` |
| Pre-merge (every phase) | **Full** `pytest` suite green before merge |

Phase 0 ships no code, so only the local docs checks listed in the
[issue split](../plans/agent-capability-layer-issue-split.md) apply.

## Open decisions before implementation

- Category names are **operator-extensible**; bundled presets may suggest common
  names, but the engine must accept any valid configured name. A requested name
  missing from config returns a structured error.
- Recipe prompt fragments still need a storage decision (skills vs. a dedicated
  recipes directory).
- Fallback advancement is limited to provider/transport availability errors for
  MVP; model-level task failures need an explicit later design before support.

## Related documents

- [Skill-pack & MCP threat model](../security/skill-pack-and-mcp-threat-model.md)
- [Issue split (phases 1-7)](../plans/agent-capability-layer-issue-split.md)
- [`SECURITY.md`](../../SECURITY.md)
- [Network egress isolation](../security/network-egress-isolation.md)
