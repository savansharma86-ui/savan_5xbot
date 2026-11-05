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
