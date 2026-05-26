"""Generic slash-command confirmation primitive (gateway-side).

Slash commands that have a non-destructive but expensive side effect worth
surfacing to the user (currently only ``/reload-mcp``, which invalidates
the provider prompt cache) route through this module.

Two delivery paths:

  1. Button UI — adapters that override ``send_slash_confirm`` render
     three inline buttons (Approve Once / Always Approve / Cancel).  The
     button callback calls ``resolve(session_key, confirm_id, choice)``.

  2. Text fallback — adapters without button UIs get a plain text prompt.
     Users reply with ``/approve``, ``/always``, or ``/cancel``; the
     gateway's ``_handle_message`` intercepts those replies and calls
     ``resolve()`` directly.

State is stored module-level (like ``tools.approval``) so platform
adapters can resolve callbacks without needing a backreference to the
``GatewayRunner`` instance.  The CLI path (``cli.py``) uses a local
synchronous variant — see ``_prompt_slash_confirm`` there.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
import time
from typing import Any, Awaitable, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# Pending confirmations keyed by gateway session_key.  Each entry:
#   {
#       "confirm_id": str,
#       "command":    str,                       # e.g. "reload-mcp"
#       "handler":    Callable[[str], Awaitable[Optional[str]]],
#       "created_at": float,                     # time.time()
#   }
_pending: Dict[str, Dict[str, Any]] = {}
_lock = threading.RLock()

# Default timeout — a pending confirm older than this is discarded when
# the next message arrives for the same session.  Buttons work up until
# the adapter drops the callback_data (Telegram: ~48h; Discord: ephemeral;
# Slack: 3s ack + long-lived actions).
DEFAULT_TIMEOUT_SECONDS = 300


def register(
    session_key: str,
    confirm_id: str,
    command: str,
    handler: Callable[[str], Awaitable[Optional[str]]],
    *,
    requester_platform: Optional[str] = None,
    requester_user_id: Optional[str] = None,
) -> None:
    """Register a pending slash-command confirmation.

    Overwrites any prior pending confirm for the same ``session_key`` — the
    user invoking a new confirmable command supersedes the stale one.  When a
    requester identity is supplied, resolve() only accepts approval/cancel from
    the same platform user; this is used for ACL mutations where another user in
    the channel must not be able to approve a pending change.
    """
    with _lock:
        _pending[session_key] = {
            "confirm_id": confirm_id,
            "command": command,
            "handler": handler,
            "created_at": time.time(),
            "requester_platform": str(requester_platform or ""),
            "requester_user_id": str(requester_user_id or ""),
        }


def get_pending(session_key: str) -> Optional[Dict[str, Any]]:
    """Return the pending confirm dict for a session, or None."""
    with _lock:
        entry = _pending.get(session_key)
        return dict(entry) if entry else None


def clear(session_key: str) -> None:
    """Drop the pending confirm for ``session_key`` without running it."""
    with _lock:
        _pending.pop(session_key, None)


def clear_if_stale(session_key: str, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> bool:
    """Drop the pending confirm if older than ``timeout`` seconds.

    Returns True if an entry was dropped.
    """
    with _lock:
        entry = _pending.get(session_key)
        if not entry:
            return False
        if time.time() - float(entry.get("created_at", 0) or 0) > timeout:
            _pending.pop(session_key, None)
            return True
        return False


async def resolve(
    session_key: str,
    confirm_id: str,
    choice: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    *,
    requester_platform: Optional[str] = None,
    requester_user_id: Optional[str] = None,
) -> Optional[str]:
    """Resolve a pending confirm.

    ``choice`` must be one of ``"once"``, ``"always"``, or ``"cancel"``.
    Returns the handler's output string (to be sent as a follow-up
    message), or ``None`` if the confirm was stale, already resolved, or
    the confirm_id doesn't match.

    If the pending entry has requester identity, a mismatched requester is
    rejected without consuming the pending confirmation.

    Safe to call from an asyncio callback (button click) or from the
    gateway's message intercept path.
    """
    with _lock:
        entry = _pending.get(session_key)
        if not entry:
            return None
        if entry.get("confirm_id") != confirm_id:
            # Stale confirm_id — superseded by a newer prompt on the same session.
            return None
        bound_platform = str(entry.get("requester_platform") or "")
        bound_user = str(entry.get("requester_user_id") or "")
        if bound_platform or bound_user:
            got_platform = str(requester_platform or "")
            got_user = str(requester_user_id or "")
            if got_platform != bound_platform or got_user != bound_user:
                return "Only the requester can approve or cancel this pending command."
        # Pop before we run the handler to prevent duplicate callbacks
        # (e.g. button double-click) from running it twice.
        _pending.pop(session_key, None)
        if time.time() - float(entry.get("created_at", 0) or 0) > timeout:
            return None
        handler = entry.get("handler")
        command = entry.get("command", "?")

    if not handler:
        return None
    try:
        result = await handler(choice)
    except Exception as exc:
        logger.error(
            "Slash-confirm handler for /%s raised: %s",
            command, exc, exc_info=True,
        )
        return f"❌ Error handling confirmation: {exc}"
    return result if isinstance(result, str) else None


async def resolve_for_requester(
    session_key: str,
    confirm_id: str,
    choice: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    *,
    requester_platform: Optional[str] = None,
    requester_user_id: Optional[str] = None,
) -> Optional[str]:
    """Resolve a pending confirm with requester identity when supported.

    Some gateway tests and third-party adapters monkeypatch ``resolve`` with
    the old 3-argument signature. Keep those call sites compatible while the
    real primitive enforces requester binding.
    """
    try:
        signature = inspect.signature(resolve)
        params = signature.parameters
        has_varkw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
        supports_timeout = "timeout" in params or has_varkw
        supports_requester_binding = (
            ("requester_platform" in params and "requester_user_id" in params)
            or has_varkw
        )
    except (TypeError, ValueError):
        supports_timeout = True
        supports_requester_binding = True

    if not supports_requester_binding or not supports_timeout:
        if requester_platform or requester_user_id:
            return (
                "Requester-bound confirmation cannot be verified by the legacy "
                "resolver; command not approved."
            )
        return await resolve(session_key, confirm_id, choice)

    return await resolve(
        session_key,
        confirm_id,
        choice,
        timeout=timeout,
        requester_platform=requester_platform,
        requester_user_id=requester_user_id,
    )


def resolve_sync_compat(
    loop: asyncio.AbstractEventLoop,
    session_key: str,
    confirm_id: str,
    choice: str,
) -> Optional[str]:
    """Synchronous helper: schedule resolve() on a loop and wait for the result.

    Used by platform callback paths that run on a different thread than the
    event loop (e.g. Discord's button click handler in some configurations).
    Prefer the async ``resolve()`` from an async context.
    """
    try:
        from agent.async_utils import safe_schedule_threadsafe
        fut = safe_schedule_threadsafe(
            resolve(session_key, confirm_id, choice), loop,
            logger=logger,
            log_message="resolve_sync_compat scheduling failed",
        )
        if fut is None:
            return None
        return fut.result(timeout=30)
    except Exception as exc:
        logger.error("resolve_sync_compat failed: %s", exc)
        return None
