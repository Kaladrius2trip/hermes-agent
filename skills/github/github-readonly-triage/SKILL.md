---
name: github-readonly-triage
description: "Read-only GitHub issue/PR triage; summarize and rank work without mutation."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [GitHub, Triage, Read-Only, Issues, Safe]
    related_skills: [github-auth, github-issues, github-pr-review-readonly]
    github:
      safety:
        mode: read_only_triage
        github_network_mutation_allowed: false
        local_mutation_allowed: false
        token_persistence: forbidden
        live_github_mutation_in_tests: forbidden
        allowed_operations: [repo:metadata, search:issues, issue:list, issue:view, pr:list, pr:view, pr:checks, api:get]
        forbidden_operations: [issue:comment, issue:create, issue:edit, issue:label, issue:assign, issue:close, issue:reopen, pr:comment, pr:create, pr:review, pr:merge, pr:close, pr:push, repo:write, release:create]
        requires_user_approval_for: [issue:comment, issue:create, issue:edit, issue:label, issue:assign, issue:close, issue:reopen, pr:comment, pr:create, pr:review, pr:merge, pr:push, release:create]
---

# github-readonly-triage

Triage GitHub issues and PRs by reading repository metadata, open issues, open PRs, labels, and discussion context. Output local summary only: priority buckets, duplicate candidates, stale items, missing reproduction details, suggested next owner, and questions for a human.

Use when user asks to inspect GitHub work queue but has not explicitly approved any write action.

## Non-goals

- Do not comment, label, assign, close, reopen, create, edit, merge, push, or publish.
- Do not run `gh issue comment`, `gh issue edit`, `gh pr review`, `gh pr merge`, write-mode `gh api`, or mutating `curl` calls.
- Do not create issues or PRs.
- Do not infer permission to mutate from phrases like "triage this". Triage means read and recommend.

## Dangerous operations

Dangerous operations are any GitHub write or repo history mutation: issue comments, labels, assignments, state changes, PR reviews, PR creation, branch push, merge, release creation, write-mode REST/GraphQL calls, and force operations. Treat these as blocked by default.

## Approval gates

If triage finds a needed mutation, stop and ask the user for explicit approval with exact action, target repo, target issue/PR number, and message/body/label. After approval, switch to a write-capable skill such as `github-issues` or `github-code-review`; do not continue under this read-only skill.

## Token handling

No token persistence. Use an existing authenticated `gh` session if present. Do not read, print, copy, create, or store tokens. Do not inspect `.env`, `auth.json`, credential helpers, or GitHub token files. If auth is missing, report the blocker and ask the user to authenticate outside this skill.

## Read-only command examples

```bash
gh auth status --hostname github.com
git remote get-url origin
gh issue list --state open --limit 50
gh issue list --state open --label "needs-triage" --limit 50
gh issue view 42 --comments
gh pr list --state open --limit 50
gh pr view 137
gh pr checks 137
```

```bash
gh api repos/OWNER/REPO/issues --paginate
gh api repos/OWNER/REPO/pulls --paginate
gh api -X GET search/issues -f q='repo:OWNER/REPO is:issue is:open needs-triage'
```

## Output shape

Return local text only:

- Summary: counts by issue/PR/type/status.
- High-priority: item number, reason, missing data.
- Duplicates/stale: candidates, evidence links or titles.
- Suggested actions: recommendations only, no writes.
- Approval-needed: exact mutations that require user approval.

## Verification

Before reporting, confirm no write command ran. If any command would mutate GitHub or local history, abort and ask the user before doing it.
