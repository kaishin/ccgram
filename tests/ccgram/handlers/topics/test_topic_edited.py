"""Tests for FORUM_TOPIC_EDITED handler (bidirectional name sync)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccgram.handlers.status.topic_icon import reset_all_state
from ccgram.handlers.topics.topic_lifecycle import topic_edited_handler

CHAT_ID = -100
THREAD_ID = 42


@pytest.fixture(autouse=True)
def _reset():
    reset_all_state()
    yield
    reset_all_state()


@pytest.fixture(autouse=True)
def allowed_user():
    with patch("ccgram.config.Config.is_user_allowed", return_value=True):
        yield


@pytest.fixture
def mux():
    with patch("ccgram.handlers.topics.topic_lifecycle.tmux_manager") as mock_mux:
        mock_mux.rename_window = AsyncMock(return_value=True)
        yield mock_mux


@pytest.fixture
def router():
    with patch("ccgram.handlers.topics.topic_lifecycle.thread_router") as mock_router:
        yield mock_router


@pytest.fixture
def session():
    with patch("ccgram.handlers.topics.topic_lifecycle.session_manager") as mock_sm:
        yield mock_sm


def _make_update(
    new_name: str | None,
    thread_id: int = THREAD_ID,
    chat_id: int = CHAT_ID,
    user_id: int = 1,
) -> MagicMock:
    """Create a mock Update for FORUM_TOPIC_EDITED."""
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.id = chat_id
    update.message.forum_topic_edited.name = new_name
    update.message.forum_topic_edited.icon_custom_emoji_id = None
    update.message.message_thread_id = thread_id
    return update


class TestTopicEditedRenamesWindow:
    @pytest.mark.parametrize(
        ("window_id", "old_display"),
        [("@0", "old-name"), ("w1:t1", "workspace ▸ old-agent")],
        ids=["tmux_window", "herdr_tab"],
    )
    async def test_rename_reaches_the_multiplexer_proxy(
        self,
        mux: MagicMock,
        router: MagicMock,
        session: MagicMock,
        window_id: str,
        old_display: str,
    ) -> None:
        router.get_window_for_chat_thread.return_value = window_id
        router.get_display_name.return_value = old_display

        await topic_edited_handler(_make_update("new-name"), MagicMock())

        mux.rename_window.assert_called_once_with(window_id, "new-name")
        session.set_display_name.assert_called_once_with(window_id, "new-name")


class TestTopicEditedStripsLegacyPrefix:
    """Migration: titles left by older ccgram installs that used the
    Unicode-emoji prefix scheme. The handler strips the prefix before
    propagating to the tmux window name so the window follows the
    bare Telegram title."""

    @pytest.mark.parametrize(
        "legacy_name",
        [
            "\U0001f7e2 myproject",  # green
            "\U0001f7e1 myproject",  # yellow
            "\u2705 myproject",  # check
            "\U0001f4a5 myproject",  # collision
            "\U0001f3b2 myproject",  # dice
            "\U0001f4e1 myproject",  # satellite
            "\u26ab myproject",  # legacy ⚫
            "\u274c myproject",  # legacy ❌
        ],
    )
    async def test_legacy_prefix_is_stripped(
        self,
        legacy_name: str,
        mux: MagicMock,
        router: MagicMock,
        session: MagicMock,
    ) -> None:
        router.get_window_for_chat_thread.return_value = "@0"
        router.get_display_name.return_value = "old-name"

        await topic_edited_handler(_make_update(legacy_name), MagicMock())

        mux.rename_window.assert_called_once_with("@0", "myproject")
        session.set_display_name.assert_called_once_with("@0", "myproject")


class TestTopicEditedIgnoredEdits:
    async def test_ignores_unchanged_name(
        self, mux: MagicMock, router: MagicMock
    ) -> None:
        """No-op when Telegram reports the same name we already have."""
        router.get_window_for_chat_thread.return_value = "@0"
        router.get_display_name.return_value = "myproject"

        await topic_edited_handler(_make_update("myproject"), MagicMock())

        mux.rename_window.assert_not_called()

    async def test_ignores_icon_only_edit(
        self, mux: MagicMock, router: MagicMock
    ) -> None:
        await topic_edited_handler(_make_update(None), MagicMock())

        router.get_window_for_chat_thread.assert_not_called()
        mux.rename_window.assert_not_called()

    async def test_ignores_unbound_topic(
        self, mux: MagicMock, router: MagicMock
    ) -> None:
        router.get_window_for_chat_thread.return_value = None

        await topic_edited_handler(_make_update("new-name"), MagicMock())

        mux.rename_window.assert_not_called()

    async def test_unchanged_after_failed_rename(
        self, mux: MagicMock, router: MagicMock
    ) -> None:
        router.get_window_for_chat_thread.return_value = "@0"
        router.get_display_name.return_value = "old-name"
        mux.rename_window = AsyncMock(return_value=False)

        await topic_edited_handler(_make_update("new-name"), MagicMock())

        router.set_display_name.assert_not_called()
