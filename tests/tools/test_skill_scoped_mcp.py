"""Tests for skill-scoped MCP manifest parsing and hardening."""

import json
from unittest.mock import patch

import pytest


def _create_skill(root, name, frontmatter_extra=""):
    """Create a directory-based skill under *root*."""
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        "description: Skill-scoped MCP test skill\n"
        f"{frontmatter_extra}"
        "---\n\n"
        f"# {name}\n\n"
        "Test content.\n",
        encoding="utf-8",
    )
    return skill_dir


MCP_FRONTMATTER = """mcp:
  servers:
    github-readonly:
      command: gh
      args: [api, graphql]
      env_allowlist: [SKILL_MCP_TOKEN]
      tools_allowlist: [search_issues, read_pr]
      trust: user
"""


class TestSkillViewMcpManifest:
    def test_skill_view_surfaces_manifest_without_starting_mcp_or_values(
        self, tmp_path, monkeypatch
    ):
        """Plain skill_view parses MCP metadata but never starts servers or leaks env values."""
        _create_skill(tmp_path, "github-triage", MCP_FRONTMATTER)
        monkeypatch.setattr("tools.skills_tool.SKILLS_DIR", tmp_path)
        monkeypatch.setenv("SKILL_MCP_TOKEN", "secret-token-value")

        with patch("tools.mcp_tool.register_skill_mcp_servers") as register_mcp:
            from tools.skills_tool import skill_view

            result = json.loads(skill_view("github-triage"))

        assert result["success"] is True
        assert result["mcp"] == {
            "servers": {
                "github-readonly": {
                    "command": "gh",
                    "args": ["api", "graphql"],
                    "env_allowlist": ["SKILL_MCP_TOKEN"],
                    "tools_allowlist": ["search_issues", "read_pr"],
                    "trust": "user",
                }
            }
        }
        assert "secret-token-value" not in json.dumps(result, ensure_ascii=False)
        register_mcp.assert_not_called()

    def test_project_skill_with_mcp_env_allowlist_is_rejected(self, tmp_path, monkeypatch):
        """Project skills may declare MCP servers, but cannot request env passthrough in MVP."""
        user_skills = tmp_path / "user-skills"
        project_skills = tmp_path / "repo" / ".hermes" / "skills"
        user_skills.mkdir()
        _create_skill(project_skills, "project-triage", MCP_FRONTMATTER)
        monkeypatch.setattr("tools.skills_tool.SKILLS_DIR", user_skills)
        monkeypatch.setattr(
            "agent.skill_utils.get_external_skills_dirs",
            lambda: [project_skills],
        )
        monkeypatch.setenv("SKILL_MCP_TOKEN", "project-secret-token")

        from tools.skills_tool import skill_view

        result = json.loads(skill_view("project-triage"))

        assert result["success"] is False
        assert "project skills cannot request MCP env_allowlist" in result["error"]
        assert "SKILL_MCP_TOKEN" in result["error"]
        assert "project-secret-token" not in json.dumps(result, ensure_ascii=False)

    def test_skill_view_rejects_raw_mcp_env_without_leaking_values(self, tmp_path, monkeypatch):
        """Skill-scoped MCP manifests must not surface literal env/headers secrets."""
        _create_skill(
            tmp_path,
            "raw-env-triage",
            """mcp:
  servers:
    raw-secret:
      command: gh
      env:
        GITHUB_TOKEN: literal-secret-value
      headers:
        Authorization: "Bearer header-secret-value"
""",
        )
        monkeypatch.setattr("tools.skills_tool.SKILLS_DIR", tmp_path)

        from tools.skills_tool import skill_view

        result = json.loads(skill_view("raw-env-triage"))

        assert result["success"] is False
        assert "unsupported MCP manifest keys" in result["error"]
        assert "env" in result["error"]
        assert "headers" in result["error"]
        assert "literal-secret-value" not in json.dumps(result, ensure_ascii=False)
        assert "header-secret-value" not in json.dumps(result, ensure_ascii=False)

    def test_skill_view_rejects_top_level_mcp_env_without_leaking_values(self, tmp_path, monkeypatch):
        """Unexpected top-level MCP keys must not be mirrored through skill_view."""
        _create_skill(
            tmp_path,
            "raw-top-level-env",
            """mcp:
  env:
    GITHUB_TOKEN: literal-secret-value
  servers: {}
""",
        )
        monkeypatch.setattr("tools.skills_tool.SKILLS_DIR", tmp_path)

        from tools.skills_tool import skill_view

        result = json.loads(skill_view("raw-top-level-env"))

        assert result["success"] is False
        assert "unsupported MCP manifest keys" in result["error"]
        assert "env" in result["error"]
        assert "literal-secret-value" not in json.dumps(result, ensure_ascii=False)

    def test_hub_community_skill_in_user_dir_with_mcp_env_allowlist_is_rejected(
        self, tmp_path, monkeypatch
    ):
        """Hub provenance can downgrade a skill inside SKILLS_DIR; path alone is not trust."""
        _create_skill(tmp_path, "community-triage", MCP_FRONTMATTER)
        monkeypatch.setattr("tools.skills_tool.SKILLS_DIR", tmp_path)
        monkeypatch.setenv("SKILL_MCP_TOKEN", "community-secret-token")

        with patch("tools.skills_hub.HubLockFile.get_installed") as get_installed:
            get_installed.return_value = {
                "source": "github",
                "trust_level": "community",
            }
            from tools.skills_tool import skill_view

            result = json.loads(skill_view("community-triage"))

        assert result["success"] is False
        assert "project skills cannot request MCP env_allowlist" in result["error"]
        assert "SKILL_MCP_TOKEN" in result["error"]
        assert "community-secret-token" not in json.dumps(result, ensure_ascii=False)

    def test_hub_entry_with_blank_trust_rejects_env_allowlist(self, tmp_path, monkeypatch):
        """Installed hub skills must prove trusted provenance; blank trust fails closed."""
        _create_skill(tmp_path, "blank-trust-triage", MCP_FRONTMATTER)
        monkeypatch.setattr("tools.skills_tool.SKILLS_DIR", tmp_path)
        monkeypatch.setenv("SKILL_MCP_TOKEN", "blank-trust-secret-token")

        with patch("tools.skills_hub.HubLockFile.get_installed") as get_installed:
            get_installed.return_value = {"source": "hub", "trust_level": ""}
            from tools.skills_tool import skill_view

            result = json.loads(skill_view("blank-trust-triage"))

        assert result["success"] is False
        assert "project skills cannot request MCP env_allowlist" in result["error"]
        assert "SKILL_MCP_TOKEN" in result["error"]
        assert "blank-trust-secret-token" not in json.dumps(result, ensure_ascii=False)

    def test_hub_lock_lookup_failure_rejects_env_allowlist(self, tmp_path, monkeypatch):
        """If provenance cannot be read, fail closed instead of upgrading to user trust."""
        _create_skill(tmp_path, "locked-triage", MCP_FRONTMATTER)
        monkeypatch.setattr("tools.skills_tool.SKILLS_DIR", tmp_path)
        monkeypatch.setenv("SKILL_MCP_TOKEN", "locked-secret-token")

        with patch("tools.skills_hub.HubLockFile.get_installed") as get_installed:
            get_installed.side_effect = RuntimeError("lock unavailable")
            from tools.skills_tool import skill_view

            result = json.loads(skill_view("locked-triage"))

        assert result["success"] is False
        assert "project skills cannot request MCP env_allowlist" in result["error"]
        assert "SKILL_MCP_TOKEN" in result["error"]
        assert "locked-secret-token" not in json.dumps(result, ensure_ascii=False)


class TestSkillMcpActivationConfig:
    def test_default_config_disables_skill_scoped_mcp(self):
        from hermes_cli.config import DEFAULT_CONFIG

        assert DEFAULT_CONFIG["skills"]["mcp"]["enabled"] is False


class TestSkillMcpServerBuilder:
    def test_build_skill_mcp_servers_scopes_name_and_maps_allowlists(
        self, monkeypatch
    ):
        from tools.mcp_tool import build_skill_mcp_servers

        monkeypatch.setenv("SKILL_MCP_TOKEN", "secret-token-value")
        servers = build_skill_mcp_servers(
            "github-triage",
            {
                "servers": {
                    "github-readonly": {
                        "command": "gh",
                        "args": ["api", "graphql"],
                        "env_allowlist": ["SKILL_MCP_TOKEN"],
                        "tools_allowlist": ["search_issues", "read_pr"],
                        "trust": "user",
                    }
                }
            },
            skill_source="user",
        )

        assert list(servers) == ["skill:github_triage:github_readonly"]
        cfg = servers["skill:github_triage:github_readonly"]
        assert cfg["command"] == "gh"
        assert cfg["args"] == ["api", "graphql"]
        assert cfg["env"] == {"SKILL_MCP_TOKEN": "secret-token-value"}
        assert cfg["tools"] == {"include": ["search_issues", "read_pr"]}
        assert "env_allowlist" not in cfg
        assert "tools_allowlist" not in cfg

    def test_build_skill_mcp_servers_fails_closed_for_missing_or_empty_env(
        self, monkeypatch
    ):
        from tools.mcp_tool import build_skill_mcp_servers

        monkeypatch.setenv("SKILL_MCP_TOKEN", "secret-token-value")
        monkeypatch.setenv("EMPTY_SKILL_MCP_TOKEN", "")

        with pytest.raises(ValueError) as excinfo:
            build_skill_mcp_servers(
                "github-triage",
                {
                    "servers": {
                        "github-readonly": {
                            "command": "gh",
                            "env_allowlist": [
                                "SKILL_MCP_TOKEN",
                                "MISSING_SKILL_MCP_TOKEN",
                                "EMPTY_SKILL_MCP_TOKEN",
                            ],
                            "trust": "user",
                        }
                    }
                },
                skill_source="user",
            )

        message = str(excinfo.value)
        assert "MISSING_SKILL_MCP_TOKEN" in message
        assert "EMPTY_SKILL_MCP_TOKEN" in message
        assert "secret-token-value" not in message

    def test_build_skill_mcp_servers_rejects_string_env_allowlist(self, monkeypatch):
        """env_allowlist must be a list of env names, not an arbitrary scalar."""
        from tools.mcp_tool import build_skill_mcp_servers

        monkeypatch.setenv("SKILL_MCP_TOKEN", "secret-token-value")

        with pytest.raises(ValueError) as excinfo:
            build_skill_mcp_servers(
                "github-triage",
                {
                    "servers": {
                        "github-readonly": {
                            "command": "gh",
                            "env_allowlist": "SKILL_MCP_TOKEN",
                        }
                    }
                },
                skill_source="user",
            )

        message = str(excinfo.value)
        assert "env_allowlist must be a list of env variable names" in message
        assert "secret-token-value" not in message

    def test_build_skill_mcp_servers_rejects_top_level_env_without_leaking_values(self):
        """Unexpected top-level MCP keys fail closed before server startup."""
        from tools.mcp_tool import build_skill_mcp_servers

        with pytest.raises(ValueError) as excinfo:
            build_skill_mcp_servers(
                "github-triage",
                {
                    "env": {"GITHUB_TOKEN": "literal-secret-value"},
                    "servers": {},
                },
                skill_source="user",
            )

        message = str(excinfo.value)
        assert "unsupported MCP manifest keys" in message
        assert "env" in message
        assert "literal-secret-value" not in message

    def test_build_skill_mcp_servers_rejects_non_mapping_server_config(self):
        """Server entries must be mappings so strings cannot crash with opaque errors."""
        from tools.mcp_tool import build_skill_mcp_servers

        with pytest.raises(ValueError, match="server 'github-readonly' config must be a mapping"):
            build_skill_mcp_servers(
                "github-triage",
                {"servers": {"github-readonly": "gh"}},
                skill_source="user",
            )

    def test_project_source_cannot_build_mcp_server_with_env_allowlist(self, monkeypatch):
        from tools.mcp_tool import build_skill_mcp_servers

        monkeypatch.setenv("SKILL_MCP_TOKEN", "project-secret-token")

        with pytest.raises(ValueError) as excinfo:
            build_skill_mcp_servers(
                "project-triage",
                {
                    "servers": {
                        "github-readonly": {
                            "command": "gh",
                            "env_allowlist": ["SKILL_MCP_TOKEN"],
                            "trust": "user",
                        }
                    }
                },
                skill_source="project",
            )

        message = str(excinfo.value)
        assert "project skills cannot request MCP env_allowlist" in message
        assert "SKILL_MCP_TOKEN" in message
        assert "project-secret-token" not in message

    def test_unknown_source_cannot_build_mcp_server_with_env_allowlist(self, monkeypatch):
        from tools.mcp_tool import build_skill_mcp_servers

        monkeypatch.setenv("SKILL_MCP_TOKEN", "community-secret-token")

        with pytest.raises(ValueError) as excinfo:
            build_skill_mcp_servers(
                "community-triage",
                {
                    "servers": {
                        "github-readonly": {
                            "command": "gh",
                            "env_allowlist": ["SKILL_MCP_TOKEN"],
                        }
                    }
                },
                skill_source="community",
            )

        message = str(excinfo.value)
        assert "skills cannot request MCP env_allowlist" in message
        assert "SKILL_MCP_TOKEN" in message
        assert "community-secret-token" not in message

    def test_build_skill_mcp_servers_fails_closed_for_whitespace_env(self, monkeypatch):
        from tools.mcp_tool import build_skill_mcp_servers

        monkeypatch.setenv("WHITESPACE_SKILL_MCP_TOKEN", "   ")

        with pytest.raises(ValueError) as excinfo:
            build_skill_mcp_servers(
                "github-triage",
                {
                    "servers": {
                        "github-readonly": {
                            "command": "gh",
                            "env_allowlist": ["WHITESPACE_SKILL_MCP_TOKEN"],
                        }
                    }
                },
                skill_source="user",
            )

        message = str(excinfo.value)
        assert "WHITESPACE_SKILL_MCP_TOKEN" in message
        assert "   " not in message

    def test_skill_mcp_safe_env_excludes_unallowlisted_parent_secret(self, monkeypatch):
        """Skill MCP env mapping does not make unrelated process secrets inherited."""
        from tools.mcp_tool import _build_safe_env, build_skill_mcp_servers

        monkeypatch.setenv("SKILL_MCP_TOKEN", "secret-token-value")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "unrelated-parent-secret")

        servers = build_skill_mcp_servers(
            "github-triage",
            {
                "servers": {
                    "github-readonly": {
                        "command": "gh",
                        "env_allowlist": ["SKILL_MCP_TOKEN"],
                    }
                }
            },
            skill_source="user",
        )

        child_env = _build_safe_env(
            servers["skill:github_triage:github_readonly"].get("env")
        )
        assert child_env["SKILL_MCP_TOKEN"] == "secret-token-value"
        assert "AWS_SECRET_ACCESS_KEY" not in child_env
        assert "unrelated-parent-secret" not in json.dumps(child_env, ensure_ascii=False)

    def test_build_skill_mcp_servers_sanitizes_scoped_server_key(self):
        from tools.mcp_tool import build_skill_mcp_servers

        servers = build_skill_mcp_servers(
            "github/triage",
            {"servers": {"read-only": {"command": "gh"}}},
            skill_source="user",
        )

        assert list(servers) == ["skill:github_triage:read_only"]


class TestSkillMcpExplicitActivation:
    def test_register_skill_mcp_servers_honors_string_false_kill_switch(self):
        from tools.mcp_tool import register_skill_mcp_servers

        with (
            patch(
                "hermes_cli.config.load_config",
                return_value={"skills": {"mcp": {"enabled": "false"}}},
            ),
            patch("tools.mcp_tool.register_mcp_servers") as register_mcp,
        ):
            result = register_skill_mcp_servers(
                "github-triage", {"servers": {"github-readonly": {"command": "gh"}}}
            )

        assert result == []
        register_mcp.assert_not_called()

    def test_register_skill_mcp_servers_explicit_enable_defaults_fail_closed_for_env_allowlist(
        self, monkeypatch
    ):
        from tools.mcp_tool import register_skill_mcp_servers

        monkeypatch.setenv("SKILL_MCP_TOKEN", "secret-token-value")
        manifest = {
            "servers": {
                "github-readonly": {
                    "command": "gh",
                    "env_allowlist": ["SKILL_MCP_TOKEN"],
                }
            }
        }

        with patch(
            "hermes_cli.config.load_config",
            return_value={"skills": {"mcp": {"enabled": True}}},
        ):
            with pytest.raises(ValueError) as excinfo:
                register_skill_mcp_servers("github-triage", manifest)

        assert "project skills cannot request MCP env_allowlist" in str(excinfo.value)
        assert "SKILL_MCP_TOKEN" in str(excinfo.value)
        assert "secret-token-value" not in str(excinfo.value)

    def test_register_skill_mcp_servers_explicit_enable_registers_scoped_servers(
        self, monkeypatch
    ):
        from tools.mcp_tool import register_skill_mcp_servers

        monkeypatch.setenv("SKILL_MCP_TOKEN", "secret-token-value")
        manifest = {
            "servers": {
                "github-readonly": {
                    "command": "gh",
                    "env_allowlist": ["SKILL_MCP_TOKEN"],
                    "trust": "user",
                }
            }
        }

        with (
            patch(
                "hermes_cli.config.load_config",
                return_value={"skills": {"mcp": {"enabled": True}}},
            ),
            patch("tools.mcp_tool.register_mcp_servers", return_value=["mcp_tool"])
            as register_mcp,
        ):
            result = register_skill_mcp_servers(
                "github-triage", manifest, skill_source="user"
            )

        assert result == ["mcp_tool"]
        register_mcp.assert_called_once_with(
            {
                "skill:github_triage:github_readonly": {
                    "command": "gh",
                    "env": {"SKILL_MCP_TOKEN": "secret-token-value"},
                }
            }
        )

    def test_register_skill_mcp_servers_does_not_log_env_values_before_dispatch(
        self, monkeypatch, caplog
    ):
        """Registration helper must not log allowlisted env values while building configs."""
        import logging

        from tools.mcp_tool import register_skill_mcp_servers

        monkeypatch.setenv("SKILL_MCP_TOKEN", "secret-token-value")
        manifest = {
            "servers": {
                "github-readonly": {
                    "command": "gh",
                    "env_allowlist": ["SKILL_MCP_TOKEN"],
                }
            }
        }

        caplog.set_level(logging.DEBUG, logger="tools.mcp_tool")
        with (
            patch(
                "hermes_cli.config.load_config",
                return_value={"skills": {"mcp": {"enabled": True}}},
            ),
            patch("tools.mcp_tool.register_mcp_servers", return_value=[]) as register_mcp,
        ):
            register_skill_mcp_servers("github-triage", manifest, skill_source="user")

        register_mcp.assert_called_once()
        assert "secret-token-value" not in caplog.text
