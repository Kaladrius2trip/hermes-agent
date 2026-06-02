---
name: safe-implementation-branch
description: "Create an isolated local git branch/worktree with explicit gates for PR/push/merge."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [GitHub, Git, Branching, Worktree, Safe]
    related_skills: [github-auth, github-issue-to-plan, github-pr-workflow]
    github:
      safety:
        mode: local_branch_only
        github_network_mutation_allowed: false
        local_mutation_allowed: true
        token_persistence: forbidden
        live_github_mutation_in_tests: forbidden
        allowed_operations: [repo:metadata, git:fetch, git:worktree, git:branch, git:diff, git:status, git:test]
        forbidden_operations: [issue:comment, issue:create, issue:edit, issue:label, issue:assign, issue:close, issue:reopen, pr:comment, pr:create, pr:review, pr:merge, pr:close, pr:push, repo:write, release:create]
        requires_user_approval_for: [pr:create, pr:push, pr:merge, pr:comment, pr:review, issue:comment, issue:label, issue:close, release:create]
---

# safe-implementation-branch

Create an isolated local implementation branch or worktree from a clean base. Keep all work local until the user explicitly approves push, PR creation, review posting, or merge.

Use after a plan exists and the user has approved local implementation scope.

## Non-goals

- Do not push a branch.
- Do not create a PR.
- Do not merge, rebase shared history, force-push, tag a release, or delete remote branches.
- Do not comment on GitHub or edit issues/PRs.
- Do not read or persist GitHub tokens.

## Dangerous operations

Dangerous operations are any remote GitHub mutation or shared-history mutation: push, force-push, PR creation, PR review/comment, issue comment/label/close, merge, release, tag, delete branch, or write-mode API call. These operations are blocked until explicit user approval names the target and action.

## Approval gates

Ask the user before `pr:create`, `pr:push`, or `pr:merge`. Also ask before comments, reviews, labels, releases, or any command touching a remote branch. Approval must include repo, branch, target PR/issue if any, and exact intent.

## Token handling

No token persistence. This skill does not need GitHub tokens for local branch/worktree setup. Do not read, print, copy, create, or store tokens. Do not inspect `.env`, `auth.json`, credential helpers, or token files. If later publishing needs auth, stop and ask the user to authenticate/approve under a write-capable skill.

## Local branch path

```bash
git status --short --branch
git remote get-url origin
git fetch origin main
ISSUE=42
SLUG="short-safe-description"
BRANCH="feat/${ISSUE}-${SLUG}"
git switch -c "$BRANCH" "origin/main"
git status --short --branch
```

## Local worktree path

```bash
git status --short --branch
git fetch origin main
ISSUE=42
SLUG="short-safe-description"
BRANCH="feat/${ISSUE}-${SLUG}"
WORKTREE="../${BRANCH//\//-}"
git worktree add -b "$BRANCH" "$WORKTREE" origin/main
git -C "$WORKTREE" status --short --branch
```

## Implementation loop

1. Read the local plan and touched files.
2. Add regression tests first when changing behavior.
3. Edit inside the branch/worktree only.
4. Run focused tests, then broader checks.
5. Show `git diff --stat` and `git status --short --branch`.
6. Stop before push/PR/merge unless user approved.

## Output shape

Report local branch/worktree, changed files, tests run, and approval-needed remote actions. If user asks to publish, switch to `github-pr-workflow` after approval.
