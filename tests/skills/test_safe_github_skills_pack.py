"""Safety contract tests for the optional safe GitHub skills pack."""
from __future__ import annotations

import re
from pathlib import Path

from agent.skill_utils import parse_frontmatter

ROOT = Path(__file__).resolve().parents[2]
GITHUB_SKILLS = ROOT / "skills" / "github"

SAFE_GITHUB_SKILLS = {
    "github-readonly-triage": "read_only_triage",
    "github-pr-review-readonly": "read_only_review",
    "github-issue-to-plan": "planning_only",
    "safe-implementation-branch": "local_branch_only",
}

READ_ONLY_SKILLS = {"github-readonly-triage", "github-pr-review-readonly"}
GITHUB_NETWORK_READONLY_SKILLS = {
    "github-readonly-triage",
    "github-pr-review-readonly",
    "github-issue-to-plan",
}

READ_ONLY_ALLOWED_OPERATIONS = {
    "repo:metadata",
    "search:issues",
    "issue:list",
    "issue:view",
    "pr:list",
    "pr:view",
    "pr:diff",
    "pr:checks",
    "api:get",
    "git:fetch",
    "git:diff",
    "git:log",
    "git:show",
}

DANGEROUS_GITHUB_OPERATIONS = {
    "issue:comment",
    "issue:create",
    "issue:edit",
    "issue:label",
    "issue:assign",
    "issue:close",
    "issue:reopen",
    "pr:comment",
    "pr:create",
    "pr:review",
    "pr:merge",
    "pr:close",
    "pr:push",
    "repo:write",
    "release:create",
}

MUTATING_COMMAND_PATTERNS = [
    re.compile(r"\bgh\s+issue\s+(?:create|edit|comment|close|reopen|develop|delete)\b"),
    re.compile(r"\bgh\s+pr\s+(?:create|review|comment|merge|close|reopen|edit|ready|checkout)\b"),
    re.compile(r"\bgh\s+api\b[^\n]*(?:--method|-X)\s*(?:POST|PATCH|PUT|DELETE)\b"),
    re.compile(r"\bgh\s+api\b(?![^\n]*(?:--method|-X)\s*GET)[^\n]*(?:-f|-F|--field|--raw-field)\b"),
    re.compile(r"\bcurl\b[^\n]*\s-X\s*(?:POST|PATCH|PUT|DELETE)\b"),
    re.compile(r"\bgit\s+(?:push|commit|merge|rebase|cherry-pick|tag)\b"),
]

TOKEN_PERSISTENCE_PATTERNS = [
    re.compile(r">\s*[^\n]*(?:\.env|\.git-credentials|auth\.json)"),
    re.compile(r"\b(?:GITHUB_TOKEN|GH_TOKEN)\s*="),
    re.compile(r"\bgh\s+auth\s+login\b"),
]


def _load_skill(slug: str) -> tuple[dict, str, str]:
    path = GITHUB_SKILLS / slug / "SKILL.md"
    assert path.exists(), f"missing bundled skill: {path}"
    content = path.read_text(encoding="utf-8")
    frontmatter, body = parse_frontmatter(content)
    return frontmatter, body, content


def _bash_blocks(markdown: str) -> list[str]:
    return re.findall(r"```(?:bash|sh|shell)\n(.*?)```", markdown, flags=re.DOTALL | re.IGNORECASE)


def _github_safety(frontmatter: dict) -> dict:
    metadata = frontmatter.get("metadata") or {}
    hermes = metadata.get("hermes") or {}
    github = hermes.get("github") or {}
    safety = github.get("safety") or {}
    assert isinstance(safety, dict), "metadata.hermes.github.safety must be a mapping"
    return safety


def test_safe_github_skill_pack_has_expected_skills_and_frontmatter():
    for slug, mode in SAFE_GITHUB_SKILLS.items():
        frontmatter, body, _ = _load_skill(slug)
        assert frontmatter["name"] == slug
        assert frontmatter.get("description")
        assert frontmatter.get("version")
        assert frontmatter.get("license") == "MIT"
        assert "linux" in frontmatter.get("platforms", [])
        assert "GitHub" in (frontmatter.get("metadata", {}).get("hermes", {}).get("tags") or [])
        assert body.startswith(f"# {frontmatter['name']}")

        safety = _github_safety(frontmatter)
        assert safety["mode"] == mode
        assert safety["token_persistence"] == "forbidden"
        assert safety["live_github_mutation_in_tests"] == "forbidden"


def test_safe_github_skills_document_non_goals_dangerous_ops_and_gates():
    required_sections = ("## Non-goals", "## Dangerous operations", "## Approval gates", "## Token handling")
    for slug in SAFE_GITHUB_SKILLS:
        _, body, _ = _load_skill(slug)
        for section in required_sections:
            assert section in body, f"{slug} missing {section}"
        assert "No token persistence" in body
        assert "ask the user" in body.lower() or "user approval" in body.lower()


def test_readonly_github_skills_expose_only_read_capabilities():
    for slug in READ_ONLY_SKILLS:
        frontmatter, _, _ = _load_skill(slug)
        safety = _github_safety(frontmatter)
        assert safety["github_network_mutation_allowed"] is False
        assert safety["local_mutation_allowed"] is False

        allowed = set(safety.get("allowed_operations") or [])
        forbidden = set(safety.get("forbidden_operations") or [])
        assert allowed
        assert allowed <= READ_ONLY_ALLOWED_OPERATIONS
        assert DANGEROUS_GITHUB_OPERATIONS <= forbidden


def test_github_network_readonly_skill_command_blocks_do_not_mutate_github_or_git():
    for slug in GITHUB_NETWORK_READONLY_SKILLS:
        _, body, _ = _load_skill(slug)
        command_text = "\n".join(_bash_blocks(body))
        assert command_text, f"{slug} must include concrete read-only command examples"
        for pattern in MUTATING_COMMAND_PATTERNS:
            assert not pattern.search(command_text), f"{slug} has mutating command matching {pattern.pattern}"


def test_safe_github_skills_never_persist_or_scrape_tokens_in_commands():
    for slug in SAFE_GITHUB_SKILLS:
        _, body, _ = _load_skill(slug)
        command_text = "\n".join(_bash_blocks(body))
        for pattern in TOKEN_PERSISTENCE_PATTERNS:
            assert not pattern.search(command_text), f"{slug} has token persistence command matching {pattern.pattern}"


def test_safe_implementation_branch_is_local_only_until_approval_gate():
    frontmatter, body, _ = _load_skill("safe-implementation-branch")
    safety = _github_safety(frontmatter)
    assert safety["github_network_mutation_allowed"] is False
    assert safety["local_mutation_allowed"] is True
    assert safety["allowed_operations"] == [
        "repo:metadata",
        "git:fetch",
        "git:worktree",
        "git:branch",
        "git:diff",
        "git:status",
        "git:test",
    ]
    assert {"pr:create", "pr:push", "pr:merge"} <= set(safety["requires_user_approval_for"])
    assert "git worktree" in body
    assert "git switch -c" in body or "git checkout -b" in body


def test_issue_to_plan_is_network_readonly_and_plan_output_is_local_only():
    frontmatter, body, _ = _load_skill("github-issue-to-plan")
    safety = _github_safety(frontmatter)
    assert safety["github_network_mutation_allowed"] is False
    assert safety["local_mutation_allowed"] is True
    assert safety["allowed_operations"] == ["issue:view", "pr:view", "repo:metadata", "api:get", "local:plan-file"]
    assert "gh issue view" in body
    assert "local plan" in body.lower()
    assert "do not comment" in body.lower()
