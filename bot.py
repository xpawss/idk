"""Псевдо-казино бот для Telegram (виртуальные монеты, без реальных денег).

Запуск:
    export BOT_TOKEN="123:ABC..."
    python bot.py
"""
import asyncio
import html
import logging
import os
import sqlite3
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyParameters,
)

from logic import (
    HAND_NAMES,
    PAYTABLE,
    RNG,
    card_str,
    draw_hands,
    evaluate,
    fmt,
    is_all,
    new_hand,
    parse_amount,
    parse_command,
    parse_roulette_choice,
    plural,
    roulette_color,
    roulette_label,
    roulette_mult,
    slot_result,
    spin_roulette,
)

# ============================== Настройки ==============================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
DB_PATH = os.getenv("DB_PATH", os.path.join(BASE_DIR, "casino.db"))
DEALER_IMAGE = os.path.join(BASE_DIR, "dealer.jpg")   # картинка крупье

CURRENCY = "🪙"
START_BALANCE = 1000
DAILY_BONUS = 500
DAILY_COOLDOWN = 24 * 3600
MIN_BET = 10
MAX_BET = 100_000          # максимум на одну ставку / одну руку
GAME_COOLDOWN = 3          # секунд между играми одного игрока

VP_MAX_HANDS = 100         # максимум рук в видеопокере
VP_TIMEOUT = 120           # через сколько секунд бездействия карты раздаются сами
ROUND_SECONDS = 20         # сколько секунд открыт стол общей рулетки
ROULETTE_MAX_BETS = 5      # максимум ставок одного игрока за раунд
DUEL_FEE = 0.05            # комиссия казино с банка дуэли
DUEL_TIMEOUT = 120

# Страны: код -> (флаг, название). Хочешь добавить ещё — просто допиши строку.
COUNTRIES = {
    "KZ": ("🇰🇿", "Казахстан"),
    "RU": ("🇷🇺", "Россия"),
    "UA": ("🇺🇦", "Украина"),
    "BY": ("🇧🇾", "Беларусь"),
    "UZ": ("🇺🇿", "Узбекистан"),
    "KG": ("🇰🇬", "Кыргызстан"),
    "TJ": ("🇹🇯", "Таджикистан"),
    "AZ": ("🇦🇿", "Азербайджан"),
    "AM": ("🇦🇲", "Армения"),
    "GE": ("🇬🇪", "Грузия"),
    "MD": ("🇲🇩", "Молдова"),
    "PL": ("🇵🇱", "Польша"),
    "DE": ("🇩🇪", "Германия"),
    "TR": ("🇹🇷", "Турция"),
    "US": ("🇺🇸", "США"),
}

BOT_USERNAME = ""

# ============================== База данных ==============================
db = sqlite3.connect(DB_PATH, isolation_level=None)  # autocommit

# Старая версия хранила баланс отдельно по чатам — откладываем её таблицу в бэкап
_cols = {r[1] for r in db.execute("PRAGMA table_info(users)")}
if "chat_id" in _cols:
    db.execute("ALTER TABLE users RENAME TO users_v1_backup")

db.executescript(
    """
    CREATE TABLE IF NOT EXISTS users (
        user_id    INTEGER PRIMARY KEY,
        name       TEXT NOT NULL DEFAULT '',
        balance    INTEGER NOT NULL,
        last_daily REAL NOT NULL DEFAULT 0,
        country    TEXT,
        games      INTEGER NOT NULL DEFAULT 0,
        wins       INTEGER NOT NULL DEFAULT 0,
        best_win   INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS chat_members (
        chat_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        PRIMARY KEY (chat_id, user_id)
    );
    -- ставки, которые "заморожены" в незавершённых играх (на случай перезапуска бота)
    CREATE TABLE IF NOT EXISTS holds (
        key     TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        amount  INTEGER NOT NULL
    );
    """
)


def touch(user_id: int, name: str) -> None:
    db.execute(
        "INSERT OR IGNORE INTO users (user_id, name, balance) VALUES (?, ?, ?)",
        (user_id, name, START_BALANCE),
    )
    db.execute("UPDATE users SET name = ? WHERE user_id = ?", (name, user_id))


def get_balance(user_id: int) -> int:
    row = db.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,)).fetchone()
    return row[0] if row else 0


def add_balance(user_id: int, delta: int) -> None:
    db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (delta, user_id))


def record(user_id: int, profit: int) -> None:
    """Записывает результат игры в статистику профиля."""
    db.execute(
        "UPDATE users SET games = games + 1, wins = wins + ?, best_win = MAX(best_win, ?) WHERE user_id = ?",
        (1 if profit > 0 else 0, max(profit, 0), user_id),
    )


def hold(key: str, user_id: int, amount: int) -> None:
    """Списывает ставку и запоминает её, пока игра не завершена."""
    add_balance(user_id, -amount)
    db.execute("INSERT OR REPLACE INTO holds (key, user_id, amount) VALUES (?, ?, ?)", (key, user_id, amount))


def release(key: str) -> None:
    """Игра завершена, ставка 'сгорела' или выплачена отдельно."""
    db.execute("DELETE FROM holds WHERE key = ?", (key,))


def refund(key: str) -> None:
    row = db.execute("SELECT user_id, amount FROM holds WHERE key = ?", (key,)).fetchone()
    if row:
        add_balance(row[0], row[1])
        release(key)


def refund_all() -> int:
    rows = db.execute("SELECT key FROM holds").fetchall()
    for (key,) in rows:
        refund(key)
    return len(rows)


# ============================== Утилиты ==============================
router = Router()
tasks: set[asyncio.Task] = set()


def spawn(coro) -> None:
    t = asyncio.create_task(coro)
    tasks.add(t)
    t.add_done_callback(tasks.discard)


def esc(s: str) -> str:
    return html.escape(s)


def uname(u) -> str:
    return u.full_name or u.username or str(u.id)


def me(m: Message) -> int:
    """Регистрирует игрока (и запоминает, что он играет в этом чате). Возвращает user_id."""
    u = m.from_user
    touch(u.id, uname(u))
    if m.chat.type != "private":
        db.execute("INSERT OR IGNORE INTO chat_members VALUES (?, ?)", (m.chat.id, u.id))
    return u.id


def rp(message_id: int) -> ReplyParameters:
    return ReplyParameters(message_id=message_id, allow_sending_without_reply=True)


def first_word(args: str) -> str | None:
    parts = args.split()
    return parts[0] if parts else None


def cmd(*names: str):
    """Фильтр команд: работает и с '/', и с '!', в том числе с русскими названиями."""
    wanted = set(names)

    async def _filter(message: Message):
        if not message.text or message.from_user is None:
            return False
        parsed = parse_command(message.text)
        if parsed and parsed[0] in wanted:
            return {"cmdname": parsed[0], "args": parsed[1]}
        return False

    return _filter


last_game: dict[int, float] = {}


async def take_bet(m: Message, raw: str | None, *, units: int = 1, key: str | None = None,
                   cooldown: bool = True) -> int | None:
    """Проверяет ставку и сразу списывает bet * units. Возвращает ставку (на одну единицу)."""
    uid = me(m)
    if not raw:
        await m.reply("Укажи ставку, например: <code>100</code>, <code>5к</code> или <code>all</code>")
        return None

    balance = get_balance(uid)
    if is_all(raw):
        bet = min(balance // units, MAX_BET)
    else:
        bet = parse_amount(raw)
        if bet is None:
            await m.reply("Ставка — число (<code>100</code>, <code>5к</code>, <code>1.5к</code>) или <code>all</code>.")
            return None

    if bet < MIN_BET:
        await m.reply(f"Минимальная ставка — {MIN_BET} {CURRENCY}.")
        return None
    if bet > MAX_BET:
        await m.reply(f"Максимальная ставка — {fmt(MAX_BET)} {CURRENCY}.")
        return None
    total = bet * units
    if total > balance:
        await m.reply(f"Не хватает монет. Нужно {fmt(total)} {CURRENCY}, у тебя {fmt(balance)} {CURRENCY}")
        return None

    if cooldown:
        now = time.time()
        if now - last_game.get(uid, 0) < GAME_COOLDOWN:
            await m.reply("Не так быстро, подожди пару секунд ⏳")
            return None
        last_game[uid] = now

    if key:
        hold(key, uid, total)
    else:
        add_balance(uid, -total)
    return bet


async def finish(m: Message, bet: int, mult: float, title: str) -> None:
    """Выплата для быстрых игр (mult — итоговый множитель ставки)."""
    uid = m.from_user.id
    win = int(bet * mult)
    if win:
        add_balance(uid, win)
    profit = win - bet
    record(uid, profit)
    if profit > 0:
        res = f"✅ Выигрыш: <b>+{fmt(profit)}</b> {CURRENCY}"
    elif profit == 0:
        res = "🤝 Ставка возвращена"
    else:
        res = f"❌ Проигрыш: <b>-{fmt(-profit)}</b> {CURRENCY}"
    await m.reply(f"{title}\n{res}\nБаланс: {fmt(get_balance(uid))} {CURRENCY}")


# ---- картинка крупье ----
dealer_file_id: str | None = None


async def send_dealer(bot: Bot, chat_id: int, caption: str, kb, reply_to: int | None = None):
    """Отправляет картинку крупье с подписью. Возвращает (сообщение, есть_ли_фото)."""
    global dealer_file_id
    reply = rp(reply_to) if reply_to else None
    photo = dealer_file_id or (FSInputFile(DEALER_IMAGE) if os.path.exists(DEALER_IMAGE) else None)
    if photo is None:  # нет картинки — работаем текстом
        msg = await bot.send_message(chat_id, caption, reply_markup=kb, reply_parameters=reply)
        return msg, False
    msg = await bot.send_photo(chat_id, photo, caption=caption, reply_markup=kb, reply_parameters=reply)
    if not dealer_file_id and msg.photo:
        dealer_file_id = msg.photo[-1].file_id  # дальше шлём по file_id — быстро и без загрузки
    return msg, True


async def edit_card_message(bot: Bot, chat_id: int, msg_id: int, has_photo: bool, text: str, kb=None) -> None:
    try:
        if has_photo:
            await bot.edit_message_caption(chat_id=chat_id, message_id=msg_id, caption=text, reply_markup=kb)
        else:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id, reply_markup=kb)
    except TelegramBadRequest:
        pass


# ============================== Справка ==============================
HELP_TEXT = (
    "🎰 <b>Псевдо-казино</b> (виртуальные монеты)\n"
    "Команды работают и с <code>/</code>, и с <code>!</code>, можно по-русски.\n\n"
    "<b>Профиль и экономика</b>\n"
    "!профиль — профиль, баланс, статистика (ответом — чужой профиль)\n"
    "!страна — выбрать страну\n"
    "!бонус — ежедневный бонус\n"
    "!топ — топ чата · !топстран — топ стран\n"
    "!дать &lt;сумма&gt; — перевод (ответом на сообщение)\n\n"
    "<b>Соло</b> (ставка: <code>100</code>, <code>5к</code>, <code>all</code>)\n"
    "!слоты &lt;ставка&gt;\n"
    "!кубики &lt;ставка&gt; — против бота\n"
    "!монетка &lt;ставка&gt; &lt;орел|решка&gt;\n"
    "!покер &lt;ставка на руку&gt; [рук] — видеопокер\n\n"
    "<b>С другими игроками</b>\n"
    "!рулетка &lt;ставка&gt; &lt;red|black|even|odd|0-36&gt; — общий стол\n"
    "!дуэль &lt;ставка&gt; — вызов (можно ответом на сообщение игрока)"
)


@router.message(cmd("start", "help", "помощь", "хелп", "команды"))
async def cmd_help(m: Message, cmdname: str, args: str) -> None:
    me(m)
    if cmdname == "start" and args == "country" and m.chat.type == "private":
        await m.answer("🌍 Выбери свою страну:", reply_markup=country_kb())
        return
    await m.answer(HELP_TEXT)


# ============================== Страна ==============================
def country_kb() -> InlineKeyboardMarkup:
    rows, row = [], []
    for code, (flag, name) in COUNTRIES.items():
        row.append(InlineKeyboardButton(text=f"{flag} {name}", callback_data=f"cty:{code}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def dm_country_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🌍 Выбрать страну", url=f"https://t.me/{BOT_USERNAME}?start=country")
    ]])


@router.message(cmd("страна", "country"))
async def cmd_country(m: Message) -> None:
    me(m)
    if m.chat.type == "private":
        await m.answer("🌍 Выбери свою страну:", reply_markup=country_kb())
    else:
        await m.reply("🌍 Страну выбирают в личке со мной — жми кнопку:", reply_markup=dm_country_kb())


@router.callback_query(F.data.startswith("cty:"))
async def cb_country(cb: CallbackQuery) -> None:
    touch(cb.from_user.id, uname(cb.from_user))
    code = cb.data.split(":", 1)[1]
    if code == "menu":
        await cb.answer()
        await cb.message.edit_text("🌍 Выбери свою страну:", reply_markup=country_kb())
        return
    if code not in COUNTRIES:
        await cb.answer("Неизвестная страна", show_alert=True)
        return
    db.execute("UPDATE users SET country = ? WHERE user_id = ?", (code, cb.from_user.id))
    flag, name = COUNTRIES[code]
    await cb.answer("Готово!")
    await cb.message.edit_text(f"✅ Страна: {flag} <b>{name}</b>\nПосмотреть профиль: <code>!профиль</code>")


@router.message(cmd("топстран", "countries", "страны"))
async def cmd_top_countries(m: Message) -> None:
    me(m)
    rows = db.execute(
        "SELECT country, SUM(balance), COUNT(*) FROM users WHERE country IS NOT NULL "
        "GROUP BY country ORDER BY SUM(balance) DESC LIMIT 10"
    ).fetchall()
    if not rows:
        await m.reply("Пока никто не выбрал страну. Сделай это командой <code>!страна</code> 🌍")
        return
    lines = []
    for i, (code, total, n) in enumerate(rows, 1):
        flag, name = COUNTRIES.get(code, ("🏳️", code))
        lines.append(f"{i}. {flag} {name} — {fmt(total)} {CURRENCY} ({n} {plural(n, 'игрок', 'игрока', 'игроков')})")
    await m.answer("🌍 <b>Топ стран по богатству</b>\n" + "\n".join(lines))


# ============================== Профиль и экономика ==============================
@router.message(cmd("профиль", "profile", "prof", "я"))
async def cmd_profile(m: Message) -> None:
    me(m)
    target = m.from_user
    if m.reply_to_message and m.reply_to_message.from_user and not m.reply_to_message.from_user.is_bot:
        target = m.reply_to_message.from_user
        touch(target.id, uname(target))
    own = target.id == m.from_user.id

    name, balance, country, games, wins, best = db.execute(
        "SELECT name, balance, country, games, wins, best_win FROM users WHERE user_id = ?", (target.id,)
    ).fetchone()
    if country in COUNTRIES:
        flag, cname = COUNTRIES[country]
        country_line = f"{flag} {cname}"
    else:
        country_line = "не выбрана"
    rate = f" ({wins * 100 // games}%)" if games else ""

    text = (
        f"👤 <b>{esc(name)}</b>\n"
        f"🌍 Страна: {country_line}\n"
        f"💰 Баланс: <b>{fmt(balance)}</b> {CURRENCY}\n"
        f"🎮 Игр сыграно: {games}\n"
        f"🏆 Побед: {wins}{rate}\n"
        f"🏅 Лучший выигрыш: {fmt(best)} {CURRENCY}"
    )
    kb = None
    if own:
        if m.chat.type == "private":
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🌍 Выбрать страну", callback_data="cty:menu")
            ]])
        else:
            kb = dm_country_kb()
    await m.reply(text, reply_markup=kb)


@router.message(cmd("баланс", "balance", "bal"))
async def cmd_balance(m: Message) -> None:
    uid = me(m)
    await m.reply(f"💰 Баланс: <b>{fmt(get_balance(uid))}</b> {CURRENCY}")


@router.message(cmd("бонус", "daily"))
async def cmd_daily(m: Message) -> None:
    uid = me(m)
    (last,) = db.execute("SELECT last_daily FROM users WHERE user_id = ?", (uid,)).fetchone()
    left = DAILY_COOLDOWN - (time.time() - last)
    if left > 0:
        await m.reply(f"Бонус уже получен. Приходи через {int(left // 3600)} ч {int(left % 3600 // 60)} мин ⏳")
        return
    add_balance(uid, DAILY_BONUS)
    db.execute("UPDATE users SET last_daily = ? WHERE user_id = ?", (time.time(), uid))
    await m.reply(f"🎁 Бонус <b>+{DAILY_BONUS}</b> {CURRENCY}! Баланс: {fmt(get_balance(uid))} {CURRENCY}")


@router.message(cmd("топ", "top"))
async def cmd_top(m: Message) -> None:
    me(m)
    rows = db.execute(
        "SELECT u.name, u.balance FROM users u JOIN chat_members c ON c.user_id = u.user_id "
        "WHERE c.chat_id = ? ORDER BY u.balance DESC LIMIT 10",
        (m.chat.id,),
    ).fetchall()
    medals = ["🥇", "🥈", "🥉"]
    lines = [
        f"{medals[i] if i < 3 else f'{i + 1}.'} {esc(n)} — {fmt(b)} {CURRENCY}" for i, (n, b) in enumerate(rows)
    ]
    await m.answer("🏆 <b>Топ игроков чата</b>\n" + "\n".join(lines))


@router.message(cmd("дать", "give", "перевод"))
async def cmd_give(m: Message, args: str) -> None:
    uid = me(m)
    target = m.reply_to_message.from_user if m.reply_to_message else None
    if not target or target.is_bot or target.id == uid:
        await m.reply("Ответь командой <code>!дать 100</code> на сообщение другого игрока.")
        return
    amount = parse_amount(first_word(args) or "")
    if not amount or amount <= 0 or amount > get_balance(uid):
        await m.reply("Некорректная сумма или не хватает монет.")
        return
    touch(target.id, uname(target))
    add_balance(uid, -amount)
    add_balance(target.id, amount)
    await m.reply(f"💸 Переведено {fmt(amount)} {CURRENCY} → {esc(uname(target))}")


@router.message(cmd("addcoins", "выдать"))
async def cmd_addcoins(m: Message, args: str) -> None:
    """Для админов бота: !выдать 1000 (ответом на сообщение игрока)."""
    if m.from_user.id not in ADMIN_IDS or not m.reply_to_message or not m.reply_to_message.from_user:
        return
    try:
        amount = int(first_word(args) or "")
    except ValueError:
        return
    t = m.reply_to_message.from_user
    touch(t.id, uname(t))
    add_balance(t.id, amount)
    await m.reply(f"Готово: {amount:+} {CURRENCY} для {esc(uname(t))}")


# ============================== Быстрые игры ==============================
@router.message(cmd("слоты", "slots"))
async def cmd_slots(m: Message, args: str) -> None:
    bet = await take_bet(m, first_word(args))
    if bet is None:
        return
    msg = await m.reply_dice(emoji="🎰")
    await asyncio.sleep(2.5)  # ждём окончания анимации
    mult, text = slot_result(msg.dice.value)
    await finish(m, bet, mult, f"🎰 {text}" + (f" (x{mult})" if mult else ""))


@router.message(cmd("кубики", "dice"))
async def cmd_dice(m: Message, args: str) -> None:
    bet = await take_bet(m, first_word(args))
    if bet is None:
        return
    mine = await m.reply_dice(emoji="🎲")
    theirs = await m.answer_dice(emoji="🎲")
    await asyncio.sleep(4)
    a, b = mine.dice.value, theirs.dice.value
    if a > b:
        await finish(m, bet, 2, f"🎲 Ты: {a} · Бот: {b}")
    elif a == b:
        await finish(m, bet, 0, f"🎲 Ты: {a} · Бот: {b} — ничья, при равенстве выигрывает казино")
    else:
        await finish(m, bet, 0, f"🎲 Ты: {a} · Бот: {b}")


@router.message(cmd("монетка", "coin"))
async def cmd_coin(m: Message, args: str) -> None:
    parts = args.lower().split()
    if len(parts) < 2 or parts[1] not in ("орел", "орёл", "решка", "heads", "tails"):
        await m.reply("Формат: <code>!монетка 100 орел</code> или <code>!монетка 100 решка</code>")
        return
    bet = await take_bet(m, parts[0])
    if bet is None:
        return
    pick = "орел" if parts[1] in ("орел", "орёл", "heads") else "решка"
    result = RNG.choice(["орел", "решка"])
    emoji = "🦅" if result == "орел" else "🌑"
    await finish(m, bet, 1.95 if pick == result else 0, f"{emoji} Выпало: <b>{result}</b> (ты выбрал: {pick})")


# ============================== Видеопокер ==============================
@dataclass
class VPGame:
    gid: str
    chat_id: int
    user_id: int
    name: str
    bet: int
    hands: int
    cards: list
    held: set = field(default_factory=set)
    msg_id: int = 0
    photo: bool = False


vp_games: dict[str, VPGame] = {}

VP_HELP = (
    "🃏 <b>Видеопокер</b> · Jacks or Better\n"
    "Формат: <code>!покер &lt;ставка на руку&gt; [рук]</code>\n"
    "Пример: <code>!покер 1к 29</code> — 29 рук по 1 000 = 29 000 {c}\n\n"
    "Тебе сдают 5 карт. Выбираешь, какие оставить, — и эти карты копируются во <b>все</b> руки, "
    "а остальные в каждой руке добираются заново.\n\n"
    "<b>Выплаты (x ставки на руку)</b>\n"
    "Роял-флеш x800 · Стрит-флеш x50 · Каре x25 · Фулл-хаус x8\n"
    "Флеш x5 · Стрит x4 · Тройка x3 · Две пары x2 · Пара J+ x1"
).format(c=CURRENCY)


def vp_text(g: VPGame) -> str:
    line = " ".join(f"[{card_str(c)}]" for c in g.cards)
    return (
        f"🃏 <b>Видеопокер</b> · Jacks or Better\n"
        f"Игрок: {esc(g.name)}\n"
        f"Ставка: {fmt(g.bet)} × {g.hands} {plural(g.hands, 'рука', 'руки', 'рук')} = "
        f"<b>{fmt(g.bet * g.hands)}</b> {CURRENCY}\n\n"
        f"<code>{line}</code>\n\n"
        f"Нажми на карты, которые оставляешь (✅), затем «Раздать»."
    )


def vp_kb(g: VPGame) -> InlineKeyboardMarkup:
    cards = [
        InlineKeyboardButton(
            text=("✅ " if i in g.held else "") + card_str(c), callback_data=f"vp:{g.gid}:{i}"
        )
        for i, c in enumerate(g.cards)
    ]
    controls = [
        InlineKeyboardButton(text="✋ Все", callback_data=f"vp:{g.gid}:all"),
        InlineKeyboardButton(text="🗑 Сброс", callback_data=f"vp:{g.gid}:none"),
        InlineKeyboardButton(text="🃏 Раздать", callback_data=f"vp:{g.gid}:go"),
    ]
    return InlineKeyboardMarkup(inline_keyboard=[cards, controls])


@router.message(cmd("покер", "видеопокер", "vp", "poker", "videopoker"))
async def cmd_poker(m: Message, bot: Bot, args: str) -> None:
    parts = args.split()
    uid = me(m)
    if not parts:
        await m.reply(VP_HELP)
        return
    hands = 1
    if len(parts) >= 2:
        if not parts[1].isdigit() or not 1 <= int(parts[1]) <= VP_MAX_HANDS:
            await m.reply(f"Количество рук — число от 1 до {VP_MAX_HANDS}.")
            return
        hands = int(parts[1])
    if any(g.user_id == uid for g in vp_games.values()):
        await m.reply("У тебя уже идёт раздача — доиграй её 🙂")
        return

    gid = uuid.uuid4().hex[:8]
    bet = await take_bet(m, parts[0], units=hands, key=f"vp:{gid}")
    if bet is None:
        return
    g = VPGame(gid, m.chat.id, uid, uname(m.from_user), bet, hands, new_hand())
    vp_games[gid] = g
    try:
        msg, g.photo = await send_dealer(bot, m.chat.id, vp_text(g), vp_kb(g), reply_to=m.message_id)
    except Exception:
        logging.exception("Не удалось отправить раздачу")
        vp_games.pop(gid, None)
        refund(f"vp:{gid}")
        await m.reply("Не получилось раздать карты, ставка возвращена.")
        return
    g.msg_id = msg.message_id
    spawn(vp_timeout(bot, gid))


async def vp_timeout(bot: Bot, gid: str) -> None:
    await asyncio.sleep(VP_TIMEOUT)
    g = vp_games.get(gid)
    if g:
        await vp_draw(bot, g)


@router.callback_query(F.data.startswith("vp:"))
async def cb_vp(cb: CallbackQuery, bot: Bot) -> None:
    _, gid, action = cb.data.split(":")
    g = vp_games.get(gid)
    if not g:
        await cb.answer("Эта раздача уже закончена", show_alert=True)
        return
    if cb.from_user.id != g.user_id:
        await cb.answer("Это не твоя раздача 🙂", show_alert=True)
        return
    if action == "go":
        await cb.answer()
        await vp_draw(bot, g)
        return
    if action == "all":
        g.held = set(range(5))
    elif action == "none":
        g.held = set()
    else:
        g.held ^= {int(action)}
    await cb.answer()
    try:
        await cb.message.edit_reply_markup(reply_markup=vp_kb(g))
    except TelegramBadRequest:
        pass


async def vp_draw(bot: Bot, g: VPGame) -> None:
    if vp_games.pop(g.gid, None) is None:  # защита от двойного нажатия
        return
    hands = draw_hands(g.cards, g.held, g.hands)
    keys = [evaluate(h) for h in hands]
    total_bet = g.bet * g.hands
    payout = sum(g.bet * PAYTABLE.get(k, 0) for k in keys)
    if payout:
        add_balance(g.user_id, payout)
    release(f"vp:{g.gid}")
    profit = payout - total_bet
    record(g.user_id, profit)

    kept = [card_str(g.cards[i]) for i in sorted(g.held)]
    kept_line = " ".join(f"[{c}]" for c in kept) if kept else "ничего (все карты заменены)"
    await edit_card_message(
        bot, g.chat_id, g.msg_id, g.photo,
        f"🃏 <b>Видеопокер</b> · {esc(g.name)}\nОставлено: <code>{kept_line}</code>\nРаздача завершена 👇",
    )

    lines = [f"🃏 <b>Результат</b> · {esc(g.name)}"]
    if g.hands <= 5:
        for i, (h, k) in enumerate(zip(hands, keys), 1):
            extra = f" (x{PAYTABLE[k]})" if k in PAYTABLE else ""
            lines.append(f"{i}. <code>{' '.join(card_str(c) for c in h)}</code> — {HAND_NAMES[k]}{extra}")
    else:
        cnt = Counter(keys)
        for k in PAYTABLE:
            if cnt[k]:
                lines.append(f"{HAND_NAMES[k]} (x{PAYTABLE[k]}) — {cnt[k]} шт.")
        if cnt["nothing"]:
            lines.append(f"Мимо — {cnt['nothing']} шт.")
    lines.append("")
    lines.append(f"Ставка: {fmt(total_bet)} {CURRENCY}")
    lines.append(f"Выплата: {fmt(payout)} {CURRENCY}")
    if profit > 0:
        lines.append(f"✅ Выигрыш: <b>+{fmt(profit)}</b> {CURRENCY}")
    elif profit == 0:
        lines.append("🤝 В ноль")
    else:
        lines.append(f"❌ Проигрыш: <b>-{fmt(-profit)}</b> {CURRENCY}")
    lines.append(f"Баланс: {fmt(get_balance(g.user_id))} {CURRENCY}")
    await bot.send_message(g.chat_id, "\n".join(lines), reply_parameters=rp(g.msg_id))


# ============================== Общая рулетка ==============================
rounds: dict[int, list[dict]] = {}   # chat_id -> ставки текущего раунда


@router.message(cmd("рулетка", "roulette", "rl"))
async def cmd_roulette(m: Message, bot: Bot, args: str) -> None:
    parts = args.split()
    if len(parts) < 2:
        await m.reply(
            "Формат: <code>!рулетка 100 red</code>\n"
            "Варианты: red, black, even, odd (x2) или число 0–36 (x36).\n"
            f"Стол открыт {ROUND_SECONDS} сек — за это время могут присоединиться другие игроки."
        )
        return
    choice = parse_roulette_choice(parts[1])
    if not choice:
        await m.reply("Выбери: red, black, even, odd или число 0–36.")
        return
    uid = me(m)
    if sum(1 for b in rounds.get(m.chat.id, []) if b["uid"] == uid) >= ROULETTE_MAX_BETS:
        await m.reply(f"Максимум {ROULETTE_MAX_BETS} ставок за раунд.")
        return

    key = f"rl:{uuid.uuid4().hex[:8]}"
    bet = await take_bet(m, parts[0], key=key, cooldown=False)
    if bet is None:
        return
    is_new = m.chat.id not in rounds
    rounds.setdefault(m.chat.id, []).append(
        {"uid": uid, "name": uname(m.from_user), "bet": bet, "choice": choice, "key": key}
    )
    line = f"✅ {esc(uname(m.from_user))}: {fmt(bet)} {CURRENCY} на {roulette_label(choice)}"
    if is_new:
        delay = 1 if m.chat.type == "private" else ROUND_SECONDS
        await m.reply(
            f"🎡 Стол открыт! Вращение через {delay} сек.\n{line}\n"
            f"Присоединяйся: <code>!рулетка 100 red</code>"
        )
        spawn(roulette_spin(bot, m.chat.id, delay))
    else:
        await m.reply(line)


async def roulette_spin(bot: Bot, chat_id: int, delay: int) -> None:
    await asyncio.sleep(delay)
    bets = rounds.pop(chat_id, [])
    if not bets:
        return
    n = spin_roulette()
    net: dict[int, list] = {}   # uid -> [имя, итог]
    for b in bets:
        win = b["bet"] * roulette_mult(b["choice"], n)
        if win:
            add_balance(b["uid"], win)
        release(b["key"])
        profit = win - b["bet"]
        record(b["uid"], profit)
        entry = net.setdefault(b["uid"], [b["name"], 0])
        entry[1] += profit

    lines = [f"🎡 Выпало: {roulette_color(n)} <b>{n}</b>", ""]
    for name, total in list(net.values())[:25]:
        if total > 0:
            lines.append(f"✅ {esc(name)}: <b>+{fmt(total)}</b> {CURRENCY}")
        elif total == 0:
            lines.append(f"🤝 {esc(name)}: 0")
        else:
            lines.append(f"❌ {esc(name)}: -{fmt(-total)} {CURRENCY}")
    if len(net) > 25:
        lines.append(f"…и ещё {len(net) - 25}")
    await bot.send_message(chat_id, "\n".join(lines))


# ============================== Дуэли (игрок против игрока) ==============================
@dataclass
class Duel:
    did: str
    chat_id: int
    user_id: int
    name: str
    stake: int
    target_id: int | None = None
    target_name: str = ""
    msg_id: int = 0


duels: dict[str, Duel] = {}


def duel_kb(did: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⚔️ Принять", callback_data=f"duel:{did}:acc"),
        InlineKeyboardButton(text="✖️ Отмена", callback_data=f"duel:{did}:cancel"),
    ]])


@router.message(cmd("дуэль", "duel"))
async def cmd_duel(m: Message, bot: Bot, args: str) -> None:
    target = m.reply_to_message.from_user if m.reply_to_message else None
    if target and (target.is_bot or target.id == m.from_user.id):
        await m.reply("Нельзя вызвать бота или самого себя 🙂")
        return
    did = uuid.uuid4().hex[:8]
    bet = await take_bet(m, first_word(args), key=f"duel:{did}:a")
    if bet is None:
        return
    d = Duel(did, m.chat.id, m.from_user.id, uname(m.from_user), bet,
             target.id if target else None, uname(target) if target else "")
    duels[did] = d
    who = f"для {esc(d.target_name)}" if target else "для любого смельчака"
    msg = await m.answer(
        f"⚔️ <b>Дуэль!</b> {esc(d.name)} ставит {fmt(bet)} {CURRENCY} — вызов {who}.\n"
        f"Бросаем кубики, у кого больше — забирает банк (комиссия казино {int(DUEL_FEE * 100)}%).",
        reply_markup=duel_kb(did),
    )
    d.msg_id = msg.message_id
    spawn(duel_timeout(bot, did))


async def duel_timeout(bot: Bot, did: str) -> None:
    await asyncio.sleep(DUEL_TIMEOUT)
    d = duels.pop(did, None)
    if d:
        refund(f"duel:{did}:a")
        try:
            await bot.edit_message_text(
                f"⌛ Дуэль {esc(d.name)} никто не принял — ставка возвращена.",
                chat_id=d.chat_id, message_id=d.msg_id,
            )
        except TelegramBadRequest:
            pass


@router.callback_query(F.data.startswith("duel:"))
async def cb_duel(cb: CallbackQuery, bot: Bot) -> None:
    _, did, action = cb.data.split(":")
    d = duels.get(did)
    if not d:
        await cb.answer("Эта дуэль уже закончена", show_alert=True)
        return
    uid = cb.from_user.id

    if action == "cancel":
        if uid != d.user_id:
            await cb.answer("Отменить может только автор вызова", show_alert=True)
            return
        duels.pop(did, None)
        refund(f"duel:{did}:a")
        await cb.answer()
        await cb.message.edit_text(f"✖️ {esc(d.name)} отменил дуэль, ставка возвращена.")
        return

    if uid == d.user_id:
        await cb.answer("Нельзя принять свой вызов 🙂", show_alert=True)
        return
    if d.target_id and uid != d.target_id:
        await cb.answer("Этот вызов адресован другому игроку", show_alert=True)
        return
    touch(uid, uname(cb.from_user))
    if get_balance(uid) < d.stake:
        await cb.answer(f"Не хватает монет: нужно {fmt(d.stake)}", show_alert=True)
        return

    duels.pop(did, None)             # теперь вызов занят
    hold(f"duel:{did}:b", uid, d.stake)
    await cb.answer()
    rival = uname(cb.from_user)
    await cb.message.edit_text(
        f"⚔️ <b>{esc(d.name)}</b> против <b>{esc(rival)}</b> — банк {fmt(d.stake * 2)} {CURRENCY}\n"
        f"Первый кубик — {esc(d.name)}, второй — {esc(rival)}."
    )
    d1 = await bot.send_dice(d.chat_id, emoji="🎲")
    d2 = await bot.send_dice(d.chat_id, emoji="🎲")
    await asyncio.sleep(4)
    a, b = d1.dice.value, d2.dice.value

    if a == b:
        refund(f"duel:{did}:a")
        refund(f"duel:{did}:b")
        await bot.send_message(d.chat_id, f"🎲 {a} : {b} — ничья! Ставки возвращены 🤝")
        return

    pot = d.stake * 2
    prize = pot - int(pot * DUEL_FEE)
    winner, wname, loser = (d.user_id, d.name, uid) if a > b else (uid, rival, d.user_id)
    add_balance(winner, prize)
    release(f"duel:{did}:a")
    release(f"duel:{did}:b")
    record(winner, prize - d.stake)
    record(loser, -d.stake)
    await bot.send_message(
        d.chat_id,
        f"🎲 {a} : {b}\n🏆 Побеждает <b>{esc(wname)}</b> и забирает "
        f"<b>{fmt(prize)}</b> {CURRENCY} (профит +{fmt(prize - d.stake)})",
    )


# ============================== Запуск ==============================
async def main() -> None:
    global BOT_USERNAME
    if not BOT_TOKEN:
        raise SystemExit("Задай токен: export BOT_TOKEN='...'")
    logging.basicConfig(level=logging.INFO)
    if not os.path.exists(DEALER_IMAGE):
        logging.warning("Нет файла dealer.jpg рядом с bot.py — покер будет работать без картинки крупье")

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    BOT_USERNAME = (await bot.get_me()).username

    returned = refund_all()   # если бот падал посреди игр — возвращаем замороженные ставки
    if returned:
        logging.info("После перезапуска возвращено ставок: %s", returned)

    await bot.set_my_commands([
        BotCommand(command="profile", description="Профиль и баланс"),
        BotCommand(command="country", description="Выбрать страну"),
        BotCommand(command="daily", description="Ежедневный бонус"),
        BotCommand(command="slots", description="Слоты"),
        BotCommand(command="dice", description="Кубики против бота"),
        BotCommand(command="coin", description="Монетка"),
        BotCommand(command="poker", description="Видеопокер"),
        BotCommand(command="roulette", description="Общая рулетка"),
        BotCommand(command="duel", description="Дуэль с игроком"),
        BotCommand(command="top", description="Топ чата"),
        BotCommand(command="help", description="Все команды"),
    ])

    dp = Dispatcher()
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
