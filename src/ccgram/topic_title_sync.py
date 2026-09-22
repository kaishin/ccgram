"""Sync bound Telegram forum topic titles with live Herdr display labels.

The ``wenhanweime/herdr-plugin-renamer`` plugin retitles Herdr workspaces to
an LLM-generated session topic shortly after an agent starts working. ccgram
only sets a topic title at creation/bind time (``window_launch_service`` /
``window_callbacks``), so bound Telegram topics go stale. This module is the
watcher: on each reconciliation tick it compares the stored ``window_name``
of every bound thread with the live reconciliation listing's ``window_name``
— the same ``format_agent_topic_prefix`` projection the herdr adapter already
computes from ``herdr workspace list`` / ``herdr tab list`` labels in
``multiplexer/herdr.py::_project_live_refs`` — and renames the topic once the
new label has settled.

Guards (all fail open — every error is logged and never breaks the tick):

  - herdr only: a binding whose ``window_id`` is not a herdr session target
    is never touched, so tmux/agterm topics stay exactly as bound.
  - internal labels: a live ref herdr stamps ineligible — internal ``__*__``
    workspace/tab labels (the ``_INTERNAL_LABEL_RE`` guard in the adapter)
    and label-less records — never triggers a rename, mirroring how listings
    refuse to adopt them.
  - debounce: a new label must be observed on two consecutive ticks before a
    rename, so a transient label never churns the topic.
  - rate limit: at most one edit per topic per pass, and never within
    ``MIN_RENAME_INTERVAL_SECONDS`` of that topic's previous successful edit.
  - stored names update only after a successful ``edit_forum_topic``, so a
    failed edit retries on the next tick.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

import structlog

from .herdr_targets import is_herdr_session_target
from .multiplexer.base import WindowRef, canonical_window_id

if TYPE_CHECKING:
    from .telegram_client import TelegramClient
    from .thread_router import ThreadRouter

__all__ = [
    "MIN_RENAME_INTERVAL_SECONDS",
    "TitleSyncVerdict",
    "TopicTitleSyncer",
    "sync_topic_titles",
]

logger = structlog.get_logger()

# Minimum gap between successful renames of the same topic. Telegram's forum
# topic edit is rate-limited; a settled Herdr label does not get less settled
# for waiting one extra tick.
MIN_RENAME_INTERVAL_SECONDS = 30.0

TitleSyncAction = Literal[
    "rename", "debounce", "unchanged", "ineligible", "rate_limited"
]

# One Telegram topic: (chat_id, message_thread_id).
TopicKey = tuple[int, int]


@dataclass(frozen=True)
class TitleSyncVerdict:
    """One binding's decision for a single watcher observation."""

    action: TitleSyncAction
    name: str = ""
    """The settled live label; only set when ``action == "rename"``."""


@dataclass
class _SyncPassState:
    """Per-pass shared state, so one pass decides from one consistent view.

    ``stored_names`` is a snapshot: a successful rename updates the router,
    but every binding in this pass keeps comparing against the snapshot, so
    two topics bound to the same renamed window both get the edit instead of
    the second one reading the first one's update as "already synced".
    ``edited`` enforces the one-edit-per-topic-per-pass cap.
    """

    stored_names: dict[str, str]
    edited: set[TopicKey] = field(default_factory=set)


class TopicTitleSyncer:
    """Debounced, rate-limited topic-title watcher.

    Holds the cross-tick state (pending labels per window, last successful
    edit per topic). The decision step is a pure function of that state plus
    one observation, so tests drive it with a fake clock.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] | None = None,
        min_interval: float = MIN_RENAME_INTERVAL_SECONDS,
    ) -> None:
        self._clock = clock or time.monotonic
        self._min_interval = min_interval
        # window_id -> (label awaiting a second consecutive observation, the
        # pass sequence that first saw it).
        self._pending: dict[str, tuple[str, int]] = {}
        # topic_key -> monotonic timestamp of the last successful rename.
        self._last_edit: dict[TopicKey, float] = {}
        self._pass_seq = 0

    def observe(
        self,
        *,
        window_id: str,
        stored_name: str,
        live_name: str,
        eligible: bool,
        topic_key: TopicKey = (0, 0),
        tick: int | None = None,
    ) -> TitleSyncVerdict:
        """Decide one observation for one bound window.

        ``eligible`` is the live ref's ``topic_eligible`` stamp: the herdr
        adapter sets it False for internal ``__*__`` labels and unlabeled
        records, which is exactly the ``_INTERNAL_LABEL_RE`` guard applied to
        the same listing this name came from. ``topic_key`` scopes the rate
        limit; pure callers without a topic keep the (0, 0) placeholder.
        ``tick`` is the reconciliation pass sequence: a label first seen in
        this same pass (by another binding to the same window) is one
        observation, not two consecutive ticks. Pure callers leave it None,
        which treats any recorded pending label as stable.
        """
        if not eligible:
            self._pending.pop(window_id, None)
            return TitleSyncVerdict("ineligible")
        if live_name == stored_name:
            self._pending.pop(window_id, None)
            return TitleSyncVerdict("unchanged")
        pending = self._pending.get(window_id)
        if pending is None or pending[0] != live_name:
            # First sighting of this label: record it and wait for the next
            # tick to prove it is not transient.
            self._pending[window_id] = (live_name, tick if tick is not None else -1)
            return TitleSyncVerdict("debounce")
        if tick is not None and pending[1] == tick:
            # A second binding for the same window in this same pass saw the
            # same listing — that is still the first observation.
            return TitleSyncVerdict("debounce")
        last = self._last_edit.get(topic_key)
        if last is not None and self._clock() - last < self._min_interval:
            return TitleSyncVerdict("rate_limited")
        return TitleSyncVerdict("rename", name=live_name)

    async def sync_pass(
        self,
        client: "TelegramClient",
        live_windows: Sequence[WindowRef],
        *,
        router: "ThreadRouter | None" = None,
    ) -> None:
        """Run one watcher pass over the tick's reconciliation listing.

        Fail open throughout: one broken binding, one Telegram error, even a
        broken router iteration is logged and skipped; the audit pass that
        invoked this continues.
        """
        if router is None:
            # Lazy: mirrors session_monitor's deferred thread_router import.
            from .thread_router import thread_router as router
        try:
            refs = {
                canonical_window_id(window.window_id): window
                for window in live_windows
                if is_herdr_session_target(window.window_id)
            }
            bindings = list(router.iter_thread_bindings_with_chat())
            state = _SyncPassState(stored_names=dict(router.window_display_names))
        except Exception:
            logger.exception("topic title sync: cannot enumerate bindings")
            return
        self._pass_seq += 1
        for user_id, chat_id, thread_id, window_id in bindings:
            try:
                await self._sync_binding(
                    client,
                    router,
                    state,
                    user_id,
                    chat_id,
                    thread_id,
                    window_id,
                    refs,
                )
            except Exception:
                logger.exception(
                    "topic title sync: binding failed; will retry next pass",
                    window_id=window_id,
                    thread_id=thread_id,
                )

    async def _sync_binding(
        self,
        client: "TelegramClient",
        router: "ThreadRouter",
        state: _SyncPassState,
        user_id: int,
        chat_id: int | None,
        thread_id: int,
        window_id: str,
        refs: dict[str, WindowRef],
    ) -> None:
        """Sync one bound thread, if its window is a live, eligible herdr ref."""
        if not is_herdr_session_target(window_id):
            return
        stored = state.stored_names.get(window_id)
        if not stored:
            # No stored window_name — nothing this watcher promised to follow.
            return
        ref = refs.get(canonical_window_id(window_id))
        if ref is None:
            # Not in this tick's listing (quarantined or between listings):
            # liveness/audit owns that question, never this watcher.
            return
        resolved_chat_id = (
            chat_id
            if chat_id is not None
            else router.resolve_chat_id(user_id, thread_id)
        )
        if not resolved_chat_id:
            return
        topic_key = (resolved_chat_id, thread_id)
        if topic_key in state.edited:
            # At most one edit per topic per audit cycle.
            return
        verdict = self.observe(
            window_id=window_id,
            stored_name=stored,
            live_name=ref.window_name,
            eligible=ref.topic_eligible,
            topic_key=topic_key,
            tick=self._pass_seq,
        )
        if verdict.action != "rename":
            return
        try:
            await client.edit_forum_topic(
                chat_id=resolved_chat_id,
                message_thread_id=thread_id,
                name=verdict.name,
            )
        except Exception:
            # Deliberately broad fail-open: a Telegram error, a network
            # reset, anything. The stored name stays stale so the next tick
            # retries the edit.
            logger.exception(
                "topic title sync: rename failed; will retry next pass",
                window_id=window_id,
                thread_id=thread_id,
            )
            return
        state.edited.add(topic_key)
        router.set_display_name(window_id, verdict.name)
        self._last_edit[topic_key] = self._clock()
        # The label is now proven stable: keep it as the pending entry with a
        # non-tick sentinel so another topic bound to this same window in this
        # same pass renames too instead of restarting the debounce.
        self._pending[window_id] = (verdict.name, -1)
        logger.info(
            "synced topic title to live Herdr label",
            window_id=window_id,
            thread_id=thread_id,
            name=verdict.name,
        )


_topic_title_syncer = TopicTitleSyncer()


async def sync_topic_titles(
    client: "TelegramClient", live_windows: Sequence[WindowRef]
) -> None:
    """Run one watcher pass; every failure is logged, never raised."""
    try:
        await _topic_title_syncer.sync_pass(client, live_windows)
    except Exception:
        logger.exception("topic title sync pass failed")
