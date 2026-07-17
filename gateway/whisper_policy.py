"""Recipient policy core for the whisper capability (scoped outbound DM).

Pure fail-closed decision logic from the office security handoff. The
platform adapter resolves a recipient reference to candidate facts
(live-verified roles, bot flags, guild) and delivers the message; this
module only decides whether exactly one approved human recipient exists.
Every deny path returns a stable reason code suitable for the ACL
decision log (capability_type='dm_recipient').
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

# Adapter-verified role snapshots older than this are stale evidence and
# fail closed; delivery must re-verify membership close to send time.
ROLE_SNAPSHOT_MAX_AGE_S = 300.0


@dataclass(frozen=True)
class WhisperCandidate:
    """One resolved recipient candidate with adapter-verified facts."""

    user_id: str
    is_bot: bool
    guild_id: str
    role_names: tuple[str, ...] = ()
    roles_verified_at: float = 0.0
    roles_are_managed: tuple[str, ...] = field(default_factory=tuple)


def resolve_whisper_recipient(
    candidates: Sequence[WhisperCandidate],
    *,
    trusted_guild_id: str,
    approved_role_names: Iterable[str],
    now: Optional[float] = None,
    max_role_age_s: float = ROLE_SNAPSHOT_MAX_AGE_S,
) -> tuple[Optional[WhisperCandidate], str]:
    """Return (recipient, 'allowed') or (None, stable_deny_reason)."""
    trusted_guild = str(trusted_guild_id or "").strip()
    approved = {str(r) for r in (approved_role_names or ()) if str(r).strip()}
    if not trusted_guild or not approved:
        return None, "whisper_not_configured"
    if not candidates:
        return None, "recipient_not_found"
    if len(candidates) != 1:
        return None, "ambiguous_recipient"
    candidate = candidates[0]
    if candidate.is_bot:
        return None, "recipient_is_bot"
    if str(candidate.guild_id or "").strip() != trusted_guild:
        return None, "untrusted_guild"
    moment = time.time() if now is None else float(now)
    verified_at = float(candidate.roles_verified_at or 0.0)
    max_age = float(max_role_age_s)
    if (
        not math.isfinite(moment)
        or not math.isfinite(verified_at)
        or not math.isfinite(max_age)
        or max_age < 0
        or verified_at <= 0
        or verified_at > moment
        or (moment - verified_at) > max_age
    ):
        return None, "stale_role_membership"
    managed = {str(r) for r in (candidate.roles_are_managed or ())}
    human_roles = {str(r) for r in (candidate.role_names or ())} - managed
    if not (human_roles & approved):
        return None, "no_approved_role"
    return candidate, "allowed"
