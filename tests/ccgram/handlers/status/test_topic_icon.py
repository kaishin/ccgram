"""Tests for ccgram.handlers.status.topic_icon — icon-only fork behavior.

Covers:
  * Icon selection priority (RC > YOLO > state)
  * Debounce (active 5s, idle 30s, done/dead 5s)
  * Inherited-topic first paint skips debounce
  * Same-token no-op
  * State-change debounce
  * Rapid toggling suppressed
  * Permission errors disable chat
  * TOPIC_NOT_MODIFIED tracks state
  * Telegram errors ignored
  * Legacy prefix migration strip
  * Remote Control + YOLO priority over state
  * /sync sleeps out chat spacing
  * Flood-control cooldown
  * Burst pacing for inherited topics
  * Same-topic updates not paced
  * Paced name change survives deferral
  * sync_lock cleanup never replaces held chat lock
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.error import BadRequest, RetryAfter, TelegramError

from _helpers import make_mock_provider

from ccgram.handlers.status.topic_icon import (
    DEBOUNCE_TERMINAL_SECONDS,
    DEBOUNCE_TO_ACTIVE_SECONDS,
    DEBOUNCE_TO_IDLE_SECONDS,
    TOPIC_ICON_IDS,
    clear_topic_icon_state,
    mark_awaiting_first_paint,
    reset_all_state,
    strip_legacy_prefix,
    sync_topic_icon,
    update_topic_icon,
)


_DEBOUNCE_FOR: dict[str, float] = {
    "active": DEBOUNCE_TO_ACTIVE_SECONDS,
    "idle": DEBOUNCE_TO_IDLE_SECONDS,
    "done": DEBOUNCE_TERMINAL_SECONDS,
    "dead": DEBOUNCE_TERMINAL_SECONDS,
}


def _debounce_for(state: str) -> float:
    return _DEBOUNCE_FOR[state]


@pytest.fixture(autouse=True)
def _reset():
    from ccgram.handlers.polling.polling_state import terminal_poll_state

    reset_all_state()
    terminal_poll_state.reset_all_seen_status()
    yield
    reset_all_state()
    terminal_poll_state.reset_all_seen_status()


# ── TOPIC_ICON_IDS ────────────────────────────────────────────────────


class TestTopicIconIds:
    def test_has_all_four_states(self) -> None:
        for state in ("active", "idle", "done", "dead"):
            assert state in TOPIC_ICON_IDS
            assert TOPIC_ICON_IDS[state].isdigit()

    def test_has_rc_and_yolo(self) -> None:
        assert "rc" in TOPIC_ICON_IDS
        assert "yolo" in TOPIC_ICON_IDS
        assert TOPIC_ICON_IDS["rc"].isdigit()
        assert TOPIC_ICON_IDS["yolo"].isdigit()

    def test_all_ids_are_unique(self) -> None:
        ids = list(TOPIC_ICON_IDS.values())
        assert len(ids) == len(set(ids))


# ── strip_legacy_prefix ───────────────────────────────────────────────


class TestStripLegacyPrefix:
    @pytest.mark.parametrize(
        "prefix",
        [
            "\U0001f7e2",  # green
            "\U0001f7e1",  # yellow
            "\u2705",  # check
            "\U0001f4a5",  # collision
            "\U0001f3b2",  # dice
            "\U0001f4e1",  # satellite
            "\u26ab",  # legacy ⚫
            "\u274c",  # legacy ❌
        ],
    )
    def test_strips_each_legacy_prefix(self, prefix: str) -> None:
        assert strip_legacy_prefix(f"{prefix} myproject") == "myproject"

    def test_returns_unchanged_when_no_prefix(self) -> None:
        assert strip_legacy_prefix("myproject") == "myproject"

    def test_strips_only_first_prefix(self) -> None:
        # If somehow a name carries two prefixes (shouldn't happen in
        # production), only the first is stripped — preserves the
        # invariant that ``strip_emoji_prefix`` had.
        assert (
            strip_legacy_prefix("\U0001f7e2 \U0001f7e1 myproject")
            == "\U0001f7e1 myproject"
        )


# ── Icon selection priority ───────────────────────────────────────────


class TestIconSelectionPriority:
    async def test_rc_overrides_state(self, monkeypatch: pytest.MonkeyPatch) -> None:
        bot = AsyncMock()
        with (
            patch(
                "ccgram.handlers.status.topic_icon._resolve_approval_mode",
                return_value="normal",
            ),
            patch(
                "ccgram.handlers.status.topic_icon._resolve_rc_mode", return_value=True
            ),
        ):
            await _debounced_update(bot, -100, 42, "active", "myproject")
        bot.edit_forum_topic.assert_called_once_with(
            chat_id=-100,
            message_thread_id=42,
            icon_custom_emoji_id=TOPIC_ICON_IDS["rc"],
        )

    async def test_yolo_overrides_state(self, monkeypatch: pytest.MonkeyPatch) -> None:
        bot = AsyncMock()
        with (
            patch(
                "ccgram.handlers.status.topic_icon._resolve_approval_mode",
                return_value="yolo",
            ),
            patch(
                "ccgram.handlers.status.topic_icon._resolve_rc_mode", return_value=False
            ),
        ):
            await _debounced_update(bot, -100, 42, "active", "myproject")
        bot.edit_forum_topic.assert_called_once_with(
            chat_id=-100,
            message_thread_id=42,
            icon_custom_emoji_id=TOPIC_ICON_IDS["yolo"],
        )

    async def test_rc_overrides_yolo(self, monkeypatch: pytest.MonkeyPatch) -> None:
        bot = AsyncMock()
        with (
            patch(
                "ccgram.handlers.status.topic_icon._resolve_approval_mode",
                return_value="yolo",
            ),
            patch(
                "ccgram.handlers.status.topic_icon._resolve_rc_mode", return_value=True
            ),
        ):
            await _debounced_update(bot, -100, 42, "active", "myproject")
        bot.edit_forum_topic.assert_called_once_with(
            chat_id=-100,
            message_thread_id=42,
            icon_custom_emoji_id=TOPIC_ICON_IDS["rc"],
        )


# ── update_topic_icon ─────────────────────────────────────────────────


_PATCH_MONOTONIC = "ccgram.handlers.status.topic_icon.time.monotonic"


async def _debounced_update(
    bot: AsyncMock,
    chat_id: int,
    thread_id: int,
    state: str,
    display_name: str,
) -> None:
    with patch(_PATCH_MONOTONIC) as mock_monotonic:
        mock_monotonic.return_value = 0.0
        await update_topic_icon(bot, chat_id, thread_id, state, display_name)
        mock_monotonic.return_value = _debounce_for(state) + 0.1
        await update_topic_icon(bot, chat_id, thread_id, state, display_name)


class TestInheritedTopicsRepaintImmediately:
    async def test_seeded_topic_paints_without_waiting(self) -> None:
        bot = AsyncMock()
        mark_awaiting_first_paint(-100, 42)

        with patch(_PATCH_MONOTONIC, return_value=0.0):
            await update_topic_icon(bot, -100, 42, "idle", "myproject")

        bot.edit_forum_topic.assert_called_once_with(
            chat_id=-100,
            message_thread_id=42,
            icon_custom_emoji_id=TOPIC_ICON_IDS["idle"],
        )

    async def test_only_the_first_sighting_skips_the_debounce(self) -> None:
        bot = AsyncMock()
        mark_awaiting_first_paint(-100, 42)
        with patch(_PATCH_MONOTONIC, return_value=0.0):
            await update_topic_icon(bot, -100, 42, "idle", "myproject")
        bot.edit_forum_topic.reset_mock()

        with patch(_PATCH_MONOTONIC, return_value=0.0):
            await update_topic_icon(bot, -100, 42, "active", "myproject")

        bot.edit_forum_topic.assert_not_called()

    async def test_unseeded_topic_still_debounces(self) -> None:
        bot = AsyncMock()
        mark_awaiting_first_paint(-100, 42)

        with patch(_PATCH_MONOTONIC, return_value=0.0):
            await update_topic_icon(bot, -100, 99, "idle", "other")

        bot.edit_forum_topic.assert_not_called()


_STATE_ICONS = [
    ("active", TOPIC_ICON_IDS["active"]),
    ("idle", TOPIC_ICON_IDS["idle"]),
    ("done", TOPIC_ICON_IDS["done"]),
    ("dead", TOPIC_ICON_IDS["dead"]),
]


class TestUpdateTopicIcon:
    async def test_first_call_starts_debounce(self) -> None:
        bot = AsyncMock()
        with patch(_PATCH_MONOTONIC, return_value=0.0):
            await update_topic_icon(bot, -100, 42, "active", "myproject")
        bot.edit_forum_topic.assert_not_called()

    @pytest.mark.parametrize("state,icon_id", _STATE_ICONS)
    async def test_sets_icon_after_debounce(self, state: str, icon_id: str) -> None:
        bot = AsyncMock()
        await _debounced_update(bot, -100, 42, state, "myproject")
        bot.edit_forum_topic.assert_called_once_with(
            chat_id=-100,
            message_thread_id=42,
            icon_custom_emoji_id=icon_id,
        )

    async def test_skips_same_state(self) -> None:
        bot = AsyncMock()
        await _debounced_update(bot, -100, 42, "active", "myproject")
        bot.edit_forum_topic.reset_mock()
        await update_topic_icon(bot, -100, 42, "active", "myproject")
        bot.edit_forum_topic.assert_not_called()

    async def test_updates_on_state_change(self) -> None:
        bot = AsyncMock()
        await _debounced_update(bot, -100, 42, "active", "myproject")
        bot.edit_forum_topic.reset_mock()
        await _debounced_update(bot, -100, 42, "idle", "myproject")
        bot.edit_forum_topic.assert_called_once()

    async def test_legacy_prefix_does_not_change_title(self) -> None:
        """A legacy title is not included in an icon-only edit."""
        bot = AsyncMock()
        await _debounced_update(bot, -100, 42, "idle", "\U0001f7e2 myproject")
        bot.edit_forum_topic.assert_called_once_with(
            chat_id=-100,
            message_thread_id=42,
            icon_custom_emoji_id=TOPIC_ICON_IDS["idle"],
        )

    async def test_rapid_toggling_suppressed(self) -> None:
        bot = AsyncMock()
        with patch(_PATCH_MONOTONIC) as mock_monotonic:
            for i in range(10):
                mock_monotonic.return_value = float(i)
                state = "active" if i % 2 == 0 else "idle"
                await update_topic_icon(bot, -100, 42, state, "myproject")
        bot.edit_forum_topic.assert_not_called()

    async def test_permission_error_disables_chat(self) -> None:
        bot = AsyncMock()
        bot.edit_forum_topic.side_effect = BadRequest("Not enough rights")
        await _debounced_update(bot, -100, 42, "active", "myproject")
        bot.edit_forum_topic.reset_mock()
        await _debounced_update(bot, -100, 42, "idle", "myproject")
        bot.edit_forum_topic.assert_not_called()

    async def test_topic_not_modified_still_tracks(self) -> None:
        bot = AsyncMock()
        bot.edit_forum_topic.side_effect = BadRequest("TOPIC_NOT_MODIFIED")
        await _debounced_update(bot, -100, 42, "active", "myproject")
        bot.edit_forum_topic.reset_mock()
        await update_topic_icon(bot, -100, 42, "active", "myproject")
        bot.edit_forum_topic.assert_not_called()

    async def test_other_telegram_error_ignored(self) -> None:
        bot = AsyncMock()
        bot.edit_forum_topic.side_effect = TelegramError("Network error")
        await _debounced_update(bot, -100, 42, "active", "myproject")
        assert bot.edit_forum_topic.called

    async def test_invalid_state_ignored(self) -> None:
        bot = AsyncMock()
        await update_topic_icon(bot, -100, 42, "unknown", "myproject")
        bot.edit_forum_topic.assert_not_called()

    async def test_active_fires_faster_than_idle(self) -> None:
        bot = AsyncMock()
        midpoint = DEBOUNCE_TO_ACTIVE_SECONDS + 0.1
        assert midpoint < DEBOUNCE_TO_IDLE_SECONDS

        with patch(_PATCH_MONOTONIC) as mock_monotonic:
            mock_monotonic.return_value = 0.0
            await update_topic_icon(bot, -100, 42, "active", "myproject")
            mock_monotonic.return_value = midpoint
            await update_topic_icon(bot, -100, 42, "active", "myproject")
        bot.edit_forum_topic.assert_called_once()

    async def test_idle_does_not_fire_at_active_debounce_time(self) -> None:
        bot = AsyncMock()
        midpoint = DEBOUNCE_TO_ACTIVE_SECONDS + 0.1
        assert midpoint < DEBOUNCE_TO_IDLE_SECONDS

        with patch(_PATCH_MONOTONIC) as mock_monotonic:
            mock_monotonic.return_value = 0.0
            await update_topic_icon(bot, -100, 42, "idle", "myproject")
            mock_monotonic.return_value = midpoint
            await update_topic_icon(bot, -100, 42, "idle", "myproject")
        bot.edit_forum_topic.assert_not_called()

    async def test_brief_pause_during_work_stays_active(self) -> None:
        bot = AsyncMock()
        with patch(_PATCH_MONOTONIC) as mock_monotonic:
            mock_monotonic.return_value = 0.0
            await update_topic_icon(bot, -100, 42, "active", "myproject")
            mock_monotonic.return_value = DEBOUNCE_TO_ACTIVE_SECONDS + 0.1
            await update_topic_icon(bot, -100, 42, "active", "myproject")
        assert bot.edit_forum_topic.call_count == 1
        bot.edit_forum_topic.reset_mock()

        with patch(_PATCH_MONOTONIC) as mock_monotonic:
            mock_monotonic.return_value = 10.0
            await update_topic_icon(bot, -100, 42, "idle", "myproject")
            mock_monotonic.return_value = 20.0
            await update_topic_icon(bot, -100, 42, "active", "myproject")
        bot.edit_forum_topic.assert_not_called()


class TestClearTopicIconState:
    async def test_clear_resets_pending_transition(self) -> None:
        bot = AsyncMock()
        with patch(_PATCH_MONOTONIC, return_value=0.0):
            await update_topic_icon(bot, -100, 42, "active", "myproject")
        clear_topic_icon_state(-100, 42)
        with patch(_PATCH_MONOTONIC) as mock_monotonic:
            mock_monotonic.return_value = 100.0
            await update_topic_icon(bot, -100, 42, "active", "myproject")
            bot.edit_forum_topic.assert_not_called()
            mock_monotonic.return_value = 100.0 + _debounce_for("active") + 0.1
            await update_topic_icon(bot, -100, 42, "active", "myproject")
        bot.edit_forum_topic.assert_called_once()


class TestSyncTopicIcon:
    async def test_no_op_without_cached_state(self) -> None:
        """If /sync runs before the bot has observed any state for the
        topic, there's nothing to repair — don't fire a Telegram call."""
        from ccgram.handlers.status.topic_icon import _topic_states

        bot = AsyncMock()
        _topic_states.pop((-100, 42), None)
        await sync_topic_icon(bot, -100, 42, "myproject")
        bot.edit_forum_topic.assert_not_called()

    async def test_preserves_cached_state_and_refreshes_icon(self) -> None:
        from ccgram.handlers.status.topic_icon import _topic_states

        bot = AsyncMock()
        _topic_states[(-100, 42)] = ("idle", "normal", False)
        with (
            patch(
                "ccgram.handlers.status.topic_icon._resolve_approval_mode",
                return_value="normal",
            ),
            patch(
                "ccgram.handlers.status.topic_icon._resolve_rc_mode",
                return_value=False,
            ),
        ):
            await sync_topic_icon(bot, -100, 42, "myproject")

        bot.edit_forum_topic.assert_called_once_with(
            chat_id=-100,
            message_thread_id=42,
            icon_custom_emoji_id=TOPIC_ICON_IDS["idle"],
        )

    async def test_legacy_prefix_does_not_change_title_in_sync(self) -> None:
        from ccgram.handlers.status.topic_icon import _topic_states

        bot = AsyncMock()
        _topic_states[(-100, 42)] = ("active", "normal", False)
        with (
            patch(
                "ccgram.handlers.status.topic_icon._resolve_approval_mode",
                return_value="normal",
            ),
            patch(
                "ccgram.handlers.status.topic_icon._resolve_rc_mode",
                return_value=False,
            ),
        ):
            await sync_topic_icon(bot, -100, 42, "\U0001f7e2 myproject")

        bot.edit_forum_topic.assert_called_once_with(
            chat_id=-100,
            message_thread_id=42,
            icon_custom_emoji_id=TOPIC_ICON_IDS["active"],
        )


_APPLY = "ccgram.handlers.polling.window_tick.apply"


@contextmanager
def _status_poll_env(*, has_status: bool, pane_command: str = "node"):
    """Patch the window-tick collaborators and yield ``(bot, mock_icon)``."""
    with (
        patch(f"{_APPLY}.tmux_manager") as mock_tm,
        patch(f"{_APPLY}.window_query"),
        patch(f"{_APPLY}.thread_router") as mock_tr,
        patch(f"{_APPLY}.update_topic_icon") as mock_icon,
        patch(f"{_APPLY}.enqueue_status_update"),
        patch(f"{_APPLY}.get_interactive_window", return_value=None),
        patch(
            f"{_APPLY}.get_provider_for_window",
            return_value=make_mock_provider(has_status=has_status),
        ),
    ):
        window = MagicMock()
        window.pane_current_command = pane_command
        mock_tm.find_window_by_id = AsyncMock(return_value=window)
        mock_tm.capture_pane = AsyncMock(return_value="some output")
        mock_tm.get_pane_title = AsyncMock(return_value="")
        mock_tr.resolve_chat_id.return_value = -100
        mock_tr.get_display_name.return_value = "myproject"
        yield AsyncMock(), mock_icon, mock_tr


class TestStatusPollingIntegration:
    """The 1s poll resolves a window state and hands it to update_topic_icon."""

    async def test_active_window_with_status_updates_icon(self) -> None:
        from ccgram.handlers.polling.window_tick import _update_status

        with _status_poll_env(has_status=True) as (bot, mock_icon, _tr):
            await _update_status(bot, 1, "@0", thread_id=42)

        from ccgram.telegram_client import PTBTelegramClient

        mock_icon.assert_called_once()
        args = mock_icon.call_args.args
        assert isinstance(args[0], PTBTelegramClient)
        assert args[0].bot is bot
        assert args[1:] == (-100, 42, "active", "myproject")

    async def test_idle_window_without_status_updates_icon(self) -> None:
        from ccgram.handlers.polling.polling_state import terminal_poll_state
        from ccgram.handlers.polling.window_tick import _update_status

        terminal_poll_state.get_state("@0").has_seen_status = True

        with _status_poll_env(has_status=False) as (bot, mock_icon, _tr):
            await _update_status(bot, 1, "@0", thread_id=42)

        from ccgram.telegram_client import PTBTelegramClient

        mock_icon.assert_called_once()
        args = mock_icon.call_args.args
        assert isinstance(args[0], PTBTelegramClient)
        assert args[0].bot is bot
        assert args[1:] == (-100, 42, "idle", "myproject")

    async def test_no_thread_id_skips_icon(self) -> None:
        from ccgram.handlers.polling.window_tick import _update_status

        with _status_poll_env(has_status=True) as (bot, mock_icon, _tr):
            await _update_status(bot, 1, "@0", thread_id=None)

        mock_icon.assert_not_called()


class TestRemoteControlAndYoloBadges:
    async def test_rc_active_uses_rc_icon(self) -> None:
        bot = AsyncMock()
        with (
            patch(
                "ccgram.handlers.status.topic_icon._resolve_approval_mode",
                return_value="normal",
            ),
            patch(
                "ccgram.handlers.status.topic_icon._resolve_rc_mode", return_value=True
            ),
        ):
            await _debounced_update(bot, -100, 42, "active", "myproject")
        bot.edit_forum_topic.assert_called_once_with(
            chat_id=-100,
            message_thread_id=42,
            icon_custom_emoji_id=TOPIC_ICON_IDS["rc"],
        )

    async def test_yolo_mode_uses_yolo_icon(self) -> None:
        bot = AsyncMock()
        with (
            patch(
                "ccgram.handlers.status.topic_icon._resolve_approval_mode",
                return_value="yolo",
            ),
            patch(
                "ccgram.handlers.status.topic_icon._resolve_rc_mode", return_value=False
            ),
        ):
            await _debounced_update(bot, -100, 42, "active", "myproject")
        bot.edit_forum_topic.assert_called_once_with(
            chat_id=-100,
            message_thread_id=42,
            icon_custom_emoji_id=TOPIC_ICON_IDS["yolo"],
        )


MOD = "ccgram.handlers.status.topic_icon"


class TestFloodControlCooldown:
    """Upstream #199: a RetryAfter on a topic rename must pause the chat's
    renames instead of silently re-arming the debounce forever."""

    async def test_retry_after_pauses_renames_for_the_chat(self) -> None:
        from ccgram.handlers.status.topic_icon import FLOOD_COOLDOWN_SECONDS

        bot = AsyncMock()
        bot.edit_forum_topic.side_effect = RetryAfter(3)
        mark_awaiting_first_paint(-100, 42)

        with patch(_PATCH_MONOTONIC) as mock_monotonic:
            mock_monotonic.return_value = 0.0
            await update_topic_icon(bot, -100, 42, "idle", "myproject")

            bot.edit_forum_topic.reset_mock()
            bot.edit_forum_topic.side_effect = None
            await update_topic_icon(bot, -100, 42, "active", "myproject")
            await sync_topic_icon(bot, -100, 42, "myproject")
            bot.edit_forum_topic.assert_not_called()

            mock_monotonic.return_value = FLOOD_COOLDOWN_SECONDS + 0.1
            await update_topic_icon(bot, -100, 42, "active", "myproject")
            mock_monotonic.return_value = (
                FLOOD_COOLDOWN_SECONDS + 0.1 + _debounce_for("active") + 0.1
            )
            await update_topic_icon(bot, -100, 42, "active", "myproject")
        bot.edit_forum_topic.assert_called_once()


class TestFirstPaintPacing:
    """Upstream #199: the inherited-topic repaint burst at startup must be
    spaced across poll cycles, one topic per chat at a time."""

    async def test_burst_of_inherited_topics_is_paced(self) -> None:
        from ccgram.handlers.status.topic_icon import CHAT_EDIT_MIN_INTERVAL

        bot = AsyncMock()
        mark_awaiting_first_paint(-100, 1)
        mark_awaiting_first_paint(-100, 2)

        with patch(_PATCH_MONOTONIC) as mock_monotonic:
            mock_monotonic.return_value = 0.0
            await update_topic_icon(bot, -100, 1, "idle", "alpha")
            await update_topic_icon(bot, -100, 2, "idle", "beta")
            assert bot.edit_forum_topic.await_count == 1

            mock_monotonic.return_value = CHAT_EDIT_MIN_INTERVAL + 0.1
            await update_topic_icon(bot, -100, 2, "idle", "beta")
        assert bot.edit_forum_topic.await_count == 2

    async def test_topic_cleanup_keeps_chat_pacing_stamp(self) -> None:
        """Greptile #206: the pacing stamp is chat-scoped and must survive
        a single topic's teardown (only the permission set is cleared)."""
        from ccgram.handlers.status.topic_icon import (
            _disabled_chats,
            _last_chat_edit,
            clear_disabled_chat,
        )

        _disabled_chats.add(-100)
        _last_chat_edit[-100] = (0.0, (-100, 1))

        clear_disabled_chat(-100, 42)

        assert -100 not in _disabled_chats
        assert _last_chat_edit[-100] == (0.0, (-100, 1))
