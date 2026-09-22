"""Topic icon updates via editForumTopic — icon-only fork behavior.

This module replaces the previous Unicode-emoji title-prefix scheme
with Telegram's native ``icon_custom_emoji_id`` parameter. The topic
title is never included in the edit request; only the icon is mutated.

Icon selection — six custom-emoji IDs are hardcoded in
``TOPIC_ICON_IDS``:

  * ``active`` / ``idle`` / ``done`` / ``dead`` — session lifecycle
  * ``rc`` — Remote Control active (highest priority)
  * ``yolo`` — YOLO / approval-skip mode

Selection priority: ``rc > yolo > state``. When RC is active, the RC
icon wins regardless of YOLO or session state. When in YOLO mode
without RC, the YOLO icon wins. Otherwise the state icon is shown.

Topic titles are deliberately never changed. Existing titles,
including any legacy Unicode-emoji prefixes, are left exactly as-is.

Debounce, pacing, and flood-control rules are preserved from the
title-prefix scheme:

  * active (5s):  fast feedback for the user
  * idle   (30s): brief pauses during work don't cause flicker
  * done/dead (5s): meaningful lifecycle events fire fast

The chat-edit pacing stamp and RetryAfter flood cooldown are unchanged;
these icon edits are still subject to Telegram's per-chat rate limits.
"""

import asyncio
import time
from weakref import WeakValueDictionary

import structlog
from telegram.error import BadRequest, RetryAfter, TelegramError

from ...telegram_client import TelegramClient
from ...telegram_rate_limiter import retry_after_seconds
from ...thread_router import thread_router
from ...topic_state_registry import topic_state
from ...window_query import get_approval_mode

logger = structlog.get_logger()

# Hardcoded custom-emoji IDs (Telegram Bot API ``icon_custom_emoji``).
TOPIC_ICON_IDS: dict[str, str] = {
    "active": "5417915203100613993",
    "idle": "5350392020785437399",
    "done": "5237699328843200968",
    "dead": "5379748062124056162",
    "rc": "5350513667144163474",
    "yolo": "5350422527938141909",
}

# Stale Unicode-emoji prefixes from earlier ccgram installs. The
# icon-only fork never adds these — the strip step exists purely to
# migrate existing topics to bare names on first icon update.
_LEGACY_TITLE_PREFIXES: tuple[str, ...] = (
    "\U0001f7e2",  # 🟢 green circle (active, system mode)
    "\U0001f7e1",  # 🟡 yellow circle (idle, system mode)
    "\u2705",  # ✅ check mark (done)
    "\U0001f4a5",  # 💥 collision (dead)
    "\U0001f3b2",  # 🎲 dice (yolo badge)
    "\U0001f4e1",  # 📡 satellite dish (RC badge)
    "\u26ab",  # ⚫ legacy dead pre-2026-02
    "\u274c",  # ❌ legacy dead pre-2026-03
)


def _select_icon_id(state: str, approval_mode: str, rc_active: bool) -> str:
    """Pick the custom-emoji ID for the current state triple.

    Telegram allows only one ``icon_custom_emoji`` per topic, so the
    most-specific signal wins: RC > YOLO > state. Raises ``KeyError``
    if ``state`` is not one of the four known lifecycle states
    (``active``, ``idle``, ``done``, ``dead``) — callers gate on the
    ``state`` value before invoking this.
    """
    if rc_active:
        return TOPIC_ICON_IDS["rc"]
    if approval_mode == "yolo":
        return TOPIC_ICON_IDS["yolo"]
    return TOPIC_ICON_IDS[state]


def strip_legacy_prefix(name: str) -> str:
    """Remove a stale Unicode-emoji prefix from a topic title.

    Returns the name unchanged if no legacy prefix is present. The
    candidate list is the closed set of glyphs older ccgram versions
    emitted; users who customized via TOML had their own glyphs but
    those installs are now migrated to the icon-only behavior.
    """
    for prefix in _LEGACY_TITLE_PREFIXES:
        candidate = f"{prefix} "
        if name.startswith(candidate):
            return name[len(candidate) :]
    return name


# Debounce: state must be stable for this many seconds before updating the topic.
# Prevents rapid active↔idle toggling from flooding chat with rename calls.
#
# Asymmetric by design (same as the title-prefix scheme):
#   → active (5s):  fast feedback — user sees "agent is working" quickly
#   → idle (30s):   slow transition — brief pauses during work don't cause flicker
#   → done/dead (5s): meaningful lifecycle events, fire fast
DEBOUNCE_TO_ACTIVE_SECONDS = 5.0
DEBOUNCE_TO_IDLE_SECONDS = 30.0
DEBOUNCE_TERMINAL_SECONDS = 5.0  # done, dead

_DEBOUNCE_BY_STATE: dict[str, float] = {
    "active": DEBOUNCE_TO_ACTIVE_SECONDS,
    "idle": DEBOUNCE_TO_IDLE_SECONDS,
    "done": DEBOUNCE_TERMINAL_SECONDS,
    "dead": DEBOUNCE_TERMINAL_SECONDS,
}

# Topic state tracking: (chat_id, thread_id) -> (state, approval_mode, rc_active).
_topic_states: dict[tuple[int, int], tuple[str, str, bool]] = {}

# Pending transitions: (chat_id, thread_id) -> (desired_state, first_seen_monotonic).
# See the title-prefix module's history for the backdate rationale —
# unchanged here because the pacing + debounce invariants are identical.
_pending_transitions: dict[tuple[int, int], tuple[str, float]] = {}

# Topics inherited at process startup. Their first observed state replaces the
# stale state left by the previous process without waiting for debounce.
_awaiting_first_paint: set[tuple[int, int]] = set()

# Chats where editForumTopic is disabled due to permission errors
_disabled_chats: set[int] = set()

# Flood-control cooldown (upstream #199): after a RetryAfter on a topic
# rename, renames for that chat pause entirely for this long.
FLOOD_COOLDOWN_SECONDS = 300.0

# Minimum spacing between renames of DIFFERENT topics in the same chat.
CHAT_EDIT_MIN_INTERVAL = 1.5

# chat_id -> monotonic time until which renames are paused (lazily expires).
_flood_cooldown_until: dict[int, float] = {}
# chat_id -> (monotonic time of the last rename attempt, its topic key).
_last_chat_edit: dict[int, tuple[float, tuple[int, int]]] = {}

# Serializes /sync-driven icon updates within each chat so the per-chat
# spacing is slept out rather than raced against by other sync tasks.
_sync_rename_locks: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()


def _flood_paused(chat_id: int, now: float) -> bool:
    """True while the chat's renames are paused after flood control."""
    until = _flood_cooldown_until.get(chat_id)
    if until is None:
        return False
    if now >= until:
        _flood_cooldown_until.pop(chat_id, None)
        return False
    return True


def _pause_renames_for_flood(chat_id: int) -> None:
    """Start (or extend) the chat-wide rename flood cooldown."""
    _flood_cooldown_until[chat_id] = time.monotonic() + FLOOD_COOLDOWN_SECONDS


def _paced_out(chat_id: int, key: tuple[int, int], now: float) -> bool:
    """True when a rename of ``key`` must wait: a different topic of the
    same chat was renamed within CHAT_EDIT_MIN_INTERVAL."""
    last = _last_chat_edit.get(chat_id)
    if last is None:
        return False
    last_ts, last_key = last
    return last_key != key and now - last_ts < CHAT_EDIT_MIN_INTERVAL


def _should_apply_update(
    key: tuple[int, int],
    state: str,
    state_token: tuple[str, str, bool],
    now: float,
) -> bool:
    """Return True when the icon update should be sent to Telegram."""
    if _topic_states.get(key) == state_token:
        _pending_transitions.pop(key, None)
        return False

    if key in _awaiting_first_paint:
        _awaiting_first_paint.discard(key)
        _pending_transitions.pop(key, None)
        return True

    pending = _pending_transitions.get(key)
    if pending is None or pending[0] != state:
        _pending_transitions[key] = (state, now)
        return False

    debounce = _DEBOUNCE_BY_STATE.get(state, DEBOUNCE_TO_IDLE_SECONDS)
    if now - pending[1] < debounce:
        return False

    _pending_transitions.pop(key, None)
    return True


def _resolve_approval_mode(chat_id: int, thread_id: int) -> str:
    """Resolve approval mode for a topic via session bindings."""
    window_id = thread_router.get_window_for_chat_thread(chat_id, thread_id)
    if not window_id:
        return "normal"
    return get_approval_mode(window_id)


def _resolve_rc_mode(chat_id: int, thread_id: int) -> bool:
    """Resolve Remote Control active state for a topic via session bindings."""
    window_id = thread_router.get_window_for_chat_thread(chat_id, thread_id)
    if not window_id:
        return False
    # Lazy: polling_state cycle — same as title-prefix module's sites.
    from ..polling.polling_state import terminal_screen_buffer

    return terminal_screen_buffer.is_rc_active(window_id)


async def _edit_topic_icon(
    client: TelegramClient,
    chat_id: int,
    thread_id: int,
    key: tuple[int, int],
    display_name: str,
    icon_id: str,
    *,
    state_token: tuple[str, str, bool] | None = None,
) -> None:
    """Apply a topic icon update with shared Telegram error handling."""
    try:
        await client.edit_forum_topic(
            chat_id=chat_id,
            message_thread_id=thread_id,
            icon_custom_emoji_id=icon_id,
        )
        if state_token is not None:
            _topic_states[key] = state_token
        logger.debug(
            "Updated topic icon: chat=%d thread=%d name='%s' icon=%s",
            chat_id,
            thread_id,
            display_name,
            icon_id[:8] + "…",
        )
    except RetryAfter as exc:
        _pause_renames_for_flood(chat_id)
        logger.warning(
            "Flood control on topic icon update for chat %d (retry_after %ss): "
            "pausing this chat's renames for %.0fs",
            chat_id,
            retry_after_seconds(exc),
            FLOOD_COOLDOWN_SECONDS,
        )
    except BadRequest as e:
        if "Not enough rights" in e.message:
            _disabled_chats.add(chat_id)
            logger.info(
                "Topic icon disabled for chat %d: insufficient permissions",
                chat_id,
            )
        elif (
            "topic_not_modified" in e.message.lower() or "Topic_id_invalid" in e.message
        ):
            if state_token is not None:
                _topic_states[key] = state_token
        else:
            logger.debug("Failed to update topic icon: %s", e)
    except TelegramError:
        pass


async def sync_topic_icon(
    client: TelegramClient,
    chat_id: int,
    thread_id: int,
    display_name: str,
) -> None:
    """Reapply the cached icon for a topic.

    Called from ``/sync`` to repair stale icons after a process
    restart. Sleeps out the per-chat spacing stamp instead of
    deferring to a later poll cycle, the same way the title-prefix
    module's ``sync_topic_name`` did (title-prefix scheme).
    """
    if chat_id in _disabled_chats:
        return

    key = (chat_id, thread_id)
    approval_mode = _resolve_approval_mode(chat_id, thread_id)
    rc_active = _resolve_rc_mode(chat_id, thread_id)
    cached = _topic_states.get(key)
    state_token = (cached[0], approval_mode, rc_active) if cached else None
    if state_token is None:
        return
    icon_id = _select_icon_id(state_token[0], state_token[1], state_token[2])

    lock = _sync_rename_locks.setdefault(chat_id, asyncio.Lock())
    async with lock:
        now = time.monotonic()
        if _flood_paused(chat_id, now):
            return
        for _ in range(3):
            last = _last_chat_edit.get(chat_id)
            if (
                last is None
                or last[1] == key
                or now - last[0] >= CHAT_EDIT_MIN_INTERVAL
            ):
                break
            await asyncio.sleep(CHAT_EDIT_MIN_INTERVAL - (now - last[0]))
            now = time.monotonic()
            if _flood_paused(chat_id, now):
                return
        _last_chat_edit[chat_id] = (now, key)
        await _edit_topic_icon(
            client,
            chat_id,
            thread_id,
            key,
            display_name,
            icon_id,
            state_token=state_token,
        )


async def update_topic_icon(
    client: TelegramClient,
    chat_id: int,
    thread_id: int,
    state: str,
    display_name: str,
) -> None:
    """Update a topic's icon to reflect session state.

    Debounces transitions: the new state must be requested consistently
    for the debounce period before the API call is made. This prevents
    rapid active/idle flickering from generating lots of edit calls.

    The topic title is never passed to Telegram; ``display_name`` is
    retained only for diagnostic logging.
    """
    if chat_id in _disabled_chats:
        return

    key = (chat_id, thread_id)

    approval_mode = _resolve_approval_mode(chat_id, thread_id)
    rc_active = _resolve_rc_mode(chat_id, thread_id)
    state_token = (state, approval_mode, rc_active)

    if state not in _DEBOUNCE_BY_STATE:
        return

    now = time.monotonic()
    if _flood_paused(chat_id, now):
        return
    if not _should_apply_update(key, state, state_token, now=now):
        return
    if _paced_out(chat_id, key, now):
        # Different topic was just renamed; defer to next poll cycle.
        # Backdate the debounce so the rename fires without sitting
        # through the full period again.
        _pending_transitions[key] = (
            state,
            now - _DEBOUNCE_BY_STATE.get(state, DEBOUNCE_TO_IDLE_SECONDS),
        )
        return
    _last_chat_edit[chat_id] = (now, key)

    icon_id = _select_icon_id(state, approval_mode, rc_active)
    await _edit_topic_icon(
        client,
        chat_id,
        thread_id,
        key,
        display_name,
        icon_id,
        state_token=state_token,
    )


@topic_state.register("chat")
def clear_topic_icon_state(chat_id: int, thread_id: int) -> None:
    """Clear icon tracking for a topic (called on topic cleanup)."""
    key = (chat_id, thread_id)
    _topic_states.pop(key, None)
    _pending_transitions.pop(key, None)
    _awaiting_first_paint.discard(key)


def mark_awaiting_first_paint(chat_id: int, thread_id: int) -> None:
    """Let an inherited topic apply its next observed state without debounce."""
    _awaiting_first_paint.add((chat_id, thread_id))


_MAX_DISABLED_CHATS = 1000


@topic_state.register("chat")
def clear_disabled_chat(chat_id: int, _thread_id: int = 0) -> None:
    """Clear chat-scoped rename state on topic cleanup: the permission
    disabled set only. The flood cooldown and the pacing stamp are NOT
    cleared here: both are chat-wide and must outlive any single topic's
    teardown."""
    _disabled_chats.discard(chat_id)
    if len(_disabled_chats) > _MAX_DISABLED_CHATS:
        _disabled_chats.clear()


def reset_all_state() -> None:
    """Reset all tracking state (for testing)."""
    _topic_states.clear()
    _pending_transitions.clear()
    _awaiting_first_paint.clear()
    _disabled_chats.clear()
    _flood_cooldown_until.clear()
    _last_chat_edit.clear()
    _sync_rename_locks.clear()
