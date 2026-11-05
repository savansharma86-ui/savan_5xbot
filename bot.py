"""
JoinGate Bot v4 - 2 channel join-request approval bot
aiogram 3.13+ / aiosqlite / long polling

Note: the Bot API cannot approve requests that arrived before the bot was running.
"""
import asyncio
import json
import logging
import os
import re
import time
from contextlib import suppress
from html import escape

import aiosqlite
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    ChatJoinRequest,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    LinkPreviewOptions,
    Message,
    ReplyKeyboardMarkup,
)
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
DB_PATH = os.getenv("DB_PATH", "joingate.db")
DAY = 86400

router = Router()
db: aiosqlite.Connection = None  # type: ignore
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


# ----------------------------------------------------------------------------
# Small-caps style: "Savan" -> "Sᴀᴠᴀɴ" for EVERY outgoing message, caption and button.
# Set STYLE=0 in .env to turn it off.
# ----------------------------------------------------------------------------
STYLE_ON = os.getenv("STYLE", "1") != "0"
_SC = dict(zip("abcdefghijklmnopqrstuvwxyz", "ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀꜱᴛᴜᴠᴡxʏᴢ"))
_SKIP = re.compile(
    r"(<code>.*?</code>|<[^>]+>|&[a-zA-Z#0-9]+;|https?://\S+|\bt\.me/\S+|@\w+|(?<![\w/])/[A-Za-z_]+|\{[^}]*\})",
    re.S,
)
_WORD = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")


def _sc_word(m):
    w = m.group(0)
    return w[0].upper() + "".join(_SC.get(ch, ch) for ch in w[1:].lower())


def style(text):
    if not STYLE_ON or not text:
        return text
    out = []
    for i, part in enumerate(_SKIP.split(text)):
        out.append(part if i % 2 else _WORD.sub(_sc_word, part))
    return "".join(out)


class StyleMiddleware(BaseRequestMiddleware):
    """Applies the small-caps style to the text, caption and every button of every outgoing request."""

    async def __call__(self, make_request, bot, method):
        for attr in ("text", "caption"):
            v = getattr(method, attr, None)
            if isinstance(v, str):
                setattr(method, attr, style(v))
        rm = getattr(method, "reply_markup", None)
        if isinstance(rm, InlineKeyboardMarkup):
            rows = [[b.model_copy(update={"text": style(b.text)}) for b in row] for row in rm.inline_keyboard]
            method.reply_markup = InlineKeyboardMarkup(inline_keyboard=rows)
        return await make_request(bot, method)


# ----------------------------------------------------------------------------
# Text helpers: every point lives in its own box, English + Hinglish
# ----------------------------------------------------------------------------
def box(title, en, hi=None):
    s = f"<blockquote><b>{title}</b>\n{en}"
    if hi:
        s += f"\n<i>{hi}</i>"
    return s + "</blockquote>"


def bi(en, hi):
    return f"{en}\n<i>{hi}</i>"


def sections(*parts):
    return "\n\n".join(parts)


DEFAULT_START = sections(
    "👋 <b>Welcome to JoinGate</b>",
    box(
        "What it does",
        "Messages everyone who requests to join your channel, and lets you approve requests in one tap.",
        "Jo bhi aapke channel me join request dalta hai, bot usse turant message karta hai aur aap ek tap me requests approve kar sakte ho.",
    ),
    box(
        "Quick start",
        "Use the menu below, or tap ⚙️ Settings to connect your channel.",
        "Neeche wala menu use karo, ya channel connect karne ke liye ⚙️ Settings dabao.",
    ),
    box("Price", "100% free for everyone.", "Sabke liye bilkul free."),
)

DEFAULT_HELP = sections(
    "📖 <b>JoinGate Guide</b>",
    box(
        "1️⃣ Add the bot as admin",
        "Add this bot as an admin in your channel with the Invite Users permission.",
        "Bot ko apne channel me admin banao (Invite Users permission ke saath).",
    ),
    box(
        "2️⃣ Connect Channel A",
        "Open ⚙️ Settings → Set/Change A. Send the @username, the channel ID, or forward any post from the channel. Channel A is where people send join requests.",
        "Settings → Set/Change A kholo. @username, channel ID bhejo ya channel ka koi post forward karo. Channel A wo hai jisme log join request dalte hain.",
    ),
    box(
        "3️⃣ Connect Channel B (optional)",
        "Channel B is your gate. Users get its link and must request it too. If you skip B, the bot owner's default link is sent and nothing is auto-approved.",
        "Channel B aapka gate hai. Users ko uska link milta hai aur usme bhi request dalni hoti hai. B set nahi kiya to bot owner ka default link jayega aur kuch auto-approve nahi hoga.",
    ),
    box(
        "4️⃣ Choose the approve mode",
        "When both channels are set, the bot asks you: ✅ Auto-approve (B request → approved in A) or 🕓 Manual (users only send requests, you approve later).",
        "Dono channel set hone par bot poochega: Auto-approve (B me request → A me approve) ya Manual (users sirf request bhejenge, aap baad me approve karoge).",
    ),
    box(
        "5️⃣ Approve requests",
        "Post <code>/accept_all</code> inside your channel (as admin) to approve every pending request of that channel. It also works in this bot.",
        "Apne channel me admin ke roop me <code>/accept_all</code> likho, us channel ki saari pending requests approve ho jayengi. Ye command bot me bhi chalta hai.",
    ),
    box(
        "6️⃣ Check status, get support",
        "Tap 📊 My Status anytime to see your channels and numbers. Tap 💬 Support to message the bot owner directly.",
        "Apna channel aur numbers dekhne ke liye kabhi bhi 📊 My Status dabao. Bot owner se seedha baat karne ke liye 💬 Support dabao.",
    ),
)


def default_welcome(auto: bool, has_link: bool) -> str:
    if has_link:
        en = "✅ Your join request has been received.\nFor fast approval, send a join request to the channel below"
        hi = "Aapki join request mil gayi hai.\nFast approval ke liye neeche wale channel me request bhejo"
        if auto:
            en += " and you will be approved right away."
            hi += " aur aap turant approve ho jaoge."
        else:
            en += ". You will be approved shortly."
            hi += ". Jaldi hi aapko approve kar diya jayega."
    else:
        en = "✅ Your join request has been received.\nYou will be approved shortly."
        hi = "Aapki join request mil gayi hai.\nJaldi hi aapko approve kar diya jayega."
    return f"👋 <b>Hi {{name}}!</b>\n<blockquote>{en}\n<i>{hi}</i></blockquote>"


MODE_TEXT = sections(
    "🔀 <b>Approve Mode</b>",
    box(
        "How it works",
        "A user requests Channel A and gets your Channel B link. What should happen after they request Channel B?",
        "User Channel A me request karta hai aur use aapke Channel B ka link milta hai. B me request ke baad kya hona chahiye?",
    ),
    box("✅ Auto-approve", "Their Channel A request is approved automatically.", "Unki A ki request automatically approve ho jayegi."),
    box(
        "🕓 Manual",
        "Users only send requests. You approve later by posting <code>/accept_all</code> in your channel.",
        "Users sirf request bhejenge. Aap baad me channel me <code>/accept_all</code> likhkar approve karoge.",
    ),
)

MENU_STATUS, MENU_SUPPORT, MENU_SETTINGS, MENU_HELP = "📊 My Status", "💬 Support", "⚙️ Settings", "📖 Help"


def main_menu_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=MENU_STATUS), KeyboardButton(text=MENU_SUPPORT)],
            [KeyboardButton(text=MENU_SETTINGS), KeyboardButton(text=MENU_HELP)],
        ],
        resize_keyboard=True,
    )


# ----------------------------------------------------------------------------
# DB helpers
# ----------------------------------------------------------------------------
async def init_db():
    global db
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY,
            name TEXT, username TEXT,
            banned INTEGER DEFAULT 0,
            created_at INTEGER,
            last_seen INTEGER
        );
        CREATE TABLE IF NOT EXISTS owners(
            user_id INTEGER PRIMARY KEY,
            a_id INTEGER, a_title TEXT, a_link TEXT,
            b_id INTEGER, b_title TEXT, b_link TEXT,
            mode TEXT DEFAULT 'later',
            welcome TEXT,
            mode_confirmed INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS requests(
            chat_id INTEGER, user_id INTEGER, owner_id INTEGER,
            status TEXT DEFAULT 'pending',
            created_at INTEGER,
            PRIMARY KEY(chat_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS support_msgs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, name TEXT, username TEXT,
            content TEXT, created_at INTEGER,
            status TEXT DEFAULT 'open'
        );
        """
    )
    await db.commit()
    for stmt in (
        "ALTER TABLE owners ADD COLUMN mode_confirmed INTEGER DEFAULT 0",
        "ALTER TABLE users ADD COLUMN last_seen INTEGER",
    ):
        with suppress(Exception):
            await db.execute(stmt)
            await db.commit()


async def one(sql, args=()):
    async with db.execute(sql, args) as c:
        return await c.fetchone()


async def many(sql, args=()):
    async with db.execute(sql, args) as c:
        return await c.fetchall()


async def run(sql, args=()):
    await db.execute(sql, args)
    await db.commit()


async def get_setting(key, default):
    r = await one("SELECT value FROM settings WHERE key=?", (key,))
    return r["value"] if r else default


async def upsert_user(u):
    now = int(time.time())
    await run(
        "INSERT INTO users(user_id,name,username,created_at,last_seen) VALUES(?,?,?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET name=excluded.name, username=excluded.username, last_seen=excluded.last_seen",
        (u.id, u.full_name, u.username, now, now),
    )


async def touch_user(user_id):
    with suppress(Exception):
        await run("UPDATE users SET last_seen=? WHERE user_id=?", (int(time.time()), user_id))


async def get_owner(uid):
    r = await one("SELECT * FROM owners WHERE user_id=?", (uid,))
    if not r:
        await run("INSERT INTO owners(user_id) VALUES(?)", (uid,))
        r = await one("SELECT * FROM owners WHERE user_id=?", (uid,))
    return r


# ----------------------------------------------------------------------------
# Rich content (text / photo / video / GIF / document) stored as JSON
# ----------------------------------------------------------------------------
def extract_content(m: Message):
    c = m.html_text or ""
    if m.photo:
        return {"t": "photo", "f": m.photo[-1].file_id, "c": c}
    if m.video:
        return {"t": "video", "f": m.video.file_id, "c": c}
    if m.animation:
        return {"t": "animation", "f": m.animation.file_id, "c": c}
    if m.document:
        return {"t": "document", "f": m.document.file_id, "c": c}
    if m.text:
        return {"t": "text", "c": c}
    return None


def parse_content(raw, legacy_escape=False):
    if not raw:
        return None
    try:
        d = json.loads(raw)
        if isinstance(d, dict) and "t" in d:
            return d
    except ValueError:
        pass
    return {"t": "text", "c": escape(raw) if legacy_escape else raw}


async def get_content(key, default_text):
    d = parse_content(await get_setting(key, None))
    return d or {"t": "text", "c": default_text}


def content_preview(raw):
    d = parse_content(raw, legacy_escape=True)
    if not d:
        return "Default message"
    txt = re.sub(r"<[^>]+>", "", d.get("c", "")).strip()
    tag = "" if d["t"] == "text" else f"[{d['t']}] "
    return escape(tag + txt[:160])


async def send_content(bot: Bot, chat_id, data, markup=None, name=None, preview_url=None):
    t = data.get("t", "text")
    c = data.get("c") or ""
    if name is not None:
        c = c.replace("{name}", escape(name))

    async def _send(parse):
        if t == "text":
            lp = (
                LinkPreviewOptions(url=preview_url, prefer_large_media=True, show_above_text=False)
                if preview_url
                else NO_PREVIEW
            )
            return await bot.send_message(chat_id, c or "👋", reply_markup=markup, parse_mode=parse, link_preview_options=lp)
        sender = {
            "photo": bot.send_photo,
            "video": bot.send_video,
            "animation": bot.send_animation,
            "document": bot.send_document,
        }[t]
        cap = c if 0 < len(c) <= 1024 else None
        msg = await sender(chat_id, data["f"], caption=cap, reply_markup=markup if (cap or not c) else None, parse_mode=parse)
        if c and cap is None:
            await bot.send_message(chat_id, c, reply_markup=markup, parse_mode=parse, link_preview_options=NO_PREVIEW)
        return msg

    for _ in range(2):
        try:
            try:
                return await _send(ParseMode.HTML)
            except TelegramBadRequest as e:
                s = str(e).lower()
                if "parse" in s or "entit" in s:
                    return await _send(None)
                raise
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
    return None


# ----------------------------------------------------------------------------
# UI helpers
# ----------------------------------------------------------------------------
def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows])


def home_kb(uid):
    rows = [[("⚙️ Settings", "s:main"), ("📖 Help", "help")]]
    if uid == OWNER_ID:
        rows.append([("👑 Admin Panel", "adm:main")])
    return kb(rows)


async def show(msg: Message, text: str, markup=None):
    try:
        await msg.edit_text(text, reply_markup=markup, link_preview_options=NO_PREVIEW)
    except TelegramBadRequest as e:
        s = str(e).lower()
        if "not modified" in s:
            return
        try:
            if "parse" in s or "entit" in s:
                await msg.answer(text, reply_markup=markup, parse_mode=None, link_preview_options=NO_PREVIEW)
            else:
                await msg.answer(text, reply_markup=markup, link_preview_options=NO_PREVIEW)
        except TelegramBadRequest:
            await msg.answer(text, reply_markup=markup, parse_mode=None, link_preview_options=NO_PREVIEW)


async def reply(msg: Message, text: str, markup=None):
    try:
        await msg.answer(text, reply_markup=markup, link_preview_options=NO_PREVIEW)
    except TelegramBadRequest:
        await msg.answer(text, reply_markup=markup, parse_mode=None, link_preview_options=NO_PREVIEW)


async def show_content(c: CallbackQuery, bot: Bot, key, default, markup):
    data = await get_content(key, default)
    msg = c.message
    if data.get("t", "text") == "text" and msg.text:
        await show(msg, data["c"], markup)
    else:
        with suppress(TelegramAPIError):
            await msg.delete()
        await send_content(bot, msg.chat.id, data, markup)


# ----------------------------------------------------------------------------
# Middlewares
# ----------------------------------------------------------------------------
class BanMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user and user.id != OWNER_ID:
            r = await one("SELECT banned FROM users WHERE user_id=?", (user.id,))
            if r and r["banned"]:
                return
        return await handler(event, data)


class SeenMiddleware(BaseMiddleware):
    """Keeps last_seen fresh for 'monthly active users' stats."""

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user:
            await touch_user(user.id)
        return await handler(event, data)


# ----------------------------------------------------------------------------
# FSM
# ----------------------------------------------------------------------------
class SetChannel(StatesGroup):
    waiting = State()


class EditWelcome(StatesGroup):
    waiting = State()


class AdminEdit(StatesGroup):
    waiting = State()


class Broadcast(StatesGroup):
    waiting = State()
    confirm = State()


class ChannelBroadcast(StatesGroup):
    waiting = State()
    confirm = State()


class SupportMsg(StatesGroup):
    waiting = State()


class SupportReply(StatesGroup):
    waiting = State()


# ----------------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------------
@router.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext, bot: Bot):
    await state.clear()
    await upsert_user(m.from_user)
    data = await get_content("start_text", DEFAULT_START)
    await send_content(bot, m.chat.id, data, home_kb(m.from_user.id))
    await m.answer("👇", reply_markup=main_menu_kb())


@router.message(Command("help"))
async def cmd_help(m: Message, state: FSMContext, bot: Bot):
    await state.clear()
    await upsert_user(m.from_user)
    data = await get_content("help_text", DEFAULT_HELP)
    await send_content(bot, m.chat.id, data, kb([[("🏠 Home", "home")]]))


@router.message(F.text == MENU_HELP)
async def menu_help(m: Message, state: FSMContext, bot: Bot):
    await cmd_help(m, state, bot)


@router.message(Command("cancel"))
async def cmd_cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(bi("❌ Cancelled.", "Cancel kar diya gaya."))


@router.message(Command("settings"))
async def cmd_settings(m: Message, state: FSMContext):
    await state.clear()
    await upsert_user(m.from_user)
    text, markup = await settings_view(m.from_user.id)
    await reply(m, text, markup)


@router.message(F.text == MENU_SETTINGS)
async def menu_settings(m: Message, state: FSMContext):
    await cmd_settings(m, state)


@router.message(F.text == MENU_STATUS)
async def menu_status(m: Message, state: FSMContext):
    await state.clear()
    text, markup = await status_view(m.from_user.id)
    await reply(m, text, markup)


@router.message(Command("approve_all", "accept_all", ignore_case=True))
async def cmd_approve_all(m: Message, state: FSMContext):
    await state.clear()
    text, markup = await approve_all_view(m.from_user.id)
    await reply(m, text, markup)


@router.message(Command("admin"))
async def cmd_admin(m: Message, state: FSMContext):
    await state.clear()
    if m.from_user.id != OWNER_ID:
        return
    await reply(m, "👑 <b>Admin Panel</b>", admin_kb())


@router.message(Command("broadcast"))
async def cmd_broadcast(m: Message, state: FSMContext):
    if m.from_user.id != OWNER_ID:
        return
    await state.set_state(Broadcast.waiting)
    await m.answer("📢 Send the message to broadcast to all bot users (text / photo / video / anything).\n/cancel to stop.")


@router.message(Command("ban"))
async def cmd_ban(m: Message):
    if m.from_user.id != OWNER_ID:
        return
    parts = (m.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        return await m.answer("Usage: /ban USER_ID")
    await run(
        "INSERT INTO users(user_id,banned,created_at) VALUES(?,1,?) ON CONFLICT(user_id) DO UPDATE SET banned=1",
        (int(parts[1]), int(time.time())),
    )
    await m.answer(f"🚫 Banned {parts[1]}")


@router.message(Command("unban"))
async def cmd_unban(m: Message):
    if m.from_user.id != OWNER_ID:
        return
    parts = (m.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        return await m.answer("Usage: /unban USER_ID")
    await run("UPDATE users SET banned=0 WHERE user_id=?", (int(parts[1]),))
    await m.answer(f"✅ Unbanned {parts[1]}")


# ----------------------------------------------------------------------------
# Support: any user can message the bot owner; owner replies with one tap
# ----------------------------------------------------------------------------
@router.message(Command("support"))
async def cmd_support(m: Message, state: FSMContext):
    if m.from_user.id == OWNER_ID:
        return await m.answer("You are the bot owner — users' messages will appear here with a Reply button.")
    await state.set_state(SupportMsg.waiting)
    await m.answer(
        sections(
            "💬 <b>Support</b>",
            box(
                "Send your message",
                "Type your question or issue below. The bot owner will reply here directly.",
                "Apna sawaal ya dikkat neeche likho. Bot owner yahin seedha reply karega.",
            ),
            "/cancel to stop.",
        )
    )


@router.message(F.text == MENU_SUPPORT)
async def menu_support(m: Message, state: FSMContext):
    await cmd_support(m, state)


@router.message(SupportMsg.waiting)
async def support_got(m: Message, state: FSMContext, bot: Bot):
    await state.clear()
    u = m.from_user
    data = extract_content(m)
    cur = await db.execute(
        "INSERT INTO support_msgs(user_id,name,username,content,created_at) VALUES(?,?,?,?,?)",
        (u.id, u.full_name, u.username, json.dumps(data) if data else None, int(time.time())),
    )
    await db.commit()
    ticket_id = cur.lastrowid
    header = (
        f"📩 <b>Support message #{ticket_id}</b>\nFrom: {escape(u.full_name)} "
        f"(@{u.username or '-'}, <code>{u.id}</code>)"
    )
    with suppress(TelegramAPIError):
        await bot.send_message(OWNER_ID, header, reply_markup=kb([[("↩️ Reply", f"sup:reply:{u.id}:{ticket_id}")]]))
        await bot.copy_message(OWNER_ID, m.chat.id, m.message_id)
    await m.answer(bi("✅ Sent! The owner will reply here.", "Bhej diya! Owner yahin reply karega."))


@router.callback_query(F.data.startswith("sup:reply:"))
async def sup_reply_start(c: CallbackQuery, state: FSMContext):
    if c.from_user.id != OWNER_ID:
        return await c.answer("Owner only", show_alert=True)
    parts = c.data.split(":")
    target = int(parts[2])
    ticket_id = int(parts[3]) if len(parts) > 3 else None
    await state.set_state(SupportReply.waiting)
    await state.update_data(target=target, ticket_id=ticket_id)
    await c.message.answer(f"✏️ Type your reply to <code>{target}</code>.\n/cancel to stop.")
    await c.answer()


@router.message(SupportReply.waiting)
async def sup_reply_send(m: Message, state: FSMContext, bot: Bot):
    if m.from_user.id != OWNER_ID:
        return
    data = await state.get_data()
    target, ticket_id = data.get("target"), data.get("ticket_id")
    await state.clear()
    try:
        await bot.send_message(target, f"📩 <b>Reply from support</b>\n\n{m.html_text or ''}")
        if ticket_id:
            await run("UPDATE support_msgs SET status='replied' WHERE id=?", (ticket_id,))
        await m.answer("✅ Sent.")
    except TelegramAPIError as e:
        await m.answer(f"❌ Could not deliver: {e}")


# ----------------------------------------------------------------------------
# Home / help callbacks
# ----------------------------------------------------------------------------
@router.callback_query(F.data == "home")
async def cb_home(c: CallbackQuery, state: FSMContext, bot: Bot):
    await state.clear()
    await show_content(c, bot, "start_text", DEFAULT_START, home_kb(c.from_user.id))
    await c.answer()


@router.callback_query(F.data == "help")
async def cb_help(c: CallbackQuery, bot: Bot):
    await show_content(c, bot, "help_text", DEFAULT_HELP, kb([[("🏠 Home", "home")]]))
    await c.answer()


# ----------------------------------------------------------------------------
# My Status (for any channel owner)
# ----------------------------------------------------------------------------
async def status_view(uid):
    o = await get_owner(uid)
    if not o["a_id"] and not o["b_id"]:
        return (
            sections("📊 <b>My Status</b>", box("No channel yet", "Connect a channel in ⚙️ Settings first.", "Pehle Settings me channel connect karo.")),
            kb([[("⚙️ Go to Settings", "s:main")]]),
        )
    parts = ["📊 <b>My Status</b>"]
    for w in ("a", "b"):
        if o[f"{w}_id"]:
            r = await one(
                "SELECT COUNT(*) t, SUM(status='approved') a, SUM(status='pending') p, "
                "SUM(status='approved' AND created_at >= ?) m FROM requests WHERE chat_id=?",
                (int(time.time()) - 30 * DAY, o[f"{w}_id"]),
            )
            parts.append(
                box(
                    f"Channel {w.upper()} — {escape(o[f'{w}_title'])}",
                    f"Total requests: {r['t'] or 0}\n✅ Approved (members added): {r['a'] or 0}\n"
                    f"📅 Approved this month: {r['m'] or 0}\n⏳ Pending: {r['p'] or 0}",
                )
            )
    return sections(*parts), kb([[("✅ Accept all", "s:approve"), ("⚙️ Settings", "s:main")]])


# ----------------------------------------------------------------------------
# Channel owner settings
# ----------------------------------------------------------------------------
def ch_line(title, link):
    if not title:
        return "❌ Not set"
    return f"{escape(title)}\n{escape(link or '')}"


async def settings_view(uid):
    o = await get_owner(uid)
    default_link = await get_setting("default_b_link", "")
    if o["a_id"] and o["b_id"]:
        if o["mode_confirmed"] and o["mode"] == "chain":
            mode = ("✅ Auto-approve", "B request → A is approved automatically.", "B me request aate hi A me approve.")
        elif o["mode_confirmed"]:
            mode = ("🕓 Manual", "Users only send requests. You approve with /accept_all.", "Users sirf request bhejenge. Aap /accept_all se approve karoge.")
        else:
            mode = ("⚠️ Not chosen yet", "Tap “Approve Mode”. Nothing is auto-approved until you confirm.", "“Approve Mode” dabao. Aapke confirm karne tak kuch auto-approve nahi hoga.")
    elif o["a_id"]:
        en = "Channel B is not set, so nothing is auto-approved."
        hi = "Channel B set nahi hai, isliye kuch auto-approve nahi hoga."
        if default_link:
            en += " Requesters get the bot owner's default link."
            hi += " Requesters ko bot owner ka default link milega."
        mode = ("🕓 Manual", en, hi)
    else:
        mode = ("—", "Set Channel A first.", "Pehle Channel A set karo.")
    text = sections(
        "⚙️ <b>Your Settings</b>",
        box("📢 Channel A (main)", ch_line(o["a_title"], o["a_link"])),
        box("📣 Channel B (gate, optional)", ch_line(o["b_title"], o["b_link"])),
        box(f"🔀 Approve mode: {mode[0]}", mode[1], mode[2]),
        box("💬 Welcome message", content_preview(o["welcome"])),
        box(
            "✅ Approve requests",
            "Post <code>/accept_all</code> in your channel to approve all pending requests.",
            "Channel me <code>/accept_all</code> likho, saari pending requests approve ho jayengi.",
        ),
    )
    markup = kb(
        [
            [("📢 Set/Change A", "s:set:a"), ("📣 Set/Change B", "s:set:b")],
            [("🔄 New link A", "s:link:a"), ("🔄 New link B", "s:link:b")],
            [("🔀 Approve Mode", "s:mode"), ("✏️ Welcome msg", "s:welcome")],
            [("📊 My Status", "s:stats"), ("✅ Accept all", "s:approve")],
            [("🏠 Home", "home")],
        ]
    )
    return text, markup


@router.callback_query(F.data == "s:main")
async def cb_settings(c: CallbackQuery, state: FSMContext):
    await state.clear()
    text, markup = await settings_view(c.from_user.id)
    await show(c.message, text, markup)
    await c.answer()


def mode_kb():
    return kb([[("✅ Auto-approve", "md:chain")], [("🕓 Manual (I approve later)", "md:later")], [("⬅️ Back", "s:main")]])


@router.callback_query(F.data == "s:mode")
async def cb_mode(c: CallbackQuery):
    await show(c.message, MODE_TEXT, mode_kb())
    await c.answer()


@router.callback_query(F.data.in_({"md:chain", "md:later"}))
async def cb_mode_set(c: CallbackQuery):
    o = await get_owner(c.from_user.id)
    new = c.data.split(":")[1]
    if new == "chain" and not (o["a_id"] and o["b_id"]):
        return await c.answer("Set both Channel A and B first.", show_alert=True)
    await run("UPDATE owners SET mode=?, mode_confirmed=1 WHERE user_id=?", (new, c.from_user.id))
    text, markup = await settings_view(c.from_user.id)
    await show(c.message, text, markup)
    await c.answer("Saved ✅")


@router.callback_query(F.data.startswith("s:set:"))
async def cb_set_channel(c: CallbackQuery, state: FSMContext):
    which = c.data.split(":")[2]
    await state.set_state(SetChannel.waiting)
    await state.update_data(which=which)
    await c.message.answer(
        sections(
            f"📢 <b>Send Channel {which.upper()}</b>",
            box("How to send", "Send its @username, its ID (like -100123...), or forward any post from it.", "Uska @username, ID (jaise -100123...) bhejo ya uska koi post forward karo."),
            box("Required", "The bot must be admin there (Invite Users) and you must be admin too.", "Bot wahan admin hona chahiye (Invite Users) aur aap bhi admin hone chahiye."),
            "/cancel to stop.",
        )
    )
    await c.answer()


@router.callback_query(F.data.startswith("s:link:"))
async def cb_new_link(c: CallbackQuery, bot: Bot):
    which = c.data.split(":")[2]
    o = await get_owner(c.from_user.id)
    chat_id = o[f"{which}_id"]
    if not chat_id:
        return await c.answer("Set the channel first.", show_alert=True)
    try:
        link = await bot.create_chat_invite_link(chat_id, name="JoinGate", creates_join_request=True)
    except TelegramAPIError as e:
        return await c.answer(f"Error: {e}"[:190], show_alert=True)
    await run(f"UPDATE owners SET {which}_link=? WHERE user_id=?", (link.invite_link, c.from_user.id))
    text, markup = await settings_view(c.from_user.id)
    await show(c.message, text, markup)
    await c.answer("New link created ✅")


@router.callback_query(F.data == "s:welcome")
async def cb_welcome(c: CallbackQuery, state: FSMContext):
    await state.set_state(EditWelcome.waiting)
    await c.message.answer(
        sections(
            "✏️ <b>Edit welcome message</b>",
            box("What to send", "Send your new welcome message. Text, photo, video or GIF (with caption) all work. Use <code>{name}</code> for the user's name.", "Naya welcome message bhejo. Text, photo, video ya GIF (caption ke saath) sab chalega. User ka naam dalne ke liye <code>{name}</code> likho."),
            box("Reset", "Send <code>default</code> to go back to the default message.", "Default par wapas jaane ke liye <code>default</code> bhejo."),
            box("Channel card", "With a text welcome, the bot adds your Channel B card (logo + bio) automatically.", "Text welcome ke saath bot Channel B ka card (logo + bio) khud laga deta hai."),
            "/cancel to stop.",
        )
    )
    await c.answer()


@router.message(EditWelcome.waiting)
async def got_welcome(m: Message, state: FSMContext):
    await get_owner(m.from_user.id)
    if m.text and m.text.strip().lower() == "default":
        await run("UPDATE owners SET welcome=NULL WHERE user_id=?", (m.from_user.id,))
    else:
        data = extract_content(m)
        if not data:
            return await m.answer(bi("❌ Send text, photo, video or GIF.", "Text, photo, video ya GIF bhejo."))
        await run("UPDATE owners SET welcome=? WHERE user_id=?", (json.dumps(data), m.from_user.id))
    await state.clear()
    text, markup = await settings_view(m.from_user.id)
    await m.answer(bi("✅ Welcome message updated.", "Welcome message update ho gaya."))
    await reply(m, text, markup)


@router.message(SetChannel.waiting)
async def got_channel(m: Message, state: FSMContext, bot: Bot):
    which = (await state.get_data()).get("which", "a")
    chat = None
    try:
        fo = getattr(m, "forward_origin", None)
        if fo is not None and getattr(fo, "chat", None) is not None:
            chat = await bot.get_chat(fo.chat.id)
        else:
            txt = (m.text or "").strip()
            for p in ("https://t.me/", "http://t.me/", "t.me/"):
                txt = txt.replace(p, "")
            if txt.lstrip("-").isdigit():
                ident = int(txt)
            else:
                ident = txt if txt.startswith("@") else "@" + txt.split("/")[0]
            chat = await bot.get_chat(ident)
    except TelegramAPIError:
        return await m.answer(bi("❌ Channel not found. Send @username, numeric ID, or forward a post. The bot must be admin there.", "Channel nahi mila. @username, ID bhejo ya post forward karo. Bot wahan admin hona chahiye."))

    if chat.type not in ("channel", "supergroup", "group"):
        return await m.answer(bi("❌ This is not a channel or group.", "Ye channel ya group nahi hai."))

    try:
        me = await bot.get_chat_member(chat.id, bot.id)
    except TelegramAPIError:
        return await m.answer(bi("❌ Add the bot as admin there first.", "Pehle bot ko wahan admin banao."))
    if me.status != ChatMemberStatus.ADMINISTRATOR or not getattr(me, "can_invite_users", False):
        return await m.answer(bi("❌ The bot needs admin rights with the Invite Users permission.", "Bot ko Invite Users permission ke saath admin banao."))

    try:
        um = await bot.get_chat_member(chat.id, m.from_user.id)
    except TelegramAPIError:
        um = None
    if m.from_user.id != OWNER_ID and (um is None or um.status not in (ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR)):
        return await m.answer(bi("❌ You must be an admin of that channel.", "Aap us channel ke admin nahi ho."))

    used = await one("SELECT user_id FROM owners WHERE (a_id=? OR b_id=?) AND user_id!=?", (chat.id, chat.id, m.from_user.id))
    if used:
        return await m.answer(bi("❌ This channel is already linked to another owner.", "Ye channel pehle se kisi aur owner se juda hai."))

    o = await get_owner(m.from_user.id)
    other = "b" if which == "a" else "a"
    if o[f"{other}_id"] == chat.id:
        return await m.answer(bi("❌ Channel A and B must be different.", "Channel A aur B alag hone chahiye."))

    try:
        link = await bot.create_chat_invite_link(chat.id, name="JoinGate", creates_join_request=True)
    except TelegramAPIError as e:
        return await m.answer(f"❌ Could not create link: {e}")

    await run(
        f"UPDATE owners SET {which}_id=?, {which}_title=?, {which}_link=? WHERE user_id=?",
        (chat.id, chat.title or str(chat.id), link.invite_link, m.from_user.id),
    )
    await state.clear()
    text, markup = await settings_view(m.from_user.id)
    await m.answer(bi(f"✅ Channel {which.upper()} saved.", f"Channel {which.upper()} save ho gaya."))
    await reply(m, text, markup)
    o = await get_owner(m.from_user.id)
    if o["a_id"] and o["b_id"] and not o["mode_confirmed"]:
        await m.answer(
            bi("❓ <b>Both channels are set. Please confirm below.</b>", "Dono channel set hain. Neeche confirm karo.") + "\n\n" + MODE_TEXT,
            reply_markup=mode_kb(),
        )


@router.callback_query(F.data == "s:stats")
async def cb_stats(c: CallbackQuery):
    text, markup = await status_view(c.from_user.id)
    await show(c.message, text, markup)
    await c.answer()


# ----------------------------------------------------------------------------
# Approve all
# ----------------------------------------------------------------------------
async def approve_all_view(uid):
    o = await get_owner(uid)
    if not o["a_id"] and not o["b_id"]:
        return bi("❌ Set your channels first in /settings.", "Pehle /settings me channel set karo."), None
    counts = {}
    for w in ("a", "b"):
        if o[f"{w}_id"]:
            r = await one("SELECT COUNT(*) n FROM requests WHERE chat_id=? AND status='pending'", (o[f"{w}_id"],))
            counts[w] = r["n"]
    rows = []
    if "a" in counts:
        rows.append([(f"✅ Approve A ({counts['a']})", "aa:a")])
    if "b" in counts:
        rows.append([(f"✅ Approve B ({counts['b']})", "aa:b")])
    if len(counts) == 2:
        rows.append([("✅ Approve Both", "aa:both")])
    rows.append([("❌ Close", "home")])
    text = sections(
        "✅ <b>Accept all pending requests</b>",
        box("Note", "Only requests received while the bot was running are counted.", "Sirf wahi requests ginti me aati hain jo bot chalu hone par aayi thi."),
    )
    return text, kb(rows)


@router.callback_query(F.data == "s:approve")
async def cb_approve_menu(c: CallbackQuery):
    text, markup = await approve_all_view(c.from_user.id)
    await show(c.message, text, markup)
    await c.answer()


async def approve_one(bot: Bot, chat_id, user_id) -> bool:
    for _ in range(2):
        try:
            await bot.approve_chat_join_request(chat_id, user_id)
            return True
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except TelegramAPIError:
            return False
    return False


async def run_approve_all(bot: Bot, chat_ids, notify_id):
    ok = fail = 0
    marks = ",".join("?" * len(chat_ids))
    rows = await many(f"SELECT chat_id,user_id FROM requests WHERE status='pending' AND chat_id IN ({marks})", chat_ids)
    for r in rows:
        good = await approve_one(bot, r["chat_id"], r["user_id"])
        await run("UPDATE requests SET status=? WHERE chat_id=? AND user_id=?", ("approved" if good else "failed", r["chat_id"], r["user_id"]))
        ok += good
        fail += not good
        await asyncio.sleep(0.1)
    with suppress(TelegramAPIError):
        await bot.send_message(notify_id, f"✅ Done.\nApproved: {ok}\nFailed (left or expired): {fail}")


@router.callback_query(F.data.startswith("aa:"))
async def cb_approve_run(c: CallbackQuery, bot: Bot):
    which = c.data.split(":")[1]
    o = await get_owner(c.from_user.id)
    ids = []
    if which in ("a", "both") and o["a_id"]:
        ids.append(o["a_id"])
    if which in ("b", "both") and o["b_id"]:
        ids.append(o["b_id"])
    if not ids:
        return await c.answer("Nothing to approve", show_alert=True)
    asyncio.create_task(run_approve_all(bot, ids, c.from_user.id))
    await show(c.message, bi("⏳ Approving... I will message you when done.", "Approve ho raha hai, pura hone par bata dunga."), kb([[("🏠 Home", "home")]]))
    await c.answer()


# ----------------------------------------------------------------------------
# Join requests - the DM goes out FIRST, before any database work
# ----------------------------------------------------------------------------
async def record_request(chat_id, user, owner_id):
    await upsert_user(user)
    await run(
        "INSERT OR REPLACE INTO requests(chat_id,user_id,owner_id,status,created_at) VALUES(?,?,?,'pending',?)",
        (chat_id, user.id, owner_id, int(time.time())),
    )


def approved_text(a_title: str) -> str:
    return (
        f"🎉 <b>You are approved in {escape(a_title)}!</b>\n"
        f"<i>Aap {escape(a_title)} me approve ho gaye ho.</i>"
    )


@router.chat_join_request()
async def on_join_request(req: ChatJoinRequest, bot: Bot):
    chat_id = req.chat.id
    user = req.from_user
    o = await one("SELECT * FROM owners WHERE a_id=? OR b_id=?", (chat_id, chat_id))
    if not o:
        return
    target = req.user_chat_id or user.id

    if o["a_id"] == chat_id:
        gate_link = (o["b_link"] if o["b_id"] else None) or (await get_setting("default_b_link", "")) or None
        auto = bool(o["b_id"] and o["mode"] == "chain" and o["mode_confirmed"])
        data = parse_content(o["welcome"], legacy_escape=True) or {"t": "text", "c": default_welcome(auto, bool(gate_link))}
        markup = None
        if gate_link:
            rows = [[InlineKeyboardButton(text="📣 Open channel & send request", url=gate_link)]]
            if o["b_id"]:
                rows.append([InlineKeyboardButton(text="✅ Done — I already joined", callback_data=f"chk:{chat_id}")])
            markup = InlineKeyboardMarkup(inline_keyboard=rows)
        sent = False
        for dest in dict.fromkeys([target, user.id]):
            try:
                await send_content(bot, dest, data, markup, name=user.first_name or "friend", preview_url=gate_link)
                sent = True
                logging.info("DM sent to %s for request in chat %s", user.id, chat_id)
                break
            except TelegramAPIError as e:
                logging.warning("DM to %s (via %s) failed: %s", user.id, dest, e)
        if not sent:
            logging.warning("Could not DM user %s. Is the bot admin with Invite Users in chat %s?", user.id, chat_id)
        await record_request(chat_id, user, o["user_id"])
        return

    await record_request(chat_id, user, o["user_id"])
    if o["mode"] == "chain" and o["mode_confirmed"] and o["a_id"]:
        pending_a = await one("SELECT 1 FROM requests WHERE chat_id=? AND user_id=? AND status='pending'", (o["a_id"], user.id))
        if pending_a:
            good = await approve_one(bot, o["a_id"], user.id)
            await run("UPDATE requests SET status=? WHERE chat_id=? AND user_id=?", ("approved" if good else "failed", o["a_id"], user.id))
            if good:
                with suppress(TelegramAPIError):
                    await bot.send_message(target, approved_text(o["a_title"] or "the channel"))


@router.callback_query(F.data.startswith("chk:"))
async def cb_check_join(c: CallbackQuery, bot: Bot):
    a_id = int(c.data.split(":")[1])
    user_id = c.from_user.id
    o = await one("SELECT * FROM owners WHERE a_id=?", (a_id,))
    if not o or not o["b_id"]:
        return await c.answer("This check is not available anymore.", show_alert=True)

    req_row = await one("SELECT status FROM requests WHERE chat_id=? AND user_id=?", (a_id, user_id))
    if req_row and req_row["status"] == "approved":
        return await c.answer("You're already approved! / Aap pehle se approve ho.", show_alert=True)

    try:
        member = await bot.get_chat_member(o["b_id"], user_id)
        is_member = member.status in (
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.CREATOR,
        )
    except TelegramAPIError:
        is_member = False

    if is_member:
        good = await approve_one(bot, a_id, user_id)
        await run(
            "UPDATE requests SET status=? WHERE chat_id=? AND user_id=?",
            ("approved" if good else "failed", a_id, user_id),
        )
        if good:
            with suppress(TelegramAPIError):
                await c.message.answer(approved_text(o["a_title"] or "the channel"))
            await c.answer("Approved! ✅")
        else:
            await c.answer("Could not approve — your request may have expired.", show_alert=True)
    else:
        with suppress(TelegramAPIError):
            await bot.decline_chat_join_request(a_id, user_id)
        await run(
            "UPDATE requests SET status='rejected' WHERE chat_id=? AND user_id=?",
            (a_id, user_id),
        )
        with suppress(TelegramAPIError):
            await c.message.answer(
                bi(
                    "❌ You are not a member of the backup channel, so your request was declined. Please send a fresh join request after actually joining.",
                    "Aap backup channel ke member nahi ho, isliye request decline kar di gayi. Pehle us channel me join karke dobara request bhejo.",
                )
            )
        await c.answer("Not a member — request declined.", show_alert=True)


@router.channel_post(F.text)
async def on_channel_post(m: Message, bot: Bot):
    cmd = m.text.strip().split()[0].lower().split("@")[0]
    if cmd not in ("/accept_all", "/approve_all"):
        return
    o = await one("SELECT * FROM owners WHERE a_id=? OR b_id=?", (m.chat.id, m.chat.id))
    if not o:
        return
    with suppress(TelegramAPIError):
        await bot.delete_message(m.chat.id, m.message_id)
    n = (await one("SELECT COUNT(*) n FROM requests WHERE chat_id=? AND status='pending'", (m.chat.id,)))["n"]
    with suppress(TelegramAPIError):
        await bot.send_message(
            o["user_id"],
            f"⏳ /accept_all received in <b>{escape(m.chat.title or str(m.chat.id))}</b>.\n"
            f"Approving {n} pending requests...\n<i>{n} pending requests approve ho rahi hain...</i>",
        )
    asyncio.create_task(run_approve_all(bot, [m.chat.id], o["user_id"]))


# ----------------------------------------------------------------------------
# Admin panel (bot owner only)
# ----------------------------------------------------------------------------
def admin_kb():
    return kb(
        [
            [("📊 Stats", "adm:stats"), ("👥 Channel owners", "adm:owners")],
            [("📥 Support Inbox", "adm:inbox")],
            [("📢 Broadcast (users)", "adm:bc"), ("📣 Broadcast (channels)", "adm:cbc")],
            [("✏️ Edit /start", "adm:edit:start"), ("✏️ Edit /help", "adm:edit:help")],
            [("🎯 Default gate link", "adm:edit:defb")],
            [("🤖 Bot name", "adm:edit:name")],
            [("📝 Bot description", "adm:edit:desc"), ("💬 Bot about", "adm:edit:about")],
            [("🏠 Home", "home")],
        ]
    )


def owner_only(c: CallbackQuery) -> bool:
    return c.from_user.id == OWNER_ID


@router.callback_query(F.data == "adm:main")
async def adm_main(c: CallbackQuery, state: FSMContext):
    if not owner_only(c):
        return await c.answer("Owner only", show_alert=True)
    await state.clear()
    await show(c.message, "👑 <b>Admin Panel</b>", admin_kb())
    await c.answer()


@router.callback_query(F.data == "adm:stats")
async def adm_stats(c: CallbackQuery):
    if not owner_only(c):
        return await c.answer("Owner only", show_alert=True)
    cutoff = int(time.time()) - 30 * DAY
    u = await one("SELECT COUNT(*) n, SUM(banned) b FROM users")
    active = (await one("SELECT COUNT(*) n FROM users WHERE last_seen >= ?", (cutoff,)))["n"]
    ch = await one(
        "SELECT (SELECT COUNT(*) FROM owners WHERE a_id IS NOT NULL) + (SELECT COUNT(*) FROM owners WHERE b_id IS NOT NULL) n"
    )
    owners = (await one("SELECT COUNT(*) n FROM owners WHERE a_id IS NOT NULL OR b_id IS NOT NULL"))["n"]
    r = await one(
        "SELECT COUNT(*) n, SUM(status='approved') a, SUM(status='pending') p, "
        "SUM(status='approved' AND created_at >= ?) m FROM requests",
        (cutoff,),
    )
    text = sections(
        "📊 <b>Bot Stats</b>",
        box("Bot users", f"Total: {u['n']}\n📅 Active last 30 days: {active}\n🚫 Banned: {u['b'] or 0}"),
        box("Channels", f"Bot is admin in: {ch['n']}\nChannel owners: {owners}"),
        box(
            "Members added (via requests)",
            f"Total approved: {r['a'] or 0}\n📅 Approved last 30 days: {r['m'] or 0}\n⏳ Pending: {r['p'] or 0}\nAll requests: {r['n']}",
        ),
    )
    await show(c.message, text, kb([[("👥 Per-owner breakdown", "adm:owners")], [("⬅️ Back", "adm:main")]]))
    await c.answer()


@router.callback_query(F.data == "adm:owners")
async def adm_owners(c: CallbackQuery):
    if not owner_only(c):
        return await c.answer("Owner only", show_alert=True)
    cutoff = int(time.time()) - 30 * DAY
    rows = await many("SELECT user_id,a_id,a_title,b_id,b_title,mode,mode_confirmed FROM owners WHERE a_id IS NOT NULL OR b_id IS NOT NULL LIMIT 40")
    parts = ["👥 <b>Channel owners</b> — members added via this bot (ban with /ban ID)"]
    for r in rows:
        ids = [r["a_id"], r["b_id"]]
        ids = [i for i in ids if i]
        marks = ",".join("?" * len(ids))
        agg = await one(
            f"SELECT SUM(status='approved') a, SUM(status='approved' AND created_at >= ?) m FROM requests WHERE chat_id IN ({marks})",
            (cutoff, *ids),
        )
        parts.append(
            box(
                f"<code>{r['user_id']}</code>",
                f"A: {escape(r['a_title'] or '-')}\nB: {escape(r['b_title'] or '-')}\n"
                f"👥 Total members: {agg['a'] or 0} | 📅 This month: {agg['m'] or 0}",
            )
        )
    if not rows:
        parts.append("No one yet.")
    await show(c.message, sections(*parts), kb([[("⬅️ Back", "adm:main")]]))
    await c.answer()


EDIT_PROMPTS = {
    "start": "✏️ Send the new <b>/start</b> message. Text, photo, video or GIF (with caption) all work, with formatting. Send <code>default</code> to reset.",
    "help": "✏️ Send the new <b>/help</b> guide. Text, photo, video or GIF (with caption) all work, with formatting. Send <code>default</code> to reset.",
    "defb": "🎯 Send your channel link (https://t.me/yourchannel or @yourchannel). It is sent to requesters when a channel owner has NOT set their own Channel B (no auto-approve then). Send <code>off</code> to disable.",
    "name": "🤖 Send the new bot <b>name</b> (max 64 chars).",
    "desc": "📝 Send the new bot <b>description</b> (max 512 chars).",
    "about": "💬 Send the new bot <b>about</b> text (max 120 chars).",
}


@router.callback_query(F.data == "adm:inbox")
async def adm_inbox(c: CallbackQuery):
    if not owner_only(c):
        return await c.answer("Owner only", show_alert=True)
    rows = await many(
        "SELECT id,user_id,name,username,content,status,created_at FROM support_msgs ORDER BY id DESC LIMIT 15"
    )
    if not rows:
        return await show(c.message, sections("📥 <b>Support Inbox</b>", "No messages yet.\n<i>Abhi koi message nahi hai.</i>"), kb([[("⬅️ Back", "adm:main")]]))
    parts = ["📥 <b>Support Inbox</b> — latest 15"]
    buttons = []
    for r in rows:
        tag = "✅" if r["status"] == "replied" else "🆕"
        parts.append(
            box(
                f"{tag} #{r['id']} — {escape(r['name'] or '-')} (@{r['username'] or '-'})",
                content_preview(r["content"]) or "(no text)",
            )
        )
        if r["status"] != "replied":
            buttons.append([(f"↩️ Reply #{r['id']}", f"sup:reply:{r['user_id']}:{r['id']}")])
    buttons.append([("⬅️ Back", "adm:main")])
    await show(c.message, sections(*parts), kb(buttons))
    await c.answer()


@router.callback_query(F.data.startswith("adm:edit:"))
async def adm_edit(c: CallbackQuery, state: FSMContext):
    if not owner_only(c):
        return await c.answer("Owner only", show_alert=True)
    key = c.data.split(":")[2]
    await state.set_state(AdminEdit.waiting)
    await state.update_data(key=key)
    extra = ""
    if key == "defb":
        cur = await get_setting("default_b_link", "")
        extra = f"\n\nCurrent: {escape(cur) if cur else 'not set'}"
    await c.message.answer(EDIT_PROMPTS[key] + extra + "\n/cancel to stop.")
    await c.answer()


@router.message(AdminEdit.waiting)
async def adm_edit_got(m: Message, state: FSMContext, bot: Bot):
    if m.from_user.id != OWNER_ID:
        return
    key = (await state.get_data()).get("key")
    try:
        if key in ("start", "help"):
            k = f"{key}_text"
            if m.text and m.text.strip().lower() == "default":
                await run("DELETE FROM settings WHERE key=?", (k,))
            else:
                data = extract_content(m)
                if not data:
                    return await m.answer("❌ Send text, photo, video or GIF.")
                await run("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (k, json.dumps(data)))
        else:
            if not m.text:
                return await m.answer("❌ Send text only.")
            plain = m.text.strip()
            if key == "defb":
                if plain.lower() in ("off", "none", "default"):
                    await run("DELETE FROM settings WHERE key='default_b_link'")
                else:
                    link = plain
                    if link.startswith("@"):
                        link = "https://t.me/" + link[1:]
                    elif link.startswith("t.me/"):
                        link = "https://" + link
                    if not link.startswith("https://t.me/"):
                        return await m.answer("❌ Send a valid link like https://t.me/yourchannel or @yourchannel (or off).")
                    await run("INSERT OR REPLACE INTO settings(key,value) VALUES('default_b_link',?)", (link,))
            elif key == "name":
                await bot.set_my_name(name=plain[:64])
            elif key == "desc":
                await bot.set_my_description(description=plain[:512])
            elif key == "about":
                await bot.set_my_short_description(short_description=plain[:120])
    except TelegramAPIError as e:
        return await m.answer(f"❌ Failed: {e}")
    await state.clear()
    await m.answer("✅ Updated!")


# ----------------------------------------------------------------------------
# Broadcast to USERS (bot owner only) - DMs everyone who has talked to the bot
# ----------------------------------------------------------------------------
@router.callback_query(F.data == "adm:bc")
async def adm_bc(c: CallbackQuery, state: FSMContext):
    if not owner_only(c):
        return await c.answer("Owner only", show_alert=True)
    await state.set_state(Broadcast.waiting)
    await c.message.answer("📢 Send the message to broadcast to all bot users (text / photo / video / anything).\n/cancel to stop.")
    await c.answer()


@router.message(Broadcast.waiting)
async def bc_got(m: Message, state: FSMContext):
    if m.from_user.id != OWNER_ID:
        return
    n = (await one("SELECT COUNT(*) n FROM users WHERE banned=0"))["n"]
    await state.update_data(chat_id=m.chat.id, message_id=m.message_id)
    await state.set_state(Broadcast.confirm)
    await m.answer(f"Send this to <b>{n}</b> bot users?", reply_markup=kb([[("✅ Send", "bc:go"), ("❌ Cancel", "bc:no")]]))


async def do_broadcast(bot: Bot, src_chat, src_msg, notify_id):
    rows = await many("SELECT user_id FROM users WHERE banned=0")
    ok = fail = 0
    for r in rows:
        uid = r["user_id"]
        for _ in range(2):
            try:
                await bot.copy_message(uid, src_chat, src_msg)
                ok += 1
                break
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except TelegramAPIError:
                fail += 1
                break
        await asyncio.sleep(0.05)
    with suppress(TelegramAPIError):
        await bot.send_message(notify_id, f"📢 User broadcast finished\n✅ Sent: {ok}\n❌ Failed: {fail}")


@router.callback_query(Broadcast.confirm, F.data.in_({"bc:go", "bc:no"}))
async def bc_confirm(c: CallbackQuery, state: FSMContext, bot: Bot):
    if not owner_only(c):
        return await c.answer("Owner only", show_alert=True)
    data = await state.get_data()
    await state.clear()
    if c.data == "bc:no":
        await show(c.message, "❌ Broadcast cancelled")
        return await c.answer()
    asyncio.create_task(do_broadcast(bot, data["chat_id"], data["message_id"], c.from_user.id))
    await show(c.message, "⏳ Broadcasting to users... I will notify you when done.")
    await c.answer()


# ----------------------------------------------------------------------------
# Broadcast to CHANNELS (bot owner only) - posts into every channel where the
# bot is admin through this system (every owner's Channel A and B).
# ----------------------------------------------------------------------------
async def all_admin_channels():
    rows = await many(
        "SELECT a_id c FROM owners WHERE a_id IS NOT NULL UNION SELECT b_id c FROM owners WHERE b_id IS NOT NULL"
    )
    return [r["c"] for r in rows]


@router.callback_query(F.data == "adm:cbc")
async def adm_cbc(c: CallbackQuery, state: FSMContext):
    if not owner_only(c):
        return await c.answer("Owner only", show_alert=True)
    n = len(await all_admin_channels())
    await state.set_state(ChannelBroadcast.waiting)
    await c.message.answer(
        f"📣 Send the message to post in all <b>{n}</b> channels the bot is admin in (not a DM — posted directly in the channels).\n/cancel to stop."
    )
    await c.answer()


@router.message(ChannelBroadcast.waiting)
async def cbc_got(m: Message, state: FSMContext):
    if m.from_user.id != OWNER_ID:
        return
    n = len(await all_admin_channels())
    await state.update_data(chat_id=m.chat.id, message_id=m.message_id)
    await state.set_state(ChannelBroadcast.confirm)
    await m.answer(f"Post this in <b>{n}</b> channels?", reply_markup=kb([[("✅ Post", "cbc:go"), ("❌ Cancel", "cbc:no")]]))


async def do_channel_broadcast(bot: Bot, src_chat, src_msg, notify_id):
    ids = await all_admin_channels()
    ok = fail = 0
    for cid in ids:
        for _ in range(2):
            try:
                await bot.copy_message(cid, src_chat, src_msg)
                ok += 1
                break
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except TelegramAPIError:
                fail += 1
                break
        await asyncio.sleep(0.2)
    with suppress(TelegramAPIError):
        await bot.send_message(notify_id, f"📣 Channel broadcast finished\n✅ Posted: {ok}\n❌ Failed: {fail}")


@router.callback_query(ChannelBroadcast.confirm, F.data.in_({"cbc:go", "cbc:no"}))
async def cbc_confirm(c: CallbackQuery, state: FSMContext, bot: Bot):
    if not owner_only(c):
        return await c.answer("Owner only", show_alert=True)
    data = await state.get_data()
    await state.clear()
    if c.data == "cbc:no":
        await show(c.message, "❌ Channel broadcast cancelled")
        return await c.answer()
    asyncio.create_task(do_channel_broadcast(bot, data["chat_id"], data["message_id"], c.from_user.id))
    await show(c.message, "⏳ Posting to channels... I will notify you when done.")
    await c.answer()


# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------
async def main():
    if not BOT_TOKEN or not OWNER_ID:
        raise SystemExit("Set BOT_TOKEN and OWNER_ID in .env")
    await init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    bot.session.middleware(StyleMiddleware())
    dp = Dispatcher(storage=MemoryStorage())
    dp.message.outer_middleware(BanMiddleware())
    dp.callback_query.outer_middleware(BanMiddleware())
    dp.message.outer_middleware(SeenMiddleware())
    dp.callback_query.outer_middleware(SeenMiddleware())
    dp.include_router(router)
    await bot.set_my_commands(
        [
            BotCommand(command="start", description=style("Start")),
            BotCommand(command="help", description=style("Guide")),
            BotCommand(command="settings", description=style("Channel settings")),
            BotCommand(command="accept_all", description=style("Accept all pending requests")),
            BotCommand(command="support", description=style("Contact bot owner")),
            BotCommand(command="cancel", description=style("Cancel")),
        ]
    )
    await bot.delete_webhook(drop_pending_updates=False)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
