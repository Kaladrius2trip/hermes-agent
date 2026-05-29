"""Tests for ``scripts/claude_yolo_worktree_preflight.py``.

The preflight verifier guards the non-root ``claude-yolo`` worktree setup: it
must FAIL when the worktree ``.git`` pointer or shared ``.git`` metadata resolve
into a root-only path (the regression where the Hermes CLI re-chmods
``~/.hermes`` and ``git status`` dies with ``not a git repository: (null)``).

These tests exercise the parsing / decision logic and the filesystem checks
against temp trees, with a fake subprocess runner -- no real ``runuser``, no
real mounts, and (critically) no mutation of any git config or ACL.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "_preflight_under_test",
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "claude_yolo_worktree_preflight.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


pf = _load_module()


def _fake_runner(returncode=0, stdout="", stderr=""):
    """Build a runner returning a canned CompletedProcess; records calls."""
    calls = []

    def run(argv):
        calls.append(list(argv))
        return subprocess.CompletedProcess(
            args=list(argv), returncode=returncode, stdout=stdout, stderr=stderr
        )

    run.calls = calls  # type: ignore[attr-defined]
    return run


# --------------------------------------------------------------------------- #
# Pure parsing helpers.
# --------------------------------------------------------------------------- #


def test_parse_git_pointer_extracts_path():
    assert pf.parse_git_pointer("gitdir: /a/b/c\n") == "/a/b/c"


def test_parse_git_pointer_handles_whitespace_and_extra_lines():
    assert pf.parse_git_pointer("\n  gitdir:   /x/y \nnoise\n") == "/x/y"


def test_parse_git_pointer_none_for_plain_directory():
    # A normal .git directory has no 'gitdir:' line.
    assert pf.parse_git_pointer("ref: refs/heads/main\n") is None


def test_is_under_matches_prefix_and_exact():
    assert pf.is_under(Path("/root/.config/x"), ("/root/",))
    assert pf.is_under(Path("/root"), ("/root/",))
    assert not pf.is_under(Path("/home/claude-yolo/x"), ("/root/",))


def test_resolve_commondir_relative_and_absolute(tmp_path):
    gitdir = tmp_path / "live" / "worktrees" / "wt"
    gitdir.mkdir(parents=True)
    assert pf.resolve_commondir(gitdir, "../..\n") == (tmp_path / "live").resolve()
    assert pf.resolve_commondir(gitdir, "/abs/path\n") == Path("/abs/path")


# --------------------------------------------------------------------------- #
# Pointer classification -- the core safety decision.
# --------------------------------------------------------------------------- #


def test_classify_pointer_pass_under_live_git():
    live = Path("/home/u/live")
    res = pf.classify_pointer("/home/u/live/worktrees/wt", live)
    assert res.status == pf.PASS


def test_classify_pointer_fail_on_root_path():
    live = Path("/home/u/live")
    res = pf.classify_pointer("/root/.config/superpowers/x/.git", live)
    assert res.status == pf.FAIL
    assert "root-only" in res.detail


def test_classify_pointer_warn_when_outside_live_git():
    live = Path("/home/u/live")
    res = pf.classify_pointer("/home/u/somewhere-else/.git", live)
    assert res.status == pf.WARN


def test_classify_pointer_fail_when_missing():
    res = pf.classify_pointer(None, Path("/home/u/live"))
    assert res.status == pf.FAIL


# --------------------------------------------------------------------------- #
# Filesystem checks against temp trees.
# --------------------------------------------------------------------------- #


def _make_stable_worktree(tmp_path: Path):
    """Construct a valid, non-root, stable worktree + live-git bind."""
    live = tmp_path / "live"
    shared = live  # the bind *is* the shared .git dir
    (shared / "objects").mkdir(parents=True)
    (shared / "refs").mkdir()
    gitdir = live / "worktrees" / "wt"
    gitdir.mkdir(parents=True)
    (gitdir / "HEAD").write_text("ref: refs/heads/feat\n")
    (gitdir / "commondir").write_text("../..\n")
    (gitdir / "gitdir").write_text(str(tmp_path / "checkout" / ".git") + "\n")

    worktree = tmp_path / "checkout"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {gitdir}\n")
    return worktree, live, gitdir


def test_check_git_pointer_pass(tmp_path):
    worktree, live, _ = _make_stable_worktree(tmp_path)
    assert pf.check_git_pointer(worktree, live).status == pf.PASS


def test_check_git_pointer_fail_missing(tmp_path):
    worktree = tmp_path / "checkout"
    worktree.mkdir()
    assert pf.check_git_pointer(worktree, tmp_path / "live").status == pf.FAIL


def test_check_gitdir_readable_pass(tmp_path):
    _, _, gitdir = _make_stable_worktree(tmp_path)
    assert pf.check_gitdir_readable(gitdir).status == pf.PASS


def test_check_gitdir_readable_fail_missing_files(tmp_path):
    gitdir = tmp_path / "gd"
    gitdir.mkdir()
    res = pf.check_gitdir_readable(gitdir)
    assert res.status == pf.FAIL
    assert "commondir" in res.detail


def test_check_commondir_pass(tmp_path):
    _, live, gitdir = _make_stable_worktree(tmp_path)
    assert pf.check_commondir(gitdir, live).status == pf.PASS


def test_check_commondir_warn_outside_live(tmp_path):
    _, _, gitdir = _make_stable_worktree(tmp_path)
    # Point the live-git arg somewhere unrelated -> shared dir is "outside".
    res = pf.check_commondir(gitdir, tmp_path / "elsewhere")
    assert res.status == pf.WARN


def test_check_commondir_fails_root_prefix_before_outside_live_warn(tmp_path, monkeypatch):
    root_like = tmp_path / "rootlike" / "repo.git"
    (root_like / "objects").mkdir(parents=True)
    (root_like / "refs").mkdir()
    gitdir = tmp_path / "live" / "worktrees" / "wt"
    gitdir.mkdir(parents=True)
    (gitdir / "commondir").write_text(str(root_like) + "\n", encoding="utf-8")
    monkeypatch.setattr(pf, "ROOT_ONLY_PREFIXES", (str(tmp_path / "rootlike") + "/",))

    res = pf.check_commondir(gitdir, tmp_path / "different-live-git")

    assert res.status == pf.FAIL
    assert "root-only" in res.detail


def test_check_wrapper_warn_when_absent(tmp_path):
    assert pf.check_wrapper(tmp_path / "nope").status == pf.WARN


# --------------------------------------------------------------------------- #
# Subprocess-backed checks via fake runner.
# --------------------------------------------------------------------------- #


def test_check_root_git_status_pass(tmp_path):
    run = _fake_runner(returncode=0, stdout="## feat\n")
    res = pf.check_root_git_status(tmp_path, run)
    assert res.status == pf.PASS
    assert res.detail == "## feat"


def test_check_root_git_status_fail(tmp_path):
    run = _fake_runner(returncode=128, stderr="fatal: not a git repository: (null)\n")
    res = pf.check_root_git_status(tmp_path, run)
    assert res.status == pf.FAIL
    assert "(null)" in res.detail


def test_check_root_git_status_fail_handles_blank_output(tmp_path):
    run = _fake_runner(returncode=128, stderr="\n")
    res = pf.check_root_git_status(tmp_path, run)
    assert res.status == pf.FAIL
    assert res.detail == "git status failed"


def test_check_user_git_status_skips_when_not_root(tmp_path, monkeypatch):
    monkeypatch.setattr(pf.os, "geteuid", lambda: 1000)
    run = _fake_runner()
    res = pf.check_user_git_status(tmp_path, "claude-yolo", run)
    assert res.status == pf.SKIP
    # Must NOT have invoked runuser when non-root.
    assert run.calls == []


def test_check_user_git_status_pass_as_root(tmp_path, monkeypatch):
    monkeypatch.setattr(pf.os, "geteuid", lambda: 0)
    monkeypatch.setattr(pf.shutil, "which", lambda name: "/usr/bin/" + name)
    run = _fake_runner(returncode=0, stdout="## feat\n")
    res = pf.check_user_git_status(tmp_path, "claude-yolo", run)
    assert res.status == pf.PASS
    assert run.calls[0][:3] == ["runuser", "-u", "claude-yolo"]


def test_check_safe_directory_warn_on_wildcard(tmp_path, monkeypatch):
    monkeypatch.setattr(pf.os, "geteuid", lambda: 0)
    monkeypatch.setattr(pf.shutil, "which", lambda name: "/usr/bin/" + name)
    run = _fake_runner(returncode=0, stdout="*\n")
    res = pf.check_safe_directory(tmp_path, tmp_path / "live", "claude-yolo", run)
    assert res.status == pf.WARN
    assert "too broad" in res.detail


def test_check_safe_directory_pass_with_narrow_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(pf.os, "geteuid", lambda: 0)
    monkeypatch.setattr(pf.shutil, "which", lambda name: "/usr/bin/" + name)
    live = tmp_path / "live"
    run = _fake_runner(returncode=0, stdout=f"{tmp_path}\n{live}\n")
    res = pf.check_safe_directory(tmp_path, live, "claude-yolo", run)
    assert res.status == pf.PASS


def test_hermes_precheck_reverifies_git_status(tmp_path, monkeypatch):
    monkeypatch.setattr(pf.os, "geteuid", lambda: 0)
    monkeypatch.setattr(pf.shutil, "which", lambda name: "/usr/bin/" + name)
    run = _fake_runner(returncode=0, stdout="## feat\n")
    results = pf.check_hermes_precheck("hermes --version", tmp_path, "claude-yolo", run)
    names = [r.name for r in results]
    assert names == ["hermes-precheck", "post-hermes-git-status"]
    assert all(r.status == pf.PASS for r in results)
    assert run.calls[0] == ["hermes", "--version"]  # type: ignore[attr-defined]
    assert run.calls[1][:3] == ["runuser", "-u", "claude-yolo"]  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# Orchestration + exit code.
# --------------------------------------------------------------------------- #


def test_run_checks_all_pass_non_root(tmp_path, monkeypatch):
    worktree, live, _ = _make_stable_worktree(tmp_path)
    monkeypatch.setattr(pf.os, "geteuid", lambda: 1000)
    run = _fake_runner(returncode=0, stdout="## feat\n")
    results = pf.run_checks(
        worktree=worktree,
        live_git=live,
        user="claude-yolo",
        wrapper=tmp_path / "wrapper",  # absent -> WARN, not FAIL
        hermes_command=None,
        run=run,
    )
    assert not [r for r in results if r.is_fail]


def test_run_checks_fail_on_root_pointer(tmp_path, monkeypatch):
    worktree = tmp_path / "checkout"
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: /root/.config/superpowers/wt/.git\n")
    monkeypatch.setattr(pf.os, "geteuid", lambda: 1000)
    run = _fake_runner(returncode=0, stdout="## feat\n")
    results = pf.run_checks(
        worktree=worktree,
        live_git=tmp_path / "live",
        user="claude-yolo",
        wrapper=tmp_path / "wrapper",
        hermes_command=None,
        run=run,
    )
    pointer = next(r for r in results if r.name == "git-pointer")
    assert pointer.status == pf.FAIL


def test_run_checks_warns_without_false_gitdir_fail_for_plain_repo(tmp_path, monkeypatch):
    worktree = tmp_path / "plain-repo"
    (worktree / ".git").mkdir(parents=True)
    monkeypatch.setattr(pf.os, "geteuid", lambda: 1000)
    run = _fake_runner(returncode=0, stdout="## main\n")

    results = pf.run_checks(
        worktree=worktree,
        live_git=tmp_path / "live",
        user="claude-yolo",
        wrapper=tmp_path / "wrapper",
        hermes_command=None,
        run=run,
    )

    assert next(r for r in results if r.name == "git-pointer").status == pf.WARN
    assert next(r for r in results if r.name == "gitdir-readable").status == pf.SKIP
    assert not [r for r in results if r.is_fail]


def test_main_exit_zero_on_clean_tree(tmp_path, monkeypatch, capsys):
    worktree, live, _ = _make_stable_worktree(tmp_path)
    monkeypatch.setattr(pf.os, "geteuid", lambda: 1000)
    # Make root-git-status pass without a real git binary.
    monkeypatch.setattr(pf, "_default_runner", lambda argv: subprocess.CompletedProcess(argv, 0, "## feat\n", ""))
    monkeypatch.setattr(pf.shutil, "which", lambda name: "/usr/bin/" + name)
    code = pf.main(
        [
            "--worktree",
            str(worktree),
            "--live-git",
            str(live),
            "--wrapper",
            str(tmp_path / "wrapper"),
        ]
    )
    assert code == 0
    assert "Summary:" in capsys.readouterr().out
