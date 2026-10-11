"""School Lost & Found Telegram bot.

People DM the bot to report found items, the bot posts them to the group,
and every school morning it reposts everything that's still unclaimed.
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import random
import re
import sys
import warnings
from datetime import date, datetime, timedelta, timezone

from telegram import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ChatType, ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    TypeHandler,
    filters,
)
from telegram.warnings import PTBUserWarning

import config
import db
import moderation

warnings.filterwarnings("ignore", message=".*per_message.*", category=PTBUserWarning)
logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("lostfound")

HTML = ParseMode.HTML
esc = html.escape

# Only respond in private chats and in the configured school group.
ALLOWED_CHATS = filters.ChatType.PRIVATE | filters.Chat(config.GROUP_ID)
PRIVATE = filters.ChatType.PRIVATE

NEW_LINES = [
    "Just handed in! If it's yours, go grab it 🙌",
    "Fresh find! Know who this belongs to? Let them know 💙",
    "Someone's going to be very happy to see this again 😊",
]
REMINDER_LINES = [
    "Still waiting for its owner. Could it be yours? 👀",
    "Ring a bell? Swing by and pick it up! 🙌",
    "Know whose this is? Give them a nudge 💙",
    "This one would love to go home 🏡",
]
HEADER_LINES = [
    "☀️ Good morning, everyone!",
    "👋 Hello again, school!",
    "🌤 Morning check-in time!",
]
CLOSED_LINES = {
    "returned": "✅ <b>Returned to its owner!</b> Thanks for helping 💙",
    "removed": "🗑 <b>This post was removed.</b>",
    "expired": "⌛ <b>No longer in the daily reminders.</b> Ask at the claim point if you think it's yours.",
}


# ---------- formatting helpers ----------

def fmt_day(d: date) -> str:
    return f"{d:%a}, {d.day} {d:%b %Y}"


def posted_on(item) -> date:
    return datetime.fromisoformat(item["created_at"]).astimezone(config.TIMEZONE).date()


def item_caption(item, kind: str) -> str:
    """kind: new | reminder | preview | returned | removed | expired"""
    number = f"#{item['id']}" if item["id"] else ""
    lines = [
        f"<b>🔎 FOUND ITEM {number}</b>",
        "",
        f"📦 <b>Item:</b> {esc(item['description'])}",
        f"📅 <b>Found on:</b> {fmt_day(date.fromisoformat(item['found_date']))}",
        f"📍 <b>Found at:</b> {esc(item['found_location'])}",
        f"🏫 <b>Claim at:</b> {esc(item['claim_location'])}",
        f"🗓 <b>Posted:</b> {fmt_day(posted_on(item))}",
    ]
    if kind == "reminder":
        waiting = (datetime.now(config.TIMEZONE).date() - posted_on(item)).days
        if waiting > 0:
            lines.append(f"⏳ <b>Waiting:</b> {waiting} day{'s' if waiting != 1 else ''}")
    lines.append("")
    if kind in CLOSED_LINES:
        lines.append(CLOSED_LINES[kind])
    elif kind == "reminder":
        lines.append(f"<i>{random.choice(REMINDER_LINES)}</i>")
    elif kind == "new":
        lines.append(f"<i>{random.choice(NEW_LINES)}</i>")
    return "\n".join(lines)


def group_keyboard(item_id: int, bot_username: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Owner found", callback_data=f"ret:{item_id}")],
        [InlineKeyboardButton("📮 Found something? Tell me", url=f"https://t.me/{bot_username}?start=report")],
    ])


def owner_keyboard(item_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Owner found", callback_data=f"ret:{item_id}"),
        InlineKeyboardButton("🗑 Remove", callback_data=f"del:{item_id}"),
    ]])


async def send_item(bot, chat_id: int, item, kind: str, reply_markup=None, disable_notification=False):
    text = item_caption(item, kind)
    if item["photo_file_id"]:
        return await bot.send_photo(chat_id, item["photo_file_id"], caption=text, parse_mode=HTML,
                                    reply_markup=reply_markup, disable_notification=disable_notification)
    return await bot.send_message(chat_id, text, parse_mode=HTML,
                                  reply_markup=reply_markup, disable_notification=disable_notification)


async def close_item(bot, item, status: str) -> None:
    """Take an item out of the loop and update its group post."""
    db.set_status(item["id"], status)
    for message_id in db.pop_reminder_messages(item["id"]):
        try:
            await bot.delete_message(config.GROUP_ID, message_id)
        except TelegramError:
            pass

    message_id = item["group_message_id"]
    if not message_id:
        return
    if status == "removed":
        try:
            await bot.delete_message(config.GROUP_ID, message_id)
            return
        except TelegramError:
            pass  # Too old to delete (48h limit), so mark it instead.
    text = item_caption(item, status)
    try:
        if item["photo_file_id"]:
            await bot.edit_message_caption(chat_id=config.GROUP_ID, message_id=message_id, caption=text, parse_mode=HTML)
        else:
            await bot.edit_message_text(text, chat_id=config.GROUP_ID, message_id=message_id, parse_mode=HTML)
    except TelegramError:
        pass


# ---------- general commands ----------

WELCOME = (
    "👋 Hi {name}! I'm the school's Lost &amp; Found helper.\n\n"
    "Here's what I can do for you:\n"
    "📮 /report: tell me about something you found, and I'll post it to the group\n"
    "🔎 /list: see everything that's still waiting for its owner\n"
    "🗂 /mine: manage the items you've posted (mark them returned or remove them)\n"
    "❓ /help: show this message again\n\n"
    "Every school morning I remind the group about unclaimed items, so nothing gets lost in the chat 💙"
)
ADMIN_HELP = (
    "\n\n🛡 <b>Admin commands</b> (reply to someone's message in the group):\n"
    "/warn [reason]: give a strike (1 warning → 1h mute → 1 day → 1 week → ban)\n"
    "/unwarn: take one strike away\n"
    "/strikes: check someone's strikes\n"
    "/ban [reason]: ban right away\n"
    "/unmute, /unban: lift a punishment (a user ID also works instead of replying)\n\n"
    "Anywhere:\n"
    "/remove &lt;item number&gt;: take any item out of the loop\n"
    "/remindnow: send the daily reminder right now"
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    text = WELCOME.format(name=esc(user.first_name))
    if await moderation.is_admin(context.bot, user.id):
        text += ADMIN_HELP
    await update.effective_message.reply_text(text, parse_mode=HTML)


async def cmd_point_to_dm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Someone used a DM-only command in the group."""
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(
        "💬 Open a chat with me", url=f"https://t.me/{context.bot.username}?start=report")]])
    reply = await update.effective_message.reply_text(
        "Let's do that in private so the group stays tidy 🙂 Tap below!", reply_markup=keyboard)
    context.job_queue.run_once(delete_later, 60, data=[reply.message_id, update.effective_message.message_id])


async def delete_later(context: ContextTypes.DEFAULT_TYPE) -> None:
    for message_id in context.job.data:
        try:
            await context.bot.delete_message(config.GROUP_ID, message_id)
        except TelegramError:
            pass


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    items = db.active_items()
    if not items:
        await update.effective_message.reply_text("🎉 Nothing is waiting for an owner right now. Great news!")
        return
    await update.effective_message.reply_text(
        f"Here {'is' if len(items) == 1 else 'are'} the <b>{len(items)}</b> item{'s' if len(items) != 1 else ''} "
        "still waiting for their owners 👇", parse_mode=HTML)
    for item in items:
        await send_item(context.bot, update.effective_chat.id, item, "reminder")
        await asyncio.sleep(0.5)


async def cmd_mine(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    items = db.active_items_for_user(update.effective_user.id)
    if not items:
        await update.effective_message.reply_text("You don't have any active posts right now. Use /report to add one 📮")
        return
    await update.effective_message.reply_text(
        "Here are your active posts. Tap <b>✅ Owner found</b> when someone picks theirs up, "
        "or <b>🗑 Remove</b> if it was posted by mistake. Either way it leaves the daily reminders straight away.",
        parse_mode=HTML)
    for item in items:
        await send_item(context.bot, update.effective_chat.id, item, "preview", reply_markup=owner_keyboard(item["id"]))
        await asyncio.sleep(0.3)


async def on_close_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    action, item_id = query.data.split(":")
    item = db.get_item(int(item_id))
    in_private = query.message is not None and query.message.chat.type == ChatType.PRIVATE

    if not item or item["status"] != "active":
        await query.answer("This item is already closed 👍")
        if in_private:
            await query.edit_message_reply_markup(None)
        return

    user = query.from_user
    if user.id != item["user_id"] and not await moderation.is_admin(context.bot, user.id):
        text = f"Only the person who posted this (or an admin) can close it. If it's yours, pick it up at {item['claim_location']} 🙌"
        await query.answer(text[:200], show_alert=True)
        return

    status = "returned" if action == "ret" else "removed"
    await close_item(context.bot, item, status)
    await query.answer("Done! It's out of the daily reminders ✅")
    if in_private:
        await query.edit_message_reply_markup(None)
        if status == "returned":
            await query.message.reply_text(f"Amazing, thank you for getting #{item['id']} back to its owner! 🎉")
        else:
            await query.message.reply_text(f"Post #{item['id']} has been removed 🗑")


# ---------- reporting a found item (private chat conversation) ----------

PHOTO, DESCRIPTION, FOUND_DATE, FOUND_LOCATION, CLAIM_LOCATION, CONFIRM = range(6)


async def report_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    chat = update.effective_chat

    restriction = moderation.is_restricted(user.id)
    if restriction:
        await chat.send_message(
            f"Sorry, you can't post items right now because you're {restriction} in the group. "
            "If you think that's a mistake, please talk to one of the admins 🙏")
        return ConversationHandler.END
    if not await moderation.is_group_member(context.bot, user.id):
        await chat.send_message("To post items you need to be a member of the school Lost &amp; Found group first 🙂", parse_mode=HTML)
        return ConversationHandler.END
    if len(db.active_items_for_user(user.id)) >= config.MAX_ACTIVE_PER_USER:
        await chat.send_message(
            f"You already have {config.MAX_ACTIVE_PER_USER} active posts. Please close some with /mine first 🙏")
        return ConversationHandler.END

    context.user_data["draft"] = {}
    await chat.send_message(
        "Thank you for helping someone get their things back! 🙌\n\n"
        "<b>Step 1/5:</b> Send me a <b>photo</b> of the item 📸\n"
        "Can't take one? Just tap <b>Skip</b>.\n\n"
        "<i>You can type /cancel at any time.</i>",
        parse_mode=HTML,
        reply_markup=ReplyKeyboardMarkup([["Skip"]], resize_keyboard=True, one_time_keyboard=True),
    )
    return PHOTO


async def got_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.effective_message
    context.user_data["draft"]["photo_file_id"] = message.photo[-1].file_id if message.photo else None
    await message.reply_text(
        "<b>Step 2/5:</b> What is it? Describe it in a few words 📦\n"
        "e.g. <i>Black Casio calculator</i> or <i>Navy hoodie, size M</i>\n\n"
        "💡 Tip: leave out one detail (like a name written inside) so the real owner can prove it's theirs.",
        parse_mode=HTML, reply_markup=ReplyKeyboardRemove(),
    )
    return DESCRIPTION


async def photo_expected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.effective_message.reply_text("Please send a photo, or tap Skip 📸")
    return PHOTO


async def got_description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.effective_message.text.strip()
    if not 3 <= len(text) <= 200:
        await update.effective_message.reply_text("Please keep the description between 3 and 200 characters 🙂")
        return DESCRIPTION
    context.user_data["draft"]["description"] = text
    await update.effective_message.reply_text(
        "<b>Step 3/5:</b> When did you find it? 📅\n"
        "Tap a button or type a date like <i>06.10</i> or <i>06.10.2026</i>.",
        parse_mode=HTML,
        reply_markup=ReplyKeyboardMarkup([["Today", "Yesterday"]], resize_keyboard=True, one_time_keyboard=True),
    )
    return FOUND_DATE


def parse_date(text: str):
    today = datetime.now(config.TIMEZONE).date()
    text = text.strip().lower()
    if text == "today":
        return today
    if text == "yesterday":
        return today - timedelta(days=1)
    year_given = True
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if m:
        y, mo, d = map(int, m.groups())
    else:
        m = re.fullmatch(r"(\d{1,2})[./-](\d{1,2})(?:[./-](\d{2,4}))?", text)
        if not m:
            return None
        d, mo = int(m.group(1)), int(m.group(2))
        year_given = m.group(3) is not None
        y = int(m.group(3)) if year_given else today.year
        if y < 100:
            y += 2000
    try:
        found = date(y, mo, d)
    except ValueError:
        return None
    if not year_given and found > today:
        # e.g. typing 28.12 in early January means last December.
        try:
            found = found.replace(year=found.year - 1)
        except ValueError:
            return None
    return found


async def got_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    found = parse_date(update.effective_message.text)
    today = datetime.now(config.TIMEZONE).date()
    if found is None:
        await update.effective_message.reply_text("Hmm, I couldn't read that date. Try something like 06.10 or tap Today 🙂")
        return FOUND_DATE
    if found > today or found < today - timedelta(days=365):
        await update.effective_message.reply_text("That date doesn't look right. It should be today or in the past year 🙂")
        return FOUND_DATE
    context.user_data["draft"]["found_date"] = found.isoformat()
    await update.effective_message.reply_text(
        "<b>Step 4/5:</b> Where did you find it? 📍\n"
        "e.g. <i>Gym changing room</i>, <i>Room 204</i>, <i>Canteen</i>",
        parse_mode=HTML, reply_markup=ReplyKeyboardRemove(),
    )
    return FOUND_LOCATION


async def got_found_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.effective_message.text.strip()
    if not 2 <= len(text) <= 100:
        await update.effective_message.reply_text("Please keep the location between 2 and 100 characters 🙂")
        return FOUND_LOCATION
    context.user_data["draft"]["found_location"] = text
    buttons = [config.CLAIM_LOCATIONS[i:i + 2] for i in range(0, len(config.CLAIM_LOCATIONS), 2)]
    await update.effective_message.reply_text(
        "<b>Step 5/5:</b> Where can the owner pick it up? 🏫\n"
        "Tap one of the options or type your own.\n\n"
        "🙏 Please make sure the item is actually there (or with you, if you write your class).",
        parse_mode=HTML,
        reply_markup=ReplyKeyboardMarkup(buttons, resize_keyboard=True, one_time_keyboard=True) if buttons else ReplyKeyboardRemove(),
    )
    return CLAIM_LOCATION


async def got_claim_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.effective_message.text.strip()
    if not 2 <= len(text) <= 100:
        await update.effective_message.reply_text("Please keep the claim location between 2 and 100 characters 🙂")
        return CLAIM_LOCATION
    draft = context.user_data["draft"]
    draft["claim_location"] = text
    draft["id"] = None
    draft["created_at"] = datetime.now(timezone.utc).isoformat()

    await update.effective_message.reply_text("Here's how your post will look 👇", reply_markup=ReplyKeyboardRemove())
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Post it", callback_data="draft:post")],
        [InlineKeyboardButton("✏️ Start over", callback_data="draft:restart"),
         InlineKeyboardButton("❌ Cancel", callback_data="draft:cancel")],
    ])
    await send_item(context.bot, update.effective_chat.id, draft, "preview", reply_markup=keyboard)
    return CONFIRM


async def on_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_reply_markup(None)
    choice = query.data.split(":")[1]

    if choice == "restart":
        return await report_start(update, context)
    if choice == "cancel":
        context.user_data.pop("draft", None)
        await query.message.reply_text("No problem, nothing was posted 👍")
        return ConversationHandler.END

    user = query.from_user
    if moderation.is_restricted(user.id):
        await query.message.reply_text("Sorry, you can't post items right now 🙏")
        return ConversationHandler.END

    draft = context.user_data.pop("draft")
    item_id = db.add_item(user.id, user.full_name, draft["description"], draft["photo_file_id"],
                          draft["found_date"], draft["found_location"], draft["claim_location"])
    item = db.get_item(item_id)
    try:
        posted = await send_item(context.bot, config.GROUP_ID, item, "new",
                                 reply_markup=group_keyboard(item_id, context.bot.username))
        db.set_group_message(item_id, posted.message_id)
    except TelegramError:
        log.exception("Could not post item %s to the group", item_id)

    await query.message.reply_text(
        f"🎉 Posted as <b>#{item_id}</b>! Thank you, you're a star ⭐\n\n"
        "I'll remind the group about it every school morning. "
        "When the owner picks it up, open /mine and tap <b>✅ Owner found</b> so it leaves the reminders.",
        parse_mode=HTML,
    )
    log.info("Item #%s posted by %s (%s)", item_id, user.full_name, user.id)
    return ConversationHandler.END


async def report_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("draft", None)
    await update.effective_message.reply_text("Cancelled. Nothing was posted 👍", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


async def text_expected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text("I didn't quite get that. Please follow the last step above, or type /cancel to stop 🙂")


# ---------- daily reminder ----------

async def expire_old_items(bot) -> None:
    if config.ITEM_EXPIRY_DAYS <= 0:
        return
    cutoff = (datetime.now(timezone.utc) - timedelta(days=config.ITEM_EXPIRY_DAYS)).isoformat()
    for item in db.items_older_than(cutoff):
        await close_item(bot, item, "expired")
        try:
            await bot.send_message(
                item["user_id"],
                f"Hi! Your post <b>#{item['id']}</b> ({esc(item['description'])}) has been in the daily reminders "
                f"for {config.ITEM_EXPIRY_DAYS} days, so I've retired it. If it's still unclaimed, feel free to "
                "/report it again. Thanks for helping out 💙",
                parse_mode=HTML,
            )
        except TelegramError:
            pass


async def daily_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    bot = context.bot
    await expire_old_items(bot)

    # Clear yesterday's reminder posts so the group doesn't fill up with copies.
    for message_id in db.pop_reminder_messages():
        try:
            await bot.delete_message(config.GROUP_ID, message_id)
        except TelegramError:
            pass

    items = db.active_items()
    if not items:
        return
    count = len(items)
    header = await bot.send_message(
        config.GROUP_ID,
        f"{random.choice(HEADER_LINES)}\n\n"
        f"📢 <b>{count}</b> item{'s are' if count != 1 else ' is'} still waiting for {'their owners' if count != 1 else 'its owner'}. "
        "Take a quick look below, you might spot something of yours! 👇\n\n"
        f"<i>Found something? Message me privately with /report 📮</i>",
        parse_mode=HTML,
    )
    db.add_reminder_message(header.message_id, None)

    for item in items:
        await asyncio.sleep(config.REPOST_DELAY)
        item = db.get_item(item["id"])  # it may have been closed while we were posting
        if item["status"] != "active":
            continue
        try:
            sent = await send_item(bot, config.GROUP_ID, item, "reminder",
                                   reply_markup=group_keyboard(item["id"], bot.username), disable_notification=True)
            db.add_reminder_message(sent.message_id, item["id"])
        except TelegramError:
            log.exception("Could not repost item %s", item["id"])
    log.info("Daily reminder sent with %s items", count)


# ---------- admin commands ----------

async def require_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    message = update.effective_message
    if moderation.sent_by_anonymous_admin(message) or await moderation.is_admin(context.bot, update.effective_user.id):
        return True
    await message.reply_text("Sorry, only group admins can use that command 🙂")
    return False


def replied_user(message):
    reply = message.reply_to_message
    if not reply or not reply.from_user or reply.from_user.is_bot or reply.sender_chat:
        return None
    return reply.from_user


def target_id(message, args) -> int | None:
    user = replied_user(message)
    if user:
        return user.id
    if args and args[0].lstrip("-").isdigit():
        return int(args[0])
    return None


async def cleanup(context, *messages) -> None:
    for message in messages:
        if message:
            try:
                await message.delete()
            except TelegramError:
                pass


async def cmd_warn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return
    message = update.effective_message
    target = replied_user(message)
    if not target:
        await message.reply_text("Reply to the person's message with /warn [reason], e.g. <code>/warn trolling</code>", parse_mode=HTML)
        return
    if await moderation.is_admin(context.bot, target.id):
        await message.reply_text("Admins can't be warned 🙂")
        return
    reason = " ".join(context.args) or "breaking the group rules"
    text = await moderation.add_strike(context.bot, target, reason, update.effective_user.id)
    await cleanup(context, message.reply_to_message, message)
    await context.bot.send_message(config.GROUP_ID, text, parse_mode=HTML)


async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return
    message = update.effective_message
    target = replied_user(message)
    if not target:
        await message.reply_text("Reply to the person's message with /ban [reason]")
        return
    if await moderation.is_admin(context.bot, target.id):
        await message.reply_text("Admins can't be banned by the bot 🙂")
        return
    reason = " ".join(context.args) or "breaking the group rules"
    await moderation.ban(context.bot, target, reason, update.effective_user.id)
    await cleanup(context, message.reply_to_message, message)
    await context.bot.send_message(
        config.GROUP_ID,
        f"⛔ {moderation.mention(target)} has been removed from the group.\nReason: <i>{esc(reason)}</i>",
        parse_mode=HTML,
    )


async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return
    user_id = target_id(update.effective_message, context.args)
    if user_id is None:
        await update.effective_message.reply_text("Reply to one of their old messages or use /unban <user id>")
        return
    await moderation.unban(context.bot, user_id, update.effective_user.id)
    await update.effective_message.reply_text(
        f"✅ User {user_id} is unbanned and can rejoin. They're on strike {moderation.MAX_STRIKES - 1}, so one more means another ban.")


async def cmd_unmute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return
    user_id = target_id(update.effective_message, context.args)
    if user_id is None:
        await update.effective_message.reply_text("Reply to their message or use /unmute <user id>")
        return
    await moderation.unmute(context.bot, user_id, update.effective_user.id)
    await update.effective_message.reply_text(f"🔊 User {user_id} can talk again.")


async def cmd_unwarn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return
    user_id = target_id(update.effective_message, context.args)
    if user_id is None:
        await update.effective_message.reply_text("Reply to their message or use /unwarn <user id>")
        return
    count = max(0, db.get_strikes(user_id)["count"] - 1)
    db.set_strikes(user_id, count)
    db.log_action(user_id, update.effective_user.id, "unwarn", None)
    await update.effective_message.reply_text(f"👍 One strike removed. They now have {count}/{moderation.MAX_STRIKES}.")


async def cmd_strikes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user_id = target_id(message, context.args)
    if user_id is not None and user_id != update.effective_user.id:
        if not await require_admin(update, context):
            return
    else:
        user_id = update.effective_user.id
    row = db.get_strikes(user_id)
    status = "banned" if row["banned"] else (moderation.is_restricted(user_id) or "in good standing")
    await message.reply_text(f"Strikes: {row['count']}/{moderation.MAX_STRIKES} ({status})")


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return
    if not context.args or not context.args[0].lstrip("#").isdigit():
        await update.effective_message.reply_text("Usage: /remove <item number>, e.g. /remove 12")
        return
    item = db.get_item(int(context.args[0].lstrip("#")))
    if not item or item["status"] != "active":
        await update.effective_message.reply_text("That item doesn't exist or is already closed.")
        return
    await close_item(context.bot, item, "removed")
    await update.effective_message.reply_text(f"🗑 Item #{item['id']} removed from the loop.")


async def cmd_remindnow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return
    await update.effective_message.reply_text("Sending the reminder now 📢")
    await daily_reminder(context)


# ---------- group activity ----------

async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = message.from_user
    if not user or user.is_bot or user.id == 777000 or moderation.sent_by_anonymous_admin(message):
        return
    if await moderation.is_admin(context.bot, user.id):
        return
    reason = moderation.detect_spam(message)
    if not reason:
        return
    await cleanup(context, message)
    text = await moderation.add_strike(context.bot, user, reason)
    await context.bot.send_message(config.GROUP_ID, text, parse_mode=HTML)


async def on_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    names = ", ".join(esc(u.first_name) for u in update.effective_message.new_chat_members if not u.is_bot)
    if not names:
        return
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(
        "📮 Report a found item", url=f"https://t.me/{context.bot.username}?start=report")]])
    welcome = await update.effective_message.reply_text(
        f"Welcome, {names}! 👋\n\nLost something? Every school morning I post everything that's still unclaimed. "
        "Found something? Message me privately and I'll share it here. Please keep this chat friendly and on-topic 💙",
        reply_markup=keyboard,
    )
    context.job_queue.run_once(delete_later, 300, data=[welcome.message_id])


async def on_migrate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The group was upgraded to a supergroup: save its new ID and restart."""
    new_id = update.effective_message.migrate_to_chat_id
    if not new_id:
        return
    with open(".env") as f:
        lines = [f"GROUP_ID={new_id}\n" if line.startswith("GROUP_ID=") else line for line in f]
    with open(".env", "w") as f:
        f.writelines(lines)
    log.info("Group upgraded to supergroup %s, restarting", new_id)
    os.execv(sys.executable, [sys.executable] + sys.argv)


async def log_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    msg = update.effective_message
    log.info("update from chat %s (%s): %r", chat.id if chat else None,
             chat.type if chat else None, (msg.text or msg.caption or "")[:50] if msg else type(update).__name__)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Error while handling an update", exc_info=context.error)


# ---------- startup ----------

async def post_init(app: Application) -> None:
    await app.bot.set_my_commands(
        [
            BotCommand("report", "Report an item you found"),
            BotCommand("list", "See all unclaimed items"),
            BotCommand("mine", "Manage the items you posted"),
            BotCommand("help", "How this bot works"),
            BotCommand("cancel", "Stop what you're doing"),
        ],
        scope=BotCommandScopeAllPrivateChats(),
    )
    log.info("Bot @%s is running. Daily reminder at %s on days %s",
             app.bot.username, config.REMINDER_TIME.strftime("%H:%M"), config.REMINDER_DAYS)


def main() -> None:
    app = Application.builder().token(config.BOT_TOKEN).post_init(post_init).build()
    app.add_handler(TypeHandler(Update, log_update), group=-100)

    text_answer = filters.TEXT & ~filters.COMMAND
    report_flow = ConversationHandler(
        entry_points=[
            CommandHandler("report", report_start, filters=PRIVATE),
            CommandHandler("start", report_start, filters=PRIVATE & filters.Regex(r"^/start report$")),
        ],
        states={
            PHOTO: [
                MessageHandler(filters.PHOTO, got_photo),
                MessageHandler(filters.Regex(r"(?i)^skip$"), got_photo),
                MessageHandler(~filters.COMMAND, photo_expected),
            ],
            DESCRIPTION: [MessageHandler(text_answer, got_description)],
            FOUND_DATE: [MessageHandler(text_answer, got_date)],
            FOUND_LOCATION: [MessageHandler(text_answer, got_found_location)],
            CLAIM_LOCATION: [MessageHandler(text_answer, got_claim_location)],
            CONFIRM: [CallbackQueryHandler(on_confirm, pattern=r"^draft:")],
        },
        fallbacks=[
            CommandHandler("cancel", report_cancel),
            CommandHandler("report", report_start, filters=PRIVATE),
            MessageHandler(~filters.COMMAND, text_expected),
        ],
        allow_reentry=True,
    )
    app.add_handler(MessageHandler(filters.StatusUpdate.MIGRATE & filters.Chat(config.GROUP_ID), on_migrate), group=-1)
    app.add_handler(report_flow)

    group_only = filters.Chat(config.GROUP_ID)
    app.add_handler(CommandHandler(["report", "start", "mine", "list", "help"], cmd_point_to_dm, filters=group_only))
    app.add_handler(CommandHandler(["start", "help"], cmd_start, filters=PRIVATE))
    app.add_handler(CommandHandler("list", cmd_list, filters=PRIVATE))
    app.add_handler(CommandHandler("mine", cmd_mine, filters=PRIVATE))
    app.add_handler(CallbackQueryHandler(on_close_button, pattern=r"^(ret|del):\d+$"))

    app.add_handler(CommandHandler("warn", cmd_warn, filters=group_only))
    app.add_handler(CommandHandler("ban", cmd_ban, filters=group_only))
    app.add_handler(CommandHandler("unban", cmd_unban, filters=ALLOWED_CHATS))
    app.add_handler(CommandHandler("unmute", cmd_unmute, filters=ALLOWED_CHATS))
    app.add_handler(CommandHandler("unwarn", cmd_unwarn, filters=ALLOWED_CHATS))
    app.add_handler(CommandHandler("strikes", cmd_strikes, filters=ALLOWED_CHATS))
    app.add_handler(CommandHandler("remove", cmd_remove, filters=ALLOWED_CHATS))
    app.add_handler(CommandHandler("remindnow", cmd_remindnow, filters=ALLOWED_CHATS))

    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS & group_only, on_new_members))
    # Spam checks run in their own handler group so they see every group message.
    app.add_handler(MessageHandler(group_only & ~filters.StatusUpdate.ALL, on_group_message), group=1)
    app.add_error_handler(on_error)

    app.job_queue.run_daily(daily_reminder, time=config.REMINDER_TIME, days=config.REMINDER_DAYS, name="daily_reminder")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
