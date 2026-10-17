import asyncio
import html
import logging
import os
import random
import secrets
import sqlite3

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from aiogram.utils.deep_linking import create_start_link

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise SystemExit("Задайте токен: переменная окружения BOT_TOKEN")

bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()

# ---------- база данных ----------

db = sqlite3.connect(os.getenv("DB_PATH", "santa.db"))  # путь к базе можно задать переменной DB_PATH
db.row_factory = sqlite3.Row
db.executescript(
    """
CREATE TABLE IF NOT EXISTS games(
    code TEXT PRIMARY KEY, admin_id INTEGER, title TEXT, budget TEXT,
    drawn INTEGER DEFAULT 0, status TEXT DEFAULT 'open', rules TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS players(
    code TEXT, user_id INTEGER, name TEXT, wish TEXT DEFAULT '',
    nogift TEXT DEFAULT '', giftee_id INTEGER,
    addr_cdek TEXT DEFAULT '', addr_ozon TEXT DEFAULT '', addr_wb TEXT DEFAULT '',
    arrived INTEGER DEFAULT 0, qr_file TEXT DEFAULT '', qr_text TEXT DEFAULT '',
    gift_photo TEXT DEFAULT '',
    PRIMARY KEY(code, user_id));
CREATE TABLE IF NOT EXISTS relay(
    chat_id INTEGER, message_id INTEGER, to_user INTEGER, label TEXT,
    PRIMARY KEY(chat_id, message_id));
CREATE TABLE IF NOT EXISTS msgs(
    id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, santa_id INTEGER,
    giftee_id INTEGER, sender TEXT, text TEXT);
CREATE TABLE IF NOT EXISTS broadcasts(
    id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, text TEXT);
CREATE TABLE IF NOT EXISTS votes(
    id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, status TEXT DEFAULT 'open');
CREATE TABLE IF NOT EXISTS vote_choices(
    vote_id INTEGER, user_id INTEGER, choice INTEGER, PRIMARY KEY(vote_id, user_id));
CREATE TABLE IF NOT EXISTS screens(
    user_id INTEGER PRIMARY KEY, message_id INTEGER, kind TEXT, ref INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS extras(
    user_id INTEGER, message_id INTEGER);
CREATE TABLE IF NOT EXISTS modes(
    user_id INTEGER PRIMARY KEY, admin INTEGER DEFAULT 0, pending TEXT DEFAULT '');
"""
)


def _add_column(table: str, column: str, decl: str):
    try:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    except sqlite3.OperationalError:
        pass  # колонка уже есть


# Для баз, созданных старыми версиями бота
for _c in ("addr_cdek", "addr_ozon", "addr_wb", "nogift", "qr_file", "qr_text", "gift_photo"):
    _add_column("players", _c, "TEXT DEFAULT ''")
_add_column("players", "arrived", "INTEGER DEFAULT 0")
_add_column("games", "status", "TEXT DEFAULT 'open'")
_add_column("games", "rules", "TEXT DEFAULT ''")
db.execute("UPDATE games SET status = 'active' WHERE drawn = 1 AND (status IS NULL OR status = 'open')")
db.commit()

e = html.escape
EMPTY = "пока не указал(а)"
STATUS_TXT = {"open": "идёт регистрация", "active": "игра идёт", "paused": "⏸ игра остановлена"}

# ---------- кнопки меню ----------
# Меню внизу экрана есть у всех и всегда. Все действия делаются только через него.

B_CHAT_G = "💬 Чат с получателем"
B_CHAT_S = "💬 Чат с дарителем"
B_CARD = "🎅 Мой получатель"
B_MYGIFT = "📥 Мой подарок"
B_QR = "📤 Отправить QR"
B_ADDR = "📦 Адреса"
B_WISH = "💝 Пожелания"
B_PLAYERS = "👥 Участники"
B_NEWS = "📣 Объявления"
B_RULES = "📜 Правила"
B_EDIT = "✏️ Моя анкета"
B_ADMIN = "👑 Админ-панель"
B_HIDE = "🔽 Скрыть админ-меню"
V_YES = "✅ За перетасовку"
V_NO = "❌ Против перетасовки"

A_DRAW = "🎲 Провести жеребьёвку"
A_PAUSE = "⏸ Остановить игру"
A_RESUME = "▶️ Возобновить игру"
A_SUPP = "➕ Дополнить жеребьёвку"
A_REDRAW = "🔄 Новая жеребьёвка"
A_VOTE = "🗳 Голосование за перетасовку"
A_VOTESTOP = "🛑 Закрыть голосование"
A_BC = "📣 Сообщение всем"
A_BCDEL = "🗑 Удалить объявление"
A_RULES = "✏️ Изменить правила"
A_RULES_RESET = "♻️ Стандартные правила"
A_LINK = "🔗 Ссылка-приглашение"
A_YES = "✅ Да, подтверждаю"
A_NO = "❌ Отмена"

USER_BUTTONS = {
    B_CHAT_G, B_CHAT_S, B_CARD, B_MYGIFT, B_QR, B_ADDR, B_WISH,
    B_PLAYERS, B_NEWS, B_RULES, B_EDIT, B_ADMIN, B_HIDE, V_YES, V_NO,
}
ADMIN_BUTTONS = {
    A_DRAW, A_PAUSE, A_RESUME, A_SUPP, A_REDRAW, A_VOTE, A_VOTESTOP,
    A_BC, A_BCDEL, A_RULES, A_RULES_RESET, A_LINK, A_YES, A_NO,
}

L_SANTA = "🎅 Сообщение от твоего дарителя (Тайного Санты):"
L_GIFTEE = "🎁 Сообщение от твоего получателя:"

# поле анкеты -> (как назвать, колонка в базе, вопрос)
FIELDS = {
    "name": (
        "имя",
        "name",
        "👋 Как тебя называть? Это имя увидят все участники в списке.\n"
        "Можно просто имя, без фамилии.",
    ),
    "wish": (
        "«Что любит»",
        "wish",
        "💝 <b>Что ты любишь?</b> Хобби, вкусы, любимые сладости и цвета: "
        "подсказка для твоего Санты.",
    ),
    "nogift": (
        "«Что дарить не надо»",
        "nogift",
        "🚫 <b>Что дарить не надо?</b> Аллергии, нелюбимое, «у меня уже три кружки».",
    ),
    "cdek": (
        "адрес СДЭК",
        "addr_cdek",
        "📦 Адрес пункта выдачи <b>СДЭК</b>, куда тебе можно отправить подарок.\n"
        "Например: <i>Москва, ул. Ленина 1</i>",
    ),
    "ozon": (
        "адрес Ozon",
        "addr_ozon",
        "📦 Адрес пункта выдачи <b>Ozon</b>.\nНапример: <i>Москва, ул. Мира 5</i>",
    ),
    "wb": (
        "адрес Wildberries",
        "addr_wb",
        "📦 Адрес пункта выдачи <b>Wildberries</b>.\n"
        "Например: <i>Москва, пр. Победы 10</i>",
    ),
}
ORDER = ["name", "wish", "nogift", "cdek", "ozon", "wb"]

HELP = (
    "🎅 <b>Бот «Тайный Санта»</b>\n\n"
    "Меню внизу экрана всегда на месте, всё управление только через него. "
    "В чате всегда одно сообщение бота, оно обновляется само.\n\n"
    "💬 Чат с получателем / с дарителем — анонимная переписка: открой чат и просто пиши\n"
    "🎅 Мой получатель — карточка того, кому ты даришь\n"
    "📤 Отправить QR — Санта присылает получателю QR для выдачи подарка\n"
    "📥 Мой подарок — QR, который прислал твой Санта\n"
    "📦 Адреса · 💝 Пожелания — твои и получателя\n"
    "✏️ Моя анкета — изменить свои данные\n"
    "📜 Правила · 📣 Объявления · 👥 Участники\n\n"
    "<b>Организатору:</b> /newgame Название | бюджет — создать игру, "
    "дальше кнопка 👑 Админ-панель.\n\n"
    "Чтобы вступить в игру, перейди по ссылке от организатора. "
    "Если меню не видно, напиши /menu."
)
NO_GAME = "Сначала вступи в игру по ссылке от организатора или создай свою: /newgame"

# Получатель узнаёт, что подарок приехал (приходит вместе с QR)
ARRIVED_LINES = [
    "📬 Ура! Твой подарок доехал до пункта выдачи. Вот QR, беги за ним в шапке набекрень! 🏃",
    "🦌 Олени справились! Посылка прибыла. Держи QR и забирай подарок!",
    "📦 Подарок на месте! Тайный Санта прислал QR: покажи его в пункте выдачи 🎁",
    "🕵️ Санта доставил посылку в пункт выдачи, а его личность осталась в секрете. Вот твой QR!",
    "🎉 Подарок приехал целым и почти невредимым. Вот QR, танцуй за подарком!",
]
GIFT_WAIT_LINES = [
    "Подарка пока нет? Видимо, Санта всё ещё выбирает между «красиво» и «практично» 🎅",
    "Олени застряли в пробке, но скоро будут 🦌",
    "Посылка уже в пути. Говорят, она едет на санях, поэтому не быстро 🛷",
    "Тише-тише, подарок почти готов. Волшебство не терпит суеты ✨",
]
# Санта получает подтверждение, что QR ушёл
GIFT_DONE_LINES = [
    "QR улетел получателю! Теперь можно выдохнуть и съесть мандаринку 🍊",
    "Отправлено анонимно! Получатель уже бежит за подарком 🏃",
    "Миссия выполнена, Санта! QR у получателя, а твоя личность в тайне 🕵️",
]

DEFAULT_RULES = (
    "1. Ты Тайный Санта для одного человека. Его имя, пожелания и адреса пунктов выдачи "
    "всегда доступны по кнопке «🎅 Мой получатель».\n"
    "2. {budget}\n"
    "3. Подарок отправляй в любой пункт выдачи из списка получателя: СДЭК, Ozon или Wildberries.\n"
    "4. Тайна священна 🤫 Общайтесь только анонимно через «💬 Чат с получателем» и "
    "«💬 Чат с дарителем». Выдать себя значит лишиться мандаринок.\n"
    "5. Из списка «Что дарить не надо» ничего не дарим. Это преступление против праздника.\n"
    "6. Когда подарок приедет в пункт выдачи, Санта присылает получателю QR-код кнопкой "
    "«📤 Отправить QR». Получатель видит его в «📥 Мой подарок».\n"
    "7. Все вопросы к организатору 👑"
)


class Form(StatesGroup):
    name = State()
    wish = State()
    nogift = State()
    cdek = State()
    ozon = State()
    wb = State()


class Msg(StatesGroup):
    text = State()


class Gift(StatesGroup):
    qr = State()


class Adm(StatesGroup):
    broadcast = State()
    rules = State()
    bcdel = State()


STATE_OF = {
    "name": Form.name, "wish": Form.wish, "nogift": Form.nogift,
    "cdek": Form.cdek, "ozon": Form.ozon, "wb": Form.wb,
}
FORM_STATES = tuple(STATE_OF.values())


# ---------- режимы пользователя (админ-меню, ожидание подтверждения) ----------

def get_mode(uid: int):
    row = db.execute("SELECT admin, pending FROM modes WHERE user_id = ?", (uid,)).fetchone()
    return (row["admin"], row["pending"]) if row else (0, "")


def set_mode(uid: int, admin=None, pending=None):
    db.execute("INSERT OR IGNORE INTO modes(user_id) VALUES (?)", (uid,))
    if admin is not None:
        db.execute("UPDATE modes SET admin = ? WHERE user_id = ?", (admin, uid))
    if pending is not None:
        db.execute("UPDATE modes SET pending = ? WHERE user_id = ?", (pending, uid))
    db.commit()


# ---------- меню ----------

def open_vote_for(uid: int, code: str):
    """id голосования, если оно идёт и человек ещё не проголосовал."""
    vt = db.execute("SELECT id FROM votes WHERE code = ? AND status = 'open'", (code,)).fetchone()
    if not vt:
        return None
    if db.execute("SELECT 1 FROM vote_choices WHERE vote_id = ? AND user_id = ?",
                  (vt["id"], uid)).fetchone():
        return None
    return vt["id"]


def admin_rows(uid: int, g) -> list:
    admin_open, pending = get_mode(uid)
    if pending:
        return [[A_YES, A_NO]]
    rows = []
    if g["status"] == "open":
        rows.append([A_DRAW])
    elif g["status"] == "active":
        rows.append([A_PAUSE])
        has_vote = db.execute(
            "SELECT 1 FROM votes WHERE code = ? AND status = 'open'", (g["code"],)
        ).fetchone()
        rows.append([A_VOTESTOP] if has_vote else [A_VOTE])
    else:  # paused
        rows.append([A_RESUME])
        rows.append([A_SUPP, A_REDRAW])
    rows.append([A_BC, A_BCDEL])
    rows.append([A_RULES, A_RULES_RESET])
    rows.append([A_LINK])
    return rows


def menu_kb(uid: int) -> ReplyKeyboardMarkup:
    rows = [
        [B_CHAT_G, B_CHAT_S],
        [B_CARD, B_MYGIFT],
        [B_QR],
        [B_ADDR, B_WISH],
        [B_PLAYERS, B_NEWS],
        [B_RULES, B_EDIT],
    ]
    me = player(uid)
    if me and open_vote_for(uid, me["code"]):
        rows.append([V_YES, V_NO])
    g = admin_game(uid)
    if g:
        if get_mode(uid)[0]:
            rows.append([B_HIDE])
            rows += admin_rows(uid, g)
        else:
            rows.append([B_ADMIN])
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t) for t in r] for r in rows],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выбери действие в меню 👇",
    )


# ---------- единственное сообщение бота («экран») ----------

_tasks: set = set()


async def _delete(chat_id: int, message_id: int):
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception:
        pass  # уже удалено или прошло больше 48 часов


def delete_later(chat_id: int, message_id: int):
    task = asyncio.create_task(_delete(chat_id, message_id))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def drop(message: Message):
    """Убрать из чата сообщение пользователя."""
    delete_later(message.chat.id, message.message_id)


def _screen(uid: int):
    return db.execute(
        "SELECT message_id, kind, ref FROM screens WHERE user_id = ?", (uid,)
    ).fetchone()


async def show(uid: int, text: str, *, notify: bool = False,
               photo: str = None, extra_photo: str = None,
               remove_kb: bool = False, keep_prev: bool = False) -> int:
    """Показать пользователю экран: у него в чате всегда ОДНО сообщение бота.

    Каждый экран это новое сообщение (старое удаляется), и к нему всегда
    прикреплено актуальное меню. notify=True: сообщение приходит со звуком.
    extra_photo: дополнительное фото вторым сообщением (удалится вместе с экраном).
    """
    if len(text) > 4000:
        text = text[:3990] + "…"
    # remove_kb: меню скрыто (анкета), поле ввода свободно для текста
    kb = ReplyKeyboardRemove() if remove_kb else menu_kb(uid)
    quiet = not notify
    sent, kind = None, "text"
    if photo:
        try:
            sent = await bot.send_photo(
                uid, photo, caption=text[:1000], reply_markup=kb, disable_notification=quiet
            )
            kind = "photo"
        except Exception:
            text += "\n\n⚠️ Не удалось показать фото. Попроси Санту прислать его заново."
    if sent is None:
        sent = await bot.send_message(uid, text, reply_markup=kb, disable_notification=quiet)

    extra_ids = []
    if extra_photo:
        try:
            m = await bot.send_photo(
                uid, extra_photo, caption="🎁 Фото подарка", disable_notification=True
            )
            extra_ids.append(m.message_id)
        except Exception:
            pass

    old = _screen(uid)
    kept = None
    if old and old["message_id"] != sent.message_id:
        if keep_prev:
            kept = old["message_id"]  # останется в чате до следующего экрана
        else:
            delete_later(uid, old["message_id"])
    for r in db.execute("SELECT message_id FROM extras WHERE user_id = ?", (uid,)).fetchall():
        delete_later(uid, r["message_id"])
    db.execute("DELETE FROM extras WHERE user_id = ?", (uid,))
    for mid in extra_ids + ([kept] if kept else []):
        db.execute("INSERT INTO extras(user_id, message_id) VALUES (?,?)", (uid, mid))
    db.execute(
        "INSERT OR REPLACE INTO screens(user_id, message_id, kind, ref) VALUES (?,?,?,0)",
        (uid, sent.message_id, kind),
    )
    db.commit()
    return sent.message_id


async def info(uid: int, text: str):
    await show(uid, text)


async def notice(uid: int, text: str, **kw):
    """Сообщение от бота «со звонком»: заменяет текущий экран новым сообщением."""
    try:
        await show(uid, text, notify=True, **kw)
    except Exception:
        pass  # человек мог заблокировать бота


# ---------- работа с базой ----------

def player(user_id: int):
    """Участник в последней игре, куда он вступил (вместе с данными игры)."""
    return db.execute(
        "SELECT p.*, g.title AS title, g.budget AS budget, g.drawn AS drawn, "
        "g.status AS status, g.admin_id AS admin_id "
        "FROM players p JOIN games g ON g.code = p.code "
        "WHERE p.user_id = ? ORDER BY p.rowid DESC LIMIT 1",
        (user_id,),
    ).fetchone()


def pl(code: str, user_id: int):
    return db.execute(
        "SELECT * FROM players WHERE code = ? AND user_id = ?", (code, user_id)
    ).fetchone()


def players_of(code: str):
    return db.execute(
        "SELECT * FROM players WHERE code = ? ORDER BY rowid", (code,)
    ).fetchall()


def admin_game(user_id: int):
    return db.execute(
        "SELECT * FROM games WHERE admin_id = ? ORDER BY rowid DESC LIMIT 1", (user_id,)
    ).fetchone()


def game(code: str):
    return db.execute("SELECT * FROM games WHERE code = ?", (code,)).fetchone()


def santa_of(code: str, user_id: int):
    row = db.execute(
        "SELECT user_id FROM players WHERE code = ? AND giftee_id = ?", (code, user_id)
    ).fetchone()
    return row["user_id"] if row else None


def giftee_of(code: str, user_id: int):
    row = pl(code, user_id)
    return row["giftee_id"] if row else None


def full_name(user) -> str:
    return user.full_name or user.username or str(user.id)


def v(value) -> str:
    return e(value) if value else EMPTY


def likes_block(c) -> str:
    return f"💝 Любит: {v(c['wish'])}\n🚫 Не дарить: {v(c['nogift'])}"


def addr_block(c) -> str:
    return f"СДЭК: {v(c['addr_cdek'])}\nOzon: {v(c['addr_ozon'])}\nWildberries: {v(c['addr_wb'])}"


def log_msg(code, santa_id, giftee_id, sender, text):
    db.execute(
        "INSERT INTO msgs(code, santa_id, giftee_id, sender, text) VALUES (?,?,?,?,?)",
        (code, santa_id, giftee_id, sender, text),
    )
    db.commit()


def chat_block_reason(me):
    """Почему сейчас нельзя писать/отправлять QR (или None, если можно)."""
    if not me:
        return NO_GAME
    if me["status"] == "open" or not me["giftee_id"]:
        return "Жеребьёвка ещё не проводилась, писать пока некому."
    if me["status"] == "paused":
        return "⏸ Игра остановлена организатором. Подожди, скоро продолжим."
    return None


def _note(note: str) -> str:
    return f"{note}\n\n" if note else ""


# ---------- карточка получателя и правила ----------

def card_text(code: str, santa_uid: int, intro: str = "") -> str:
    g = game(code)
    me = pl(code, santa_uid)
    c = pl(code, me["giftee_id"])
    t = f"🎅 <b>{e(g['title'])}</b>\n"
    if intro:
        t += f"\n{intro}\n"
    t += (
        f"\nТы Тайный Санта для: <b>{e(c['name'])}</b>\n\n"
        f"{likes_block(c)}\n\n"
        f"📦 Куда отправлять подарок:\n{addr_block(c)}\n"
    )
    if g["budget"]:
        t += f"\nБюджет: {e(g['budget'])}\n"
    return t


def rules_text(g) -> str:
    if g["rules"]:
        body = e(g["rules"])
    else:
        budget = f"Бюджет подарка: {e(g['budget'])}." if g["budget"] else "Бюджет обсудите с организатором."
        body = DEFAULT_RULES.format(budget=budget)
    return f"📜 <b>Правила игры «{e(g['title'])}»</b>\n\n{body}"


def home_text(uid: int) -> str:
    me = player(uid)
    if not me:
        return "👋 Привет! Вступи в игру по ссылке от организатора или создай свою: /newgame"
    t = f"🎄 <b>{e(me['title'])}</b>\nСтатус: {STATUS_TXT[me['status']]}"
    if me["giftee_id"]:
        t += f"\n\nТы Тайный Санта для: <b>{e(pl(me['code'], me['giftee_id'])['name'])}</b>"
    if open_vote_for(uid, me["code"]):
        t += "\n\n🗳 Идёт голосование: нажми «✅ За перетасовку» или «❌ Против перетасовки» в меню."
    return t + "\n\nВсё управление кнопками внизу 👇"


async def send_cards(code: str, uids, intro: str = "", with_rules: bool = False):
    """Разослать карточки «кому ты даришь» (и правила при первой жеребьёвке)."""
    failed = []
    g = game(code)
    for uid in uids:
        text = card_text(code, uid, intro)
        if with_rules:
            full = text + "\n" + rules_text(g)
            text = full if len(full) <= 3990 else text + "\n📜 Правила: кнопка «📜 Правила»."
        try:
            await show(uid, text, notify=True)
        except Exception:
            failed.append(pl(code, uid)["name"])
    return failed


async def announce(code: str, text: str):
    for p in players_of(code):
        await notice(p["user_id"], text)


# ---------- анонимные сообщения ----------

async def relay_send(code: str, to_user: int, text: str, label: str,
                     back_user: int, back_label: str):
    """Присылает анонимное сообщение получателю как его активный экран."""
    # в каком чате получатель будет отвечать
    chat_btn = B_CHAT_S if label == L_SANTA else B_CHAT_G
    mid = await show(
        to_user,
        f"{label}\n\n{e(text)}\n\n<i>Ответить: свайп по этому сообщению "
        f"или кнопка «{chat_btn}» внизу.</i>",
        notify=True,
    )
    db.execute(
        "INSERT OR REPLACE INTO relay VALUES (?,?,?,?)", (to_user, mid, back_user, back_label)
    )
    db.commit()


# ---------- анкета ----------

async def begin_form(uid: int, state: FSMContext, intro: str = ""):
    await state.update_data(changed=[])
    await ask(uid, state, "name", intro)


async def ask(uid: int, state: FSMContext, field: str, intro: str = ""):
    """Задать вопрос анкеты. «-» оставляет текущее значение."""
    await state.set_state(STATE_OF[field])
    await state.update_data(field=field)
    me = player(uid)
    cur = me[FIELDS[field][1]] if me else ""
    prefix = f"<b>{ORDER.index(field) + 1}/{len(ORDER)}</b>  "
    text = (intro + "\n\n" if intro else "") + prefix + FIELDS[field][2]
    if cur:
        text += f"\n\nСейчас: {e(cur)}"
    text += ("\n\n<i>Напиши «-», чтобы пропустить" + (" и оставить как есть" if cur else "")
             + ". Выйти из анкеты: /menu.</i>")
    await show(uid, text, remove_kb=True)


def save_field(user_id: int, field: str, value: str) -> bool:
    """Сохранить ответ. True, если значение действительно изменилось."""
    column = FIELDS[field][1]
    me = player(user_id)
    if not me:
        return False
    value = value[:40] if field == "name" else value[:300]
    if not value or me[column] == value:
        return False
    db.execute(
        f"UPDATE players SET {column} = ? WHERE code = ? AND user_id = ?",
        (value, me["code"], user_id),
    )
    db.commit()
    return True


async def finish_form(uid: int, state: FSMContext):
    data = await state.get_data()
    changed = data.get("changed", [])
    await state.clear()
    me = player(uid)
    if me and changed:
        santa = santa_of(me["code"], uid)
        if santa and me["status"] != "open":
            await notice(
                santa,
                f"✏️ Получатель обновил анкету ({', '.join(changed)}). "
                "Свежие данные: «🎅 Мой получатель».",
            )
    text = "✅ Анкета сохранена!"
    if me and not (me["addr_cdek"] or me["addr_ozon"] or me["addr_wb"]):
        text += (
            "\n\n⚠️ Ты не указал(а) ни одного адреса, Санте будет некуда отправить "
            "подарок. Добавить можно в «✏️ Моя анкета»."
        )
    await show(uid, text + "\n\n" + home_text(uid))


async def advance(uid: int, state: FSMContext, field: str):
    i = ORDER.index(field)
    if i + 1 < len(ORDER):
        await ask(uid, state, ORDER[i + 1])
    else:
        await finish_form(uid, state)


# ---------- команды ----------

@dp.message(Command("start"))
async def start(message: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    drop(message)
    uid = message.from_user.id
    code = (command.args or "").strip()
    if not code:
        await show(uid, HELP)
        return

    g = game(code)
    if not g:
        await info(uid, "Игра не найдена. Проверь ссылку.")
        return
    if g["status"] == "active":
        await info(uid, "Жеребьёвка в этой игре уже прошла. Если хочешь вступить, "
                        "попроси организатора остановить игру и добавить тебя.")
        return

    cur = db.execute(
        "INSERT OR IGNORE INTO players(code, user_id, name) VALUES (?,?,?)",
        (code, uid, full_name(message.from_user)),
    )
    db.commit()
    if not cur.rowcount:
        await show(uid, "Ты уже участвуешь в этой игре 🙂\n\n" + home_text(uid))
        return

    intro = f"🎄 Ты в игре <b>{e(g['title'])}</b>!"
    if g["budget"]:
        intro += f"\nБюджет подарка: {e(g['budget'])}"
    if g["status"] == "paused":
        intro += "\n⏸ Игра сейчас на паузе: тебя добавят в жеребьёвку, когда организатор продолжит."
    intro += "\n\nОтветь на 6 коротких вопросов: так твой Санта узнает, что подарить и куда отправить."
    await begin_form(uid, state, intro)


@dp.message(Command("help"))
async def help_cmd(message: Message, state: FSMContext):
    await state.clear()
    drop(message)
    await show(message.from_user.id, HELP)


@dp.message(Command("menu"))
async def menu_cmd(message: Message, state: FSMContext):
    await state.clear()
    drop(message)
    uid = message.from_user.id
    await show(uid, home_text(uid))


@dp.message(Command("newgame"))
async def newgame(message: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    drop(message)
    uid = message.from_user.id
    args = command.args or "Тайный Санта"
    title, _, budget = (part.strip() for part in args.partition("|"))
    title = title or "Тайный Санта"
    code = secrets.token_urlsafe(6)

    db.execute(
        "INSERT INTO games(code, admin_id, title, budget) VALUES (?,?,?,?)",
        (code, uid, title, budget),
    )
    db.execute(
        "INSERT INTO players(code, user_id, name) VALUES (?,?,?)",
        (code, uid, full_name(message.from_user)),
    )
    db.commit()
    set_mode(uid, admin=0, pending="")

    link = await create_start_link(bot, code)
    intro = (
        f"✅ Игра <b>{e(title)}</b> создана, ты уже в ней.\n\n"
        f"Отправь эту ссылку участникам:\n{link}\n"
        "(Потом её всегда можно найти в «👑 Админ-панель».)\n\n"
        "А пока ответь на короткую анкету 👇"
    )
    await begin_form(uid, state, intro)


@dp.message(Command("admin", "draw"))
async def admin_cmd(message: Message, state: FSMContext):
    await state.clear()
    drop(message)
    await admin_panel(message.from_user.id)


# ---------- кнопки меню ----------

@dp.message(F.text.in_(USER_BUTTONS))
async def menu_buttons(message: Message, state: FSMContext):
    await state.clear()
    drop(message)
    uid, t = message.from_user.id, message.text
    set_mode(uid, pending="")
    if t != B_ADMIN:
        set_mode(uid, admin=0)  # админ-меню сворачивается, пока снова не откроют

    if t == B_CHAT_G:
        await open_chat(uid, state, "giftee")
    elif t == B_CHAT_S:
        await open_chat(uid, state, "santa")
    elif t == B_CARD:
        await view_card(uid)
    elif t == B_MYGIFT:
        await view_mygift(uid)
    elif t == B_QR:
        await ask_qr(uid, state)
    elif t == B_ADDR:
        await view_addresses(uid)
    elif t == B_WISH:
        await view_wishes(uid)
    elif t == B_PLAYERS:
        await view_players(uid)
    elif t == B_NEWS:
        await view_news(uid)
    elif t == B_RULES:
        await view_rules(uid)
    elif t == B_EDIT:
        await edit_form(uid, state)
    elif t == B_ADMIN:
        await admin_panel(uid)
    elif t == B_HIDE:
        await show(uid, home_text(uid))
    elif t in (V_YES, V_NO):
        await cast_vote(uid, 1 if t == V_YES else 0)


async def edit_form(uid: int, state: FSMContext):
    if not player(uid):
        await info(uid, NO_GAME)
        return
    await begin_form(uid, state, "✏️ <b>Моя анкета</b>: обнови данные или пропусти вопрос, написав «-».")


async def view_card(uid: int):
    me = player(uid)
    if not me:
        await info(uid, NO_GAME)
        return
    if not me["giftee_id"]:
        await info(uid, "Жеребьёвка ещё не проводилась, получателя пока нет.")
        return
    await show(uid, card_text(me["code"], uid))


async def view_rules(uid: int):
    me = player(uid)
    if not me:
        await info(uid, NO_GAME)
        return
    await show(uid, rules_text(game(me["code"])))


async def view_news(uid: int):
    me = player(uid)
    if not me:
        await info(uid, NO_GAME)
        return
    items = db.execute(
        "SELECT text FROM broadcasts WHERE code = ? ORDER BY id DESC LIMIT 5", (me["code"],)
    ).fetchall()
    if not items:
        await info(uid, "📣 Объявлений от организатора пока нет.")
        return
    body = "\n\n— — —\n\n".join(e(r["text"]) for r in items)
    await show(uid, f"📣 <b>Объявления организатора</b> (новые сверху)\n\n{body}")


async def view_wishes(uid: int):
    me = player(uid)
    if not me:
        await info(uid, NO_GAME)
        return
    text = f"💝 <b>Твои пожелания</b> ({e(me['name'])}):\n{likes_block(me)}"
    if me["giftee_id"]:
        c = pl(me["code"], me["giftee_id"])
        text += f"\n\n🎁 <b>Пожелания получателя ({e(c['name'])}):</b>\n{likes_block(c)}"
    text += "\n\n<i>Изменить свои данные: «✏️ Моя анкета».</i>"
    await show(uid, text)


async def view_addresses(uid: int):
    me = player(uid)
    if not me:
        await info(uid, NO_GAME)
        return
    text = f"📦 <b>Твои адреса:</b>\n{addr_block(me)}"
    if me["giftee_id"]:
        c = pl(me["code"], me["giftee_id"])
        text += f"\n\n🎁 <b>Адреса получателя ({e(c['name'])}):</b>\n{addr_block(c)}"
    text += "\n\n<i>Изменить свои данные: «✏️ Моя анкета».</i>"
    await show(uid, text)


async def view_players(uid: int):
    me = player(uid)
    if not me:
        await info(uid, NO_GAME)
        return
    rows = players_of(me["code"])
    lines = [
        f"{i}. {e(r['name'])}  {'💝' if r['wish'] else '▫️'}"
        f"{'📦' if (r['addr_cdek'] or r['addr_ozon'] or r['addr_wb']) else '▫️'}"
        for i, r in enumerate(rows, 1)
    ]
    text = (
        f"👥 Участники игры <b>{e(me['title'])}</b> ({len(rows)}):\n"
        + "\n".join(lines)
        + "\n\n💝 — написал(а) пожелания\n📦 — указал(а) адрес"
        + f"\n\nСтатус: {STATUS_TXT[me['status']]}"
    )
    if open_vote_for(uid, me["code"]):
        text += ("\n\n🗳 <b>Идёт голосование:</b> перетасовать пары заново? "
                 "Ответь кнопками «✅ За перетасовку» / «❌ Против перетасовки» в меню.")
    await show(uid, text)


# ---------- чаты ----------

def chat_pages(code: str, role: str, uid: int):
    """Переписка, разбитая на страницы по ~3300 символов."""
    me = pl(code, uid)
    if role == "giftee":
        santa_id, giftee_id, mine, other = uid, me["giftee_id"], "santa", "Получатель"
        title = f"🎁 <b>Чат с получателем ({e(pl(code, giftee_id)['name'])})</b>"
    else:
        santa_id, giftee_id, mine, other = santa_of(code, uid), uid, "giftee", "Даритель"
        title = "🎅 <b>Чат с дарителем (Тайным Сантой)</b>"
    rows = db.execute(
        "SELECT sender, text FROM msgs WHERE code = ? AND santa_id = ? AND giftee_id = ? ORDER BY id",
        (code, santa_id, giftee_id),
    ).fetchall()
    lines = []
    for r in rows:
        t = r["text"] if len(r["text"]) <= 500 else r["text"][:500] + "…"
        lines.append(f"<b>{'Ты' if r['sender'] == mine else other}:</b> {e(t)}")
    if not lines:
        lines = ["<i>Пока пусто. Напиши первым!</i>"]
    pages, cur = [], ""
    for line in lines:
        if len(cur) + len(line) + 2 > 3300:
            pages.append(cur)
            cur = ""
        cur += line + "\n\n"
    pages.append(cur)
    return title, pages


async def open_chat(uid: int, state: FSMContext, role: str, note: str = ""):
    """role='giftee': чат с тем, кому я дарю; role='santa': чат с тем, кто дарит мне.

    Пока чат открыт, всё, что человек пишет, уходит собеседнику анонимно.
    """
    me = player(uid)
    if not me or not me["giftee_id"] or (role == "santa" and not santa_of(me["code"], uid)):
        await info(uid, "Жеребьёвка ещё не проводилась, переписки пока нет.")
        return
    title, pages = chat_pages(me["code"], role, uid)
    head = title + ("\n<i>(показаны последние сообщения)</i>" if len(pages) > 1 else "")
    reason = chat_block_reason(me)
    if reason:
        tail = reason
    else:
        await state.set_state(Msg.text)
        await state.update_data(to=role)
        who = "получателю" if role == "giftee" else "дарителю"
        tail = f"✍️ Просто напиши сообщение: оно уйдёт {who} анонимно."
    await show(uid, _note(note) + head + "\n\n" + pages[-1] + tail)


# ---------- подарок: QR и фото от Санты к получателю ----------

async def view_mygift(uid: int):
    """Получатель смотрит, что прислал его Санта."""
    me = player(uid)
    if not me:
        await info(uid, NO_GAME)
        return
    if me["status"] == "open" or not santa_of(me["code"], uid):
        await info(uid, "Жеребьёвки ещё не было, так что и подарок пока не едет 😉")
        return
    await show_gift(uid)


async def show_gift(uid: int, intro: str = "", notify: bool = False):
    me = player(uid)
    head = _note(intro)
    if me["arrived"]:
        text = head + "📬 <b>QR для получения подарка</b>\nПокажи его в пункте выдачи."
        if me["qr_file"]:
            await show(uid, text, notify=notify, photo=me["qr_file"])
        else:
            await show(uid, text + f"\n\n<code>{e(me['qr_text'])}</code>", notify=notify)
    else:
        await show(
            uid,
            head + "📥 <b>Мой подарок</b>\n\n" + random.choice(GIFT_WAIT_LINES) + "\n\n"
            "Когда посылка приедет в пункт выдачи, твой Тайный Санта пришлёт сюда QR-код "
            "для получения.",
            notify=notify,
        )


def _giftee_row(me):
    return pl(me["code"], me["giftee_id"])


async def ask_qr(uid: int, state: FSMContext):
    me = player(uid)
    reason = chat_block_reason(me)
    if reason:
        await info(uid, reason)
        return
    c = _giftee_row(me)
    await state.set_state(Gift.qr)
    text = (
        f"📤 <b>QR-код для получателя ({e(c['name'])})</b>\n\n"
        "Когда подарок приедет в пункт выдачи, пришли сюда QR-код для получения: "
        "фото или текст (код/ссылка). Он уйдёт получателю анонимно, от Тайного Санты."
    )
    if c["arrived"]:
        text += "\n\n✅ QR уже отправлен. Новый заменит прежний."
    await show(uid, text)


async def save_qr(uid: int, state: FSMContext, file_id: str, text: str):
    await state.clear()
    me = player(uid)
    reason = chat_block_reason(me)
    if reason:
        await info(uid, reason)
        return
    c = _giftee_row(me)
    was = c["arrived"]
    db.execute(
        "UPDATE players SET arrived = 1, qr_file = ?, qr_text = ? WHERE code = ? AND user_id = ?",
        (file_id, text, me["code"], c["user_id"]),
    )
    db.commit()
    intro = (random.choice(ARRIVED_LINES) if not was
             else "🔄 Тайный Санта прислал новый QR-код для получения подарка.")
    delivered = True
    try:
        await show_gift(c["user_id"], intro, notify=True)
    except Exception:
        delivered = False
    note = "✅ QR отправлен получателю. " + random.choice(GIFT_DONE_LINES)
    if not delivered:
        note += "\n⚠️ Получатель ещё не открывал бота: он увидит QR в «📥 Мой подарок»."
    await show(uid, note)


# ---------- голосование ----------

async def start_vote(code: str):
    g = game(code)
    cur = db.execute("INSERT INTO votes(code) VALUES (?)", (code,))
    db.commit()
    for p in players_of(code):
        await notice(
            p["user_id"],
            f"🗳 <b>Голосование «{e(g['title'])}»</b>\n\n"
            "Перетасовать пары заново? Если больше половины участников «за», "
            "все получат новых получателей.\n\n"
            "Ответь кнопками «✅ За перетасовку» / «❌ Против перетасовки» в меню.",
        )
    return cur.lastrowid


def close_vote(vid: int, status: str):
    db.execute("UPDATE votes SET status = ? WHERE id = ?", (status, vid))
    db.commit()


async def cast_vote(uid: int, choice: int):
    me = player(uid)
    if not me:
        await info(uid, NO_GAME)
        return
    code = me["code"]
    vt = db.execute("SELECT id FROM votes WHERE code = ? AND status = 'open'", (code,)).fetchone()
    if not vt:
        await info(uid, "Сейчас голосования нет.")
        return
    vid = vt["id"]
    db.execute("INSERT OR REPLACE INTO vote_choices VALUES (?,?,?)", (vid, uid, choice))
    db.commit()
    total = len(players_of(code))
    yes = db.execute("SELECT COUNT(*) FROM vote_choices WHERE vote_id = ? AND choice = 1", (vid,)).fetchone()[0]
    no = db.execute("SELECT COUNT(*) FROM vote_choices WHERE vote_id = ? AND choice = 0", (vid,)).fetchone()[0]
    if yes * 2 > total:
        close_vote(vid, "passed")
        failed = await do_full_draw(code, f"🗳 Голосование прошло ({yes} из {total} «за»): пары перетасованы!")
        if failed:
            await notice(game(code)["admin_id"], "Не удалось отправить карточки: " + ", ".join(map(e, failed)))
    elif no * 2 >= total:
        close_vote(vid, "failed")
        await announce(code, f"🗳 Голосование не прошло ({no} из {total} «против»). Пары остаются как были.")
    else:
        await show(uid, f"🗳 Голос учтён.\nЗа: {yes} · против: {no} · всего участников: {total}\n"
                        "Когда решение будет принято, вы получите сообщение.")


# ---------- админ-панель ----------

async def admin_panel(uid: int, note: str = "", keep: bool = False):
    g = admin_game(uid)
    if not g:
        await info(uid, "Админ-панель есть только у организатора. Создай игру: /newgame")
        return
    set_mode(uid, admin=1)
    code, status = g["code"], g["status"]
    rows = players_of(code)
    unassigned = sum(1 for r in rows if not r["giftee_id"])
    text = (
        _note(note) + f"👑 <b>Админ-панель</b>\nИгра: <b>{e(g['title'])}</b>\n"
        f"Статус: {STATUS_TXT[status]}\nУчастников: {len(rows)}"
    )
    if status != "open" and unassigned:
        text += f"\nБез получателя: {unassigned}. Дополни жеребьёвку или проведи заново."
    if status == "paused":
        text += (
            "\n\n➕ Дополнить: старые пары сохраняются, новичков вставят в круг.\n"
            "🔄 Новая: все пары перемешиваются заново."
        )
    text += "\n\nДействия организатора в меню внизу 👇"
    await show(uid, text, keep_prev=keep)


async def confirm(uid: int, pending: str, text: str):
    set_mode(uid, pending=pending)
    await show(uid, text + "\n\nПодтверди кнопкой «✅ Да, подтверждаю» или нажми «❌ Отмена».")


def assign_cycle(code: str, ids: list):
    """Случайный круг: каждый дарит следующему, сам себе никто не достанется."""
    ids = list(ids)
    random.shuffle(ids)
    for i, uid in enumerate(ids):
        db.execute(
            "UPDATE players SET giftee_id = ? WHERE code = ? AND user_id = ?",
            (ids[(i + 1) % len(ids)], code, uid),
        )
    db.commit()


def reset_pairs(code: str, uids, clear_gift: bool):
    """Забыть старые ответы по свайпу; при смене Санты сбросить QR подарка."""
    for uid in uids:
        db.execute("DELETE FROM relay WHERE chat_id = ?", (uid,))
        if clear_gift:
            db.execute(
                "UPDATE players SET arrived = 0, qr_file = '', qr_text = '', gift_photo = '' "
                "WHERE code = ? AND user_id = ?",
                (code, uid),
            )
    db.commit()


async def do_full_draw(code: str, intro: str = ""):
    g = game(code)
    first = not g["drawn"]
    ids = [r["user_id"] for r in players_of(code)]
    assign_cycle(code, ids)
    reset_pairs(code, ids, clear_gift=True)
    db.execute("UPDATE games SET status = 'active', drawn = 1 WHERE code = ?", (code,))
    db.commit()
    if first:
        return await send_cards(code, ids, with_rules=True)
    return await send_cards(code, ids, intro or "🔄 Пары перетасованы: у тебя новый получатель!")


async def do_supplement(code: str):
    """Добавить новичков в уже проведённую жеребьёвку, меняя как можно меньше пар."""
    rows = players_of(code)
    new = [r["user_id"] for r in rows if not r["giftee_id"]]
    old = [r["user_id"] for r in rows if r["giftee_id"]]
    if not new:
        return "Новых участников нет, всё уже распределено."
    santas, giftees = set(), set()
    if len(new) >= 3 or not old:
        if len(new) < 3:
            return "Нужно минимум 3 участника."
        assign_cycle(code, new)  # новички играют отдельным кругом, старые пары не трогаем
    else:
        for n in new:
            a = random.choice(old)
            b = giftee_of(code, a)
            db.execute("UPDATE players SET giftee_id = ? WHERE code = ? AND user_id = ?", (n, code, a))
            db.execute("UPDATE players SET giftee_id = ? WHERE code = ? AND user_id = ?", (b, code, n))
            santas.add(a)
            giftees.add(b)
            old.append(n)
        db.commit()
    reset_pairs(code, giftees, clear_gift=True)
    reset_pairs(code, santas, clear_gift=False)
    await send_cards(code, new)
    await send_cards(code, santas, "🔁 К игре присоединились новые участники, твой получатель изменился!")
    for uid in giftees:
        await notice(uid, "🔁 У тебя новый Тайный Санта: к игре присоединились новые участники 😉")
    return (
        f"✅ Добавлено новых участников: {len(new)}. "
        f"Изменились пары у {len(santas)} Сант. Теперь нажми «▶️ Возобновить игру»."
    )


@dp.message(F.text.in_(ADMIN_BUTTONS))
async def admin_buttons(message: Message, state: FSMContext):
    await state.clear()
    drop(message)
    uid, t = message.from_user.id, message.text
    g = admin_game(uid)
    if not g:
        await info(uid, "Это действие доступно только организатору.")
        return
    code = g["code"]
    rows = players_of(code)
    pending = get_mode(uid)[1]
    set_mode(uid, admin=1, pending="")

    if t == A_NO:
        await admin_panel(uid, "Отменено.")

    elif t == A_YES:
        if pending == "draw":
            if g["status"] != "open" or len(rows) < 3:
                await admin_panel(uid, "Жеребьёвку сейчас провести нельзя.")
                return
            failed = await do_full_draw(code)
            note = "🎉 Жеребьёвка проведена! Всем отправлены карточки и правила."
            if failed:
                note += "\nНе удалось отправить: " + ", ".join(map(e, failed)) + ". Пусть откроют бота и нажмут /start."
            await admin_panel(uid, note, keep=True)
        elif pending == "supp":
            if g["status"] != "paused":
                await admin_panel(uid, "Сначала останови игру.")
                return
            await admin_panel(uid, await do_supplement(code), keep=True)
        elif pending == "redraw":
            if g["status"] != "paused" or len(rows) < 3:
                await admin_panel(uid, "Для новой жеребьёвки останови игру (нужно минимум 3 участника).")
                return
            failed = await do_full_draw(code)
            await admin_panel(uid, "🔄 Новая жеребьёвка проведена, игра продолжается."
                              + (("\nНе удалось отправить: " + ", ".join(map(e, failed))) if failed else ""),
                              keep=True)
        else:
            await admin_panel(uid)

    elif t == A_DRAW:
        if g["status"] != "open":
            await admin_panel(uid, "Жеребьёвка уже проведена.")
            return
        if len(rows) < 3:
            await admin_panel(uid, f"Пока в игре {len(rows)} чел., нужно минимум 3.")
            return
        lazy = [r["name"] for r in rows if not (r["wish"] and (r["addr_cdek"] or r["addr_ozon"] or r["addr_wb"]))]
        text = f"🎲 Провести жеребьёвку? Участников: {len(rows)}."
        if lazy:
            text += "\nНе заполнили анкету: " + ", ".join(map(e, lazy)) + "."
        text += "\n\nДальше новые участники смогут вступить, только если остановить игру."
        await confirm(uid, "draw", text)

    elif t == A_PAUSE:
        if g["status"] != "active":
            await admin_panel(uid, "Остановить можно только идущую игру.")
            return
        db.execute("UPDATE games SET status = 'paused' WHERE code = ?", (code,))
        db.commit()
        await announce(code, "⏸ Организатор остановил игру. Новые участники смогут вступить, "
                             "а потом жеребьёвку дополнят или проведут заново.")
        await admin_panel(uid, "⏸ Игра остановлена. Новые участники теперь могут вступать по ссылке.", keep=True)

    elif t == A_RESUME:
        if g["status"] != "paused":
            await admin_panel(uid, "Игра и так идёт.")
            return
        if any(not r["giftee_id"] for r in rows):
            await admin_panel(uid, "Есть участники без получателя. Сначала «➕ Дополнить жеребьёвку» или «🔄 Новая жеребьёвка».")
            return
        db.execute("UPDATE games SET status = 'active' WHERE code = ?", (code,))
        db.commit()
        await announce(code, "▶️ Игра продолжается! Можно снова писать друг другу и дарить подарки 🎁")
        await admin_panel(uid, "▶️ Игра возобновлена.", keep=True)

    elif t == A_SUPP:
        if g["status"] != "paused":
            await admin_panel(uid, "Сначала останови игру.")
            return
        n_new = sum(1 for r in rows if not r["giftee_id"])
        await confirm(uid, "supp", f"➕ Дополнить жеребьёвку? Новых участников: {n_new}. "
                                   "Старые пары сохранятся, насколько это возможно.")

    elif t == A_REDRAW:
        if g["status"] != "paused" or len(rows) < 3:
            await admin_panel(uid, "Для новой жеребьёвки останови игру (нужно минимум 3 участника).")
            return
        await confirm(uid, "redraw", "🔄 Провести жеребьёвку заново? ВСЕ пары изменятся, "
                                     "QR подарков сбросятся.")

    elif t == A_VOTE:
        if g["status"] != "active":
            await admin_panel(uid, "Голосование доступно, когда игра идёт.")
            return
        if db.execute("SELECT 1 FROM votes WHERE code = ? AND status = 'open'", (code,)).fetchone():
            await admin_panel(uid, "Голосование уже идёт.")
            return
        await start_vote(code)
        await admin_panel(uid, "🗳 Голосование запущено: у участников появились кнопки «За» и «Против».", keep=True)

    elif t == A_VOTESTOP:
        vt = db.execute("SELECT id FROM votes WHERE code = ? AND status = 'open'", (code,)).fetchone()
        if vt:
            close_vote(vt["id"], "closed")
            await announce(code, "🛑 Организатор закрыл голосование. Пары остаются как были.")
        await admin_panel(uid, "🛑 Голосование закрыто.", keep=bool(vt))

    elif t == A_BC:
        await state.set_state(Adm.broadcast)
        await show(uid, "📣 Напиши объявление для всех участников. Оно придёт каждому, "
                        "а потом останется в «📣 Объявления».\n\n"
                        "<i>Чтобы отменить, нажми любую кнопку меню.</i>")

    elif t == A_BCDEL:
        items = db.execute(
            "SELECT id, text FROM broadcasts WHERE code = ? ORDER BY id DESC LIMIT 5", (code,)
        ).fetchall()
        if not items:
            await admin_panel(uid, "Объявлений пока нет.")
            return
        await state.set_state(Adm.bcdel)
        await state.update_data(ids=[r["id"] for r in items])
        lines = [f"{i}. {e(r['text'][:60])}" for i, r in enumerate(items, 1)]
        await show(uid, "🗑 Какое объявление удалить? Пришли его номер. Оно пропадёт из раздела "
                        "«📣 Объявления» у всех.\n\n" + "\n".join(lines)
                   + "\n\n<i>Чтобы отменить, нажми любую кнопку меню.</i>")

    elif t == A_RULES:
        await state.set_state(Adm.rules)
        await show(uid, rules_text(g) + "\n\n✏️ Пришли новый текст правил одним сообщением.\n"
                                        "<i>Чтобы отменить, нажми любую кнопку меню.</i>")

    elif t == A_RULES_RESET:
        db.execute("UPDATE games SET rules = '' WHERE code = ?", (code,))
        db.commit()
        if g["drawn"]:
            await announce(code, rules_text(game(code)))
        await admin_panel(uid, "♻️ Вернул стандартные правила.", keep=bool(g["drawn"]))

    elif t == A_LINK:
        link = await create_start_link(bot, code)
        await show(uid, f"🔗 Ссылка-приглашение в игру <b>{e(g['title'])}</b>:\n{link}")


# ---------- старые инлайн-кнопки (от прошлых версий бота) ----------

@dp.callback_query()
async def legacy_callbacks(cb: CallbackQuery):
    await cb.answer("Бот обновился: теперь всё управление кнопками меню внизу 👇")
    uid = cb.from_user.id
    try:
        await cb.message.delete()
    except Exception:
        pass
    await show(uid, home_text(uid))


# ---------- ответы пользователя ----------

@dp.message(StateFilter(*FORM_STATES), F.text, ~F.text.startswith("/"))
async def form_answer(message: Message, state: FSMContext):
    data = await state.get_data()
    field = data["field"]
    uid = message.from_user.id
    drop(message)  # ответы не должны висеть в чате
    text = message.text.strip()
    if text not in ("-", "—", "–"):
        if save_field(uid, field, text):
            changed = data.get("changed", [])
            changed.append(FIELDS[field][0])
            await state.update_data(changed=changed)
    await advance(uid, state, field)


@dp.message(StateFilter(Msg.text), F.text, ~F.text.startswith("/"))
async def msg_text(message: Message, state: FSMContext):
    data = await state.get_data()
    to = data["to"]
    drop(message)  # анонимный текст не остаётся в чате отправителя

    uid = message.from_user.id
    me = player(uid)
    reason = chat_block_reason(me)
    if reason:
        await state.clear()
        await info(uid, reason)
        return
    code = me["code"]
    if to == "giftee":
        target, label, back_label = me["giftee_id"], L_SANTA, L_GIFTEE
    else:
        target, label, back_label = santa_of(code, uid), L_GIFTEE, L_SANTA
    if not target:
        await state.clear()
        await info(uid, "Получатель не найден.")
        return
    try:
        await relay_send(code, target, message.text, label, uid, back_label)
    except Exception:
        await open_chat(uid, state, to, note="⚠️ Не удалось доставить: человек ещё не открывал бота.")
        return
    if to == "giftee":
        log_msg(code, uid, target, "santa", message.text)
    else:
        log_msg(code, target, uid, "giftee", message.text)
    await open_chat(uid, state, to, note="✅ Отправлено анонимно")


@dp.message(StateFilter(Gift.qr), F.photo)
async def qr_photo(message: Message, state: FSMContext):
    drop(message)
    await save_qr(message.from_user.id, state, message.photo[-1].file_id, "")


@dp.message(StateFilter(Gift.qr), F.text, ~F.text.startswith("/"))
async def qr_text(message: Message, state: FSMContext):
    drop(message)
    await save_qr(message.from_user.id, state, "", message.text.strip())


@dp.message(StateFilter(Adm.broadcast), F.text, ~F.text.startswith("/"))
async def adm_broadcast(message: Message, state: FSMContext):
    await state.clear()
    drop(message)
    uid = message.from_user.id
    g = admin_game(uid)
    if not g:
        return
    code = g["code"]
    db.execute("INSERT INTO broadcasts(code, text) VALUES (?,?)", (code, message.text))
    db.commit()
    for p in players_of(code):
        await notice(p["user_id"], f"📣 <b>Сообщение от организатора</b>\n\n{e(message.text)}")
    await admin_panel(uid, f"📣 Объявление отправлено всем участникам: {len(players_of(code))} (включая тебя).",
                      keep=True)


@dp.message(StateFilter(Adm.rules), F.text, ~F.text.startswith("/"))
async def adm_rules(message: Message, state: FSMContext):
    await state.clear()
    drop(message)
    uid = message.from_user.id
    g = admin_game(uid)
    if not g:
        return
    db.execute("UPDATE games SET rules = ? WHERE code = ?", (message.text.strip()[:3000], g["code"]))
    db.commit()
    if g["drawn"]:
        for p in players_of(g["code"]):
            await notice(p["user_id"], "📜 Правила обновлены:\n\n" + rules_text(game(g["code"])))
    await admin_panel(uid, "📜 Правила обновлены.", keep=bool(g["drawn"]))


@dp.message(StateFilter(Adm.bcdel), F.text, ~F.text.startswith("/"))
async def adm_bcdel(message: Message, state: FSMContext):
    data = await state.get_data()
    ids = data.get("ids", [])
    drop(message)
    uid = message.from_user.id
    g = admin_game(uid)
    txt = message.text.strip()
    if not g:
        await state.clear()
        return
    if not txt.isdigit() or not (1 <= int(txt) <= len(ids)):
        await show(uid, f"Нужно число от 1 до {len(ids)}. Или нажми любую кнопку меню, чтобы отменить.")
        return
    await state.clear()
    db.execute("DELETE FROM broadcasts WHERE id = ? AND code = ?", (ids[int(txt) - 1], g["code"]))
    db.commit()
    await admin_panel(uid, "🗑 Объявление удалено.")


# Ответ (reply) на анонимное сообщение пересылается дальше
@dp.message(F.reply_to_message, F.text, ~F.text.startswith("/"))
async def on_reply(message: Message, state: FSMContext):
    drop(message)
    row = db.execute(
        "SELECT to_user, label FROM relay WHERE chat_id = ? AND message_id = ?",
        (message.chat.id, message.reply_to_message.message_id),
    ).fetchone()
    if not row:
        return
    uid = message.from_user.id
    me = player(uid)
    reason = chat_block_reason(me)
    if reason:
        await info(uid, reason)
        return
    to_user, label = row["to_user"], row["label"]
    opposite = L_GIFTEE if label == L_SANTA else L_SANTA
    role = "giftee" if label == L_SANTA else "santa"  # в каком чате отвечает человек
    try:
        await relay_send(me["code"], to_user, message.text, label, uid, opposite)
    except Exception:
        await info(uid, "Не удалось доставить сообщение.")
        return
    if label == L_SANTA:  # отвечает Санта получателю
        log_msg(me["code"], uid, to_user, "santa", message.text)
    else:  # отвечает получатель Санте
        log_msg(me["code"], to_user, uid, "giftee", message.text)
    await open_chat(uid, state, role, note="✅ Отправлено анонимно")


# Всё остальное: лишние сообщения убираем, ориентируем на меню
@dp.message()
async def fallback(message: Message, state: FSMContext):
    drop(message)
    if await state.get_state() is None:
        uid = message.from_user.id
        await show(uid, "Пользуйся кнопками меню внизу 👇\n\n" + home_text(uid))


async def main():
    logging.basicConfig(level=logging.INFO)
    await bot.set_my_commands(
        [
            BotCommand(command="menu", description="Показать меню"),
            BotCommand(command="newgame", description="Создать игру"),
            BotCommand(command="admin", description="Админ-панель"),
            BotCommand(command="help", description="Справка"),
        ]
    )
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
