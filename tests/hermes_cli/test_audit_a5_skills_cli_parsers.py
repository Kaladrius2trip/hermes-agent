"""Regression tests for the 2026-06-11 fork-audit A5 skills CLI restore.

The upstream merge 73a1794cd resolved hermes_cli/main.py in favor of the
extracted subcommand parsers and dropped the fork's doctor/convert/import/
rollback subparsers and --dry-run/--to flags, leaving the skills_command
router branches unreachable from the CLI. These tests pin the parser surface
itself so a future merge regression is caught at parse level, not by users.
"""

import argparse

import pytest

from hermes_cli.subcommands.skills import build_skills_parser


@pytest.fixture()
def parser():
    root = argparse.ArgumentParser(prog="hermes")
    subparsers = root.add_subparsers(dest="command")
    build_skills_parser(subparsers, cmd_skills=lambda args: None)
    return root


def test_doctor_subcommand_parses(parser):
    args = parser.parse_args(["skills", "doctor", "."])
    assert args.skills_action == "doctor"
    assert args.path == "."


def test_doctor_path_defaults_to_cwd(parser):
    args = parser.parse_args(["skills", "doctor"])
    assert args.path == "."


def test_convert_subcommand_parses_with_flags(parser):
    args = parser.parse_args(
        ["skills", "convert", "foo.md", "--output", "/tmp/skills", "--dry-run", "--force"]
    )
    assert args.skills_action == "convert"
    assert args.path == "foo.md"
    assert args.output == "/tmp/skills"
    assert args.dry_run is True
    assert args.force is True


def test_import_subcommand_parses_with_flags(parser):
    args = parser.parse_args(
        ["skills", "import", "./pack", "--dry-run", "--convert-flat", "--force"]
    )
    assert args.skills_action == "import"
    assert args.path == "./pack"
    assert args.dry_run is True
    assert args.convert_flat is True


def test_rollback_subcommand_parses(parser):
    args = parser.parse_args(["skills", "rollback", "my-skill"])
    assert args.skills_action == "rollback"
    assert args.name == "my-skill"


def test_update_accepts_dry_run_and_to(parser):
    args = parser.parse_args(["skills", "update", "my-skill", "--to", "abc123", "--dry-run"])
    assert args.skills_action == "update"
    assert args.to == "abc123"
    assert args.dry_run is True


def test_uninstall_accepts_dry_run(parser):
    args = parser.parse_args(["skills", "uninstall", "my-skill", "--dry-run"])
    assert args.dry_run is True


def test_router_reaches_all_restored_actions(parser, monkeypatch):
    """Every restored subcommand routes into its skills_hub handler."""
    import hermes_cli.skills_hub as hub

    calls = []
    monkeypatch.setattr(hub, "do_doctor", lambda *a, **k: calls.append("doctor"))
    monkeypatch.setattr(hub, "do_convert", lambda *a, **k: calls.append("convert"))
    monkeypatch.setattr(hub, "do_import", lambda *a, **k: calls.append("import"))
    monkeypatch.setattr(hub, "do_rollback", lambda *a, **k: calls.append("rollback"))

    for argv in (
        ["skills", "doctor", "."],
        ["skills", "convert", "foo.md"],
        ["skills", "import", "./pack"],
        ["skills", "rollback", "my-skill"],
    ):
        hub.skills_command(parser.parse_args(argv))

    assert calls == ["doctor", "convert", "import", "rollback"]
