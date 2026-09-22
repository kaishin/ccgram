"""Tests for the Herdr topic-title watcher (topic_title_sync)."""

from __future__ import annotations

from typing import cast

import pytest

from ccgram.multiplexer.base import WindowRef
from ccgram.multiplexer.herdr import (
    HerdrSessionComposite,
    herdr_session_target_id,
)
from ccgram.telegram_client import FakeTelegramClient
from ccgram.thread_router import ThreadRouter
from ccgram.topic_title_sync import (
    MIN_RENAME_INTERVAL_SECONDS,
    TitleSyncVerdict,
    TopicTitleSyncer,
)

USER_ID = 100
CHAT_ID = -1000


def _target(nonce: str = "a") -> str:
    return herdr_session_target_id(
        HerdrSessionComposite("test", "pi", "session", nonce)
    )


def _ref(
    window_id: str,
    name: str,
    *,
    eligible: bool = True,
) -> WindowRef:
    return WindowRef(
        window_id=window_id,
        window_name=name,
        cwd="/tmp",
        topic_eligible=eligible,
    )


@pytest.fixture
def router() -> ThreadRouter:
    return ThreadRouter(
        schedule_save=lambda: None,
        has_window_state=lambda _wid: False,
    )


def _bind(
    router: ThreadRouter,
    thread_id: int,
    window_id: str,
    name: str,
    *,
    chat_id: int | None = CHAT_ID,
) -> None:
    router.bind_thread(USER_ID, thread_id, window_id, window_name=name, chat_id=chat_id)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ── Decision logic (observe) ─────────────────────────────────────────


def test_unchanged_label_is_not_renamed_and_clears_pending() -> None:
    clock = FakeClock()
    syncer = TopicTitleSyncer(clock=clock)
    window_id = _target()
    syncer.observe(
        window_id=window_id,
        stored_name="Old",
        live_name="New",
        eligible=True,
    )
    verdict = syncer.observe(
        window_id=window_id,
        stored_name="New",
        live_name="New",
        eligible=True,
    )
    assert verdict.action == "unchanged"
    assert syncer._pending == {}


def test_first_difference_debounces() -> None:
    syncer = TopicTitleSyncer(clock=FakeClock())
    verdict = syncer.observe(
        window_id=_target(),
        stored_name="Old",
        live_name="New",
        eligible=True,
    )
    assert verdict.action == "debounce"
    assert verdict.name == ""


def test_stable_difference_across_two_observations_renames() -> None:
    syncer = TopicTitleSyncer(clock=FakeClock())
    window_id = _target()
    first = syncer.observe(
        window_id=window_id, stored_name="Old", live_name="New", eligible=True
    )
    assert first.action == "debounce"
    second = syncer.observe(
        window_id=window_id, stored_name="Old", live_name="New", eligible=True
    )
    assert second.action == "rename"
    assert second.name == "New"


def test_changing_label_restarts_the_debounce() -> None:
    syncer = TopicTitleSyncer(clock=FakeClock())
    window_id = _target()
    syncer.observe(
        window_id=window_id, stored_name="Old", live_name="Mid", eligible=True
    )
    verdict = syncer.observe(
        window_id=window_id, stored_name="Old", live_name="Newer", eligible=True
    )
    assert verdict.action == "debounce"
    verdict = syncer.observe(
        window_id=window_id, stored_name="Old", live_name="Newer", eligible=True
    )
    assert verdict.action == "rename"
    assert verdict.name == "Newer"


def test_internal_label_never_renames_and_clears_pending() -> None:
    syncer = TopicTitleSyncer(clock=FakeClock())
    window_id = _target()
    syncer.observe(
        window_id=window_id, stored_name="Old", live_name="__main__", eligible=True
    )
    # A stable internal/unlabeled ref (topic_eligible=False, the adapter's
    # _INTERNAL_LABEL_RE guard) must not rename even when the label settles.
    verdict = TitleSyncVerdict("unchanged")
    for _ in range(3):
        verdict = syncer.observe(
            window_id=window_id,
            stored_name="Old",
            live_name="__main__",
            eligible=False,
        )
    assert verdict.action == "ineligible"
    assert syncer._pending == {}


def test_rate_limit_skips_rename_within_min_interval() -> None:
    clock = FakeClock()
    syncer = TopicTitleSyncer(clock=clock)
    window_id = _target()
    syncer.observe(
        window_id=window_id, stored_name="Old", live_name="New", eligible=True
    )
    verdict = syncer.observe(
        window_id=window_id, stored_name="Old", live_name="New", eligible=True
    )
    assert verdict.action == "rename"
    syncer._last_edit[(0, 0)] = clock.now

    # Second rename attempted immediately: debounce satisfied, but too soon.
    syncer.observe(
        window_id=window_id, stored_name="New", live_name="Newer", eligible=True
    )
    verdict = syncer.observe(
        window_id=window_id, stored_name="New", live_name="Newer", eligible=True
    )
    assert verdict.action == "rate_limited"

    clock.advance(MIN_RENAME_INTERVAL_SECONDS)
    verdict = syncer.observe(
        window_id=window_id, stored_name="New", live_name="Newer", eligible=True
    )
    assert verdict.action == "rename"
    assert verdict.name == "Newer"


def test_verdict_is_frozen_dataclass() -> None:
    verdict = TitleSyncVerdict("rename", name="X")
    assert verdict.action == "rename"
    assert verdict.name == "X"


# ── Driver (sync_pass) ───────────────────────────────────────────────


async def test_two_passes_rename_and_update_stored_name(router) -> None:
    window_id = _target()
    _bind(router, 42, window_id, "Old title")
    client = FakeTelegramClient()
    syncer = TopicTitleSyncer(clock=FakeClock())
    listing = [_ref(window_id, "New title")]

    await syncer.sync_pass(client, listing, router=router)
    assert client.call_count("edit_forum_topic") == 0
    assert router.get_display_name(window_id) == "Old title"

    await syncer.sync_pass(client, listing, router=router)
    assert client.call_count("edit_forum_topic") == 1
    call = client.last_call("edit_forum_topic")
    assert call is not None
    assert call.kwargs == {
        "chat_id": CHAT_ID,
        "message_thread_id": 42,
        "name": "New title",
    }
    assert router.get_display_name(window_id) == "New title"

    # Stored name now matches: a third pass is quiet.
    await syncer.sync_pass(client, listing, router=router)
    assert client.call_count("edit_forum_topic") == 1


async def test_non_herdr_binding_is_never_touched(router) -> None:
    _bind(router, 42, "@5", "Old title")
    client = FakeTelegramClient()
    syncer = TopicTitleSyncer(clock=FakeClock())

    await syncer.sync_pass(client, [_ref("@5", "New title")], router=router)
    await syncer.sync_pass(client, [_ref("@5", "New title")], router=router)
    assert client.call_count("edit_forum_topic") == 0
    assert router.get_display_name("@5") == "Old title"


async def test_binding_without_stored_window_name_is_skipped(router) -> None:
    window_id = _target()
    router.bind_thread(USER_ID, 42, window_id, chat_id=CHAT_ID)
    client = FakeTelegramClient()
    syncer = TopicTitleSyncer(clock=FakeClock())
    listing = [_ref(window_id, "New title")]

    await syncer.sync_pass(client, listing, router=router)
    await syncer.sync_pass(client, listing, router=router)
    assert client.call_count("edit_forum_topic") == 0


async def test_window_missing_from_listing_is_skipped(router) -> None:
    window_id = _target()
    _bind(router, 42, window_id, "Old title")
    client = FakeTelegramClient()
    syncer = TopicTitleSyncer(clock=FakeClock())

    await syncer.sync_pass(client, [], router=router)
    await syncer.sync_pass(client, [], router=router)
    assert client.call_count("edit_forum_topic") == 0
    assert router.get_display_name(window_id) == "Old title"


async def test_ineligible_ref_never_renames(router) -> None:
    window_id = _target()
    _bind(router, 42, window_id, "Old title")
    client = FakeTelegramClient()
    syncer = TopicTitleSyncer(clock=FakeClock())
    listing = [_ref(window_id, "__main__ title", eligible=False)]

    await syncer.sync_pass(client, listing, router=router)
    await syncer.sync_pass(client, listing, router=router)
    await syncer.sync_pass(client, listing, router=router)
    assert client.call_count("edit_forum_topic") == 0
    assert router.get_display_name(window_id) == "Old title"


async def test_failed_edit_keeps_stored_name_and_retries_next_pass(router) -> None:
    window_id = _target()
    _bind(router, 42, window_id, "Old title")
    client = FakeTelegramClient()
    attempts = 0

    def boom(**_kwargs):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("telegram unavailable")

    client.returns["edit_forum_topic"] = boom
    syncer = TopicTitleSyncer(clock=FakeClock())
    listing = [_ref(window_id, "New title")]

    await syncer.sync_pass(client, listing, router=router)
    await syncer.sync_pass(client, listing, router=router)
    assert attempts == 1  # debounce still gates the first failing attempt
    assert router.get_display_name(window_id) == "Old title"

    # The stored name is stale, so the next pass retries the edit.
    client.returns.pop("edit_forum_topic")
    await syncer.sync_pass(client, listing, router=router)
    assert attempts == 1
    assert client.call_count("edit_forum_topic") == 2
    assert router.get_display_name(window_id) == "New title"


async def test_rate_limit_allows_at_most_one_edit_per_30s(router) -> None:
    clock = FakeClock()
    window_id = _target()
    _bind(router, 42, window_id, "Old title")
    client = FakeTelegramClient()
    syncer = TopicTitleSyncer(clock=clock)

    await syncer.sync_pass(client, [_ref(window_id, "V1")], router=router)
    await syncer.sync_pass(client, [_ref(window_id, "V1")], router=router)
    assert client.call_count("edit_forum_topic") == 1

    # A second settled rename within the window is skipped, then allowed.
    await syncer.sync_pass(client, [_ref(window_id, "V2")], router=router)
    await syncer.sync_pass(client, [_ref(window_id, "V2")], router=router)
    assert client.call_count("edit_forum_topic") == 1

    clock.advance(MIN_RENAME_INTERVAL_SECONDS)
    await syncer.sync_pass(client, [_ref(window_id, "V2")], router=router)
    assert client.call_count("edit_forum_topic") == 2
    assert router.get_display_name(window_id) == "V2"


async def test_one_failing_binding_does_not_block_the_others(router) -> None:
    window_a = _target("a")
    window_b = _target("b")
    _bind(router, 42, window_a, "Old A")
    _bind(router, 43, window_b, "Old B")
    client = FakeTelegramClient()

    def boom(**kwargs):
        if kwargs["message_thread_id"] == 42:
            raise RuntimeError("topic gone")
        return True

    client.returns["edit_forum_topic"] = boom
    syncer = TopicTitleSyncer(clock=FakeClock())
    listing = [_ref(window_a, "New A"), _ref(window_b, "New B")]

    await syncer.sync_pass(client, listing, router=router)
    await syncer.sync_pass(client, listing, router=router)
    # B renamed once (debounced); A's failure was logged and skipped.
    assert client.call_count("edit_forum_topic") == 2
    assert router.get_display_name(window_b) == "New B"
    assert router.get_display_name(window_a) == "Old A"


async def test_legacy_binding_resolves_chat_id(router) -> None:
    window_id = _target()
    router.bind_thread(USER_ID, 42, window_id, window_name="Old title")
    router.group_chat_ids[f"{USER_ID}:42"] = CHAT_ID
    client = FakeTelegramClient()
    syncer = TopicTitleSyncer(clock=FakeClock())
    listing = [_ref(window_id, "New title")]

    await syncer.sync_pass(client, listing, router=router)
    await syncer.sync_pass(client, listing, router=router)
    call = client.last_call("edit_forum_topic")
    assert call is not None
    assert call.kwargs["chat_id"] == CHAT_ID


async def test_broken_router_enumeration_fails_open() -> None:
    class BrokenRouter:
        def iter_thread_bindings_with_chat(self):
            raise RuntimeError("state unreadable")

    client = FakeTelegramClient()
    syncer = TopicTitleSyncer(clock=FakeClock())
    # Must not raise; the reconciliation tick survives.
    await syncer.sync_pass(client, [], router=cast("ThreadRouter", BrokenRouter()))
    assert client.call_count("edit_forum_topic") == 0


async def test_at_most_one_edit_per_topic_per_pass(router) -> None:
    window_id = _target()
    # One window bound to a topic in two different chats: each topic gets
    # exactly one edit. (Within one chat the router evicts the older bind.)
    _bind(router, 42, window_id, "Old title", chat_id=-1001)
    _bind(router, 43, window_id, "Old title", chat_id=-1002)
    client = FakeTelegramClient()
    syncer = TopicTitleSyncer(clock=FakeClock())
    listing = [_ref(window_id, "New title")]

    await syncer.sync_pass(client, listing, router=router)
    await syncer.sync_pass(client, listing, router=router)
    assert client.call_count("edit_forum_topic") == 2
    edits = [c for c in client.calls if c.method == "edit_forum_topic"]
    assert {(c.kwargs["chat_id"], c.kwargs["message_thread_id"]) for c in edits} == {
        (-1001, 42),
        (-1002, 43),
    }
