#!/usr/bin/env python3
"""
Read-only preflight verifier for the non-root ``claude-yolo`` worktree setup.

Background
----------
Hermes runs the sandboxed CLI as the non-root user ``claude-yolo`` via the
``/usr/local/bin/claude-yolo`` wrapper. When that CLI operates on a git
*worktree*, three pointers must all resolve to paths the non-root user can read:

  1. ``<worktree>/.git``                     -> per-worktree gitdir
  2. ``<gitdir>/commondir`` (``../..``)      -> the shared ``.git`` metadata
  3. ``<gitdir>/gitdir``                     -> back-pointer to the checkout

The fragile setup points ``<worktree>/.git`` straight at ``/root/.config/...``
(or relies on traversal through ``/root/.hermes``). The Hermes CLI may
``chmod`` ``~/.hermes`` and reset ACL masks at runtime, after which a non-root
``git status`` regresses to ``fatal: not a git repository: (null)``.

The stable setup (see ``website/docs/user-guide/git-worktrees.md``) binds the
shared ``.git`` metadata to a non-root-visible path
(``~/workspaces/hermes-agent-live-git``) and rewrites *only this worktree's*
``.git`` pointer to that bind, so nothing the worktree needs lives behind
``/root`` permissions.

This script VERIFIES that stable setup. It is strictly read-only: it never
mounts, never ``setfacl``s, never edits git config. It is off by default
(nothing invokes it automatically) -- an operator runs it by hand before
launching the wrapper, optionally with ``--hermes-command`` to confirm the
pointers survive a harmless Hermes CLI call.

Usage
-----
    # Verify the default worktree + live-git bind
    python scripts/claude_yolo_worktree_preflight.py

    # Verify a specific worktree / bind / user
    python scripts/claude_yolo_worktree_preflight.py \
        --worktree /home/claude-yolo/workspaces/hermes-agent-openagent-capability-layer \
        --live-git /home/claude-yolo/workspaces/hermes-agent-live-git \
        --user claude-yolo

    # Also run a harmless Hermes command, then re-verify non-root git status
    python scripts/claude_yolo_worktree_preflight.py --hermes-command "hermes --version"

Exit status:
    0 -- all checks PASS (or only WARN/SKIP, unless --strict)
    1 -- at least one FAIL (or any WARN when --strict)
"""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_USER = "claude-yolo"
DEFAULT_WRAPPER = "/usr/local/bin/claude-yolo"
# Paths the worktree's git metadata must NOT depend on for read access. The
# Hermes CLI re-chmods these at runtime, so a non-root pointer into them is the
# root cause of the "(null)" regression this script guards against.
ROOT_ONLY_PREFIXES = ("/root/",)

PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"
SKIP = "SKIP"

# Type of a subprocess runner: takes argv, returns CompletedProcess.
Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


@dataclass
class CheckResult:
    """Outcome of a single preflight check."""

    name: str
    status: str
    detail: str = ""

    @property
    def is_fail(self) -> bool:
        return self.status == FAIL

    @property
    def is_warn(self) -> bool:
        return self.status == WARN


# --------------------------------------------------------------------------- #
# Pure helpers (no subprocess / no mutation) -- unit-testable in isolation.
# --------------------------------------------------------------------------- #


def parse_git_pointer(text: str) -> Optional[str]:
    """Return the gitdir path from a ``.git`` pointer file, or None.

    A worktree's ``.git`` is a file of the form ``gitdir: <path>``. A normal
    repository has a ``.git`` *directory* instead, which has no such line.
    """
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("gitdir:"):
            return line[len("gitdir:"):].strip()
    return None


def is_under(path: Path, prefixes: Sequence[str]) -> bool:
    """True if ``path`` (as an absolute string) starts with any prefix."""
    p = str(path)
    return any(p == prefix.rstrip("/") or p.startswith(prefix) for prefix in prefixes)


def resolve_commondir(gitdir: Path, commondir_text: str) -> Path:
    """Resolve the ``commondir`` file contents relative to the gitdir.

    ``commondir`` is typically ``../..`` (relative) but may be absolute.
    """
    value = commondir_text.strip()
    candidate = Path(value)
    if candidate.is_absolute():
        return candidate
    return (gitdir / candidate).resolve()


def classify_pointer(
    gitdir_path: Optional[str],
    live_git: Path,
    root_prefixes: Sequence[str] = ROOT_ONLY_PREFIXES,
) -> CheckResult:
    """Decide PASS/FAIL/WARN for a parsed ``.git`` gitdir pointer.

    Pure decision logic, separated from filesystem access so tests can drive
    every branch with plain strings.
    """
    if gitdir_path is None:
        return CheckResult(
            "git-pointer",
            FAIL,
            ".git has no 'gitdir:' line (not a worktree pointer)",
        )
    gp = Path(gitdir_path)
    if is_under(gp, root_prefixes):
        return CheckResult(
            "git-pointer",
            FAIL,
            f"gitdir points into a root-only path ({gitdir_path}); "
            f"rewrite it under {live_git}",
        )
    try:
        gp.relative_to(live_git)
    except ValueError:
        return CheckResult(
            "git-pointer",
            WARN,
            f"gitdir ({gitdir_path}) is not under the live-git bind "
            f"({live_git}); confirm it is non-root-readable",
        )
    return CheckResult("git-pointer", PASS, gitdir_path)


# --------------------------------------------------------------------------- #
# Filesystem + subprocess checks.
# --------------------------------------------------------------------------- #


def _default_runner(argv: Sequence[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _first_output_line(text: str, fallback: str) -> str:
    """Return the first subprocess output line, or fallback."""
    lines = text.strip().splitlines()
    return lines[0] if lines else fallback


def check_git_pointer(worktree: Path, live_git: Path) -> CheckResult:
    git_file = worktree / ".git"
    if not git_file.exists():
        return CheckResult("git-pointer", FAIL, f"missing {git_file}")
    if git_file.is_dir():
        return CheckResult(
            "git-pointer",
            WARN,
            f"{git_file} is a directory (not a worktree pointer); "
            "this checker targets the worktree setup",
        )
    try:
        text = git_file.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return CheckResult("git-pointer", FAIL, f"cannot read {git_file}: {exc}")
    return classify_pointer(parse_git_pointer(text), live_git)


def check_gitdir_readable(gitdir: Path) -> CheckResult:
    if not gitdir.exists():
        return CheckResult("gitdir-readable", FAIL, f"missing {gitdir}")
    if not os.access(gitdir, os.R_OK | os.X_OK):
        return CheckResult(
            "gitdir-readable", FAIL, f"{gitdir} is not readable/traversable"
        )
    required = ["HEAD", "commondir", "gitdir"]
    missing = [name for name in required if not (gitdir / name).exists()]
    if missing:
        return CheckResult(
            "gitdir-readable",
            FAIL,
            f"{gitdir} missing {', '.join(missing)}",
        )
    return CheckResult("gitdir-readable", PASS, str(gitdir))


def check_commondir(gitdir: Path, live_git: Path) -> CheckResult:
    commondir_file = gitdir / "commondir"
    try:
        text = commondir_file.read_text(encoding="utf-8")
    except OSError as exc:
        return CheckResult("commondir", FAIL, f"cannot read {commondir_file}: {exc}")
    resolved = resolve_commondir(gitdir, text)
    if not resolved.exists():
        return CheckResult(
            "commondir", FAIL, f"commondir resolves to missing {resolved}"
        )
    if not os.access(resolved, os.R_OK | os.X_OK):
        return CheckResult("commondir", FAIL, f"{resolved} is not readable")
    # The shared metadata must hold objects + refs and live on the non-root bind.
    for name in ("objects", "refs"):
        if not (resolved / name).exists():
            return CheckResult(
                "commondir", FAIL, f"{resolved} missing {name}/ (not a .git dir)"
            )
    if is_under(resolved, ROOT_ONLY_PREFIXES):
        return CheckResult(
            "commondir",
            FAIL,
            f"shared .git resolves into a root-only path ({resolved})",
        )
    try:
        resolved.relative_to(live_git)
    except ValueError:
        return CheckResult(
            "commondir",
            WARN,
            f"shared .git ({resolved}) is not under the live-git bind ({live_git})",
        )
    return CheckResult("commondir", PASS, str(resolved))


def check_wrapper(wrapper: Path) -> CheckResult:
    if not wrapper.exists():
        return CheckResult("wrapper", WARN, f"{wrapper} not found")
    if not os.access(wrapper, os.X_OK):
        return CheckResult("wrapper", FAIL, f"{wrapper} is not executable")
    return CheckResult("wrapper", PASS, str(wrapper))


def _runuser_argv(user: str, *args: str) -> List[str]:
    return ["runuser", "-u", user, "--", *args]


def check_root_git_status(worktree: Path, run: Runner) -> CheckResult:
    """``git status`` from the current (likely root) process."""
    if shutil.which("git") is None:
        return CheckResult("root-git-status", SKIP, "git not on PATH")
    cp = run(["git", "-C", str(worktree), "status", "--short", "--branch"])
    if cp.returncode != 0:
        detail = _first_output_line(cp.stderr or cp.stdout or "", "git status failed")
        return CheckResult(
            "root-git-status",
            FAIL,
            detail,
        )
    branch = (cp.stdout or "").strip().splitlines()
    return CheckResult("root-git-status", PASS, branch[0] if branch else "clean")


def check_user_git_status(worktree: Path, user: str, run: Runner) -> CheckResult:
    """``git status`` as the non-root user via ``runuser`` -- the real test."""
    if os.geteuid() != 0:
        return CheckResult(
            "user-git-status",
            SKIP,
            "must run as root to exercise runuser; re-run with sudo to verify",
        )
    if shutil.which("runuser") is None:
        return CheckResult("user-git-status", SKIP, "runuser not on PATH")
    cp = run(_runuser_argv(user, "git", "-C", str(worktree), "status", "--short", "--branch"))
    if cp.returncode != 0:
        return CheckResult(
            "user-git-status",
            FAIL,
            f"as {user}: {(cp.stderr or cp.stdout or 'failed').strip()}",
        )
    branch = (cp.stdout or "").strip().splitlines()
    return CheckResult("user-git-status", PASS, f"as {user}: {branch[0] if branch else 'clean'}")


def check_safe_directory(
    worktree: Path, live_git: Path, user: str, run: Runner
) -> CheckResult:
    """Confirm narrow ``safe.directory`` entries exist for the user.

    We want *narrow* entries (the worktree and the bind) -- a ``*`` wildcard
    is flagged as too broad.
    """
    if os.geteuid() != 0:
        return CheckResult(
            "safe-directory", SKIP, "must run as root to read user git config"
        )
    if shutil.which("runuser") is None:
        return CheckResult("safe-directory", SKIP, "runuser not on PATH")
    cp = run(
        _runuser_argv(
            user, "git", "config", "--global", "--get-all", "safe.directory"
        )
    )
    entries = [line.strip() for line in (cp.stdout or "").splitlines() if line.strip()]
    if "*" in entries:
        return CheckResult(
            "safe-directory",
            WARN,
            "safe.directory='*' is too broad; add narrow per-path entries instead",
        )
    wanted = {str(worktree), str(live_git)}
    missing = sorted(w for w in wanted if w not in entries)
    if missing:
        return CheckResult(
            "safe-directory",
            WARN,
            f"missing narrow entries for {user}: {', '.join(missing)}",
        )
    return CheckResult("safe-directory", PASS, f"{len(entries)} entries for {user}")


def check_hermes_precheck(
    command: str, worktree: Path, user: str, run: Runner
) -> List[CheckResult]:
    """Run a harmless Hermes command, then re-verify non-root git status.

    This catches the regression where the Hermes CLI re-chmods ``~/.hermes``
    and breaks the worktree pointer mid-run. The Hermes command intentionally
    runs as the invoking user (usually root inside a Hermes worker), because
    that is the process whose ``~/.hermes`` chmod side effect used to reset ACL
    masks. Afterward, ``git status`` is checked as ``claude-yolo``.
    """
    results: List[CheckResult] = []
    argv = shlex.split(command)
    if not argv:
        results.append(CheckResult("hermes-precheck", SKIP, "empty command"))
        return results
    cp = run(argv)
    if cp.returncode != 0:
        output = (cp.stderr or cp.stdout or "").strip()
        last_line = output.splitlines()[-1] if output else "no output"
        results.append(
            CheckResult(
                "hermes-precheck",
                WARN,
                f"`{command}` exited {cp.returncode}: {last_line}",
            )
        )
    else:
        results.append(CheckResult("hermes-precheck", PASS, command))
    # Re-verify the worktree git status survived the Hermes command.
    post = check_user_git_status(worktree, user, run)
    post.name = "post-hermes-git-status"
    results.append(post)
    return results


# --------------------------------------------------------------------------- #
# Orchestration / CLI.
# --------------------------------------------------------------------------- #


def run_checks(
    worktree: Path,
    live_git: Path,
    user: str,
    wrapper: Path,
    hermes_command: Optional[str],
    run: Runner,
) -> List[CheckResult]:
    results: List[CheckResult] = []

    pointer = check_git_pointer(worktree, live_git)
    results.append(pointer)

    # Resolve the gitdir for downstream checks only when the pointer is known
    # valid. WARN details are prose, not machine-readable paths.
    gitdir: Optional[Path] = None
    if pointer.status == PASS and pointer.detail:
        candidate = Path(pointer.detail)
        if candidate.is_absolute():
            gitdir = candidate

    if gitdir is not None:
        results.append(check_gitdir_readable(gitdir))
        results.append(check_commondir(gitdir, live_git))
    else:
        results.append(
            CheckResult("gitdir-readable", SKIP, "no usable gitdir pointer")
        )
        results.append(CheckResult("commondir", SKIP, "no usable gitdir pointer"))

    results.append(check_wrapper(wrapper))
    results.append(check_root_git_status(worktree, run))
    results.append(check_user_git_status(worktree, user, run))
    results.append(check_safe_directory(worktree, live_git, user, run))

    if hermes_command:
        results.extend(check_hermes_precheck(hermes_command, worktree, user, run))

    return results


def format_result(result: CheckResult) -> str:
    detail = f" — {result.detail}" if result.detail else ""
    return f"[{result.status:<4}] {result.name}{detail}"


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only preflight verifier for the non-root claude-yolo worktree "
            "git setup. Mutates nothing."
        )
    )
    parser.add_argument(
        "--worktree",
        type=Path,
        default=REPO_ROOT,
        help="Worktree checkout to verify (default: this repo root)",
    )
    parser.add_argument(
        "--live-git",
        type=Path,
        default=Path("/home/claude-yolo/workspaces/hermes-agent-live-git"),
        help="Non-root-visible bind of the shared .git metadata",
    )
    parser.add_argument(
        "--user",
        default=DEFAULT_USER,
        help=f"Non-root user the wrapper runs as (default: {DEFAULT_USER})",
    )
    parser.add_argument(
        "--wrapper",
        type=Path,
        default=Path(DEFAULT_WRAPPER),
        help=f"Path to the launch wrapper (default: {DEFAULT_WRAPPER})",
    )
    parser.add_argument(
        "--hermes-command",
        default=None,
        help=(
            "Optional harmless Hermes CLI command to run as the invoking "
            "process, after which git status is re-verified as --user "
            "(e.g. 'hermes --version')"
        ),
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Treat WARN as failure (non-zero exit)",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    results = run_checks(
        worktree=args.worktree.resolve() if args.worktree.exists() else args.worktree,
        live_git=args.live_git,
        user=args.user,
        wrapper=args.wrapper,
        hermes_command=args.hermes_command,
        run=_default_runner,
    )

    print(f"claude-yolo worktree preflight — worktree={args.worktree}")
    for result in results:
        print(format_result(result))

    fails = [r for r in results if r.is_fail]
    warns = [r for r in results if r.is_warn]
    print(
        f"\nSummary: {len(results) - len(fails) - len(warns)} pass, "
        f"{len(warns)} warn, {len(fails)} fail"
    )

    if fails or (args.strict and warns):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
