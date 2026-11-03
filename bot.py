"""
JoinGate Bot v3 - 2 channel join-request approval bot
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
    LinkPreviewOptions,
    Message,
)
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
DB_PATH = os.getenv("DB_PATH", "joingate.db")

router = Router()
db: aiosqlite.Connection = None  # type: ignore
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

# ----------------------------------------------------------------------------
# Small-caps style: "Savan" -> "Sᴀᴠᴀɴ" for EVERY outgoing message and button.
# Set STYLE=0 in .env to turn it off.
# ----------------------------------------------------------------------------
STYLE_ON = os.getenv("STYLE", "1") != "0"
_SC = dict(zip("abcdefghijklmnopqrstuvwxyz", "ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀꜱᴛᴜᴠᴡxʏᴢ"))
# parts that must stay untouched: <code>..</code>, html tags, entities, links, @names, /commands, {placeholders}
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
    """Applies the small-caps style to the text, caption and buttons of every outgoing request."""

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
        "Tap ⚙️ Settings to connect your channel. Tap 📖 Help for the full guide.",
        "Channel connect karne ke liye ⚙️ Settings dabao. Poori guide ke liye 📖 Help dabao.",
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
        "6️⃣ Welcome message",
        "Edit it anytime in ⚙️ Settings. Use <code>{name}</code> for the user's name. You can add a photo or video too.",
        "Settings me kabhi bhi badlo. User ka naam dalne ke liye <code>{name}</code> likho. Photo ya video bhi laga sakte ho.",
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
    box(
        "✅ Auto-approve",
        "Their Channel A request is approved automatically.",
        "Unki A ki request automatically approve ho jayegi.",
    ),
    box(
        "🕓 Manual",
        "Users only send requests. You approve later by posting <code>/accept_all</code> in your channel.",
        "Users sirf request bhejenge. Aap baad me channel me <code>/accept_all</code> likhkar approve karoge.",
    ),
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
            created_at INTEGER
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
        """
    )
    await db.commit()
    try:  # migration for databases created by older versions
        await db.execute("ALTER TABLE owners ADD COLUMN mode_confirmed INTEGER DEFAULT 0")
        await db.commit()
    except Exception:
        pass


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
    await run(
        "INSERT INTO users(user_id,name,username,created_at) VALUES(?,?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET name=excluded.name, username=excluded.username",
        (u.id, u.full_name, u.username, int(time.time())),
    )


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
    """Return a content dict from a stored value (JSON, or legacy plain/HTML text)."""
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
    """Send stored content. For text, preview_url shows the channel card (logo + bio) under the text."""
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
            return await bot.send_message(
                chat_id, c or "👋", reply_markup=markup, parse_mode=parse, link_preview_options=lp
            )
        sender = {
            "photo": bot.send_photo,
            "video": bot.send_video,
            "animation": bot.send_animation,
            "document": bot.send_document,
        }[t]
        cap = c if 0 < len(c) <= 1024 else None
        msg = await sender(
            chat_id,
            data["f"],
            caption=cap,
            reply_markup=markup if (cap or not c) else None,
            parse_mode=parse,
        )
        if c and cap is None:
            await bot.send_message(
                chat_id, c, reply_markup=markup, parse_mode=parse, link_preview_options=NO_PREVIEW
            )
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
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows]
    )


def home_kb(uid):
    rows = [[("⚙️ Settings", "s:main"), ("📖 Help", "help")]]
    if uid == OWNER_ID:
        rows.append([("👑 Admin Panel", "adm:main")])
    return kb(rows)


async def show(msg: Message, text: str, markup=None):
    """Edit the message if possible, otherwise send a new one (plain text if HTML is broken)."""
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
# Middleware: banned users
# ----------------------------------------------------------------------------
class BanMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user and user.id != OWNER_ID:
            r = await one("SELECT banned FROM users WHERE user_id=?", (user.id,))
            if r and r["banned"]:
                return
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


# ----------------------------------------------------------------------------
# Commands (registered first so they always win over FSM handlers)
# ----------------------------------------------------------------------------
@router.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext, bot: Bot):
    await state.clear()
    await upsert_user(m.from_user)
    data = await get_content("start_text", DEFAULT_START)
    await send_content(bot, m.chat.id, data, home_kb(m.from_user.id))


@router.message(Command("help"))
async def cmd_help(m: Message, state: FSMContext, bot: Bot):
    await state.clear()
    await upsert_user(m.from_user)
    data = await get_content("help_text", DEFAULT_HELP)
    await send_content(bot, m.chat.id, data, kb([[("🏠 Home", "home")]]))


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
    await m.answer("📢 Send the message to broadcast (text / photo / video / anything).\n/cancel to stop.")


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
            [("📊 Stats", "s:stats"), ("✅ Accept all", "s:approve")],
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
    return kb(
        [
            [("✅ Auto-approve", "md:chain")],
            [("🕓 Manual (I approve later)", "md:later")],
            [("⬅️ Back", "s:main")],
        ]
    )


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
            box(
                "How to send",
                "Send its @username, its ID (like -100123...), or forward any post from it.",
                "Uska @username, ID (jaise -100123...) bhejo ya uska koi post forward karo.",
            ),
            box(
                "Required",
                "The bot must be admin there (Invite Users) and you must be admin too.",
                "Bot wahan admin hona chahiye (Invite Users) aur aap bhi admin hone chahiye.",
            ),
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
            box(
                "What to send",
                "Send your new welcome message. Text, photo, video or GIF (with caption) all work. Use <code>{name}</code> for the user's name.",
                "Naya welcome message bhejo. Text, photo, video ya GIF (caption ke saath) sab chalega. User ka naam dalne ke liye <code>{name}</code> likho.",
            ),
            box(
                "Reset",
                "Send <code>default</code> to go back to the default message.",
                "Default par wapas jaane ke liye <code>default</code> bhejo.",
            ),
            box(
                "Channel card",
                "With a text welcome, the bot adds your Channel B card (logo + bio) automatically.",
                "Text welcome ke saath bot Channel B ka card (logo + bio) khud laga deta hai.",
            ),
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
        return await m.answer(
            bi(
                "❌ Channel not found. Send @username, numeric ID, or forward a post. The bot must be admin there.",
                "Channel nahi mila. @username, ID bhejo ya post forward karo. Bot wahan admin hona chahiye.",
            )
        )

    if chat.type not in ("channel", "supergroup", "group"):
        return await m.answer(bi("❌ This is not a channel or group.", "Ye channel ya group nahi hai."))

    try:
        me = await bot.get_chat_member(chat.id, bot.id)
    except TelegramAPIError:
        return await m.answer(bi("❌ Add the bot as admin there first.", "Pehle bot ko wahan admin banao."))
    if me.status != ChatMemberStatus.ADMINISTRATOR or not getattr(me, "can_invite_users", False):
        return await m.answer(
            bi(
                "❌ The bot needs admin rights with the Invite Users permission.",
                "Bot ko Invite Users permission ke saath admin banao.",
            )
        )

    try:
        um = await bot.get_chat_member(chat.id, m.from_user.id)
    except TelegramAPIError:
        um = None
    if m.from_user.id != OWNER_ID and (
        um is None or um.status not in (ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR)
    ):
        return await m.answer(bi("❌ You must be an admin of that channel.", "Aap us channel ke admin nahi ho."))

    used = await one(
        "SELECT user_id FROM owners WHERE (a_id=? OR b_id=?) AND user_id!=?",
        (chat.id, chat.id, m.from_user.id),
    )
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
            bi("❓ <b>Both channels are set. Please confirm below.</b>", "Dono channel set hain. Neeche confirm karo.")
            + "\n\n"
            + MODE_TEXT,
            reply_markup=mode_kb(),
        )


@router.callback_query(F.data == "s:stats")
async def cb_stats(c: CallbackQuery):
    o = await get_owner(c.from_user.id)
    parts = ["📊 <b>Your Stats</b>"]
    for w in ("a", "b"):
        if o[f"{w}_id"]:
            r = await one(
                "SELECT COUNT(*) t, SUM(status='approved') a, SUM(status='pending') p FROM requests WHERE chat_id=?",
                (o[f"{w}_id"],),
            )
            parts.append(
                box(f"Channel {w.upper()}", f"Total: {r['t'] or 0}\n✅ Approved: {r['a'] or 0}\n⏳ Pending: {r['p'] or 0}")
            )
    if len(parts) == 1:
        parts.append(box("No data", "No channels set yet.", "Abhi koi channel set nahi hai."))
    await show(c.message, sections(*parts), kb([[("⬅️ Back", "s:main")]]))
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
        box(
            "Note",
            "Only requests received while the bot was running are counted.",
            "Sirf wahi requests ginti me aati hain jo bot chalu hone par aayi thi.",
        ),
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
    rows = await many(
        f"SELECT chat_id,user_id FROM requests WHERE status='pending' AND chat_id IN ({marks})", chat_ids
    )
    for r in rows:
        good = await approve_one(bot, r["chat_id"], r["user_id"])
        await run(
            "UPDATE requests SET status=? WHERE chat_id=? AND user_id=?",
            ("approved" if good else "failed", r["chat_id"], r["user_id"]),
        )
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
    await show(
        c.message,
        bi("⏳ Approving... I will message you when done.", "Approve ho raha hai, pura hone par bata dunga."),
        kb([[("🏠 Home", "home")]]),
    )
    await c.answer()


# ----------------------------------------------------------------------------
# Join requests (the core flow) - the DM goes out FIRST, before any database work
# ----------------------------------------------------------------------------
async def record_request(chat_id, user, owner_id):
    await upsert_user(user)
    await run(
        "INSERT OR REPLACE INTO requests(chat_id,user_id,owner_id,status,created_at) VALUES(?,?,?,'pending',?)",
        (chat_id, user.id, owner_id, int(time.time())),
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
        # gate link = owner's own Channel B, otherwise the bot owner's default link
        gate_link = (o["b_link"] if o["b_id"] else None) or (await get_setting("default_b_link", "")) or None
        auto = bool(o["b_id"] and o["mode"] == "chain" and o["mode_confirmed"])
        data = parse_content(o["welcome"], legacy_escape=True) or {
            "t": "text",
            "c": default_welcome(auto, bool(gate_link)),
        }
        markup = None
        if gate_link:
            markup = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="📣 Open channel & send request", url=gate_link)]]
            )
        # Telegram lets the bot DM a requester via user_chat_id even if they never started the bot
        sent = False
        for dest in dict.fromkeys([target, user.id]):
            try:
                await send_content(
                    bot, dest, data, markup, name=user.first_name or "friend", preview_url=gate_link
                )
                sent = True
                logging.info("DM sent to %s for request in chat %s", user.id, chat_id)
                break
            except TelegramAPIError as e:
                logging.warning("DM to %s (via %s) failed: %s", user.id, dest, e)
        if not sent:
            logging.warning("Could not DM user %s. Is the bot admin with Invite Users in chat %s?", user.id, chat_id)
        await record_request(chat_id, user, o["user_id"])
        return

    # request came to the owner's own Channel B
    await record_request(chat_id, user, o["user_id"])
    if o["mode"] == "chain" and o["mode_confirmed"] and o["a_id"]:
        pending_a = await one(
            "SELECT 1 FROM requests WHERE chat_id=? AND user_id=? AND status='pending'",
            (o["a_id"], user.id),
        )
        if pending_a:
            good = await approve_one(bot, o["a_id"], user.id)
            await run(
                "UPDATE requests SET status=? WHERE chat_id=? AND user_id=?",
                ("approved" if good else "failed", o["a_id"], user.id),
            )
            if good:
                with suppress(TelegramAPIError):
                    await bot.send_message(
                        target,
                        "🎉 <b>You are approved!</b>\n<i>Aap approve ho gaye ho.</i>",
                    )


@router.channel_post(F.text)
async def on_channel_post(m: Message, bot: Bot):
    """A channel admin posts /accept_all inside the channel -> approve all pending requests of that channel."""
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
            [("📢 Broadcast", "adm:bc")],
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
    u = await one("SELECT COUNT(*) n, SUM(banned) b FROM users")
    o = await one("SELECT COUNT(*) n FROM owners WHERE a_id IS NOT NULL OR b_id IS NOT NULL")
    r = await one("SELECT COUNT(*) n, SUM(status='approved') a, SUM(status='pending') p FROM requests")
    text = sections(
        "📊 <b>Bot Stats</b>",
        box("Users", f"Total: {u['n']}\nBanned: {u['b'] or 0}"),
        box("Channel owners", f"{o['n']}"),
        box("Requests", f"Total: {r['n']}\n✅ Approved: {r['a'] or 0}\n⏳ Pending: {r['p'] or 0}"),
    )
    await show(c.message, text, kb([[("⬅️ Back", "adm:main")]]))
    await c.answer()


@router.callback_query(F.data == "adm:owners")
async def adm_owners(c: CallbackQuery):
    if not owner_only(c):
        return await c.answer("Owner only", show_alert=True)
    rows = await many(
        "SELECT user_id,a_title,b_title,mode FROM owners WHERE a_id IS NOT NULL OR b_id IS NOT NULL LIMIT 50"
    )
    lines = ["👥 <b>Channel owners</b> (ban with /ban ID)"]
    for r in rows:
        lines.append(
            box(
                f"<code>{r['user_id']}</code>",
                f"A: {escape(r['a_title'] or '-')}\nB: {escape(r['b_title'] or '-')}\nMode: {r['mode']}",
            )
        )
    if not rows:
        lines.append("No one yet.")
    await show(c.message, sections(*lines), kb([[("⬅️ Back", "adm:main")]]))
    await c.answer()


EDIT_PROMPTS = {
    "start": "✏️ Send the new <b>/start</b> message. Text, photo, video or GIF (with caption) all work, with formatting. Send <code>default</code> to reset.",
    "help": "✏️ Send the new <b>/help</b> guide. Text, photo, video or GIF (with caption) all work, with formatting. Send <code>default</code> to reset.",
    "defb": "🎯 Send your channel link (https://t.me/yourchannel or @yourchannel). It is sent to requesters when a channel owner has NOT set their own Channel B (no auto-approve then). Send <code>off</code> to disable.",
    "name": "🤖 Send the new bot <b>name</b> (max 64 chars).",
    "desc": "📝 Send the new bot <b>description</b> (max 512 chars).",
    "about": "💬 Send the new bot <b>about</b> text (max 120 chars).",
}


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
# Broadcast (bot owner only)
# ----------------------------------------------------------------------------
@router.callback_query(F.data == "adm:bc")
async def adm_bc(c: CallbackQuery, state: FSMContext):
    if not owner_only(c):
        return await c.answer("Owner only", show_alert=True)
    await state.set_state(Broadcast.waiting)
    await c.message.answer("📢 Send the message to broadcast (text / photo / video / anything).\n/cancel to stop.")
    await c.answer()


@router.message(Broadcast.waiting)
async def bc_got(m: Message, state: FSMContext):
    if m.from_user.id != OWNER_ID:
        return
    n = (await one("SELECT COUNT(*) n FROM users WHERE banned=0"))["n"]
    await state.update_data(chat_id=m.chat.id, message_id=m.message_id)
    await state.set_state(Broadcast.confirm)
    await m.answer(f"Send this to <b>{n}</b> users?", reply_markup=kb([[("✅ Send", "bc:go"), ("❌ Cancel", "bc:no")]]))


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
        await bot.send_message(notify_id, f"📢 Broadcast finished\n✅ Sent: {ok}\n❌ Failed: {fail}")


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
    await show(c.message, "⏳ Broadcasting... I will notify you when done.")
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
    dp.include_router(router)
    await bot.set_my_commands(
        [
            BotCommand(command="start", description=style("Start")),
            BotCommand(command="help", description=style("Guide")),
            BotCommand(command="settings", description=style("Channel settings")),
            BotCommand(command="accept_all", description=style("Accept all pending requests")),
            BotCommand(command="cancel", description=style("Cancel")),
        ]
    )
    await bot.delete_webhook(drop_pending_updates=False)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
