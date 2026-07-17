"""S6 driver: deterministic grant-path recommendation engine."""
from __future__ import annotations

from gateway.acl import ACLStore
from gateway.acl_recommender import recommend_grant_paths

CATALOG = {"jenkins_build_pc": "runtime_safe", "web_search": "runtime_safe"}


def _store(tmp_path) -> ACLStore:
    store = ACLStore(tmp_path / "acl.sqlite3")
    store.create_group("builders")
    store.grant_group_access("builders", "tool:jenkins_build_pc")
    store.grant_group_access("builders", "web")
    store.create_group("crowd")
    store.grant_membership(
        platform="discord", subject_type="user", subject_id="u1",
        group_name="crowd", scope="channel", scope_id="c1",
    )
    store.grant_membership(
        platform="discord", subject_type="user", subject_id="mate",
        group_name="crowd", scope="channel", scope_id="c1",
    )
    store.grant_membership(
        platform="discord", subject_type="role", subject_id="team",
        group_name="crowd", scope="channel", scope_id="c1",
    )
    return store


def _subject(**kw):
    base = dict(platform="discord", user_id="u1", role_ids=("team",))
    base.update(kw)
    return base


def _ctx():
    return dict(scope="channel", channel_id="c1", guild_id="g1")


def test_ranked_options_deterministic(tmp_path):
    options = recommend_grant_paths(
        _store(tmp_path), _subject(), "tool:jenkins_build_pc", _ctx(),
        catalog=CATALOG,
    )
    kinds = [o["kind"] for o in options]
    assert kinds == [
        "join_group",
        "direct_user_grant",
        "create_group",
        "grant_to_existing_group",
    ]
    ranks = [o["rank"] for o in options]
    assert ranks == sorted(ranks)


def test_join_group_option_details(tmp_path):
    options = recommend_grant_paths(
        _store(tmp_path), _subject(), "tool:jenkins_build_pc", _ctx(),
        catalog=CATALOG,
    )
    join = options[0]
    assert join["group_name"] == "builders"
    assert join["blast_radius"] == 1
    assert join["excess_privileges"] == 1


def test_direct_grant_blast_one(tmp_path):
    options = recommend_grant_paths(
        _store(tmp_path), _subject(), "tool:jenkins_build_pc", _ctx(),
        catalog=CATALOG,
    )
    direct = [o for o in options if o["kind"] == "direct_user_grant"][0]
    assert direct["blast_radius"] == 1


def test_existing_group_blast_unknown_with_roles(tmp_path):
    options = recommend_grant_paths(
        _store(tmp_path), _subject(), "tool:jenkins_build_pc", _ctx(),
        catalog=CATALOG,
    )
    existing = [o for o in options if o["kind"] == "grant_to_existing_group"][0]
    assert existing["group_name"] == "crowd"
    assert existing["blast_radius"] == "unknown"
    assert any("all effective members" in w for w in existing["warnings"])


def test_existing_group_blast_counted_with_roster(tmp_path):
    options = recommend_grant_paths(
        _store(tmp_path), _subject(), "tool:jenkins_build_pc", _ctx(),
        catalog=CATALOG, roster_provider=lambda role_id: 7,
    )
    existing = [o for o in options if o["kind"] == "grant_to_existing_group"][0]
    assert existing["blast_radius"] == 9


def test_already_effective_short_circuits(tmp_path):
    store = _store(tmp_path)
    store.grant_membership(
        platform="discord", subject_type="user", subject_id="u1",
        group_name="builders", scope="channel", scope_id="c1",
    )
    options = recommend_grant_paths(
        store, _subject(), "tool:jenkins_build_pc", _ctx(), catalog=CATALOG,
    )
    assert options[0]["kind"] == "already_effective"
    assert options[0]["source"] == {"type": "group", "name": "builders"}
