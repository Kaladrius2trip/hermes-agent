---
name: github-pr-review-readonly
description: "Read-only GitHub PR review; inspect metadata/diffs/checks without posting."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [GitHub, Code-Review, Read-Only, Pull-Requests, Safe]
    related_skills: [github-auth, github-code-review, github-pr-workflow]
    github:
      safety:
        mode: read_only_review
        github_network_mutation_allowed: false
        local_mutation_allowed: false
        token_persistence: forbidden
        live_github_mutation_in_tests: forbidden
        allowed_operations: [repo:metadata, pr:list, pr:view, pr:diff, pr:checks, api:get, git:diff, git:log, git:show]
        forbidden_operations: [issue:comment, issue:create, issue:edit, issue:label, issue:assign, issue:close, issue:reopen, pr:comment, pr:create, pr:review, pr:merge, pr:close, pr:push, repo:write, release:create]
        requires_user_approval_for: [pr:comment, pr:review, pr:create, pr:merge, pr:push, issue:comment, issue:label, issue:close, release:create]
---

# github-pr-review-readonly

Review an existing GitHub PR by reading metadata, diff, checks, and local repository context. Output findings locally. This skill never posts a GitHub review, never comments, never approves, never requests changes, never merges, and never pushes.

Use when user asks for a safe pre-review, second opinion, or release gate that should not touch GitHub state.

## Non-goals

- Do not submit a PR review, approval, request-changes review, or comment.
- Do not checkout or edit the PR branch under this skill.
- Do not push, merge, close, reopen, retarget, label, or assign anything.
- Do not run write-mode REST or GraphQL calls.

## Dangerous operations

Dangerous operations include PR review submission, PR comments, issue comments, approvals, request-changes, merges, branch pushes, label edits, state changes, and any POST/PATCH/PUT/DELETE API call. These require a different skill plus explicit user approval.

## Approval gates

If the user wants findings posted, ask the user for explicit approval and exact posting mode: comment, formal review, approval, or request changes. Then switch to `github-code-review`. This read-only skill stops at local text.

## Token handling

No token persistence. Use existing `gh` authentication only. Do not read, print, copy, create, or store tokens. Do not inspect `.env`, `auth.json`, credential helpers, or token files. If auth is missing, report blocker and ask the user to authenticate.

## Read-only command examples

```bash
gh auth status --hostname github.com
git remote get-url origin
gh pr view 137
gh pr view 137 --json number,title,author,baseRefName,headRefName,mergeable,isDraft,reviewDecision,statusCheckRollup
gh pr diff 137
gh pr checks 137
git log --oneline --decorate -20
git diff main...HEAD --stat
```

```bash
gh api repos/OWNER/REPO/pulls/137
gh api repos/OWNER/REPO/pulls/137/files --paginate
gh api repos/OWNER/REPO/commits/HEAD_SHA/status
```

## Review checklist

- Correctness: behavior matches PR goal, edge cases covered.
- Security: no secret exposure, injection, unsafe auth/authz, path traversal.
- Tests: changed behavior has tests; risky paths have regression coverage.
- Maintainability: names, boundaries, errors, docs.
- CI: summarize failing checks but do not rerun remote jobs unless approved.

## Output shape

Use findings only:

Severity | File:Line | Problem | Fix

End with `Would post only after user approval.` if user asks for GitHub posting.
