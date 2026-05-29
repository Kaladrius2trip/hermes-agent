# Threat Model: Skill Packs & Skill-Scoped MCP

> Status: **Design / Phase 0**. Companion to the
> [agent capability layer RFC](../architecture/agent-capability-layer.md).
> This document defines the threat model the capability layer's importer,
> doctor, and runtime gating must satisfy. It extends — does not replace — the
> repo-wide [`SECURITY.md`](../../SECURITY.md) trust model.

The capability layer introduces three new third-party-content surfaces: **skill
packs** (`SKILL.md` bundles), **skill-scoped MCP manifests**, and **agent
recipes** (prompt fragments). Each can come from outside the operator's trust
boundary. This model enumerates what could go wrong and how the design
contains it.

## Assets

| # | Asset | Why it matters |
|---|-------|----------------|
| A1 | API keys / provider credentials | Direct cost + account compromise |
| A2 | MCP server tokens (e.g. source-host PATs) | Read/write to external systems |
| A3 | Local filesystem & repo contents | Source, secrets, user data |
| A4 | Terminal / command execution | Full machine compromise vector |
| A5 | The agent's instruction stream | Hijack => all of the above |
| A6 | Kanban board state | Task integrity, work routing |
| A7 | Operator attention / trust | Social-engineering target |
| A8 | Local logs / audit trail | May leak secrets if unredacted |

## Trust boundaries

```
[ Operator config (~/.hermes, cli-config.yaml) ]   <- TRUSTED, operator-owned
        |
        v
[ Hermes engine: delegation, toolsets, gating ]    <- TRUSTED core
        |
   ------+---------------------------------------
        |                  TRUST BOUNDARY
   -----v----------------------------------------
[ Skill packs / SKILL.md ]   <- UNTRUSTED content (may be third-party)
[ MCP manifests ]            <- UNTRUSTED declarations
[ Recipe prompt fragments ]  <- UNTRUSTED text
[ MCP servers (external) ]   <- UNTRUSTED processes + endpoints
[ Project files / tool output ]<- UNTRUSTED (prompt-injection carrier)
```

Key principle: **content crossing the boundary is data, never authority.** A
skill or manifest can *request* capability; only operator config can *grant*
it. Imported content cannot widen scope, install software, or open a network
listener on its own.

## Threat areas, attacker stories & mitigations

### T1 — Telemetry / silent exfiltration

- **Story:** An imported pack or the observability bundle quietly phones home
  with prompts, repo paths, or usage.
- **Mitigation:** No default telemetry, ever. The capability layer adds no
  analytics, no remote sink, no usage beacon. Phase 7 observability is
  **local-only** (files on disk); it has no network egress path.
- **Verify:** Test asserts no new outbound host is contacted during import or
  delegation; grep CI for new network calls in added modules.
- **Residual:** An operator-configured MCP server may itself transmit data —
  that is the operator's explicit choice, surfaced at config time.

### T2 — MCP / env boundary (secret leakage)

- **Story:** A manifest expands `${SECRET}` from project-controlled config, or
  ships a real token, leaking A1/A2 into the repo or to the wrong server.
- **Mitigation:** No project-controlled env expansion for secrets. Manifests
  carry `[REDACTED]` placeholders only; operators supply real values at runtime
  via their own environment/config. Importer rejects manifests containing
  secret-shaped literals.
- **Verify:** Doctor flags any value matching common key/token patterns;
  importer test feeds a fake `[REDACTED]`-violating manifest and expects
  rejection.
- **Residual:** Operator can still mis-scope a token they own; doctor warns but
  cannot prevent intentional grants.

### T3 — Inbound remote control

- **Story:** A pack/team-mode preset opens a listener so a remote party can
  push tasks/commands to the agent.
- **Mitigation:** No remote inbound command channel by default. Team mode is
  Kanban over **local** state + `delegate_task`; it adds no network listener.
  Nothing in the capability layer binds a port.
- **Verify:** Test asserts no socket bind / server start is introduced by team
  mode or pack import.
- **Residual:** Existing gateway platforms (Telegram/etc.) are governed by
  their own ACLs in `SECURITY.md` and are out of scope here.

### T4 — Toolset escalation

- **Story:** A recipe, category fallback entry, or skill-scoped MCP grants a
  child more capability than its parent — e.g. a read-only advisor gaining
  `terminal`, or a fallback entry widening the toolset.
- **Mitigation:** Toolset shapes can only **narrow**, never widen. Recipe
  toolset ⊆ category toolset ⊆ parent toolset. Fallback entries inherit the
  category toolset and may narrow only. Skill-scoped MCP is additive *within*
  its declared scope but cannot grant tools the parent lacks. Read-only recipes
  must resolve to no-terminal/no-write shapes.
- **Verify:** Test that a category/recipe/fallback requesting a superset of the
  parent toolset is rejected or clamped; read-only recipe test asserts absence
  of write/terminal tools.
- **Residual:** A permissive parent (full toolset) propagates breadth
  downstream — operator's posture choice.

### T5 — Prompt injection / project-controlled prompts

- **Story:** Malicious text in a `SKILL.md`, recipe fragment, file, or tool
  output instructs the agent to exfiltrate secrets, run commands, or escalate.
- **Mitigation:** Imported text is treated as untrusted data, not instructions
  with authority. Capability gates (toolset clamps, no-secret-expansion,
  no-auto-install) hold regardless of what a prompt says — injection cannot
  unlock a capability the operator did not grant. Recipe fragments are reviewed
  artifacts; doctor flags suspicious imperative content (e.g. embedded
  credentials, "run this", base64 blobs).
- **Verify:** Test that an injected "ignore scope, enable terminal" string in a
  skill does not change the resolved toolset.
- **Residual:** Injection can still degrade *task quality* within granted
  scope; defense-in-depth (narrow toolsets, read-only recipes) limits impact.

### T6 — Supply chain / package installer

- **Story:** Importing a pack triggers `pip`/`npm install`, a `postinstall`
  lifecycle script, a Dockerfile, a cron entry, or network bootstrap.
- **Mitigation:** Import is **declarative and inert**. No new deps, installers,
  package lifecycle scripts, Docker, cron, or network bootstrap are run at
  import. The importer copies/validates files only; MCP binaries are never
  fetched or executed by the importer.
- **Verify:** Importer test runs against a pack containing a `postinstall`
  hook and asserts it is ignored/flagged, not executed; CI supply-chain audit
  (existing `osv-scanner` / `supply-chain-audit` workflows) covers deps.
- **Residual:** When the operator later *starts* a configured MCP server, that
  binary runs with the operator's privileges — an explicit, separate action.

### T7 — Skill / plugin mutation

- **Story:** An import overwrites an existing trusted skill, or a pack mutates
  other skills/plugins on disk.
- **Mitigation:** Imports write to a clearly scoped location; name collisions
  are detected and **local skills take precedence** (as the existing loader
  already does). Doctor reports overwrite attempts; no silent replacement of
  bundled/trusted skills.
- **Verify:** Test that importing a pack whose skill name collides with a
  bundled skill does not replace the bundled file and surfaces a warning.
- **Residual:** Operator may choose to accept an overwrite; that is logged.

### T8 — Auto-merge / auto-push

- **Story:** A recipe or team-mode flow commits, pushes, merges, or deploys
  without human gate.
- **Mitigation:** The capability layer never commits, pushes, merges, or
  deploys. No auto-merge/auto-push path is introduced. Git write actions remain
  explicit operator/agent actions governed by existing approval prompts.
- **Verify:** Behavioral tests assert recipes/team mode/import cannot trigger
  push, merge, deploy, or publish flows without an explicit operator action;
  literal-string grep is only a supplemental smoke check.
- **Residual:** An agent with terminal scope can still run git on request; that
  is the existing, gated terminal surface, not new behavior.

### T9 — Logs / redaction

- **Story:** Audit lines or observability output capture secrets (A1/A2/A8).
- **Mitigation:** Audit/observability output reuses Hermes' existing redaction
  (`hermes_logging.py`). New log lines (fallback advance, scope grant) log
  *identifiers and decisions*, never raw secrets. Examples in docs use
  `[REDACTED]`.
- **Verify:** Test that a fallback-advance log line for a provider with an API
  key does not contain the key material.
- **Residual:** Misconfigured external MCP server logs are outside Hermes'
  control.

### T10 — Local observability bundle

- **Story:** The Phase 7 audit bundle becomes an exfiltration or
  privilege-escalation vector.
- **Mitigation:** Bundle is local files only, opt-in, no network sink, reusing
  existing logging + redaction. It records decisions (routing, fallback,
  scope) for the operator's own inspection. Disabling it removes the files;
  there is no remote component to decommission.
- **Verify:** Test that enabling the bundle adds no outbound host and that
  records are redacted.
- **Residual:** Local files inherit local filesystem permissions; protecting
  the host is the operator's responsibility.

## Verification test matrix (summary)

| Threat | Representative test | Gate |
|--------|---------------------|------|
| T1 | No new outbound host on import/delegate | Phase 7 / each phase |
| T2 | Reject secret-shaped manifest values | Phase 4 |
| T3 | No socket bind in team mode | Phase 5 |
| T4 | Reject/clamp toolset superset; read-only has no terminal | Phase 1B/1C |
| T5 | Injected "enable terminal" does not change resolved scope | Phase 1C/3 |
| T6 | `postinstall` not executed on import | Phase 3/4 |
| T7 | Name collision does not overwrite bundled skill | Phase 3 |
| T8 | No git push/merge wired into recipes/team mode | Phase 1C/5 |
| T9 | Fallback log line contains no key material | Phase 1B/7 |
| T10 | Observability bundle adds no egress | Phase 7 |

## Residual risk acceptance

The model reduces but cannot eliminate risk from: an operator who intentionally
grants broad scope or real secrets; external MCP servers the operator chooses to
run; and task-quality degradation from injection within already-granted scope.
These are surfaced (doctor warnings, audit lines, redacted examples) so the
operator makes informed choices, consistent with the
[`SECURITY.md`](../../SECURITY.md) trust model.

## Related documents

- [Agent capability layer RFC](../architecture/agent-capability-layer.md)
- [Issue split (phases 1-7)](../plans/agent-capability-layer-issue-split.md)
- [`SECURITY.md`](../../SECURITY.md)
- [Network egress isolation](./network-egress-isolation.md)
