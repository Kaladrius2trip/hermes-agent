# Agent Capability Layer — Issue Split (Phases 0-7)

> Status: **Planning / Phase 0**. Companion to the
> [agent capability layer RFC](../architecture/agent-capability-layer.md) and
> the [skill-pack & MCP threat model](../security/skill-pack-and-mcp-threat-model.md).
> This document slices the work into small, independently reviewable PRs. Each
> slice is additive and backward-compatible: deleting its new config/keys
> reverts to prior behavior.

## Conventions

Each slice lists: **Objective**, **Files likely touched**, **Acceptance**,
**Tests**, **Rollback**, **Security gate**. Keep PRs small (ideally one slice =
one PR). Every PR must keep the full `pytest` suite green before merge.

Global guardrails (apply to every slice):

- [ ] Clean-room: no code/text from `oh-my-openagent`, `oh-my-opencode`,
      `oh-my-hermes`.
- [ ] No new deps, installers, package lifecycle scripts, Docker, cron, MCP
      servers, or network bootstrap.
- [ ] No default telemetry; no remote inbound command channel; no
      auto-merge/auto-push.
- [ ] No project-controlled env expansion for secrets; examples use
      `[REDACTED]`.
- [ ] Additive only: absent new config => current behavior unchanged.

---

## Phase 0 — Docs (this slice)

- **Objective:** Land the RFC, threat model, and this issue split. No runtime
  change.
- **Files:** `docs/architecture/agent-capability-layer.md`,
  `docs/security/skill-pack-and-mcp-threat-model.md`,
  `docs/plans/agent-capability-layer-issue-split.md`.
- **Acceptance:** Three docs exist; RFC covers clean-room policy, categories,
  fallback, recipes, skill-scoped MCP, team mode, additive guarantee, per-phase
  rollback, CI plan; threat model covers all areas in its matrix; relative
  links resolve within the repo.
- **Tests (docs lint, no deps):**
  - [ ] All three files exist.
  - [ ] Each has a top-level `#` heading and required sections.
  - [ ] No tab characters; no trailing whitespace.
  - [ ] No secret-like strings (only `[REDACTED]` placeholders).
  - [ ] `git diff --check` clean.
- **Rollback:** Delete the three docs.
- **Security gate:** Threat model reviewed; redaction policy stated.

---

## Phase 1 — Delegation categories MVP

- **Objective:** Add optional `delegation.categories.<name>` routing
  (provider/model/toolsets/budgets) and a `category` argument on
  `delegate_task`.
- **Files:** `tools/delegate_tool.py`, new `tools/delegation_categories.py`,
  `hermes_cli/config.py`, `cli-config.yaml.example` (commented example), tests.
- **Acceptance:** When a category is requested and defined, the child uses its
  provider/model/toolsets/budgets; existing `delegate_task` calls without
  `category` behave exactly as before; explicitly unknown category returns a
  structured error listing valid categories before any child spawn.
- **Tests:** `pytest tests/ -k "delegat or categor"` — category resolution,
  legacy no-category behavior, unknown-category error.
- **Rollback:** Delete `delegation.categories`; routing reverts to
  global/inherit.
- **Security gate (T4):** Category toolset cannot widen parent toolset; clamp
  or reject supersets.

---

## Phase 1B — Per-category fallback chains

- **Objective:** Add ordered `categories.<name>.fallback_chain` list; advance on
  retryable transport/availability errors only, bounded retries, one local
  audit line per advance.
- **Files:** delegation resolver, retry/fallback logic, `hermes_logging.py`
  usage, `cli-config.yaml.example`, tests.
- **Acceptance:** Single-entry chain == plain category; multi-entry advances on
  auth/not-found/timeout/rate-limit, never on model task failure; entries may
  narrow but not widen the category toolset.
- **Tests:** `pytest tests/ -k "fallback or categor"` — advance-on-transport,
  no-advance-on-task-failure, toolset narrowing enforced.
- **Rollback:** Delete `fallback_chain` lists; single-candidate routing remains.
- **Security gate (T4, T9):** No widening via fallback; audit line carries no
  key material.

---

## Phase 1C — Agent prompt orchestration recipes

- **Objective:** Add named recipes (orchestrator, team-orchestrator/planner,
  deep-worker, focused-executor, read-only-advisor, explorer/researcher,
  critic-reviewer) as prompt fragment + category + toolset shape.
- **Files:** recipe prompt/preset assets, recipe selection in delegation,
  tests.
- **Acceptance:** Selecting a recipe applies its prompt + category + clamped
  toolset; read-only recipes resolve to no-terminal/no-write shapes; recipe
  toolset ⊆ category ⊆ parent.
- **Tests:** `pytest tests/ -k "recipe or prompt"` — read-only recipe has no
  terminal; toolset subset enforced; injected "enable terminal" text does not
  change resolved scope.
- **Rollback:** Stop referencing recipes; default prompts/toolsets apply.
- **Security gate (T4, T5, T8):** No scope widening; no git push/merge wired
  into recipes.

---

## Phase 2 — Category docs + presets

- **Objective:** Ship documented, copy-pasteable category/recipe presets and a
  usage guide.
- **Files:** docs (under `docs/`), shipped preset YAML, `cli-config.yaml.example`
  cross-references.
- **Acceptance:** Operators can enable a sane category set from a documented
  preset; presets honor narrowing rules; docs link RFC + threat model.
- **Tests:** `pytest tests/ -k "preset or categor"` — presets parse and obey
  toolset constraints.
- **Rollback:** Delete presets; hand-write categories or omit.
- **Security gate (T4):** Presets ship no secrets; toolsets are least-privilege
  by default.

---

## Phase 3 — Skills Hub doctor / import hardening

- **Objective:** Harden skill import + add a `doctor` that validates packs
  (no executed lifecycle scripts, no secret-shaped values, collision warnings).
- **Files:** `tools/skills_hub.py`, `tools/skill_manager_tool.py`,
  `tools/skills_tool.py`, tests.
- **Acceptance:** Import is inert/declarative — never runs `postinstall` or
  network bootstrap; doctor flags secret-shaped literals, suspicious prompt-
  injection text, and name collisions; local skills keep precedence; no silent
  overwrite of bundled skills.
- **Tests:** `pytest tests/ -k "skill or hub or doctor or import"` —
  postinstall ignored, secret-shaped value flagged, prompt-injection warning
  surfaced, collision warned not overwritten.
- **Rollback:** Skip/disable doctor; import path unchanged.
- **Security gate (T2, T5, T6, T7):** No install/execution at import; secrets
  rejected; prompt-injection content does not change authority; no skill
  mutation.

---

## Phase 4 — Skill-scoped MCP manifest MVP

- **Objective:** Support an optional per-pack `mcp.manifest.yaml` that
  *declares* MCP server config scoped to named skills (read-only flag, scope
  list, redacted env).
- **Files:** manifest parser/validator, MCP scope wiring into `mcp_servers`
  resolution, tests; example manifest under an optional pack.
- **Acceptance:** Manifest is declarative only; MCP scope is additive within
  declared skills and cannot widen the parent toolset; manifests with
  secret-shaped values are rejected; no binary fetched/run at import.
- **Tests:** `pytest tests/ -k "mcp or manifest or skill_scope"` — scope bound
  to skills, no widening, secret rejection, no auto-fetch.
- **Rollback:** Remove manifest; skills load without MCP scope.
- **Security gate (T2, T4, T6):** No env-secret expansion; no escalation; no
  install.

---

## Phase 5 — Team mode MVP over Kanban

- **Objective:** Document + thin presets for orchestrator-driven team mode over
  existing `delegate_task` + Kanban tools.
- **Files:** team-mode presets/docs, optional orchestrator recipe glue, tests.
- **Acceptance:** Orchestrator creates/links tasks and dispatches workers;
  workers (`HERMES_KANBAN_TASK` set) see only lifecycle tools; board-routing
  stays orchestrator-only; no network listener introduced.
- **Tests:** `pytest tests/ -k "kanban or team or delegat"` — worker tool
  gating preserved, no socket bind, no auto-push.
- **Rollback:** Use plain `delegate_task`; Kanban gating unchanged.
- **Security gate (T3, T8):** No inbound remote channel; no auto-merge/push.

---

## Phase 6 — Optional safe GitHub read-only skills pack

- **Objective:** Ship an optional, read-only GitHub skill pack (e.g. list PRs,
  read issue) demonstrating skill-scoped MCP with a read-only manifest.
- **Files:** `optional-skills/<github-readonly>/` with `SKILL.md` files +
  redacted `mcp.manifest.yaml`, docs.
- **Acceptance:** Pack is read-only (no write/merge tools); manifest ships
  `[REDACTED]` env; operator supplies token at runtime; passes doctor.
- **Tests:** `pytest tests/ -k "skill"` + manual read-only verification.
- **Rollback:** Delete the pack directory.
- **Security gate (T2, T4, T8):** Read-only scope enforced; no secrets shipped;
  no write/push capability.

---

## Phase 7 — Local observability / audit bundle

- **Objective:** Optional, local-only bundle recording routing/fallback/scope
  decisions for operator inspection, reusing existing logging + redaction.
- **Files:** observability/audit module, opt-in config key, `hermes_logging.py`
  integration, docs, tests.
- **Acceptance:** Records are local files only, opt-in, redacted; no outbound
  host; disabling removes files (no remote component).
- **Tests:** `pytest tests/ -k "observ or audit or log"` — no egress, records
  redacted, off-by-default.
- **Rollback:** Disable the bundle key; no remote sink existed to remove.
- **Security gate (T1, T9, T10):** No telemetry; redaction enforced;
  local-only.

---

## Cross-phase checklist

- [ ] Each slice merges only with full `pytest` green.
- [ ] Each slice's new config is independently removable (rollback verified).
- [ ] Security gate tests in the
      [threat model matrix](../security/skill-pack-and-mcp-threat-model.md#verification-test-matrix-summary)
      pass for the relevant threats.
- [ ] Docs updated alongside behavior in the same PR.

## Related documents

- [Agent capability layer RFC](../architecture/agent-capability-layer.md)
- [Skill-pack & MCP threat model](../security/skill-pack-and-mcp-threat-model.md)
- [`SECURITY.md`](../../SECURITY.md)
