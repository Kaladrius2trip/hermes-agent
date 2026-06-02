---
name: github-issue-to-plan
description: "Turn a GitHub issue/PR into a local implementation plan without GitHub writes."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [GitHub, Planning, Read-Only, Issues, Safe]
    related_skills: [github-auth, github-readonly-triage, safe-implementation-branch]
    github:
      safety:
        mode: planning_only
        github_network_mutation_allowed: false
        local_mutation_allowed: true
        token_persistence: forbidden
        live_github_mutation_in_tests: forbidden
        allowed_operations: [issue:view, pr:view, repo:metadata, api:get, local:plan-file]
        forbidden_operations: [issue:comment, issue:create, issue:edit, issue:label, issue:assign, issue:close, issue:reopen, pr:comment, pr:create, pr:review, pr:merge, pr:close, pr:push, repo:write, release:create]
        requires_user_approval_for: [local:plan-file, pr:create, pr:push, pr:merge, issue:comment, issue:label, issue:close]
---

# github-issue-to-plan

Convert a GitHub issue or PR into a local plan. Read the issue/PR body, comments, labels, linked PRs, and repository context, then write or present a local plan with scope, acceptance criteria, tests, risks, and implementation slices.

GitHub remains read-only. The only allowed mutation is local plan creation after user-approved scope.

## Non-goals

- Do not comment the plan back to GitHub.
- Do not label, assign, close, reopen, edit, or create issues.
- Do not create, push, review, or merge PRs.
- Do not start implementation; hand off to `safe-implementation-branch` after plan approval.

## Dangerous operations

Dangerous operations include any GitHub write, PR/issue state change, branch push, merge, and write-mode API call. Publishing the plan as a comment is also dangerous because it mutates GitHub state.

## Approval gates

Ask the user before creating a local plan file if the path is not already specified. Ask again before turning the plan into a branch, PR, comment, label, or merge. Use exact target issue/PR number and local plan path in the approval request.

## Token handling

No token persistence. Use an existing authenticated `gh` session only. Do not read, print, copy, create, or store tokens. Do not inspect `.env`, `auth.json`, credential helpers, or token files. If auth is missing, report blocker and ask the user to authenticate.

## Read-only command examples

```bash
gh auth status --hostname github.com
git remote get-url origin
gh issue view 42 --comments
gh pr view 137
gh api repos/OWNER/REPO/issues/42
gh api repos/OWNER/REPO/issues/42/comments --paginate
```

## Local plan template

Create local plan only after path/scope is known, for example `.hermes/plans/issue-42.md`:

```markdown
# Local plan for issue #42: <title>

## Goal
<one concrete outcome>

## Constraints
- <from issue body/comments>

## Acceptance criteria
- [ ] <testable behavior>

## Implementation slices
1. Add regression test.
2. Make minimal code change.
3. Run focused tests.
4. Run broader checks.

## Approval gates
- PR creation: user approval required.
- Push: user approval required.
- Merge: user approval required.
```

## Output shape

If not writing a file, return the local plan inline. If writing a file, report the exact local path and remind that GitHub was not mutated.
