"""Backend-neutral projection of multiplexer windows onto Telegram topics.

Consumes the multiplexer seam; it is **not** part of the ``Multiplexer``
contract (which stops at opaque ``window_id``). It defines how a backend's
windows/tabs project onto ccgram's flat ``group → topic`` structure.

The design maps each reported Herdr agent to one Telegram topic. Herdr
``WindowRef.window_id`` is an opaque target, never a raw tab, pane, or other
locator. A published agent session has a durable target; a sessionless detected
agent does not become a topic. A bare shell record does not become a topic, and
raw locator aliases are never auto-migrated. tmux behavior remains unchanged.

Lives in ``multiplexer/`` (not ``handlers/``) so both the core session monitor
and the topic handlers can import it without crossing the F1 boundary, and
because it is pure logic over the neutral value types.
"""

from __future__ import annotations

from .base import MultiplexerCapabilities, WindowRef

# Separates an optional pane suffix from the tab label in a Herdr topic title.
# Only used for multi-pane tabs; single-pane tabs inherit the tab label verbatim.
TOPIC_PREFIX_SEPARATOR = " ▸ "


def format_agent_topic_label(
    tab_label: str, pane_count: int, pane_id: str
) -> str:
    """Render a Herdr topic label from the tab's own name, verbatim.

    Herdr's renamer plugin produces a per-tab session topic that already names
    the work in progress, so the topic title just inherits it. A pane suffix
    is only appended when the tab holds more than one pane; the single-pane
    case is the common one and the ``p1`` suffix is noise. The status emoji
    is prepended later by the topic-emoji machinery.

    An empty *tab_label* degrades to the lone pane suffix when one applies,
    or empty string otherwise, so a half-populated tab never renders a stray
    separator.
    """
    tab = tab_label.strip()
    suffix = ""
    if pane_count > 1:
        suffix = f"{TOPIC_PREFIX_SEPARATOR}{pane_id.rsplit(':', 1)[-1]}"
    return f"{tab}{suffix}"


def is_agent_topic_window(window: WindowRef, caps: MultiplexerCapabilities) -> bool:
    """Return True when a discovered window should surface as its own topic.

    The backend decides, in ``WindowRef.topic_eligible``. No capability flag
    gates discovery: keying it on one is what produced two bugs in a row. It
    was first keyed on ``native_agent_status``, which made every agterm session
    permanently ineligible the moment agterm began reporting status natively;
    keying it on ``native_topic_targets`` instead only moved the overload onto
    a flag whose documented meaning is which creation flow to use.

    Only the backend can answer this. herdr knows a record carries a guarded
    target and an agent label, agterm knows its workspace scope and how to read
    a wrapped argv, and tmux excludes only the placeholder main window, its
    own, and the ``_``-prefixed ones its complete listing marks ineligible
    instead of dropping. ``caps`` stays in the signature
    because it is the seam's shape and callers pass it.
    """
    del caps
    return window.topic_eligible
