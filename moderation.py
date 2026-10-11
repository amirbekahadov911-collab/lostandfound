"""Warnings, escalating mutes, bans and automatic spam detection."""
from __future__ import annotations

import html
import re
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Optional

from telegram import Bot, ChatPermissions, Message, User
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.error import TelegramError

import config
import db

# Punishment for each strike number. None = warning only, "ban" = permanent ban,
# otherwise a timedelta mute. Anything past the end of the list is a ban.
LADDER = [
    None,                    # strike 1: friendly warning
    timedelta(hours=1),      # strike 2: muted 1 hour
    timedelta(days=1),       # strike 3: muted 1 day
    timedelta(days=7),       # strike 4: muted 1 week
    "ban",                   # strike 5: banned
]
MAX_STRIKES = len(LADDER)


def punishment_for(strike: int):
    if strike <= 0:
        return None
    return LADDER[min(strike, MAX_STRIKES) - 1]


def describe(punishment) -> str:
    if punishment is None:
        return "a friendly warning"
    if punishment == "ban":
        return "a permanent ban"
    hours = int(punishment.total_seconds() // 3600)
    if hours < 24:
        return f"a {hours}-hour mute"
    days = hours // 24
    return f"a {days}-day mute" if days != 7 else "a 1-week mute"


def mention(user: User) -> str:
    return f'<a href="tg://user?id={user.id}">{html.escape(user.full_name)}</a>'


_admin_cache: tuple[float, set[int]] = (0.0, set())


async def is_admin(bot: Bot, user_id: int) -> bool:
    """Group admin check, cached for 5 minutes so we don't hit the API on every message."""
    global _admin_cache
    fetched_at, admins = _admin_cache
    if time.monotonic() - fetched_at > 300:
        try:
            admins = {m.user.id for m in await bot.get_chat_administrators(config.GROUP_ID)}
            _admin_cache = (time.monotonic(), admins)
        except TelegramError:
            pass
    return user_id in admins


def sent_by_anonymous_admin(message: Message) -> bool:
    return message.sender_chat is not None and message.sender_chat.id == config.GROUP_ID


async def is_group_member(bot: Bot, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(config.GROUP_ID, user_id)
    except TelegramError:
        return False
    if member.status == ChatMemberStatus.RESTRICTED:
        return bool(getattr(member, "is_member", False))
    return member.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)


def is_restricted(user_id: int) -> Optional[str]:
    """Return a reason string if this user may not post items right now."""
    row = db.get_strikes(user_id)
    if row["banned"]:
        return "banned"
    if row["muted_until"] and datetime.fromisoformat(row["muted_until"]) > datetime.now(timezone.utc):
        return "muted"
    return None


async def add_strike(bot: Bot, user: User, reason: str, by_user_id: Optional[int] = None) -> str:
    """Give the user one strike, apply the matching punishment, and return the group announcement."""
    strike = db.get_strikes(user.id)["count"] + 1
    db.set_strikes(user.id, strike)
    punishment = punishment_for(strike)
    next_up = describe(punishment_for(strike + 1)) if strike < MAX_STRIKES else None
    reason_html = html.escape(reason)

    if punishment == "ban":
        await ban(bot, user, reason, by_user_id, strike_already_added=True)
        return (
            f"⛔ {mention(user)} has been removed from the group after {strike} strikes.\n"
            f"Reason: <i>{reason_html}</i>\n\n"
            "We want this space to stay helpful for everyone. Thanks for understanding 🙏"
        )

    if punishment is None:
        db.log_action(user.id, by_user_id, "warn", reason)
        text = (
            f"⚠️ Hey {mention(user)}, this is a friendly heads-up (strike {strike}/{MAX_STRIKES}).\n"
            f"Reason: <i>{reason_html}</i>\n\n"
            "This group is just for helping people get their lost things back 💙 "
            f"If it happens again, the next step is {next_up}."
        )
    else:
        until = datetime.now(timezone.utc) + punishment
        try:
            await bot.restrict_chat_member(
                config.GROUP_ID, user.id, ChatPermissions.no_permissions(), until_date=until
            )
        except TelegramError:
            pass
        db.set_muted_until(user.id, until.isoformat())
        db.log_action(user.id, by_user_id, f"mute {punishment}", reason)
        text = (
            f"🔇 {mention(user)} has received {describe(punishment)} (strike {strike}/{MAX_STRIKES}).\n"
            f"Reason: <i>{reason_html}</i>\n\n"
            f"Please keep things respectful when you're back. Next step: {next_up}."
        )

    # Let the person know privately too (only works if they've started the bot before).
    try:
        await bot.send_message(
            user.id,
            f"Hi {html.escape(user.first_name)}, you received strike {strike}/{MAX_STRIKES} in the "
            f"Lost &amp; Found group for: <i>{reason_html}</i>.\n"
            "If you think this was a mistake, please reach out to one of the group admins.",
            parse_mode=ParseMode.HTML,
        )
    except TelegramError:
        pass
    return text


async def ban(bot: Bot, user: User, reason: str, by_user_id: Optional[int], strike_already_added: bool = False) -> None:
    if not strike_already_added:
        db.set_strikes(user.id, max(db.get_strikes(user.id)["count"], MAX_STRIKES))
    try:
        await bot.ban_chat_member(config.GROUP_ID, user.id)
    except TelegramError:
        pass
    db.set_banned(user.id, True)
    db.log_action(user.id, by_user_id, "ban", reason)


async def unban(bot: Bot, user_id: int, by_user_id: int) -> None:
    try:
        await bot.unban_chat_member(config.GROUP_ID, user_id, only_if_banned=True)
    except TelegramError:
        pass
    db.set_banned(user_id, False)
    # Coming back from a ban puts them one step below it, so the next strike bans again.
    db.set_strikes(user_id, MAX_STRIKES - 1)
    db.log_action(user_id, by_user_id, "unban", None)


async def unmute(bot: Bot, user_id: int, by_user_id: int) -> None:
    chat = await bot.get_chat(config.GROUP_ID)
    try:
        await bot.restrict_chat_member(config.GROUP_ID, user_id, chat.permissions or ChatPermissions.all_permissions())
    except TelegramError:
        pass
    db.set_muted_until(user_id, None)
    db.log_action(user_id, by_user_id, "unmute", None)


# ---------- automatic spam detection ----------

_recent: dict[int, deque] = defaultdict(deque)          # user_id -> timestamps
_recent_text: dict[int, deque] = defaultdict(deque)     # user_id -> (timestamp, text)
_LINK_RE = re.compile(r"(https?://|www\.|t\.me/|telegram\.me/)", re.IGNORECASE)


def detect_spam(message: Message) -> Optional[str]:
    """Return a reason if this message looks like spam, else None."""
    user_id = message.from_user.id
    now = time.monotonic()

    stamps = _recent[user_id]
    stamps.append(now)
    while stamps and now - stamps[0] > config.FLOOD_WINDOW:
        stamps.popleft()
    if len(stamps) > config.FLOOD_LIMIT:
        stamps.clear()
        return "sending too many messages too quickly"

    text = (message.text or message.caption or "").strip().lower()
    if text:
        texts = _recent_text[user_id]
        texts.append((now, text))
        while texts and now - texts[0][0] > 120:
            texts.popleft()
        if sum(1 for _, t in texts if t == text) >= 3:
            texts.clear()
            return "repeating the same message"

    if config.BLOCK_LINKS:
        entities = (message.entities or ()) + (message.caption_entities or ())
        if _LINK_RE.search(text) or any(e.type in ("url", "text_link") for e in entities):
            return "posting links (not allowed in this group)"

    return None
