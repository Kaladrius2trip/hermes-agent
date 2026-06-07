"""Privacy-safe, local-only delegation/observability audit bundle (P10 Phase 7).

This module records *safe* event dicts about delegation and team runs as a local
JSONL bundle so failed runs can be debugged after the fact. It is deliberately
small and has no external surface:

* **Local only** — appends JSON lines to a file under the Hermes logs dir (or a
  configured ``delegation.audit.dir``). No network, no remote telemetry.
* **Disabled by default** — :func:`record_audit_event` is a no-op and creates no
  file unless ``delegation.audit.enabled`` is true. Remote telemetry stays
  opt-in through the existing plugin system only.
* **Never persists prompts/goals** — the event builders capture metadata
  (category, recipe, provider/model, toolsets, timeouts, status) plus short
  result summaries; raw prompt/goal/context bodies are never included.
* **Secret-safe** — every event is recursively redacted before it is written:
  values under secret-like keys, known credential prefixes, and URL query
  strings are replaced with ``[REDACTED]``. Credentials, API keys, tokens,
  passwords, and env values never reach disk.

The builders are pure; only :func:`record_audit_event` touches the filesystem
(and only when enabled), so the shapes can be exercised cheaply in tests.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

REDACTED = "[REDACTED]"
MAX_AUDIT_TEXT_CHARS = 1000

# Default JSONL filename within the resolved audit directory.
_AUDIT_FILENAME = "delegation-audit.jsonl"


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

# Secret-like dict key names (case-insensitive). Word-boundary anchored so a
# counter named "tokens" or a field like "task_index" is NOT mistaken for a
# secret, while "api_key", "OPENAI_API_KEY", "access_token", "client_secret",
# and "authorization" all match.
_SECRET_KEY_RE = re.compile(
    r"(?:^|_)(?:"
    r"api[_-]?key|apikey|"
    r"secret|client[_-]?secret|secret[_-]?value|raw[_-]?secret|"
    r"password|passwd|passphrase|"
    r"token|access[_-]?token|refresh[_-]?token|id[_-]?token|session[_-]?token|"
    r"auth[_-]?token|bearer|"
    r"credential|credentials|private[_-]?key|key[_-]?material|"
    r"authorization|connection[_-]?string"
    r")(?:$|_)",
    re.IGNORECASE,
)

# URL with a query string: keep scheme/authority/path, drop the query value.
# ``https://x/v1?api_key=abc`` -> ``https://x/v1?[REDACTED]``.
_URL_QUERY_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.\-]*://[^\s?#]+)\?[^\s#]*")
_URL_USERINFO_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.\-]*://)[^\s/@]+@")
_INLINE_SECRET_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9._\-…\.]{6,}|gh[opsu]_[A-Za-z0-9_]{8,}|"
    r"github_pat_[A-Za-z0-9_]{8,}|xox[baprs]-[A-Za-z0-9\-]{8,}|"
    r"Bearer\s+[A-Za-z0-9._\-]{8,})\b"
)


def _redacted_string(value: str) -> str:
    """Redact URL query strings and known credential prefixes inside a string."""
    if not value:
        return value
    redacted = _URL_USERINFO_RE.sub(lambda m: f"{m.group(1)}{REDACTED}@", value)
    redacted = _URL_QUERY_RE.sub(lambda m: f"{m.group(1)}?{REDACTED}", redacted)
    # Defense in depth: replace any known vendor credential prefix (sk-, ghp_,
    # JWTs, …) with [REDACTED]. Reuse the maintained pattern set when available.
    try:
        from agent.redact import _PREFIX_RE  # type: ignore

        redacted = _PREFIX_RE.sub(REDACTED, redacted)
    except Exception:  # pragma: no cover - redactor optional
        pass
    redacted = _INLINE_SECRET_RE.sub(REDACTED, redacted)
    return redacted


def redact_audit_value(value: Any) -> Any:
    """Recursively redact a value so it is safe to persist to a local audit log.

    * dict — values under secret-like keys become ``[REDACTED]``; other values
      are redacted recursively.
    * list/tuple — each item is redacted recursively.
    * str — URL query strings and known credential prefixes are masked.
    * everything else is returned unchanged.
    """
    if isinstance(value, dict):
        out: Dict[Any, Any] = {}
        for key, val in value.items():
            if isinstance(key, str) and _SECRET_KEY_RE.search(key):
                out[key] = REDACTED
            else:
                out[key] = redact_audit_value(val)
        return out
    if isinstance(value, (list, tuple)):
        return [redact_audit_value(item) for item in value]
    if isinstance(value, str):
        return _redacted_string(value)
    return value


def _bounded_text(value: Optional[str]) -> Optional[str]:
    """Return ``value`` capped to a short audit-friendly string."""
    if value is None:
        return None
    text = str(value)
    if len(text) <= MAX_AUDIT_TEXT_CHARS:
        return text
    return text[:MAX_AUDIT_TEXT_CHARS] + "…"


# ---------------------------------------------------------------------------
# Config / path resolution
# ---------------------------------------------------------------------------

def audit_config(cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Return effective audit config from full config or ``delegation`` section."""
    raw = cfg or {}
    delegation = raw.get("delegation") or raw
    audit = delegation.get("audit") or {}
    return {
        "enabled": bool(audit.get("enabled", False)),
        "dir": str(audit.get("dir") or ""),
    }


def is_audit_enabled(cfg: Optional[Dict[str, Any]]) -> bool:
    """True only when ``delegation.audit.enabled`` is explicitly truthy."""
    return audit_config(cfg)["enabled"]


def audit_log_path(cfg: Optional[Dict[str, Any]]) -> Path:
    """Resolve the JSONL bundle path (configured dir or Hermes logs dir)."""
    conf = audit_config(cfg)
    if conf["dir"]:
        base = Path(conf["dir"]).expanduser()
    else:
        try:
            from hermes_constants import get_hermes_home

            base = get_hermes_home() / "logs"
        except Exception:  # pragma: no cover - fallback for odd environments
            base = Path.home() / ".hermes" / "logs"
    return base / _AUDIT_FILENAME


# ---------------------------------------------------------------------------
# Event builders (pure — no prompts/goals, no credentials)
# ---------------------------------------------------------------------------

def build_delegation_run_event(
    *,
    session_id: Optional[str] = None,
    parent_session_id: Optional[str] = None,
    category_requested: Optional[str] = None,
    profile_requested: Optional[str] = None,
    resolved_category: Optional[Dict[str, Any]] = None,
    recipe: Optional[str] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    toolsets: Optional[List[str]] = None,
    child_timeout_seconds: Optional[float] = None,
    max_iterations: Optional[int] = None,
    status: Optional[str] = None,
    result_summary: Optional[str] = None,
    error: Optional[str] = None,
    fallback_selected: bool = False,
    fallback_reason: Optional[str] = None,
    task_index: Optional[int] = None,
    task_count: Optional[int] = None,
    timestamp: Optional[float] = None,
) -> Dict[str, Any]:
    """Build a safe ``delegation_run`` audit event.

    When ``resolved_category`` (the output of
    :func:`tools.delegation_categories.resolve_delegation_category`) is given,
    its category/recipe/provider/model/toolsets/timeouts/fallback metadata fill
    any field not explicitly overridden. No prompt, goal, or context body is
    ever included — only a short ``result_summary`` if the caller supplies one.
    """
    rc = resolved_category if isinstance(resolved_category, dict) else {}
    fallback_metadata = rc.get("fallback_metadata") or {"enabled": False, "count": 0}

    def _pick(explicit: Any, key: str, default: Any = None) -> Any:
        if explicit is not None:
            return explicit
        val = rc.get(key)
        return val if val not in (None, "") else default

    return {
        "event_type": "delegation_run",
        "timestamp": timestamp,
        "session_id": session_id,
        "parent_session_id": parent_session_id,
        "category_requested": category_requested,
        "category_resolved": _pick(None, "category", "") or "",
        "profile_requested": profile_requested,
        "profile_resolved": _pick(None, "profile", "") or "",
        "fallback_selected": bool(fallback_selected),
        "fallback_reason": fallback_reason,
        "fallback_metadata": fallback_metadata,
        "recipe": _pick(recipe, "recipe", "") or "",
        "provider": _pick(provider, "provider", "") or "",
        "model": _pick(model, "model", "") or "",
        "base_url": base_url or "",
        "toolsets": list(_pick(toolsets, "toolsets") or []),
        "child_timeout_seconds": _pick(child_timeout_seconds, "child_timeout_seconds"),
        "max_iterations": _pick(max_iterations, "max_iterations"),
        "status": status or "",
        "result_summary": _bounded_text(result_summary),
        "error": _bounded_text(error),
        "task_index": task_index,
        "task_count": task_count,
    }


def build_mcp_env_decision_event(
    *,
    allowed_names: Optional[List[str]] = None,
    stripped_count: int = 0,
    decision: str = "",
    skill: Optional[str] = None,
    source: Optional[str] = None,
    session_id: Optional[str] = None,
    timestamp: Optional[float] = None,
) -> Dict[str, Any]:
    """Build a safe ``mcp_env_decision`` event.

    Records only env-var *names*, the count of stripped vars, and the gating
    decision — never any env value. ``values_redacted`` is always true to make
    the privacy guarantee explicit in the bundle.
    """
    return {
        "event_type": "mcp_env_decision",
        "timestamp": timestamp,
        "session_id": session_id,
        "skill": skill,
        "source": source,
        "decision": decision,
        "env_allowed_names": sorted(str(n) for n in (allowed_names or [])),
        "env_stripped_count": int(stripped_count),
        "values_redacted": True,
    }


def build_team_plan_event(
    plan: Any,
    *,
    session_id: Optional[str] = None,
    timestamp: Optional[float] = None,
) -> Dict[str, Any]:
    """Build a safe ``team_plan`` handoff event from a ``team.TeamPlan``.

    Captures team / role / category / toolsets / read-only / approval-guard
    metadata per node. The raw goal and the rendered card body (which embeds the
    goal) are intentionally excluded so no prompt is persisted by default.
    """
    nodes: List[Dict[str, Any]] = []
    for node in getattr(plan, "nodes", []) or []:
        nodes.append(
            {
                "local_id": getattr(node, "local_id", None),
                "role": getattr(node, "role", None),
                "name": getattr(node, "name", None),
                "category": getattr(node, "category", None),
                "toolsets": list(getattr(node, "toolsets", []) or []),
                "readonly": bool(getattr(node, "readonly", False)),
                "workspace": getattr(node, "workspace", None),
                "approval_guards": list(getattr(node, "approval_guards", []) or []),
                "parents_local": list(getattr(node, "parents_local", []) or []),
            }
        )
    return {
        "event_type": "team_plan",
        "timestamp": timestamp,
        "session_id": session_id,
        "team": getattr(plan, "team", None),
        "slug": getattr(plan, "slug", None),
        "tenant": getattr(plan, "tenant", None),
        "max_parallel": getattr(plan, "max_parallel", None),
        "approval_guards": list(getattr(plan, "approval_guards", []) or []),
        "nodes": nodes,
    }


# ---------------------------------------------------------------------------
# Recording (the only side-effecting surface)
# ---------------------------------------------------------------------------

def record_audit_event(
    cfg: Optional[Dict[str, Any]], event: Dict[str, Any]
) -> Optional[Path]:
    """Append a redacted ``event`` to the local JSONL bundle.

    No-op (returns ``None``, creates no file) when auditing is disabled. The
    event is recursively redacted before writing, and a ``timestamp`` is stamped
    when absent. Any I/O failure is swallowed (logged at debug) so auditing can
    never break a delegation run.
    """
    if not is_audit_enabled(cfg):
        return None
    try:
        safe = redact_audit_value(event)
        if isinstance(safe, dict) and not safe.get("timestamp"):
            safe["timestamp"] = time.time()
        path = audit_log_path(cfg)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(safe, ensure_ascii=False, sort_keys=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return path
    except Exception:  # pragma: no cover - audit must never break delegation
        logger.debug("delegation audit write failed", exc_info=True)
        return None
