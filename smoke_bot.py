"""
Телеграм-бот для перекуров в групповом чате.

Установка:
    pip install python-telegram-bot

Запуск (Mac/Linux):
    BOT_TOKEN="твой_токен" python3 smoke_bot.py

Команды:
    /smoke          — начать перекур на 10 минут
    /smoke 15       — начать перекур на 15 минут
    /vote           — запустить голосование (нужно 2 из 4)
    /vote 15        — голосование за 15-минутный перекур
    /stop           — остановить текущий перекур
    /next_smoke     — таймер обратного отсчёта до следующего перекура
    /stats          — статистика перекуров
"""

import asyncio
import os
import json
import logging
import re
from datetime import datetime, timedelta, date
from pathlib import Path
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes
)
from telegram.error import BadRequest

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

chat_states: dict[int, dict] = {}

DEFAULT_BREAK_MINUTES = 10
VOTES_NEEDED = 2
VOTE_TIMEOUT_SECONDS = 180

STATS_FILE = Path(__file__).parent / "smoke_stats.json"


# ─── УТИЛИТЫ ──────────────────────────────────────────────────────────────────

def md_escape(text: str) -> str:
    """Экранировать спецсимволы Markdown v1."""
    return re.sub(r'([*_`\[])', r'\\\1', str(text))


def fmt_delta(delta: timedelta) -> str:
    """MM:SS для обратного отсчёта."""
    total = max(0, int(delta.total_seconds()))
    m, s = divmod(total, 60)
    return f"{m:02d}:{s:02d}"


def fmt_seconds(seconds: int) -> str:
    """Человекочитаемое время: 1 ч 5 мин 38 сек."""
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h} ч")
    if m:
        parts.append(f"{m} мин")
    if s or not parts:
        parts.append(f"{s} сек")
    return " ".join(parts)


def _cancel_task(state: dict, key: str):
    task = state.get(key)
    if task and not task.done():
        task.cancel()
    state[key] = None


def _elapsed_seconds(state: dict) -> int:
    """Фактически прошедшее время перекура в секундах."""
    if state.get("break_start") is None:
        return state.get("break_duration", DEFAULT_BREAK_MINUTES) * 60
    return max(0, int((datetime.now() - state["break_start"]).total_seconds()))


# ─── СОХРАНЕНИЕ / ЗАГРУЗКА ────────────────────────────────────────────────────

def _load_stats() -> dict:
    if not STATS_FILE.exists():
        return {}
    try:
        with open(STATS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return {int(k): v for k, v in raw.items()}
    except Exception as e:
        logger.warning(f"Не удалось загрузить статистику: {e}")
        return {}


def _save_stats():
    data = {}
    for chat_id, state in chat_states.items():
        data[str(chat_id)] = {
            "stats_total_breaks": state["stats_total_breaks"],
            "stats_total_seconds": state["stats_total_seconds"],
            "stats_votes_won": state["stats_votes_won"],
            "stats_votes_lost": state["stats_votes_lost"],
            "stats_today_breaks": state["stats_today_breaks"],
            "stats_today_date": state["stats_today_date"].isoformat(),
            "stats_users": state["stats_users"],
            "stats_shop_total": state["stats_shop_total"],
            "stats_shop_today": state["stats_shop_today"],
            "stats_shop_today_date": state["stats_shop_today_date"].isoformat(),
            "stats_shop_initiated": state["stats_shop_initiated"],
            "stats_shop_goers": state["stats_shop_goers"],
            "next_smoke": state["next_smoke"].isoformat() if state["next_smoke"] else None,
        }
    try:
        with open(STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"Не удалось сохранить статистику: {e}")


def _init_stats_from_saved(state: dict, saved: dict):
    state["stats_total_breaks"] = saved.get("stats_total_breaks", 0)
    state["stats_total_seconds"] = saved.get("stats_total_seconds", 0)
    state["stats_votes_won"] = saved.get("stats_votes_won", 0)
    state["stats_votes_lost"] = saved.get("stats_votes_lost", 0)
    state["stats_today_breaks"] = saved.get("stats_today_breaks", 0)
    state["stats_users"] = saved.get("stats_users", {})
    state["stats_shop_total"] = saved.get("stats_shop_total", 0)
    state["stats_shop_initiated"] = saved.get("stats_shop_initiated", {})
    state["stats_shop_goers"] = saved.get("stats_shop_goers", {})
    next_smoke_str = saved.get("next_smoke")
    if next_smoke_str:
        try:
            ns = datetime.fromisoformat(next_smoke_str)
            state["next_smoke"] = ns if ns > datetime.now() else None
        except ValueError:
            state["next_smoke"] = None
    else:
        state["next_smoke"] = None
    state["stats_shop_today"] = saved.get("stats_shop_today", 0)
    shop_today_str = saved.get("stats_shop_today_date")
    if shop_today_str:
        try:
            shop_saved_date = date.fromisoformat(shop_today_str)
            if shop_saved_date != datetime.now().date():
                state["stats_shop_today"] = 0
                state["stats_shop_today_date"] = datetime.now().date()
            else:
                state["stats_shop_today_date"] = shop_saved_date
        except ValueError:
            state["stats_shop_today_date"] = datetime.now().date()
    else:
        state["stats_shop_today_date"] = datetime.now().date()
    today_str = saved.get("stats_today_date")
    if today_str:
        try:
            saved_date = date.fromisoformat(today_str)
            if saved_date != datetime.now().date():
                state["stats_today_breaks"] = 0
                state["stats_today_date"] = datetime.now().date()
            else:
                state["stats_today_date"] = saved_date
        except ValueError:
            state["stats_today_date"] = datetime.now().date()
    else:
        state["stats_today_date"] = datetime.now().date()


def _ensure_user(state: dict, name: str):
    """Создать запись пользователя в stats_users если её нет."""
    if name not in state["stats_users"]:
        state["stats_users"][name] = {"count": 0, "seconds": 0}


def _record_break_end(state: dict, actual_seconds: int):
    """Обновить общие счётчики и записать время каждому участнику."""
    today = datetime.now().date()
    if state["stats_today_date"] != today:
        state["stats_today_date"] = today
        state["stats_today_breaks"] = 0
    state["stats_total_breaks"] += 1
    state["stats_today_breaks"] += 1
    state["stats_total_seconds"] += actual_seconds
    for name in state.get("break_smokers_names", set()):
        _ensure_user(state, name)
        state["stats_users"][name]["count"] += 1
        state["stats_users"][name]["seconds"] += actual_seconds
    _save_stats()


_saved_stats = _load_stats()


def get_state(chat_id: int) -> dict:
    if chat_id not in chat_states:
        state = {
            # перекур
            "break_active": False,
            "break_end": None,
            "break_start": None,
            "next_smoke": None,
            "break_duration": DEFAULT_BREAK_MINUTES,
            "end_task": None,
            "countdown_task": None,
            "countdown_msg_id": None,
            "break_smokers_names": set(),
            "break_starter_id": None,   # user_id того кто запустил перекур
            "join_msg_id": None,
            # голосование
            "vote_active": False,
            "vote_quorum": False,    # кворум набрался, перекур идёт
            "vote_yes": set(),
            "vote_yes_names": {},
            "vote_no": set(),
            "vote_msg_id": None,
            "vote_task": None,
            "vote_duration": DEFAULT_BREAK_MINUTES,
            "vote_proposer": "Кто-то",
            "vote_starter_id": None,
            # статистика
            "stats_total_breaks": 0,
            "stats_total_seconds": 0,
            "stats_votes_won": 0,
            "stats_votes_lost": 0,
            "stats_today_breaks": 0,
            "stats_today_date": datetime.now().date(),
            "stats_users": {},
            # статистика магазина
            "stats_shop_total": 0,
            "stats_shop_today": 0,
            "stats_shop_today_date": datetime.now().date(),
            "stats_shop_initiated": {},   # name -> count
            "stats_shop_goers": {},       # name -> count
            # магазин
            "shop_active": False,
            "shop_vote_active": False,
            "shop_vote_quorum": False,
            "shop_yes": set(),
            "shop_yes_names": {},
            "shop_no": set(),
            "shop_msg_id": None,
            "shop_vote_task": None,
            "shop_proposer": "Кто-то",
            "shop_starter_id": None,
            "shop_goers_names": set(),
        }
        if chat_id in _saved_stats:
            _init_stats_from_saved(state, _saved_stats[chat_id])
        chat_states[chat_id] = state
    return chat_states[chat_id]


# ─── КЛАВИАТУРЫ И ТЕКСТЫ ──────────────────────────────────────────────────────

def _build_vote_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🚬 Лес гооу", callback_data="vote_yes"),
        InlineKeyboardButton("😞 Не хочу (((", callback_data="vote_no"),
    ]])


def _build_join_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🚬 Айда!", callback_data="join_smoke"),
    ]])


def _build_shop_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🛒 Иду!", callback_data="shop_yes"),
        InlineKeyboardButton("😴 Не пойду", callback_data="shop_no"),
    ]])


def _shop_vote_text(proposer: str, yes_names: list, no: int,
                    quorum: bool = False) -> str:
    names_str = ", ".join(md_escape(n) for n in yes_names) if yes_names else "пока никого"
    yes = len(yes_names)
    if quorum:
        header = "Поход в магазин начался!"
        goers_line = f"\nИдут в магазин ({yes}): *{names_str}*"
        footer = "Нажми «Иду!» чтобы присоединиться."
    else:
        header = f"*{md_escape(proposer)}* предлагает сходить в магазин!"
        goers_line = f"\nИдут ({yes}): *{names_str}*"
        footer = f"Нужно *{VOTES_NEEDED}* голоса. Против: *{no}*\nГолосование закроется через 3 минуты."
    return f"{header}{goers_line}\n\n{footer}"


def _vote_text(proposer: str, duration: int, yes_names: list,
               no: int, quorum: bool = False, break_end_str: str = "") -> str:
    names_str = ", ".join(md_escape(n) for n in yes_names) if yes_names else "пока никого"
    yes = len(yes_names)

    if quorum:
        header = f"Перекур идёт до *{break_end_str}*!"
        smokers_line = f"\nИдут курить ({yes}): *{names_str}*"
        footer = "Нажми «Лес гооу» чтобы присоединиться."
    else:
        header = f"*{md_escape(proposer)}* предлагает перекур на *{duration} мин*!"
        smokers_line = f"\nЗа ({yes}): *{names_str}*"
        footer = f"Нужно *{VOTES_NEEDED}* голоса. Против: *{no}*\nГолосование закроется через 3 минуты."

    return f"{header}{smokers_line}\n\n{footer}"


def _join_text(state: dict) -> str:
    """Текст сообщения с кнопкой Айда."""
    end_time = state["break_end"].strftime("%H:%M:%S") if state["break_end"] else "?"
    smokers = ", ".join(md_escape(n) for n in sorted(state["break_smokers_names"]))
    return (
        f"Перекур идёт до *{end_time}*\n\n"
        f"Идут курить: *{smokers}*\n\n"
        f"Остановить досрочно: /stop"
    )


# ─── РУЧНОЙ ЗАПУСК ────────────────────────────────────────────────────────────

async def smoke_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    state = get_state(chat_id)
    user = update.effective_user.first_name

    if state["break_active"]:
        await update.message.reply_text("Перекур уже идёт!")
        return

    duration = DEFAULT_BREAK_MINUTES
    if context.args:
        try:
            duration = int(context.args[0])
            if duration <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Укажи целое число минут, например: /smoke 15")
            return

    # Инициатор автоматически идёт курить
    state["break_smokers_names"] = {user}
    state["break_starter_id"] = update.effective_user.id
    _ensure_user(state, user)

    await _start_break(context, chat_id, duration, started_by=user,
                       reply_func=lambda t, **kw: update.message.reply_text(t, **kw),
                       show_join=True)
    _save_stats()


async def _start_break(context, chat_id: int, duration: int, started_by: str,
                       reply_func, show_join: bool = False):
    state = get_state(chat_id)

    _cancel_task(state, "countdown_task")
    _cancel_task(state, "end_task")

    now = datetime.now()
    state["break_active"] = True
    state["break_end"] = now + timedelta(minutes=duration)
    state["break_start"] = now
    state["break_duration"] = duration

    text = (
        f"🚬 *{md_escape(started_by)}* объявил перекур!\n"
        f"Длительность: *{duration} мин*\n"
        f"Конец в: *{state['break_end'].strftime('%H:%M:%S')}*\n\n"
        f"Остановить досрочно: /stop"
    )

    if show_join:
        msg = await reply_func(text, parse_mode="Markdown",
                               reply_markup=_build_join_keyboard())
        state["join_msg_id"] = msg.message_id if msg else None
    else:
        await reply_func(text, parse_mode="Markdown")
        state["join_msg_id"] = None

    state["end_task"] = asyncio.create_task(_remind_end(context, chat_id))


async def _remind_end(context, chat_id: int):
    state = get_state(chat_id)
    duration = state["break_duration"]
    await asyncio.sleep(duration * 60)

    if not state["break_active"]:
        return

    actual_seconds = _elapsed_seconds(state)
    state["break_active"] = False
    state["break_end"] = None
    state["next_smoke"] = datetime.now() + timedelta(hours=1)
    _record_break_end(state, actual_seconds)

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"🏁 Перекур окончен! Курили от звонка до звонка — молодцы! 💪\n"
            f"⏱ Время перекура: {fmt_seconds(actual_seconds)}\n\n"
            f"⏭ Следующий через 1 час — /next_smoke"
        ),
    )


# ─── КНОПКА «АЙДА!» ───────────────────────────────────────────────────────────

async def join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = query.message.chat_id
    user_name = query.from_user.first_name
    state = get_state(chat_id)

    if not state["break_active"]:
        await query.answer("Перекур уже закончился.", show_alert=True)
        return

    if user_name in state["break_smokers_names"]:
        await query.answer("Ты уже идёшь!", show_alert=True)
        return

    state["break_smokers_names"].add(user_name)
    _ensure_user(state, user_name)
    await query.answer(f"Ты в деле, {user_name}!")

    try:
        await query.edit_message_text(
            _join_text(state),
            parse_mode="Markdown",
            reply_markup=_build_join_keyboard(),
        )
    except BadRequest as e:
        logger.warning(f"Join edit failed: {e}")


# ─── ГОЛОСОВАНИЕ ──────────────────────────────────────────────────────────────

async def vote_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    state = get_state(chat_id)
    user = update.effective_user.first_name

    if state["break_active"]:
        await update.message.reply_text("Перекур уже идёт!")
        return

    if state["vote_active"]:
        await update.message.reply_text("Голосование уже идёт!")
        return

    duration = DEFAULT_BREAK_MINUTES
    if context.args:
        try:
            duration = int(context.args[0])
            if duration <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Укажи целое число минут, например: /vote 15")
            return

    state["vote_active"] = True
    state["vote_quorum"] = False
    state["vote_yes"] = set()
    state["vote_yes_names"] = {}
    state["vote_no"] = set()
    state["vote_duration"] = duration
    state["vote_proposer"] = user
    state["vote_starter_id"] = update.effective_user.id
    state["break_smokers_names"] = set()

    _ensure_user(state, user)
    _save_stats()

    msg = await update.message.reply_text(
        _vote_text(user, duration, [], 0),
        parse_mode="Markdown",
        reply_markup=_build_vote_keyboard(),
    )
    state["vote_msg_id"] = msg.message_id
    state["vote_task"] = asyncio.create_task(
        _vote_timeout(context, chat_id, msg.message_id)
    )


async def vote_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = query.message.chat_id
    user_id = query.from_user.id
    user_name = query.from_user.first_name
    state = get_state(chat_id)

    # ── После кворума: кнопка «Лес гооу» = присоединиться, «Не хочу» игнорируется ──
    if state["vote_quorum"]:
        if query.data == "vote_no":
            await query.answer()
            return

        if user_name in state["break_smokers_names"]:
            await query.answer("Ты уже идёшь!", show_alert=True)
            return

        state["break_smokers_names"].add(user_name)
        _ensure_user(state, user_name)
        await query.answer(f"Ты в деле, {user_name}!")

        break_end_str = state["break_end"].strftime("%H:%M:%S") if state["break_end"] else "?"
        try:
            await query.edit_message_text(
                _vote_text(state["vote_proposer"], state["vote_duration"],
                           sorted(state["break_smokers_names"]),
                           0, quorum=True, break_end_str=break_end_str),
                parse_mode="Markdown",
                reply_markup=_build_vote_keyboard(),
            )
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                logger.warning(f"Vote join edit failed: {e}")
        return

    # ── Голосование ещё идёт ──
    if not state["vote_active"]:
        await query.answer("Голосование уже закончилось.", show_alert=True)
        return

    # Снять предыдущий голос (можно переголосовать)
    state["vote_yes"].discard(user_id)
    state["vote_yes_names"].pop(user_id, None)
    state["vote_no"].discard(user_id)

    if query.data == "vote_yes":
        state["vote_yes"].add(user_id)
        state["vote_yes_names"][user_id] = user_name
    else:
        state["vote_no"].add(user_id)

    await query.answer()

    yes_names = list(state["vote_yes_names"].values())
    no = len(state["vote_no"])
    duration = state["vote_duration"]
    proposer = state["vote_proposer"]

    # Победа «за» — кворум набрался
    if len(yes_names) >= VOTES_NEEDED:
        logger.info(f"QUORUM: vote_yes_names={state['vote_yes_names']}, yes_names={yes_names}")
        state["vote_active"] = False
        state["vote_quorum"] = True
        _cancel_task(state, "vote_task")
        state["stats_votes_won"] += 1
        state["break_smokers_names"] = set(state["vote_yes_names"].values())
        state["break_starter_id"] = state.get("vote_starter_id")
        for name in state["break_smokers_names"]:
            _ensure_user(state, name)
        _save_stats()

        # Запустить перекур
        async def send(text, **kw):
            await context.bot.send_message(chat_id=chat_id, text=text, **kw)

        await _start_break(context, chat_id, duration,
                           started_by="Голосование", reply_func=send, show_join=False)

        # Обновить сообщение голосования — оставить кнопки, показать идущих
        break_end_str = state["break_end"].strftime("%H:%M:%S") if state["break_end"] else "?"
        try:
            await query.edit_message_text(
                _vote_text(proposer, duration, sorted(state["break_smokers_names"]),
                           0, quorum=True, break_end_str=break_end_str),
                parse_mode="Markdown",
                reply_markup=_build_vote_keyboard(),
            )
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                logger.warning(f"Vote quorum edit failed: {e}")
        return

    # Победа «против»
    if no >= VOTES_NEEDED:
        state["vote_active"] = False
        state["vote_quorum"] = False
        _cancel_task(state, "vote_task")
        state["stats_votes_lost"] += 1
        _save_stats()

        try:
            await query.edit_message_text(
                f"*{no}* проголосовали Против — перекур отменён! Работаем",
                parse_mode="Markdown",
            )
        except BadRequest as e:
            logger.warning(f"Vote against edit failed: {e}")
        return

    # Голосование продолжается — обновить имена
    try:
        await query.edit_message_text(
            _vote_text(proposer, duration, yes_names, no),
            parse_mode="Markdown",
            reply_markup=_build_vote_keyboard(),
        )
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            logger.warning(f"Vote update edit failed: {e}")


async def _vote_timeout(context, chat_id: int, msg_id: int):
    await asyncio.sleep(VOTE_TIMEOUT_SECONDS)
    state = get_state(chat_id)
    if not state["vote_active"] or state["vote_quorum"]:
        return

    state["vote_active"] = False
    yes = len(state["vote_yes"])
    no = len(state["vote_no"])
    state["stats_votes_lost"] += 1
    _save_stats()

    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=msg_id,
            text=f"Время вышло. За: *{yes}*, Против: *{no}* — перекур не состоялся",
            parse_mode="Markdown",
        )
    except BadRequest as e:
        logger.warning(f"Vote timeout edit failed: {e}")


# ─── СТОП ─────────────────────────────────────────────────────────────────────

async def smoke_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    state = get_state(chat_id)

    if not state["break_active"]:
        await update.message.reply_text("Нет активного перекура.")
        return

    if state.get("break_starter_id") and update.effective_user.id != state["break_starter_id"]:
        await update.message.reply_text("Только тот кто начал перекур может его остановить.")
        return

    _cancel_task(state, "end_task")

    actual_seconds = _elapsed_seconds(state)
    state["break_active"] = False
    state["break_end"] = None
    state["next_smoke"] = datetime.now() + timedelta(hours=1)
    _record_break_end(state, actual_seconds)

    await update.message.reply_text(
        f"🛑 Перекур окончен!\n"
        f"⏱ Покурили за: {fmt_seconds(actual_seconds)}\n\n"
        f"⏭ Следующий через 1 час — /next_smoke",
    )


# ─── ОБРАТНЫЙ ОТСЧЁТ ──────────────────────────────────────────────────────────

async def next_smoke(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    state = get_state(chat_id)

    if state["break_active"]:
        if state["break_end"]:
            remaining = state["break_end"] - datetime.now()
            await update.message.reply_text(
                f"Сейчас идёт перекур! Осталось *{fmt_delta(remaining)}*\n"
                f"Остановить: /stop",
                parse_mode="Markdown",
            )
        else:
            await update.message.reply_text("Сейчас идёт перекур!")
        return

    if not state["next_smoke"] or state["next_smoke"] <= datetime.now():
        await update.message.reply_text(
            "Следующий перекур не запланирован.\n"
            "Запусти /smoke или /vote"
        )
        return

    _cancel_task(state, "countdown_task")

    remaining = state["next_smoke"] - datetime.now()
    msg = await update.message.reply_text(
        f"До следующего перекура:\n\n`{fmt_delta(remaining)}`",
        parse_mode="Markdown",
    )
    state["countdown_msg_id"] = msg.message_id
    state["countdown_task"] = asyncio.create_task(
        _countdown_ticker(context, chat_id, msg.message_id)
    )


async def _countdown_ticker(context, chat_id: int, msg_id: int):
    state = get_state(chat_id)
    last_text = ""
    try:
        while True:
            await asyncio.sleep(5)
            now = datetime.now()

            if state["break_active"]:
                new_text = "Перекур начался!"
                if new_text != last_text:
                    try:
                        await context.bot.edit_message_text(
                            chat_id=chat_id, message_id=msg_id, text=new_text,
                        )
                    except BadRequest as e:
                        logger.warning(f"Countdown final edit failed: {e}")
                return

            if not state["next_smoke"] or state["next_smoke"] <= now:
                new_text = "Время перекура! Запускай /smoke или /vote"
                if new_text != last_text:
                    try:
                        await context.bot.edit_message_text(
                            chat_id=chat_id, message_id=msg_id, text=new_text,
                        )
                    except BadRequest as e:
                        logger.warning(f"Countdown final edit failed: {e}")
                return

            remaining = state["next_smoke"] - now
            new_text = f"До следующего перекура:\n\n`{fmt_delta(remaining)}`"
            if new_text != last_text:
                try:
                    await context.bot.edit_message_text(
                        chat_id=chat_id, message_id=msg_id,
                        text=new_text, parse_mode="Markdown",
                    )
                    last_text = new_text
                except BadRequest as e:
                    if "not modified" in str(e).lower():
                        last_text = new_text
                    else:
                        logger.warning(f"Countdown edit failed: {e}")
                        return

    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.warning(f"Countdown error: {e}")


# ─── СТАТИСТИКА ───────────────────────────────────────────────────────────────

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    state = get_state(chat_id)

    total = state["stats_total_breaks"]
    total_sec = state["stats_total_seconds"]
    today = state["stats_today_breaks"]
    won = state["stats_votes_won"]
    lost = state["stats_votes_lost"]
    users = {k: v for k, v in state["stats_users"].items() if v.get("count", 0) > 0}

    if users:
        top = sorted(users.items(), key=lambda x: x[1]["seconds"], reverse=True)
        medals = ["🥇", "🥈", "🥉"]
        top_lines = "\n🏆 *Кто больше всех курит:*\n" + "\n".join(
            f"  {medals[i] if i < 3 else '•'} {md_escape(name)} — "
            f"{data['count']} раз, {fmt_seconds(data['seconds'])}"
            for i, (name, data) in enumerate(top[:5])
        )
    else:
        top_lines = ""

    # Статистика магазина
    shop_total = state["stats_shop_total"]
    shop_today = state["stats_shop_today"]
    shop_initiated = {k: v for k, v in state["stats_shop_initiated"].items() if v > 0}
    shop_goers = {k: v for k, v in state["stats_shop_goers"].items() if v > 0}

    if shop_initiated:
        shop_init_top = sorted(shop_initiated.items(), key=lambda x: x[1], reverse=True)
        medals = ["🥇", "🥈", "🥉"]
        shop_init_lines = "\n🏆 *Кто чаще всех предлагал магазин:*\n" + "\n".join(
            f"  {medals[i] if i < 3 else '•'} {md_escape(name)} — {count} раз"
            for i, (name, count) in enumerate(shop_init_top[:5])
        )
    else:
        shop_init_lines = ""

    if shop_goers:
        shop_goers_top = sorted(shop_goers.items(), key=lambda x: x[1], reverse=True)
        medals = ["🥇", "🥈", "🥉"]
        shop_goers_lines = "\n🛒 *Кто чаще всех ходил в магазин:*\n" + "\n".join(
            f"  {medals[i] if i < 3 else '•'} {md_escape(name)} — {count} раз"
            for i, (name, count) in enumerate(shop_goers_top[:5])
        )
    else:
        shop_goers_lines = ""

    shop_block = ""
    if shop_total > 0 or shop_init_lines or shop_goers_lines:
        shop_block = (
            f"\n\n🏪 *Статистика магазина*\n\n"
            f"Сегодня: *{shop_today}* походов\n"
            f"Всего: *{shop_total}* походов"
            f"{shop_init_lines}"
            f"{shop_goers_lines}"
        )

    text = (
        f"📊 *Статистика перекуров*\n\n"
        f"Сегодня: *{today}* перекуров\n"
        f"Всего: *{total}* перекуров\n"
        f"Суммарно покурено: *{fmt_seconds(total_sec)}*\n\n"
        f"Голосований выиграно: *{won}*\n"
        f"Голосований провалено: *{lost}*\n"
        f"{top_lines}"
        f"{shop_block}"
    )

    await update.message.reply_text(text, parse_mode="Markdown")


# ─── МАГАЗИН ──────────────────────────────────────────────────────────────────

async def shop_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    state = get_state(chat_id)
    user = update.effective_user.first_name

    if state["shop_active"]:
        await update.message.reply_text("Поход в магазин уже идёт!")
        return

    if state["shop_vote_active"]:
        await update.message.reply_text("Голосование за магазин уже идёт!")
        return

    state["shop_vote_active"] = True
    state["shop_vote_quorum"] = False
    state["shop_yes"] = set()
    state["shop_yes_names"] = {}
    state["shop_no"] = set()
    state["shop_proposer"] = user
    state["shop_starter_id"] = update.effective_user.id
    state["shop_goers_names"] = {user}

    # Записать инициатора
    state["stats_shop_initiated"][user] = state["stats_shop_initiated"].get(user, 0) + 1
    _save_stats()

    msg = await update.message.reply_text(
        _shop_vote_text(user, [user], 0),
        parse_mode="Markdown",
        reply_markup=_build_shop_keyboard(),
    )
    state["shop_msg_id"] = msg.message_id
    state["shop_vote_task"] = asyncio.create_task(
        _shop_vote_timeout(context, chat_id, msg.message_id)
    )


async def shop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = query.message.chat_id
    user_id = query.from_user.id
    user_name = query.from_user.first_name
    state = get_state(chat_id)

    # После кворума: «Иду!» = присоединиться, «Не пойду» игнорируется
    if state["shop_vote_quorum"]:
        if query.data == "shop_no":
            await query.answer()
            return

        if user_name in state["shop_goers_names"]:
            await query.answer("Ты уже идёшь!", show_alert=True)
            return

        state["shop_goers_names"].add(user_name)
        state["stats_shop_goers"][user_name] = state["stats_shop_goers"].get(user_name, 0) + 1
        _save_stats()
        await query.answer(f"Ты в деле, {user_name}!")

        try:
            await query.edit_message_text(
                _shop_vote_text(state["shop_proposer"],
                                sorted(state["shop_goers_names"]),
                                0, quorum=True),
                parse_mode="Markdown",
                reply_markup=_build_shop_keyboard(),
            )
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                logger.warning(f"Shop join edit failed: {e}")
        return

    if not state["shop_vote_active"]:
        await query.answer("Голосование уже закончилось.", show_alert=True)
        return

    # Снять предыдущий голос
    state["shop_yes"].discard(user_id)
    state["shop_yes_names"].pop(user_id, None)
    state["shop_no"].discard(user_id)

    if query.data == "shop_yes":
        state["shop_yes"].add(user_id)
        state["shop_yes_names"][user_id] = user_name
    else:
        state["shop_no"].add(user_id)

    await query.answer()

    yes_names = list(state["shop_yes_names"].values())
    no = len(state["shop_no"])
    proposer = state["shop_proposer"]

    # Победа «за»
    if len(yes_names) >= VOTES_NEEDED:
        state["shop_vote_active"] = False
        state["shop_vote_quorum"] = True
        state["shop_active"] = True
        _cancel_task(state, "shop_vote_task")
        state["shop_goers_names"] = set(state["shop_yes_names"].values())

        # Записать поход и участников
        today = datetime.now().date()
        if state["stats_shop_today_date"] != today:
            state["stats_shop_today_date"] = today
            state["stats_shop_today"] = 0
        state["stats_shop_total"] += 1
        state["stats_shop_today"] += 1
        for name in state["shop_goers_names"]:
            state["stats_shop_goers"][name] = state["stats_shop_goers"].get(name, 0) + 1
        _save_stats()

        try:
            await query.edit_message_text(
                _shop_vote_text(proposer, sorted(state["shop_goers_names"]),
                                0, quorum=True),
                parse_mode="Markdown",
                reply_markup=_build_shop_keyboard(),
            )
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                logger.warning(f"Shop quorum edit failed: {e}")

        await context.bot.send_message(
            chat_id=chat_id,
            text="Поход в магазин объявлен! Завершить: /shop_stop",
        )
        return

    # Победа «против»
    if no >= VOTES_NEEDED:
        state["shop_vote_active"] = False
        state["shop_vote_quorum"] = False
        _cancel_task(state, "shop_vote_task")

        try:
            await query.edit_message_text(
                f"*{no}* проголосовали Против — в магазин не идём!",
                parse_mode="Markdown",
            )
        except BadRequest as e:
            logger.warning(f"Shop against edit failed: {e}")
        return

    # Голосование продолжается
    try:
        await query.edit_message_text(
            _shop_vote_text(proposer, yes_names, no),
            parse_mode="Markdown",
            reply_markup=_build_shop_keyboard(),
        )
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            logger.warning(f"Shop vote update failed: {e}")


async def _shop_vote_timeout(context, chat_id: int, msg_id: int):
    await asyncio.sleep(VOTE_TIMEOUT_SECONDS)
    state = get_state(chat_id)
    if not state["shop_vote_active"] or state["shop_vote_quorum"]:
        return

    state["shop_vote_active"] = False
    yes = len(state["shop_yes"])
    no = len(state["shop_no"])

    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=msg_id,
            text=f"Время вышло. За: *{yes}*, Против: *{no}* — в магазин не идём",
            parse_mode="Markdown",
        )
    except BadRequest as e:
        logger.warning(f"Shop timeout edit failed: {e}")


async def shop_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    state = get_state(chat_id)

    if not state["shop_active"] and not state["shop_vote_active"]:
        await update.message.reply_text("Нет активного похода в магазин.")
        return

    if state.get("shop_starter_id") and update.effective_user.id != state["shop_starter_id"]:
        await update.message.reply_text("Только тот кто начал поход может его отменить.")
        return

    _cancel_task(state, "shop_vote_task")
    state["shop_active"] = False
    state["shop_vote_active"] = False
    state["shop_vote_quorum"] = False

    await update.message.reply_text("Поход в магазин отменён!")


# ─── ПОМОЩЬ ───────────────────────────────────────────────────────────────────

async def smoke_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Бот перекуров — команды:\n\n"
        "/smoke — перекур на 10 мин\n"
        "/smoke 15 — перекур на 15 мин\n"
        "/vote — голосование за перекур (нужно 2 из 4)\n"
        "/vote 15 — голосование за 15-минутный перекур\n"
        "/stop — остановить перекур досрочно\n"
        "/next_smoke — таймер до следующего перекура\n"
        "/stats — статистика перекуров\n\n"
        "/shop — голосование за поход в магазин\n"
        "/shop_stop — отменить поход в магазин",
    )


# ─── ЕЖЕДНЕВНЫЙ ПОДЫТОГ ───────────────────────────────────────────────────────

def _daily_summary_text(state: dict) -> str | None:
    """Сформировать текст подытога за сегодня. Вернуть None если активности не было."""
    today_breaks = state["stats_today_breaks"]
    today_shops = state["stats_shop_today"]

    if today_breaks == 0 and today_shops == 0:
        return None

    lines = ["📋 *Итоги дня*\n"]

    # Перекуры
    if today_breaks > 0:
        lines.append(f"🚬 Перекуров сегодня: *{today_breaks}*")

        # Топ курильщиков за сегодня — берём из stats_users тех у кого count > 0
        # (точный подсчёт за сегодня не ведём отдельно, показываем общий топ)
        users = {k: v for k, v in state["stats_users"].items() if v.get("count", 0) > 0}
        if users:
            top = sorted(users.items(), key=lambda x: x[1]["count"], reverse=True)[:3]
            medals = ["🥇", "🥈", "🥉"]
            lines.append("Чаще всех курили: " + ", ".join(
                f"{medals[i]} {md_escape(name)}"
                for i, (name, _) in enumerate(top)
            ))

    # Магазин
    if today_shops > 0:
        lines.append(f"\n🛒 Походов в магазин: *{today_shops}*")

        goers = {k: v for k, v in state["stats_shop_goers"].items() if v > 0}
        if goers:
            top = sorted(goers.items(), key=lambda x: x[1], reverse=True)[:3]
            medals = ["🥇", "🥈", "🥉"]
            lines.append("Чаще всех ходили: " + ", ".join(
                f"{medals[i]} {md_escape(name)}"
                for i, (name, _) in enumerate(top)
            ))

    return "\n".join(lines)


async def _send_daily_summaries(app: Application):
    """Отправить подытог во все чаты где была активность сегодня."""
    for chat_id, state in list(chat_states.items()):
        text = _daily_summary_text(state)
        if text is None:
            continue
        try:
            await app.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.warning(f"Не удалось отправить подытог в чат {chat_id}: {e}")


async def _daily_summary_loop(app: Application):
    """Фоновая задача — каждый день в 18:00 отправляет подытог."""
    while True:
        now = datetime.now()
        target = now.replace(hour=18, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        wait_seconds = (target - now).total_seconds()
        logger.info(f"Следующий подытог через {wait_seconds:.0f} сек ({target.strftime('%Y-%m-%d %H:%M')})")
        await asyncio.sleep(wait_seconds)
        await _send_daily_summaries(app)


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "Укажи токен бота!\n"
            "  Mac/Linux: BOT_TOKEN='токен' python3 smoke_bot.py"
        )

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("smoke", smoke_start))
    app.add_handler(CommandHandler("vote", vote_start))
    app.add_handler(CommandHandler("stop", smoke_stop))
    app.add_handler(CommandHandler("next_smoke", next_smoke))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("shop", shop_start))
    app.add_handler(CommandHandler("shop_stop", shop_stop))
    app.add_handler(CommandHandler("start", smoke_help))
    app.add_handler(CommandHandler("help", smoke_help))
    app.add_handler(CallbackQueryHandler(vote_callback, pattern="^vote_(yes|no)$"))
    app.add_handler(CallbackQueryHandler(join_callback, pattern="^join_smoke$"))
    app.add_handler(CallbackQueryHandler(shop_callback, pattern="^shop_(yes|no)$"))

    # Запустить фоновый цикл подытога после старта приложения
    async def post_init(application: Application):
        asyncio.create_task(_daily_summary_loop(application))

    app.post_init = post_init

    logger.info("Бот запущен! Нажми Ctrl+C для остановки.")
    app.run_polling()


if __name__ == "__main__":
    main()
