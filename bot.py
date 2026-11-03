
Bot · PY
"""
JoinGate Bot - 2 channel join-request approval bot
aiogram 3.x + SQLite (aiosqlite)
"""
import asyncio
import logging
import os
import time
from html import escape
 
import aiosqlite
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramRetryAfter,
)
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
 
# ----------------------------------------------------------------------------
# Default texts (English + Hindi). Bot owner can edit /start and /help from bot.
# ----------------------------------------------------------------------------
DEFAULT_START = (
    "👋 <b>Welcome! / स्वागत है!</b>\n\n"
    "This bot approves join requests of your channel using a 2-channel flow.\n"
    "यह बॉट 2-चैनल सिस्टम से आपके चैनल की join requests approve करता है।\n\n"
    "⚙️ /settings - set up your channels / चैनल सेट करें\n"
    "📖 /help - how to use / इस्तेमाल करने का तरीका\n\n"
    "💯 Free for everyone / सबके लिए फ्री"
)
 
DEFAULT_HELP = (
    "📖 <b>How to use / कैसे इस्तेमाल करें</b>\n\n"
    "<b>1️⃣</b> Add this bot as <b>admin</b> in both channels (with <i>Invite Users</i> permission).\n"
    "दोनों चैनलों में बॉट को <b>admin</b> बनाएँ (Invite Users permission के साथ)।\n\n"
    "<b>2️⃣</b> Open /settings → <b>Set Channel A</b> and <b>Set Channel B</b>. "
    "Send @username, channel ID, or forward any post from the channel.\n"
    "/settings खोलें → Channel A और B सेट करें। @username, ID भेजें या चैनल की कोई पोस्ट forward करें।\n\n"
    "<b>3️⃣</b> Bot creates a join-request link for each channel. Share <b>Channel A link</b> with your audience.\n"
    "बॉट हर चैनल का join-request link बनाएगा। अपनी audience को <b>Channel A का link</b> दें।\n\n"
    "<b>4️⃣</b> Choose mode: <b>Chain</b> (user requests B → auto-approved in A) or <b>Manual</b> (approve both later).\n"
    "Mode चुनें: <b>Chain</b> (B में request → A में auto approve) या <b>Manual</b> (दोनों बाद में approve)।\n\n"
    "<b>5️⃣</b> Edit your welcome message anytime from /settings.\n"
    "Welcome message /settings से कभी भी बदलें।\n\n"
    "<b>6️⃣</b> Many pending requests? Use /approve_all.\n"
    "ज़्यादा requests हों तो /approve_all use करें।\n\n"
    "❓ /cancel - cancel current action / मौजूदा काम रोकें"
)
 
DEFAULT_WELCOME_CHAIN = (
    "Hi {name}! 👋\n"
    "Your request has been received ✅ / आपकी request आ गई है।\n\n"
    "To get approved, please send a join request to the channel below. "
    "Once you do, you'll be approved automatically.\n"
    "Approve होने के लिए नीचे वाले चैनल में join request भेजें। "
    "Request भेजते ही आप automatically approve हो जाएंगे।"
)
 
DEFAULT_WELCOME_LATER = (
    "Hi {name}! 👋\n"
    "Your request has been received ✅ / आपकी request आ गई है।\n\n"
    "Please also send a join request to the channel below. "
    "You will be approved soon.\n"
    "कृपया नीचे वाले चैनल में भी join request भेजें। जल्द approve कर दिया जाएगा।"
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
            mode TEXT DEFAULT 'chain',
            welcome TEXT
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
# UI helpers
# ----------------------------------------------------------------------------
def kb(rows):
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows]
    )
 
 
def home_kb(uid):
    rows = [[("⚙️ Settings", "s:main"), ("📖 Help / गाइड", "help")]]
    if uid == OWNER_ID:
        rows.append([("👑 Admin Panel", "adm:main")])
    return kb(rows)
 
 
async def show(msg: Message, text: str, markup=None):
    """Edit message if possible, else send a new one. Falls back to plain text if HTML is broken."""
    try:
        await msg.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
    except TelegramBadRequest as e:
        s = str(e).lower()
        if "not modified" in s:
            return
        if "parse" in s or "entities" in s:
            await msg.answer(text, reply_markup=markup, parse_mode=None)
        else:
            try:
                await msg.answer(text, reply_markup=markup, disable_web_page_preview=True)
            except TelegramBadRequest:
                await msg.answer(text, reply_markup=markup, parse_mode=None)
 
 
async def reply(msg: Message, text: str, markup=None):
    try:
        await msg.answer(text, reply_markup=markup, disable_web_page_preview=True)
    except TelegramBadRequest:
        await msg.answer(text, reply_markup=markup, parse_mode=None)
 
 
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
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    await upsert_user(m.from_user)
    text = await get_setting("start_text", DEFAULT_START)
    await reply(m, text, home_kb(m.from_user.id))
 
 
@router.message(Command("help"))
async def cmd_help(m: Message, state: FSMContext):
    await state.clear()
    await upsert_user(m.from_user)
    text = await get_setting("help_text", DEFAULT_HELP)
    await reply(m, text, kb([[("🏠 Home", "home")]]))
 
 
@router.message(Command("cancel"))
async def cmd_cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("❌ Cancelled / रद्द किया गया")
 
 
@router.message(Command("settings"))
async def cmd_settings(m: Message, state: FSMContext):
    await state.clear()
    await upsert_user(m.from_user)
    text, markup = await settings_view(m.from_user.id)
    await reply(m, text, markup)
 
 
@router.message(Command("approve_all"))
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
    await m.answer("📢 Send the message you want to broadcast (text / photo / video / anything).\n/cancel to stop.")
 
 
@router.message(Command("ban"))
async def cmd_ban(m: Message):
    if m.from_user.id != OWNER_ID:
        return
    parts = (m.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        return await m.answer("Usage: /ban USER_ID")
    await run(
        "INSERT INTO users(user_id,banned,created_at) VALUES(?,1,?) "
        "ON CONFLICT(user_id) DO UPDATE SET banned=1",
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
async def cb_home(c: CallbackQuery, state: FSMContext):
    await state.clear()
    text = await get_setting("start_text", DEFAULT_START)
    await show(c.message, text, home_kb(c.from_user.id))
    await c.answer()
 
 
@router.callback_query(F.data == "help")
async def cb_help(c: CallbackQuery):
    text = await get_setting("help_text", DEFAULT_HELP)
    await show(c.message, text, kb([[("🏠 Home", "home")]]))
    await c.answer()
 
 
# ----------------------------------------------------------------------------
# Channel owner settings
# ----------------------------------------------------------------------------
def ch_line(title, link):
    if not title:
        return "❌ Not set / सेट नहीं है"
    return f"{escape(title)}\n{link or ''}"
 
 
async def settings_view(uid):
    o = await get_owner(uid)
    mode_txt = (
        "🔗 Chain: user requests B → auto-approved in A\n    (B में request → A में auto approve)"
        if o["mode"] == "chain"
        else "🕓 Manual: approve both later (/approve_all)\n    (दोनों बाद में approve)"
    )
    welcome = o["welcome"] or "(default)"
    text = (
        "⚙️ <b>Your Settings / आपकी सेटिंग्स</b>\n\n"
        f"📢 <b>Channel A (main):</b>\n{ch_line(o['a_title'], o['a_link'])}\n\n"
        f"📣 <b>Channel B (gate):</b>\n{ch_line(o['b_title'], o['b_link'])}\n\n"
        f"🔀 <b>Mode:</b>\n{mode_txt}\n\n"
        f"💬 <b>Welcome message:</b>\n{escape(welcome[:300])}"
    )
    markup = kb(
        [
            [("📢 Set/Change A", "s:set:a"), ("📣 Set/Change B", "s:set:b")],
            [("🔄 New link A", "s:link:a"), ("🔄 New link B", "s:link:b")],
            [("🔀 Switch Mode", "s:mode"), ("✏️ Welcome msg", "s:welcome")],
            [("📊 Stats", "s:stats"), ("✅ Approve all", "s:approve")],
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
 
 
@router.callback_query(F.data == "s:mode")
async def cb_mode(c: CallbackQuery):
    o = await get_owner(c.from_user.id)
    new = "later" if o["mode"] == "chain" else "chain"
    await run("UPDATE owners SET mode=? WHERE user_id=?", (new, c.from_user.id))
    text, markup = await settings_view(c.from_user.id)
    await show(c.message, text, markup)
    await c.answer("Mode changed ✅")
 
 
@router.callback_query(F.data.startswith("s:set:"))
async def cb_set_channel(c: CallbackQuery, state: FSMContext):
    which = c.data.split(":")[2]
    await state.set_state(SetChannel.waiting)
    await state.update_data(which=which)
    await c.message.answer(
        f"📢 Send <b>Channel {which.upper()}</b> now:\n"
        "• its @username, or\n• its ID (like -100123...), or\n• forward any post from that channel\n\n"
        "⚠️ Bot must be <b>admin</b> there (Invite Users permission) and you must be admin too.\n"
        "Channel को @username / ID भेजें या कोई पोस्ट forward करें। बॉट और आप दोनों admin होने चाहिए।\n\n"
        "/cancel to stop."
    )
    await c.answer()
 
 
@router.callback_query(F.data.startswith("s:link:"))
async def cb_new_link(c: CallbackQuery, bot: Bot):
    which = c.data.split(":")[2]
    o = await get_owner(c.from_user.id)
    chat_id = o[f"{which}_id"]
    if not chat_id:
        return await c.answer("Set the channel first / पहले चैनल सेट करें", show_alert=True)
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
        "✏️ Send your new welcome message (this is DM'd to users when they request Channel A).\n"
        "Use <code>{name}</code> for the user's name. Send <code>default</code> to reset.\n\n"
        "नया welcome message भेजें। यूज़र का नाम डालने के लिए <code>{name}</code> लिखें। Reset के लिए <code>default</code> भेजें।\n"
        "/cancel to stop."
    )
    await c.answer()
 
 
@router.message(EditWelcome.waiting, F.text)
async def got_welcome(m: Message, state: FSMContext):
    txt = m.text.strip()
    val = None if txt.lower() == "default" else txt[:1000]
    await get_owner(m.from_user.id)
    await run("UPDATE owners SET welcome=? WHERE user_id=?", (val, m.from_user.id))
    await state.clear()
    text, markup = await settings_view(m.from_user.id)
    await m.answer("✅ Welcome message updated / अपडेट हो गया")
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
            "❌ Channel not found. Send @username, numeric ID, or forward a post. Bot must be added as admin.\n"
            "Channel नहीं मिला। @username, ID भेजें या पोस्ट forward करें।"
        )
 
    if chat.type not in ("channel", "supergroup", "group"):
        return await m.answer("❌ This is not a channel/group. / यह चैनल नहीं है।")
 
    # bot must be admin with invite rights
    try:
        me = await bot.get_chat_member(chat.id, bot.id)
    except TelegramAPIError:
        return await m.answer("❌ Add the bot as admin in that channel first. / पहले बॉट को admin बनाएँ।")
    if me.status != ChatMemberStatus.ADMINISTRATOR or not getattr(me, "can_invite_users", False):
        return await m.answer(
            "❌ Bot needs <b>admin</b> with <b>Invite Users</b> permission there.\n"
            "बॉट को Invite Users permission के साथ admin बनाएँ।"
        )
 
    # user must be admin of that channel (security)
    try:
        um = await bot.get_chat_member(chat.id, m.from_user.id)
    except TelegramAPIError:
        um = None
    if m.from_user.id != OWNER_ID and (
        um is None or um.status not in (ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR)
    ):
        return await m.answer("❌ You must be admin of that channel. / आप उस चैनल के admin नहीं हैं।")
 
    # channel already used by someone else?
    used = await one(
        "SELECT user_id FROM owners WHERE (a_id=? OR b_id=?) AND user_id!=?",
        (chat.id, chat.id, m.from_user.id),
    )
    if used:
        return await m.answer("❌ This channel is already linked to another owner. / यह चैनल पहले से किसी और से जुड़ा है।")
 
    o = await get_owner(m.from_user.id)
    other = "b" if which == "a" else "a"
    if o[f"{other}_id"] == chat.id:
        return await m.answer("❌ Channel A and B must be different. / A और B अलग होने चाहिए।")
 
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
    await m.answer(f"✅ Channel {which.upper()} set! / सेट हो गया!")
    await reply(m, text, markup)
 
 
@router.callback_query(F.data == "s:stats")
async def cb_stats(c: CallbackQuery):
    o = await get_owner(c.from_user.id)
    lines = ["📊 <b>Your Stats / आपके आँकड़े</b>\n"]
    for w in ("a", "b"):
        if o[f"{w}_id"]:
            r = await one(
                "SELECT COUNT(*) t, SUM(status='approved') a, SUM(status='pending') p FROM requests WHERE chat_id=?",
                (o[f"{w}_id"],),
            )
            lines.append(
                f"<b>Channel {w.upper()}:</b> total {r['t'] or 0} | ✅ {r['a'] or 0} | ⏳ {r['p'] or 0}"
            )
    if len(lines) == 1:
        lines.append("No channels set yet. / अभी कोई चैनल सेट नहीं है।")
    await show(c.message, "\n".join(lines), kb([[("⬅️ Back", "s:main")]]))
    await c.answer()
 
 
# ----------------------------------------------------------------------------
# Approve all
# ----------------------------------------------------------------------------
async def approve_all_view(uid):
    o = await get_owner(uid)
    if not o["a_id"] and not o["b_id"]:
        return "❌ Set your channels first in /settings. / पहले /settings में चैनल सेट करें।", None
    counts = {}
    for w in ("a", "b"):
        if o[f"{w}_id"]:
            r = await one(
                "SELECT COUNT(*) n FROM requests WHERE chat_id=? AND status='pending'", (o[f"{w}_id"],)
            )
            counts[w] = r["n"]
    rows = []
    if "a" in counts:
        rows.append([(f"✅ Approve A ({counts['a']})", "aa:a")])
    if "b" in counts:
        rows.append([(f"✅ Approve B ({counts['b']})", "aa:b")])
    if len(counts) == 2:
        rows.append([("✅ Approve Both", "aa:both")])
    rows.append([("❌ Close", "home")])
    text = (
        "✅ <b>Approve all pending requests / सभी pending requests approve करें</b>\n\n"
        "Only requests received while the bot was running are counted.\n"
        "सिर्फ वही requests गिनी जाती हैं जो बॉट चालू रहते आई थीं।"
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
    await bot.send_message(notify_id, f"✅ Done / पूरा हुआ\nApproved: {ok}\nFailed (left/expired): {fail}")
 
 
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
    await show(c.message, "⏳ Approving... I'll message you when done.\nApprove हो रहा है, पूरा होने पर बताऊँगा।",
               kb([[("🏠 Home", "home")]]))
    await c.answer()
 
 
# ----------------------------------------------------------------------------
# Join request handler (the core flow)
# ----------------------------------------------------------------------------
@router.chat_join_request()
async def on_join_request(req: ChatJoinRequest, bot: Bot):
    chat_id = req.chat.id
    user = req.from_user
    o = await one("SELECT * FROM owners WHERE a_id=? OR b_id=?", (chat_id, chat_id))
    if not o:
        return
    await upsert_user(user)
    await run(
        "INSERT OR REPLACE INTO requests(chat_id,user_id,owner_id,status,created_at) VALUES(?,?,?,'pending',?)",
        (chat_id, user.id, o["user_id"], int(time.time())),
    )
 
    if o["a_id"] == chat_id:
        # request came to Channel A -> DM welcome + Channel B link
        default = DEFAULT_WELCOME_CHAIN if o["mode"] == "chain" else DEFAULT_WELCOME_LATER
        text = (o["welcome"] or default).replace("{name}", user.first_name or "friend")
        markup = None
        if o["b_link"]:
            markup = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="📣 Send request / Request भेजें", url=o["b_link"])]]
            )
        try:
            await bot.send_message(req.user_chat_id, text, reply_markup=markup, parse_mode=None)
        except TelegramAPIError as e:
            logging.warning("DM failed for %s: %s", user.id, e)
        return
 
    # request came to Channel B
    if o["mode"] == "chain" and o["a_id"]:
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
                try:
                    await bot.send_message(
                        req.user_chat_id,
                        "🎉 You're approved in the main channel! / आप main channel में approve हो गए हैं!",
                        parse_mode=None,
                    )
                except TelegramAPIError:
                    pass
 
 
# ----------------------------------------------------------------------------
# Admin panel (bot owner only)
# ----------------------------------------------------------------------------
def admin_kb():
    return kb(
        [
            [("📊 Stats", "adm:stats"), ("👥 Channel owners", "adm:owners")],
            [("📢 Broadcast", "adm:bc")],
            [("✏️ Edit /start", "adm:edit:start"), ("✏️ Edit /help", "adm:edit:help")],
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
    text = (
        "📊 <b>Bot Stats</b>\n\n"
        f"👤 Users: {u['n']} (banned: {u['b'] or 0})\n"
        f"👥 Channel owners: {o['n']}\n"
        f"📩 Requests: {r['n']} | ✅ {r['a'] or 0} | ⏳ {r['p'] or 0}"
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
    lines = ["👥 <b>Channel owners</b> (ban with /ban ID)\n"]
    for r in rows:
        lines.append(
            f"<code>{r['user_id']}</code> | A: {escape(r['a_title'] or '-')} | B: {escape(r['b_title'] or '-')} | {r['mode']}"
        )
    if not rows:
        lines.append("No one yet.")
    await show(c.message, "\n".join(lines), kb([[("⬅️ Back", "adm:main")]]))
    await c.answer()
 
 
EDIT_PROMPTS = {
    "start": "✏️ Send the new <b>/start</b> message. Formatting (bold/italic/links) works. Send <code>default</code> to reset.",
    "help": "✏️ Send the new <b>/help</b> guide. Formatting works. Send <code>default</code> to reset.",
    "name": "🤖 Send the new bot <b>name</b> (max 64 chars).",
    "desc": "📝 Send the new bot <b>description</b> (shown on empty chat, max 512 chars).",
    "about": "💬 Send the new bot <b>about</b> text (shown on profile, max 120 chars).",
}
 
 
@router.callback_query(F.data.startswith("adm:edit:"))
async def adm_edit(c: CallbackQuery, state: FSMContext):
    if not owner_only(c):
        return await c.answer("Owner only", show_alert=True)
    key = c.data.split(":")[2]
    await state.set_state(AdminEdit.waiting)
    await state.update_data(key=key)
    await c.message.answer(EDIT_PROMPTS[key] + "\n/cancel to stop.")
    await c.answer()
 
 
@router.message(AdminEdit.waiting, F.text)
async def adm_edit_got(m: Message, state: FSMContext, bot: Bot):
    if m.from_user.id != OWNER_ID:
        return
    key = (await state.get_data()).get("key")
    plain = m.text.strip()
    try:
        if key in ("start", "help"):
            k = f"{key}_text"
            if plain.lower() == "default":
                await run("DELETE FROM settings WHERE key=?", (k,))
            else:
                await run("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (k, m.html_text))
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
    await c.message.answer("📢 Send the message you want to broadcast (text / photo / video / anything).\n/cancel to stop.")
    await c.answer()
 
 
@router.message(Broadcast.waiting)
async def bc_got(m: Message, state: FSMContext):
    if m.from_user.id != OWNER_ID:
        return
    n = (await one("SELECT COUNT(*) n FROM users WHERE banned=0"))["n"]
    await state.update_data(chat_id=m.chat.id, message_id=m.message_id)
    await state.set_state(Broadcast.confirm)
    await m.answer(
        f"Send this to <b>{n}</b> users?",
        reply_markup=kb([[("✅ Send", "bc:go"), ("❌ Cancel", "bc:no")]]),
    )
 
 
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
    await show(c.message, "⏳ Broadcasting... I'll notify you when done.")
    await c.answer()
 
 
# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------
async def main():
    if not BOT_TOKEN or not OWNER_ID:
        raise SystemExit("Set BOT_TOKEN and OWNER_ID in .env")
    await init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.message.outer_middleware(BanMiddleware())
    dp.callback_query.outer_middleware(BanMiddleware())
    dp.include_router(router)
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Start / शुरू करें"),
            BotCommand(command="help", description="Guide / गाइड"),
            BotCommand(command="settings", description="Channel settings / सेटिंग्स"),
            BotCommand(command="approve_all", description="Approve all pending / सब approve करें"),
            BotCommand(command="cancel", description="Cancel / रद्द करें"),
        ]
    )
    await bot.delete_webhook(drop_pending_updates=False)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
 
 
if __name__ == "__main__":
    asyncio.run(main())
 
