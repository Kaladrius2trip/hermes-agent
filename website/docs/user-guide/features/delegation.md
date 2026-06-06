---
sidebar_position: 7
title: "Subagent Delegation"
description: "Spawn isolated child agents for parallel workstreams with delegate_task"
---

# Subagent Delegation

The `delegate_task` tool spawns child AIAgent instances with isolated context, restricted toolsets, and their own terminal sessions. Each child gets a fresh conversation and works independently — only its final summary enters the parent's context.

## Single Task

```python
delegate_task(
    goal="Debug why tests fail",
    context="Error: assertion in test_foo.py line 42",
    toolsets=["terminal", "file"]
)
```

## Parallel Batch

Up to 3 concurrent subagents by default (configurable, no hard ceiling):

```python
delegate_task(tasks=[
    {"goal": "Research topic A", "toolsets": ["web"]},
    {"goal": "Research topic B", "toolsets": ["web"]},
    {"goal": "Fix the build", "toolsets": ["terminal", "file"]}
])
```

## How Subagent Context Works

:::warning Critical: Subagents Know Nothing
Subagents start with a **completely fresh conversation**. They have zero knowledge of the parent's conversation history, prior tool calls, or anything discussed before delegation. The subagent's only context comes from the `goal` and `context` fields the parent agent populates when it calls `delegate_task`.
:::

This means the parent agent must pass **everything** the subagent needs in the call:

```python
# BAD - subagent has no idea what "the error" is
delegate_task(goal="Fix the error")

# GOOD - subagent has all context it needs
delegate_task(
    goal="Fix the TypeError in api/handlers.py",
    context="""The file api/handlers.py has a TypeError on line 47:
    'NoneType' object has no attribute 'get'.
    The function process_request() receives a dict from parse_body(),
    but parse_body() returns None when Content-Type is missing.
    The project is at /home/user/myproject and uses Python 3.11."""
)
```

The subagent receives a focused system prompt built from your goal and context, instructing it to complete the task and provide a structured summary of what it did, what it found, any files modified, and any issues encountered.

## Practical Examples

### Parallel Research

Research multiple topics simultaneously and collect summaries:

```python
delegate_task(tasks=[
    {
        "goal": "Research the current state of WebAssembly in 2025",
        "context": "Focus on: browser support, non-browser runtimes, language support",
        "toolsets": ["web"]
    },
    {
        "goal": "Research the current state of RISC-V adoption in 2025",
        "context": "Focus on: server chips, embedded systems, software ecosystem",
        "toolsets": ["web"]
    },
    {
        "goal": "Research quantum computing progress in 2025",
        "context": "Focus on: error correction breakthroughs, practical applications, key players",
        "toolsets": ["web"]
    }
])
```

### Code Review + Fix

Delegate a review-and-fix workflow to a fresh context:

```python
delegate_task(
    goal="Review the authentication module for security issues and fix any found",
    context="""Project at /home/user/webapp.
    Auth module files: src/auth/login.py, src/auth/jwt.py, src/auth/middleware.py.
    The project uses Flask, PyJWT, and bcrypt.
    Focus on: SQL injection, JWT validation, password handling, session management.
    Fix any issues found and run the test suite (pytest tests/auth/).""",
    toolsets=["terminal", "file"]
)
```

### Multi-File Refactoring

Delegate a large refactoring task that would flood the parent's context:

```python
delegate_task(
    goal="Refactor all Python files in src/ to replace print() with proper logging",
    context="""Project at /home/user/myproject.
    Use the 'logging' module with logger = logging.getLogger(__name__).
    Replace print() calls with appropriate log levels:
    - print(f"Error: ...") -> logger.error(...)
    - print(f"Warning: ...") -> logger.warning(...)
    - print(f"Debug: ...") -> logger.debug(...)
    - Other prints -> logger.info(...)
    Don't change print() in test files or CLI output.
    Run pytest after to verify nothing broke.""",
    toolsets=["terminal", "file"]
)
```

## Batch Mode Details

When you provide a `tasks` array, subagents run in **parallel** using a thread pool:

- **Maximum concurrency:** 3 tasks by default (configurable via `delegation.max_concurrent_children` or the `DELEGATION_MAX_CONCURRENT_CHILDREN` env var; floor of 1, no hard ceiling). Batches larger than the limit return a tool error rather than being silently truncated.
- **Thread pool:** Uses `ThreadPoolExecutor` with the configured concurrency limit as max workers
- **Progress display:** In CLI mode, a tree-view shows tool calls from each subagent in real-time with per-task completion lines. In gateway mode, progress is batched and relayed to the parent's progress callback
- **Result ordering:** Results are sorted by task index to match input order regardless of completion order
- **Interrupt propagation:** Interrupting the parent (e.g., sending a new message) interrupts all active children

Single-task delegation runs directly without thread pool overhead.

## Model Override

You can configure a different model for subagents via `config.yaml` — useful for delegating simple tasks to cheaper/faster models:

```yaml
# In ~/.hermes/config.yaml
delegation:
  model: "google/gemini-flash-2.0"    # Cheaper model for subagents
  provider: "openrouter"              # Optional: route subagents to a different provider
```

If omitted, subagents use the same model as the parent.

## Toolset Selection Tips

The `toolsets` parameter controls what tools the subagent has access to. Choose based on the task:

| Toolset Pattern | Use Case |
|----------------|----------|
| `["terminal", "file"]` | Code work, debugging, file editing, builds |
| `["web"]` | Research, fact-checking, documentation lookup |
| `["terminal", "file", "web"]` | Full-stack tasks (default) |
| `["file"]` | Read-only analysis, code review without execution |
| `["terminal"]` | System administration, process management |

Certain toolsets are blocked for subagents regardless of what you specify:
- `delegation` — blocked for leaf subagents (the default). Retained for `role="orchestrator"` children, bounded by `max_spawn_depth` — see [Depth Limit and Nested Orchestration](#depth-limit-and-nested-orchestration) below.
- `clarify` — subagents cannot interact with the user
- `memory` — no writes to shared persistent memory
- `code_execution` — children should reason step-by-step
- `send_message` — no cross-platform side effects (e.g., sending Telegram messages)

## Max Iterations

Each subagent has an iteration limit (default: 50) that controls how many tool-calling turns it can take:

```python
delegate_task(
    goal="Quick file check",
    context="Check if /etc/nginx/nginx.conf exists and print its first 10 lines",
    max_iterations=10  # Simple task, don't need many turns
)
```

## Child Timeout

Subagents are killed as stuck if they go quiet for more than `delegation.child_timeout_seconds` wall-clock seconds. The default is **600** (10 minutes) — bumped up from 300s in earlier releases because high-reasoning models on non-trivial research tasks were getting killed mid-think. Tune it per-install:

```yaml
delegation:
  child_timeout_seconds: 600   # default
```

Lower it for fast local models; raise it for slow reasoning models on hard problems. The timer resets every time the child makes an API call or tool call — only genuinely idle workers trigger the kill.

:::tip Diagnostic dump on zero-call timeout
If a subagent times out having made **zero** API calls (usually: provider unreachable, auth failure, or tool-schema rejection), `delegate_task` writes a structured diagnostic to `~/.hermes/logs/subagent-timeout-<session>-<timestamp>.log` containing the subagent's config snapshot, credential-resolution trace, and any early error messages. Much easier to root-cause than the previous silent-timeout behavior.
:::

## Monitoring Running Subagents (`/agents`)

The TUI ships a `/agents` overlay (alias `/tasks`) that turns recursive `delegate_task` fan-out into a first-class audit surface:

- Live tree view of running and recently-finished subagents, grouped by parent
- Per-branch cost, token, and file-touched rollups
- Kill and pause controls — cancel a specific subagent mid-flight without interrupting its siblings
- Post-hoc review: step through each subagent's turn-by-turn history even after they've returned to the parent

The classic CLI just prints `/agents` as a text summary; the TUI is where the overlay shines. See [TUI — Slash commands](/user-guide/tui#slash-commands).

## Depth Limit and Nested Orchestration

By default, delegation is **flat**: a parent (depth 0) spawns children (depth 1), and those children cannot delegate further. This prevents runaway recursive delegation.

For multi-stage workflows (research → synthesis, or parallel orchestration over sub-problems), a parent can spawn **orchestrator** children that *can* delegate their own workers:

```python
delegate_task(
    goal="Survey three code review approaches and recommend one",
    role="orchestrator",  # Allows this child to spawn its own workers
    context="...",
)
```

- `role="leaf"` (default): child cannot delegate further — identical to the flat-delegation behavior.
- `role="orchestrator"`: child retains the `delegation` toolset. Gated by `delegation.max_spawn_depth` (default **1** = flat, so `role="orchestrator"` is a no-op at defaults). Raise `max_spawn_depth` to 2 to allow orchestrator children to spawn leaf grandchildren; 3+ for deeper trees. There is no upper ceiling — cost is the practical limit.
- `delegation.orchestrator_enabled: false`: global kill switch that forces every child to `leaf` regardless of the `role` parameter.

**Cost warning:** With `max_spawn_depth: 3` and `max_concurrent_children: 3`, the tree can reach 3×3×3 = 27 concurrent leaf agents. Each extra level multiplies spend — raise `max_spawn_depth` intentionally.

## Lifetime and Durability

:::warning delegate_task is synchronous — not durable
`delegate_task` runs **inside the parent's current turn**. It blocks the parent until every child finishes (or is cancelled). It is **not** a background job queue:

- If the parent is interrupted (user sends a new message, `/stop`, `/new`), all active children are cancelled and return `status="interrupted"`. Their in-progress work is discarded.
- Children do **not** continue running after the parent turn ends.
- Cancelled children return a structured result (`status="interrupted"`, `exit_reason="interrupted"`), but because the parent was interrupted too, that result often never makes it into a user-visible reply.

For **durable long-running work** that must survive interrupts or outlive the current turn, use:

- `cronjob` (action=`create`) — schedules a separate agent run; immune to parent-turn interrupts.
- `terminal(background=True, notify_on_complete=True)` — long-running shell commands that keep running while the agent does other things.
:::

## Key Properties

- Each subagent gets its **own terminal session** (separate from the parent)
- **Nested delegation is opt-in** — only `role="orchestrator"` children can delegate further, and only when `max_spawn_depth` is raised from its default of 1 (flat). Disable globally with `orchestrator_enabled: false`.
- Leaf subagents **cannot** call: `delegate_task`, `clarify`, `memory`, `send_message`, `execute_code`. Orchestrator subagents retain `delegate_task` but still cannot use the other four.
- **Interrupt propagation** — interrupting the parent interrupts all active children (including grandchildren under orchestrators)
- Only the final summary enters the parent's context, keeping token usage efficient
- Subagents inherit the parent's **API key, provider configuration, and credential pool** (enabling key rotation on rate limits)

## Delegation vs execute_code

| Factor | delegate_task | execute_code |
|--------|--------------|-------------|
| **Reasoning** | Full LLM reasoning loop | Just Python code execution |
| **Context** | Fresh isolated conversation | No conversation, just script |
| **Tool access** | All non-blocked tools with reasoning | 7 tools via RPC, no reasoning |
| **Parallelism** | 3 concurrent subagents by default (configurable) | Single script |
| **Best for** | Complex tasks needing judgment | Mechanical multi-step pipelines |
| **Token cost** | Higher (full LLM loop) | Lower (only stdout returned) |
| **User interaction** | None (subagents can't clarify) | None |

**Rule of thumb:** Use `delegate_task` when the subtask requires reasoning, judgment, or multi-step problem solving. Use `execute_code` when you need mechanical data processing or scripted workflows.

## Delegation Capability Categories

Capability categories are the optional **capability layer** on top of legacy delegation. A
*category* bundles a provider/model, a runtime budget, a prompt recipe, and a
toolset scope under a short intent name, so the agent delegates by intent
("quick", "deep", "review", "visual", "writing") instead of wiring providers ad
hoc on every call. The layer is resolved by
`tools/delegation_categories.resolve_delegation_category` **before** any child is
spawned, so configuration mistakes fail fast with a precise error.

:::info Clean-room notice
Categories and recipe names are Hermes-native concepts. They map only to the
built-in recipes in `tools/agent_recipes.py` and the resolver in
`tools/delegation_categories.py` — no code or prompts are copied from any
external project.
:::

### Safe category presets

A ready-to-merge `delegation:` block lives at repository path
`docs/config/delegation-category-presets.yaml`. It defines five categories you can copy straight into
`~/.hermes/config.yaml`:

| Category | Recipe | Toolsets | Posture |
|----------|--------|----------|---------|
| `quick` | `focused-executor` | `[file, search]` | Cheap/fast leaf edits and lookups |
| `deep` | `deep-worker` | `[file, search, terminal, web]` | Long, high-effort multi-file work |
| `review` | `critic-reviewer` | `[file, search]` | Read-only critique — **no `terminal`** |
| `visual` | `explorer` | `[vision, browser, file]` | Screenshots, diagrams, UI inspection |
| `writing` | `researcher` | `[file, search, web]` | Drafting prose/docs from sources |

Every category sets `toolsets_mode: intersect`. This is the core safety
guarantee: a category can only ever **narrow** toolset scope, never escalate it.
The child's effective toolsets are `category ∩ parent ∩ caller-requested`, with
category order preserved. `review` deliberately omits `terminal` so a reviewer
cannot execute or mutate anything.

Valid recipe names come from `tools/agent_recipes.py`: `focused-executor`,
`deep-worker`, `critic-reviewer`, `explorer`, `researcher`, `readonly-advisor`,
`orchestrator`, `team-orchestrator`.

### Enabling categories

```yaml
# In ~/.hermes/config.yaml — merged with any existing delegation: settings
delegation:
  default_category: ""        # "" = opt-in per call; set a name to apply globally
  categories:
    quick:
      recipe: focused-executor
      reasoning_effort: low
      max_iterations: 20
      child_timeout_seconds: 300
      toolsets_mode: intersect
      toolsets: [file, search]
      fallback_chain:                  # optional; provider/model identifiers only
        - provider: openrouter
          model: "google/gemini-3-flash-preview"
    review:
      recipe: critic-reviewer
      reasoning_effort: high
      toolsets_mode: intersect
      toolsets: [file, search]   # no terminal — read-only review
```

Set `default_category` to apply one intent to every `delegate_task`, or leave it
empty and let the agent pass a category per call.

`fallback_chain` is optional per category. Keep it to provider/model identifiers
only; credential material stays in environment or credential-pool config and is
never needed in the category preset.

:::warning No credentials in presets
Category blocks never carry `api_key`, `password`, or `base_url`. Provider/model
values are plain identifiers; real secrets stay in `~/.hermes/.env` (shown as
`[REDACTED]` in docs, never inlined). Subagents inherit the parent's resolved
credentials, so you almost never need to set credentials per category.
:::

### Migration from ad-hoc delegation prompts

Categories replace hand-written "act as a careful reviewer, don't run anything,
only look at these files…" preambles with a named, enforced posture:

| Before (ad-hoc prompt) | After (category) |
|------------------------|------------------|
| `delegate_task(goal="Review auth for bugs but DON'T run anything", toolsets=["file"])` | `delegate_task(goal="Review auth for bugs", category="review")` |
| `delegate_task(goal="Quick: just check this one file", toolsets=["file"], max_iterations=10)` | `delegate_task(goal="Check this file", category="quick")` |
| `delegate_task(goal="Deep refactor across src/", toolsets=["terminal","file","web"])` | `delegate_task(goal="Deep refactor across src/", category="deep")` |

Migration is **incremental and reversible**:

1. **Nothing changes by default.** `delegation.model` / `delegation.provider`
   still work exactly as before; the category layer stays inactive until you add
   `categories`.
2. **Add categories alongside** your existing `delegation:` settings. A category
   with no `provider`/`model` inherits the legacy values.
3. **Opt in gradually** — pass `category=` on the calls you want, or set
   `default_category` once you trust it.
4. **Revert any time** by deleting `default_category` and `categories`; behavior
   falls straight back to legacy single-provider delegation.

Override precedence: an explicit `category=` argument wins over
`default_category`; a category's `provider`/`model`/`reasoning_effort` override
the top-level `delegation.*` defaults; caller-passed `toolsets` further narrow
the category scope.

### Capability profiles

Capability profiles are the higher-level delegation contract on top of categories
and recipes. A profile bundles responsibility, category/model/budget/tool scope,
workspace mutation policy, verification requirements, approval gates, and a
handoff schema. Profiles are opt-in: pass `profile=` on a `delegate_task` call or
set `capabilities.default_profile`; otherwise legacy delegation is unchanged.

Built-in profiles:

| Profile | Use | Mutates | Default toolsets |
|---------|-----|---------|------------------|
| `implementation` | scoped code changes with tests | yes | `[terminal, file, search]` |
| `review` | read-only correctness/security review | no | `[file, search]` |
| `testing` | add/run focused tests and isolate failures | yes | `[terminal, file, search]` |
| `research` | evidence-backed synthesis from files/web | no | `[file, search, web]` |
| `orchestration` | decompose and route child work | no | `[delegation, file, search]` |
| `documentation` | update docs from verified project facts | yes | `[file, search, web]` |
| `webui-ux` | inspect/fix WebUI UX with visual evidence | yes | `[browser, vision, file, search, terminal]` |

Example call:

```python
delegate_task(
    goal="Implement retry handling in src/client.py and verify focused tests",
    profile="implementation"
)
```

Custom profile YAML:

```yaml
capabilities:
  default_profile: ""  # keep empty for opt-in per call
  profiles:
    security-review:
      responsibility: >
        Review changed files for security regressions and return findings only.
      category: review
      prompt_sections:
        recipe: critic-reviewer
      allowed_toolsets: [file, search]
      workspace_policy:
        kind: scratch
        mutate: false
      verification_policy:
        require_evidence: true
        on_unverifiable: report
      handoff_schema:
        findings: list
        evidence: list
        blockers: list
      approval_gates: [push, merge, publish, send_message]
```

Rules:

- Profiles can only narrow tool scope: `profile ∩ category ∩ parent ∩ caller-requested`.
- `provider`/`model` are identifiers only; never put `api_key`, `password`,
  `base_url`, or auth headers in a profile.
- `capability_profiles:` is accepted as a legacy shorthand for
  `capabilities.profiles`, but new configs should use `capabilities.profiles`.
- Unknown, disabled, unsafe, secret-like, or external-prompt-import profile
  config fails before a child spawns.

Capability profile rollout is additive and reversible: existing `delegation:`
config keeps working unchanged, and profile-backed surfaces stay opt-in. See
[`docs/architecture/capability-profiles.md`](https://github.com/NousResearch/hermes-agent/blob/main/docs/architecture/capability-profiles.md)
for the full schema, migration crosswalk, clean-room policy, rollout/rollback
plan, and the flag-gated [WebUI API contract](https://github.com/NousResearch/hermes-agent/blob/main/docs/architecture/capability-profiles.md#webui-api-contract)
for plan preview, effective-scope preview, and approval-gate visibility.

#### Profiles as Task Contracts

A profile *is* the **Task Contract** for a delegated agent: one declarative record
that answers "what is this child allowed to be, and what must it return?" Instead
of re-writing posture, tool scope, and guardrails into each `goal`/`context`
string, you bind one profile and the contract is resolved (and validated) before
any child spawns. The contract fields:

| Contract field | Answers | Fail-closed default |
|----------------|---------|---------------------|
| `responsibility` | What slice of work it owns (its only identity — no persona) | *required* |
| `allowed_toolsets` | What it may touch — `profile ∩ category ∩ parent ∩ caller` (narrow only) | `[]` (no tools) |
| `workspace_policy` | Where it runs and whether it may mutate | `{kind: scratch, mutate: false}` |
| `verification_policy` | What counts as "done" (`require_evidence`, `on_unverifiable`) | `{require_evidence: true}` |
| `handoff_schema` | The structured result it must hand back | minimal summary |
| `approval_gates` | Outward actions that need explicit human approval | `[push, merge, publish, send_message]` |
| `model`/`provider`/`budget`/`fallbacks` | Routing and runtime caps (identifiers only — never credentials) | inherit parent |

The built-in pack gives you a ready contract per intent. Pass `profile=`; the
contract does the rest:

```python
# implementation — mutates; returns changed files, commands, tests, risks, blockers
delegate_task(goal="Add retry handling to src/client.py and run focused tests",
              profile="implementation")

# review — read-only (no terminal); returns findings, evidence, blockers
delegate_task(goal="Review the changed auth files for correctness and security",
              profile="review")

# research — read-only with web; returns sources, findings, recommendation, confidence
delegate_task(goal="Compare two rate-limit strategies for our gateway",
              profile="research")

# testing — mutates; returns tests added, commands, failures, coverage gaps
delegate_task(goal="Add focused tests for the parser and isolate the failing case",
              profile="testing")
```

A requested-but-unknown profile fails before any child runs with a structured
error naming the valid profiles (`unknown_profile`); a profile marked
`enabled: false` fails with `disabled_profile`. Resolution is pure — no I/O, no
spawn — so contract mistakes are caught up front, not mid-run.

### Manual profile workflow: dry-run → canary → live

Roll a new or changed profile out on a low-blast-radius path before you trust it
broadly. The whole flow is operator-driven; nothing here flips a default for you.

1. **Dry-run the plan.** Preview the graph and profile assignments without
   touching the board:

   ```bash
   hermes team plan "Harden the gateway retry path" --team coding --dry-run
   ```

   This prints the planned cards and per-node profile bindings and creates **no**
   tasks. `delegate_task` itself has no dry-run, but because profile resolution
   is pure, an invalid contract fails fast (`unknown_profile`,
   `disabled_profile`, `unknown_toolset`, `secret_field`, …) before a child
   spawns — run a single read-only delegation to surface any resolution error
   cheaply.

2. **Canary read-only profiles first.** Keep `default_profile: ""` and pass
   `profile=` per call. Exercise `review` and `research` (both `mutate: false`)
   first, so the worst-case canary failure is a no-op read/synthesis run. Promote
   to write-capable (`implementation`, `testing`) and child-spawning
   (`orchestration`) profiles only after the read-only ones behave.

3. **Create cards.** Re-run the plan without `--dry-run` to materialize the
   board (the agent can also call `kanban_create` directly). Each card carries
   its profile binding and approval gates in team metadata:

   ```bash
   hermes team plan "Harden the gateway retry path" --team coding
   ```

4. **Monitor status.** Watch progress, blockers, and which gates are armed:

   ```bash
   hermes team status --team coding
   ```

   `hermes team status` reports per-team card counts, blocked cards, and the
   active **approval gates**. In the TUI, the `/agents` overlay (alias `/tasks`)
   shows the live subagent tree; `kanban_show` / `kanban_list` inspect individual
   cards.

5. **Approval gate.** Gated actions (`push`, `merge`, `publish`, `send_message`)
   are fail-closed: denied unless a human explicitly approves. During canary,
   confirm every gate denies without approval — a single un-gated push is a hard
   stop, not a warning.

6. **Rollback.** Reverting is a config delete, not a migration. Remove
   `capabilities.profiles` (or legacy `capability_profiles`) and clear
   `default_profile` to fall straight back to legacy category/global/inherit
   delegation once the gateway/session loads that config; remove `profile:`
   from team nodes to revert those. There is no resolver database or migration
   state to clean up. Do **not** restart the live gateway/WebUI for rollback
   unless that separate release action is approved.

:::warning Canary & live safety
- **Canary first — and don't restart the live gateway/WebUI to test a profile.** Start the canary process with the profile config already present, keep the live process untouched, and use the WebUI profile surface as read-only inspect/audit only (never edit or execute). Bouncing the live gateway mid-canary risks dropping live sessions and remains a separate release action.
- **Side-by-side Discord bot token.** Never run a canary instance with the **same** `DISCORD_BOT_TOKEN` as your live bot. Hermes uses token-scoped gateway locks so two instances cannot share one bot credential; give any second instance its own token **and** its own `HERMES_HOME`. Reusing the live token collides with the running bot.
- **Secrets stay out of profiles.** Profiles and presets carry provider/model **identifiers only** — never `api_key`, `password`, `base_url`, or auth headers (shown as `[REDACTED]` in docs). Children inherit the parent's resolved credentials, so you almost never set credentials per profile.
- **Clean-room.** Profile names are operator-chosen labels with no built-in behavior; there is no registry of magic persona names. Do not import external personas, prompts, or schemas — `responsibility` plus the explicit fields are the whole identity.
:::

### Troubleshooting category routing

| Symptom | Cause | Fix |
|---------|-------|-----|
| `unknown_category` error listing valid names | Typo or category not defined | Use one of the listed categories, or add it under `categories:` |
| `disabled_category` error | Category has `enabled: false` | Remove the flag or set `enabled: true` |
| `invalid_toolsets_mode` error | A category set `toolsets_mode` to something other than `intersect` | Categories may only narrow scope — set it back to `intersect` |
| Child has **fewer** tools than its category lists | Intersection with parent/requested toolsets — this is by design | Widen the parent's toolsets, or pass matching `toolsets=` on the call |
| `reasoning_effort` ignored, "inheriting parent level" in logs | Value isn't one of `xhigh/high/medium/low/minimal/none` | Use a supported level (see [`hermes_constants.parse_reasoning_effort`]) |
| Subagent times out with zero API calls | Missing/unreachable provider for the category's `provider`/`model` | Check the diagnostic dump in `~/.hermes/logs/`; verify the provider is configured |
| Unknown recipe error before spawn | `recipe:` not in `tools/agent_recipes.py` | Use a valid built-in recipe name |

## Configuration

```yaml
# In ~/.hermes/config.yaml
delegation:
  max_iterations: 50                        # Max turns per child (default: 50)
  # max_concurrent_children: 3              # Parallel children per batch (default: 3)
  # max_spawn_depth: 1                      # Tree depth (floor 1, no ceiling, default 1 = flat). Raise to 2 to allow orchestrator children to spawn leaves; 3+ for deeper trees.
  # orchestrator_enabled: true              # Disable to force all children to leaf role.
  model: "google/gemini-3-flash-preview"             # Optional provider/model override
  provider: "openrouter"                             # Optional built-in provider
  api_mode: anthropic_messages                       # optional; auto-detected from base_url for anthropic_messages endpoints

# Or use a direct custom endpoint instead of provider:
delegation:
  model: "qwen2.5-coder"
  base_url: "http://localhost:1234/v1"
  api_key: "[REDACTED]"
  # api_mode: "anthropic_messages"  # Optional. Wire protocol override for base_url ("chat_completions", "codex_responses", or "anthropic_messages"). Empty = auto-detect from URL (e.g. /anthropic suffix). Set explicitly for endpoints the heuristic can't classify (Azure AI Foundry, MiniMax, Zhipu GLM, LiteLLM proxies, …).
```

When `base_url` points at an Anthropic-compatible endpoint — for example a path ending in `/anthropic`, an Azure Foundry Claude route, or a MiniMax `/anthropic` proxy — `api_mode` is auto-detected as `anthropic_messages` so the subagent uses the right wire format without you setting anything. Set `api_mode` explicitly when the auto-detection guess is wrong (rare).

:::tip
The agent handles delegation automatically based on the task complexity. You don't need to explicitly ask it to delegate — it will do so when it makes sense.
:::
