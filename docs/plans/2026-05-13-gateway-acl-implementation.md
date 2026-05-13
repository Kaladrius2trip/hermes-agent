# Gateway ACL Implementation Plan

> **For Hermes:** Implement this with Claude Code using strict TDD, task-by-task. Keep unrelated dirty files in the main checkout untouched; use an isolated worktree for implementation.

**Goal:** Build a general gateway ACL layer, with Discord as the first platform, so configured bootstrap users can grant scoped DM/channel access and tool/slash-command permissions to regular users.

**Architecture:** Add a small gateway ACL module backed by profile-aware SQLite. Gateway message handling resolves an ACL context per incoming request, filters the agent's visible tools before a run, and enforces the same ACL at tool dispatch as a backstop. Discord reuses current `DISCORD_ALLOWED_USERS` / `allow_from` as bootstrap super-admins; normal users live in the ACL DB.

**Tech Stack:** Python, SQLite stdlib, existing Hermes gateway/session/toolset architecture, pytest.

---

## Source spec

Read first:

- `docs/superpowers/specs/2026-05-13-gateway-acl-design.md`
- `SECURITY.md` sections on trust model and external surfaces
- `gateway/slash_access.py`
- `gateway/session.py::SessionSource`
- `gateway/run.py` around agent construction and slash command dispatch
- `gateway/platforms/discord.py` around allowlist checks
- `model_tools.py::get_tool_definitions` and `model_tools.py::handle_function_call`
- `hermes_cli/tools_config.py::_get_platform_tools`

## Constraints

- Use strict TDD: write failing tests before production code.
- Do not treat ACL as an OS security boundary.
- Do not make Discord roles bootstrap super-admins in v1.
- Do not interrupt already-running tasks when ACL changes.
- Do not grant `/acl` management to ACL `admin` group in v1.
- Preserve existing behavior for configured `DISCORD_ALLOWED_USERS`.
- Keep normal users in SQLite ACL state, not in `DISCORD_ALLOWED_USERS`.
- Avoid touching unrelated existing dirty files.

---

### Task 1: Create ACL storage tests

**Objective:** Define the SQLite persistence contract for groups, subjects, scoped memberships, grants, and audit log.

**Files:**
- Create: `tests/gateway/test_acl_store.py`
- Create later: `gateway/acl.py`

**Step 1: Write failing tests**

Create `tests/gateway/test_acl_store.py` with tests equivalent to:

```python
from gateway.acl import ACLStore


def test_store_initializes_builtin_groups(tmp_path):
    store = ACLStore(tmp_path / "acl.sqlite")

    assert store.group_exists("default")
    assert store.group_exists("admin")
    assert store.get_group("default").built_in is True
    assert store.get_group("admin").built_in is True


def test_can_create_runtime_group(tmp_path):
    store = ACLStore(tmp_path / "acl.sqlite")

    store.create_group("developer", description="Can use dev tools")

    group = store.get_group("developer")
    assert group.name == "developer"
    assert group.description == "Can use dev tools"
    assert group.built_in is False


def test_scoped_user_memberships_are_independent(tmp_path):
    store = ACLStore(tmp_path / "acl.sqlite")

    store.add_user_to_group("discord", "42", "default", scope="dm")

    assert store.groups_for_user("discord", "42", scope="dm") == {"default"}
    assert store.groups_for_user("discord", "42", scope="channel") == set()


def test_group_grants_round_trip(tmp_path):
    store = ACLStore(tmp_path / "acl.sqlite")

    store.grant_access("default", "safe_chat")
    store.grant_access("default", "web")

    assert store.grants_for_group("default") == {"safe_chat", "web"}


def test_audit_log_records_mutations(tmp_path):
    store = ACLStore(tmp_path / "acl.sqlite")

    store.audit(
        requester_platform="discord",
        requester_user_id="1",
        action="user.add",
        summary="Added 42 to default in dm",
        confirmation_id="abc",
    )

    entries = store.audit_entries()
    assert len(entries) == 1
    assert entries[0].requester_user_id == "1"
    assert entries[0].action == "user.add"
```

**Step 2: Run test to verify failure**

Run:

```bash
python -m pytest tests/gateway/test_acl_store.py -q -o 'addopts='
```

Expected: FAIL because `gateway.acl` does not exist.

**Step 3: Implement minimal storage**

Create `gateway/acl.py` with:

- dataclasses: `ACLGroup`, `ACLAuditEntry`
- `ACLStore.__init__(path=None)` using `get_hermes_home() / "acl.sqlite"` by default
- SQLite schema creation in `_init_schema()`
- built-in groups seeded idempotently: `default`, `admin`
- methods used by tests:
  - `group_exists(name)`
  - `get_group(name)`
  - `create_group(name, description="")`
  - `add_user_to_group(platform, user_id, group_name, scope)`
  - `groups_for_user(platform, user_id, scope)`
  - `grant_access(group_name, access_name)`
  - `grants_for_group(group_name)`
  - `audit(...)`
  - `audit_entries(limit=100)`

Use SQLite constraints for idempotency:

- unique group names
- unique subject by `(platform, subject_type, subject_id)`
- unique membership by `(subject_id, group_id, scope)`
- unique grant by `(group_id, access_name)`

**Step 4: Run test to verify pass**

Run:

```bash
python -m pytest tests/gateway/test_acl_store.py -q -o 'addopts='
```

Expected: PASS.

**Step 5: Commit**

```bash
git add gateway/acl.py tests/gateway/test_acl_store.py
git commit -m "feat: add gateway ACL store"
```

---

### Task 2: Add access preset resolution tests

**Objective:** Define how friendly access names resolve to concrete tools and slash permissions.

**Files:**
- Modify: `tests/gateway/test_acl_store.py` or create `tests/gateway/test_acl_resolution.py`
- Modify later: `gateway/acl.py`

**Step 1: Write failing tests**

Create `tests/gateway/test_acl_resolution.py`:

```python
from gateway.acl import ACLStore, resolve_access


def test_default_safe_chat_resolves_to_minimal_tools(tmp_path):
    store = ACLStore(tmp_path / "acl.sqlite")
    store.grant_access("default", "safe_chat")
    store.add_user_to_group("discord", "42", "default", scope="dm")

    access = resolve_access(store, platform="discord", user_id="42", scope="dm")

    assert access.allowed_tools == {"clarify", "todo"}
    assert "terminal" not in access.allowed_tools
    assert "write_file" not in access.allowed_tools
    assert access.can_chat is True


def test_web_access_resolves_to_web_tools(tmp_path):
    store = ACLStore(tmp_path / "acl.sqlite")
    store.create_group("researcher")
    store.grant_access("researcher", "web")
    store.add_user_to_group("discord", "42", "researcher", scope="channel")

    access = resolve_access(store, platform="discord", user_id="42", scope="channel")

    assert {"web_search", "web_extract"}.issubset(access.allowed_tools)


def test_exact_tool_grant_is_supported(tmp_path):
    store = ACLStore(tmp_path / "acl.sqlite")
    store.create_group("custom")
    store.grant_access("custom", "read_file")
    store.add_user_to_group("discord", "42", "custom", scope="dm")

    access = resolve_access(store, platform="discord", user_id="42", scope="dm")

    assert "read_file" in access.allowed_tools


def test_dm_and_channel_access_are_independent(tmp_path):
    store = ACLStore(tmp_path / "acl.sqlite")
    store.grant_access("default", "safe_chat")
    store.add_user_to_group("discord", "42", "default", scope="dm")

    dm_access = resolve_access(store, platform="discord", user_id="42", scope="dm")
    channel_access = resolve_access(store, platform="discord", user_id="42", scope="channel")

    assert dm_access.can_chat is True
    assert channel_access.can_chat is False
```

**Step 2: Run test to verify failure**

Run:

```bash
python -m pytest tests/gateway/test_acl_resolution.py -q -o 'addopts='
```

Expected: FAIL because `resolve_access` does not exist.

**Step 3: Implement minimal resolution**

In `gateway/acl.py`, add:

- `ACLAccess` dataclass:
  - `can_chat: bool`
  - `allowed_tools: set[str]`
  - `allowed_slash_commands: set[str]`
  - `missing_reason: str | None = None`
- `ACCESS_PRESETS` mapping:
  - `safe_chat`: tools `clarify`, `todo`
  - `web`: `web_search`, `web_extract`
  - `file_read`: `read_file`, `search_files`
  - `file_write`: `write_file`, `patch`
  - `terminal`: `terminal`, `process`
  - `code_execution`: `execute_code`
  - `memory`: `memory`
  - `cronjob`: `cronjob`
  - `messaging`: `send_message`
  - `discord_admin`: `discord`, `discord_admin`
- `resolve_access(store, platform, user_id, scope)` that unions group grants for that scope.
- Exact tool names should pass through. Use `model_tools.get_all_tool_names()` defensively if importable, but do not fail tests if registry import fails; unknown names can remain as literal grants for now.

**Step 4: Run test to verify pass**

Run:

```bash
python -m pytest tests/gateway/test_acl_resolution.py tests/gateway/test_acl_store.py -q -o 'addopts='
```

Expected: PASS.

**Step 5: Commit**

```bash
git add gateway/acl.py tests/gateway/test_acl_resolution.py tests/gateway/test_acl_store.py
git commit -m "feat: resolve gateway ACL access"
```

---

### Task 3: Add bootstrap super-admin resolution tests

**Objective:** Reuse current configured user allowlist as ACL bootstrap/super-admin authority.

**Files:**
- Modify: `gateway/acl.py`
- Create/modify: `tests/gateway/test_acl_bootstrap.py`

**Step 1: Write failing tests**

Create `tests/gateway/test_acl_bootstrap.py`:

```python
from types import SimpleNamespace

from gateway.acl import is_bootstrap_super_admin
from gateway.config import Platform
from gateway.session import SessionSource


class PlatformConfig:
    def __init__(self, extra):
        self.extra = extra


class GatewayConfig:
    def __init__(self, extra):
        self.platforms = {Platform.DISCORD: PlatformConfig(extra)}


def test_discord_allow_from_user_is_bootstrap_super_admin():
    source = SessionSource(platform=Platform.DISCORD, chat_id="c", user_id="42", chat_type="dm")
    cfg = GatewayConfig({"allow_from": ["42"]})

    assert is_bootstrap_super_admin(cfg, source) is True


def test_discord_allowed_role_is_not_bootstrap_super_admin():
    source = SessionSource(platform=Platform.DISCORD, chat_id="c", user_id="42", chat_type="dm")
    cfg = GatewayConfig({"allow_from": [], "allowed_roles": ["999"]})

    assert is_bootstrap_super_admin(cfg, source) is False


def test_non_matching_user_is_not_bootstrap_super_admin():
    source = SessionSource(platform=Platform.DISCORD, chat_id="c", user_id="99", chat_type="dm")
    cfg = GatewayConfig({"allow_from": ["42"]})

    assert is_bootstrap_super_admin(cfg, source) is False
```

**Step 2: Run test to verify failure**

Run:

```bash
python -m pytest tests/gateway/test_acl_bootstrap.py -q -o 'addopts='
```

Expected: FAIL because helper does not exist.

**Step 3: Implement bootstrap helper**

In `gateway/acl.py`, add:

- `is_bootstrap_super_admin(gateway_config, source)`
- It should inspect the source platform's config `extra`.
- It should accept `allow_from` and, if available in code paths, any normalized platform allowlist list used by current gateway config.
- It should compare stringified user IDs.
- It should not treat roles as super-admins.
- If config is missing, return False.

**Step 4: Run test to verify pass**

Run:

```bash
python -m pytest tests/gateway/test_acl_bootstrap.py tests/gateway/test_acl_resolution.py tests/gateway/test_acl_store.py -q -o 'addopts='
```

Expected: PASS.

**Step 5: Commit**

```bash
git add gateway/acl.py tests/gateway/test_acl_bootstrap.py
git commit -m "feat: resolve ACL bootstrap admins"
```

---

### Task 4: Add gateway ACL decision tests

**Objective:** Define per-message authorization and effective tool filtering without wiring the whole gateway yet.

**Files:**
- Modify: `gateway/acl.py`
- Create: `tests/gateway/test_acl_decision.py`

**Step 1: Write failing tests**

Create `tests/gateway/test_acl_decision.py`:

```python
from gateway.acl import ACLStore, acl_decision_for_source
from gateway.config import Platform
from gateway.session import SessionSource


class PlatformConfig:
    def __init__(self, extra):
        self.extra = extra


class GatewayConfig:
    def __init__(self, extra):
        self.platforms = {Platform.DISCORD: PlatformConfig(extra)}


def test_unknown_non_bootstrap_user_has_no_access(tmp_path):
    store = ACLStore(tmp_path / "acl.sqlite")
    source = SessionSource(platform=Platform.DISCORD, chat_id="c", user_id="42", chat_type="dm")

    decision = acl_decision_for_source(GatewayConfig({"allow_from": ["1"]}), store, source)

    assert decision.can_chat is False
    assert decision.is_bootstrap_super_admin is False


def test_default_user_can_chat_in_dm_only(tmp_path):
    store = ACLStore(tmp_path / "acl.sqlite")
    store.grant_access("default", "safe_chat")
    store.add_user_to_group("discord", "42", "default", scope="dm")

    dm = SessionSource(platform=Platform.DISCORD, chat_id="dm", user_id="42", chat_type="dm")
    channel = SessionSource(platform=Platform.DISCORD, chat_id="ch", user_id="42", chat_type="channel")

    assert acl_decision_for_source(GatewayConfig({"allow_from": ["1"]}), store, dm).can_chat is True
    assert acl_decision_for_source(GatewayConfig({"allow_from": ["1"]}), store, channel).can_chat is False


def test_bootstrap_super_admin_has_full_platform_tools(tmp_path):
    store = ACLStore(tmp_path / "acl.sqlite")
    source = SessionSource(platform=Platform.DISCORD, chat_id="c", user_id="1", chat_type="dm")

    decision = acl_decision_for_source(GatewayConfig({"allow_from": ["1"]}), store, source, platform_tools={"web", "terminal"})

    assert decision.can_chat is True
    assert decision.is_bootstrap_super_admin is True
    assert decision.enabled_toolsets == {"web", "terminal"}
```

**Step 2: Run test to verify failure**

Run:

```bash
python -m pytest tests/gateway/test_acl_decision.py -q -o 'addopts='
```

Expected: FAIL because `acl_decision_for_source` does not exist.

**Step 3: Implement decision helper**

In `gateway/acl.py`, add:

- `ACLDecision` dataclass:
  - `can_chat`
  - `is_bootstrap_super_admin`
  - `allowed_tools`
  - `enabled_toolsets`
  - `allowed_slash_commands`
  - `scope`
  - `denial_reason`
- `scope_for_source(source)`: `dm` if `source.chat_type == "dm"`, otherwise `channel`.
- `acl_decision_for_source(gateway_config, store, source, platform_tools=None)`:
  - bootstrap super-admin: can chat; gets platform tools unchanged.
  - non-bootstrap: resolve scoped ACL access; can chat only if `can_chat` true; enabled tools are derived from `allowed_tools`.
  - For v1, the effective `enabled_toolsets` for non-bootstrap users may be exact tool names or synthetic toolset names only if supported by `model_tools`. Prefer later filtering by exact tool names to avoid requiring one-tool toolsets.

**Step 4: Run tests**

Run:

```bash
python -m pytest tests/gateway/test_acl_decision.py tests/gateway/test_acl_bootstrap.py tests/gateway/test_acl_resolution.py tests/gateway/test_acl_store.py -q -o 'addopts='
```

Expected: PASS.

**Step 5: Commit**

```bash
git add gateway/acl.py tests/gateway/test_acl_decision.py
git commit -m "feat: add gateway ACL decisions"
```

---

### Task 5: Add exact-tool schema filtering support

**Objective:** Let gateway create an agent with an exact allowed tool set, not only toolsets.

**Files:**
- Modify: `model_tools.py`
- Modify: `run_agent.py`
- Create/modify: `tests/tools/test_model_tools_acl_filter.py`

**Step 1: Write failing tests**

Create `tests/tools/test_model_tools_acl_filter.py`:

```python
from model_tools import get_tool_definitions


def _names(tools):
    return {t["function"]["name"] for t in tools}


def test_get_tool_definitions_filters_exact_allowed_tools():
    tools = get_tool_definitions(enabled_toolsets=["hermes-discord"], allowed_tools={"web_search", "todo"}, quiet_mode=True)

    assert _names(tools) <= {"web_search", "todo"}
    assert "web_search" in _names(tools)
    assert "terminal" not in _names(tools)
```

If `allowed_tools` conflicts with existing API, name it `allowed_tool_names`. Pick one name and use consistently.

**Step 2: Run test to verify failure**

Run:

```bash
python -m pytest tests/tools/test_model_tools_acl_filter.py -q -o 'addopts='
```

Expected: FAIL because `get_tool_definitions` does not accept the new parameter.

**Step 3: Implement exact tool filtering**

In `model_tools.py`:

- Add optional parameter `allowed_tools: Optional[Iterable[str]] = None` to `get_tool_definitions` and `_compute_tool_definitions`.
- Include `allowed_tools` in the quiet-mode cache key.
- After resolving `tools_to_include` from toolsets and disabled toolsets, intersect with `allowed_tools` if provided.
- Preserve existing dynamic schema behavior for `execute_code`, browser, Discord schemas, etc.

In `run_agent.py`:

- Add optional `allowed_tools` parameter to `AIAgent.__init__`.
- Store it on `self`.
- Pass it to `get_tool_definitions`.
- Ensure `self.valid_tool_names` remains based on final filtered schemas.

**Step 4: Run tests**

Run:

```bash
python -m pytest tests/tools/test_model_tools_acl_filter.py -q -o 'addopts='
```

Expected: PASS.

**Step 5: Commit**

```bash
git add model_tools.py run_agent.py tests/tools/test_model_tools_acl_filter.py
git commit -m "feat: support exact tool filtering"
```

---

### Task 6: Wire ACL schema filtering into gateway agent creation

**Objective:** Apply ACL decisions before each gateway agent run, without affecting bootstrap super-admins.

**Files:**
- Modify: `gateway/run.py`
- Create/modify: `tests/gateway/test_gateway_acl_agent_filter.py`

**Step 1: Write failing tests**

Create a gateway-level test with mocks around the agent constructor. Use existing tests such as `tests/gateway/test_compress_focus.py` as a pattern for patching `run_agent.AIAgent` if needed.

Required behaviors:

```python
# Pseudocode shape; adapt to existing GatewayRunner construction helpers.

def test_acl_default_user_agent_gets_only_safe_tools(monkeypatch, tmp_path):
    # Arrange ACLStore with user 42 in default/dm and safe_chat grant.
    # Arrange GatewayRunner config where user 42 is not bootstrap super-admin.
    # Patch run_agent.AIAgent to capture kwargs and return final_response.
    # Act: process a DM MessageEvent from user 42.
    # Assert: AIAgent kwargs include allowed_tools == {"clarify", "todo"}.


def test_bootstrap_super_admin_does_not_receive_acl_tool_restriction(monkeypatch, tmp_path):
    # Arrange source user_id in allow_from.
    # Assert AIAgent allowed_tools is None or unrestricted.
```

**Step 2: Run test to verify failure**

Run the new test file. Expected: FAIL because gateway does not compute/pass ACL decisions yet.

**Step 3: Implement gateway filtering**

In `gateway/run.py`:

- Instantiate `ACLStore` lazily or via a runner helper, e.g. `_get_acl_store()`.
- Add helper `_acl_decision_for_event_or_source(source, platform_tools)` that calls `gateway.acl.acl_decision_for_source`.
- In the normal agent creation path, after `_get_platform_tools`, compute the ACL decision.
- If ACL is disabled/not configured, preserve existing behavior. If enabling flag is needed, use config key such as `gateway.acl.enabled` defaulting to False for compatibility. If the product owner accepts immediate behavior change, default can be True only for Discord when DB exists; prefer opt-in in implementation.
- If decision denies chat, return/send a short denial before creating AIAgent.
- Pass `allowed_tools=decision.allowed_tools` to AIAgent for non-bootstrap ACL users.
- Do not restrict bootstrap super-admins.

**Step 4: Run tests**

Run:

```bash
python -m pytest tests/gateway/test_gateway_acl_agent_filter.py tests/tools/test_model_tools_acl_filter.py -q -o 'addopts='
```

Expected: PASS.

**Step 5: Commit**

```bash
git add gateway/run.py tests/gateway/test_gateway_acl_agent_filter.py
git commit -m "feat: filter gateway tools by ACL"
```

---

### Task 7: Add dispatch-time ACL backstop

**Objective:** Deny accidental unauthorized tool calls even if schema filtering misses a path.

**Files:**
- Modify: `model_tools.py` or `run_agent.py`
- Modify: `gateway/acl.py`
- Create/modify: `tests/tools/test_acl_tool_dispatch.py`

**Step 1: Write failing tests**

Create `tests/tools/test_acl_tool_dispatch.py`:

```python
import json

from model_tools import handle_function_call


def test_handle_function_call_denies_tool_not_in_allowed_tools():
    result = handle_function_call("terminal", {"command": "echo nope"}, allowed_tools={"todo"})
    payload = json.loads(result)

    assert payload["error"]
    assert "terminal" in payload["error"]
    assert "Ask a Hermes super-admin" in payload["error"]
```

If `handle_function_call` already uses `enabled_tools` for execute_code, either reuse `enabled_tools` as the dispatch allowlist for all tools or add a clearer `allowed_tools` parameter. Prefer reusing `enabled_tools` carefully only if it does not break existing behavior.

**Step 2: Run test to verify failure**

Run:

```bash
python -m pytest tests/tools/test_acl_tool_dispatch.py -q -o 'addopts='
```

Expected: FAIL because non-`execute_code` calls are not denied based on enabled tools.

**Step 3: Implement backstop**

In `model_tools.py::handle_function_call`:

- Before plugin pre-tool-call hooks and registry dispatch, if an allowlist parameter is provided and `function_name` is not in it, return JSON error:
  - `{"error": "This requires access to '<tool>'. Ask a Hermes super-admin for access."}`
- Preserve `_AGENT_LOOP_TOOLS` behavior.
- Do not apply this restriction when the allowlist parameter is None.

In `run_agent.py`, ensure calls to `handle_function_call` pass the filtered `self.valid_tool_names` or the explicit ACL allowed tools. Since `self.valid_tool_names` already reflects schema filtering, this should work as a backstop for agent-loop tool calls.

**Step 4: Run tests**

Run:

```bash
python -m pytest tests/tools/test_acl_tool_dispatch.py tests/tools/test_model_tools_acl_filter.py -q -o 'addopts='
```

Expected: PASS.

**Step 5: Commit**

```bash
git add model_tools.py run_agent.py tests/tools/test_acl_tool_dispatch.py
git commit -m "feat: enforce ACL at tool dispatch"
```

---

### Task 8: Add structured `/acl` parser and confirmation model

**Objective:** Parse structured `/acl` mutations into pending operations without applying them immediately.

**Files:**
- Modify: `gateway/acl.py`
- Create: `tests/gateway/test_acl_commands.py`

**Step 1: Write failing tests**

Create tests:

```python
from gateway.acl import parse_acl_command


def test_parse_user_add_with_scope():
    op = parse_acl_command("user add 42 default --scope dm")

    assert op.kind == "user.add"
    assert op.target_user_id == "42"
    assert op.group_name == "default"
    assert op.scope == "dm"


def test_parse_group_grant():
    op = parse_acl_command("group grant developer web")

    assert op.kind == "group.grant"
    assert op.group_name == "developer"
    assert op.access_name == "web"


def test_parse_human_chat_dm_request():
    op = parse_acl_command("give 42 ability to chat with you in DM")

    assert op.kind == "user.add"
    assert op.target_user_id == "42"
    assert op.group_name == "default"
    assert op.scope == "dm"
```

**Step 2: Run test to verify failure**

Run:

```bash
python -m pytest tests/gateway/test_acl_commands.py -q -o 'addopts='
```

Expected: FAIL because parser does not exist.

**Step 3: Implement minimal parser**

In `gateway/acl.py`, add:

- `ACLMutation` dataclass with fields used by tests plus `summary` and `risk_label`.
- `parse_acl_command(text)` supporting:
  - `group add <name>`
  - `group remove <name>`
  - `group grant <name> <access>`
  - `group revoke <name> <access>`
  - `user add <id-or-mention> <group> --scope <dm|channel>`
  - `user remove <id-or-mention> <group> --scope <dm|channel>`
  - minimal human-friendly phrases from the spec.
- Mention normalization for `<@123>` and `<@!123>`.
- Ambiguous scope should produce an operation requiring scope clarification or an error; do not silently mutate.

**Step 4: Run tests**

Run:

```bash
python -m pytest tests/gateway/test_acl_commands.py -q -o 'addopts='
```

Expected: PASS.

**Step 5: Commit**

```bash
git add gateway/acl.py tests/gateway/test_acl_commands.py
git commit -m "feat: parse ACL commands"
```

---

### Task 9: Wire `/acl` into slash command handling with requester-only confirmation

**Objective:** Super-admins can propose ACL mutations; mutations apply only after approval.

**Files:**
- Modify: `gateway/run.py`
- Modify: `gateway/platforms/discord.py` if button metadata handling is platform-specific
- Create/modify: `tests/gateway/test_acl_command_flow.py`

**Step 1: Write failing tests**

Test at runner/helper level, not live Discord:

- non-super-admin `/acl user add 42 default --scope dm` returns denial
- super-admin `/acl user add 42 default --scope dm` creates pending confirmation and does not mutate DB yet
- approve applies DB change
- cancel leaves DB unchanged
- confirmation stores requester user id and refuses approval by another user

Use existing gateway button/approval tests as patterns.

**Step 2: Run test to verify failure**

Run:

```bash
python -m pytest tests/gateway/test_acl_command_flow.py -q -o 'addopts='
```

Expected: FAIL because command flow is not wired.

**Step 3: Implement command flow**

In `gateway/run.py`:

- Add `/acl` command handling near slash command dispatch.
- Check `is_bootstrap_super_admin` before parsing mutation.
- Implement read-only subcommands immediately:
  - `show user <id-or-mention>`
  - `show group <name>`
  - `audit [target]`
- For mutations:
  - parse into `ACLMutation`
  - create pending confirmation with requester id, operation, expiry
  - send confirmation text/buttons where adapter supports it
- Add approval/cancel handlers for ACL confirmation ids.
- Ensure only original requester can approve/cancel.
- On approve, apply mutation through `ACLStore`, then audit.

**Step 4: Run tests**

Run:

```bash
python -m pytest tests/gateway/test_acl_command_flow.py tests/gateway/test_acl_commands.py tests/gateway/test_acl_store.py -q -o 'addopts='
```

Expected: PASS.

**Step 5: Commit**

```bash
git add gateway/run.py gateway/platforms/discord.py gateway/acl.py tests/gateway/test_acl_command_flow.py
git commit -m "feat: add ACL command flow"
```

---

### Task 10: Add docs and whoami/show output

**Objective:** Make effective access understandable to users and admins.

**Files:**
- Modify: `website/docs/user-guide/messaging/discord.md`
- Modify: `gateway/run.py` if `/whoami` includes access summary
- Create/modify tests as needed

**Step 1: Write failing or snapshot-style tests**

Add tests for helper formatting if existing `/whoami` tests are available:

- default user in DM sees `scope=dm`, groups `default`, tools `clarify,todo`
- channel-only user in DM sees no access
- bootstrap super-admin sees bootstrap status

**Step 2: Run tests to verify failure**

Run targeted tests.

**Step 3: Implement formatting/docs**

- Add `/acl show user` and `/acl show group` output if not already covered.
- Add `/whoami` effective ACL summary where appropriate.
- Update Discord docs:
  - explain `DISCORD_ALLOWED_USERS` as bootstrap super-admin list when ACL is enabled
  - show `/acl` examples for DM and channel
  - explain default/admin groups
  - explain high-risk grants, especially terminal/execute_code/cron/messaging

**Step 4: Run tests**

Run targeted gateway tests.

**Step 5: Commit**

```bash
git add website/docs/user-guide/messaging/discord.md gateway/run.py tests/gateway/*acl* tests/gateway/*whoami*
git commit -m "docs: document Discord ACL"
```

---

### Task 11: Run focused regression suite

**Objective:** Verify ACL changes do not break gateway/tool behavior.

**Files:** None expected.

**Step 1: Run ACL tests**

```bash
python -m pytest \
  tests/gateway/test_acl_store.py \
  tests/gateway/test_acl_resolution.py \
  tests/gateway/test_acl_bootstrap.py \
  tests/gateway/test_acl_decision.py \
  tests/gateway/test_acl_commands.py \
  tests/gateway/test_acl_command_flow.py \
  tests/gateway/test_gateway_acl_agent_filter.py \
  tests/tools/test_model_tools_acl_filter.py \
  tests/tools/test_acl_tool_dispatch.py \
  -q -o 'addopts='
```

Expected: PASS.

**Step 2: Run existing related tests**

```bash
python -m pytest \
  tests/gateway/test_slash_access.py \
  tests/gateway/test_discord_roles_dm_scope.py \
  tests/gateway/test_compress_focus.py \
  tests/tools/test_delegate_composite_toolsets.py \
  tests/test_toolsets.py \
  -q -o 'addopts='
```

Expected: PASS.

**Step 3: Run broader gateway/tools tests if time permits**

```bash
python -m pytest tests/gateway tests/tools -q -o 'addopts='
```

Expected: PASS or only documented pre-existing failures.

**Step 4: Commit any final fixes**

If fixes are needed, use TDD for each fix and commit separately.

---

### Task 12: Final review and handoff

**Objective:** Confirm implementation matches the spec and is safe to review.

**Step 1: Inspect diff**

```bash
git diff --stat main...HEAD
git diff main...HEAD -- gateway/acl.py gateway/run.py model_tools.py run_agent.py
```

**Step 2: Check for placeholders**

```bash
grep -R "TODO\|TBD\|pass  #\|NotImplemented" gateway/acl.py tests/gateway/test_acl* tests/tools/test_acl* tests/tools/test_model_tools_acl_filter.py || true
```

Expected: no implementation placeholders.

**Step 3: Verify commit history**

```bash
git log --oneline main..HEAD
```

Expected: each task has a focused commit.

**Step 4: Summarize**

Prepare final summary:

- what changed
- how bootstrap users work
- how DM vs channel scoping works
- what tests passed
- known limitations: ACL is not an OS security boundary; terminal remains high-risk; resource scoping is future work
