"""Status subpackage — status bubble, status-bar callbacks, topic icon.

Bundles the modules that own the per-topic status surface:
``status_bubble`` (status message lifecycle, task-list formatting,
status-to-content conversion), ``status_bar_actions`` (status-bar
callbacks that remain reachable via legacy inline buttons — notify
toggle, recall, remote control, esc, quick keys), and ``topic_icon``
(forum topic icon updates via Telegram's ``icon_custom_emoji``
parameter, with debounced state transitions).

Public surface re-exported here is the entry point for ``bot.py`` and
the rest of ``handlers/``; internals stay in the per-module files.
"""

from .status_bar_actions import build_dashboard_button
from .status_bubble import (
    clear_status_message,
    clear_status_msg_info,
    convert_status_to_content,
    process_status_clear,
    process_status_update,
    send_status_text,
)
from .topic_icon import (
    TOPIC_ICON_IDS,
    clear_disabled_chat,
    clear_topic_icon_state,
    mark_awaiting_first_paint,
    reset_all_state,
    sync_topic_icon,
    update_topic_icon,
)

__all__ = [
    "TOPIC_ICON_IDS",
    "build_dashboard_button",
    "clear_disabled_chat",
    "clear_status_message",
    "clear_status_msg_info",
    "clear_topic_icon_state",
    "convert_status_to_content",
    "mark_awaiting_first_paint",
    "process_status_clear",
    "process_status_update",
    "reset_all_state",
    "send_status_text",
    "sync_topic_icon",
    "update_topic_icon",
]
