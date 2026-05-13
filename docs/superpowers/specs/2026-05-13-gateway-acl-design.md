# Gateway ACL Design

Date: 2026-05-13
Status: Approved design draft
Scope: General gateway ACL layer, with Discord as the first implementation

## 1. Scope and trust model

Hermes needs a gateway-level access control list (ACL) so shared-platform users are not all treated as equally trusted after they pass the initial platform allowlist.

The ACL answers two questions:

1. Can this caller talk to Hermes at all?
2. If yes, what tools and slash commands can this caller use?

This design targets gateway platforms generally, with Discord implemented first. It is not a replacement for OS isolation. Hermes' security model treats OS-level isolation as the only hard security boundary against adversarial model behavior. Tool allowlists, approval gates, redaction, and this ACL are authorization and accident-prevention layers. High-risk deployments should still combine ACL with Docker, OpenShell, or another whole-process sandbox.

Initial behavior:

- Unknown/random Discord users have no access.
- Authorized default users get safe chat only.
- Elevated users/groups receive additional ACL grants.
- Bootstrap super-admins come from the current configured user allowlist.
- ACL changes are re-evaluated on every future message/run.
- Already-running tasks are not interrupted by ACL changes in v1.

This preserves the current network-surface requirement that callers must be authorized, while removing the current all-authorized-callers-are-equally-trusted behavior for gateway platforms that enable ACL.

## 2. Bootstrap super-admins

Discord v1 should reuse the current user-based allowlist as the bootstrap authority.

For Discord:

- Users in `DISCORD_ALLOWED_USERS` or platform `allow_from` are Hermes ACL super-admins.
- Super-admins can run `/acl` management commands.
- Discord server administrators are not automatically Hermes ACL administrators.
- Normal users are not added to `DISCORD_ALLOWED_USERS`; they are granted access through the ACL database.
- `DISCORD_ALLOWED_ROLES` should not grant ACL super-admin status in v1. Role-based regular access can be represented later as ACL subjects.

After ACL exists, `DISCORD_ALLOWED_USERS` should be documented as the bootstrap/super-admin list, not the place to add all regular bot users.

## 3. Data model and storage

Mutable ACL state lives in a dedicated SQLite database under Hermes home, using `get_hermes_home()` so it is profile-aware. Suggested path: `acl.sqlite`.

Built-in ACL groups:

- `default`
  - Safe-chat access for regular authorized users.
  - Human-friendly requests like “give @user ability to chat with you” map to adding the user to `default`.
- `admin`
  - Broad agent/tool access preset.
  - Does not grant `/acl` management in v1. Only bootstrap super-admins manage ACL.

Runtime-created groups:

- Any other group can be created with `/acl`, for example `developer`, `researcher`, or `moderator`.
- Runtime groups can receive friendly access names/toolsets or exact tool names.

Scopes:

- ACL distinguishes private DM access from server/channel access.
- Minimum v1 scopes are `dm` and `channel`.
- A user can have different memberships in each scope.
- Examples:
  - DM: `default`, channel: no access.
  - DM: `default`, channel: `researcher`.
  - DM: `admin`, channel: `default`.

Core entities:

- `acl_groups`
  - group name
  - description
  - built-in flag
  - timestamps
- `acl_subjects`
  - platform, e.g. `discord`
  - subject type, `user` in v1
  - subject id, e.g. Discord user ID
- `acl_memberships`
  - subject id
  - group id
  - scope: `dm` or `channel`
- `acl_grants`
  - group id
  - access name, toolset name, or exact tool name
  - v1 grants are group-global; scope is handled by scoped memberships
- `acl_audit_log`
  - requester platform/user id
  - action
  - before/after summary
  - confirmation id
  - timestamp

Access names:

- Admin-facing access can be friendly: `safe_chat`, `web`, `research`, `file_read`, `developer`, `terminal`.
- Internally, access names resolve to toolsets and individual tool names.
- Exact tool names remain supported for advanced admins.

## 4. Enforcement flow

ACL is enforced in two places.

### 4.1 Schema filtering before the agent run

On every incoming gateway message, Hermes resolves:

- platform
- caller user id
- scope: `dm` or `channel`
- group memberships for that scope
- grants from those groups
- final allowed tool names and slash commands

The agent is created with only the allowed tools/toolsets. Default users should not see risky tools in the model schema.

### 4.2 Tool-dispatch backstop

Every tool call is checked against the current request's ACL context. If the tool is not allowed:

- the tool call is denied
- the audit log records the denied attempt
- the final response tells the user which capability is missing and asks them to contact a Hermes super-admin

This protects against stale sessions, plugins, MCP tools, bugs, or future paths that accidentally expose a tool.

### 4.3 Slash command enforcement

Existing slash command gating should be unified with ACL.

- `/acl` is special and can only be run by bootstrap super-admins from the current configured user allowlist.
- ACL `admin` group does not grant `/acl` management in v1.
- Other slash commands map to access names or capabilities:
  - `/cron` requires `cronjob` or future `cron.manage`.
  - `/background` requires a background-run permission and the effective tool grants for the spawned run.
  - `/model` requires a model-change permission.
  - Skill slash commands require skill-use permission or the tool/capability set the skill needs.

If a command is denied, the user should see a clear message such as: “This requires `<capability>`. Ask a Hermes super-admin for access.”

### 4.4 Authorization path

- Bootstrap allowlist users are super-admins and bypass normal ACL restrictions for Discord v1.
- Non-bootstrap users are checked against the ACL database.
- If the non-bootstrap user has no membership for the current scope, they have no access.
- If the user has memberships for the current scope, their allowed tools and commands are derived from the grants on those groups.

ACL is re-evaluated on every message. Changes affect future messages/runs immediately. Running tasks are not cancelled in v1.

## 5. `/acl` command UX

`/acl` is available only to bootstrap super-admins.

It supports structured commands and human-friendly requests.

Structured examples:

- `/acl group add developer`
- `/acl group remove developer`
- `/acl group grant developer web`
- `/acl group revoke developer terminal`
- `/acl user add @user default --scope dm`
- `/acl user add @user developer --scope channel`
- `/acl user remove @user developer --scope channel`
- `/acl show user @user`
- `/acl show group developer`
- `/acl audit @user`

Human-friendly examples:

- `/acl give @user ability to chat with you in DM`
  - normalized operation: add user to `default` in `dm`
- `/acl let @user chat in this channel`
  - normalized operation: add user to `default` in `channel`
- `/acl give @user web research access in this channel`
  - normalized operation: add user to an appropriate group or propose a group/grant change
- `/acl create a developer group that can use web and file read`
  - normalized operation: create group and grant access names

Every mutation requires confirmation. The confirmation must be visible/actionable only to the requester.

Discord confirmation behavior:

- Use ephemeral interactions when the command arrives through native Discord slash commands.
- If the command arrives as normal message text, send a confirmation with buttons that only the requester can use.
- The confirmation shows the exact normalized operation:
  - requester
  - target user/group
  - scope
  - grants/memberships changed
  - risk label, if applicable
- Buttons: Approve and Cancel.
- Pending confirmations expire after a short timeout.
- Proposed, approved, cancelled, and timed-out operations are logged.

Denied `/acl` requests should be short: “Only configured Hermes super-admins can manage ACL.”

## 6. Built-in access presets and risk labels

Built-in groups:

- `default`
  - safe chat only
  - no terminal, file write, memory mutation, cron, messaging, browser automation, Discord admin, or environment access
- `admin`
  - broad agent/tool access
  - no `/acl` management in v1

Suggested built-in access presets:

- `safe_chat`
  - basic conversation with `clarify` and `todo`
- `web`
  - `web_search` and `web_extract`
- `research`
  - web plus read-only browser/page extraction if enabled
- `file_read`
  - `read_file`, `search_files`
- `file_write`
  - `write_file`, `patch`
- `terminal`
  - `terminal`, `process`
  - high-risk
- `code_execution`
  - `execute_code`
  - high-risk because it can run Python and orchestrate tool calls
- `memory`
  - memory tool access
  - high privacy impact
- `cronjob`
  - create/update/remove scheduled jobs
  - high-risk because it creates durable autonomous behavior
- `messaging`
  - `send_message`
  - high-risk because it can send to external channels/people
- `discord_admin`
  - Discord moderation/admin tools
  - high-risk and platform-specific

Risk labels:

- Low: safe chat, web search
- Medium: file read, browser
- High: file write, terminal, execute_code, memory, cronjob, messaging, Discord admin, MCP tools by default

Important v1 rule:

- A user with `terminal` should be treated as highly trusted.
- Limiting `uv` and arbitrary scripts in v1 is handled by denying `terminal` and `execute_code` unless the user is trusted.
- Future versions can add resource-level constraints such as allowed working directories, command allowlists, environment variable policies, network egress policy, and per-group sandboxing.

## 7. Future capability model

V1 may store grants as friendly access names and exact tool names, but the resolver should be structured so simple grants can later become capabilities such as:

- `chat.use`
- `web.search`
- `fs.read`
- `fs.write`
- `shell.exec`
- `env.read`
- `cron.manage`
- `memory.write`

This avoids redesign when Hermes adds more risky features or needs argument/resource-level restrictions.

Future resource controls can include:

- path prefix allowlists for file tools
- read/write separation for filesystem access
- command allowlists for terminal, e.g. allowing `uv run pytest` without arbitrary shell
- environment-variable allow/deny lists
- network target/egress restrictions
- Docker/OpenShell/whole-process sandbox selection per ACL group

## 8. Testing and rollout

Unit tests for ACL storage:

- creates built-in `default` and `admin`
- creates runtime groups
- assigns/removes scoped memberships
- grants/revokes access names
- writes audit entries

Unit tests for access resolution:

- unknown user has no access
- bootstrap allowlist user is super-admin
- same user can have DM access but no channel access
- same user can have different DM/channel groups
- group grants resolve to individual tools

Gateway tests:

- Discord non-allowlist and no ACL membership cannot dispatch work
- ACL `default` user can safe-chat but cannot call tools
- channel access does not imply DM access, and DM access does not imply channel access
- denied tool call reports the missing capability
- slash commands and natural-language tool use share the same ACL decision

`/acl` tests:

- only bootstrap super-admins can invoke mutations
- every mutation creates requester-only confirmation
- approving applies the DB change
- cancelling or timeout applies nothing
- human-friendly phrases normalize to expected operations

Regression tests:

- existing installs without ACL DB still allow current `DISCORD_ALLOWED_USERS` to work as before
- existing slash access behavior is preserved or migrated intentionally

Rollout:

- Add ACL as opt-in initially if compatibility risk is high.
- When enabled, `DISCORD_ALLOWED_USERS` becomes the bootstrap/super-admin list for Discord ACL management.
- Regular users should be granted through `/acl`.
- Provide documentation examples:
  - add user to `default` in DM
  - add user to `default` in channel
  - create a developer group
  - grant web/file_read
  - explain why terminal is high-risk
- Add clear `/acl show` and `/whoami` output so users and admins can understand effective access.

## 9. Open implementation notes

- The existing `gateway/slash_access.py` provides a useful pattern for DM vs group scoping, but v1 ACL should not remain slash-only.
- The existing platform toolset resolver is platform-wide. ACL needs a per-request effective tool filter layered on top.
- Enforcement should avoid only hiding tool schemas. Dispatch-time enforcement is required as a safety backstop.
- The audit log must record the originating platform user, not just that the bot took an action.
- Plugin and MCP tools should default to high risk unless explicitly granted or mapped to a known capability.
