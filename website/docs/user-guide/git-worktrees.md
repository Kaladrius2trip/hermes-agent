---
sidebar_position: 3
sidebar_label: "Git Worktrees"
title: "Git Worktrees"
description: "Run multiple Hermes agents safely on the same repository using git worktrees and isolated checkouts"
---

# Git Worktrees

Hermes Agent is often used on large, long‑lived repositories. When you want to:

- Run **multiple agents in parallel** on the same project, or
- Keep experimental refactors isolated from your main branch,

Git **worktrees** are the safest way to give each agent its own checkout without duplicating the entire repository.

This page shows how to combine worktrees with Hermes so each session has a clean, isolated working directory.

## Why Use Worktrees with Hermes?

Hermes treats the **current working directory** as the project root:

- CLI: the directory where you run `hermes` or `hermes chat`
- Messaging gateways: the directory set by `terminal.cwd` in `~/.hermes/config.yaml`

If you run multiple agents in the **same checkout**, their changes can interfere with each other:

- One agent may delete or rewrite files the other is using.
- It becomes harder to understand which changes belong to which experiment.

With worktrees, each agent gets:

- Its **own branch and working directory**
- Its **own Checkpoint Manager history** for `/rollback`

See also: [Checkpoints and /rollback](./checkpoints-and-rollback.md).

## Quick Start: Creating a Worktree

From your main repository (containing `.git/`), create a new worktree for a feature branch:

```bash
# From the main repo root
cd /path/to/your/repo

# Create a new branch and worktree in ../repo-feature
git worktree add ../repo-feature feature/hermes-experiment
```

This creates:

- A new directory: `../repo-feature`
- A new branch: `feature/hermes-experiment` checked out in that directory

Now you can `cd` into the new worktree and run Hermes there:

```bash
cd ../repo-feature

# Start Hermes in the worktree
hermes
```

Hermes will:

- See `../repo-feature` as the project root.
- Use that directory for context files, code edits, and tools.
- Use a **separate checkpoint history** for `/rollback` scoped to this worktree.

## Running Multiple Agents in Parallel

You can create multiple worktrees, each with its own branch:

```bash
cd /path/to/your/repo

git worktree add ../repo-experiment-a feature/hermes-a
git worktree add ../repo-experiment-b feature/hermes-b
```

In separate terminals:

```bash
# Terminal 1
cd ../repo-experiment-a
hermes

# Terminal 2
cd ../repo-experiment-b
hermes
```

Each Hermes process:

- Works on its own branch (`feature/hermes-a` vs `feature/hermes-b`).
- Writes checkpoints under a different shadow repo hash (derived from the worktree path).
- Can use `/rollback` independently without affecting the other.

This is especially useful when:

- Running batch refactors.
- Trying different approaches to the same task.
- Pairing CLI + gateway sessions against the same upstream repo.

## Cleaning Up Worktrees Safely

When you are done with an experiment:

1. Decide whether to keep or discard the work.
2. If you want to keep it:
   - Merge the branch into your main branch as usual.
3. Remove the worktree:

```bash
cd /path/to/your/repo

# Remove the worktree directory and its reference
git worktree remove ../repo-feature
```

Notes:

- `git worktree remove` will refuse to remove a worktree with uncommitted changes unless you force it.
- Removing a worktree does **not** automatically delete the branch; you can delete or keep the branch using normal `git branch` commands.
- Hermes checkpoint data under `~/.hermes/checkpoints/` is not automatically pruned when you remove a worktree, but it is usually very small.

## Best Practices

- **One worktree per Hermes experiment**
  - Create a dedicated branch/worktree for each substantial change.
  - This keeps diffs focused and PRs small and reviewable.
- **Name branches after the experiment**
  - e.g. `feature/hermes-checkpoints-docs`, `feature/hermes-refactor-tests`.
- **Commit frequently**
  - Use git commits for high‑level milestones.
  - Use [checkpoints and /rollback](./checkpoints-and-rollback.md) as a safety net for tool‑driven edits in between.
- **Avoid running Hermes from the bare repo root when using worktrees**
  - Prefer the worktree directories instead, so each agent has a clear scope.

## Using `hermes -w` (Automatic Worktree Mode)

Hermes has a built‑in `-w` flag that **automatically creates a disposable git worktree** with its own branch. You don't need to set up worktrees manually — just `cd` into your repo and run:

```bash
cd /path/to/your/repo
hermes -w
```

Hermes will:

- Create a temporary worktree under `.worktrees/` inside your repo.
- Check out an isolated branch (e.g. `hermes/hermes-<hash>`).
- Run the full CLI session inside that worktree.

This is the easiest way to get worktree isolation. You can also combine it with a single query:

```bash
hermes -w -q "Fix issue #123"
```

For parallel agents, open multiple terminals and run `hermes -w` in each — every invocation gets its own worktree and branch automatically.

## Running non-root Claude Code against a root-owned worktree

Some WSL/Hermes operator setups launch Claude Code through a non-root wrapper such as `/usr/local/bin/claude-yolo`. If Hermes itself runs as `root`, do **not** rely on broad ACL traversal through `/root` or `/root/.hermes` for the worktree's git metadata. Hermes CLI commands may tighten permissions on `~/.hermes`, which can reset ACL masks and make a later non-root `git status` fail with `fatal: not a git repository: (null)`.

Use a scoped bind layout instead:

1. Bind only the intended worktree to a non-root-visible checkout path.
2. Bind only the source repository's shared `.git` metadata to a non-root-visible metadata path.
3. Rewrite only that worktree's `.git` pointer to the bound metadata path.
4. Add narrow `safe.directory` entries for those exact paths. Do not use `safe.directory='*'`.
5. Run a preflight after a harmless Hermes CLI command before launching Claude Code.

Example local runbook (adjust paths for your worktree):

```bash
ROOT_WORKTREE=/root/.config/superpowers/worktrees/hermes-agent/feat-openagent-capability-layer
CLAUDE_WORKTREE=/home/claude-yolo/workspaces/hermes-agent-openagent-capability-layer
LIVE_GIT=/home/claude-yolo/workspaces/hermes-agent-live-git
CLAUDE_USER=claude-yolo

# Capture git paths before rewriting the worktree .git pointer.
WORKTREE_GITDIR=$(git -C "$ROOT_WORKTREE" rev-parse --absolute-git-dir)
WORKTREE_GITDIR_NAME=$(basename "$WORKTREE_GITDIR")
SOURCE_COMMON_GIT=$(git -C "$ROOT_WORKTREE" rev-parse --path-format=absolute --git-common-dir)

sudo mkdir -p "$CLAUDE_WORKTREE" "$LIVE_GIT"
sudo mount --bind "$ROOT_WORKTREE" "$CLAUDE_WORKTREE"
sudo mount --bind "$SOURCE_COMMON_GIT" "$LIVE_GIT"

# Rewrite only this checkout's .git pointer. Do not rewrite the source repo .git.
printf 'gitdir: %s/worktrees/%s\n' "$LIVE_GIT" "$WORKTREE_GITDIR_NAME" | sudo tee "$ROOT_WORKTREE/.git" >/dev/null

runuser -u "$CLAUDE_USER" -- git config --global --add safe.directory "$ROOT_WORKTREE"
runuser -u "$CLAUDE_USER" -- git config --global --add safe.directory "$CLAUDE_WORKTREE"
runuser -u "$CLAUDE_USER" -- git config --global --add safe.directory "$LIVE_GIT"
```

Then verify the exact setup with the read-only preflight script:

```bash
# Optional but recommended: include a harmless Hermes command to catch permission resets.
python scripts/claude_yolo_worktree_preflight.py \
  --worktree "$ROOT_WORKTREE" \
  --live-git "$LIVE_GIT" \
  --user "$CLAUDE_USER" \
  --hermes-command "hermes --version"

runuser -u "$CLAUDE_USER" -- git -C "$ROOT_WORKTREE" status --short --branch
runuser -u "$CLAUDE_USER" -- git -C "$CLAUDE_WORKTREE" status --short --branch
git -C "$ROOT_WORKTREE" status --short --branch
```

Launch `/usr/local/bin/claude-yolo` from `$CLAUDE_WORKTREE`, not from the private root path. The script is verification-only: it does not mount, change ACLs, edit git config, or expose credentials.

## Putting It All Together

- Use **git worktrees** to give each Hermes session its own clean checkout.
- Use **branches** to capture the high‑level history of your experiments.
- Use **checkpoints + `/rollback`** to recover from mistakes inside each worktree.

This combination gives you:

- Strong guarantees that different agents and experiments do not step on each other.
- Fast iteration cycles with easy recovery from bad edits.
- Clean, reviewable pull requests.
