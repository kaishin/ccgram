#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.14"
# dependencies = ["python-telegram-bot>=22.8,<22.9"]
# ///
"""Discover custom-emoji stickers usable as Telegram forum-topic icons.

The Telegram Bot API restricts ``icon_custom_emoji`` on ``editForumTopic``
to a fixed pool of stickers exposed by ``getForumTopicIconStickers``.
This script prints every sticker in that pool so you can pick the IDs
to hardcode into ccgram's topic-icon mapping.

Usage:
    # Bot token from env, no chat context needed (the pool is global).
    TELEGRAM_BOT_TOKEN=... uv run scripts/discover_topic_icon_stickers.py

    # Or pass the token explicitly.
    uv run scripts/discover_topic_icon_stickers.py --token <TOKEN>

The pool is global to the bot, not per-chat, so this call works from
any script that can reach the Bot API. The output is plain text so
you can paste it into the conversation.

Output columns:
    index, custom_emoji_id, associated emoji, sticker set name, file_id

A typical interaction:
    1. Add a Premium sticker pack to yourself so its custom emoji are
       eligible to appear in the icon pool. (Bots can't upload custom
       emoji; this is a Premium-user action.)
    2. Run this script. Find the IDs whose associated emoji match the
       states you want to encode (green circle, yellow circle, etc.).
    3. Paste the relevant IDs back; I'll wire them into the topic-icon
       mapping in the fork.

Note: if the pool is empty, no Premium sticker pack has been added to
the account that owns the bot token. Add one first.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import pathlib
import sys

from telegram import Bot
from telegram.error import TelegramError


def _resolve_token(arg_token: str) -> str:
    """Find a bot token without ever prompting the user to paste one.

    Resolution order:
      1. ``--token`` argument (if non-empty).
      2. ``$TELEGRAM_BOT_TOKEN`` env var.
      3. The ``TELEGRAM_BOT_TOKEN=`` line in ``~/.ccgram/.env``.

    The third path is the default and the recommended one — it means
    this script can run without exposing the token in shell history
    or process listings. The token is read but never printed.
    """
    if arg_token:
        return arg_token.strip()
    env = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if env:
        return env
    env_path = pathlib.Path.home() / ".ccgram" / ".env"
    if env_path.is_file():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("TELEGRAM_BOT_TOKEN="):
                value = line.split("=", 1)[1].strip().strip('"').strip("'")
                if value:
                    return value
    return ""


async def discover(token: str) -> int:
    bot = Bot(token=token)
    try:
        stickers = await bot.get_forum_topic_icon_stickers()
    except TelegramError as exc:
        print(f"Bot API error: {exc}", file=sys.stderr)
        return 2

    if not stickers:
        print(
            "No custom-emoji stickers available as topic icons. "
            "Add a Premium sticker pack to the account that owns "
            "TELEGRAM_BOT_TOKEN, then re-run.",
            file=sys.stderr,
        )
        return 1

    # Width-pad each column for readability.
    print(f"Found {len(stickers)} icon-eligible sticker(s):")
    print()
    header = f"{'idx':>3}  {'custom_emoji_id':<20}  {'emoji':<6}  {'set':<32}  file_id"
    print(header)
    print("-" * len(header))
    for i, s in enumerate(stickers):
        cid = s.custom_emoji_id or ""
        emoji = s.emoji or ""
        set_name = s.set_name or ""
        file_id = s.file_id
        print(f"{i:>3}  {cid:<20}  {emoji:<6}  {set_name:<32}  {file_id}")

    print()
    print("Copy the custom_emoji_id column for the stickers you want.")
    print("Empty pool? Add a Premium pack to the bot-owning account first.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--token",
        default="",
        help=(
            "Bot token. Default: read from $TELEGRAM_BOT_TOKEN or "
            "~/.ccgram/.env (never type or paste the token — let the "
            "script read it)."
        ),
    )
    args = parser.parse_args()
    token = _resolve_token(args.token)
    if not token:
        print(
            "No bot token found. Set $TELEGRAM_BOT_TOKEN, or add "
            "TELEGRAM_BOT_TOKEN=... to ~/.ccgram/.env, or pass --token.",
            file=sys.stderr,
        )
        return 2
    return asyncio.run(discover(token))


if __name__ == "__main__":
    raise SystemExit(main())
